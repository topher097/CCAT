"""Tests for module 2 — AI screening (§6)."""

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
    _audited_count,
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


def child(**overrides: Any) -> dict[str, Any]:
    """One raw_child row for screening tests."""
    base = {
        "zite_record_id": "rec_c1",
        "first_name": "Kiddo",
        "primary_email": "fam@example.com",
        "dob": "2018-05-10",
        "age_at_xmas": 8,
        "age_reported": 7,
        "gender": "Male",
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
        "family_id": 1,
        "angel_id": "1A",
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


def test_run_test_mode_backfills_age_from_raw(
    screen_cfg: Config, monkeypatch
) -> None:
    """adj_child.age_at_xmas is copied from raw_child, existing rows too."""
    raw = pl.DataFrame(
        [
            child(zite_record_id="f1c1", family_id=1, angel_id="1A"),
            child(zite_record_id="f1c2", family_id=1, angel_id="1B"),
        ]
    )
    write_sheets(screen_cfg.excel_file, "acquire", {"raw_child": raw})
    # simulate a pre-existing row screened before the column existed
    stale = pl.DataFrame(
        [{"zite_record_id": "f1c1", "need_1": "coat"}],
        schema={"zite_record_id": pl.String, "need_1": pl.String},
    )
    write_sheets(screen_cfg.excel_file, "screen", {"adj_child": stale})
    FakeOpencode.responses = ["null"]
    import opencode

    monkeypatch.setattr(opencode, "Opencode", FakeOpencode)
    run(screen_cfg, test=True)
    adj = read_sheet(screen_cfg.excel_file, "adj_child")
    assert adj is not None
    assert "age_at_xmas" in adj.columns
    by_id = {row["zite_record_id"]: row for row in adj.to_dicts()}
    assert by_id["f1c1"]["age_at_xmas"] == "8"  # backfilled
    assert by_id["f1c2"]["age_at_xmas"] == "8"  # freshly screened


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


def _seed_workbook(cfg: Config) -> None:
    """Write a raw_child sheet with two children; no adj rows yet."""
    raw_child = pl.DataFrame(
        [child(), child(zite_record_id="rec_c2", first_name="Two")]
    )
    write_sheets(cfg.excel_file, "acquire", {"raw_child": raw_child})


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
    raw = pl.DataFrame(
        [
            child(zite_record_id="f1c1", family_id=1, angel_id="1A"),
            child(zite_record_id="f1c2", family_id=1, angel_id="1B"),
            child(zite_record_id="f2c1", family_id=2, angel_id="2A"),
        ]
    )
    write_sheets(screen_cfg.excel_file, "acquire", {"raw_child": raw})
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
    raw = pl.DataFrame(
        [
            child(zite_record_id="o1", family_id=None, angel_id=None),
            child(zite_record_id="o2", family_id=None, angel_id=None),
        ]
    )
    write_sheets(screen_cfg.excel_file, "acquire", {"raw_child": raw})
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
    """--test screens nothing new when the first family is done."""
    raw = pl.DataFrame(
        [
            child(zite_record_id="f1c1", family_id=1, angel_id="1A"),
            child(zite_record_id="f2c1", family_id=2, angel_id="2A"),
        ]
    )
    adj = pl.DataFrame(
        [{"zite_record_id": "f1c1"}],
        schema={"zite_record_id": pl.String},
    )
    write_sheets(screen_cfg.excel_file, "acquire", {"raw_child": raw})
    write_sheets(screen_cfg.excel_file, "screen", {"adj_child": adj})
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


def test_audited_count_string_dtype() -> None:
    """String audited columns count only "true" values."""
    frame = pl.DataFrame(
        {"audited": ["true", "false", None, "true", "TRUE", " true "]},
        schema={"audited": pl.String},
    )
    assert _audited_count(frame) == 4


def test_audited_count_boolean_dtype() -> None:
    """Boolean audited columns count true values, nulls as false."""
    frame = pl.DataFrame(
        {"audited": [True, False, None, True]},
        schema={"audited": pl.Boolean},
    )
    assert _audited_count(frame) == 2


def test_audited_count_missing_or_empty() -> None:
    """Missing column and empty frame both count zero."""
    assert _audited_count(pl.DataFrame({"other": ["x"]})) == 0
    empty = pl.DataFrame(
        {"audited": []},
        schema={"audited": pl.Boolean},
    )
    assert _audited_count(empty) == 0


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
