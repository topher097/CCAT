"""Tests for module 2 — pre-audit screening (§6)."""

import json
from typing import Any
from types import SimpleNamespace

import polars as pl
import pytest

import ccat.screening as screening
from ccat.config import Config
from ccat.io import read_sheet, write_sheets
from ccat.screening import (
    ADJ_CHILD_COLUMNS,
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
    """A valid response becomes an adj row with sparse cells (§6.3)."""
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
        for column in ADJ_CHILD_COLUMNS
        if column != "zite_record_id"
    )


def test_screen_child_null_response_has_no_age_key(screen_cfg: Config) -> None:
    """The model's null response carries no age_at_xmas (§5.5, §6.2)."""
    row = screen_child(child(), screen_cfg, lambda prompt: "null")
    assert row is not None
    assert "age_at_xmas" in row  # sheet column exists
    assert row["age_at_xmas"] is None  # filled by run(), not the model


def test_run_writes_dense_rows_equal_to_raw(
    screen_cfg: Config, monkeypatch
) -> None:
    """Screening writes dense rows: cleaned values, screened true (§7.3)."""
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
    import opencode

    monkeypatch.setattr(opencode, "Opencode", KeyedFakeOpencode)
    run(screen_cfg)
    adj = read_sheet(screen_cfg.excel_file, "adj_child")
    assert adj is not None and adj.height == 2
    by_id = {row["zite_record_id"]: row for row in adj.to_dicts()}
    # no-change child: dense row equals the cleaned row (plus defaults)
    assert by_id["f1c1"]["need_1"] == "winter coat taht fits"
    assert by_id["f1c1"]["screened"] is True
    assert by_id["f1c1"]["status"] == "new"
    assert by_id["f1c1"]["age_at_xmas"] == 8
    # corrected child: the correction overlays the cleaned value
    assert by_id["f1c2"]["need_1"] == "winter coat that fits"
    assert by_id["f1c2"]["screened"] is True


def test_run_targets_absent_or_unscreened_rows(
    screen_cfg: Config, monkeypatch
) -> None:
    """Target selection uses the screened marker, not row presence (§7.3)."""
    _seed_workbook(screen_cfg)
    # f1c1 has a dense row but was never screened (audit seeded it);
    # f1c2-style screened row below blocks re-screening.
    from ccat.audit.store import build_adj_frame

    seeded = {
        "zite_record_id": "f1c1",
        "first_name": "Kiddo",
        "screened": False,
        "potential_duplicate": False,
    }
    screened = {
        "zite_record_id": "f1c2",
        "first_name": "Two",
        "screened": True,
        "potential_duplicate": False,
    }
    write_sheets(
        screen_cfg.excel_file,
        "audit",
        {"adj_child": build_adj_frame("child", [seeded, screened])},
    )
    FakeOpencode.responses = ["null"]
    import opencode

    monkeypatch.setattr(opencode, "Opencode", FakeOpencode)
    run(screen_cfg)
    assert FakeOpencode.instances[-1].session  # one session used
    # only the unscreened-but-present row was re-screened
    assert len(FakeOpencode.responses) == 0


def test_run_preserves_human_edits_and_controls(
    screen_cfg: Config, monkeypatch
) -> None:
    """Corrections never clobber human edits or controls (§7.3)."""
    _seed_workbook(
        screen_cfg, children=[child(zite_record_id="f1c1", angel_id="1A")]
    )
    from ccat.audit.store import build_adj_frame

    # a human (audit) already fixed need_1 and left a note; the rest of
    # the row is the dense baseline audit seeded from raw
    seeded = {
        **child(zite_record_id="f1c1"),
        "need_1": "human-fixed coat",
        "require_clarification": "call the family",
        "screened": False,
        "potential_duplicate": False,
    }
    write_sheets(
        screen_cfg.excel_file,
        "audit",
        {"adj_child": build_adj_frame("child", [seeded])},
    )
    KeyedFakeOpencode.responses = {
        # the model proposes a different need_1 AND a desire_1 fix
        "f1c1": json.dumps(
            full_response("f1c1")
            | {"need_1": "model coat", "desire_1": "LEGO set"}
        )
    }
    import opencode

    monkeypatch.setattr(opencode, "Opencode", KeyedFakeOpencode)
    run(screen_cfg)
    adj = read_sheet(screen_cfg.excel_file, "adj_child")
    assert adj is not None
    row = adj.to_dicts()[0]
    # need_1: the human changed it (differs from raw) -> correction skipped
    assert row["need_1"] == "human-fixed coat"
    # desire_1: untouched by the human -> correction applied
    assert row["desire_1"] == "LEGO set"
    # controls preserved, marker set
    assert row["require_clarification"] == "call the family"
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
    responses: list[str] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Record the fake session resource."""
        self.session = SimpleNamespace(
            create=lambda model: SimpleNamespace(id="ses_test"),
            generate=self._generate,
        )
        FakeOpencode.instances.append(self)

    def _generate(self, session_id: str, prompt: str) -> SimpleNamespace:
        """Return the next canned response text."""
        return SimpleNamespace(text=FakeOpencode.responses.pop(0))

    def __enter__(self) -> "FakeOpencode":
        """Return self as the context manager target."""
        return self

    def __exit__(self, *args: Any) -> None:
        """Swallow exceptions like the real client does."""
        return None


def _seed_workbook(
    cfg: Config,
    children: list[dict[str, Any]] | None = None,
    families: list[dict[str, Any]] | None = None,
    sponsors: list[dict[str, Any]] | None = None,
) -> None:
    """Write raw_sponsor/raw_family/raw_child sheets (all config columns)."""
    if children is None:
        children = [
            child(),
            child(zite_record_id="rec_c2", first_name="Two", angel_id="1B"),
        ]
    write_sheets(
        cfg.excel_file,
        "acquire",
        {
            "raw_sponsor": pl.DataFrame(
                sponsors or [sponsor_row()],
                schema={key: _raw_dtype(key) for key in sponsor_row()},
            ),
            "raw_family": pl.DataFrame(
                families or [family_row()],
                schema={key: _raw_dtype(key) for key in family_row()},
            ),
            "raw_child": pl.DataFrame(
                children,
                schema={key: _raw_dtype(key) for key in child()},
            ),
        },
    )


def _raw_dtype(key: str) -> Any:
    """Sheet dtype for one raw fixture key (ints are Int64)."""
    return pl.Int64 if key in {"num_children", "age_reported"} else pl.String


def test_run_appends_rows_only_for_new_children(
    screen_cfg: Config, monkeypatch
) -> None:
    """Only zite_record_ids absent from adj_child are screened (§6.3)."""
    _seed_workbook(screen_cfg)
    FakeOpencode.responses = [json.dumps(full_response()), "null"]
    import opencode

    monkeypatch.setattr(opencode, "Opencode", FakeOpencode)
    run(screen_cfg)
    adj = read_sheet(screen_cfg.excel_file, "adj_child")
    assert adj is not None and adj.height == 2
    # second run: both ids present, nothing new screened
    FakeOpencode.responses = []
    run(screen_cfg)
    adj = read_sheet(screen_cfg.excel_file, "adj_child")
    assert adj is not None and adj.height == 2
    assert FakeOpencode.instances[-1].session  # one session per run


def test_run_redo_replaces_one_row(screen_cfg: Config, monkeypatch) -> None:
    """--redo re-screens exactly one child and replaces its row (§6.3)."""
    _seed_workbook(screen_cfg)
    FakeOpencode.responses = ["null", "null"]
    import opencode

    monkeypatch.setattr(opencode, "Opencode", FakeOpencode)
    run(screen_cfg)
    FakeOpencode.responses = [json.dumps(full_response())]
    run(screen_cfg, redo="rec_c1")
    adj = read_sheet(screen_cfg.excel_file, "adj_child")
    assert adj is not None and adj.height == 2
    row = adj.filter(pl.col("zite_record_id") == "rec_c1").to_dicts()[0]
    assert row["need_1"] == "winter coat that fits"


def test_run_redo_unknown_child_fails(screen_cfg: Config, monkeypatch) -> None:
    """Redo of an unknown zite_record_id fails fast."""
    _seed_workbook(screen_cfg)
    import opencode

    monkeypatch.setattr(opencode, "Opencode", FakeOpencode)
    with pytest.raises(screening.ScreeningError, match="redo target"):
        run(screen_cfg, redo="nope")


def test_run_overwrite_replaces_all_rows(
    screen_cfg: Config, monkeypatch
) -> None:
    """--overwrite re-screens every child, replacing all adj rows (§6.3)."""
    _seed_workbook(screen_cfg)
    FakeOpencode.responses = ["null", "null"]
    import opencode

    monkeypatch.setattr(opencode, "Opencode", FakeOpencode)
    run(screen_cfg)
    adj = read_sheet(screen_cfg.excel_file, "adj_child")
    assert adj is not None and adj.height == 2
    FakeOpencode.responses = [json.dumps(full_response()), "null"]
    run(screen_cfg, overwrite=True)
    adj = read_sheet(screen_cfg.excel_file, "adj_child")
    assert adj is not None and adj.height == 2
    row = adj.filter(pl.col("zite_record_id") == "rec_c1").to_dicts()[0]
    assert row["need_1"] == "winter coat that fits"


def test_run_overwrite_test_mode_limits_to_first_family(
    screen_cfg: Config, monkeypatch
) -> None:
    """--overwrite --test re-screens only the first family's children."""
    _seed_workbook(screen_cfg)
    FakeOpencode.responses = ["null", "null"]
    import opencode

    monkeypatch.setattr(opencode, "Opencode", FakeOpencode)
    run(screen_cfg)
    adj = read_sheet(screen_cfg.excel_file, "adj_child")
    assert adj is not None and adj.height == 2
    FakeOpencode.responses = [json.dumps(full_response()), "null"]
    run(screen_cfg, overwrite=True, test=True)
    adj = read_sheet(screen_cfg.excel_file, "adj_child")
    assert adj is not None and adj.height == 2
    row = adj.filter(pl.col("zite_record_id") == "rec_c1").to_dicts()[0]
    assert row["need_1"] == "winter coat that fits"


def test_run_requires_raw_child(screen_cfg: Config) -> None:
    """Missing raw_child fails with an actionable error."""
    with pytest.raises(screening.ScreeningError, match="acquire"):
        run(screen_cfg)


def test_run_requires_opencode_v2(cfg: Config, monkeypatch) -> None:
    """Opencode < 2 is refused before any work happens (§6.2)."""
    monkeypatch.setattr(screening, "_opencode_major_version", lambda: 1)
    with pytest.raises(screening.ScreeningError, match="upgrade --major"):
        screening._require_opencode_v2()


def test_run_requires_opencode_installed(cfg: Config, monkeypatch) -> None:
    """A missing/unparseable opencode CLI is refused (§6.2)."""
    monkeypatch.setattr(screening, "_opencode_major_version", lambda: None)
    with pytest.raises(screening.ScreeningError, match="install opencode"):
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
    adj = read_sheet(screen_cfg.excel_file, "adj_child")
    assert adj is not None
    assert set(adj["zite_record_id"].to_list()) == {"f1c1", "f1c2"}


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
    adj = read_sheet(screen_cfg.excel_file, "adj_child")
    assert adj is not None
    assert adj["zite_record_id"].to_list() == ["o1"]


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
    )
    from ccat.audit.store import build_adj_frame

    screened = {
        "zite_record_id": "f1c1",
        "first_name": "Kiddo",
        "screened": True,
        "potential_duplicate": False,
    }
    write_sheets(
        screen_cfg.excel_file,
        "audit",
        {"adj_child": build_adj_frame("child", [screened])},
    )
    import opencode

    monkeypatch.setattr(opencode, "Opencode", FakeOpencode)
    FakeOpencode.responses = []
    run(screen_cfg, test=True)
    after = read_sheet(screen_cfg.excel_file, "adj_child")
    assert after is not None
    assert after["zite_record_id"].to_list() == ["f1c1"]  # untouched


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


def test_upsert_adj_row_replaces_and_appends() -> None:
    """Rows upsert by zite_record_id: append new, replace in place."""
    rows: list[dict[str, Any]] = [{"zite_record_id": "a"}]
    screening._upsert_adj_row(rows, {"zite_record_id": "b"})
    assert [row["zite_record_id"] for row in rows] == ["a", "b"]
    screening._upsert_adj_row(rows, {"zite_record_id": "a"})
    assert [row["zite_record_id"] for row in rows] == ["a", "b"]
    assert rows[0] == {"zite_record_id": "a"}  # the replacement row


class KeyedFakeOpencode:
    """Fake opencode client answering each child a canned response.

    Responses are keyed by the ``zite_record_id`` found inside the
    prompt, so results are deterministic under the thread pool.
    """

    responses: dict[str, str] = {}

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
    """Each screened child is written to adj_child before the next ends."""
    _seed_workbook(screen_cfg)
    KeyedFakeOpencode.responses = {
        "rec_c1": "null",
        "rec_c2": json.dumps(full_response("rec_c2")),
    }
    import opencode

    monkeypatch.setattr(opencode, "Opencode", KeyedFakeOpencode)
    save_calls: list[str] = []
    real_write = screening.write_sheets

    def counting_write(
        path: str,
        module: str,
        sheets: dict[str, pl.DataFrame],
        renames: dict[str, str] | None = None,
    ) -> None:
        save_calls.append(module)
        real_write(path, module, sheets, renames)

    monkeypatch.setattr(screening, "write_sheets", counting_write)
    run(screen_cfg)
    adj = read_sheet(screen_cfg.excel_file, "adj_child")
    assert adj is not None and adj.height == 2
    # final reconcile write plus one save per completed child
    assert save_calls.count("screen") >= 2


# ---------------------------------------------------------------------------
# New-stage tests: local pre-audit, duplicate flagging, validation logs.
# ---------------------------------------------------------------------------


def test_clean_rows_keeps_rows_and_collects_notes(screen_cfg: Config) -> None:
    """Cleaning never drops rows; failures become notes (§6.2)."""
    from ccat.screening import clean_rows

    rows = [
        sponsor_row(email="broken", num_children="2", sponsor_type="Weird"),
        sponsor_row(),
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
    from ccat.screening import clean_rows

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
    log_rows, survivor_of = screening.detect_duplicates(rows, "family")
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
    log_rows, survivor_of = screening.detect_duplicates(rows, "child")
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
    log_rows, survivor_of = screening.detect_duplicates(rows, "sponsor")
    assert set(survivor_of) == {"s1", "s2"}
    assert len(log_rows) == 2


def test_run_writes_submissions_and_duplicate_log(
    screen_cfg: Config, monkeypatch
) -> None:
    """run() writes every submission + duplicate flags (§6.3/§6.4)."""
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
    # every submission landed in the submissions sheets, cleaned + IDs
    family_submissions = read_sheet(screen_cfg.excel_file, "family_submissions")
    assert family_submissions is not None and family_submissions.height == 2
    assert "family_id" in family_submissions.columns
    assert family_submissions["family_id"].to_list() == [1, 1]
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
    child_submissions = read_sheet(screen_cfg.excel_file, "child_submissions")
    assert child_submissions is not None
    child_ids = {
        row["zite_record_id"]: row for row in child_submissions.to_dicts()
    }
    assert child_ids["rec_c2"]["angel_id"] == "1A"
    assert child_ids["rec_c1"]["angel_id"] is None
    # adj rows carry the flag + note
    adj_family = read_sheet(screen_cfg.excel_file, "adj_family")
    assert adj_family is not None and adj_family.height == 1
    family_row_dict = adj_family.to_dicts()[0]
    assert family_row_dict["potential_duplicate"] is True
    assert "Potential duplicate family submission" in (
        family_row_dict["require_clarification"] or ""
    )
    adj_child = read_sheet(screen_cfg.excel_file, "adj_child")
    assert adj_child is not None
    adj_by_id = {row["zite_record_id"]: row for row in adj_child.to_dicts()}
    assert adj_by_id["rec_c1"]["potential_duplicate"] is True
    assert adj_by_id["rec_c2"]["potential_duplicate"] is True
    # validation notes propagated (sponsor has none here)
    assert adj_by_id["rec_c1"]["screened"] is True


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
    adj = read_sheet(screen_cfg.excel_file, "adj_child")
    assert adj is not None
    row = adj.to_dicts()[0]
    assert ORPHAN_NOTE in (row["require_clarification"] or "")
    assert row["status"] == "needs_clarification"


def test_run_existing_orphan_row_gets_the_note(
    screen_cfg: Config, monkeypatch
) -> None:
    """An already-seeded orphan adj row picks up the note idempotently."""
    from ccat.audit.store import ORPHAN_NOTE, build_adj_frame

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
    seeded = {
        "zite_record_id": "o1",
        "first_name": "Kiddo",
        "family_id": None,
        "screened": True,
        "potential_duplicate": False,
    }
    write_sheets(
        screen_cfg.excel_file,
        "audit",
        {"adj_child": build_adj_frame("child", [seeded])},
    )
    FakeOpencode.responses = []
    import opencode

    monkeypatch.setattr(opencode, "Opencode", FakeOpencode)
    run(screen_cfg)
    adj = read_sheet(screen_cfg.excel_file, "adj_child")
    assert adj is not None
    row = adj.to_dicts()[0]
    assert ORPHAN_NOTE in (row["require_clarification"] or "")
