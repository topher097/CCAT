"""One-time bootstrap of the new v2 workbook (audit_architecture.md §11).

``ccat audit-migrate --config config/ccat_2026_v2.json --from
./output/ccat_2026.xlsx`` copies the raw layer verbatim from the old
workbook (opened **read-only** — polars reads only; no ``write_sheets``
call ever targets it), builds the dense adj sheets from the old sparse
``adj_child`` plus the old ``final_*`` rows (the most recent
adjudicated truth), and projects all three finals so downstream
modules work immediately.  The old workbook is never modified.
"""

import logging
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

import polars as pl

from ccat.acquire import AGE_PREF_BOUNDS
from ccat.audit import AuditError
from ccat.audit.store import (
    ADJ_CHILD_COLUMNS,
    ADJ_FAMILY_COLUMNS,
    ADJ_SPONSOR_COLUMNS,
    CHILD_EDITABLE_COLUMNS,
    CHILD_LINKAGE_COLUMNS,
    FAMILY_EDITABLE_COLUMNS,
    FAMILY_LINKAGE_COLUMNS,
    FINAL_FAMILY_COLUMNS,
    FINAL_SPONSOR_COLUMNS,
    SPONSOR_EDITABLE_COLUMNS,
    SPONSOR_LINKAGE_COLUMNS,
    STATUS_AUDITED,
    STATUS_NEEDS_CLARIFICATION,
    STATUS_NEW,
    _computed_age,
    as_bool,
    build_adj_frame,
    normalize_row,
    project_final,
    project_final_child,
    status_of,
)
from ccat.config import Config
from ccat.io import list_sheets, read_sheet, write_sheets

logger = logging.getLogger(__name__)

#: Sheets whose presence in the target workbook blocks the migration.
_BLOCKING_SHEETS = (
    "adj_child",
    "adj_family",
    "adj_sponsor",
    "final_child",
    "final_family",
    "final_sponsor",
)

#: Raw-layer sheets copied verbatim from the old workbook (§11.2).
#: The old workbook's ``raw_*`` sheets already carry the old pipeline's
#: cleaned, ID-assigned rows, so they double as the prepared
#: ``*_submissions`` rows the audit store works from.
_RAW_SHEETS = (
    "raw_sponsor",
    "raw_family",
    "raw_child",
    "dedup_log",
    "dob_conflict_log",
)

#: Old-final columns overlaid onto the dense adj_child row (§11.3):
#: data, computed, and module-owned columns; linkage always comes from
#: the raw row.
_FINAL_TAKEOVER_COLUMNS = (
    *CHILD_EDITABLE_COLUMNS,
    "age_at_xmas",
    "generated_info_sheet",
)

#: The D4 mapping for the old Boolean special_needs column.
_SPECIAL_NEEDS_LEGACY = "other: unspecified"


def run(
    cfg: Config,
    from_path: str,
    force: bool = False,
) -> None:
    """Bootstrap the new workbook from the old one (§11).

    Args:
        cfg: Pipeline configuration (``cfg.excel_file`` is the **new**
            workbook; never written by an old-config command).
        from_path: The old workbook, opened read-only.
        force: Re-create the adj/final sheets even when the target
            already contains them.

    Raises:
        AuditError: If the source workbook or a required sheet is
            missing, or the target already contains adj/final sheets
            without ``force``.
    """
    source = Path(from_path)
    if not source.is_file():
        raise AuditError(f"source workbook not found: {source}")
    existing = set(list_sheets(cfg.excel_file))
    blocking = sorted(set(_BLOCKING_SHEETS) & existing)
    if blocking and not force:
        raise AuditError(
            f"target workbook {cfg.excel_file} already contains"
            f" {blocking}; the bootstrap only writes a fresh workbook"
            " (pass --force to re-create them from the source)"
        )
    if str(source.resolve()) == str(Path(cfg.excel_file).resolve()):
        raise AuditError("the --from workbook must differ from excel_file")

    frames: dict[str, pl.DataFrame | None] = {}
    for sheet in _RAW_SHEETS + (
        "adj_child",
        "final_child",
        "final_family",
        "final_sponsor",
    ):
        frames[sheet] = read_sheet(source, sheet)
    raw_child_frame = frames["raw_child"]
    raw_family_frame = frames["raw_family"]
    raw_sponsor_frame = frames["raw_sponsor"]
    if (
        raw_child_frame is None
        or raw_family_frame is None
        or raw_sponsor_frame is None
    ):
        raise AuditError(
            "source workbook has no raw_sponsor/raw_family/raw_child sheet;"
            " run `ccat acquire` on the old system first"
        )
    raw_children = raw_child_frame.to_dicts()
    raw_families = raw_family_frame.to_dicts()
    raw_sponsors = raw_sponsor_frame.to_dicts()
    old_adj = (
        frames["adj_child"].to_dicts()
        if frames["adj_child"] is not None
        else []
    )
    old_adj_by_id = {
        row.get("zite_record_id"): row
        for row in old_adj
        if row.get("zite_record_id")
    }
    old_final_by_id = _keyed_by(frames["final_child"], "zite_record_id")
    old_final_family = _keyed_by(frames["final_family"], "family_id")
    old_final_sponsor = _keyed_by(frames["final_sponsor"], "sponsor_id")

    # Cutover marking rules (§11.3): sponsorship from the old final
    # sheet; clarification reply state from the clarify status store
    # (the output directories are shared across the old and v2 configs).
    sponsored_families = {
        family_id
        for family_id, row in old_final_family.items()
        if row.get("assigned_sponsor_id") is not None
    }
    from ccat.clarify.status import load_statuses

    statuses = load_statuses(cfg)
    replied_families = {
        family_id for family_id, status in statuses.items() if status.replied
    }
    marked_at = datetime.now(UTC).isoformat()

    adj_children = [
        _migrated_child_row(
            raw,
            old_adj_by_id.get(raw.get("zite_record_id")),
            old_final_by_id.get(raw.get("zite_record_id")),
            cfg,
            sponsored=raw.get("family_id") in sponsored_families,
            replied=raw.get("family_id") in replied_families,
            marked_at=marked_at,
        )
        for raw in raw_children
        if raw.get("zite_record_id") is not None
    ]
    children_per_family: dict[Any, int] = {}
    for row in adj_children:
        if row.get("family_id") is not None:
            children_per_family[row["family_id"]] = (
                children_per_family.get(row["family_id"], 0) + 1
            )

    adj_families = []
    for raw in raw_families:
        if raw.get("family_id") is None:
            continue
        row = _seeded_family(raw)
        prior = old_final_family.get(raw["family_id"], {})
        row["assigned_sponsor_id"] = prior.get("assigned_sponsor_id")
        row["assigned_angel_ids"] = prior.get("assigned_angel_ids")
        row["num_angels_in_family"] = children_per_family.get(
            raw["family_id"], 0
        )
        adj_families.append(row)

    adj_sponsors = []
    for raw in raw_sponsors:
        if raw.get("sponsor_id") is None:
            continue
        row = _seeded_sponsor(raw)
        prior = old_final_sponsor.get(raw["sponsor_id"], {})
        row["assigned_angel_ids"] = prior.get("assigned_angel_ids")
        row["num_angels_assigned"] = prior.get("num_angels_assigned")
        adj_sponsors.append(row)

    sheets: dict[str, pl.DataFrame] = {}
    for sheet in _RAW_SHEETS:
        frame = frames[sheet]
        if frame is not None and sheet != "dedup_log":
            # dedup_log is a record of the OLD pipeline's merged
            # duplicates; the new pipeline flags duplicates without
            # merging (duplicate_log), so the legacy sheet is dropped.
            sheets[sheet] = frame
    # The old raw sheets are already cleaned and ID-assigned; they feed
    # the prepared submissions layer directly (§11.2).  The old
    # `dedup_log` (merged duplicates) is not carried over.
    for entity, sheet in (
        ("sponsor", "raw_sponsor"),
        ("family", "raw_family"),
        ("child", "raw_child"),
    ):
        frame = frames[sheet]
        sheets[f"{entity}_submissions"] = (
            frame if frame is not None else pl.DataFrame()
        )
    sheets["dob_conflict_log"] = sheets.get("dob_conflict_log", pl.DataFrame())
    child_frame = build_adj_frame("child", adj_children)
    family_frame = build_adj_frame("family", adj_families)
    sponsor_frame = build_adj_frame("sponsor", adj_sponsors)
    sheets["adj_child"] = child_frame
    sheets["adj_family"] = family_frame
    sheets["adj_sponsor"] = sponsor_frame
    sheets["final_child"] = project_final_child(child_frame)
    sheets["final_family"] = project_final(family_frame, FINAL_FAMILY_COLUMNS)
    sheets["final_sponsor"] = project_final(
        sponsor_frame, FINAL_SPONSOR_COLUMNS
    )
    write_sheets(cfg.excel_file, "migrate", sheets, xmas=cfg.xmas)

    audited = sum(1 for row in adj_children if row["status"] == STATUS_AUDITED)
    notes = sum(
        1 for row in adj_children if row["status"] == STATUS_NEEDS_CLARIFICATION
    )
    assignments = sum(
        1 for row in adj_families if row.get("assigned_sponsor_id") is not None
    )
    logger.info(
        "migration complete: %d children (%d audited, %d needing"
        " clarification), %d families (%d with carried-over assignments),"
        " %d sponsors; source never written",
        len(adj_children),
        audited,
        notes,
        len(adj_families),
        assignments,
        len(adj_sponsors),
    )


def _keyed_by(
    frame: pl.DataFrame | None, key: str
) -> dict[Any, dict[str, Any]]:
    """Index one old frame's rows by a key column."""
    if frame is None:
        return {}
    return {
        row.get(key): row
        for row in frame.to_dicts()
        if row.get(key) is not None
    }


def _seeded_family(raw: dict[str, Any]) -> dict[str, Any]:
    """One dense adj_family row seeded from its raw row (§11.4)."""
    row: dict[str, Any] = {column: None for column in ADJ_FAMILY_COLUMNS}
    for column in (*FAMILY_LINKAGE_COLUMNS, *FAMILY_EDITABLE_COLUMNS):
        row[column] = raw.get(column)
    row["family_id"] = raw.get("family_id")
    row["status"] = STATUS_NEW
    row["audited"] = False
    row["ignore"] = False
    return row


def _seeded_sponsor(raw: dict[str, Any]) -> dict[str, Any]:
    """One dense adj_sponsor row seeded from its raw row (§11.4)."""
    row: dict[str, Any] = {column: None for column in ADJ_SPONSOR_COLUMNS}
    for column in (*SPONSOR_LINKAGE_COLUMNS, *SPONSOR_EDITABLE_COLUMNS):
        row[column] = raw.get(column)
    row["sponsor_id"] = raw.get("sponsor_id")
    bounds = AGE_PREF_BOUNDS.get(str(raw.get("age_preference") or ""))
    if bounds is not None:
        row["pref_age_min"], row["pref_age_max"] = bounds
    row["status"] = STATUS_NEW
    row["audited"] = False
    row["ignore"] = False
    return row


def _migrated_child_row(
    raw: dict[str, Any],
    old_adj: dict[str, Any] | None,
    old_final: dict[str, Any] | None,
    cfg: Config,
    sponsored: bool = False,
    replied: bool = True,
    marked_at: str | None = None,
) -> dict[str, Any]:
    """Build one dense adj_child row from the old sheets (§11.3).

    Starts from the raw row, overlays the old sparse adj overrides,
    then — when an old final_child row exists — takes its data columns
    as the dense values (the most recent adjudicated truth) and its
    controls.  ``screened`` marks children the old screening touched;
    ``status`` is computed against the raw baseline.

    Cutover marking rules (§11.3, user decision): every child whose
    family carries an assigned sponsor is marked **audited**; a child
    with a clarification note whose family has **no response** (no
    reply recorded in the clarify status store) is marked
    **needs_clarification** instead — the outstanding question must be
    answered before the row counts as fully audited.

    Args:
        raw: The raw_child row.
        old_adj: The old sparse adj_child row, when screened.
        old_final: The old final_child row, when audited/saved.
        cfg: Pipeline configuration.
        sponsored: The child's family has an assigned sponsor.
        replied: The child's family's clarification was replied to
            (True when no clarification is outstanding).
        marked_at: ISO timestamp stamped when the cutover marks a
            never-stamped row audited.

    Returns:
        The dense adj_child row with controls and status set.
    """
    row: dict[str, Any] = {column: None for column in ADJ_CHILD_COLUMNS}
    for column in (*CHILD_LINKAGE_COLUMNS, *CHILD_EDITABLE_COLUMNS):
        row[column] = raw.get(column)
    if old_adj is not None:
        for column in CHILD_EDITABLE_COLUMNS:
            value = old_adj.get(column)
            if value is not None:
                row[column] = value
    if old_final is not None:
        for column in _FINAL_TAKEOVER_COLUMNS:
            if column in old_final:
                row[column] = old_final.get(column)
        special = old_final.get("special_needs")
        if as_bool(special):
            row["special_needs"] = _SPECIAL_NEEDS_LEGACY
        else:
            row["special_needs"] = None
        row["audited"] = as_bool(old_final.get("audited"))
        row["require_clarification"] = old_final.get("require_clarification")
        row["audited_at"] = old_final.get("audited_at")
        row["generated_info_sheet"] = as_bool(
            old_final.get("generated_info_sheet")
        )
    row["screened"] = old_adj is not None
    row["ignore"] = False
    # Cutover marking rules.
    has_note = (
        row.get("require_clarification") is not None
        and str(row.get("require_clarification")).strip() != ""
    )
    if sponsored and not (has_note and not replied):
        row["audited"] = True
        if not row.get("audited_at"):
            row["audited_at"] = marked_at
    if has_note and not replied:
        # An outstanding clarification beats the audited mark (§5.3
        # precedence via the state machine: the note must be answered).
        row["audited"] = False
    age = _computed_age(row.get("dob"), cfg)
    if age is None and old_final is not None:
        age = _as_int_or_none(old_final.get("age_at_xmas"))
    row["age_at_xmas"] = age
    baseline = {
        **{column: raw.get(column) for column in CHILD_LINKAGE_COLUMNS},
        **{column: raw.get(column) for column in CHILD_EDITABLE_COLUMNS},
    }
    row["status"] = status_of(row, baseline, "child")
    return normalize_row("child", row)


def _as_int_or_none(value: Any) -> int | None:
    """Coerce a legacy age cell to int, tolerating text round-trips."""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
