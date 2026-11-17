"""Module 3 data layer — dense adjudication store (audit_architecture.md §5–§7).

Pure polars/pydantic helpers over the dense ``adj_*`` sheets and their
``final_*`` projections.  No NiceGUI imports: every pipeline module
(screening, match, generate, migrate) shares this layer, so the adj
contract is defined exactly once.

Core model (§5.1):

- An adj row's data columns ARE the adjudicated record — seeded from the
  entity's baseline (§5.2), pre-filled by screening (§7.3), then edited
  by humans.  A null cell means the value is genuinely absent, never
  "see raw".
- ``final_X`` is a pure projection of ``adj_X`` (§7.1): drop ignored
  rows, drop the pure control columns (``audited``/``ignore`` plus the
  child-only ``screened``), keep everything else in the documented
  column order.
- ``status`` (§5.3) is a derived, snake_case string recomputed on every
  audit save so the workbook is filterable in Excel and downstream
  modules can gate on it (§7.5).
"""

import logging
import threading
from datetime import datetime, UTC
from typing import Any
from collections.abc import Sequence

import polars as pl
from polars._typing import PolarsDataType
from pydantic import BaseModel, Field

from ccat.acquire import AGE_PREF_BOUNDS, compute_age_at_xmas
from ccat.audit import AuditError
from ccat.config import Config
from ccat.io import read_sheet, write_sheets

logger = logging.getLogger(__name__)


#: Status values (§5.3) and their UI badge colors.
STATUS_NEW = "new"
STATUS_IN_PROGRESS = "in_progress"
STATUS_NEEDS_CLARIFICATION = "needs_clarification"
STATUS_IGNORED = "ignored"
STATUS_AUDITED = "audited"
STATUS_COLORS: dict[str, str] = {
    STATUS_NEW: "blue",
    STATUS_IN_PROGRESS: "yellow",
    STATUS_NEEDS_CLARIFICATION: "orange",
    STATUS_IGNORED: "red",
    STATUS_AUDITED: "green",
}
STATUS_ORDER = (
    STATUS_NEW,
    STATUS_IN_PROGRESS,
    STATUS_NEEDS_CLARIFICATION,
    STATUS_IGNORED,
    STATUS_AUDITED,
)

#: Common control columns on all three adj sheets (§4.3).
CONTROL_COLUMNS = (
    "status",
    "audited",
    "ignore",
    "require_clarification",
    "audited_at",
)

#: Pre-filled clarification note for orphaned children (§8.7).
ORPHAN_NOTE = "No matching family registration for this child's primary email"

#: Prefix of the sibling-DOB-conflict note merged into
#: ``potential_anomoly`` (moved from the old audit server).
DOB_CONFLICT_PREFIX = "Sibling DOB conflict"


# ---------------------------------------------------------------------------
# Sheet schemas (§4.3) — the single source of truth for adj/final columns.
# ---------------------------------------------------------------------------

ADJ_CHILD_COLUMNS: tuple[str, ...] = (
    "zite_record_id",
    "zite_created_at",
    "created",
    "family_id",
    "angel_id",
    "first_name",
    "primary_email",
    "dob",
    "age_reported",
    "gender",
    "special_needs",
    "need_1",
    "need_2",
    "desire_1",
    "desire_2",
    "shoe_size",
    "shoe_size_type",
    "pant_waist_size",
    "pant_inseam",
    "pant_size_letter",
    "pant_size_type",
    "jean_size",
    "shirt_size_letter",
    "shirt_size_type",
    "favorite_colors",
    "interests",
    "other_notes",
    "potential_anomoly",
    "source",
    "age_at_xmas",
    "generated_info_sheet",
    "screened",
    "potential_duplicate",
    *CONTROL_COLUMNS,
)

ADJ_CHILD_SCHEMA: dict[str, PolarsDataType] = {
    "zite_record_id": pl.String,
    "zite_created_at": pl.String,
    "created": pl.String,
    "family_id": pl.Int64,
    "angel_id": pl.String,
    "first_name": pl.String,
    "primary_email": pl.String,
    "dob": pl.String,
    "age_reported": pl.Int64,
    "gender": pl.String,
    "special_needs": pl.String,
    "need_1": pl.String,
    "need_2": pl.String,
    "desire_1": pl.String,
    "desire_2": pl.String,
    "shoe_size": pl.String,
    "shoe_size_type": pl.String,
    "pant_waist_size": pl.String,
    "pant_inseam": pl.String,
    "pant_size_letter": pl.String,
    "pant_size_type": pl.String,
    "jean_size": pl.String,
    "shirt_size_letter": pl.String,
    "shirt_size_type": pl.String,
    "favorite_colors": pl.String,
    "interests": pl.String,
    "other_notes": pl.String,
    "potential_anomoly": pl.String,
    "source": pl.String,
    "age_at_xmas": pl.Int64,
    "generated_info_sheet": pl.Boolean,
    "screened": pl.Boolean,
    "potential_duplicate": pl.Boolean,
    "status": pl.String,
    "audited": pl.Boolean,
    "ignore": pl.Boolean,
    "require_clarification": pl.String,
    "audited_at": pl.String,
}

#: adj_child column partitions (§4.3).
CHILD_LINKAGE_COLUMNS = (
    "zite_record_id",
    "zite_created_at",
    "created",
    "family_id",
    "angel_id",
    "source",
)
CHILD_EDITABLE_COLUMNS = (
    "first_name",
    "primary_email",
    "dob",
    "age_reported",
    "gender",
    "special_needs",
    "need_1",
    "need_2",
    "desire_1",
    "desire_2",
    "shoe_size",
    "shoe_size_type",
    "pant_waist_size",
    "pant_inseam",
    "pant_size_letter",
    "pant_size_type",
    "jean_size",
    "shirt_size_letter",
    "shirt_size_type",
    "favorite_colors",
    "interests",
    "other_notes",
    "potential_anomoly",
)
CHILD_COMPUTED_COLUMNS = ("age_at_xmas",)
CHILD_MODULE_COLUMNS = ("generated_info_sheet",)
CHILD_CONTROL_COLUMNS = (*CONTROL_COLUMNS, "screened")

#: Column keys the child detail UI accepts in a save payload.
CHILD_SAVE_FIELDS = (
    *CHILD_EDITABLE_COLUMNS,
    "audited",
    "ignore",
    "require_clarification",
)

ADJ_FAMILY_COLUMNS: tuple[str, ...] = (
    "family_id",
    "zite_record_id",
    "zite_created_at",
    "created",
    "primary_email",
    "first_name",
    "last_name",
    "phone",
    "street_address",
    "apt_floor",
    "city",
    "zip_code",
    "state",
    "secondary_first_name",
    "secondary_last_name",
    "secondary_email",
    "secondary_phone",
    "gift_pickup_location",
    "source",
    "potential_duplicate",
    "accepts_disclaimer",
    "assigned_sponsor_id",
    "assigned_angel_ids",
    "num_angels_in_family",
    *CONTROL_COLUMNS,
)

ADJ_FAMILY_SCHEMA: dict[str, PolarsDataType] = {
    "family_id": pl.Int64,
    "zite_record_id": pl.String,
    "zite_created_at": pl.String,
    "created": pl.String,
    "primary_email": pl.String,
    "first_name": pl.String,
    "last_name": pl.String,
    "phone": pl.String,
    "street_address": pl.String,
    "apt_floor": pl.String,
    "city": pl.String,
    "zip_code": pl.String,
    "state": pl.String,
    "secondary_first_name": pl.String,
    "secondary_last_name": pl.String,
    "secondary_email": pl.String,
    "secondary_phone": pl.String,
    "gift_pickup_location": pl.String,
    "source": pl.String,
    "potential_duplicate": pl.Boolean,
    "accepts_disclaimer": pl.Boolean,
    "assigned_sponsor_id": pl.Int64,
    "assigned_angel_ids": pl.String,
    "num_angels_in_family": pl.Int64,
    "status": pl.String,
    "audited": pl.Boolean,
    "ignore": pl.Boolean,
    "require_clarification": pl.String,
    "audited_at": pl.String,
}

FAMILY_LINKAGE_COLUMNS = (
    "zite_record_id",
    "zite_created_at",
    "created",
    "source",
    "accepts_disclaimer",
)
FAMILY_EDITABLE_COLUMNS = (
    "primary_email",
    "first_name",
    "last_name",
    "phone",
    "street_address",
    "apt_floor",
    "city",
    "zip_code",
    "state",
    "secondary_first_name",
    "secondary_last_name",
    "secondary_email",
    "secondary_phone",
    "gift_pickup_location",
)
FAMILY_ASSIGNMENT_COLUMNS = (
    "assigned_sponsor_id",
    "assigned_angel_ids",
    "num_angels_in_family",
)
FAMILY_SAVE_FIELDS = (
    *FAMILY_EDITABLE_COLUMNS,
    "audited",
    "ignore",
    "require_clarification",
)

ADJ_SPONSOR_COLUMNS: tuple[str, ...] = (
    "sponsor_id",
    "zite_record_id",
    "create",
    "email",
    "sponsor_type",
    "organization_name",
    "prior_assigned_angel_ids",
    "first_name",
    "last_name",
    "phone",
    "num_children",
    "age_preference",
    "gender_preference",
    "dropoff_location",
    "source",
    "potential_duplicate",
    "accepts_disclaimer",
    "pref_age_min",
    "pref_age_max",
    "assigned_angel_ids",
    "num_angels_assigned",
    *CONTROL_COLUMNS,
)

ADJ_SPONSOR_SCHEMA: dict[str, PolarsDataType] = {
    "sponsor_id": pl.Int64,
    "zite_record_id": pl.String,
    "create": pl.String,
    "email": pl.String,
    "sponsor_type": pl.String,
    "organization_name": pl.String,
    "prior_assigned_angel_ids": pl.String,
    "first_name": pl.String,
    "last_name": pl.String,
    "phone": pl.String,
    "num_children": pl.Int64,
    "age_preference": pl.String,
    "gender_preference": pl.String,
    "dropoff_location": pl.String,
    "source": pl.String,
    "potential_duplicate": pl.Boolean,
    "accepts_disclaimer": pl.Boolean,
    "pref_age_min": pl.Int64,
    "pref_age_max": pl.Int64,
    "assigned_angel_ids": pl.String,
    "num_angels_assigned": pl.Int64,
    "status": pl.String,
    "audited": pl.Boolean,
    "ignore": pl.Boolean,
    "require_clarification": pl.String,
    "audited_at": pl.String,
}

SPONSOR_LINKAGE_COLUMNS = (
    "zite_record_id",
    "create",
    "source",
    "accepts_disclaimer",
)
SPONSOR_EDITABLE_COLUMNS = (
    "email",
    "sponsor_type",
    "organization_name",
    "prior_assigned_angel_ids",
    "first_name",
    "last_name",
    "phone",
    "num_children",
    "age_preference",
    "gender_preference",
    "dropoff_location",
)
SPONSOR_COMPUTED_COLUMNS = ("pref_age_min", "pref_age_max")
SPONSOR_ASSIGNMENT_COLUMNS = ("assigned_angel_ids", "num_angels_assigned")
SPONSOR_SAVE_FIELDS = (
    *SPONSOR_EDITABLE_COLUMNS,
    "audited",
    "ignore",
    "require_clarification",
)

#: Final-sheet column orders (§7.1): the adj order minus the pure control
#: columns dropped by the projection.
_DROPPED_BY_PROJECTION = {"audited", "ignore", "screened"}
FINAL_CHILD_COLUMNS: tuple[str, ...] = tuple(
    column
    for column in ADJ_CHILD_COLUMNS
    if column not in _DROPPED_BY_PROJECTION
)
FINAL_FAMILY_COLUMNS: tuple[str, ...] = tuple(
    column
    for column in ADJ_FAMILY_COLUMNS
    if column not in _DROPPED_BY_PROJECTION
)
FINAL_SPONSOR_COLUMNS: tuple[str, ...] = tuple(
    column
    for column in ADJ_SPONSOR_COLUMNS
    if column not in _DROPPED_BY_PROJECTION
)

#: Entity registry: name -> (sheet, key column, columns, schema, editable).
ENTITY_SPECS: dict[str, dict[str, Any]] = {
    "child": {
        "sheet": "adj_child",
        "key": "zite_record_id",
        "columns": ADJ_CHILD_COLUMNS,
        "schema": ADJ_CHILD_SCHEMA,
        "editable": CHILD_EDITABLE_COLUMNS,
        "save_fields": CHILD_SAVE_FIELDS,
        "status_editable": tuple(
            column
            for column in CHILD_EDITABLE_COLUMNS
            if column != "special_needs"
        ),
    },
    "family": {
        "sheet": "adj_family",
        "key": "family_id",
        "columns": ADJ_FAMILY_COLUMNS,
        "schema": ADJ_FAMILY_SCHEMA,
        "editable": FAMILY_EDITABLE_COLUMNS,
        "save_fields": FAMILY_SAVE_FIELDS,
        "status_editable": FAMILY_EDITABLE_COLUMNS,
    },
    "sponsor": {
        "sheet": "adj_sponsor",
        "key": "sponsor_id",
        "columns": ADJ_SPONSOR_COLUMNS,
        "schema": ADJ_SPONSOR_SCHEMA,
        "editable": SPONSOR_EDITABLE_COLUMNS,
        "save_fields": SPONSOR_SAVE_FIELDS,
        "status_editable": SPONSOR_EDITABLE_COLUMNS,
    },
}


#: Serializes read-modify-write cycles across threads/tabs (§9.2).
_WRITE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Value helpers.
# ---------------------------------------------------------------------------


def as_bool(value: Any) -> bool:
    """Coerce a sheet value (bool or string) to bool, tolerating round-trips.

    Args:
        value: Raw cell value (may be None, bool, or "true"/"false" text).

    Returns:
        The boolean value; None and unrecognized text are False.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() == "true"


def _as_int(value: Any) -> int | None:
    """Coerce a sheet value to int, tolerating float/text round-trips.

    Args:
        value: Raw cell value.

    Returns:
        The integer, or None when the value is absent or unparsable.
    """
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        return int(float(value)) if isinstance(value, float) else int(value)
    except (TypeError, ValueError):
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None


def _as_text(value: Any) -> str | None:
    """Normalize a sheet value to stripped text; blank becomes None.

    Args:
        value: Raw cell value.

    Returns:
        The stripped string, or None when the value is absent/blank.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _values_differ(left: Any, right: Any) -> bool:
    """String-normalized inequality used by ``has_edits`` (§5.3).

    Args:
        left: One cell value.
        right: The other cell value.

    Returns:
        False when both are absent/blank or equal after normalization.
    """
    return _as_text(left) != _as_text(right)


def compute_status(
    has_edits: bool, has_note: bool, audited: bool, ignore: bool
) -> str:
    """Derive one entity's status (§5.3).

    Args:
        has_edits: Any adjudicable data column differs from the baseline.
        has_note: The clarification note is non-empty.
        audited: The "information is audited" checkbox state.
        ignore: The ignore flag.

    Returns:
        The snake_case status string.
    """
    if ignore:
        return STATUS_IGNORED
    if audited:
        return STATUS_AUDITED
    if has_note:
        return STATUS_NEEDS_CLARIFICATION
    if has_edits:
        return STATUS_IN_PROGRESS
    return STATUS_NEW


def status_of(
    row: dict[str, Any], baseline: dict[str, Any], entity: str
) -> str:
    """Compute the status of one adj row against its baseline.

    Special-needs selection is a control, not an override (§5.3): it
    never counts toward ``has_edits``, mirroring today's checkbox which
    never affected queue state.

    Args:
        row: The dense adj row (already normalized).
        baseline: The entity's baseline values (§5.2).
        entity: "child", "family", or "sponsor".

    Returns:
        The derived status string (§5.3).
    """
    editable = ENTITY_SPECS[entity]["status_editable"]
    has_edits = any(
        _values_differ(row.get(column), baseline.get(column))
        for column in editable
    )
    has_note = _as_text(row.get("require_clarification")) is not None
    return compute_status(
        has_edits,
        has_note,
        as_bool(row.get("audited")),
        as_bool(row.get("ignore")),
    )


# ---------------------------------------------------------------------------
# DOB-conflict helpers (moved from the old audit server, §9.2).
# ---------------------------------------------------------------------------


def dob_conflict_notes(
    log_rows: list[dict[str, Any]] | None, zite_record_id: str
) -> str | None:
    """Collect sibling DOB conflict notes for one child (§5.6, §8.6).

    Args:
        log_rows: Rows of the ``dob_conflict_log`` sheet (or None when
            the sheet is absent).
        zite_record_id: The child's join key.

    Returns:
        One note per conflicting sibling pair (child seen as either
        side of the pair), joined with ``"; "``, or None when the child
        has no conflicts.
    """
    notes: list[str] = []
    seen_siblings: set[Any] = set()
    for row in log_rows or []:
        for side in ("a", "b"):
            if row.get(f"child_{side}_zite_record_id") != zite_record_id:
                continue
            other = "b" if side == "a" else "a"
            sibling_id = row.get(f"child_{other}_zite_record_id")
            # One note per conflicting sibling, however the sheet was
            # written (an older run could contain both directions).
            if sibling_id in seen_siblings:
                break
            seen_siblings.add(sibling_id)
            notes.append(
                f"{DOB_CONFLICT_PREFIX}: born"
                f" {row.get('gap_months')} month(s)"
                f" ({row.get('gap_days')} day(s)) apart from sibling"
                f" {row.get(f'child_{other}_first_name')}"
                f" ({row.get(f'child_{other}_angel_id')},"
                f" dob {row.get(f'child_{other}_dob')})"
            )
            break
    return "; ".join(notes) if notes else None


def merge_anomoly(existing: str | None, notes: str | None) -> str | None:
    """Merge conflict notes into a ``potential_anomoly`` value.

    Args:
        existing: The current ``potential_anomoly`` text (may be null).
        notes: Notes from :func:`dob_conflict_notes` (may be null).

    Returns:
        The merged text: notes are appended after any existing text
        once (idempotent), or unchanged when there are no notes.
    """
    if not notes:
        return existing
    text = (existing or "").strip()
    if not text:
        return notes
    if notes in text:
        return text
    return f"{text}; {notes}"


# ---------------------------------------------------------------------------
# Duplicate-submission groups (§6).
# ---------------------------------------------------------------------------


def _submission_sort_key(row: dict[str, Any]) -> tuple[str, str]:
    """Order key matching acquire's dedup ordering (§5.4)."""
    return (str(row.get("created") or ""), str(row.get("zite_record_id") or ""))


def coalesce_family_group(submissions: list[dict[str, Any]]) -> dict[str, Any]:
    """Coalesce one family submission group (§6.1, D8).

    Semantics are identical to acquire's ``dedup_families``: rows are
    ordered by (``created``, ``zite_record_id``) ascending and the first
    non-null value per column wins.

    Args:
        submissions: The group's cleaned submission rows (any order).

    Returns:
        The coalesced values; empty dict for an empty group.
    """
    if not submissions:
        return {}
    ordered = sorted(submissions, key=_submission_sort_key)
    merged: dict[str, Any] = {}
    for key in {key for row in ordered for key in row}:
        for row in ordered:
            if row.get(key) is not None:
                merged[key] = row[key]
                break
    return merged


def combine_child_group(submissions: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine one child duplicate group (§6.2, D8).

    Most-recent-non-null per column: rows are ordered by
    (``created``, ``zite_record_id``) **descending** — the child dedup
    keeps the latest submission — and the first non-null value wins, so
    the latest submission's value is used whenever it has one and blanks
    fall through to earlier submissions.

    Args:
        submissions: The group's cleaned submission rows (any order).

    Returns:
        The combined values; empty dict for an empty group.
    """
    if not submissions:
        return {}
    ordered = sorted(submissions, key=_submission_sort_key, reverse=True)
    merged: dict[str, Any] = {}
    for key in {key for row in ordered for key in row}:
        for row in ordered:
            if row.get(key) is not None:
                merged[key] = row[key]
                break
    return merged


def group_submissions(
    submissions: list[dict[str, Any]], entity: str
) -> dict[tuple[str | None, ...], list[dict[str, Any]]]:
    """Group submission rows the way acquire's dedup does (§6).

    Families group by ``primary_email``; children group by
    (``primary_email``, ``first_name``).  Rows missing any key value are
    singleton groups keyed by their zite id.

    Args:
        submissions: The submission-sheet rows.
        entity: "family" or "child".

    Returns:
        Mapping of group key to that group's rows (unordered).
    """
    keys = (
        ("primary_email",)
        if entity == "family"
        else ("primary_email", "first_name")
    )
    groups: dict[tuple[str | None, ...], list[dict[str, Any]]] = {}
    for row in submissions:
        values = tuple(_as_text(row.get(key)) for key in keys)
        if any(value is None for value in values):
            values = (str(row.get("zite_record_id") or ""),)
        groups.setdefault(values, []).append(row)
    return groups


# ---------------------------------------------------------------------------
# Ignore semantics (§5.5).
# ---------------------------------------------------------------------------

#: Columns an ignored row keeps, per entity, beyond the controls (§5.5).
_IGNORE_RETAINED: dict[str, frozenset[str]] = {
    "child": frozenset(
        {"zite_record_id", "first_name", "primary_email", "potential_duplicate"}
    ),
    "family": frozenset(
        {
            "family_id",
            "zite_record_id",
            "first_name",
            "last_name",
            "primary_email",
            "potential_duplicate",
        }
    ),
    "sponsor": frozenset(
        {
            "sponsor_id",
            "zite_record_id",
            "first_name",
            "last_name",
            "email",
            "potential_duplicate",
        }
    ),
}

#: Control columns kept on ignored rows, per entity.
_IGNORE_RETAINED_CONTROLS: dict[str, tuple[str, ...]] = {
    "child": CHILD_CONTROL_COLUMNS,
    "family": CONTROL_COLUMNS,
    "sponsor": CONTROL_COLUMNS,
}


def reduce_ignored(row: dict[str, Any], entity: str) -> dict[str, Any]:
    """Reduce one adj row to its identity skeleton (§5.5, D7).

    Args:
        row: The dense adj row (already carrying ``ignore`` set).
        entity: "child", "family", or "sponsor".

    Returns:
        The row with every column except the key, ``zite_record_id``,
        names, primary contact email, and the control columns nulled.
    """
    retained = _IGNORE_RETAINED[entity]
    controls = _IGNORE_RETAINED_CONTROLS[entity]
    reduced: dict[str, Any] = {}
    for column in ENTITY_SPECS[entity]["columns"]:
        if column in retained or column in controls:
            reduced[column] = row.get(column)
        else:
            reduced[column] = None
    return reduced


# ---------------------------------------------------------------------------
# Frame helpers.
# ---------------------------------------------------------------------------


def normalize_row(entity: str, row: dict[str, Any]) -> dict[str, Any]:
    """Coerce one adj row's cells to the documented schema types.

    Args:
        entity: "child", "family", or "sponsor".
        row: The raw row dict (any value types).

    Returns:
        The row with every documented column present and typed.
    """
    schema: dict[str, PolarsDataType] = ENTITY_SPECS[entity]["schema"]
    normalized: dict[str, Any] = {}
    for column, dtype in schema.items():
        value = row.get(column)
        if dtype == pl.Boolean:
            normalized[column] = as_bool(value)
        elif dtype == pl.Int64:
            normalized[column] = _as_int(value)
        else:
            normalized[column] = _as_text(value)
    return normalized


def build_adj_frame(entity: str, rows: list[dict[str, Any]]) -> pl.DataFrame:
    """Build one typed adj DataFrame from row dicts (§4.3).

    Args:
        entity: "child", "family", or "sponsor".
        rows: The adj rows (values of any type; normalized here).

    Returns:
        The frame with the documented column order and dtypes.
    """
    schema: dict[str, PolarsDataType] = ENTITY_SPECS[entity]["schema"]
    normalized = [normalize_row(entity, row) for row in rows]
    return pl.DataFrame(normalized, schema=schema)


def project_final(
    adj_frame: pl.DataFrame, final_columns: Sequence[str]
) -> pl.DataFrame:
    """Project one adj frame into its final sheet (§7.1, D2).

    Drops every row with ``ignore == true`` and selects the documented
    final column order.  The input frame must already be typed (built
    via :func:`build_adj_frame`), so the projection keeps adj dtypes.

    Args:
        adj_frame: The typed adj frame.
        final_columns: The documented final-sheet column order.

    Returns:
        The projection frame.
    """
    kept = adj_frame.filter(~pl.col("ignore"))
    return kept.select(list(final_columns))


def project_final_child(adj_frame: pl.DataFrame) -> pl.DataFrame:
    """Project ``adj_child`` into ``final_child`` (§7.1, plus orphan rule).

    Like :func:`project_final` (drop ignored rows, documented column
    order) and additionally drops orphaned children — ``adj_child`` rows
    with a null ``family_id`` (no matching family registration) never
    reach ``final_child``; they stay in ``adj_child`` for the audit tool
    to fix.

    Args:
        adj_frame: The typed adj_child frame.

    Returns:
        The projection frame.
    """
    kept = adj_frame.filter(
        ~pl.col("ignore") & pl.col("family_id").is_not_null()
    )
    return kept.select(list(FINAL_CHILD_COLUMNS))


def read_adj(cfg: Config, entity: str) -> list[dict[str, Any]]:
    """Read one adj sheet as normalized row dicts.

    Args:
        cfg: Pipeline configuration.
        entity: "child", "family", or "sponsor".

    Returns:
        The adj rows; empty list when the sheet is absent.
    """
    frame = read_sheet(cfg.excel_file, ENTITY_SPECS[entity]["sheet"])
    if frame is None:
        return []
    return [normalize_row(entity, row) for row in frame.to_dicts()]


def _upsert_row(
    rows: list[dict[str, Any]], row: dict[str, Any], key: str
) -> None:
    """Insert or replace one adj row in place, keyed by its id column.

    Args:
        rows: The mutable row list.
        row: The row to insert (new key) or replace (existing key).
        key: The entity's key column name.
    """
    for index, existing in enumerate(rows):
        if str(existing.get(key)) == str(row.get(key)):
            rows[index] = row
            return
    rows.append(row)


# ---------------------------------------------------------------------------
# Entries and the aggregate load (§9.2).
# ---------------------------------------------------------------------------


class ControlState(BaseModel):
    """The three human controls shared by every entity (§5.3)."""

    audited: bool = False
    ignore: bool = False
    note: str | None = None


class SponsorEntry(BaseModel):
    """One sponsor as the audit UI sees it (§9.2)."""

    sponsor_id: int
    adj: dict[str, Any]
    baseline: dict[str, Any]
    status: str = STATUS_NEW


class FamilyEntry(BaseModel):
    """One family as the audit UI sees it (§9.2)."""

    family_id: int
    adj: dict[str, Any]
    baseline: dict[str, Any]
    submissions: list[dict[str, Any]] = Field(default_factory=list)
    status: str = STATUS_NEW


class ChildEntry(BaseModel):
    """One child as the audit UI sees it (§9.2)."""

    zite_record_id: str
    family_id: int | None = None
    angel_id: str | None = None
    adj: dict[str, Any]
    baseline: dict[str, Any]
    submissions: list[dict[str, Any]] = Field(default_factory=list)
    status: str = STATUS_NEW
    conflict_note: str | None = None


class AuditStats(BaseModel):
    """Dashboard counts (§8.1)."""

    total: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    children_total: int = 0
    children_by_status: dict[str, int] = Field(default_factory=dict)
    children_needs_clarification: int = 0


class AuditData(BaseModel):
    """Everything one page load needs, built by :func:`load_audit_data`."""

    sponsors: list[SponsorEntry] = Field(default_factory=list)
    families: list[FamilyEntry] = Field(default_factory=list)
    children: list[ChildEntry] = Field(default_factory=list)
    orphans: list[ChildEntry] = Field(default_factory=list)
    sponsor_stats: AuditStats = Field(default_factory=AuditStats)
    family_stats: AuditStats = Field(default_factory=AuditStats)
    child_stats: AuditStats = Field(default_factory=AuditStats)


def _default_row(entity: str, key_value: Any) -> dict[str, Any]:
    """One all-default adj row (controls defaulted, §7.4)."""
    row: dict[str, Any] = {
        column: None for column in ENTITY_SPECS[entity]["columns"]
    }
    row[ENTITY_SPECS[entity]["key"]] = key_value
    row["status"] = STATUS_NEW
    row["audited"] = False
    row["ignore"] = False
    row["potential_duplicate"] = False
    if entity == "child":
        row["screened"] = False
        row["generated_info_sheet"] = False
    return row


def _child_baseline(
    raw_row: dict[str, Any], group: list[dict[str, Any]]
) -> dict[str, Any]:
    """Baseline values for one child (§5.2, §6.2, D8).

    The combined duplicate-group values when submissions exist, else the
    raw row's own values.
    """
    combined = combine_child_group(group) if group else {}
    baseline: dict[str, Any] = {}
    for column in (*CHILD_LINKAGE_COLUMNS, *CHILD_EDITABLE_COLUMNS):
        value = combined.get(column)
        baseline[column] = value if value is not None else raw_row.get(column)
    return baseline


def _raw_columns(entity: str) -> tuple[str, ...]:
    """Linkage + editable columns read from the entity's raw row."""
    if entity == "child":
        return (*CHILD_LINKAGE_COLUMNS, *CHILD_EDITABLE_COLUMNS)
    if entity == "family":
        return (*FAMILY_LINKAGE_COLUMNS, *FAMILY_EDITABLE_COLUMNS)
    return (*SPONSOR_LINKAGE_COLUMNS, *SPONSOR_EDITABLE_COLUMNS)


def load_audit_data(cfg: Config) -> AuditData:
    """Load all entities, groups, orphans, and stats in one read (§9.2).

    Works from the prepared ``{entity}_submissions`` rows (cleaned,
    ID-assigned, written by ``ccat screen``); legacy workbooks whose
    submissions sheets predate that stage fall back to their ``raw_*``
    sheets.  Duplicate-group baselines always come from the
    ``child_submissions`` sheet alone (legacy raw rows are never
    regrouped).

    Args:
        cfg: Pipeline configuration.

    Returns:
        The aggregate :class:`AuditData`.

    Raises:
        AuditError: If no prepared/raw rows exist (run ``ccat acquire``
            and ``ccat screen``).
    """
    sponsors = _entity_rows(cfg, "sponsor")
    families = _entity_rows(cfg, "family")
    children = _entity_rows(cfg, "child")
    if not sponsors and not families and not children:
        raise AuditError(
            "no prepared sponsor/family/child rows;"
            " run `ccat acquire` and `ccat screen` first"
        )
    adj_sponsors = {row["sponsor_id"]: row for row in read_adj(cfg, "sponsor")}
    adj_families = {row["family_id"]: row for row in read_adj(cfg, "family")}
    adj_children = {
        row["zite_record_id"]: row for row in read_adj(cfg, "child")
    }
    family_submission_frame = read_sheet(cfg.excel_file, "family_submissions")
    email_to_submissions: dict[str, list[dict[str, Any]]] = {}
    for row in (
        family_submission_frame.to_dicts()
        if family_submission_frame is not None
        else []
    ):
        email = _as_text(row.get("primary_email"))
        if email is not None:
            email_to_submissions.setdefault(email, []).append(row)
    dob_log = read_sheet(cfg.excel_file, "dob_conflict_log")
    log_rows = dob_log.to_dicts() if dob_log is not None else []

    data = AuditData()
    seen_sponsor_ids: set[int] = set()
    for prepared in sponsors:
        sponsor_id = _as_int(prepared.get("sponsor_id"))
        if sponsor_id is None or sponsor_id in seen_sponsor_ids:
            continue
        seen_sponsor_ids.add(sponsor_id)
        baseline = {
            column: prepared.get(column) for column in _raw_columns("sponsor")
        }
        adj = adj_sponsors.get(sponsor_id) or _default_row(
            "sponsor", sponsor_id
        )
        data.sponsors.append(
            SponsorEntry(
                sponsor_id=sponsor_id,
                adj=adj,
                baseline=baseline,
                status=str(adj.get("status") or STATUS_NEW),
            )
        )
    seen_family_ids: set[int] = set()
    for prepared in families:
        family_id = _as_int(prepared.get("family_id"))
        if family_id is None or family_id in seen_family_ids:
            continue  # duplicate submissions share one family entity
        seen_family_ids.add(family_id)
        baseline = {
            column: prepared.get(column) for column in _raw_columns("family")
        }
        adj = adj_families.get(family_id) or _default_row("family", family_id)
        email = _as_text(prepared.get("primary_email"))
        submissions = email_to_submissions.get(email, [])
        data.families.append(
            FamilyEntry(
                family_id=family_id,
                adj=adj,
                baseline=baseline,
                submissions=submissions,
                status=str(adj.get("status") or STATUS_NEW),
            )
        )
    child_groups = _child_groups(cfg)
    keyed_child_groups: dict[
        tuple[str | None, str | None], list[dict[str, Any]]
    ] = {}
    for values, rows in child_groups.items():
        if len(values) == 2:
            keyed_child_groups[(values[0], values[1])] = rows
    for prepared in children:
        record_id = _as_text(prepared.get("zite_record_id"))
        if record_id is None:
            continue
        group = keyed_child_groups.get(
            (
                _as_text(prepared.get("primary_email")),
                _as_text(prepared.get("first_name")),
            )
        )
        baseline = _child_baseline(prepared, group or [])
        adj = adj_children.get(record_id) or _default_row("child", record_id)
        entry = ChildEntry(
            zite_record_id=record_id,
            family_id=_as_int(prepared.get("family_id")),
            angel_id=_as_text(prepared.get("angel_id")),
            adj=adj,
            baseline=baseline,
            submissions=group or [],
            status=str(adj.get("status") or STATUS_NEW),
            conflict_note=dob_conflict_notes(log_rows, record_id),
        )
        data.children.append(entry)
        if entry.family_id is None:
            data.orphans.append(entry)
    data.sponsor_stats = _stats(
        [entry.status for entry in data.sponsors],
        [entry.status for entry in data.children],
    )
    data.family_stats = _stats(
        [entry.status for entry in data.families],
        [entry.status for entry in data.children],
    )
    data.child_stats = _stats([entry.status for entry in data.children], [])
    return data


def _entity_rows(cfg: Config, entity: str) -> list[dict[str, Any]]:
    """Read one entity's prepared rows (raw rows fill any gaps).

    The ``{entity}_submissions`` rows are the cleaned, ID-assigned
    submissions written by ``ccat screen``.  Legacy workbooks (written
    before that stage prepared submissions) have no such sheet; their
    ``raw_{entity}`` rows -- which carried the same columns under the
    old pipeline -- are used instead.  Rows present only in the raw
    sheet (e.g. a legacy row missing from partial submissions) are
    appended after the prepared rows.

    Args:
        cfg: Pipeline configuration.
        entity: "sponsor", "family", or "child".

    Returns:
        The prepared rows plus any raw-only rows.
    """
    prepared_frame = read_sheet(cfg.excel_file, f"{entity}_submissions")
    raw_frame = read_sheet(cfg.excel_file, f"raw_{entity}")
    prepared = prepared_frame.to_dicts() if prepared_frame is not None else []
    if raw_frame is None:
        return prepared
    known = {
        str(row.get("zite_record_id") or "")
        for row in prepared
        if row.get("zite_record_id") is not None
    }
    extra = [
        row
        for row in raw_frame.to_dicts()
        if str(row.get("zite_record_id") or "") not in known
    ]
    return [*prepared, *extra]


def _child_groups(
    cfg: Config,
) -> dict[tuple[str | None, str | None], list[dict[str, Any]]]:
    """Duplicate-child groups from the ``child_submissions`` sheet (§6).

    Legacy workbooks without that sheet group nothing: their raw rows
    already carry one adjudicated row per child.
    """
    frame = read_sheet(cfg.excel_file, "child_submissions")
    if frame is None:
        return {}
    return {
        values: rows
        for values, rows in group_submissions(frame.to_dicts(), "child").items()
        if len(values) == 2
    }


def _stats(statuses: list[str], child_statuses: list[str]) -> AuditStats:
    """Tally statuses for the dashboard (§8.1)."""
    by_status = {status: 0 for status in STATUS_ORDER}
    for status in statuses:
        by_status[status] = by_status.get(status, 0) + 1
    children_by_status = {status: 0 for status in STATUS_ORDER}
    for status in child_statuses:
        children_by_status[status] = children_by_status.get(status, 0) + 1
    return AuditStats(
        total=len(statuses),
        by_status=by_status,
        children_total=len(child_statuses),
        children_by_status=children_by_status,
        children_needs_clarification=children_by_status.get(
            STATUS_NEEDS_CLARIFICATION, 0
        ),
    )


# ---------------------------------------------------------------------------
# Seeding (§7.4).
# ---------------------------------------------------------------------------


def _seed_child_row(
    raw: dict[str, Any], baseline: dict[str, Any], cfg: Config
) -> dict[str, Any]:
    """Build one seeded adj_child row from its baseline (§7.4).

    Orphaned children (``family_id`` null — no matching family
    registration) get the ``ORPHAN_NOTE`` clarification note, so they
    show as ``needs_clarification`` in the audit tool immediately.
    """
    row = _default_row("child", raw.get("zite_record_id"))
    for column in (*CHILD_LINKAGE_COLUMNS, *CHILD_EDITABLE_COLUMNS):
        row[column] = baseline.get(column, raw.get(column))
    row["age_at_xmas"] = _computed_age(row.get("dob"), cfg)
    if row.get("family_id") is None:
        row["require_clarification"] = ORPHAN_NOTE
    return row


def _seed_family_row(raw: dict[str, Any]) -> dict[str, Any]:
    """Build one seeded adj_family row from the coalesced survivor (§7.4)."""
    row = _default_row("family", _as_int(raw.get("family_id")))
    for column in (*FAMILY_LINKAGE_COLUMNS, *FAMILY_EDITABLE_COLUMNS):
        row[column] = raw.get(column)
    return row


def _seed_sponsor_row(raw: dict[str, Any]) -> dict[str, Any]:
    """Build one seeded adj_sponsor row from its raw row (§7.4)."""
    row = _default_row("sponsor", _as_int(raw.get("sponsor_id")))
    for column in (*SPONSOR_LINKAGE_COLUMNS, *SPONSOR_EDITABLE_COLUMNS):
        row[column] = raw.get(column)
    bounds = AGE_PREF_BOUNDS.get(str(raw.get("age_preference") or ""))
    if bounds is not None:
        row["pref_age_min"], row["pref_age_max"] = bounds
    return row


def _computed_age(dob: Any, cfg: Config) -> int | None:
    """Compute ``age_at_xmas`` from a (possibly corrected) dob (D13).

    Args:
        dob: The adjudicated dob (ISO string or None).
        cfg: Pipeline configuration (Christmas date).

    Returns:
        The completed-years age, or None when dob is absent/unparsable.
    """
    text = _as_text(dob)
    if text is None:
        return None
    try:
        return compute_age_at_xmas(text, cfg.xmas)
    except ValueError:
        return None


def ensure_adj_sheets(cfg: Config) -> None:
    """Seed adj rows for every prepared entity missing one (§7.4, idempotent).

    ``ccat screen`` seeds the adj sheets itself now (§7.3); this
    backstop only fills rows missing from the workbook -- e.g. legacy
    workbooks that were never re-screened.  Every prepared entity
    without an adj row gets a dense row seeded from its prepared row
    (§5.2/§6), computed columns filled, controls defaulted.
    ``final_child`` is re-projected afterwards (with ``xmas``, §7.2).

    Args:
        cfg: Pipeline configuration.

    Raises:
        AuditError: If no prepared rows exist, or the workbook's
            ``adj_child`` still has the pre-v2 sparse schema (a legacy
            workbook the v2 audit tool must never write; bootstrap the
            v2 workbook with ``ccat audit-migrate`` instead, §10.1/§11).
    """
    with _WRITE_LOCK:
        existing_adj = read_sheet(cfg.excel_file, "adj_child")
        if existing_adj is not None and "screened" not in existing_adj.columns:
            raise AuditError(
                f"adj_child in {cfg.excel_file} has the pre-v2 sparse"
                " schema; this workbook predates the v2 audit system and"
                " must never be written by it. Bootstrap the v2 workbook"
                " with `ccat audit-migrate --config"
                " config/ccat_2026_v2.json --from <old workbook>`"
                " (audit_architecture.md §10.1)"
            )
        prepared_sponsors = _entity_rows(cfg, "sponsor")
        prepared_families = _entity_rows(cfg, "family")
        prepared_children = _entity_rows(cfg, "child")
        if (
            not prepared_sponsors
            and not prepared_families
            and not prepared_children
        ):
            raise AuditError(
                "no prepared sponsor/family/child rows;"
                " run `ccat acquire` and `ccat screen` first"
            )
        adj_children = read_adj(cfg, "child")
        adj_families = read_adj(cfg, "family")
        adj_sponsors = read_adj(cfg, "sponsor")
        known_children = {row["zite_record_id"] for row in adj_children}
        known_families = {row["family_id"] for row in adj_families}
        known_sponsors = {row["sponsor_id"] for row in adj_sponsors}

        child_groups = _child_groups(cfg)
        added_children = 0
        for prepared in prepared_children:
            record_id = _as_text(prepared.get("zite_record_id"))
            if record_id is None or record_id in known_children:
                continue
            group = child_groups.get(
                (
                    _as_text(prepared.get("primary_email")),
                    _as_text(prepared.get("first_name")),
                )
            )
            baseline = _child_baseline(prepared, group or [])
            adj_children.append(_seed_child_row(prepared, baseline, cfg))
            added_children += 1
        added_families = 0
        for prepared in prepared_families:
            family_id = _as_int(prepared.get("family_id"))
            if family_id is None or family_id in known_families:
                continue
            adj_families.append(_seed_family_row(prepared))
            known_families.add(family_id)
            added_families += 1
        added_sponsors = 0
        for prepared in prepared_sponsors:
            sponsor_id = _as_int(prepared.get("sponsor_id"))
            if sponsor_id is None or sponsor_id in known_sponsors:
                continue
            adj_sponsors.append(_seed_sponsor_row(prepared))
            known_sponsors.add(sponsor_id)
            added_sponsors += 1

        sheets: dict[str, pl.DataFrame] = {}
        if added_children:
            sheets["adj_child"] = build_adj_frame("child", adj_children)
        if added_families:
            sheets["adj_family"] = build_adj_frame("family", adj_families)
        if added_sponsors:
            sheets["adj_sponsor"] = build_adj_frame("sponsor", adj_sponsors)
        if added_children or added_families or added_sponsors:
            logger.info(
                "seeded adj rows: %d children, %d families, %d sponsors",
                added_children,
                added_families,
                added_sponsors,
            )
        # final_child is always re-projected at startup (§7.4), with the
        # adj sheets in the same atomic write when any were seeded.
        # Orphaned children (family_id null) never reach final_child.
        sheets["final_child"] = project_final_child(
            build_adj_frame("child", adj_children)
        )
        write_sheets(cfg.excel_file, "audit", sheets, xmas=cfg.xmas)


def _project_final_child(cfg: Config, adj_rows: list[dict[str, Any]]) -> None:
    """Re-project ``final_child`` from adj rows (§7.2, with ``xmas``)."""
    frame = project_final_child(build_adj_frame("child", adj_rows))
    write_sheets(cfg.excel_file, "audit", {"final_child": frame}, xmas=cfg.xmas)


# ---------------------------------------------------------------------------
# Saves (§9.2).
# ---------------------------------------------------------------------------


def _save_row(
    cfg: Config,
    entity: str,
    key_value: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Shared dense upsert for one entity's save (§9.2).

    Args:
        cfg: Pipeline configuration.
        entity: "child", "family", or "sponsor".
        key_value: The row's key (zite id / family_id / sponsor_id).
        payload: Edited data columns plus ``audited``/``ignore``/
            ``require_clarification`` controls.

    Returns:
        The persisted row.

    Raises:
        AuditError: On unknown keys, unknown entities, or invalid
            control combinations.
    """
    spec = ENTITY_SPECS[entity]
    key = spec["key"]
    unknown = [
        column for column in payload if column not in set(spec["save_fields"])
    ]
    if unknown:
        raise AuditError(f"unknown {entity} fields: {sorted(unknown)}")
    raw = _find_raw_row(cfg, entity, key_value)
    if raw is None:
        raise AuditError(f"unknown {entity} {key_value!r}")
    with _WRITE_LOCK:
        rows = read_adj(cfg, entity)
        existing = next(
            (row for row in rows if str(row.get(key)) == str(key_value)), None
        )
        ignore_now = as_bool(payload.get("ignore"))
        ignore_before = as_bool(existing.get("ignore")) if existing else False
        baseline = _entity_baseline(cfg, entity, raw)

        if ignore_now or (ignore_before and not ignore_now):
            # Ignore, or un-ignore: either way the data columns restart
            # from the baseline (§5.5); ignore then nulls the skeleton.
            row = _seed_row_from_baseline(entity, raw, baseline, cfg)
        else:
            row = existing or _seed_row_from_baseline(
                entity, raw, baseline, cfg
            )
            for column in spec["editable"]:
                if column in payload:
                    row[column] = payload[column]
        # Linkage always follows the raw record (read-only in the UI).
        for column in _linkage_columns(entity):
            row[column] = raw.get(column)
        row[key] = key_value
        # Controls.
        row["audited"] = as_bool(payload.get("audited"))
        row["ignore"] = ignore_now
        note = _as_text(payload.get("require_clarification"))
        row["require_clarification"] = note
        row["audited_at"] = datetime.now(UTC).isoformat()
        # Entity-specific computed columns.
        if entity == "child":
            row["age_at_xmas"] = _computed_age(row.get("dob"), cfg)
        elif entity == "sponsor":
            bounds = AGE_PREF_BOUNDS.get(str(row.get("age_preference") or ""))
            row["pref_age_min"] = bounds[0] if bounds else None
            row["pref_age_max"] = bounds[1] if bounds else None
        # Module-owned columns are always preserved (§4.4).
        if existing is not None:
            for column in _module_owned_columns(entity):
                row[column] = existing.get(column)
        if ignore_now:
            row = reduce_ignored(row, entity)
            row["status"] = STATUS_IGNORED
        else:
            if entity == "child":
                log_frame = read_sheet(cfg.excel_file, "dob_conflict_log")
                log_rows = log_frame.to_dicts() if log_frame is not None else []
                row["potential_anomoly"] = merge_anomoly(
                    row.get("potential_anomoly"),
                    dob_conflict_notes(log_rows, str(key_value)),
                )
            row["status"] = status_of(row, baseline, entity)
        _upsert_row(rows, row, key)
        sheets: dict[str, pl.DataFrame] = {
            spec["sheet"]: build_adj_frame(entity, rows)
        }
        if entity == "child":
            sheets["final_child"] = project_final_child(
                build_adj_frame("child", rows)
            )
        write_sheets(
            cfg.excel_file,
            "audit",
            sheets,
            xmas=cfg.xmas if "final_child" in sheets else None,
        )
    return row


def _linkage_columns(entity: str) -> tuple[str, ...]:
    """Return the entity's acquire-owned, read-only linkage columns."""
    if entity == "child":
        return CHILD_LINKAGE_COLUMNS
    if entity == "family":
        return FAMILY_LINKAGE_COLUMNS
    return SPONSOR_LINKAGE_COLUMNS


def _module_owned_columns(entity: str) -> tuple[str, ...]:
    """Columns owned by other modules and preserved on every save (§4.4)."""
    if entity == "child":
        return (*CHILD_MODULE_COLUMNS, "screened", "potential_duplicate")
    if entity == "family":
        return (*FAMILY_ASSIGNMENT_COLUMNS, "potential_duplicate")
    return (*SPONSOR_ASSIGNMENT_COLUMNS, "potential_duplicate")


def _seed_row_from_baseline(
    entity: str, raw: dict[str, Any], baseline: dict[str, Any], cfg: Config
) -> dict[str, Any]:
    """One default row seeded from the entity's baseline values."""
    row = _default_row(entity, raw.get(ENTITY_SPECS[entity]["key"]))
    for column in _raw_columns(entity):
        row[column] = baseline.get(column, raw.get(column))
    if entity == "child":
        row["age_at_xmas"] = _computed_age(row.get("dob"), cfg)
    elif entity == "sponsor":
        bounds = AGE_PREF_BOUNDS.get(str(row.get("age_preference") or ""))
        if bounds is not None:
            row["pref_age_min"], row["pref_age_max"] = bounds
    return row


def _entity_baseline(
    cfg: Config, entity: str, raw: dict[str, Any]
) -> dict[str, Any]:
    """Return the entity's baseline (§5.2): prepared row, or combined child group."""
    if entity != "child":
        return {column: raw.get(column) for column in _raw_columns(entity)}
    group = _child_groups(cfg).get(
        (_as_text(raw.get("primary_email")), _as_text(raw.get("first_name")))
    )
    return _child_baseline(raw, group or [])


def _find_raw_row(
    cfg: Config, entity: str, key_value: Any
) -> dict[str, Any] | None:
    """Find one entity's prepared row by key.

    Searches the prepared ``{entity}_submissions`` rows (legacy
    workbooks fall back to their ``raw_{entity}`` sheet), keeping the
    first matching submission (the survivor: prepared rows are sorted
    by created ascending).
    """
    key = ENTITY_SPECS[entity]["key"]
    for row in _entity_rows(cfg, entity):
        if str(row.get(key)) == str(key_value):
            return row
    return None


def save_child(
    cfg: Config, zite_record_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Save one child's dense adj row and re-project final_child (§9.2).

    Args:
        cfg: Pipeline configuration.
        zite_record_id: The child's join key.
        payload: Edited data columns plus the three controls.

    Returns:
        The persisted row.

    Raises:
        AuditError: On unknown keys, an unknown child, or attempting to
            mark an orphaned child audited (§8.7).
    """
    raw = _find_raw_row(cfg, "child", zite_record_id)
    if (
        raw is not None
        and raw.get("angel_id") is None
        and as_bool(payload.get("audited"))
    ):
        raise AuditError(
            "orphaned child cannot be marked audited before a matching"
            " family registration exists (§8.7)"
        )
    return _save_row(cfg, "child", zite_record_id, payload)


def save_family(
    cfg: Config, family_id: int, payload: dict[str, Any]
) -> dict[str, Any]:
    """Save one family's dense adj row (assignment columns preserved).

    Args:
        cfg: Pipeline configuration.
        family_id: The family's key.
        payload: Edited data columns plus the three controls.

    Returns:
        The persisted row.

    Raises:
        AuditError: On unknown keys or an unknown family.
    """
    return _save_row(cfg, "family", family_id, payload)


def save_sponsor(
    cfg: Config, sponsor_id: int, payload: dict[str, Any]
) -> dict[str, Any]:
    """Save one sponsor's dense adj row (assignment columns preserved).

    Args:
        cfg: Pipeline configuration.
        sponsor_id: The sponsor's key.
        payload: Edited data columns plus the three controls.

    Returns:
        The persisted row.

    Raises:
        AuditError: On unknown keys or an unknown sponsor.
    """
    return _save_row(cfg, "sponsor", sponsor_id, payload)
