"""Tests for module 3 — human audit tool (§7)."""

import threading
from typing import Any

import polars as pl
import pytest
import requests

from ccat.audit.server import (
    AuditClientError,
    AuditState,
    build_final_child_row,
    dob_conflict_notes,
    merge_anomoly,
    run,
)
from ccat.config import Config
from ccat.io import read_sheet, write_sheets

RAW_COLUMNS = [
    "zite_record_id",
    "first_name",
    "primary_email",
    "dob",
    "age_at_xmas",
    "age_reported",
    "need_1",
    "shoe_size",
    "family_id",
    "angel_id",
]


def raw_child_row(
    record_id: str, angel_id: str | None = "1A"
) -> dict[str, Any]:
    """One raw_child row for audit tests."""
    return {
        "zite_record_id": record_id,
        "first_name": "Kiddo",
        "primary_email": "fam@example.com",
        "dob": "2018-05-10",
        "age_at_xmas": 8,
        "age_reported": 7,
        "need_1": "winter coat taht fits",
        "shoe_size": "13",
        "family_id": 1 if angel_id else None,
        "angel_id": angel_id,
    }


def _seed(cfg: Config, **sheets: pl.DataFrame) -> None:
    """Write sheets into the test workbook."""
    default_raw = pl.DataFrame(
        [
            raw_child_row("c1"),
            raw_child_row("c2", "1B"),
            raw_child_row("orphan1", None),
        ],
        schema={
            "zite_record_id": pl.String,
            "first_name": pl.String,
            "primary_email": pl.String,
            "dob": pl.String,
            "age_at_xmas": pl.Int64,
            "age_reported": pl.Int64,
            "need_1": pl.String,
            "shoe_size": pl.String,
            "family_id": pl.Int64,
            "angel_id": pl.String,
        },
    )
    raw = sheets.pop("raw_child", default_raw)
    write_sheets(cfg.excel_file, "acquire", {"raw_child": raw})
    if sheets:
        write_sheets(cfg.excel_file, "screen", sheets)


DOB_CONFLICT_SCHEMA: dict[str, Any] = {
    "family_id": pl.Int64,
    "primary_email": pl.String,
    "child_a_zite_record_id": pl.String,
    "child_a_angel_id": pl.String,
    "child_a_first_name": pl.String,
    "child_a_dob": pl.String,
    "child_b_zite_record_id": pl.String,
    "child_b_angel_id": pl.String,
    "child_b_first_name": pl.String,
    "child_b_dob": pl.String,
    "gap_months": pl.Int64,
    "gap_days": pl.Int64,
}


def _seed_conflict(cfg: Config) -> str:
    """Seed one dob_conflict_log pair and return the expected c1 note.

    The pair is c1 (child_b) born 219 days after c2 (child_a).
    """
    write_sheets(
        cfg.excel_file,
        "acquire",
        {
            "dob_conflict_log": pl.DataFrame(
                [
                    {
                        "family_id": 1,
                        "primary_email": "fam@example.com",
                        "child_a_zite_record_id": "c2",
                        "child_a_angel_id": "1B",
                        "child_a_first_name": "Kiddo",
                        "child_a_dob": "2018-05-10",
                        "child_b_zite_record_id": "c1",
                        "child_b_angel_id": "1A",
                        "child_b_first_name": "Kiddo",
                        "child_b_dob": "2018-12-15",
                        "gap_months": 7,
                        "gap_days": 219,
                    }
                ],
                schema=DOB_CONFLICT_SCHEMA,
            )
        },
    )
    return (
        "Sibling DOB conflict: born 7 month(s) (219 day(s)) apart from"
        " sibling Kiddo (1B, dob 2018-05-10)"
    )


def test_queue_includes_unaudited_and_unscreened(cfg: Config) -> None:
    """Queue holds children without final rows or with audited == false (§7.1)."""
    _seed(cfg)
    state = AuditState(cfg)
    queue = state.queue()
    assert [item["zite_record_id"] for item in queue] == ["c1", "c2", "orphan1"]


def test_queue_excludes_audited_children(cfg: Config) -> None:
    """Children with audited == true never appear (§7.2)."""
    _seed(cfg)
    final = pl.DataFrame(
        [
            {
                "zite_record_id": "c1",
                "angel_id": "1A",
                "audited": True,
                "special_needs": False,
            },
            {
                "zite_record_id": "c2",
                "angel_id": "1B",
                "audited": False,
                "special_needs": False,
            },
        ],
        schema={
            "zite_record_id": pl.String,
            "angel_id": pl.String,
            "audited": pl.Boolean,
            "special_needs": pl.Boolean,
        },
    )
    write_sheets(cfg.excel_file, "audit", {"final_child": final})
    queue = AuditState(cfg).queue()
    assert [item["zite_record_id"] for item in queue] == ["c2", "orphan1"]


def test_queue_overwrite_requeues_audited_children(cfg: Config) -> None:
    """--overwrite queues audited children too (§7.1)."""
    _seed(cfg)
    state = AuditState(cfg)
    state.save("c1", {"audited": True, "special_needs": False})
    assert [item["zite_record_id"] for item in AuditState(cfg).queue()] == [
        "c2",
        "orphan1",
    ]
    overwrite_queue = AuditState(cfg, overwrite=True).queue()
    assert [item["zite_record_id"] for item in overwrite_queue] == [
        "c1",
        "c2",
        "orphan1",
    ]


def test_queue_adj_object_null_vs_sparse(cfg: Config) -> None:
    """Adj is the adj row, an all-null object, or null when unscreened (§7.1)."""
    adj = pl.DataFrame(
        [{"zite_record_id": "c1", "need_1": "winter coat that fits"}],
        schema={"zite_record_id": pl.String, "need_1": pl.String},
    )
    _seed(cfg, adj_child=adj)
    queue = AuditState(cfg).queue()
    by_id = {item["zite_record_id"]: item for item in queue}
    assert by_id["c1"]["adj"]["need_1"] == "winter coat that fits"
    assert by_id["orphan1"]["adj"] is None


def test_dob_conflict_notes_match_either_side(cfg: Config) -> None:
    """Notes build from both sides of each conflicting pair (§5.6, §7)."""
    note_c1 = _seed_conflict(cfg)
    _seed(cfg)
    log = read_sheet(cfg.excel_file, "dob_conflict_log")
    assert log is not None
    rows = log.to_dicts()
    assert dob_conflict_notes(rows, "c1") == note_c1
    assert dob_conflict_notes(rows, "c2") == (
        "Sibling DOB conflict: born 7 month(s) (219 day(s)) apart from"
        " sibling Kiddo (1A, dob 2018-12-15)"
    )
    assert dob_conflict_notes(rows, "orphan1") is None
    assert dob_conflict_notes(None, "c1") is None


def test_merge_anomoly_appends_once() -> None:
    """Conflict notes append after existing text exactly once."""
    notes = "Sibling DOB conflict: X"
    assert merge_anomoly(None, notes) == notes
    assert merge_anomoly("AI text", notes) == f"AI text; {notes}"
    assert merge_anomoly(f"AI text; {notes}", notes) == f"AI text; {notes}"
    assert merge_anomoly("AI text", None) == "AI text"
    assert merge_anomoly(None, None) is None


def test_queue_shows_conflict_note_for_unscreened_child(
    cfg: Config,
) -> None:
    """Unscreened conflict children get an adj carrying the note (§7.1)."""
    note_c1 = _seed_conflict(cfg)
    _seed(cfg)
    queue = AuditState(cfg).queue()
    by_id = {item["zite_record_id"]: item for item in queue}
    adj = by_id["c1"]["adj"]
    assert adj is not None
    assert adj["potential_anomoly"] == note_c1
    assert adj["need_1"] is None  # other fields stay null over raw
    assert by_id["c2"]["adj"]["potential_anomoly"] == (
        "Sibling DOB conflict: born 7 month(s) (219 day(s)) apart from"
        " sibling Kiddo (1A, dob 2018-12-15)"
    )
    assert by_id["orphan1"]["adj"] is None


def test_queue_merges_conflict_into_screened_adj(cfg: Config) -> None:
    """Screened adj text keeps the AI note plus the conflict note (§7.1)."""
    note_c1 = _seed_conflict(cfg)
    adj = pl.DataFrame(
        [
            {
                "zite_record_id": "c1",
                "potential_anomoly": "age_reported 7 differs from computed 8",
            }
        ],
        schema={
            "zite_record_id": pl.String,
            "potential_anomoly": pl.String,
        },
    )
    _seed(cfg, adj_child=adj)
    queue = AuditState(cfg).queue()
    by_id = {item["zite_record_id"]: item for item in queue}
    assert by_id["c1"]["adj"]["potential_anomoly"] == (
        f"age_reported 7 differs from computed 8; {note_c1}"
    )


def test_save_appends_conflict_note_idempotently(cfg: Config) -> None:
    """Saved final_child rows always carry the conflict note once (§7.3)."""
    note_c1 = _seed_conflict(cfg)
    _seed(cfg)
    state = AuditState(cfg)
    state.save("c1", {"audited": False, "special_needs": False})
    final = read_sheet(cfg.excel_file, "final_child")
    assert final is not None
    assert final.to_dicts()[0]["potential_anomoly"] == note_c1
    # re-save with the note already in the body: not duplicated
    state.save(
        "c1",
        {
            "audited": False,
            "special_needs": False,
            "potential_anomoly": note_c1,
        },
    )
    final = read_sheet(cfg.excel_file, "final_child")
    assert final is not None
    anomoly = final.to_dicts()[0]["potential_anomoly"] or ""
    assert anomoly.count("Sibling DOB conflict") == 1
    # auditor replaces the field with their own text: note re-appended
    state.save(
        "c1",
        {
            "audited": True,
            "special_needs": False,
            "potential_anomoly": "hand-written note",
        },
    )
    final = read_sheet(cfg.excel_file, "final_child")
    assert final is not None
    assert final.to_dicts()[0]["potential_anomoly"] == (
        f"hand-written note; {note_c1}"
    )


def test_queue_orphan_has_null_angel_id(cfg: Config) -> None:
    """Orphaned children appear with angel_id null (§7.2)."""
    _seed(cfg)
    orphan = next(
        item
        for item in AuditState(cfg).queue()
        if item["zite_record_id"] == "orphan1"
    )
    assert orphan["angel_id"] is None


def test_build_final_child_row_merge_rules() -> None:
    """Non-null adj values override raw; nulls keep raw (§7.3)."""
    raw = raw_child_row("c1")
    adj_body = {
        "need_1": "winter coat that fits",
        "need_2": None,
        "dob": None,
        "shoe_size": None,
        "potential_anomoly": "age_reported 7 differs from computed 8",
    }
    row = build_final_child_row(
        raw,
        adj_body,
        special_needs=True,
        audited=True,
        require_clarification=None,
    )
    assert row["need_1"] == "winter coat that fits"  # override
    assert row["dob"] == "2018-05-10"  # null adj keeps raw
    assert row["angel_id"] == "1A"  # copied from raw
    assert row["special_needs"] is True
    assert row["audited"] is True
    assert row["require_clarification"] is None
    assert row["audited_at"] is not None


def test_save_writes_and_rereads_row(cfg: Config) -> None:
    """A save writes the final_child row and can overwrite while unaudited (§7.3)."""
    _seed(cfg)
    state = AuditState(cfg)
    body = {
        "need_1": "winter coat that fits",
        "special_needs": False,
        "audited": False,
        "require_clarification": "call the family",
    }
    state.save("c1", body)
    final = read_sheet(cfg.excel_file, "final_child")
    assert final is not None and final.height == 1
    row = final.to_dicts()[0]
    assert row["need_1"] == "winter coat that fits"
    assert row["audited"] is False
    assert row["require_clarification"] == "call the family"
    # iterate: overwrite while unaudited
    body2 = dict(body, require_clarification=None, audited=True)
    state.save("c1", body2)
    final = read_sheet(cfg.excel_file, "final_child")
    assert final is not None and final.height == 1
    assert final.to_dicts()[0]["audited"] is True


def test_build_final_child_row_generated_info_sheet_default() -> None:
    """New rows carry generated_info_sheet False; True passes through (§13.3)."""
    row = build_final_child_row(
        raw_child_row("c1"),
        {},
        special_needs=False,
        audited=True,
        require_clarification=None,
    )
    assert row["generated_info_sheet"] is False
    row = build_final_child_row(
        raw_child_row("c1"),
        {},
        special_needs=False,
        audited=True,
        require_clarification=None,
        generated_info_sheet=True,
    )
    assert row["generated_info_sheet"] is True


def test_save_preserves_generated_info_sheet(cfg: Config) -> None:
    """Re-saving a child keeps its generated_info_sheet flag (§13.3)."""
    _seed(cfg)
    state = AuditState(cfg)
    state.save("c1", {"audited": False, "special_needs": False})
    # module 5 runs between audit saves: flag flips to True
    final = read_sheet(cfg.excel_file, "final_child")
    assert final is not None
    final = final.with_columns(
        pl.when(pl.col("zite_record_id") == "c1")
        .then(True)
        .otherwise(pl.col("generated_info_sheet"))
        .alias("generated_info_sheet")
    )
    write_sheets(cfg.excel_file, "generate", {"final_child": final})
    # a later audit save of the same child must not lose the flag
    state.save(
        "c1",
        {"audited": True, "special_needs": False, "need_1": "updated coat"},
    )
    # and a brand-new row must default to False
    state.save("c2", {"audited": True, "special_needs": False})
    final = read_sheet(cfg.excel_file, "final_child")
    assert final is not None
    rows = {row["zite_record_id"]: row for row in final.to_dicts()}
    assert rows["c1"]["generated_info_sheet"] is True
    assert rows["c1"]["need_1"] == "updated coat"
    assert rows["c2"]["generated_info_sheet"] is False


def test_save_frozen_row_returns_409(cfg: Config) -> None:
    """PUT for an audited child is rejected (§7.3)."""
    _seed(cfg)
    state = AuditState(cfg)
    state.save("c1", {"audited": True, "special_needs": False})
    with pytest.raises(AuditClientError) as error:
        state.save("c1", {"audited": False, "special_needs": False})
    assert error.value.status == 409


def test_save_overwrite_replaces_frozen_row(cfg: Config) -> None:
    """--overwrite bypasses the frozen-row 409 and re-saves (§7.3)."""
    _seed(cfg)
    state = AuditState(cfg, overwrite=True)
    state.save("c1", {"audited": True, "special_needs": False})
    state.save(
        "c1",
        {"audited": True, "special_needs": False, "need_1": "fixed coat"},
    )
    final = read_sheet(cfg.excel_file, "final_child")
    assert final is not None
    row = next(row for row in final.to_dicts() if row["zite_record_id"] == "c1")
    assert row["need_1"] == "fixed coat"
    assert row["audited"] is True


def test_save_orphan_cannot_be_marked_audited(cfg: Config) -> None:
    """Orphaned children cannot be marked audited (§7.2)."""
    _seed(cfg)
    state = AuditState(cfg)
    with pytest.raises(AuditClientError) as error:
        state.save("orphan1", {"audited": True, "special_needs": False})
    assert error.value.status == 400
    # saving with audited = false is allowed
    state.save(
        "orphan1",
        {
            "audited": False,
            "special_needs": False,
            "require_clarification": "find family",
        },
    )
    final = read_sheet(cfg.excel_file, "final_child")
    assert final is not None and final.height == 1


def test_save_unknown_child_returns_404(cfg: Config) -> None:
    """Unknown zite_record_ids are rejected."""
    _seed(cfg)
    with pytest.raises(AuditClientError) as error:
        AuditState(cfg).save("nope", {"audited": True, "special_needs": False})
    assert error.value.status == 404


def test_save_unknown_keys_return_400(cfg: Config) -> None:
    """Only known child columns are accepted (§7.1)."""
    _seed(cfg)
    with pytest.raises(AuditClientError) as error:
        AuditState(cfg).save(
            "c1", {"nickname": "x", "audited": False, "special_needs": False}
        )
    assert error.value.status == 400


def test_http_api_round_trip(cfg: Config) -> None:
    """The HTTP server serves the page, queue, and PUT endpoint (§7.1)."""
    _seed(cfg)
    from http.server import HTTPServer

    from ccat.audit.server import make_handler

    server = HTTPServer(("127.0.0.1", 0), make_handler(AuditState(cfg)))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        page = requests.get(f"{base}/", timeout=5)
        assert page.status_code == 200
        assert "Child Audit" in page.text
        queue = requests.get(f"{base}/api/queue", timeout=5).json()
        assert len(queue) == 3
        save = requests.put(
            f"{base}/api/children/c1",
            json={"need_1": "fixed", "special_needs": True, "audited": True},
            timeout=5,
        )
        assert save.status_code == 200
        frozen = requests.put(
            f"{base}/api/children/c1",
            json={"need_1": "again", "special_needs": True, "audited": True},
            timeout=5,
        )
        assert frozen.status_code == 409
        bad_json = requests.put(
            f"{base}/api/children/c2",
            data="not json",
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        assert bad_json.status_code == 400
        missing = requests.get(f"{base}/api/nothing", timeout=5)
        assert missing.status_code == 404
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_run_serves_until_stopped(cfg: Config, monkeypatch) -> None:
    """run() blocks in serve_forever and shuts down on KeyboardInterrupt."""
    from http.server import HTTPServer

    original = HTTPServer.serve_forever

    def stop_immediately(self: HTTPServer) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(HTTPServer, "serve_forever", stop_immediately)
    run(cfg)  # returns without raising
    monkeypatch.setattr(HTTPServer, "serve_forever", original)


def test_queue_test_mode_limits_to_first_family(cfg: Config) -> None:
    """In test mode the queue only holds the first family's children."""
    _seed(cfg)
    queue = AuditState(cfg, test_mode=True).queue()
    assert [item["zite_record_id"] for item in queue] == ["c1", "c2"]


def test_queue_adj_includes_read_only_age(cfg: Config) -> None:
    """The queue's adj object carries age_at_xmas for display (§5.5)."""
    adj = pl.DataFrame(
        [
            {
                "zite_record_id": "c1",
                "need_1": "winter coat that fits",
                "age_at_xmas": "8",
            }
        ],
        schema={
            "zite_record_id": pl.String,
            "need_1": pl.String,
            "age_at_xmas": pl.String,
        },
    )
    _seed(cfg, adj_child=adj)
    item = AuditState(cfg).queue()[0]
    assert item["adj"]["age_at_xmas"] == "8"
    assert item["adj"]["need_1"] == "winter coat that fits"


def test_save_rejects_editing_read_only_age(cfg: Config) -> None:
    """age_at_xmas is not an editable adj field; PUTing it is a 400."""
    _seed(cfg)
    state = AuditState(cfg)
    with pytest.raises(AuditClientError, match="unknown keys"):
        state.save("c1", {"age_at_xmas": "99", "audited": False})


def test_save_test_mode_fires_on_complete_after_first_family(
    cfg: Config,
) -> None:
    """on_complete fires exactly when the first family is fully audited."""
    _seed(cfg)
    state = AuditState(cfg, test_mode=True)
    called: list[bool] = []
    state.on_complete = lambda: called.append(True)
    state.save("c1", {"audited": True, "special_needs": False})
    assert called == []  # c2 still pending
    state.save("c2", {"audited": True, "special_needs": False})
    assert called == [True]  # first family complete


def test_run_test_mode_exits_when_first_family_audited(
    cfg: Config, monkeypatch
) -> None:
    """run(test=True) exits without serving when the family is done."""
    _seed(cfg)
    state = AuditState(cfg)
    state.save("c1", {"audited": True, "special_needs": False})
    state.save("c2", {"audited": True, "special_needs": False})
    from http.server import ThreadingHTTPServer

    def fail(self: ThreadingHTTPServer) -> None:
        raise AssertionError("server should not start in test mode")

    monkeypatch.setattr(ThreadingHTTPServer, "serve_forever", fail)
    run(cfg, test=True)  # returns without serving


def test_http_test_mode_server_stops_after_first_family(cfg: Config) -> None:
    """The HTTP server shuts itself down after the family is audited."""
    _seed(cfg)
    from http.server import ThreadingHTTPServer

    from ccat.audit.server import make_handler

    state = AuditState(cfg, test_mode=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    state.on_complete = server.shutdown
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        queue = requests.get(f"{base}/api/queue", timeout=5).json()
        assert [item["zite_record_id"] for item in queue] == ["c1", "c2"]
        for record_id in ("c1", "c2"):
            response = requests.put(
                f"{base}/api/children/{record_id}",
                json={"audited": True, "special_needs": False},
                timeout=5,
            )
            assert response.status_code == 200
        thread.join(timeout=5)  # server stopped itself
        assert not thread.is_alive()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
