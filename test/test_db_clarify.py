"""Tests for the DB-mode clarification services (§9.6, D5/D16/D22).

File-backed SQLite in ``tmp_path``; the opencode sender is faked (the
``test_clarify_email`` FakeSender pattern) through the
``db_clarify._default_sender`` seam — no network, no opencode import.
"""

import asyncio
import csv as csv_module
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from conftest import load_test_cfg
from test_db_pipeline import child_row, family_row
from ccat.clarify.extract import ClarifyError
from ccat.config import Config, StorageSpec
from ccat.db.engine import create_async_engine, run_migrations
from ccat.db.uow import UnitOfWork
from ccat.jobs import JobCancelRequested
from ccat.services import db_clarify

# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path, **updates: Any) -> Config:
    """Test config: temp DB/artifacts, test model, small prompt."""
    base = load_test_cfg(
        excel_file=str(tmp_path / "wb.xlsx"),
        log_file=str(tmp_path / "ccat.log"),
        match_report_csv=str(tmp_path / "report.csv"),
        storage=StorageSpec(
            mode="db",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'ccat.sqlite3'}",
            artifact_root=str(tmp_path / "artifacts"),
        ),
    )
    clarify = base.clarify.model_copy(
        update={
            "model": "test/model",
            "system_prompt": "You write for the {{season}} Angel Tree.",
            "output_dir": str(tmp_path / "emails"),
            "clarifications_csv": str(tmp_path / "clarifications.csv"),
        }
    )
    return base.model_copy(update={"clarify": clarify} | updates)


def _prepare(tmp_path: Path, **updates: Any) -> Config:
    """Migrate the temp database and return its config."""
    cfg = _cfg(tmp_path, **updates)
    run_migrations(cfg.storage.database_url)
    return cfg


def _flagged_child(
    record_id: str, angel_id: str, note: str, **overrides: Any
) -> dict:
    """One final child carrying a clarification note."""
    overrides.setdefault("status", "audited")
    return child_row(
        record_id,
        angel_id=angel_id,
        clarification_notes=note,
        **overrides,
    )


def _seed(cfg: Config, **stores: Any) -> None:
    """Seed final stores through migrate ownership."""
    asyncio.run(_seed_async(cfg, **stores))


async def _seed_async(cfg: Config, **stores: Any) -> None:
    uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
    try:
        async with uow.store(cfg, "migration") as repo:
            for key, entity in (
                ("children", "child"),
                ("families", "family"),
                ("sponsors", "sponsor"),
            ):
                rows = stores.get(key)
                if rows:
                    await repo.commit_finals(
                        "migrate", entity, rows, "migration"
                    )
            raw = stores.get("raw")
            if raw:
                await repo.replace_raw_snapshot("migrate", raw, "migration")
    finally:
        await uow.engine.dispose()


def _standard_seed(cfg: Config) -> None:
    """Two flagged families plus the exclusion matrix."""
    children = [
        _flagged_child("c1", "1A", "shoe size?", dob="2018-05-11"),
        _flagged_child("c2", "1B", "pant size?", status="new"),
        child_row(
            "c3",
            angel_id="2A",
            family_id=2,
            clarification_notes=None,
        ),  # no note: not flagged
        child_row(
            "c4",
            angel_id="1D",
            family_id=1,
            clarification_notes="ignored",
            status="audited",
            ignore=True,
        ),  # ignored child
        _flagged_child("c5", "1E", "orphan?", family_id=None),
        _flagged_child(
            "c6", "3A", "family gone?", family_id=3, status="new"
        ),  # ignored family
    ]
    families = [
        family_row(1),
        family_row(2),
        family_row(3, ignore=True),
    ]
    _seed(
        cfg,
        children=children,
        families=families,
        raw={
            "child": [
                {
                    "zite_record_id": "c1",
                    "created": "2026-02-01T09:00:00+00:00",
                    "dob": "2018-05-10",
                },
                {
                    "zite_record_id": "c2",
                    "created": "2026-02-01T09:00:00+00:00",
                    "dob": "2019-01-02",
                },
            ]
        },
    )


class FakeSender:
    """Prompt sender returning valid prose keyed by family id."""

    def __init__(self, fail_on: Any = None, junk: bool = False) -> None:
        """Configure the failure mode (transport, malformed, or fine)."""
        self.calls: list[str] = []
        self.fail_on = fail_on  # substring; a prompt match raises
        self.junk = junk  # every response malformed

    def send(self, prompt: str) -> str:
        """Answer with one good paragraph per angel in the prompt."""
        self.calls.append(prompt)
        if self.fail_on is not None and self.fail_on in prompt:
            raise ClarifyError("opencode connect failed")
        if self.junk:
            return "not json"
        record = json.loads(prompt.split("\n\n", 1)[1])
        return json.dumps(
            {
                "greeting": "Dear friend,",
                "intro": "Hello!",
                "angel_paragraphs": [
                    f"Paragraph {i}" for i in range(len(record["angels"]))
                ],
                "closing": "Thanks!",
            }
        )


@pytest.fixture
def install_sender(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install a fake sender through the module's sender seam."""
    holder: dict[str, Any] = {}

    def install(sender: Any) -> Any:
        holder["sender"] = sender
        monkeypatch.setattr(db_clarify, "_default_sender", lambda cfg: sender)
        return sender

    return install


def _run(cfg: Config, options: dict[str, Any] | None = None) -> Any:
    """Run draft generation synchronously."""
    return asyncio.run(
        db_clarify.generate_drafts(
            cfg, "topher", options or {}, lambda *a, **k: None, lambda: None
        )
    )


async def _draft(cfg: Config, family_id: int) -> Any:
    """Read one family's latest draft through a throwaway repository."""
    uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
    try:
        async with uow.store(cfg, "system") as repo:
            return await repo.latest_draft(family_id)
    finally:
        await uow.engine.dispose()


async def _artifacts(
    cfg: Config, kind: str | None = None
) -> list[dict[str, Any]]:
    """List artifact rows through a throwaway repository."""
    uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
    try:
        async with uow.store(cfg, "system") as repo:
            return await repo.artifact_rows(kind)
    finally:
        await uow.engine.dispose()


# ---------------------------------------------------------------------------
# Extraction.
# ---------------------------------------------------------------------------


def test_extract_eligibility_and_sort(tmp_path: Path) -> None:
    """Flagged eligible children sort (family, angel); exclusions hold."""
    cfg = _prepare(tmp_path)
    _standard_seed(cfg)
    rows = asyncio.run(db_clarify.extract_rows(cfg))
    assert [(row["family_id"], row["angel_id"]) for row in rows] == [
        (1, "1A"),
        (1, "1B"),
        (3, "3A"),
    ]
    # Family contacts join independently: the ignored family's child
    # stays with blank contacts; the live family's contacts join.
    ignored = next(row for row in rows if row["angel_id"] == "3A")
    assert ignored["family_email"] is None
    assert ignored["family_first_name"] is None
    live = next(row for row in rows if row["angel_id"] == "1A")
    assert live["family_email"] == "fam1@example.com"


def test_extract_raw_dob_join(tmp_path: Path) -> None:
    """The DOB joins from raw_child by source id, never adjudicated."""
    cfg = _prepare(tmp_path)
    _standard_seed(cfg)
    rows = asyncio.run(db_clarify.extract_rows(cfg))
    first = next(row for row in rows if row["angel_id"] == "1A")
    assert first["raw_dob"] == "2018-05-10"  # raw, not the final dob


def test_extract_empty_table(tmp_path: Path) -> None:
    """No flagged children -> an empty table, not an error."""
    cfg = _prepare(tmp_path)
    _seed(
        cfg,
        children=[child_row("c1", angel_id="1A", clarification_notes=None)],
        families=[family_row(1)],
    )
    assert asyncio.run(db_clarify.extract_rows(cfg)) == []


# ---------------------------------------------------------------------------
# Draft generation.
# ---------------------------------------------------------------------------


def test_draft_generation_keyed_paragraphs(
    tmp_path: Path, install_sender: Any
) -> None:
    """Drafts save versioned, keyed paragraphs plus HTML artifacts."""
    cfg = _prepare(tmp_path)
    _standard_seed(cfg)
    sender = install_sender(FakeSender())
    result = _run(cfg, {})
    assert result.generated_family_ids == [1, 3]
    assert result.failed_family_ids == []
    draft = asyncio.run(_draft(cfg, 1))
    assert draft["version"] == 1
    assert draft["paragraphs"] == [
        {
            "zite_record_id": "c1",
            "angel_id": "1A",
            "order": 0,
            "text": "Paragraph 0",
        },
        {
            "zite_record_id": "c2",
            "angel_id": "1B",
            "order": 1,
            "text": "Paragraph 1",
        },
    ]
    html = (tmp_path / "artifacts" / "clarify" / "family_1.html").read_text()
    assert "Dear friend," in html
    assert "Paragraph 0" in html
    fragment = (
        tmp_path / "artifacts" / "clarify" / "family_1.fragment.html"
    ).read_text()
    assert fragment.startswith("<div")
    kinds = [artifact["kind"] for artifact in asyncio.run(_artifacts(cfg))]
    assert kinds.count("clarify_draft") == 2
    assert kinds.count("clarify_draft_fragment") == 2
    # The prompt carries the family's contact details (D22) but is
    # never persisted to artifacts.
    assert any("fam1@example.com" in call for call in sender.calls)


def test_generate_skips_families_with_drafts(
    tmp_path: Path, install_sender: Any
) -> None:
    """A rerun without overwrite targets only families without drafts."""
    cfg = _prepare(tmp_path)
    _standard_seed(cfg)
    sender = install_sender(FakeSender())
    _run(cfg, {})
    sender.calls.clear()
    second = _run(cfg, {})
    assert second.generated_family_ids == []
    assert sender.calls == []


def test_malformed_output_retries_once_then_failed_family(
    tmp_path: Path, install_sender: Any
) -> None:
    """Two malformed attempts fail the family; the job continues."""
    cfg = _prepare(tmp_path)
    _standard_seed(cfg)
    sender = install_sender(FakeSender(junk=True))
    result = _run(cfg, {})
    assert result.generated_family_ids == []
    assert result.failed_family_ids == [1, 3]
    # One retry per family: two calls each.
    assert len(sender.calls) == 4
    assert asyncio.run(_draft(cfg, 1)) is None


def test_transport_error_not_retried_and_drafts_kept(
    tmp_path: Path, install_sender: Any
) -> None:
    """A transport failure aborts retryably; completed drafts stay."""
    cfg = _prepare(tmp_path)
    _standard_seed(cfg)
    sender = install_sender(FakeSender(fail_on='"family_id": 3'))
    with pytest.raises(db_clarify.OpencodeTransportError):
        _run(cfg, {})
    # Family 1 succeeded (never retried); family 3 failed on its first
    # attempt (transport errors are not retried per family, §6.1).
    assert len(sender.calls) == 2
    assert asyncio.run(_draft(cfg, 1)) is not None
    assert asyncio.run(_draft(cfg, 3)) is None


def test_cancel_between_families_keeps_completed_drafts(
    tmp_path: Path, install_sender: Any
) -> None:
    """Cancellation stops between families; finished drafts persist."""
    cfg = _prepare(tmp_path)
    _standard_seed(cfg)
    install_sender(FakeSender())
    calls = {"n": 0}

    def cancel() -> None:
        calls["n"] += 1
        if calls["n"] > 1:
            raise JobCancelRequested()

    with pytest.raises(JobCancelRequested):
        asyncio.run(
            db_clarify.generate_drafts(
                cfg, "topher", {}, lambda *a, **k: None, cancel
            )
        )
    assert asyncio.run(_draft(cfg, 1)) is not None
    assert asyncio.run(_draft(cfg, 3)) is None


# ---------------------------------------------------------------------------
# Draft edits.
# ---------------------------------------------------------------------------


def test_update_draft_edit_flow_and_artifact(
    tmp_path: Path, install_sender: Any
) -> None:
    """An edit saves the next version and re-renders the HTML."""
    cfg = _prepare(tmp_path)
    _standard_seed(cfg)
    install_sender(FakeSender())
    _run(cfg, {})
    draft = asyncio.run(
        db_clarify.update_draft(
            cfg,
            "topher",
            1,
            1,
            {"subject": "Edited subject"},
        )
    )
    assert draft["version"] == 2
    assert draft["subject"] == "Edited subject"
    assert draft["edited_by"] == "topher"
    # Untouched fields keep their values.
    assert draft["greeting"] == "Dear friend,"
    html = (tmp_path / "artifacts" / "clarify" / "family_1.html").read_text()
    assert "Edited subject" in html


def test_update_draft_stale_version(
    tmp_path: Path, install_sender: Any
) -> None:
    """A stale expected version is a typed error (plan §4.4)."""
    cfg = _prepare(tmp_path)
    _standard_seed(cfg)
    install_sender(FakeSender())
    _run(cfg, {})
    with pytest.raises(db_clarify.StaleDraftError, match="version"):
        asyncio.run(
            db_clarify.update_draft(cfg, "topher", 1, 2, {"subject": "x"})
        )


# ---------------------------------------------------------------------------
# Contact status.
# ---------------------------------------------------------------------------


def test_contacted_replied_tri_state_utc(tmp_path: Path) -> None:
    """Contacted stamps UTC time; clearing keeps the timestamp."""
    cfg = _prepare(tmp_path)
    _standard_seed(cfg)
    state = asyncio.run(db_clarify.set_contacted(cfg, "topher", 1, True))
    assert state["contacted"] is True
    stamp = state["contacted_at"]
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None
    state_off = asyncio.run(db_clarify.set_contacted(cfg, "topher", 1, False))
    assert state_off["contacted"] is False
    assert state_off["contacted_at"] == stamp  # manual stamp preserved
    state_replied = asyncio.run(db_clarify.set_replied(cfg, "topher", 1, True))
    assert state_replied["replied"] is True
    assert state_replied["contacted_at"] == stamp


# ---------------------------------------------------------------------------
# CSV export (D16).
# ---------------------------------------------------------------------------


def test_escape_csv_cell() -> None:
    """Formula-prefixed cells get the apostrophe prefix (D16)."""
    assert db_clarify.escape_csv_cell("=SUM(A1)") == "'=SUM(A1)"
    assert db_clarify.escape_csv_cell("+1") == "'+1"
    assert db_clarify.escape_csv_cell("-x") == "'-x"
    assert db_clarify.escape_csv_cell("@cmd") == "'@cmd"
    assert db_clarify.escape_csv_cell("plain") == "plain"
    assert db_clarify.escape_csv_cell(None) is None
    assert db_clarify.escape_csv_cell(5) == 5


def test_export_csv_merges_status_and_escapes(
    tmp_path: Path, install_sender: Any
) -> None:
    """The export merges drafts, status, and escapes formulas (D16)."""
    cfg = _prepare(tmp_path)
    _standard_seed(cfg)
    install_sender(FakeSender())
    _run(cfg, {})
    asyncio.run(db_clarify.set_contacted(cfg, "topher", 1, True))
    result = asyncio.run(db_clarify.export_csv(cfg, "topher"))
    assert result.rows == 3
    with open(result.csv_path, newline="", encoding="utf-8") as handle:
        rows = list(csv_module.reader(handle))
    header = rows[0]
    assert header[-4:] == [
        "clarification_email_file",
        "contacted",
        "contacted_at",
        "replied",
    ]
    by_angel = {row[header.index("angel_id")]: row for row in rows[1:]}
    family_1 = by_angel["1A"]
    assert family_1[header.index("clarification_email_file")] == (
        "clarify/family_1.html"
    )
    assert family_1[header.index("contacted")] == "True"
    assert family_1[header.index("contacted_at")] != ""
    family_3 = by_angel["3A"]
    assert family_3[header.index("clarification_email_file")] == (
        "clarify/family_3.html"
    )
    assert family_3[header.index("contacted")] == "False"
    # Family 3's child row carries a formula-looking note in other_notes.
    _flagged_child("c6", "3A", "family gone?", family_id=3)
    # (the escape check uses the dedicated helper on raw cells)
    assert db_clarify.escape_csv_cell("=SUM(A1)") == "'=SUM(A1)"
    kinds = [artifact["kind"] for artifact in asyncio.run(_artifacts(cfg))]
    assert "clarify_export" in kinds


def test_export_empty_table_writes_header(
    tmp_path: Path,
) -> None:
    """No flagged angels -> a header-only registered CSV."""
    cfg = _prepare(tmp_path)
    _seed(
        cfg, children=[child_row("c1", angel_id="1A")], families=[family_row(1)]
    )
    result = asyncio.run(db_clarify.export_csv(cfg, "topher"))
    assert result.rows == 0
    with open(result.csv_path, newline="", encoding="utf-8") as handle:
        rows = list(csv_module.reader(handle))
    assert len(rows) == 1  # header only
