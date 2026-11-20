"""Tests for ``ccat db-import`` and ``ccat workbook-export`` (plan §10).

Builds a golden persistent-final workbook from the shared
``_seed_golden`` pattern, then adds timestamped match archives, a
current match report, the four log sheets, the clarify sidecar and
drafts, and fake child-sheet PDFs — covering duplicates, orphans,
raw-missing, ignored rows, human edits, screened/unscreened children,
assignments, generated markers, one marker/file mismatch, one orphan
PDF, and one HTML-only draft.
"""

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from openpyxl import load_workbook
from pypdf import PdfWriter
from sqlalchemy import create_engine, inspect, select

import ccat
from ccat.config import Config
from ccat.db.engine import (
    ALEMBIC_DIR,
    create_async_engine,
    run_migrations,
    sync_url_from,
)
from ccat.db.migration_io import (
    ExportError,
    ImportError as DbImportError,
    run as db_import,
    workbook_export_run,
)
from ccat.db.models import Season
from ccat.db.repositories import SeasonStoreRepository, ensure_season_row
from ccat.db.uow import UnitOfWork
from ccat.acquire import DOB_CONFLICT_LOG_SCHEMA
from ccat.audit.store import build_final_frame
from ccat.io import list_sheets, read_sheet, write_sheets
from ccat.screening import (
    ADDRESS_VALIDATION_LOG_SCHEMA,
    DUPLICATE_LOG_SCHEMA,
    EMAIL_VALIDATION_LOG_SCHEMA,
)
from conftest import load_output_cfg
from test_persistent_integration import _final, _seed_golden


# ---------------------------------------------------------------------------
# Fixtures: config, workbook source, and DB read helpers.
# ---------------------------------------------------------------------------


def _import_cfg(tmp_path: Path) -> Config:
    """Config with DB storage and every output root in the temp dir."""
    base = load_output_cfg(tmp_path)
    return base.model_copy(
        update={
            "storage": _storage(tmp_path),
            "report": base.report.model_copy(
                update={"output_dir": str(tmp_path / "reports")}
            ),
        }
    )


def _storage(tmp_path: Path, name: str = "ccat.sqlite3") -> Any:
    from ccat.config import StorageSpec

    return StorageSpec(
        mode="db",
        database_url=f"sqlite+aiosqlite:///{tmp_path / name}",
    )


def _tiny_pdf(path: Path) -> None:
    """Write one minimal valid PDF."""
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as stream:
        writer.write(stream)


def _match_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows, schema={"kind": pl.String, "ref": pl.String, "reason": pl.String}
    )


def _log_frame(
    schema: dict[str, Any], rows: list[dict[str, Any]]
) -> pl.DataFrame:
    return pl.DataFrame(
        [{column: row.get(column) for column in schema} for row in rows],
        schema=dict(schema),
    )


def _archive_generation(
    cfg: Config,
    stamp: str,
    families: list[dict[str, Any]],
    sponsors: list[dict[str, Any]],
    report_rows: list[dict[str, Any]],
) -> None:
    """Archive the current match state under timestamped sheet names."""
    write_sheets(
        cfg.excel_file,
        "match",
        {
            "final_family": build_final_frame("family", families),
            "final_sponsor": build_final_frame("sponsor", sponsors),
            "match_report": _match_frame(report_rows),
        },
        renames={
            name: f"{name}_{stamp}"
            for name in ("final_family", "final_sponsor", "match_report")
        },
    )


def _sheet_dicts(source: Path, sheet: str) -> list[dict[str, Any]]:
    """Read one sheet that the fixture just wrote (never absent)."""
    frame = read_sheet(source, sheet)
    assert frame is not None
    return frame.to_dicts()


def _build_source(cfg: Config) -> Path:
    """Build the complete golden source workbook and its side files."""
    _seed_golden(cfg)
    source = Path(cfg.excel_file)
    # Extend the golden finals: AI notes on the audited child, the
    # sponsor's legacy create spelling, one marker-mismatch child, and
    # one orphaned final child.
    children = _sheet_dicts(source, "final_child")
    for row in children:
        if row["zite_record_id"] == "c-old-original":
            row["potential_anomoly"] = "AI flagged a need mismatch"
    children.append(
        _final(
            "child",
            zite_record_id="c-pending",
            family_id=1,
            angel_id="1B",
            primary_email="duplicate@example.com",
            first_name="Pending",
            dob="2021-06-15",
            age_reported=5,
            gender="Female",
            need_1="jacket",
            screened=False,
            generated_info_sheet=False,
        )
    )
    children.append(
        _final(
            "child",
            zite_record_id="c-orphan-final",
            family_id=None,
            angel_id=None,
            primary_email="orphan@example.com",
            first_name="Lone",
            need_1="hat",
            screened=False,
            generated_info_sheet=False,
        )
    )
    sponsors = _sheet_dicts(source, "final_sponsor")
    sponsors[0]["create"] = "2026-01-01T00:00:00Z"
    write_sheets(
        source,
        "migrate",
        {
            "final_child": build_final_frame("child", children),
            "final_sponsor": build_final_frame("sponsor", sponsors),
        },
        xmas=cfg.xmas,
    )

    # Timestamped match archives oldest-first, then the current report.
    family = _sheet_dicts(source, "final_family")
    sponsor = _sheet_dicts(source, "final_sponsor")
    _archive_generation(
        cfg,
        "20261101_090000",
        family,
        sponsor,
        [{"kind": "assigned", "ref": "1A", "reason": "first match"}],
    )
    _archive_generation(
        cfg,
        "20261102_090000",
        family,
        sponsor,
        [{"kind": "assigned", "ref": "1A", "reason": "second match"}],
    )
    write_sheets(
        source,
        "match",
        {
            "match_report": _match_frame(
                [
                    {"kind": "assigned", "ref": "1A", "reason": "current"},
                    {"kind": "split", "ref": "1", "reason": "overfull"},
                ]
            )
        },
    )

    # The four operational logs (screen owns them).
    write_sheets(
        source,
        "screen",
        {
            "duplicate_log": _log_frame(
                DUPLICATE_LOG_SCHEMA,
                [
                    {
                        "sheet": "family_submissions",
                        "group_key": "duplicate@example.com",
                        "zite_record_id": "f-later",
                        "created": "2026-01-03T00:00:00Z",
                        "survivor": False,
                        "conflict_columns": "first_name",
                        "conflict_details": "Family One vs Family One",
                        "record": "{}",
                    }
                ],
            ),
            "dob_conflict_log": _log_frame(
                DOB_CONFLICT_LOG_SCHEMA,
                [
                    {
                        "family_id": 1,
                        "primary_email": "duplicate@example.com",
                        "child_a_zite_record_id": "c-old-original",
                        "child_a_angel_id": "1A",
                        "child_a_first_name": "Alpha",
                        "child_a_dob": "2020-01-01",
                        "child_b_zite_record_id": "c-sibling-conflict",
                        "child_b_angel_id": "1B",
                        "child_b_first_name": "Beta",
                        "child_b_dob": "2020-02-01",
                        "gap_months": 0,
                        "gap_days": 31,
                    }
                ],
            ),
            "email_validation_log": _log_frame(
                EMAIL_VALIDATION_LOG_SCHEMA,
                [
                    {
                        "source_sheet": "final_child",
                        "zite_record_id": "c-old-original",
                        "field": "primary_email",
                        "email": "Duplicate@Example.com",
                        "status": "valid",
                        "sub_status": None,
                        "did_you_mean": None,
                        "response_json": "{}",
                        "checked_at": "2026-11-01T00:00:00Z",
                    },
                    {
                        "source_sheet": "final_child",
                        "zite_record_id": "c-pending",
                        "field": "primary_email",
                        "email": "duplicate@example.com",
                        "status": "valid",
                        "sub_status": None,
                        "did_you_mean": None,
                        "response_json": "{}",
                        "checked_at": "2026-11-01T00:00:01Z",
                    },
                    {
                        "source_sheet": "final_sponsor",
                        "zite_record_id": "s-current",
                        "field": "email",
                        "email": "current@example.com",
                        "status": "valid",
                        "sub_status": None,
                        "did_you_mean": None,
                        "response_json": "{}",
                        "checked_at": "2026-11-01T00:00:02Z",
                    },
                ],
            ),
            "address_validation_log": _log_frame(
                ADDRESS_VALIDATION_LOG_SCHEMA,
                [
                    {
                        "source_sheet": "final_family",
                        "zite_record_id": "f-early",
                        "query": "1 Main St Hancock MI 49930",
                        "status": "success",
                        "display_name": "1 Main St",
                        "lat": "47.0",
                        "lon": "-88.0",
                        "response_json": "{}",
                        "checked_at": "2026-11-01T00:00:03Z",
                    },
                    {
                        "source_sheet": "final_family",
                        "zite_record_id": "f-early",
                        "query": "2 Main St Hancock MI 49930",
                        "status": "success",
                        "display_name": "2 Main St",
                        "lat": "47.1",
                        "lon": "-88.1",
                        "response_json": "{}",
                        "checked_at": "2026-11-01T00:00:04Z",
                    },
                ],
            ),
        },
    )

    # Clarify sidecar (naive-local stamps) and drafts.
    clarify = Path(cfg.clarify.output_dir)
    clarify.mkdir(parents=True, exist_ok=True)
    (clarify / "clarification_status.json").write_text(
        json.dumps(
            {
                "1": {
                    "contacted": True,
                    "contacted_at": "2026-12-01T10:30:00",
                    "replied": True,
                }
            }
        )
    )
    (clarify / "family_1.html").write_text("<html>draft</html>")
    (clarify / "family_1.json").write_text(
        json.dumps(
            {
                "subject": "Quick question",
                "greeting": "Dear Family,",
                "intro": "Thanks for registering.",
                "angel_paragraphs": ["About 1A.", "About 1B."],
                "closing": "Please reply.",
            }
        )
    )
    (clarify / "family_8.html").write_text("<html>legacy only</html>")

    # Fake child-sheet PDFs: two marker-true children, one marker-false
    # child, and one orphan PDF with no matching angel.
    hancock = Path(cfg.info_sheet.output_dir) / "hancock"
    hancock.mkdir(parents=True, exist_ok=True)
    for angel in ("1A", "1Z", "1B", "9Z"):
        _tiny_pdf(hancock / f"AT{cfg.season}_{angel}.pdf")
    return source


def _read_db_rows(cfg: Config, entity: str) -> list[dict[str, Any]]:
    """Read one final store from the DB (plain rows, no metadata)."""

    async def main() -> list[dict[str, Any]]:
        engine = create_async_engine(cfg.storage.database_url)
        uow = UnitOfWork(engine)
        try:
            async with uow.session() as session:
                season = (
                    await session.scalars(
                        select(Season).where(Season.name == cfg.season)
                    )
                ).one()
                repo = SeasonStoreRepository(session, season)
                return [
                    {
                        key: value
                        for key, value in row.items()
                        if not key.startswith("_")
                    }
                    for row in await repo.final_rows(entity)
                ]
        finally:
            await engine.dispose()

    return asyncio.run(main())


def _db_query(cfg: Config, sql: str) -> list[tuple]:
    """Run one read-only SQL statement against the import DB."""
    path = str(cfg.storage.database_url).split("///", 1)[1]
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _report_of(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Full import.
# ---------------------------------------------------------------------------


def test_full_import_validates_and_reports(tmp_path: Path) -> None:
    """Everything imports, validates, and reports (plan §10)."""
    cfg = _import_cfg(tmp_path)
    source = _build_source(cfg)
    report_path = db_import(cfg, str(source))

    report = _report_of(report_path)
    assert report["batch"]["status"] == "completed"
    assert report["source"]["sha256_before"] == report["source"]["sha256_after"]
    assert report["validation"], "validation checks were recorded"
    assert all(check["pass"] for check in report["validation"]), report[
        "validation"
    ]

    counts = report["counts"]
    assert counts["child"]["imported"] == 4
    assert counts["child"]["orphan"] == 1  # c-orphan-final
    assert counts["child"]["raw_missing"] == 3
    assert counts["family"]["imported"] == 2
    assert counts["sponsor"]["imported"] == 2
    assert counts["logs"]["email_validation_log"] == 3

    # Per-field DB parity for two children: the human-edited audited
    # child and the ignored raw-missing child.
    children = {
        row["zite_record_id"]: row for row in _read_db_rows(cfg, "child")
    }
    edited = children["c-old-original"]
    assert edited["need_1"] == "HUMAN CORRECTED COAT"
    assert edited["angel_id"] == "1A"
    assert edited["screened"] is True
    assert edited["generated_info_sheet"] is True
    assert edited["potential_anomoly"] == "AI flagged a need mismatch"
    ignored = children["c-raw-missing-ignored"]
    assert ignored["ignore"] is True
    assert ignored["angel_id"] == "1Z"
    assert children["c-pending"]["screened"] is False
    assert children["c-orphan-final"]["family_id"] is None

    # Sponsor created mapping and family assignments.
    sponsors = {row["sponsor_id"]: row for row in _read_db_rows(cfg, "sponsor")}
    assert sponsors[1]["create"] == "2026-01-01T00:00:00Z"
    assert sponsors[1]["assigned_angel_ids"] == "1A"
    assert sponsors[8]["ignore"] is True
    families = {row["family_id"]: row for row in _read_db_rows(cfg, "family")}
    assert families[1]["assigned_sponsor_id"] == 1
    assert families[8]["status"] == "ignored"

    # Artifacts: linked sheets incl. the marker mismatch, plus the
    # orphan PDF (registered, never deleted). Draft HTML registers
    # through the clarify flow.
    artifacts = report["artifacts"]
    assert artifacts["linked"] == 3  # child sheets 1A/1Z/1B
    assert [entry["child"] for entry in artifacts["needs_reconciliation"]] == [
        "c-pending"
    ]
    assert len(artifacts["orphan"]) == 1
    assert artifacts["orphan"][0]["path"].endswith("AT2026_9Z.pdf")

    # Match runs oldest → latest, original sheet names recorded. The
    # first generation archived before any report existed, so its
    # triple legitimately carries no match_report sheet.
    assert report["match"]["runs"] == 3
    assert report["match"]["current_report_present"] is True
    assert report["match"]["archived_from"] == [
        "final_family_20261101_090000",
        "final_sponsor_20261101_090000",
        "final_family_20261102_090000",
        "final_sponsor_20261102_090000",
        "match_report_20261102_090000",
    ]

    # Clarify: sidecar naive-local → UTC (Detroit, D5), originals kept.
    status = report["clarify"]["contact_status"][0]
    assert status["family_id"] == 1
    assert status["contacted_at_original"] == "2026-12-01T10:30:00"
    assert status["contacted_at_utc"] == "2026-12-01T15:30:00+00:00"
    assert report["clarify"]["drafts"] == 1
    assert report["clarify"]["html_only"] == 1


def test_full_import_database_contents(tmp_path: Path) -> None:
    """Run-owned state lands in the right tables with the right counts."""
    cfg = _import_cfg(tmp_path)
    source = _build_source(cfg)
    db_import(cfg, str(source))

    async def main() -> tuple[
        int, list[list[str]], int, int, int, int, int, int
    ]:
        from ccat.db.models import (
            AcquisitionRun,
            Artifact,
            EmailCheck,
            EmailCheckTarget,
            MatchRun,
            ScreeningRun,
        )

        engine = create_async_engine(cfg.storage.database_url)
        uow = UnitOfWork(engine)
        try:
            async with uow.session() as session:
                season = (
                    await session.scalars(
                        select(Season).where(Season.name == cfg.season)
                    )
                ).one()
                repo = SeasonStoreRepository(session, season)
                runs = (
                    await session.scalars(
                        select(MatchRun)
                        .where(MatchRun.season_id == season.id)
                        .order_by(MatchRun.id)
                    )
                ).all()
                stamps = []
                for run in runs:
                    archived = run.archived_from
                    assert archived is not None
                    stamps.append(sorted(archived["sheets"].values()))
                raw = (
                    await session.scalars(
                        select(AcquisitionRun).where(
                            AcquisitionRun.season_id == season.id,
                            AcquisitionRun.active.is_(True),
                        )
                    )
                ).all()
                screening = (await session.scalars(select(ScreeningRun))).all()
                emails = (await session.scalars(select(EmailCheck))).all()
                targets = (
                    await session.scalars(select(EmailCheckTarget))
                ).all()
                draft_artifacts = (
                    await session.scalars(
                        select(Artifact).where(Artifact.kind == "clarify_draft")
                    )
                ).all()
                assert await repo.match_report_rows() == [
                    {"kind": "assigned", "ref": "1A", "reason": "current"},
                    {"kind": "split", "ref": "1", "reason": "overfull"},
                ]
                duplicates = await repo.duplicate_log_rows()
                assert duplicates[0]["sheet"] == "family_submissions"
                addresses = await repo.address_log_rows()
                drafts = await repo.latest_draft(1)
                assert drafts is not None
                assert drafts["legacy_positional"] is True
                paragraphs = drafts["paragraphs"]
                assert [p["zite_record_id"] for p in paragraphs] == [
                    "c-old-original",
                    "c-pending",
                    "c-raw-missing-ignored",
                ]
                assert paragraphs[0]["text"] == "About 1A."
                assert paragraphs[1]["text"] == "About 1B."
                assert paragraphs[2]["text"] is None
                assert all(
                    p["source_extraction_version"] == 1 for p in paragraphs
                )
                legacy = await repo.latest_draft(8)
                assert legacy is not None
                assert legacy["legacy_positional"] is True
                assert legacy["paragraphs"] == [
                    {"needs_recovery": True, "order": 0}
                ]
                contact = await repo.clarification_families()
                assert contact[0]["contacted_at"] == (
                    "2026-12-01T15:30:00+00:00"
                )
                return (
                    len(runs),
                    stamps,
                    len(raw),
                    len(screening),
                    len(emails),
                    len(targets),
                    len(addresses),
                    len(draft_artifacts),
                )
        finally:
            await engine.dispose()

    results = asyncio.run(main())
    (runs, stamps, raw, screening, emails, targets, addresses, drafts) = results
    assert runs == 3
    assert stamps == [
        [
            "final_family_20261101_090000",
            "final_sponsor_20261101_090000",
        ],
        [
            "final_family_20261102_090000",
            "final_sponsor_20261102_090000",
            "match_report_20261102_090000",
        ],
        ["match_report"],
    ]  # oldest → latest, original sheet names preserved
    assert raw == 3  # one active acquisition run per raw table
    assert screening == 1  # diagnostic snapshot imported as one run
    assert emails == 2  # replay by normalized email: 3 rows → 2 checks
    assert targets == 2  # skipped replays drop their target rows too
    assert addresses == 2  # one representative row per distinct query
    assert drafts == 2  # family_1.html + family_8.html


def test_age_discrepancy_reported_not_fixed(tmp_path: Path) -> None:
    """A stale cached age is reported; the stored value is kept."""
    cfg = _import_cfg(tmp_path)
    source = _build_source(cfg)
    # Only a direct openpyxl edit can produce a stale cache;
    # write_sheets always recomputes. (The re-save also drops the other
    # formula's cache — both children are then reported.)
    workbook = load_workbook(source)
    worksheet = workbook["final_child"]
    header = [cell.value for cell in worksheet[1]]
    for row_no in range(2, worksheet.max_row + 1):
        record = worksheet.cell(
            row=row_no, column=header.index("zite_record_id") + 1
        ).value
        if record == "c-old-original":
            worksheet.cell(
                row=row_no, column=header.index("age_at_xmas") + 1
            ).value = 99
    workbook.save(source)

    report_path = db_import(cfg, str(source))
    report = _report_of(report_path)
    discrepancies = {
        entry["zite_record_id"]: entry for entry in report["age_discrepancies"]
    }
    assert discrepancies["c-old-original"] == {
        "zite_record_id": "c-old-original",
        "stored": 99,
        "computed": 6,
    }
    assert set(discrepancies) == {"c-old-original", "c-pending"}
    rows = {row["zite_record_id"]: row for row in _read_db_rows(cfg, "child")}
    assert rows["c-old-original"]["age_at_xmas"] == 99  # migrate is trusted


# ---------------------------------------------------------------------------
# Batch reuse and changed sources.
# ---------------------------------------------------------------------------


def test_rerun_same_source_reuses_completed_batch(tmp_path: Path) -> None:
    """A second run of the same hash exits cleanly without new rows."""
    cfg = _import_cfg(tmp_path)
    source = _build_source(cfg)
    first = db_import(cfg, str(source))
    first_stat = first.stat()

    second = db_import(cfg, str(source))
    assert second == first
    assert second.stat().st_mtime_ns == first_stat.st_mtime_ns  # untouched

    rows = _db_query(cfg, "SELECT id, status FROM import_batches")
    assert len(rows) == 1
    assert rows[0][1] == "completed"
    children = _read_db_rows(cfg, "child")
    assert len(children) == 4  # no duplicate entity rows


def test_changed_source_requires_new_batch(tmp_path: Path) -> None:
    """A changed hash stages a new batch; finals replace, never merge."""
    cfg = _import_cfg(tmp_path)
    source = _build_source(cfg)
    db_import(cfg, str(source))

    # Touch one final cell through the house writer.
    rows = _sheet_dicts(source, "final_child")
    for row in rows:
        if row["zite_record_id"] == "c-old-original":
            row["need_1"] = "RECHECKED COAT"
    write_sheets(
        source,
        "migrate",
        {"final_child": build_final_frame("child", rows)},
        xmas=cfg.xmas,
    )

    report_path = db_import(
        cfg, str(source), force=True, backup_path=str(tmp_path / "b.sqlite3")
    )
    report = _report_of(report_path)
    assert report["batch"]["status"] == "completed"
    batches = _db_query(cfg, "SELECT status FROM import_batches")
    assert len(batches) == 2
    assert all(status == "completed" for (status,) in batches)
    edited = {row["zite_record_id"]: row for row in _read_db_rows(cfg, "child")}
    assert edited["c-old-original"]["need_1"] == "RECHECKED COAT"
    assert len(edited) == 4  # complete store, no merge duplication


# ---------------------------------------------------------------------------
# Merging into a database that already holds a later acquisition.
# ---------------------------------------------------------------------------


def test_import_merges_later_acquisition_rows(tmp_path: Path) -> None:
    """Rows the workbook lacks survive; the workbook wins where it speaks.

    The database holds a row the (older) workbook does not know about,
    and that row already reserved an angel id the workbook assigns to
    another child.  The import must keep the row, release the angel id
    it holds, and still store the workbook's audited values.
    """
    cfg = _import_cfg(tmp_path)
    source = _build_source(cfg)
    run_migrations(cfg.storage.database_url)

    # A row a later acquisition added, holding the angel id the
    # workbook assigns to c-old-original.
    async def seed() -> None:
        engine = create_async_engine(cfg.storage.database_url)
        uow = UnitOfWork(engine)
        try:
            async with uow.session() as session:
                season = await ensure_season_row(session, cfg)
                repo = SeasonStoreRepository(session, season)
                late = {
                    "zite_record_id": "c-late-arrival",
                    "family_id": 1,
                    "angel_id": "1A",
                    "first_name": "Late",
                    "status": "new",
                    "screened": False,
                    "generated_info_sheet": False,
                    "audited_at": None,
                }
                await repo.commit_finals("screen", "child", [late], "tester")
        finally:
            await engine.dispose()

    asyncio.run(seed())

    report_path = db_import(
        cfg, str(source), force=True, backup_path=str(tmp_path / "b.sqlite3")
    )
    report = _report_of(report_path)
    assert report["batch"]["status"] == "completed"
    assert report["errors"] == []
    assert all(check["pass"] for check in report["validation"]), report[
        "validation"
    ]
    assert report["merged_rows"]["child"] == 1

    children = {
        row["zite_record_id"]: row for row in _read_db_rows(cfg, "child")
    }
    assert len(children) == 5  # the workbook's four plus the stored one
    # The workbook's audited letter wins; the stored row releases it.
    assert children["c-old-original"]["angel_id"] == "1A"
    assert children["c-late-arrival"]["angel_id"] is None
    assert children["c-late-arrival"]["family_id"] == 1
    assert report["angel_ids_released"] == [
        {"zite_record_id": "c-late-arrival", "angel_id": "1A"}
    ]


# ---------------------------------------------------------------------------
# Refusals.
# ---------------------------------------------------------------------------


def test_refusals(tmp_path: Path) -> None:
    """Missing source, aliases, nonempty targets, and bad backups fail."""
    cfg = _import_cfg(tmp_path)
    with pytest.raises(DbImportError, match="source workbook not found"):
        db_import(cfg, str(tmp_path / "nope.xlsx"))

    source = _build_source(cfg)
    run_migrations(cfg.storage.database_url)
    db_file = tmp_path / "ccat.sqlite3"
    with pytest.raises(DbImportError, match="resolves to the database file"):
        db_import(cfg, str(db_file))

    db_import(cfg, str(source))  # first import completes
    rows = _sheet_dicts(source, "final_child")
    for row in rows:
        if row["zite_record_id"] == "c-old-original":
            row["need_1"] = "CHANGED"
    write_sheets(
        source,
        "migrate",
        {"final_child": build_final_frame("child", rows)},
        xmas=cfg.xmas,
    )
    with pytest.raises(DbImportError, match="already holds data"):
        db_import(cfg, str(source))
    with pytest.raises(DbImportError, match="--force requires --backup"):
        db_import(cfg, str(source), force=True)
    backup_dir = tmp_path / "backup-dir"
    backup_dir.mkdir()
    with pytest.raises(DbImportError, match="could not snapshot"):
        db_import(cfg, str(source), force=True, backup_path=str(backup_dir))


def test_failed_import_is_atomic(tmp_path: Path) -> None:
    """A corrupted source fails with a failed batch and an empty DB."""
    cfg = _import_cfg(tmp_path)
    source = _build_source(cfg)
    # Inject a duplicate child key directly (write_sheets would refuse).
    workbook = load_workbook(source)
    worksheet = workbook["final_child"]
    header = [cell.value for cell in worksheet[1]]
    duplicate = [None] * len(header)
    duplicate[header.index("zite_record_id")] = "c-old-original"
    worksheet.append(duplicate)
    workbook.save(source)

    with pytest.raises(DbImportError, match="duplicate zite_record_id"):
        db_import(cfg, str(source))
    rows = _db_query(cfg, "SELECT status FROM import_batches")
    assert [(status,) for (status,) in rows] == [("failed",)]
    assert _db_query(cfg, "SELECT count(*) FROM final_child") == [(0,)]
    report = _report_of(Path(f"{tmp_path / 'ccat.sqlite3'}.import.json"))
    assert report["batch"]["status"] == "failed"
    assert report["errors"]


# ---------------------------------------------------------------------------
# workbook-export (D6).
# ---------------------------------------------------------------------------


def test_workbook_export_round_trip(tmp_path: Path) -> None:
    """Export parity, archive exclusion, and reimport finals equality."""
    cfg = _import_cfg(tmp_path)
    source = _build_source(cfg)
    db_import(cfg, str(source))

    output = tmp_path / "export.xlsx"
    workbook_export_run(cfg, str(output))

    names = set(list_sheets(output))
    assert "final_child" in names and "match_report" in names
    assert not any(
        name.startswith(("final_family_2", "final_sponsor_2"))
        or name.startswith("match_report_2")
        for name in names
    ), "archives are not exported as sheets"
    child_sheet = read_sheet(output, "final_child")
    assert child_sheet is not None
    assert "potential_anomoly" in child_sheet.columns  # reverse mapping
    assert "ai_screening_notes" not in child_sheet.columns
    sponsor_sheet = read_sheet(output, "final_sponsor")
    assert sponsor_sheet is not None
    assert "create" in sponsor_sheet.columns
    assert "created" not in sponsor_sheet.columns

    # Reimport the export into a second database; finals must be equal.
    cfg2 = cfg.model_copy(
        update={"storage": _storage(tmp_path, "second.sqlite3")}
    )
    db_import(cfg2, str(output))
    for entity in ("child", "family", "sponsor"):
        assert _read_db_rows(cfg2, entity) == _read_db_rows(cfg, entity)


def test_workbook_export_refuses_existing_output(tmp_path: Path) -> None:
    """An existing export is refused without --force."""
    cfg = _import_cfg(tmp_path)
    source = _build_source(cfg)
    db_import(cfg, str(source))
    output = tmp_path / "export.xlsx"
    workbook_export_run(cfg, str(output))
    with pytest.raises(ExportError, match="without --force"):
        workbook_export_run(cfg, str(output))
    workbook_export_run(cfg, str(output), force=True)


# ---------------------------------------------------------------------------
# CLI dispatch and the second alembic revision.
# ---------------------------------------------------------------------------


def test_cli_db_import_and_export_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """New commands dispatch with dual --from naming and reject flags."""
    captured: dict[str, Any] = {}

    def dispatch(name: str, runner: str, config: str, **kwargs: Any) -> None:
        captured.update(name=name, runner=runner, config=config, **kwargs)

    monkeypatch.setattr(ccat, "_dispatch", dispatch)
    ccat._db_import_cmd(
        config="season.json",
        **{
            "from": "wb.xlsx",
            "db": "sqlite:///t.sqlite3",
            "force": True,
            "backup": "b.sqlite3",
            "report": "r.json",
        },
    )
    assert captured == {
        "name": "db-import",
        "runner": "db.migration_io:run",
        "config": "season.json",
        "from_path": "wb.xlsx",
        "db_url": "sqlite:///t.sqlite3",
        "force": True,
        "backup_path": "b.sqlite3",
        "report_path": "r.json",
    }
    captured.clear()
    ccat._db_import_cmd(config="season.json", from_path="wb.xlsx")
    assert captured == {
        "name": "db-import",
        "runner": "db.migration_io:run",
        "config": "season.json",
        "from_path": "wb.xlsx",
        "db_url": None,
        "force": False,
        "backup_path": None,
        "report_path": None,
    }
    captured.clear()
    ccat._workbook_export_cmd(config="season.json", output="out.xlsx")
    assert captured == {
        "name": "workbook-export",
        "runner": "db.migration_io:workbook_export_run",
        "config": "season.json",
        "output_path": "out.xlsx",
        "db_url": None,
        "report_path": None,
        "force": False,
    }
    with pytest.raises(SystemExit, match="unknown flags"):
        ccat._db_import_cmd(
            **{
                "from": "wb.xlsx",
                "dry_run": True,
            }
        )
    with pytest.raises(SystemExit, match="unknown flags"):
        ccat._workbook_export_cmd(output="o.xlsx", extra=1)
    with pytest.raises(SystemExit, match="requires --from"):
        ccat._db_import_cmd()
    with pytest.raises(SystemExit, match="requires --output"):
        ccat._workbook_export_cmd()


def test_import_batches_migration_round_trip(tmp_path: Path) -> None:
    """Revision 0002 upgrades and downgrades cleanly."""
    url = f"sqlite:///{tmp_path / 'ccat.sqlite3'}"
    run_migrations(url)
    assert "import_batches" in set(
        inspect(create_engine(url)).get_table_names()
    )
    alembic = AlembicConfig()
    alembic.set_main_option("script_location", str(ALEMBIC_DIR))
    alembic.set_main_option("sqlalchemy.url", sync_url_from(url))
    command.downgrade(alembic, "20261118_0001")
    assert "import_batches" not in set(
        inspect(create_engine(url)).get_table_names()
    )
    run_migrations(url)
    assert "import_batches" in set(
        inspect(create_engine(url)).get_table_names()
    )
