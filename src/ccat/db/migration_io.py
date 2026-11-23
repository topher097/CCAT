"""Workbook ⇄ SQLite migration I/O (plan §10, decisions D5/D6/D12).

``ccat db-import`` imports one persistent-final (v2) workbook into a
migrated SQLite database as a single controlled migration batch:

- the source is detected **explicitly** (required final sheets must be
  present — legacy v1/v2 sources are refused with guidance to run
  ``ccat audit-migrate`` / ``ccat consolidate-migrate`` first; never
  auto-selected by filename);
- guards refuse missing/aliasing sources and a nonempty target unless
  ``--force`` pairs with a verified restorable SQLite backup;
- the source hash is taken before and after; a mismatch fails the
  import and restores the pre-import database;
- finals, the raw snapshot, the four diagnostic/validation logs, match
  archives + current report, clarification sidecar state, drafts, and
  generated artifacts are imported through the season-store
  repositories as module ``migrate`` (actor ``migration``);
- the import **merges**: workbook rows win for every key they carry,
  and rows the database holds from a later acquisition survive (the
  final stores are append-and-merge, never projections);
- a validation gate compares the stored database against the workbook;
  any failure restores the pre-import database, records a ``failed``
  batch row, and writes the report;
- a completed batch for the same source SHA-256 is reused on re-run
  instead of duplicating rows.

``ccat workbook-export`` is the read-only reverse export (D6): a fresh
workbook from the database for rollback evidence — never a second
active truth.  Match archives stay queryable in the database and are
deliberately not recreated as timestamped sheets.

Reports carry counts, hashes, and IDs only — never names or emails.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from itertools import zip_longest
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import polars as pl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ccat.acquire import DOB_CONFLICT_LOG_SCHEMA
from ccat.audit.store import (
    ENTITY_SPECS,
    FAMILY_ASSIGNMENT_COLUMNS,
    SPONSOR_ASSIGNMENT_COLUMNS,
    as_bool,
    build_final_frame,
    normalize_row,
)
from ccat.config import Config
from ccat.db.engine import create_async_engine, run_migrations
from ccat.db.models import (
    MatchAssignmentSnapshot,
    MatchReportRow,
    MatchRun,
    Season,
)
from ccat.db.repositories import (
    SeasonStoreRepository,
    _config_digest,
    ensure_season,
    ensure_season_row,
)
from ccat.db.uow import UnitOfWork
from ccat.io import list_sheets, read_sheet, write_sheets
from ccat.match import canonical_location
from ccat.services.db_assignments import backfill_child_assignments
from ccat.screening import (
    ADDRESS_VALIDATION_LOG_SCHEMA,
    DUPLICATE_LOG_SCHEMA,
    EMAIL_VALIDATION_LOG_SCHEMA,
)

logger = logging.getLogger(__name__)


class ImportError(Exception):
    """Raised when a workbook import cannot be completed or verified."""


class ExportError(Exception):
    """Raised when a workbook export cannot be completed."""


#: Legacy naive-local timestamps in the clarify sidecar are assumed to
#: carry this timezone (decision D5); originals stay in the report.
SIDECAR_TIMEZONE = ZoneInfo("America/Detroit")

#: Final sheets that must all be present for an explicit v2 detection.
_REQUIRED_FINAL_SHEETS = frozenset(
    {"final_child", "final_family", "final_sponsor"}
)

#: Sheets owned by the persistent-final contract; anything else that is
#: not a timestamped archive is reported, never silently dropped.
_KNOWN_SHEETS = frozenset(
    {
        *_REQUIRED_FINAL_SHEETS,
        "raw_sponsor",
        "raw_family",
        "raw_child",
        "duplicate_log",
        "dob_conflict_log",
        "email_validation_log",
        "address_validation_log",
        "match_report",
    }
)

#: Timestamped archive sheet names written by ``ccat match`` (§8.6):
#: ``<sheet>_<YYYYMMDD>_<HHMMSS>`` plus optional ``_N`` collision
#: suffixes assigned by ``match._archive_name``.
_ARCHIVE_RE = re.compile(
    r"^(final_family|final_sponsor|match_report)_(\d{8}_\d{6})"
    r"(?:_(\d+))?$"
)

#: Assignment columns snapshotted per archived entity (§4.1).
_ASSIGNMENT_COLUMNS: dict[str, tuple[str, ...]] = {
    "family": FAMILY_ASSIGNMENT_COLUMNS,
    "sponsor": SPONSOR_ASSIGNMENT_COLUMNS,
}


class _StagedImportError(Exception):
    """Internal control-flow error carrying the partial report."""

    def __init__(self, report: dict[str, Any], message: str) -> None:
        """Capture the report so the failure path can persist it."""
        super().__init__(message)
        self.report = report
        self.message = message


# ---------------------------------------------------------------------------
# Small file/URL helpers.
# ---------------------------------------------------------------------------


def _file_sha256(path: Path) -> str:
    """Hash one file's bytes without modifying it."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _same_path(left: Path, right: Path) -> bool:
    """Compare paths after resolution, including not-yet-created files."""
    return left.resolve() == right.resolve()


def _aliases_database(source: Path, db_path: Path) -> bool:
    """Whether the source is the DB file or one of its WAL/SHM siblings."""
    return any(
        _same_path(source, db_path.with_name(db_path.name + suffix))
        for suffix in ("", "-wal", "-shm")
    )


def _db_path_from_url(url: str) -> Path:
    """Extract the SQLite file path from an async or sync URL.

    Raises:
        ImportError: For a non-SQLite or in-memory URL — the importer
            and exporter need a real file to back up and hash.
    """
    for prefix in ("sqlite+aiosqlite://", "sqlite://"):
        if url.startswith(prefix):
            rest = url[len(prefix) :]
            if rest.startswith("/"):
                rest = rest[1:]
            if rest and rest != ":memory:":
                return Path(rest)
            break
    raise ImportError(
        f"unsupported database URL {url!r}: a file-backed SQLite URL"
        " (sqlite+aiosqlite:///PATH) is required"
    )


def _sqlite_snapshot(source: Path, dest: Path) -> None:
    """Copy one SQLite database to ``dest`` via the online backup API.

    The sqlite3 backup mechanism reads a consistent snapshot (WAL-safe)
    rather than a naive file copy of a live database (plan §10).

    Raises:
        ImportError: When either end cannot be opened (e.g. the backup
            path names a directory or an unreadable file).
    """
    try:
        src = sqlite3.connect(source)
        try:
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except sqlite3.Error as error:
        raise ImportError(
            f"could not snapshot {source} to {dest}: {error}"
        ) from error


def _verify_sqlite_file(path: Path, label: str) -> None:
    """Refuse anything that is not an openable, intact SQLite file."""
    if not path.is_file():
        raise ImportError(f"{label} is not a file: {path}")
    try:
        conn = sqlite3.connect(path)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as error:
        raise ImportError(
            f"{label} is not an openable SQLite database ({path}): {error}"
        ) from error
    if not result or result[0] != "ok":
        raise ImportError(
            f"{label} failed PRAGMA integrity_check:"
            f" {result[0] if result else 'no result'}"
        )


def _db_is_nonempty(db_path: Path) -> bool:
    """Whether any CCAT data table holds a row (bookkeeping excluded)."""
    conn = sqlite3.connect(db_path)
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        ]
        for name in tables:
            if name in {"alembic_version", "import_batches"} or name.startswith(
                "sqlite_"
            ):
                continue
            row = conn.execute(f'SELECT 1 FROM "{name}" LIMIT 1').fetchone()
            if row:
                return True
    finally:
        conn.close()
    return False


def _alembic_head(db_path: Path) -> str | None:
    """Read the applied alembic version, or None."""
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT version_num FROM alembic_version LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return str(row[0]) if row else None


def _ccat_version() -> str:
    """Return the installed ccat package version (or 'unknown')."""
    from importlib import metadata

    try:
        return metadata.version("ccat")
    except Exception:  # pragma: no cover - package not installed
        return "unknown"


# ---------------------------------------------------------------------------
# Batch rows (import_batches — migration bookkeeping, raw SQL only).
# ---------------------------------------------------------------------------


def _batch_insert(
    db_path: Path,
    season_id: int,
    sha256: str,
    source: Path,
    config_hash: str,
    status: str,
    report: dict[str, Any] | None,
) -> int:
    """Insert one batch row (``staged`` or ``failed``); returns its id."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO import_batches (season_id, source_sha256,"
            " source_path, config_hash, status, report, actor)"
            " VALUES (?, ?, ?, ?, ?, ?, 'migration')",
            (
                season_id,
                sha256,
                str(source),
                config_hash,
                status,
                None if report is None else json.dumps(report),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT max(id) FROM import_batches").fetchone()
    finally:
        conn.close()
    return int(row[0]) if row and row[0] is not None else 0


def _batch_complete(
    db_path: Path, batch_id: int, report: dict[str, Any]
) -> None:
    """Mark one batch row completed with its report JSON."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE import_batches SET status = 'completed', report = ?"
            " WHERE id = ?",
            (json.dumps(report), batch_id),
        )
        conn.commit()
    finally:
        conn.close()


def _find_completed_batch(
    db_path: Path, sha256: str
) -> tuple[int, int, dict[str, Any] | None] | None:
    """Find a completed batch for this exact source hash.

    Returns:
        (batch_id, season_id, report JSON) for the latest completed
        batch carrying the hash, or None.
    """
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT id, season_id, report FROM import_batches"
            " WHERE source_sha256 = ? AND status = 'completed'"
            " ORDER BY id DESC LIMIT 1",
            (sha256,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    return int(row[0]), int(row[1]), json.loads(row[2]) if row[2] else None


# ---------------------------------------------------------------------------
# Source reading (read-only; everything parses before any DB write).
# ---------------------------------------------------------------------------


def _frame_rows(source: Path, sheet: str) -> list[dict[str, Any]]:
    """Read one optional sheet as plain row dicts (absent → [])."""
    frame = read_sheet(source, sheet)
    return frame.to_dicts() if frame is not None else []


def _content_sha(rows: list[dict[str, Any]]) -> str:
    """Hash one sheet's row content for archive deduplication."""
    canonical = json.dumps(
        [{key: row.get(key) for key in sorted(row)} for row in rows],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_archive_groups(
    source: Path, names: list[str]
) -> list[dict[str, Any]]:
    """Group and order timestamped archive triples oldest-first.

    Returns:
        One dict per (timestamp, collision suffix) group with its
        ``created_at`` (UTC), sheet-name map, and parsed rows.
    """
    groups: dict[tuple[str, str], dict[str, str]] = {}
    for name in names:
        match = _ARCHIVE_RE.match(name)
        if match is None:
            continue
        key = (match.group(2), match.group(3) or "")
        groups.setdefault(key, {})[match.group(1)] = name
    ordered: list[dict[str, Any]] = []
    for (stamp, _suffix), sheets in sorted(
        groups.items(),
        key=lambda item: (
            datetime.strptime(item[0][0], "%Y%m%d_%H%M%S"),
            int(item[0][1] or 0),
        ),
    ):
        created = datetime.strptime(stamp, "%Y%m%d_%H%M%S").replace(tzinfo=UTC)
        rows = {
            sheet: _frame_rows(source, name) for sheet, name in sheets.items()
        }
        ordered.append(
            {
                "created_at": created,
                "sheets": dict(sorted(sheets.items())),
                "rows": rows,
            }
        )
    return ordered


def _assignment_rows(
    entity: str, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Extract assignment-snapshot rows from final rows (any vintage)."""
    key = str(ENTITY_SPECS[entity]["key"])
    columns = _ASSIGNMENT_COLUMNS[entity]
    return [
        {
            "entity": entity,
            "key": str(normalized.get(key)),
            "assignment_values": {
                column: normalized.get(column) for column in columns
            },
        }
        for normalized in (
            normalize_row(entity, row)
            for row in rows
            if row.get(key) is not None
        )
    ]


def _read_source(cfg: Config, source: Path) -> dict[str, Any]:
    """Parse the whole source workbook (read-only) into import data.

    Finals are read through ``audit.store.read_final``, which validates
    the full persistent schema and entity keys before anything is
    written (reused, not reimplemented).

    Raises:
        ImportError: When the required final sheets are missing — the
            explicit legacy-source refusal (never auto-selected).
    """
    from ccat.audit.store import read_final

    names = list_sheets(source)
    missing = sorted(_REQUIRED_FINAL_SHEETS - set(names))
    if missing:
        raise ImportError(
            f"source workbook {source} is not a persistent-final (v2)"
            f" workbook: missing required final sheet(s) {missing}."
            " Convert legacy v1/v2 sources first with"
            " `ccat audit-migrate` or `ccat consolidate-migrate`;"
            " db-import never auto-selects by filename"
        )
    finals = {
        entity: read_final(cfg, entity, workbook=source)
        for entity in ("child", "family", "sponsor")
    }
    # A workbook written before the child-grain change carries the
    # placement on the family and sponsor rows only.  Rebuild the child
    # rows' own assignment from them, so an imported workbook keeps its
    # assignments instead of importing as a season with no placements.
    backfill_child_assignments(
        finals["family"], finals["sponsor"], finals["child"]
    )
    raw = {
        entity: _frame_rows(source, f"raw_{entity}")
        for entity in ("sponsor", "family", "child")
    }
    logs = {
        sheet: _frame_rows(source, sheet)
        for sheet in (
            "duplicate_log",
            "dob_conflict_log",
            "email_validation_log",
            "address_validation_log",
        )
    }
    return {
        "finals": finals,
        "raw": raw,
        "logs": logs,
        "archives": _parse_archive_groups(source, names),
        "current_report": _frame_rows(source, "match_report"),
        "current_report_present": "match_report" in names,
        "unrecognized_sheets": sorted(
            name
            for name in names
            if name not in _KNOWN_SHEETS and _ARCHIVE_RE.match(name) is None
        ),
    }


def _naive_local_to_utc(value: str) -> str:
    """Convert one sidecar timestamp to UTC ISO (assumes Detroit, D5)."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SIDECAR_TIMEZONE)
    return parsed.astimezone(UTC).isoformat()


def _read_sidecar(cfg: Config) -> dict[int, dict[str, Any]]:
    """Load the clarify sidecar (never required; absence is normal)."""
    from ccat.clarify.status import load_statuses

    try:
        statuses = load_statuses(cfg)
    except Exception as error:
        raise ImportError(
            f"clarify sidecar could not be read: {error}"
        ) from error
    converted: dict[int, dict[str, Any]] = {}
    for family_id, status in statuses.items():
        utc_at = None
        if status.contacted_at:
            try:
                utc_at = _naive_local_to_utc(status.contacted_at)
            except ValueError:
                utc_at = None
        converted[family_id] = {
            "contacted": status.contacted,
            "contacted_at": utc_at,
            "replied": status.replied,
            "contacted_at_original": status.contacted_at,
        }
    return converted


def _read_drafts(cfg: Config) -> dict[str, dict[int, Any]]:
    """Inventory clarify drafts: keyed JSON drafts and HTML-only files.

    Returns:
        ``{"json": {id: draft dict}, "html": {id: path},
        "html_only": {id: path}}`` keyed by the ``family_<id>`` stem.
    """
    from ccat.clarify.email_gen import EmailDraft

    root = Path(cfg.clarify.output_dir)
    json_drafts: dict[int, dict[str, Any]] = {}
    html_files: dict[int, Path] = {}
    for pattern in ("family_*.json", "family_*.html"):
        for path in root.glob(pattern):
            token = path.stem.removeprefix("family_")
            try:
                family_id = int(token)
            except ValueError as error:
                raise ImportError(
                    f"clarify draft {path.name} has no integer family id"
                ) from error
            if path.suffix == ".json":
                try:
                    draft = EmailDraft.model_validate_json(path.read_text())
                except ValueError as error:
                    raise ImportError(
                        f"clarify draft {path.name} is invalid: {error}"
                    ) from error
                json_drafts[family_id] = draft.model_dump()
            else:
                html_files[family_id] = path
    return {
        "json": json_drafts,
        "html": html_files,
        "html_only": {
            family_id: path
            for family_id, path in html_files.items()
            if family_id not in json_drafts
        },
    }


# ---------------------------------------------------------------------------
# Clarification draft import (decision D5).
# ---------------------------------------------------------------------------


def _family_children(
    family_id: int, children: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return the family's children in extract order (family_id, angel_id)."""
    members = [row for row in children if row.get("family_id") == family_id]
    return sorted(
        members,
        key=lambda row: (
            row.get("family_id") is not None,
            row.get("family_id") or 0,
            row.get("angel_id") is not None,
            str(row.get("angel_id") or ""),
        ),
    )


def _positional_paragraphs(
    draft: dict[str, Any], family_id: int, children: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Map legacy positional ``angel_paragraphs`` onto keyed paragraphs.

    Each paragraph keeps its positional order and gains the sorted
    child's identity plus a ``source_extraction_version`` marker; text
    is never dropped when counts disagree (the shorter side fills None).
    """
    members = _family_children(family_id, children)
    keyed: list[dict[str, Any]] = []
    for order, (child, text) in enumerate(
        zip_longest(members, draft.get("angel_paragraphs") or [])
    ):
        keyed.append(
            {
                "zite_record_id": child.get("zite_record_id")
                if child
                else None,
                "angel_id": child.get("angel_id") if child else None,
                "order": order,
                "text": text,
                "source_extraction_version": 1,
            }
        )
    return keyed


async def _import_clarify(
    repo: SeasonStoreRepository,
    cfg: Config,
    source: dict[str, Any],
    report: dict[str, Any],
) -> None:
    """Import sidecar contact state first, then drafts (D5)."""
    sidecar = _read_sidecar(cfg)
    for family_id in sorted(sidecar):
        entry = sidecar[family_id]
        await repo.set_contact_status(
            family_id,
            contacted=entry["contacted"],
            contacted_at=entry["contacted_at"],
            replied=entry["replied"],
            actor="migration",
        )
    report["clarify"]["contact_status"] = [
        {
            "family_id": family_id,
            "contacted_at_original": sidecar[family_id][
                "contacted_at_original"
            ],
            "contacted_at_utc": sidecar[family_id]["contacted_at"],
        }
        for family_id in sorted(sidecar)
    ]
    report["clarify"]["families"] = len(sidecar)

    drafts = _read_drafts(cfg)
    children = [
        normalize_row("child", row) for row in source["finals"]["child"]
    ]
    draft_count = 0
    for family_id in sorted(drafts["json"]):
        draft = drafts["json"][family_id]
        paragraphs = _positional_paragraphs(draft, family_id, children)
        existing = await repo.latest_draft(family_id)
        if existing is not None and existing.get("paragraphs") == paragraphs:
            continue  # re-import of an unchanged draft keeps one version
        await repo.save_draft(
            family_id,
            draft.get("subject"),
            draft.get("greeting"),
            draft.get("intro"),
            draft.get("closing"),
            paragraphs,
            actor="migration",
            generated_by="migration",
            legacy_positional=True,
        )
        draft_count += 1
    html_only = 0
    for family_id, path in sorted(
        drafts["html"].items(), key=lambda item: item[0]
    ):
        is_html_only = family_id in drafts["html_only"]
        if is_html_only:
            # Legacy noneditable state: never dropped, never regenerated.
            await repo.save_draft(
                family_id,
                None,
                None,
                None,
                None,
                [{"needs_recovery": True, "order": 0}],
                actor="migration",
                legacy_positional=True,
            )
            html_only += 1
        await repo.register_artifact(
            kind="clarify_draft",
            path=str(path),
            filename=path.name,
            content_type="text/html",
            size_bytes=path.stat().st_size,
            sha256=_file_sha256(path),
            actor="migration",
            meta=(
                {"needs_recovery": True}
                if is_html_only
                else {"family_id": family_id}
            ),
        )
    report["clarify"]["drafts"] = draft_count
    report["clarify"]["html_only"] = html_only


# ---------------------------------------------------------------------------
# Match-run import.
# ---------------------------------------------------------------------------


def _report_dicts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize one match-report sheet's rows to kind/ref/reason."""
    return [
        {
            "kind": row.get("kind"),
            "ref": row.get("ref"),
            "reason": row.get("reason"),
        }
        for row in rows
    ]


async def _import_match_runs(
    session: AsyncSession,
    season_id: int,
    source: dict[str, Any],
    report: dict[str, Any],
) -> None:
    """Import archive triples oldest-first, then the current report.

    Archived final sheets provide each run's assignment snapshots; the
    original sheet names are recorded in ``archived_from``.  The current
    unsuffixed match_report becomes the latest run (a legitimately
    absent report stays absent — no fabricated empty run).  Identical
    runs already stored (same ``archived_from`` content) are skipped so
    a changed-source re-import never duplicates history.
    """
    existing = (
        await session.scalars(
            select(MatchRun).where(MatchRun.season_id == season_id)
        )
    ).all()
    seen = {
        json.dumps(run.archived_from, sort_keys=True, default=str)
        for run in existing
    }

    async def add_run(
        archived_from: dict[str, Any],
        created_at: datetime,
        report_rows: list[dict[str, Any]],
        snapshots: list[dict[str, Any]],
    ) -> bool:
        token = json.dumps(archived_from, sort_keys=True, default=str)
        if token in seen:
            return False
        seen.add(token)
        run = MatchRun(
            season_id=season_id,
            created_at=created_at,
            actor="migration",
            archived_from=archived_from,
        )
        session.add(run)
        await session.flush()  # assign run.id before owned rows reference it
        session.add_all(
            MatchReportRow(
                run_id=run.id,
                kind=row.get("kind"),
                ref=row.get("ref"),
                reason=row.get("reason"),
            )
            for row in report_rows
        )
        session.add_all(
            MatchAssignmentSnapshot(
                run_id=run.id,
                entity=snapshot["entity"],
                key=snapshot["key"],
                assignment_values=snapshot["assignment_values"],
            )
            for snapshot in snapshots
        )
        return True

    runs = 0
    archived_names: list[str] = []
    for group in source["archives"]:
        rows = group["rows"]
        archived_from = {
            "sheets": group["sheets"],
            "content_sha256": _content_sha(
                [row for sheet in sorted(rows) for row in rows[sheet]]
            ),
        }
        snapshots = [
            *_assignment_rows("family", rows.get("final_family", [])),
            *_assignment_rows("sponsor", rows.get("final_sponsor", [])),
        ]
        if await add_run(
            archived_from,
            group["created_at"],
            _report_dicts(rows.get("match_report", [])),
            snapshots,
        ):
            runs += 1
            archived_names.extend(sorted(group["sheets"].values()))

    if source["current_report_present"]:
        archived_from = {
            "sheets": {"match_report": "match_report"},
            "content_sha256": _content_sha(source["current_report"]),
        }
        snapshots = [
            *_assignment_rows(
                "family",
                [
                    normalize_row("family", row)
                    for row in source["finals"]["family"]
                ],
            ),
            *_assignment_rows(
                "sponsor",
                [
                    normalize_row("sponsor", row)
                    for row in source["finals"]["sponsor"]
                ],
            ),
        ]
        if await add_run(
            archived_from,
            datetime.now(UTC),
            _report_dicts(source["current_report"]),
            snapshots,
        ):
            runs += 1
    await session.commit()
    report["match"] = {
        "runs": runs,
        "current_report_present": source["current_report_present"],
        "archived_from": archived_names,
        "unrecognized_sheets": source["unrecognized_sheets"],
    }


# ---------------------------------------------------------------------------
# Artifact inventory.
# ---------------------------------------------------------------------------


def _guess_content_type(path: Path) -> str:
    """Guess the MIME type for artifact registration."""
    return {
        ".pdf": "application/pdf",
        ".html": "text/html",
        ".json": "application/json",
        ".csv": "text/csv",
    }.get(path.suffix.lower(), "application/octet-stream")


async def _inventory_artifacts(
    repo: SeasonStoreRepository,
    cfg: Config,
    source: dict[str, Any],
    report: dict[str, Any],
) -> None:
    """Scan generated-output roots and register every expected file.

    Marker/file disagreements are reported (``needs_reconciliation``)
    and never resolved by deleting or trusting either side; PDFs with
    no matching child angel register as ``orphan`` artifacts.
    """
    children = [
        normalize_row("child", row) for row in source["finals"]["child"]
    ]
    families = {
        row.get("family_id"): row
        for row in (
            normalize_row("family", frow) for frow in source["finals"]["family"]
        )
    }
    sponsor_ids = {
        str(normalize_row("sponsor", row).get("sponsor_id"))
        for row in source["finals"]["sponsor"]
    }
    claimed: set[str] = set()
    linked: list[dict[str, Any]] = []
    orphans: list[dict[str, Any]] = []
    needs_reconciliation: list[dict[str, Any]] = []

    def stage(artifact: dict[str, Any], orphan: bool) -> None:
        path = str(artifact["path"])
        if path in claimed:
            return
        claimed.add(path)
        (orphans if orphan else linked).append(artifact)

    # Child info sheets at their canonical locations.
    for child in children:
        angel_id = child.get("angel_id")
        if angel_id is None:
            continue
        location = canonical_location(
            (families.get(child.get("family_id")) or {}).get(
                "gift_pickup_location"
            ),
            cfg.location_groups,
        )
        pdf = (
            Path(cfg.info_sheet.output_dir)
            / (location or "unknown")
            / f"AT{cfg.season}_{angel_id}.pdf"
        )
        if not pdf.is_file():
            continue
        if not as_bool(child.get("generated_info_sheet")):
            needs_reconciliation.append(
                {
                    "child": child.get("zite_record_id"),
                    "angel_id": angel_id,
                    "reason": "sheet_pdf_exists_marker_false",
                }
            )
        stage(
            {
                "kind": "child_sheet",
                "path": str(pdf),
                "filename": pdf.name,
                "content_type": "application/pdf",
                "meta": {
                    "angel_id": angel_id,
                    "child": child.get("zite_record_id"),
                },
            },
            orphan=False,
        )
    # Any other child-sheet PDF is an orphan (unknown angel/location).
    info_root = Path(cfg.info_sheet.output_dir)
    if info_root.is_dir():
        for pdf in info_root.rglob("AT*.pdf"):
            if str(pdf) in claimed:
                continue
            stage(
                {
                    "kind": "orphan",
                    "path": str(pdf),
                    "filename": pdf.name,
                    "content_type": "application/pdf",
                    "meta": {"reason": "no matching child angel"},
                },
                orphan=True,
            )

    # Sponsor packets: one <sponsor_id>/ directory per sponsor.
    packet_root = Path(cfg.sponsor_packets.output_dir)
    if packet_root.is_dir():
        for directory in sorted(packet_root.iterdir()):
            if not directory.is_dir():
                continue
            try:
                sponsor_id = int(directory.name)
            except ValueError:
                sponsor_id = None
            unknown_sponsor = (
                sponsor_id is not None and str(sponsor_id) not in sponsor_ids
            )
            for file in sorted(directory.rglob("*")):
                if not file.is_file():
                    continue
                stage(
                    {
                        "kind": (
                            "orphan" if unknown_sponsor else "sponsor_packet"
                        ),
                        "path": str(file),
                        "filename": file.name,
                        "content_type": _guess_content_type(file),
                        "meta": {
                            "sponsor_id": sponsor_id,
                            **(
                                {
                                    "reason": (
                                        "sponsor id not present in workbook"
                                        " finals"
                                    )
                                }
                                if unknown_sponsor
                                else {}
                            ),
                        },
                    },
                    orphan=unknown_sponsor,
                )

    # Stats/report outputs.
    report_root = Path(cfg.report.output_dir)
    if report_root.is_dir():
        for file in sorted(report_root.rglob("*")):
            if file.is_file():
                stage(
                    {
                        "kind": "stats_report",
                        "path": str(file),
                        "filename": file.name,
                        "content_type": _guess_content_type(file),
                        "meta": {},
                    },
                    orphan=False,
                )

    # Remaining clarify files (draft companions were handled above).
    clarify_root = Path(cfg.clarify.output_dir)
    if clarify_root.is_dir():
        for file in sorted(clarify_root.rglob("*")):
            if not file.is_file():
                continue
            if file.name.startswith("family_") or file.name == (
                "clarification_status.json"
            ):
                continue
            stage(
                {
                    "kind": "other",
                    "path": str(file),
                    "filename": file.name,
                    "content_type": _guess_content_type(file),
                    "meta": {},
                },
                orphan=False,
            )

    registered = {str(row["path"]) for row in await repo.artifact_rows()}
    for artifact in [*linked, *orphans]:
        if artifact["path"] in registered:
            continue
        file = Path(artifact["path"])
        await repo.register_artifact(
            kind=artifact["kind"],
            path=artifact["path"],
            filename=artifact["filename"],
            content_type=artifact["content_type"],
            size_bytes=file.stat().st_size,
            sha256=_file_sha256(file),
            actor="migration",
            meta=artifact["meta"],
        )
    report["artifacts"] = {
        "linked": len(linked),
        "orphan": [
            {"path": item["path"], "kind": item["kind"]} for item in orphans
        ],
        "needs_reconciliation": needs_reconciliation,
    }


# ---------------------------------------------------------------------------
# Validation gate.
# ---------------------------------------------------------------------------


def _plain(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip the repository's underscore metadata keys."""
    return [
        {key: value for key, value in row.items() if not key.startswith("_")}
        for row in rows
    ]


def _sorted_by_key(
    entity: str, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Normalize rows and order them by their entity key."""
    key = str(ENTITY_SPECS[entity]["key"])
    return sorted(
        (normalize_row(entity, row) for row in rows),
        key=lambda row: str(row.get(key)),
    )


def _entity_counts(
    entity: str,
    rows: list[dict[str, Any]],
    raw_record_ids: set[str],
) -> dict[str, int]:
    """Per-entity counts for the report and the flag-parity check."""
    return {
        "imported": len(rows),
        "ignored": sum(bool(as_bool(row.get("ignore"))) for row in rows),
        "orphan": (
            sum(row.get("family_id") is None for row in rows)
            if entity == "child"
            else 0
        ),
        "raw_missing": sum(
            str(row.get("zite_record_id") or "") not in raw_record_ids
            for row in rows
        ),
    }


async def _validate(
    repo: SeasonStoreRepository,
    source: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Run every parity check; returns the check list and merged counts.

    The comparison covers the workbook's rows: every one of them must
    be stored with identical values.  Rows the database holds from a
    later acquisition are counted, not compared — they are not the
    workbook's business.

    Returns:
        The check list for the report and the per-entity count of
        stored rows the workbook does not carry.
    """
    checks: list[dict[str, Any]] = []
    merged: dict[str, int] = {}

    def record(name: str, passed: bool, detail: str) -> None:
        checks.append({"check": name, "pass": passed, "detail": detail})

    raw_ids = {
        str(row.get("zite_record_id") or "")
        for row in source["raw"]["child"]
        if row.get("zite_record_id") is not None
    }
    db_raw_ids = {
        str(row.get("zite_record_id") or "")
        for row in await repo.raw_rows("child")
        if row.get("zite_record_id") is not None
    }

    for entity in ("child", "family", "sponsor"):
        key = str(ENTITY_SPECS[entity]["key"])
        wb_rows = _sorted_by_key(entity, source["finals"][entity])
        stored_rows = _sorted_by_key(
            entity, _plain(await repo.final_rows(entity))
        )
        wb_keys = [str(row.get(key)) for row in wb_rows]
        wb_key_set = set(wb_keys)
        db_rows = [
            row for row in stored_rows if str(row.get(key)) in wb_key_set
        ]
        db_keys = [str(row.get(key)) for row in db_rows]
        merged[entity] = len(stored_rows) - len(db_rows)
        record(
            f"{entity}_key_sets",
            set(wb_keys) == set(db_keys),
            f"{len(wb_keys)} workbook vs {len(db_keys)} db keys",
        )
        record(
            f"{entity}_row_counts",
            len(wb_keys) == len(db_keys),
            f"{len(wb_keys)} workbook vs {len(db_keys)} db rows",
        )
        record(
            f"{entity}_column_parity",
            wb_rows == db_rows,
            "normalized workbook rows equal db round-trip rows",
        )
        wb_status = Counter(str(row.get("status")) for row in wb_rows)
        db_status = Counter(str(row.get("status")) for row in db_rows)
        record(
            f"{entity}_status_distribution",
            wb_status == db_status,
            f"workbook {dict(sorted(wb_status.items()))} vs db"
            f" {dict(sorted(db_status.items()))}",
        )
        wb_flags = _entity_counts(entity, wb_rows, raw_ids)
        db_flags = _entity_counts(entity, db_rows, db_raw_ids)
        record(
            f"{entity}_ignored_orphan_raw_missing",
            wb_flags == db_flags,
            f"workbook {wb_flags} vs db {db_flags}",
        )
        if entity == "child":
            wb_angels = {
                str(row.get("angel_id"))
                for row in wb_rows
                if row.get("angel_id") is not None
            }
            db_angels = {
                str(row.get("angel_id"))
                for row in db_rows
                if row.get("angel_id") is not None
            }
            record(
                "angel_ids_reserved",
                wb_angels == db_angels,
                f"missing from db: {sorted(wb_angels - db_angels)},"
                f" extra in db: {sorted(db_angels - wb_angels)}",
            )
            wb_generated = sum(
                bool(as_bool(row.get("generated_info_sheet")))
                for row in wb_rows
            )
            db_generated = sum(
                bool(as_bool(row.get("generated_info_sheet")))
                for row in db_rows
            )
            record(
                "generated_info_sheet_count",
                wb_generated == db_generated,
                f"{wb_generated} workbook vs {db_generated} db",
            )
            # The child is the assignment grain; the family/sponsor
            # columns are derived from these rows.
            wb_child_assigned = sum(
                row.get("assigned_sponsor_id") is not None for row in wb_rows
            )
            db_child_assigned = sum(
                row.get("assigned_sponsor_id") is not None for row in db_rows
            )
            record(
                "child_assignment_count",
                wb_child_assigned == db_child_assigned,
                f"{wb_child_assigned} workbook vs {db_child_assigned} db",
            )
            wb_pending = sum(
                not bool(as_bool(row.get("screened"))) for row in wb_rows
            )
            db_pending = sum(
                not bool(as_bool(row.get("screened"))) for row in db_rows
            )
            record(
                "pending_ai_count",
                wb_pending == db_pending,
                f"{wb_pending} workbook vs {db_pending} db (screened false)",
            )
            # The DB column is ai_screening_notes; repository reads
            # render it under the legacy key at the boundary, so this
            # proves the workbook → DB → workbook mapping both ways.
            wb_notes = {
                str(row.get("zite_record_id")): row.get("potential_anomoly")
                for row in wb_rows
            }
            db_notes = {
                str(row.get("zite_record_id")): row.get("potential_anomoly")
                for row in db_rows
            }
            record(
                "ai_screening_notes_mapping",
                wb_notes == db_notes,
                "potential_anomoly values map to ai_screening_notes",
            )
        if entity == "family":
            # A family is "assigned" only when all of its placed children
            # share one sponsor; a split family has no single sponsor but
            # still lists its placed angel ids.  Both summaries derive
            # from the child rows.
            wb_assigned = sum(
                row.get("assigned_sponsor_id") is not None for row in wb_rows
            )
            db_assigned = sum(
                row.get("assigned_sponsor_id") is not None for row in db_rows
            )
            record(
                "family_assignment_count",
                wb_assigned == db_assigned,
                f"{wb_assigned} workbook vs {db_assigned} db",
            )
            wb_placed = sum(
                bool(row.get("assigned_angel_ids")) for row in wb_rows
            )
            db_placed = sum(
                bool(row.get("assigned_angel_ids")) for row in db_rows
            )
            record(
                "family_placed_count",
                wb_placed == db_placed,
                f"{wb_placed} workbook vs {db_placed} db",
            )
        if entity == "sponsor":
            wb_assigned = sum(
                bool(row.get("assigned_angel_ids")) for row in wb_rows
            )
            db_assigned = sum(
                bool(row.get("assigned_angel_ids")) for row in db_rows
            )
            record(
                "sponsor_assignment_count",
                wb_assigned == db_assigned,
                f"{wb_assigned} workbook vs {db_assigned} db",
            )
    return checks, merged


def _age_discrepancies(
    cfg: Config, source: dict[str, Any]
) -> list[dict[str, Any]]:
    """Compare cached vs recomputed age for every child (reported, never fixed)."""
    from ccat.domain import compute_age_at_xmas

    discrepancies: list[dict[str, Any]] = []
    for row in source["finals"]["child"]:
        child = normalize_row("child", row)
        dob = child.get("dob")
        try:
            computed = compute_age_at_xmas(str(dob), cfg.xmas) if dob else None
        except ValueError:
            computed = None
        cached = child.get("age_at_xmas")
        if computed != cached:
            discrepancies.append(
                {
                    "zite_record_id": child.get("zite_record_id"),
                    "stored": cached,
                    "computed": computed,
                }
            )
    return discrepancies


# ---------------------------------------------------------------------------
# Merge helpers (the final stores are append-and-merge, not projections).
# ---------------------------------------------------------------------------


def _merge_stored(
    entity: str,
    stored: list[dict[str, Any]],
    workbook: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Merge workbook rows over stored rows (workbook wins, no removals).

    A complete-store write must cover every persisted key, so the
    import submits the workbook rows plus the stored rows the workbook
    does not know about.  The workbook is usually an older snapshot
    than the database (a later acquisition adds rows); dropping those
    rows would delete people the season has not audited yet.

    Angel ids are unique per season.  A workbook row's audited letter
    wins: a stored row that holds the same letter (a duplicate
    submission reserved by the pipeline) releases it and keeps its
    family link, so the human assignments stay intact.

    Args:
        entity: "child", "family", or "sponsor".
        stored: The rows already in the database.
        workbook: The rows read from the source workbook.

    Returns:
        The merged row set (workbook rows first) and the stored rows
        that released an angel id.
    """
    key = str(ENTITY_SPECS[entity]["key"])
    known = {str(row.get(key)) for row in workbook}
    claimed = {
        str(row.get("angel_id"))
        for row in workbook
        if entity == "child" and row.get("angel_id") is not None
    }
    merged = list(workbook)
    released: list[dict[str, Any]] = []
    for row in stored:
        if str(row.get(key)) in known:
            continue
        extra = {
            name: value
            for name, value in row.items()
            if not name.startswith("_")
        }
        angel_id = extra.get("angel_id")
        if entity == "child" and angel_id is not None:
            if str(angel_id) in claimed:
                extra["angel_id"] = None
                released.append(
                    {"zite_record_id": extra.get(key), "angel_id": angel_id}
                )
        merged.append(extra)
    return merged, released


def _merge_raw(
    workbook: list[dict[str, Any]], stored: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Union one raw snapshot: workbook rows first, stored extras after.

    Args:
        workbook: The raw rows read from the source workbook.
        stored: The rows of the database's active acquisition run.

    Returns:
        The union of both row sets, keyed by ``zite_record_id``.
    """
    known = {str(row.get("zite_record_id") or "") for row in workbook}
    merged = list(workbook)
    for row in stored:
        if str(row.get("zite_record_id") or "") in known:
            continue
        merged.append(row)
    return merged


# ---------------------------------------------------------------------------
# The import flow.
# ---------------------------------------------------------------------------


async def _import_flow(
    cfg: Config,
    source: Path,
    source_data: dict[str, Any],
    sha_before: str,
    report: dict[str, Any],
    db_path: Path,
) -> int:
    """Run the controlled import; raises _StagedImportError on any problem.

    Returns:
        The batch id (already marked completed).
    """
    engine = create_async_engine(cfg.storage.database_url)
    uow = UnitOfWork(engine)
    try:
        async with uow.session() as session:
            season = await ensure_season_row(session, cfg)
            season_id = int(season.id)
        report["batch"]["season_id"] = season_id
        batch_id = _batch_insert(
            db_path,
            season_id,
            sha_before,
            source,
            _config_digest(cfg),
            "staged",
            None,
        )
        report["batch"]["id"] = batch_id

        async with uow.store(cfg, "migration") as repo:
            # Clarify sidecar first: nothing may depend on state that
            # imports later (plan §10 v1-bootstrap lesson, D5).
            await _import_clarify(repo, cfg, source_data, report)
            # Complete final stores — every stored value copied as
            # stored (migrate is trusted for derived values).  Rows the
            # database holds from a later acquisition are merged in,
            # never dropped: the stores are append-and-merge.
            for entity in ("child", "family", "sponsor"):
                rows = [
                    normalize_row(entity, row)
                    for row in source_data["finals"][entity]
                ]
                merged, released = _merge_stored(
                    entity, await repo.final_rows(entity), rows
                )
                await repo.commit_finals("migrate", entity, merged, "migration")
                report["angel_ids_released"].extend(released)
            raw = {
                entity: _merge_raw(
                    source_data["raw"][entity], await repo.raw_rows(entity)
                )
                for entity in ("sponsor", "family", "child")
            }
            await repo.replace_raw_snapshot("migrate", raw, "migration")
            await repo.replace_screen_diagnostics(
                source_data["logs"]["duplicate_log"],
                source_data["logs"]["dob_conflict_log"],
                "migration",
            )
            await repo.append_email_log(
                source_data["logs"]["email_validation_log"], "migration"
            )
            await repo.append_address_log(
                source_data["logs"]["address_validation_log"], "migration"
            )
            await _inventory_artifacts(repo, cfg, source_data, report)

        async with uow.session() as session:
            await _import_match_runs(session, season_id, source_data, report)

        sha_after = _file_sha256(source)
        report["source"]["sha256_after"] = sha_after
        if sha_after != sha_before:
            report["errors"].append("source workbook changed during import")
            raise _StagedImportError(
                report, "source workbook changed during import"
            )

        async with uow.store(cfg, "migration") as repo:
            checks, merged = await _validate(repo, source_data)
        report["validation"] = checks
        report["merged_rows"] = merged
        failed = [check for check in checks if not check["pass"]]
        if failed:
            report["errors"].append(
                f"{len(failed)} validation check(s) failed:"
                f" {[check['check'] for check in failed]}"
            )
            raise _StagedImportError(
                report,
                f"import failed validation:"
                f" {[check['check'] for check in failed]}",
            )

        _batch_complete(db_path, batch_id, report)
        report["batch"]["status"] = "completed"
        return batch_id
    finally:
        await engine.dispose()


def _write_report(path: Path, report: dict[str, Any]) -> None:
    """Atomically write the JSON report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    tmp.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _mark_failed_batch(
    cfg: Config,
    db_path: Path,
    sha256: str,
    source: Path,
    report_data: dict[str, Any],
) -> None:
    """Re-ensure the season and record the failed batch row.

    The rollback restore reverted the staged row, so the failed row is
    inserted fresh; the season re-ensure is idempotent bookkeeping.
    """
    try:
        season = asyncio.run(ensure_season(cfg, "migration"))
        _batch_insert(
            db_path,
            int(season.id),
            sha256,
            source,
            _config_digest(cfg),
            "failed",
            report_data,
        )
    except sqlite3.Error as error:  # pragma: no cover - reporting only
        logger.warning("could not record failed batch row: %s", error)


# ---------------------------------------------------------------------------
# Public entry point: ccat db-import.
# ---------------------------------------------------------------------------


def run(
    cfg: Config,
    from_path: str,
    db_url: str | None = None,
    force: bool = False,
    backup_path: str | None = None,
    report_path: str | None = None,
) -> Path:
    """Import a persistent-final workbook into SQLite (plan §10).

    Args:
        cfg: Season configuration (``--config``);
            ``storage.database_url`` names the target unless ``db_url``
            overrides it.
        from_path: Source workbook (read-only; hashed before and after
            the import).
        db_url: Optional ``--db`` override for the target database URL.
        force: Permit importing into a database that already holds
            data; requires ``backup_path``.
        backup_path: Restorable SQLite copy of the target made before
            any write; required with ``force`` against a nonempty
            target.
        report_path: JSON report destination; defaults to
            ``<db-path>.import.json``.

    Returns:
        The report path (the prior batch's report when reusing a
        completed import of the exact same source).

    Raises:
        ImportError: On any guard, parse, hash, or validation failure.
    """
    if db_url:
        cfg = cfg.model_copy(
            update={
                "storage": cfg.storage.model_copy(
                    update={"database_url": db_url}
                )
            }
        )
    source = Path(from_path)
    db_path = _db_path_from_url(cfg.storage.database_url)
    report = (
        Path(report_path) if report_path else Path(f"{db_path}.import.json")
    )

    if not source.is_file():
        raise ImportError(f"source workbook not found: {source}")
    if _aliases_database(source, db_path):
        raise ImportError(
            f"--from {source} resolves to the database file (or one of"
            " its WAL/SHM siblings); choose a different source or --db"
        )
    if _same_path(report, source) or _same_path(report, db_path):
        raise ImportError("report path must differ from source and database")

    run_migrations(cfg.storage.database_url)
    sha_before = _file_sha256(source)

    completed = _find_completed_batch(db_path, sha_before)
    if completed is not None:
        batch_id, _season_id, prior = completed
        prior_path = (
            Path(str(prior.get("report_path")))
            if prior and prior.get("report_path")
            else None
        )
        logger.info(
            "reusing completed import batch %d for source %s",
            batch_id,
            source,
        )
        if prior_path and prior_path.is_file():
            return prior_path
        if prior is not None:
            _write_report(report, prior)
            return report

    backup: Path | None = None
    if _db_is_nonempty(db_path):
        if not force:
            raise ImportError(
                f"target database {db_path} already holds data; refusing"
                " to import over it without --force AND --backup PATH"
            )
        if not backup_path:
            raise ImportError(
                "--force requires --backup PATH: a restorable SQLite"
                " copy of the target made before any write"
            )
        backup = Path(backup_path)
        if _same_path(backup, source) or _same_path(backup, db_path):
            raise ImportError("backup path must differ from source and db")
        backup.parent.mkdir(parents=True, exist_ok=True)
        _sqlite_snapshot(db_path, backup)
        _verify_sqlite_file(backup, f"backup {backup}")

    rollback = db_path.parent / f".{db_path.name}.{uuid4().hex}.rollback"
    _sqlite_snapshot(db_path, rollback)
    report_data: dict[str, Any] = {
        "source": {
            "path": str(source),
            "sha256_before": sha_before,
            "sha256_after": None,
        },
        "target_db": {
            "path": str(db_path),
            "url": cfg.storage.database_url,
        },
        "backup": {"path": str(backup)} if backup is not None else None,
        "config_hash": _config_digest(cfg),
        "counts": {},
        "merged_rows": {},
        "angel_ids_released": [],
        "age_discrepancies": [],
        "artifacts": {},
        "clarify": {},
        "match": {},
        "unrecognized_sheets": [],
        "validation": [],
        "versions": {
            "ccat": _ccat_version(),
            "alembic_head": _alembic_head(db_path),
        },
        "errors": [],
        "batch": {"id": None, "season_id": None, "status": "staged"},
        "report_path": str(report),
    }
    try:
        source_data = _read_source(cfg, source)
        report_data["unrecognized_sheets"] = source_data["unrecognized_sheets"]
        raw_ids = {
            str(row.get("zite_record_id") or "")
            for row in source_data["raw"]["child"]
            if row.get("zite_record_id") is not None
        }
        report_data["counts"] = {
            entity: _entity_counts(
                entity, source_data["finals"][entity], raw_ids
            )
            for entity in ("child", "family", "sponsor")
        }
        report_data["counts"]["raw"] = {
            table: len(rows) for table, rows in source_data["raw"].items()
        }
        report_data["counts"]["logs"] = {
            sheet: len(rows) for sheet, rows in source_data["logs"].items()
        }
        report_data["age_discrepancies"] = _age_discrepancies(cfg, source_data)
        asyncio.run(
            _import_flow(
                cfg, source, source_data, sha_before, report_data, db_path
            )
        )
    except _StagedImportError as failure:
        # The backup API restores byte-consistently in both directions.
        _sqlite_snapshot(rollback, db_path)
        _mark_failed_batch(cfg, db_path, sha_before, source, failure.report)
        failure.report["batch"]["status"] = "failed"
        _write_report(report, failure.report)
        raise ImportError(
            f"ccat db-import failed: {failure.message}"
        ) from failure
    except Exception as error:
        _sqlite_snapshot(rollback, db_path)  # backup API restores too
        report_data["errors"].append(f"{type(error).__name__}: {error}")
        _mark_failed_batch(cfg, db_path, sha_before, source, report_data)
        report_data["batch"]["status"] = "failed"
        _write_report(report, report_data)
        raise ImportError(f"ccat db-import failed: {error}") from error
    finally:
        rollback.unlink(missing_ok=True)

    _write_report(report, report_data)
    logger.info(
        "db-import complete: %d sponsors, %d families, %d children; report=%s",
        report_data["counts"]["sponsor"]["imported"],
        report_data["counts"]["family"]["imported"],
        report_data["counts"]["child"]["imported"],
        report,
    )
    return report


# ---------------------------------------------------------------------------
# Public entry point: ccat workbook-export (decision D6).
# ---------------------------------------------------------------------------


def _raw_export_frame(
    cfg: Config, entity: str, rows: list[dict[str, Any]]
) -> pl.DataFrame:
    """Rebuild one verbatim raw sheet from DB rows (legacy names)."""
    columns = list(cfg.tables[entity].columns)
    schema = {column: pl.String for column in columns}
    data = [
        {
            column: (None if row.get(column) is None else str(row.get(column)))
            for column in columns
        }
        for row in rows
    ]
    return pl.DataFrame(data, schema=schema)


def _log_export_frame(
    schema: dict[str, Any], rows: list[dict[str, Any]]
) -> pl.DataFrame:
    """Rebuild one log sheet; absent DB keys become null cells."""
    data = [{column: row.get(column) for column in schema} for row in rows]
    return pl.DataFrame(data, schema=dict(schema))


async def _export_flow(cfg: Config) -> dict[str, pl.DataFrame]:
    """Read every exportable collection from the database (read-only)."""
    engine = create_async_engine(cfg.storage.database_url)
    uow = UnitOfWork(engine)
    try:
        async with uow.session() as session:
            season = (
                await session.scalars(
                    select(Season).where(Season.name == cfg.season)
                )
            ).first()
            if season is None:
                raise ExportError(
                    f"no season {cfg.season!r} in"
                    f" {cfg.storage.database_url}; run `ccat db-import` first"
                )
            repo = SeasonStoreRepository(session, season)
            sheets: dict[str, pl.DataFrame] = {}
            for entity in ("child", "family", "sponsor"):
                rows = _plain(await repo.final_rows(entity))
                sheets[ENTITY_SPECS[entity]["sheet"]] = build_final_frame(
                    entity, rows
                )
            for entity in ("sponsor", "family", "child"):
                rows = await repo.raw_rows(entity)
                sheets[f"raw_{entity}"] = _raw_export_frame(cfg, entity, rows)
            sheets["duplicate_log"] = _log_export_frame(
                DUPLICATE_LOG_SCHEMA, await repo.duplicate_log_rows()
            )
            sheets["dob_conflict_log"] = _log_export_frame(
                DOB_CONFLICT_LOG_SCHEMA, await repo.dob_log_rows()
            )
            sheets["email_validation_log"] = _log_export_frame(
                EMAIL_VALIDATION_LOG_SCHEMA, await repo.email_log_rows()
            )
            sheets["address_validation_log"] = _log_export_frame(
                ADDRESS_VALIDATION_LOG_SCHEMA, await repo.address_log_rows()
            )
            report_rows = await repo.match_report_rows()
            if report_rows is not None:
                sheets["match_report"] = _log_export_frame(
                    {
                        "kind": pl.String,
                        "ref": pl.String,
                        "reason": pl.String,
                    },
                    report_rows,
                )
            return sheets
    finally:
        await engine.dispose()


def workbook_export_run(
    cfg: Config,
    output_path: str,
    db_url: str | None = None,
    report_path: str | None = None,
    force: bool = False,
) -> Path:
    """Export the database back into a fresh workbook (read-only, D6).

    Rollback evidence, never a second active truth: current finals, the
    raw snapshot, the four logs, and the current match report (when one
    exists) are written with the legacy boundary spellings (``create``,
    ``potential_anomoly``) and live age formulas.  Match archives stay
    queryable in the database and are NOT recreated as sheets.

    Args:
        cfg: Season configuration.
        output_path: Destination workbook; refused when it exists unless
            ``force``.
        db_url: Optional ``--db`` override for the source database URL.
        report_path: JSON report destination; defaults to
            ``<output>.export.json``.
        force: Overwrite an existing output file.

    Returns:
        The report path.

    Raises:
        ExportError: On an existing output, an unknown season, or a
            write failure.
    """
    if db_url:
        cfg = cfg.model_copy(
            update={
                "storage": cfg.storage.model_copy(
                    update={"database_url": db_url}
                )
            }
        )
    output = Path(output_path)
    if output.exists() and not force:
        raise ExportError(
            f"output already exists: {output}; refusing to overwrite"
            " without --force"
        )
    report = Path(report_path) if report_path else Path(f"{output}.export.json")
    if _same_path(report, output):
        raise ExportError("report path must differ from the output path")
    db_path = _db_path_from_url(cfg.storage.database_url)
    try:
        sheets = asyncio.run(_export_flow(cfg))
    except ExportError:
        raise
    except Exception as error:
        raise ExportError(f"workbook export failed: {error}") from error
    write_sheets(output, "migrate", sheets, xmas=cfg.xmas)
    report_data = {
        "source_db": {
            "path": str(db_path),
            "sha256": _file_sha256(db_path),
        },
        "output": {"path": str(output), "sha256": _file_sha256(output)},
        "counts": {
            name: frame.height for name, frame in sorted(sheets.items())
        },
        "archives": (
            "match archive runs remain queryable in the database and are"
            " not exported as timestamped sheets"
        ),
        "versions": {
            "ccat": _ccat_version(),
            "alembic_head": _alembic_head(db_path),
        },
    }
    _write_report(report, report_data)
    logger.info(
        "workbook-export complete: %s (sheets=%d); report=%s",
        output,
        len(sheets),
        report,
    )
    return report
