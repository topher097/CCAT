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


def test_save_frozen_row_returns_409(cfg: Config) -> None:
    """PUT for an audited child is rejected (§7.3)."""
    _seed(cfg)
    state = AuditState(cfg)
    state.save("c1", {"audited": True, "special_needs": False})
    with pytest.raises(AuditClientError) as error:
        state.save("c1", {"audited": False, "special_needs": False})
    assert error.value.status == 409


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
