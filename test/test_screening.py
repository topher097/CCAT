"""Tests for module 2 — pre-audit screening (§6)."""

import json
from types import SimpleNamespace
from typing import Any

import polars as pl
import pytest

import ccat.audit.store as store
import ccat.preparation as preparation
import ccat.screening as screening
from ccat.audit.store import FINAL_CHILD_COLUMNS, build_final_frame, read_final
from ccat.config import Config
from ccat.io import list_sheets, read_sheet, write_sheets
from ccat.screening import (
    RESPONSE_COLUMNS,
    AdjChildRow,
    build_prompt,
    parse_model_output,
    run,
    screen_child,
    screen_children,
    validate_adj_response,
)


@pytest.fixture(autouse=True)
def fake_opencode_v2(monkeypatch) -> None:
    """Assume opencode v2 is installed unless a test overrides it."""

    class Result:
        stdout = "2.0.0"
        stderr = ""

    monkeypatch.setattr(
        screening.subprocess, "run", lambda *args, **kwargs: Result()
    )


@pytest.fixture(autouse=True)
def no_external_checks(monkeypatch) -> None:
    """Keep run() tests offline: stub the ZeroBounce/Nominatim checks."""
    monkeypatch.setattr(screening, "check_emails", lambda cfg, rows: [])
    monkeypatch.setattr(screening, "check_addresses", lambda cfg, rows: [])


#: The real check functions, captured before the autouse stub replaces
#: them (the dedicated check tests restore these).
_REAL_CHECK_EMAILS = screening.check_emails
_REAL_CHECK_ADDRESSES = screening.check_addresses


def child(**overrides: Any) -> dict[str, Any]:
    """One raw_child row for screening tests (all config columns)."""
    base = {
        "zite_record_id": "rec_c1",
        "zite_created_at": "2026-02-01T09:00:00+00:00",
        "created": "2026-02-01T09:00:00+00:00",
        "first_name": "Kiddo",
        "primary_email": "fam@example.com",
        "dob": "2018-05-10",
        "age_at_xmas": 8,
        "age_reported": 7,
        "gender": "Male",
        "special_needs": None,
        "need_1": "winter coat taht fits",
        "need_2": None,
        "desire_1": "legos",
        "desire_2": None,
        "shoe_size": "13",
        "shoe_size_type": "Youth",
        "pant_waist_size": '24"',
        "pant_inseam": '20"',
        "pant_size_letter": "XS",
        "pant_size_type": "Youth",
        "jean_size": "12 slim",
        "shirt_size_letter": "XS",
        "shirt_size_type": "Youth",
        "favorite_colors": "blue",
        "interests": "trains",
        "other_notes": None,
        "source": "web",
        "family_id": 1,
        "angel_id": "1A",
    }
    base.update(overrides)
    return base


def sponsor_row(**overrides: Any) -> dict[str, Any]:
    """One raw_sponsor row (all config columns) for run() fixtures."""
    base = {
        "zite_record_id": "rec_s1",
        "create": "2026-01-01T10:00:00+00:00",
        "email": "sponsor@example.com",
        "accepts_disclaimer": True,
        "sponsor_type": "Individual",
        "organization_name": None,
        "prior_assigned_angel_ids": None,
        "first_name": "Mary",
        "last_name": "Sponsor",
        "phone": "(906) 555-1234",
        "num_children": 2,
        "age_preference": "Any Age",
        "gender_preference": "Any Gender",
        "dropoff_location": "Hancock",
        "source": "web",
    }
    base.update(overrides)
    return base


def family_row(**overrides: Any) -> dict[str, Any]:
    """One raw_family row (all config columns) for run() fixtures."""
    base = {
        "zite_record_id": "rec_f1",
        "zite_created_at": "2026-01-05T10:00:00+00:00",
        "created": "2026-01-05T10:00:00+00:00",
        "primary_email": "fam@example.com",
        "accepts_disclaimer": True,
        "first_name": "Jane",
        "last_name": "Doe",
        "phone": "(906) 555-9876",
        "street_address": "1 Main St",
        "apt_floor": None,
        "city": "Hancock",
        "zip_code": "49930",
        "state": "Michigan",
        "secondary_first_name": None,
        "secondary_last_name": None,
        "secondary_email": None,
        "secondary_phone": None,
        "gift_pickup_location": "TSA (Hancock)",
        "source": "web",
    }
    base.update(overrides)
    return base


def full_response(record_id: str = "rec_c1") -> dict[str, Any]:
    """Build a well-formed model response for the test child."""
    return {column: None for column in RESPONSE_COLUMNS} | {
        "zite_record_id": record_id,
        "need_1": "winter coat that fits",
        "potential_anomoly": "age_reported 7 differs from computed 8",
    }


def test_build_prompt_excludes_name_and_contacts(screen_cfg: Config) -> None:
    """The prompt never contains the name or contact details (§6.2)."""
    prompt = build_prompt(child(), screen_cfg)
    assert "Kiddo" not in prompt
    assert "first_name" not in prompt
    assert "fam@example.com" not in prompt
    assert "primary_email" not in prompt
    assert "rec_c1" in prompt
    assert "2018-05-10" in prompt
    assert '"age_at_xmas": 8' in prompt
    # {{xmas_date}} substituted from config
    assert "{{xmas_date}}" not in prompt
    assert "12/25/2026" in prompt


def test_build_prompt_includes_free_text_context_fields(
    screen_cfg: Config,
) -> None:
    """interests, favorite_colors, other_notes are prompt inputs (§6.2)."""
    prompt = build_prompt(child(), screen_cfg)
    assert '"interests": "trains"' in prompt
    assert '"favorite_colors": "blue"' in prompt
    assert '"other_notes"' in prompt


def test_build_prompt_contains_sizes_needs_and_reported_age(
    screen_cfg: Config,
) -> None:
    """All screening-relevant fields reach the prompt (§6.2)."""
    prompt = build_prompt(child(), screen_cfg)
    for value in (
        "13",
        "Youth",
        '24\\"',
        "12 slim",
        "legos",
        "winter coat taht fits",
    ):
        assert value in prompt
    assert '"age_reported": 7' in prompt


def test_parse_model_output_literal_null() -> None:
    """The literal null response parses to None (no changes)."""
    assert parse_model_output("null") is None
    assert parse_model_output("  null\n") is None


def test_parse_model_output_json_object() -> None:
    """A JSON object response parses to a dict."""
    parsed = parse_model_output(json.dumps(full_response()))
    assert parsed is not None
    assert parsed["zite_record_id"] == "rec_c1"


def test_parse_model_output_rejects_garbage() -> None:
    """Non-JSON responses raise ValueError."""
    with pytest.raises(ValueError, match="not a JSON object"):
        parse_model_output("the child seems fine")


def test_parse_model_output_recovers_fenced_json() -> None:
    """Markdown fences around the JSON are tolerated defensively."""
    text = "```json\n" + json.dumps(full_response()) + "\n```"
    parsed = parse_model_output(text)
    assert parsed is not None and parsed["zite_record_id"] == "rec_c1"


def test_validate_adj_response_requires_exact_key_set(
    screen_cfg: Config,
) -> None:
    """Extra or missing keys are rejected (§6.2)."""
    parsed = full_response()
    parsed["extra"] = "x"
    with pytest.raises(ValueError, match="key set"):
        validate_adj_response(parsed, child(), screen_cfg)
    del parsed["dob"]
    with pytest.raises(ValueError, match="key set"):
        validate_adj_response(parsed, child(), screen_cfg)


def test_validate_adj_response_checks_enum_vocab(screen_cfg: Config) -> None:
    """Size corrections must use exact Zite vocabulary labels (§6.2)."""
    parsed = full_response()
    parsed["shoe_size"] = "13.5x"  # not a valid label
    with pytest.raises(ValueError, match="vocabulary"):
        validate_adj_response(parsed, child(), screen_cfg)


def test_validate_adj_response_checks_dob_format(screen_cfg: Config) -> None:
    """A proposed dob must be YYYY-MM-DD."""
    parsed = full_response()
    parsed["dob"] = "05/10/2018"
    with pytest.raises(ValueError, match="dob"):
        validate_adj_response(parsed, child(), screen_cfg)


def test_validate_adj_response_checks_record_id(screen_cfg: Config) -> None:
    """The response must join back to the same child."""
    with pytest.raises(ValueError, match="zite_record_id"):
        validate_adj_response(full_response("other"), child(), screen_cfg)


def test_screen_child_returns_adjudicated_row(screen_cfg: Config) -> None:
    """A valid response becomes sparse, validated corrections (§6.3)."""
    send = lambda prompt: json.dumps(full_response())  # noqa: E731
    row = screen_child(child(), screen_cfg, send)
    assert row is not None
    assert row["zite_record_id"] == "rec_c1"
    assert row["need_1"] == "winter coat that fits"
    assert row["need_2"] is None  # no change -> null


def test_screen_child_literal_null_writes_all_null_row(
    screen_cfg: Config,
) -> None:
    """A literal null response still records a screened row (§6.3)."""
    row = screen_child(child(), screen_cfg, lambda prompt: "null")
    assert row is not None
    assert row["zite_record_id"] == "rec_c1"
    assert all(
        row[column] is None
        for column in FINAL_CHILD_COLUMNS
        if column != "zite_record_id"
    )


def test_screen_child_null_response_has_no_age_key(screen_cfg: Config) -> None:
    """The model response leaves final age computation to dense_final_row."""
    row = screen_child(child(), screen_cfg, lambda prompt: "null")
    assert row is not None
    assert "age_at_xmas" in row  # sheet column exists
    assert row["age_at_xmas"] is None  # filled by run(), not the model


def test_run_initial_ai_success_marks_full_final_rows_screened(
    screen_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Initial AI success densifies final rows and sets screened=True."""
    _seed_workbook(
        screen_cfg,
        children=[
            child(zite_record_id="f1c1", angel_id="1A"),
            child(zite_record_id="f1c2", first_name="Two", angel_id="1B"),
        ],
    )
    KeyedFakeOpencode.responses = {
        "f1c1": "null",
        "f1c2": json.dumps(full_response("f1c2")),
    }
    _use_keyed_opencode(monkeypatch)

    run(screen_cfg)

    final = read_sheet(screen_cfg.excel_file, "final_child")
    assert final is not None and final.height == 2
    assert final.columns == list(FINAL_CHILD_COLUMNS)
    by_id = _by_id(final)
    assert by_id["f1c1"]["need_1"] == "winter coat taht fits"
    assert by_id["f1c1"]["screened"] is True
    assert by_id["f1c1"]["status"] == "new"
    assert by_id["f1c1"]["age_at_xmas"] == 8
    assert by_id["f1c1"]["generated_info_sheet"] is False
    assert by_id["f1c2"]["need_1"] == "winter coat that fits"
    assert by_id["f1c2"]["screened"] is True
    assert sorted(KeyedFakeOpencode.calls) == ["f1c1", "f1c2"]


def test_run_immediate_rerun_refreshes_global_logs_without_ai(
    screen_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An immediate rerun makes no AI calls but replaces global diagnostics."""
    _seed_workbook(screen_cfg)
    KeyedFakeOpencode.responses = {"rec_c1": "null", "rec_c2": "null"}
    _use_keyed_opencode(monkeypatch)
    run(screen_cfg)
    before = _rows(screen_cfg, "final_child")
    calls_before = list(KeyedFakeOpencode.calls)
    _poison_global_logs(screen_cfg)

    run(screen_cfg)

    assert KeyedFakeOpencode.calls == calls_before
    assert _rows(screen_cfg, "final_child") == before
    duplicate_log = read_sheet(screen_cfg.excel_file, "duplicate_log")
    dob_log = read_sheet(screen_cfg.excel_file, "dob_conflict_log")
    assert duplicate_log is not None and duplicate_log.height == 0
    assert dob_log is not None and dob_log.height == 1


def test_run_targets_only_final_children_with_screened_false(
    screen_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stored presence never suppresses AI; final_child.screened controls it."""
    _seed_workbook(
        screen_cfg,
        final_children=[
            final_child_row(screened=False),
            final_child_row(
                child(zite_record_id="rec_c2", first_name="Two"),
                need_1="human already finished",
                screened=True,
            ),
        ],
    )
    KeyedFakeOpencode.responses = {"rec_c1": "null"}
    _use_keyed_opencode(monkeypatch)

    run(screen_cfg)

    by_id = _by_id(read_sheet(screen_cfg.excel_file, "final_child"))
    assert KeyedFakeOpencode.calls == ["rec_c1"]
    assert set(by_id) == {"rec_c1", "rec_c2"}
    assert by_id["rec_c1"]["screened"] is True
    assert by_id["rec_c2"]["screened"] is True
    assert by_id["rec_c2"]["need_1"] == "human already finished"


def test_run_preserves_human_edits_and_controls(
    screen_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AI overlays untouched fields only and preserves final human controls."""
    _seed_workbook(
        screen_cfg,
        children=[child(zite_record_id="f1c1", angel_id="1A")],
        final_children=[
            final_child_row(
                child(zite_record_id="f1c1"),
                need_1="human-fixed coat",
                clarification_notes="call the family",
                screened=False,
            )
        ],
    )
    KeyedFakeOpencode.responses = {
        "f1c1": json.dumps(
            full_response("f1c1")
            | {"need_1": "model coat", "desire_1": "LEGO set"}
        )
    }
    _use_keyed_opencode(monkeypatch)

    run(screen_cfg)

    row = _rows(screen_cfg, "final_child")[0]
    assert row["need_1"] == "human-fixed coat"
    assert row["desire_1"] == "LEGO set"
    assert row["clarification_notes"] == "call the family"
    assert row["screened"] is True


def test_screen_child_retries_once_then_recovers(screen_cfg: Config) -> None:
    """A malformed response is retried once (§6.2)."""
    responses = iter(["garbage", json.dumps(full_response())])
    prompts: list[str] = []

    def send(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    row = screen_child(child(), screen_cfg, send)
    assert row is not None
    assert len(prompts) == 2


def test_screen_child_gives_up_after_second_failure(screen_cfg: Config) -> None:
    """After a failed retry the child is left unscreened (§6.2)."""
    send = lambda prompt: "still garbage"  # noqa: E731
    assert screen_child(child(), screen_cfg, send) is None


class FakeOpencode:
    """Minimal stand-in for the opencode client used by screening.run."""

    instances: list["FakeOpencode"] = []
    responses: list[str | BaseException] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Record the fake session resource."""
        self.session = SimpleNamespace(
            create=lambda model: SimpleNamespace(id="ses_test"),
            generate=self._generate,
        )
        FakeOpencode.instances.append(self)

    def _generate(self, session_id: str, prompt: str) -> SimpleNamespace:
        """Return the next canned response or raise a canned interruption."""
        response = FakeOpencode.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return SimpleNamespace(text=response)

    def __enter__(self) -> "FakeOpencode":
        """Return self as the context manager target."""
        return self

    def __exit__(self, *args: Any) -> None:
        """Swallow exceptions like the real client does."""
        return None


def final_child_row(
    source: dict[str, Any] | None = None, **overrides: Any
) -> dict[str, Any]:
    """Build one full persistent child fixture with initialized controls."""
    row = child() | {
        "potential_anomoly": None,
        "age_at_xmas": 8,
        "generated_info_sheet": False,
        "screened": False,
        "potential_duplicate": False,
        "status": "new",
        "ignore": False,
        "clarification_notes": None,
        "audited_at": None,
    }
    row.update(source or {})
    row.update(overrides)
    return row


def final_family_row(
    source: dict[str, Any] | None = None, **overrides: Any
) -> dict[str, Any]:
    """Build one full persistent family fixture with initialized controls."""
    row = family_row() | {
        "family_id": 1,
        "potential_duplicate": False,
        "assigned_sponsor_id": None,
        "assigned_angel_ids": None,
        "num_angels_in_family": None,
        "status": "new",
        "ignore": False,
        "clarification_notes": None,
        "audited_at": None,
    }
    row.update(source or {})
    row.update(overrides)
    return row


def final_sponsor_row(
    source: dict[str, Any] | None = None, **overrides: Any
) -> dict[str, Any]:
    """Build one full persistent sponsor fixture with initialized controls."""
    row = sponsor_row() | {
        "sponsor_id": 1,
        "potential_duplicate": False,
        "pref_age_min": None,
        "pref_age_max": None,
        "assigned_angel_ids": None,
        "num_angels_assigned": None,
        "status": "new",
        "ignore": False,
        "clarification_notes": None,
        "audited_at": None,
    }
    row.update(source or {})
    row.update(overrides)
    return row


def _seed_workbook(
    cfg: Config,
    children: list[dict[str, Any]] | None = None,
    families: list[dict[str, Any]] | None = None,
    sponsors: list[dict[str, Any]] | None = None,
    final_children: list[dict[str, Any]] | None = None,
    final_families: list[dict[str, Any]] | None = None,
    final_sponsors: list[dict[str, Any]] | None = None,
) -> None:
    """Seed full raw and persistent-final sheets through migration ownership."""
    children = (
        [
            child(),
            child(zite_record_id="rec_c2", first_name="Two", angel_id="1B"),
        ]
        if children is None
        else children
    )
    families = [family_row()] if families is None else families
    sponsors = [sponsor_row()] if sponsors is None else sponsors
    sheets = {
        "raw_sponsor": pl.DataFrame(
            sponsors,
            schema={key: _raw_dtype(key) for key in sponsor_row()},
        ),
        "raw_family": pl.DataFrame(
            families,
            schema={key: _raw_dtype(key) for key in family_row()},
        ),
        "raw_child": pl.DataFrame(
            children,
            schema={key: _raw_dtype(key) for key in child()},
        ),
    }
    for entity, rows in (
        ("child", final_children),
        ("family", final_families),
        ("sponsor", final_sponsors),
    ):
        if rows is not None:
            sheets[f"final_{entity}"] = build_final_frame(entity, rows)
    write_sheets(
        cfg.excel_file,
        "migrate",
        sheets,
        xmas=cfg.xmas if final_children is not None else None,
    )


def _raw_dtype(key: str) -> Any:
    """Return a stable worksheet dtype for one raw fixture field."""
    if key in {
        "num_children",
        "age_reported",
        "family_id",
        "sponsor_id",
        "age_at_xmas",
    }:
        return pl.Int64
    if key == "accepts_disclaimer":
        return pl.Boolean
    return pl.String


def _rows(cfg: Config, sheet: str) -> list[dict[str, Any]]:
    """Read a required fixture sheet as dictionaries."""
    frame = read_sheet(cfg.excel_file, sheet)
    assert frame is not None
    return frame.to_dicts()


def _by_id(frame: pl.DataFrame | None) -> dict[str, dict[str, Any]]:
    """Index one required child frame by zite_record_id."""
    assert frame is not None
    return {str(row["zite_record_id"]): row for row in frame.to_dicts()}


def _use_keyed_opencode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the deterministic offline opencode fake."""
    import opencode

    KeyedFakeOpencode.calls.clear()
    monkeypatch.setattr(opencode, "Opencode", KeyedFakeOpencode)


def _replace_final_rows(
    cfg: Config, entity: str, rows: list[dict[str, Any]]
) -> None:
    """Replace one full final fixture without legacy projected writes."""
    write_sheets(
        cfg.excel_file,
        "migrate",
        {f"final_{entity}": build_final_frame(entity, rows)},
        xmas=cfg.xmas if entity == "child" else None,
    )


def _poison_global_logs(cfg: Config) -> None:
    """Put stale content into logs that every screen run must refresh."""
    write_sheets(
        cfg.excel_file,
        "screen",
        {
            "duplicate_log": pl.DataFrame({"stale": ["duplicate"]}),
            "dob_conflict_log": pl.DataFrame({"stale": ["dob"]}),
        },
    )


def _assert_no_legacy_sheets(cfg: Config) -> None:
    """Assert that run never creates submissions or adj persistence sheets."""
    present = set(list_sheets(cfg.excel_file))
    assert present.isdisjoint(
        {
            "child_submissions",
            "family_submissions",
            "sponsor_submissions",
            "adj_child",
            "adj_family",
            "adj_sponsor",
        }
    )


def test_run_no_change_preserves_edits_assignments_and_generated(
    screen_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A no-change rerun leaves every entity's human/module state untouched."""
    _seed_workbook(screen_cfg, children=[child()])
    KeyedFakeOpencode.responses = {"rec_c1": "null"}
    _use_keyed_opencode(monkeypatch)
    run(screen_cfg)

    child_rows = _rows(screen_cfg, "final_child")
    child_rows[0].update(
        need_1="human edit", generated_info_sheet=True, audited=True
    )
    _replace_final_rows(screen_cfg, "child", child_rows)
    family_rows = _rows(screen_cfg, "final_family")
    family_rows[0].update(
        first_name="Human Family",
        assigned_sponsor_id=42,
        assigned_angel_ids="1A",
        num_angels_in_family=1,
    )
    _replace_final_rows(screen_cfg, "family", family_rows)
    sponsor_rows = _rows(screen_cfg, "final_sponsor")
    sponsor_rows[0].update(
        phone="human phone", assigned_angel_ids="1A", num_angels_assigned=1
    )
    _replace_final_rows(screen_cfg, "sponsor", sponsor_rows)
    before = {
        entity: _rows(screen_cfg, f"final_{entity}")
        for entity in ("child", "family", "sponsor")
    }
    calls = list(KeyedFakeOpencode.calls)

    run(screen_cfg)

    assert KeyedFakeOpencode.calls == calls
    assert {
        entity: _rows(screen_cfg, f"final_{entity}")
        for entity in ("child", "family", "sponsor")
    } == before


def test_run_later_entities_append_without_renumbering(
    screen_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Later child/family/sponsor records append with fresh stable IDs only."""
    _seed_workbook(screen_cfg, children=[child()])
    KeyedFakeOpencode.responses = {"rec_c1": "null"}
    _use_keyed_opencode(monkeypatch)
    run(screen_cfg)
    original = {
        entity: _rows(screen_cfg, f"final_{entity}")
        for entity in ("child", "family", "sponsor")
    }

    _seed_workbook(
        screen_cfg,
        children=[
            child(),
            child(
                zite_record_id="rec_c3",
                first_name="Third",
                primary_email="fam2@example.com",
                family_id=2,
                angel_id="2A",
            ),
        ],
        families=[
            family_row(),
            family_row(
                zite_record_id="rec_f2",
                primary_email="fam2@example.com",
                created="2026-01-06T10:00:00+00:00",
                zite_created_at="2026-01-06T10:00:00+00:00",
            ),
        ],
        sponsors=[
            sponsor_row(),
            sponsor_row(
                zite_record_id="rec_s2",
                email="sponsor2@example.com",
                create="2026-01-02T10:00:00+00:00",
            ),
        ],
    )
    KeyedFakeOpencode.responses = {"rec_c3": "null"}

    run(screen_cfg)

    children = _rows(screen_cfg, "final_child")
    families = _rows(screen_cfg, "final_family")
    sponsors = _rows(screen_cfg, "final_sponsor")
    assert [row["zite_record_id"] for row in children] == ["rec_c1", "rec_c3"]
    assert children[0] == original["child"][0]
    new_child = _by_id(read_sheet(screen_cfg.excel_file, "final_child"))[
        "rec_c3"
    ]
    assert (new_child["family_id"], new_child["angel_id"]) == (2, "2A")
    assert new_child["screened"] is True
    assert [row["family_id"] for row in families] == [1, 2]
    assert families[0] == original["family"][0]
    assert [row["sponsor_id"] for row in sponsors] == [1, 2]
    assert sponsors[0] == original["sponsor"][0]
    assert KeyedFakeOpencode.calls == ["rec_c1", "rec_c3"]


def test_run_new_duplicate_never_steals_existing_angel(
    screen_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later duplicate gets no angel already owned by its existing group."""
    _seed_workbook(screen_cfg, children=[child()])
    KeyedFakeOpencode.responses = {"rec_c1": "null"}
    _use_keyed_opencode(monkeypatch)
    run(screen_cfg)

    _seed_workbook(
        screen_cfg,
        children=[
            child(),
            child(
                zite_record_id="rec_c2",
                created="2026-02-02T09:00:00+00:00",
                zite_created_at="2026-02-02T09:00:00+00:00",
            ),
        ],
    )
    KeyedFakeOpencode.responses = {"rec_c2": "null"}
    run(screen_cfg)

    by_id = _by_id(read_sheet(screen_cfg.excel_file, "final_child"))
    assert by_id["rec_c1"]["angel_id"] == "1A"
    assert by_id["rec_c2"]["angel_id"] is None
    assert by_id["rec_c1"]["potential_duplicate"] is True
    assert by_id["rec_c2"]["potential_duplicate"] is True
    assert KeyedFakeOpencode.calls == ["rec_c1", "rec_c2"]


@pytest.mark.parametrize("mode", ["redo", "overwrite"])
def test_run_redo_and_overwrite_protect_human_state(
    screen_cfg: Config, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Explicit re-screening preserves edits, controls, and row identity."""
    _seed_workbook(screen_cfg)
    KeyedFakeOpencode.responses = {"rec_c1": "null", "rec_c2": "null"}
    _use_keyed_opencode(monkeypatch)
    run(screen_cfg)
    rows = _rows(screen_cfg, "final_child")
    rows[0].update(
        need_1="human-fixed coat",
        clarification_notes="call the family",
        generated_info_sheet=True,
    )
    rows[1].update(clarification_notes="second child human note")
    _replace_final_rows(screen_cfg, "child", rows)
    KeyedFakeOpencode.responses = {
        "rec_c1": json.dumps(
            full_response("rec_c1")
            | {"need_1": "model coat", "desire_1": "LEGO set"}
        ),
        "rec_c2": "null",
    }
    calls_before = len(KeyedFakeOpencode.calls)

    if mode == "redo":
        run(screen_cfg, redo="rec_c1")
        expected_calls = calls_before + 1
    else:
        run(screen_cfg, overwrite=True)
        expected_calls = calls_before + 2

    by_id = _by_id(read_sheet(screen_cfg.excel_file, "final_child"))
    assert len(by_id) == 2
    assert len(KeyedFakeOpencode.calls) == expected_calls
    assert by_id["rec_c1"]["need_1"] == "human-fixed coat"
    assert by_id["rec_c1"]["desire_1"] == "LEGO set"
    assert by_id["rec_c1"]["clarification_notes"] == "call the family"
    assert by_id["rec_c1"]["generated_info_sheet"] is True
    assert by_id["rec_c1"]["screened"] is True
    assert by_id["rec_c2"]["clarification_notes"] == "second child human note"
    assert by_id["rec_c2"]["screened"] is True


def test_run_redo_unknown_child_fails(screen_cfg: Config, monkeypatch) -> None:
    """Redo of an unknown zite_record_id fails fast."""
    _seed_workbook(screen_cfg)
    import opencode

    monkeypatch.setattr(opencode, "Opencode", FakeOpencode)
    with pytest.raises(screening.ScreeningError, match="redo target"):
        run(screen_cfg, redo="nope")


def test_run_failed_redo_keeps_row_and_ordinary_run_retries(
    screen_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed redo persists screened=False without deleting human work."""
    _seed_workbook(screen_cfg, children=[child()])
    KeyedFakeOpencode.responses = {"rec_c1": "null"}
    _use_keyed_opencode(monkeypatch)
    run(screen_cfg)
    rows = _rows(screen_cfg, "final_child")
    rows[0].update(need_1="human-fixed coat", clarification_notes="keep me")
    _replace_final_rows(screen_cfg, "child", rows)

    KeyedFakeOpencode.responses = {"rec_c1": "garbage"}
    run(screen_cfg, redo="rec_c1")

    failed = _rows(screen_cfg, "final_child")
    assert len(failed) == 1
    assert failed[0]["zite_record_id"] == "rec_c1"
    assert failed[0]["screened"] is False
    assert failed[0]["need_1"] == "human-fixed coat"
    assert failed[0]["clarification_notes"] == "keep me"

    KeyedFakeOpencode.responses = {
        "rec_c1": json.dumps(
            full_response("rec_c1")
            | {"need_1": "model coat", "desire_1": "LEGO set"}
        )
    }
    run(screen_cfg)
    retried = _rows(screen_cfg, "final_child")[0]
    assert retried["screened"] is True
    assert retried["need_1"] == "human-fixed coat"
    assert retried["desire_1"] == "LEGO set"
    assert retried["clarification_notes"] == "keep me"


def test_run_never_creates_submissions_or_adj_sheets(
    screen_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persistent-final runs never recreate retired submissions/adj sheets."""
    _seed_workbook(screen_cfg)
    KeyedFakeOpencode.responses = {"rec_c1": "null", "rec_c2": "null"}
    _use_keyed_opencode(monkeypatch)
    run(screen_cfg)
    _assert_no_legacy_sheets(screen_cfg)

    run(screen_cfg)
    KeyedFakeOpencode.responses = {"rec_c1": "null", "rec_c2": "null"}
    run(screen_cfg, overwrite=True)
    _assert_no_legacy_sheets(screen_cfg)


def test_write_final_children_passes_xmas_to_full_store_writer(
    screen_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The final-child helper writes the full schema with formula context."""
    seen_xmas: list[Any] = []
    real_write = store.write_sheets

    def checking_write(
        path: str,
        module: str,
        sheets: dict[str, pl.DataFrame],
        renames: dict[str, str] | None = None,
        xmas: Any = None,
    ) -> None:
        if "final_child" in sheets:
            seen_xmas.append(xmas)
        real_write(path, module, sheets, renames, xmas)

    monkeypatch.setattr(store, "write_sheets", checking_write)
    frame = screening._write_final_children(
        screen_cfg, [final_child_row(screened=True)]
    )
    assert frame.columns == list(FINAL_CHILD_COLUMNS)
    assert seen_xmas == [screen_cfg.xmas]


def test_run_requires_raw_child(screen_cfg: Config) -> None:
    """Missing raw_child fails with an actionable error."""
    from ccat.preparation import PreparationError

    with pytest.raises(PreparationError, match="acquire"):
        run(screen_cfg)


def test_run_requires_opencode_v2(cfg: Config, monkeypatch) -> None:
    """Opencode < 2 is refused before any work happens (§6.2)."""
    monkeypatch.delenv("OPENCODE_BASE_URL", raising=False)
    monkeypatch.setattr(screening, "_opencode_major_version", lambda: 1)
    with pytest.raises(screening.ScreeningError, match="upgrade --major"):
        screening._require_opencode_v2()


def test_run_requires_opencode_installed(cfg: Config, monkeypatch) -> None:
    """A missing/unparseable opencode CLI is refused (§6.2)."""
    monkeypatch.delenv("OPENCODE_BASE_URL", raising=False)
    monkeypatch.setattr(screening, "_opencode_major_version", lambda: None)
    with pytest.raises(screening.ScreeningError, match="install opencode"):
        screening._require_opencode_v2()


def test_opencode_v2_check_skipped_for_remote_server(monkeypatch) -> None:
    """A remote server needs no local CLI, so the version check is skipped."""
    monkeypatch.setenv("OPENCODE_BASE_URL", "http://opencode:4097")
    monkeypatch.setattr(
        screening,
        "_opencode_major_version",
        lambda: pytest.fail("local CLI checked against a remote server"),
    )
    screening._require_opencode_v2()


def test_opencode_major_version_parses_cli_output(monkeypatch) -> None:
    """The helper parses both `opencode --version` output forms."""

    def make_result(stdout: str) -> Any:
        return SimpleNamespace(stdout=stdout, stderr="")

    monkeypatch.setattr(
        screening.subprocess,
        "run",
        lambda *args, **kwargs: make_result("2.0.21\n"),
    )
    assert screening._opencode_major_version() == 2
    # Newer builds prefix the name and a "v".
    monkeypatch.setattr(
        screening.subprocess,
        "run",
        lambda *args, **kwargs: make_result("opencode v2.2.5\n"),
    )
    assert screening._opencode_major_version() == 2
    # Garbage output yields None rather than raising.
    monkeypatch.setattr(
        screening.subprocess,
        "run",
        lambda *args, **kwargs: make_result("hello world"),
    )
    assert screening._opencode_major_version() is None


def test_opencode_major_version_handles_missing_binary(monkeypatch) -> None:
    """A missing opencode binary yields None, not an exception."""

    def fake_run(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("opencode")

    monkeypatch.setattr(screening.subprocess, "run", fake_run)
    assert screening._opencode_major_version() is None


def test_run_fails_on_todo_model(cfg: Config) -> None:
    """A TODO model reference fails fast with an actionable error (§12)."""
    from ccat.config import AIScreeningSpec

    todo_cfg = cfg.model_copy(
        update={
            "ai_screening": AIScreeningSpec(
                model="TODO",
                system_prompt=cfg.ai_screening.system_prompt,
                temperature=cfg.ai_screening.temperature,
            )
        }
    )
    _seed_workbook(todo_cfg)
    with pytest.raises(screening.ScreeningError, match="model"):
        run(todo_cfg)


def test_adj_child_row_model_rejects_extra_keys() -> None:
    """The pydantic schema enforces the exact key set."""
    data = full_response()
    data["nickname"] = "Kiddo"
    with pytest.raises(ValueError):
        AdjChildRow(**data)


def test_run_test_mode_screens_only_first_family(
    screen_cfg: Config, monkeypatch
) -> None:
    """--test stops after the first full family (§ screen test mode)."""
    _seed_workbook(
        screen_cfg,
        families=[
            family_row(),
            family_row(
                zite_record_id="rec_f2",
                primary_email="fam2@example.com",
                created="2026-01-06T10:00:00+00:00",
                zite_created_at="2026-01-06T10:00:00+00:00",
            ),
        ],
        children=[
            child(zite_record_id="f1c1", angel_id="1A"),
            child(zite_record_id="f1c2", first_name="Two", angel_id="1B"),
            child(
                zite_record_id="f2c1",
                primary_email="fam2@example.com",
                angel_id="2A",
            ),
        ],
    )
    FakeOpencode.responses = ["null", "null"]
    import opencode

    monkeypatch.setattr(opencode, "Opencode", FakeOpencode)
    run(screen_cfg, test=True)
    final = read_sheet(screen_cfg.excel_file, "final_child")
    by_id = _by_id(final)
    assert set(by_id) == {"f1c1", "f1c2", "f2c1"}
    assert by_id["f1c1"]["screened"] is True
    assert by_id["f1c2"]["screened"] is True
    assert by_id["f2c1"]["screened"] is False


def test_run_test_mode_orphans_fallback_first_child(
    screen_cfg: Config, monkeypatch
) -> None:
    """With no family links, --test screens just the first child."""
    _seed_workbook(
        screen_cfg,
        children=[
            child(
                zite_record_id="o1",
                primary_email="missing1@x.com",
                family_id=None,
                angel_id=None,
            ),
            child(
                zite_record_id="o2",
                primary_email="missing2@x.com",
                family_id=None,
                angel_id=None,
            ),
        ],
    )
    FakeOpencode.responses = ["null"]
    import opencode

    monkeypatch.setattr(opencode, "Opencode", FakeOpencode)
    run(screen_cfg, test=True)
    by_id = _by_id(read_sheet(screen_cfg.excel_file, "final_child"))
    assert set(by_id) == {"o1", "o2"}
    assert by_id["o1"]["screened"] is True
    assert by_id["o2"]["screened"] is False


def test_run_test_mode_skips_already_screened_first_family(
    screen_cfg: Config, monkeypatch
) -> None:
    """--test screens nothing new when the first family is screened."""
    _seed_workbook(
        screen_cfg,
        families=[
            family_row(),
            family_row(
                zite_record_id="rec_f2",
                primary_email="fam2@example.com",
                created="2026-01-06T10:00:00+00:00",
                zite_created_at="2026-01-06T10:00:00+00:00",
            ),
        ],
        children=[
            child(zite_record_id="f1c1", angel_id="1A"),
            child(
                zite_record_id="f2c1",
                primary_email="fam2@example.com",
                angel_id="2A",
            ),
        ],
        final_children=[
            final_child_row(
                child(zite_record_id="f1c1", angel_id="1A"), screened=True
            )
        ],
    )
    import opencode

    monkeypatch.setattr(opencode, "Opencode", FakeOpencode)
    FakeOpencode.responses = []
    run(screen_cfg, test=True)
    after = _rows(screen_cfg, "final_child")
    assert {row["zite_record_id"] for row in after} == {"f1c1", "f2c1"}
    assert (
        next(row for row in after if row["zite_record_id"] == "f1c1")[
            "screened"
        ]
        is True
    )


def test_screen_children_screens_every_child_concurrently(
    screen_cfg: Config,
) -> None:
    """The thread pool screens every child exactly once."""
    children = [child(zite_record_id=f"rec_{index}") for index in range(8)]
    rows = screen_children(
        children, screen_cfg, lambda _prompt: "null", max_workers=4
    )
    assert len(rows) == len(children)
    assert {row["zite_record_id"] for row in rows} == {
        child_row["zite_record_id"] for child_row in children
    }


def test_screen_children_survives_send_failure(screen_cfg: Config) -> None:
    """A failing LLM call skips one child and screens the rest."""

    def send(prompt: str) -> str:
        if '"rec_2"' in prompt:  # one child's call blows up
            raise RuntimeError("model session died")
        return "null"

    children = [child(zite_record_id=f"rec_{index}") for index in range(5)]
    rows = screen_children(children, screen_cfg, send, max_workers=3)
    assert len(rows) == 4
    assert "rec_2" not in {row["zite_record_id"] for row in rows}


def test_screen_children_sequential_mode(screen_cfg: Config) -> None:
    """max_workers=1 screens sequentially with the same results."""
    children = [child(zite_record_id=f"rec_{index}") for index in range(3)]
    calls: list[str] = []

    def send(prompt: str) -> str:
        calls.append(prompt)
        return "null"

    rows = screen_children(children, screen_cfg, send, max_workers=1)
    assert len(rows) == len(children)
    assert len(calls) == len(children)


def test_screen_children_reports_each_result(screen_cfg: Config) -> None:
    """on_result fires once per successfully screened child."""
    children = [child(zite_record_id=f"rec_{index}") for index in range(4)]
    reported: list[dict[str, Any]] = []
    screen_children(
        children,
        screen_cfg,
        lambda _prompt: "null",
        on_result=reported.append,
    )
    # Completion order varies with the thread pool; the set must match.
    assert {row["zite_record_id"] for row in reported} == {
        child_row["zite_record_id"] for child_row in children
    }
    assert len(reported) == len(children)


def test_upsert_final_row_replaces_and_appends() -> None:
    """Full final rows upsert by zite_record_id without renumbering others."""
    rows: list[dict[str, Any]] = [{"zite_record_id": "a"}]
    screening._upsert_final_row(rows, {"zite_record_id": "b"})
    assert [row["zite_record_id"] for row in rows] == ["a", "b"]
    screening._upsert_final_row(rows, {"zite_record_id": "a"})
    assert [row["zite_record_id"] for row in rows] == ["a", "b"]
    assert rows[0] == {"zite_record_id": "a"}


class KeyedFakeOpencode:
    """Fake opencode client answering each child a canned response.

    Responses are keyed by the ``zite_record_id`` found inside the
    prompt, so results are deterministic under the thread pool.
    """

    responses: dict[str, str] = {}
    calls: list[str] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Record the fake session resource."""
        self.session = SimpleNamespace(
            create=lambda model: SimpleNamespace(id="ses_test"),
            generate=self._generate,
        )

    def _generate(self, session_id: str, prompt: str) -> SimpleNamespace:
        """Answer with the canned response for the prompted child."""
        import re

        match = re.search(r'"zite_record_id": "([^"]+)"', prompt)
        assert match is not None
        record_id = match.group(1)
        KeyedFakeOpencode.calls.append(record_id)
        return SimpleNamespace(text=KeyedFakeOpencode.responses[record_id])

    def __enter__(self) -> "KeyedFakeOpencode":
        """Return self as the context manager target."""
        return self

    def __exit__(self, *args: Any) -> None:
        """Swallow exceptions like the real client does."""
        return None


def test_screen_children_stops_on_interrupt(screen_cfg: Config) -> None:
    """Ctrl-C cancels queued tasks and stops screening promptly."""

    def send(prompt: str) -> str:
        if '"rec_5"' in prompt:  # the interrupt lands mid-run
            raise KeyboardInterrupt
        return "null"

    children = [child(zite_record_id=f"rec_{index}") for index in range(6)]
    reported: list[dict[str, Any]] = []
    with pytest.raises(KeyboardInterrupt):
        screen_children(
            children,
            screen_cfg,
            send,
            max_workers=2,
            on_result=reported.append,
        )
    assert len(reported) < len(children)  # some tasks were cancelled


def test_run_persists_rows_as_results_arrive(
    screen_cfg: Config, monkeypatch
) -> None:
    """Each screened child is persisted before the next result ends."""
    _seed_workbook(screen_cfg)
    KeyedFakeOpencode.responses = {
        "rec_c1": "null",
        "rec_c2": json.dumps(full_response("rec_c2")),
    }
    import opencode

    KeyedFakeOpencode.calls.clear()
    monkeypatch.setattr(opencode, "Opencode", KeyedFakeOpencode)
    save_calls: list[str] = []
    real_write = screening.write_final_rows

    def counting_write(
        cfg: Config,
        module: str,
        entity: str,
        rows: list[dict[str, Any]],
    ) -> None:
        save_calls.append(module)
        real_write(cfg, module, entity, rows)

    monkeypatch.setattr(screening, "write_final_rows", counting_write)
    run(screen_cfg)
    final = read_sheet(screen_cfg.excel_file, "final_child")
    assert final is not None and final.height == 2
    # final reconcile write plus one save per completed child
    assert save_calls.count("screen") >= 2


# ---------------------------------------------------------------------------
# New-stage tests: local pre-audit, duplicate flagging, validation logs.
# ---------------------------------------------------------------------------


def test_clean_rows_keeps_rows_and_collects_notes(screen_cfg: Config) -> None:
    """Cleaning never drops rows; failures become notes (§6.2)."""
    from ccat.preparation import clean_rows

    rows = [
        sponsor_row(email="broken", num_children="2", sponsor_type="Weird"),
        sponsor_row(zite_record_id="rec_s2"),
    ]
    cleaned, notes = clean_rows(rows, "sponsor", screen_cfg)
    assert len(cleaned) == 2
    # valid row cleaned (default filled, int cast)
    assert cleaned[1]["email"] == "sponsor@example.com"
    assert cleaned[1]["num_children"] == 2
    assert cleaned[1]["age_preference"] == "Any Age"
    # broken row kept as-is, with notes
    assert cleaned[0]["email"] == "broken"
    record_id = "rec_s1"
    assert any("invalid email" in note for note in notes[record_id])
    assert any("sponsor_type" in note for note in notes[record_id])


def test_clean_rows_age_reported_coercion(screen_cfg: Config) -> None:
    """Free-text reported ages coerce to int or null (§6.2)."""
    from ccat.preparation import clean_rows

    rows = [
        child(zite_record_id="c1", age_reported="4 turning 5"),
        child(zite_record_id="c2", age_reported="2.5"),
        child(zite_record_id="c3", age_reported="young"),
    ]
    cleaned, _notes = clean_rows(rows, "child", screen_cfg)
    assert [row["age_reported"] for row in cleaned] == [4, 2, None]


def test_detect_duplicates_families_flag_not_merge() -> None:
    """Duplicate families are grouped and flagged, never merged (§6.4)."""
    rows = [
        family_row(zite_record_id="f1", created="2026-01-01T00:00:00+00:00"),
        family_row(
            zite_record_id="f2",
            created="2026-01-02T00:00:00+00:00",
            phone="(906) 555-2222",
        ),
        family_row(zite_record_id="f3", primary_email="other@x.com"),
    ]
    log_rows, survivor_of = preparation.detect_duplicates(rows, "family")
    assert set(survivor_of) == {"f1", "f2"}
    assert survivor_of["f1"]["zite_record_id"] == "f1"  # earliest = survivor
    assert len(log_rows) == 2
    by_id = {row["zite_record_id"]: row for row in log_rows}
    assert by_id["f1"]["survivor"] is True
    assert by_id["f2"]["survivor"] is False
    assert by_id["f2"]["conflict_columns"] == "phone"
    assert '"zite_record_id": "f2"' in by_id["f2"]["record"]


def test_detect_duplicates_children_latest_survives() -> None:
    """Duplicate child submissions: the latest is the survivor (§6.4)."""
    rows = [
        child(zite_record_id="c1", created="2026-01-01T00:00:00+00:00"),
        child(
            zite_record_id="c2",
            created="2026-01-02T00:00:00+00:00",
            need_1="winter coat, mittens",
        ),
    ]
    log_rows, survivor_of = preparation.detect_duplicates(rows, "child")
    assert survivor_of["c1"]["zite_record_id"] == "c2"  # latest survives
    assert survivor_of["c2"]["zite_record_id"] == "c2"
    assert len(log_rows) == 2


def test_detect_duplicates_sponsors_by_email() -> None:
    """Duplicate sponsors group by email; unkeyed rows never group (§6.4)."""
    rows = [
        sponsor_row(zite_record_id="s1"),
        sponsor_row(zite_record_id="s2"),
        sponsor_row(zite_record_id="s3", email=None),
    ]
    log_rows, survivor_of = preparation.detect_duplicates(rows, "sponsor")
    assert set(survivor_of) == {"s1", "s2"}
    assert len(log_rows) == 2


def test_run_appends_full_finals_and_duplicate_log(
    screen_cfg: Config, monkeypatch
) -> None:
    """run() appends every entity and refreshes duplicate flags/logs."""
    _seed_workbook(
        screen_cfg,
        families=[
            family_row(zite_record_id="rec_f1"),
            family_row(
                zite_record_id="rec_f2",
                created="2026-01-06T10:00:00+00:00",
                zite_created_at="2026-01-06T10:00:00+00:00",
                phone="(906) 555-2222",
            ),
        ],
        children=[
            child(zite_record_id="rec_c1"),
            child(
                zite_record_id="rec_c2",
                created="2026-02-02T09:00:00+00:00",
                zite_created_at="2026-02-02T09:00:00+00:00",
                need_1="winter coat, mittens",
            ),
        ],
        sponsors=[
            sponsor_row(zite_record_id="rec_s1"),
            sponsor_row(
                zite_record_id="rec_s2",
                create="2026-01-02T10:00:00+00:00",
            ),
        ],
    )
    FakeOpencode.responses = ["null", "null"]
    import opencode

    monkeypatch.setattr(opencode, "Opencode", FakeOpencode)
    run(screen_cfg)
    family_final = read_final(screen_cfg, "family")
    assert len(family_final) == 1
    assert family_final[0]["family_id"] == 1
    # duplicate log: one row per flagged group member
    duplicate_log = read_sheet(screen_cfg.excel_file, "duplicate_log")
    assert duplicate_log is not None
    log_by_id = {row["zite_record_id"]: row for row in duplicate_log.to_dicts()}
    assert set(log_by_id) == {
        "rec_f1",
        "rec_f2",
        "rec_c1",
        "rec_c2",
        "rec_s1",
        "rec_s2",
    }
    assert log_by_id["rec_f1"]["survivor"] is True
    assert log_by_id["rec_f2"]["survivor"] is False
    # child duplicates: the LATEST keeps the angel letter
    child_rows = read_final(screen_cfg, "child")
    child_ids = {row["zite_record_id"]: row for row in child_rows}
    assert child_ids["rec_c2"]["angel_id"] == "1A"
    assert child_ids["rec_c1"]["angel_id"] is None
    # final rows carry the flag + note
    assert family_final[0]["potential_duplicate"] is True
    assert "Potential duplicate family submission" in (
        family_final[0]["clarification_notes"] or ""
    )
    assert child_ids["rec_c1"]["potential_duplicate"] is True
    assert child_ids["rec_c2"]["potential_duplicate"] is True
    assert child_ids["rec_c1"]["screened"] is True


def test_check_emails_logs_full_response(
    screen_cfg: Config, monkeypatch
) -> None:
    """ZeroBounce results land in email_validation_log, full response (§6.7)."""
    monkeypatch.setattr(screening, "check_emails", _REAL_CHECK_EMAILS)
    monkeypatch.setattr(screening, "_email_batch_engine", lambda: None)
    captured: list[str] = []

    def fake_validate(email: str, **kwargs: Any) -> dict[str, Any]:
        captured.append(email)
        return {
            "address": email,
            "status": "valid",
            "sub_status": "",
            "did_you_mean": "",
            "raw_response": f'{{"address": "{email}", "status": "valid"}}',
        }

    monkeypatch.setattr(
        screening,
        "_email_engine",
        lambda: (fake_validate, RuntimeError),
    )
    rows = {
        "sponsor": [sponsor_row()],
        "family": [
            family_row(),
            family_row(
                zite_record_id="rec_f2", secondary_email="sec@example.com"
            ),
        ],
        "child": [],
    }
    log_rows = screening.check_emails(screen_cfg, rows)
    assert sorted(captured) == [
        "fam@example.com",
        "sec@example.com",
        "sponsor@example.com",
    ]
    # one log row per (record, field) occurrence: sponsor email, both
    # families' primary email, and the secondary email
    assert len(log_rows) == 4
    by_email = {row["email"]: row for row in log_rows}
    assert (
        by_email["sponsor@example.com"]["source_sheet"] == "sponsor_submissions"
    )
    assert by_email["sponsor@example.com"]["field"] == "email"
    assert by_email["fam@example.com"]["field"] == "primary_email"
    assert by_email["sec@example.com"]["field"] == "secondary_email"
    assert (
        by_email["fam@example.com"]["response_json"]
        == '{"address": "fam@example.com", "status": "valid"}'
    )
    assert by_email["fam@example.com"]["status"] == "valid"


def test_check_emails_uses_bulk_batch_engine(
    screen_cfg: Config, monkeypatch
) -> None:
    """The batch (bulk) engine is preferred; per-email calls skipped (§6.7)."""
    monkeypatch.setattr(screening, "check_emails", _REAL_CHECK_EMAILS)
    captured: list[str] = []

    def fake_bulk(emails: list[str]) -> dict[str, dict[str, Any] | None]:
        captured.extend(emails)
        return {
            email: {
                "status": "valid",
                "sub_status": "",
                "did_you_mean": "",
                "raw_response": f'{{"address": "{email}"}}',
            }
            for email in emails
        }

    def fail_validate(email: str, **kwargs: Any) -> None:
        raise AssertionError("per-email validate must not be called")

    monkeypatch.setattr(
        screening,
        "_email_batch_engine",
        lambda: fake_bulk,
    )
    monkeypatch.setattr(
        screening, "_email_engine", lambda: (fail_validate, RuntimeError)
    )
    rows = {
        "sponsor": [sponsor_row()],
        "family": [family_row()],
        "child": [],
    }
    log_rows = screening.check_emails(screen_cfg, rows)
    assert sorted(captured) == ["fam@example.com", "sponsor@example.com"]
    assert len(log_rows) == 2
    by_email = {row["email"]: row for row in log_rows}
    assert by_email["fam@example.com"]["response_json"] == (
        '{"address": "fam@example.com"}'
    )


def test_check_emails_bulk_failure_falls_back_threaded(
    screen_cfg: Config, monkeypatch
) -> None:
    """A failed bulk run falls back to threaded per-email checks (§6.7)."""
    monkeypatch.setattr(screening, "check_emails", _REAL_CHECK_EMAILS)
    calls: list[str] = []

    def fake_bulk(emails: list[str]) -> None:
        raise RuntimeError("batch endpoint refused")

    def fake_validate(email: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(email)
        return {
            "status": "valid",
            "sub_status": "",
            "did_you_mean": "",
            "raw_response": "{}",
        }

    class FakeOutOfCreditsError(Exception):
        """Stand-in for the script's OutOfCreditsError."""

    monkeypatch.setattr(screening, "_email_batch_engine", lambda: fake_bulk)
    monkeypatch.setattr(
        screening,
        "_email_engine",
        lambda: (fake_validate, FakeOutOfCreditsError),
    )
    rows = {
        "sponsor": [sponsor_row()],
        "family": [family_row()],
        "child": [],
    }
    log_rows = screening.check_emails(screen_cfg, rows)
    assert sorted(calls) == ["fam@example.com", "sponsor@example.com"]
    assert len(log_rows) == 2


def test_check_emails_idempotent(screen_cfg: Config, monkeypatch) -> None:
    """Already-checked emails are skipped on re-runs (§6.7)."""
    monkeypatch.setattr(screening, "check_emails", _REAL_CHECK_EMAILS)
    monkeypatch.setattr(screening, "_email_batch_engine", lambda: None)
    calls: list[str] = []

    def fake_validate(email: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(email)
        return {
            "status": "valid",
            "sub_status": "",
            "did_you_mean": "",
            "raw_response": "{}",
        }

    monkeypatch.setattr(
        screening, "_email_engine", lambda: (fake_validate, RuntimeError)
    )
    rows = {
        "sponsor": [sponsor_row()],
        "family": [family_row()],
        "child": [],
    }
    first = screening.check_emails(screen_cfg, rows)
    assert len(first) == 2  # sponsor + family primary
    write_sheets(
        screen_cfg.excel_file,
        "screen",
        {
            "email_validation_log": pl.DataFrame(
                first, schema=screening.EMAIL_VALIDATION_LOG_SCHEMA
            )
        },
    )
    second = screening.check_emails(screen_cfg, rows)
    assert second == []
    assert len(calls) == 2  # no second API round


def test_check_addresses_logs_full_response(
    screen_cfg: Config, monkeypatch
) -> None:
    """Nominatim results land in address_validation_log, full response (§6.8)."""
    monkeypatch.setattr(screening, "check_addresses", _REAL_CHECK_ADDRESSES)
    monkeypatch.setattr(
        screening,
        "_address_engine",
        lambda: (
            lambda *args, **kwargs: {
                "query": "1 Main St, Hancock, Michigan, 49930",
                "status": "found",
                "display_name": "1 Main St, Hancock, MI 49930",
                "lat": "47.12",
                "lon": "-88.6",
                "result_count": 1,
                "response_json": '[{"display_name": "1 Main St"}]',
            },
            lambda *args: "1 Main St, Hancock, Michigan, 49930",
        ),
    )
    log_rows = screening.check_addresses(screen_cfg, [family_row()])
    assert len(log_rows) == 1
    row = log_rows[0]
    assert row["status"] == "found"
    assert row["response_json"] == '[{"display_name": "1 Main St"}]'
    assert row["lat"] == "47.12"
    assert row["zite_record_id"] == "rec_f1"


def test_check_addresses_idempotent(screen_cfg: Config, monkeypatch) -> None:
    """Already-checked addresses are skipped on re-runs (§6.8)."""
    monkeypatch.setattr(screening, "check_addresses", _REAL_CHECK_ADDRESSES)
    calls: list[tuple] = []
    monkeypatch.setattr(
        screening,
        "_address_engine",
        lambda: (
            lambda *args, **kwargs: (
                calls.append(args)
                or {
                    "query": "1 Main St, Hancock, Michigan, 49930",
                    "status": "found",
                    "display_name": None,
                    "lat": None,
                    "lon": None,
                    "result_count": 1,
                    "response_json": "[]",
                }
            ),
            lambda *args: "1 Main St, Hancock, Michigan, 49930",
        ),
    )
    first = screening.check_addresses(screen_cfg, [family_row()])
    assert len(first) == 1
    write_sheets(
        screen_cfg.excel_file,
        "screen",
        {
            "address_validation_log": pl.DataFrame(
                first, schema=screening.ADDRESS_VALIDATION_LOG_SCHEMA
            )
        },
    )
    assert screening.check_addresses(screen_cfg, [family_row()]) == []
    assert len(calls) == 1


def test_run_orphans_get_the_orphan_note(
    screen_cfg: Config, monkeypatch
) -> None:
    """Orphaned children (no family match) seed with the orphan note (§8.7)."""
    from ccat.audit.store import ORPHAN_NOTE

    _seed_workbook(
        screen_cfg,
        children=[
            child(
                zite_record_id="o1",
                primary_email="missing@x.com",
                family_id=None,
                angel_id=None,
            ),
        ],
    )
    FakeOpencode.responses = ["null"]
    import opencode

    monkeypatch.setattr(opencode, "Opencode", FakeOpencode)
    run(screen_cfg)
    final = read_sheet(screen_cfg.excel_file, "final_child")
    assert final is not None
    row = final.to_dicts()[0]
    assert ORPHAN_NOTE in (row["clarification_notes"] or "")
    assert row["status"] == "needs_clarification"


def test_run_existing_orphan_row_gets_the_note(
    screen_cfg: Config, monkeypatch
) -> None:
    """An already-seeded orphan final row picks up the note idempotently."""
    from ccat.audit.store import ORPHAN_NOTE

    _seed_workbook(
        screen_cfg,
        children=[
            child(
                zite_record_id="o1",
                primary_email="missing@x.com",
                family_id=None,
                angel_id=None,
            ),
        ],
        final_children=[
            final_child_row(
                child(
                    zite_record_id="o1",
                    primary_email="missing@x.com",
                    family_id=None,
                    angel_id=None,
                ),
                screened=True,
            )
        ],
    )
    FakeOpencode.responses = []
    import opencode

    monkeypatch.setattr(opencode, "Opencode", FakeOpencode)
    run(screen_cfg)
    final = read_sheet(screen_cfg.excel_file, "final_child")
    assert final is not None
    row = final.to_dicts()[0]
    assert ORPHAN_NOTE in (row["clarification_notes"] or "")
