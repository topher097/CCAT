"""Persistent entity store shared by screening, audit, match, and generate.

The three ``final_*`` sheets are the complete, editable system of record.
Every row retains its full schema and controls; ignored, orphaned, duplicate,
and raw-missing entities are never removed by ordinary reruns. Output
eligibility is an explicit reader filter, not a destructive projection.

Human edits and module-owned values survive deliberate field-scoped writes.
Age formulas, atomic replacement, and sheet ownership remain in ``ccat.io``.
The store holds no UI or transport code, keeping it unit-testable.
"""

import logging
import threading
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

import polars as pl
from polars._typing import PolarsDataType
from pydantic import BaseModel, Field

from ccat.audit import AuditError
from ccat.config import Config
from ccat.domain import AGE_PREF_BOUNDS, _created_value, compute_age_at_xmas
from ccat.io import list_sheets, read_sheet, write_sheets
from ccat.preparation import (
    Preparation,
    append_note,
    dob_notes_by_child,
    duplicate_note,
    prepare_workbook,
)

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

#: Common control columns on all three final sheets.
CONTROL_COLUMNS = (
    "status",
    "ignore",
    "clarification_notes",
    "audited_at",
)

#: The child's own assignment columns — the canonical assignment grain.
#: The family and sponsor assignment columns are derived from them.
CHILD_ASSIGNMENT_COLUMNS = (
    "assigned_sponsor_id",
    "assignment_source",
)

#: Columns a pre-migration workbook may lack.  They are read as NULL
#: instead of failing the exact-schema check, so an older workbook still
#: imports.
OPTIONAL_FINAL_COLUMNS: dict[str, tuple[str, ...]] = {
    "child": CHILD_ASSIGNMENT_COLUMNS,
    "sponsor": (
        "elevated_budget",
        "physical_packet",
        "packets_dispatched_at",
    ),
}

#: Pre-filled clarification note for orphaned children (§8.7).
ORPHAN_NOTE = "No matching family registration for this child's primary email"

#: Pre-filled clarification note for a sponsor whose Assigned Angel ID
#: names no child: the claim cannot be honoured, so the audit fixes it.
ANGEL_CLAIM_NOTE = "Assigned angel ID {angel_id} names no child"

#: Prefix of the sibling-DOB-conflict note merged into
#: ``potential_anomoly`` (moved from the old audit server).
DOB_CONFLICT_PREFIX = "Sibling DOB conflict"


# ---------------------------------------------------------------------------
# Sheet schemas (§4.3) — the single source of truth for adj/final columns.
# ---------------------------------------------------------------------------

FINAL_CHILD_COLUMNS: tuple[str, ...] = (
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
    *CHILD_ASSIGNMENT_COLUMNS,
    *CONTROL_COLUMNS,
)

FINAL_CHILD_SCHEMA: dict[str, PolarsDataType] = {
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
    "assigned_sponsor_id": pl.Int64,
    "assignment_source": pl.String,
    "status": pl.String,
    "ignore": pl.Boolean,
    "clarification_notes": pl.String,
    "audited_at": pl.String,
}

#: final_child column partitions.
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
    "status",
    "ignore",
    "clarification_notes",
)

FINAL_FAMILY_COLUMNS: tuple[str, ...] = (
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

FINAL_FAMILY_SCHEMA: dict[str, PolarsDataType] = {
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
    "ignore": pl.Boolean,
    "clarification_notes": pl.String,
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
    "status",
    "ignore",
    "clarification_notes",
)

FINAL_SPONSOR_COLUMNS: tuple[str, ...] = (
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
    "elevated_budget",
    "physical_packet",
    "packets_dispatched_at",
    *CONTROL_COLUMNS,
)

FINAL_SPONSOR_SCHEMA: dict[str, PolarsDataType] = {
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
    "elevated_budget": pl.Boolean,
    "physical_packet": pl.Boolean,
    "packets_dispatched_at": pl.String,
    "status": pl.String,
    "ignore": pl.Boolean,
    "clarification_notes": pl.String,
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
    "elevated_budget",
    "physical_packet",
)
#: Owned by the manual matcher's dispatch; audit saves preserve it.
SPONSOR_DISPATCH_COLUMNS = ("packets_dispatched_at",)
SPONSOR_COMPUTED_COLUMNS = ("pref_age_min", "pref_age_max")
SPONSOR_ASSIGNMENT_COLUMNS = ("assigned_angel_ids", "num_angels_assigned")
SPONSOR_SAVE_FIELDS = (
    *SPONSOR_EDITABLE_COLUMNS,
    "status",
    "ignore",
    "clarification_notes",
)

#: Entity registry: name -> (sheet, key column, columns, schema, editable).
ENTITY_SPECS: dict[str, dict[str, Any]] = {
    "child": {
        "sheet": "final_child",
        "key": "zite_record_id",
        "columns": FINAL_CHILD_COLUMNS,
        "schema": FINAL_CHILD_SCHEMA,
        "editable": CHILD_EDITABLE_COLUMNS,
        "save_fields": CHILD_SAVE_FIELDS,
        "status_editable": tuple(
            column
            for column in CHILD_EDITABLE_COLUMNS
            if column != "special_needs"
        ),
    },
    "family": {
        "sheet": "final_family",
        "key": "family_id",
        "columns": FINAL_FAMILY_COLUMNS,
        "schema": FINAL_FAMILY_SCHEMA,
        "editable": FAMILY_EDITABLE_COLUMNS,
        "save_fields": FAMILY_SAVE_FIELDS,
        "status_editable": FAMILY_EDITABLE_COLUMNS,
    },
    "sponsor": {
        "sheet": "final_sponsor",
        "key": "sponsor_id",
        "columns": FINAL_SPONSOR_COLUMNS,
        "schema": FINAL_SPONSOR_SCHEMA,
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
    """Coerce a sheet value (bool, number, or string) to bool.

    Tolerates round-trips.

    Args:
        value: Raw cell value (may be None, bool, "true"/"false" text, or
            a numeric 1/0).

    Returns:
        The boolean value; None and unrecognized text are False.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, int | float):
        return value != 0
    return str(value).strip().lower() in {"true", "1"}


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
    has_edits: bool,
    has_note: bool,
    audited: bool = False,
    ignore: bool = False,
) -> str:
    """Derive one entity's status (§5.3).

    Args:
        has_edits: Any adjudicable data column differs from the baseline.
        has_note: The clarification note is non-empty.
        audited: Optional compatibility input from a converted Boolean;
            normal persisted rows derive this from ``status``.
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
    """Compute one final row's status, using status as audit authority.

    Precedence is the persisted rule (decision D11): a truthy
    ``ignore`` returns ``ignored`` before the stored-status audited
    check, so a row just marked ignored shows ``ignored`` live as
    well — previously a sticky stored ``audited`` could outrank a
    newly selected ignore flag while saves always forced ``ignored``.
    """
    if as_bool(row.get("ignore")):
        return STATUS_IGNORED
    if _as_text(row.get("status")) == STATUS_AUDITED:
        return STATUS_AUDITED
    editable = ENTITY_SPECS[entity]["status_editable"]
    has_edits = any(
        _values_differ(row.get(column), baseline.get(column))
        for column in editable
    )
    has_note = _as_text(row.get("clarification_notes")) is not None
    return compute_status(
        has_edits,
        has_note,
        ignore=as_bool(row.get("ignore")),
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
        {
            "zite_record_id",
            "family_id",
            "angel_id",
            "first_name",
            "primary_email",
            "potential_duplicate",
        }
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
    """Reduce one final row to its identity skeleton.

    Args:
        row: The full final row (already carrying ``ignore`` set).
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
    """Coerce one final row's cells to the documented schema types.

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


def _entity_for_sheet(sheet: str) -> str:
    """Resolve an active final sheet name to its entity name."""
    for entity, spec in ENTITY_SPECS.items():
        if spec["sheet"] == sheet:
            return entity
    raise AuditError(f"unknown persistent entity sheet {sheet!r}")


def _normalized_key(entity: str, value: Any) -> str | None:
    """Return one normalized, comparable entity key."""
    if entity in {"family", "sponsor"}:
        number = _as_int(value)
        return str(number) if number is not None else None
    return _as_text(value)


def _validate_entity_rows(entity: str, rows: list[dict[str, Any]]) -> None:
    """Reject null, duplicate, or silently coercible persistent identities."""
    key = ENTITY_SPECS[entity]["key"]
    for row in rows:
        value = row.get(key)
        if entity in {"family", "sponsor"} and value is not None:
            if isinstance(value, bool):
                raise AuditError(
                    f"final_{entity} contains boolean {key} {value!r};"
                    " stable IDs must remain integers"
                )
            if isinstance(value, float) and not value.is_integer():
                raise AuditError(
                    f"final_{entity} contains non-integral {key} {value!r};"
                    " stable IDs must not be truncated"
                )
            if isinstance(value, str):
                text = value.strip()
                try:
                    numeric = float(text)
                except ValueError:
                    numeric = None
                if numeric is not None and not numeric.is_integer():
                    raise AuditError(
                        f"final_{entity} contains non-integral {key}"
                        f" {value!r}; stable IDs must not be truncated"
                    )
    keys = [_normalized_key(entity, row.get(key)) for row in rows]
    if any(value is None for value in keys):
        raise AuditError(f"final_{entity} contains a missing {key} key")
    present = [value for value in keys if value is not None]
    if len(present) != len(set(present)):
        raise AuditError(f"final_{entity} contains duplicate {key} keys")


def validate_final_frame(sheet: str, frame: pl.DataFrame) -> None:
    """Validate a complete persistent-final frame before any replacement.

    Args:
        sheet: Active ``final_*`` sheet name.
        frame: Candidate full-store frame.

    Raises:
        AuditError: If columns are missing/extra or entity keys are invalid.
    """
    entity = _entity_for_sheet(sheet)
    expected = list(ENTITY_SPECS[entity]["columns"])
    missing = [column for column in expected if column not in frame.columns]
    extra = [column for column in frame.columns if column not in expected]
    if missing or extra:
        raise AuditError(
            f"{sheet} does not have the persistent full schema;"
            f" missing={missing}, extra={extra}. Convert legacy projected"
            " finals with `ccat consolidate-migrate`; never seed over them"
        )
    _validate_entity_rows(entity, frame.to_dicts())


def build_final_frame(entity: str, rows: list[dict[str, Any]]) -> pl.DataFrame:
    """Build and validate one typed persistent-final DataFrame.

    Args:
        entity: "child", "family", or "sponsor".
        rows: Full entity rows; values are normalized here.

    Returns:
        A frame with the complete schema, column order, and dtypes.
    """
    schema: dict[str, PolarsDataType] = ENTITY_SPECS[entity]["schema"]
    _validate_entity_rows(entity, rows)
    normalized = [normalize_row(entity, row) for row in rows]
    _validate_entity_rows(entity, normalized)
    frame = pl.DataFrame(normalized, schema=schema).select(
        list(ENTITY_SPECS[entity]["columns"])
    )
    validate_final_frame(ENTITY_SPECS[entity]["sheet"], frame)
    return frame


def _with_optional_columns(entity: str, frame: pl.DataFrame) -> pl.DataFrame:
    """Add the columns a pre-migration workbook lacks, as NULL.

    Args:
        entity: The entity name.
        frame: The sheet as read.

    Returns:
        The frame with every optional column present.
    """
    optional = OPTIONAL_FINAL_COLUMNS.get(entity, ())
    missing = [column for column in optional if column not in frame.columns]
    if not missing:
        return frame
    schema = ENTITY_SPECS[entity]["schema"]
    return frame.with_columns(
        [pl.lit(None, dtype=schema[column]).alias(column) for column in missing]
    )


def read_final(
    cfg: Config,
    entity: str,
    *,
    workbook: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Read one full persistent entity store as normalized row dictionaries.

    Args:
        cfg: Pipeline configuration.
        entity: "child", "family", or "sponsor".
        workbook: Optional explicit workbook path, used by conversion checks.

    Returns:
        Every stored row, including ignored/orphan/raw-missing entities.

    Raises:
        AuditError: If a projected legacy final, sparse legacy workbook, or
            malformed full store is encountered.
    """
    path = workbook or cfg.excel_file
    sheet = ENTITY_SPECS[entity]["sheet"]
    frame = read_sheet(path, sheet)
    if frame is None:
        if f"adj_{entity}" in list_sheets(path):
            raise AuditError(
                f"{path} contains adj_{entity} without {sheet}; this legacy"
                " workbook requires `ccat consolidate-migrate` before use"
            )
        return []
    validate_final_frame(sheet, _with_optional_columns(entity, frame))
    rows = [normalize_row(entity, row) for row in frame.to_dicts()]
    _validate_entity_rows(entity, rows)
    return rows


def read_final_frame(cfg: Config, entity: str) -> pl.DataFrame:
    """Read one complete persistent store as its typed full frame."""
    return build_final_frame(entity, read_final(cfg, entity))


def eligible_output_rows(
    rows: list[dict[str, Any]], entity: str
) -> list[dict[str, Any]]:
    """Apply the population filter of the retired projected final sheets.

    Families and sponsors exclude ignored rows. Children additionally
    exclude orphans (null ``family_id``). Other gates, such as audited
    status, sponsor assignment, or angel linkage, remain consumer-specific.
    """
    return [
        row
        for row in rows
        if not as_bool(row.get("ignore"))
        and (entity != "child" or row.get("family_id") is not None)
    ]


def read_eligible_final(cfg: Config, entity: str) -> list[dict[str, Any]]:
    """Read only rows eligible for downstream output consumers."""
    return eligible_output_rows(read_final(cfg, entity), entity)


def read_eligible_final_frame(cfg: Config, entity: str) -> pl.DataFrame:
    """Read the legacy-projection-equivalent output population as a frame."""
    return build_final_frame(entity, read_eligible_final(cfg, entity))


def merge_owned_fields(
    stored_rows: list[dict[str, Any]],
    updated_rows: list[dict[str, Any]],
    entity: str,
    owned_columns: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Patch owned columns onto complete rows without dropping other state.

    Args:
        stored_rows: Complete current persistent store.
        updated_rows: Rows containing new module-owned values.
        entity: Entity whose key joins the two collections.
        owned_columns: Fields that may change; all others come from storage.

    Returns:
        Complete stored rows with only the declared fields patched.
    """
    key = ENTITY_SPECS[entity]["key"]
    updates = {
        _normalized_key(entity, row.get(key)): row for row in updated_rows
    }
    merged: list[dict[str, Any]] = []
    for stored in stored_rows:
        row = dict(stored)
        updated = updates.get(_normalized_key(entity, row.get(key)))
        if updated is not None:
            for column in owned_columns:
                row[column] = updated.get(column)
        merged.append(row)
    _validate_entity_rows(entity, merged)
    return merged


def write_final_rows(
    cfg: Config,
    module: str,
    entity: str,
    rows: list[dict[str, Any]],
) -> None:
    """Atomically write one complete persistent store with formula context."""
    sheet = ENTITY_SPECS[entity]["sheet"]
    write_sheets(
        cfg.excel_file,
        module,
        {sheet: build_final_frame(entity, rows)},
        xmas=cfg.xmas if entity == "child" else None,
    )


def _upsert_row(
    rows: list[dict[str, Any]], row: dict[str, Any], key: str
) -> None:
    """Insert or replace one final row in place, keyed by its id column.

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
    """Human-controlled final state shared by every entity."""

    status: str = STATUS_NEW
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
    """One all-default final row (controls defaulted, §7.4)."""
    row: dict[str, Any] = {
        column: None for column in ENTITY_SPECS[entity]["columns"]
    }
    row[ENTITY_SPECS[entity]["key"]] = key_value
    row["status"] = STATUS_NEW
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
    """Load the full final stores with current in-memory raw baselines.

    Current prepared submissions provide duplicate comparisons. Entities
    absent from current raw remain visible using their stored final values
    as the baseline, so raw-missing human work is neither lost nor invented.

    Args:
        cfg: Pipeline configuration.

    Returns:
        Every persistent entity plus family/child groups and dashboard stats.

    Raises:
        AuditError: If neither prepared nor persistent entities exist.
    """
    preparation = prepare_persistent_store(cfg, report=False)
    final_rows = {
        entity: read_final(cfg, entity)
        for entity in ("sponsor", "family", "child")
    }
    if not any(preparation.rows.values()) and not any(final_rows.values()):
        raise AuditError(
            "no prepared sponsor/family/child rows;"
            " run `ccat acquire` and `ccat screen` first"
        )
    prepared_groups = {
        entity: _prepared_groups(preparation, entity)
        for entity in ("sponsor", "family", "child")
    }
    data = AuditData()

    sponsor_final = {
        _normalized_key("sponsor", row.get("sponsor_id")): row
        for row in final_rows["sponsor"]
    }
    sponsor_tokens = sorted(
        set(sponsor_final) | set(prepared_groups["sponsor"]),
        key=lambda value: int(value or 0),
    )
    for token in sponsor_tokens:
        group = prepared_groups["sponsor"].get(token, [])
        prepared = group[0] if group else sponsor_final[token]
        stored = sponsor_final.get(token)
        row = stored or _seed_sponsor_row(prepared)
        baseline = {
            column: prepared.get(column) for column in _raw_columns("sponsor")
        }
        data.sponsors.append(
            SponsorEntry(
                sponsor_id=int(token or 0),
                adj=row,
                baseline=baseline,
                status=str(row.get("status") or STATUS_NEW),
            )
        )

    family_final = {
        _normalized_key("family", row.get("family_id")): row
        for row in final_rows["family"]
    }
    family_tokens = sorted(
        set(family_final) | set(prepared_groups["family"]),
        key=lambda value: int(value or 0),
    )
    for token in family_tokens:
        group = prepared_groups["family"].get(token, [])
        prepared = _family_survivor(group) if group else family_final[token]
        stored = family_final.get(token)
        row = stored or _seed_family_row(prepared)
        baseline = {
            column: prepared.get(column) for column in _raw_columns("family")
        }
        data.families.append(
            FamilyEntry(
                family_id=int(token or 0),
                adj=row,
                baseline=baseline,
                submissions=group,
                status=str(row.get("status") or STATUS_NEW),
            )
        )

    child_final = {
        _normalized_key("child", row.get("zite_record_id")): row
        for row in final_rows["child"]
    }
    child_tokens = [
        token for token in prepared_groups["child"] if token is not None
    ] + [
        token
        for token in child_final
        if token not in prepared_groups["child"] and token is not None
    ]
    groups = _child_groups(cfg, preparation)
    for record_id in child_tokens:
        if record_id is None:
            continue
        group = prepared_groups["child"].get(record_id, [])
        prepared = group[0] if group else child_final[record_id]
        duplicate_group = (
            groups.get(
                (
                    _as_text(prepared.get("primary_email")),
                    _as_text(prepared.get("first_name")),
                ),
                [],
            )
            if group
            else []
        )
        baseline = _child_baseline(prepared, duplicate_group)
        stored = child_final.get(record_id)
        row = stored or _seed_child_row(prepared, baseline, cfg)
        entry = ChildEntry(
            zite_record_id=record_id,
            family_id=_as_int(row.get("family_id")),
            angel_id=_as_text(row.get("angel_id")),
            adj=row,
            baseline=baseline,
            submissions=duplicate_group,
            status=str(row.get("status") or STATUS_NEW),
            conflict_note=dob_conflict_notes(preparation.dob_log, record_id),
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
    """Return freshly prepared raw rows with stable persistent identities."""
    return prepare_persistent_store(cfg, report=False).rows[entity]


def _child_groups(
    cfg: Config,
    preparation: Preparation | None = None,
) -> dict[tuple[str | None, str | None], list[dict[str, Any]]]:
    """Return duplicate-child comparison groups from in-memory preparation."""
    current = preparation or prepare_persistent_store(cfg, report=False)
    return {
        values: rows
        for values, rows in group_submissions(
            current.rows["child"], "child"
        ).items()
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
    """Build one seeded final-child row from its baseline (§7.4).

    Orphaned children (``family_id`` null — no matching family
    registration) get the ``ORPHAN_NOTE`` clarification note, so they
    show as ``needs_clarification`` in the audit tool immediately.
    """
    row = _default_row("child", raw.get("zite_record_id"))
    for column in (*CHILD_LINKAGE_COLUMNS, *CHILD_EDITABLE_COLUMNS):
        row[column] = baseline.get(column, raw.get(column))
    row["age_at_xmas"] = _computed_age(row.get("dob"), cfg)
    if row.get("family_id") is None:
        row["clarification_notes"] = ORPHAN_NOTE
    return row


def _seed_family_row(raw: dict[str, Any]) -> dict[str, Any]:
    """Build one seeded final-family row from the stable survivor."""
    row = _default_row("family", _as_int(raw.get("family_id")))
    for column in (*FAMILY_LINKAGE_COLUMNS, *FAMILY_EDITABLE_COLUMNS):
        row[column] = raw.get(column)
    return row


def _seed_sponsor_row(raw: dict[str, Any]) -> dict[str, Any]:
    """Build one seeded final-sponsor row from its prepared row."""
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


def _apply_diagnostics(
    row: dict[str, Any],
    duplicate_text: str | None,
    validation_notes: list[str],
) -> None:
    """Merge global duplicate/validation diagnostics without replacing text."""
    row["potential_duplicate"] = bool(
        as_bool(row.get("potential_duplicate")) or duplicate_text is not None
    )
    note = "; ".join([*validation_notes, duplicate_text or ""]).strip("; ")
    row["clarification_notes"] = append_note(
        row.get("clarification_notes"), note or None
    )


def _refresh_metadata(
    existing: dict[str, Any], prepared: dict[str, Any], entity: str
) -> dict[str, Any]:
    """Copy the survivor's complete source-metadata bundle.

    Args:
        existing: Current persistent row.
        prepared: The selected survivor submission.
        entity: "child", "family", or "sponsor".

    Returns:
        The row with stable entity key and one consistent source bundle.
    """
    row = dict(existing)
    if as_bool(row.get("ignore")):
        return row
    # Preserve the entity key, but copy every other linkage field -- including
    # family zite_record_id -- so identity and metadata describe one source.
    excluded = {ENTITY_SPECS[entity]["key"]}
    for column in _linkage_columns(entity):
        if column not in excluded and column in prepared:
            row[column] = prepared.get(column)
    return row


def _family_survivor(group: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the deterministic earliest submission for one family group."""
    return min(
        group,
        key=lambda row: (
            _created_value(row) or "",
            str(row.get("zite_record_id") or ""),
        ),
    )


def _reconcile_rows(
    entity: str,
    existing: list[dict[str, Any]],
    prepared_groups: dict[Any, list[dict[str, Any]]],
    preparation: Preparation,
    cfg: Config,
    refresh_existing: bool,
) -> list[dict[str, Any]]:
    """Append new entities and deliberately merge diagnostics into old rows."""
    key = ENTITY_SPECS[entity]["key"]
    dob_notes = dob_notes_by_child(preparation.dob_log)
    merged: list[dict[str, Any]] = []
    consumed: set[str] = set()
    for stored in existing:
        token = _normalized_key(entity, stored.get(key))
        row = dict(stored)
        group = prepared_groups.get(token)
        if group is not None and refresh_existing:
            prepared = (
                _family_survivor(group) if entity == "family" else group[0]
            )
            row = _refresh_metadata(row, prepared, entity)
            if entity == "child" and not as_bool(row.get("ignore")):
                row["family_id"] = prepared.get("family_id")
                row["angel_id"] = prepared.get("angel_id")
            record_ids = [
                str(item.get("zite_record_id") or "") for item in group
            ]
            duplicate_text = (
                duplicate_note(
                    record_ids[0], preparation.survivors[entity], entity
                )
                if record_ids
                else None
            )
            validation = [
                note
                for current_id in record_ids
                for note in preparation.validation_notes[entity].get(
                    current_id, []
                )
            ]
            _apply_diagnostics(row, duplicate_text, validation)
            if entity == "child":
                row["potential_anomoly"] = merge_anomoly(
                    row.get("potential_anomoly"),
                    dob_notes.get(str(row.get("zite_record_id") or "")),
                )
                if row.get("family_id") is None:
                    row["clarification_notes"] = append_note(
                        row.get("clarification_notes"), ORPHAN_NOTE
                    )
                    if row.get("status") == STATUS_NEW:
                        row["status"] = STATUS_NEEDS_CLARIFICATION
        if token is not None:
            consumed.add(token)
        merged.append(row)

    for token, group in prepared_groups.items():
        if token in consumed:
            continue
        prepared = _family_survivor(group) if entity == "family" else group[0]
        if entity == "child":
            row = _seed_child_row(
                prepared, _child_baseline(prepared, group), cfg
            )
        elif entity == "family":
            row = _seed_family_row(prepared)
        else:
            row = _seed_sponsor_row(prepared)
        record_ids = [str(item.get("zite_record_id") or "") for item in group]
        duplicate_text = (
            duplicate_note(record_ids[0], preparation.survivors[entity], entity)
            if record_ids
            else None
        )
        validation = [
            note
            for current_id in record_ids
            for note in preparation.validation_notes[entity].get(current_id, [])
        ]
        _apply_diagnostics(row, duplicate_text, validation)
        if entity == "child":
            row["potential_anomoly"] = merge_anomoly(
                row.get("potential_anomoly"),
                dob_notes.get(str(row.get("zite_record_id") or "")),
            )
            if row.get("family_id") is None:
                row["status"] = STATUS_NEEDS_CLARIFICATION
        merged.append(row)

    if refresh_existing:
        for row in merged:
            if as_bool(row.get("ignore")):
                continue
            if entity == "child":
                row["age_at_xmas"] = _computed_age(row.get("dob"), cfg)
            elif entity == "sponsor":
                bounds = AGE_PREF_BOUNDS.get(
                    str(row.get("age_preference") or "")
                )
                row["pref_age_min"] = bounds[0] if bounds else None
                row["pref_age_max"] = bounds[1] if bounds else None
    _validate_entity_rows(entity, merged)
    return merged


def _prepared_groups(
    preparation: Preparation, entity: str
) -> dict[Any, list[dict[str, Any]]]:
    """Index prepared submissions by their stable persistent entity key."""
    groups: dict[Any, list[dict[str, Any]]] = {}
    for row in preparation.rows[entity]:
        key = ENTITY_SPECS[entity]["key"]
        token = _normalized_key(entity, row.get(key))
        if token is None:
            raise AuditError(f"prepared {entity} row lacks stable {key}")
        groups.setdefault(token, []).append(row)
    return groups


def reconcile_preparation(
    cfg: Config,
    preparation: Preparation,
    module: str,
    *,
    refresh_existing: bool,
    existing: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Reconcile and atomically persist all changed full final stores.

    ``refresh_existing=True`` is the screen path: safe source metadata,
    stable child linkage, global diagnostics, and computed fields merge into
    existing rows. ``False`` is the audit backstop: only genuinely missing
    entities are seeded. Raw-missing rows are retained in both modes.
    """
    stored = existing or {
        entity: read_final(cfg, entity)
        for entity in ("sponsor", "family", "child")
    }
    updated: dict[str, list[dict[str, Any]]] = {}
    sheets: dict[str, pl.DataFrame] = {}
    for entity in ("sponsor", "family", "child"):
        rows = _reconcile_rows(
            entity,
            stored[entity],
            _prepared_groups(preparation, entity),
            preparation,
            cfg,
            refresh_existing,
        )
        updated[entity] = rows
        normalized_old = [normalize_row(entity, row) for row in stored[entity]]
        normalized_new = [normalize_row(entity, row) for row in rows]
        if normalized_new != normalized_old:
            sheets[ENTITY_SPECS[entity]["sheet"]] = build_final_frame(
                entity, rows
            )
    if sheets:
        write_sheets(
            cfg.excel_file,
            module,
            sheets,
            xmas=cfg.xmas if "final_child" in sheets else None,
        )
    return updated


def prepare_persistent_store(cfg: Config, report: bool = True) -> Preparation:
    """Prepare raw rows against the current validated persistent identities.

    Args:
        cfg: Pipeline configuration.
        report: Emit duplicate/DOB-conflict warnings when True; audit call
            sites pass False so startup only checks conflicts silently.

    Returns:
        The pure preparation result for the workbook's current raw/final rows.
    """
    existing = {
        entity: read_final(cfg, entity)
        for entity in ("sponsor", "family", "child")
    }
    return prepare_workbook(cfg, existing, report=report)


def ensure_final_sheets(cfg: Config) -> None:
    """Backfill only genuinely missing entities before opening the audit UI.

    Existing persistent rows are never refreshed or projected by audit
    startup. Missing raw data remains an actionable acquire/screen error.

    Args:
        cfg: Pipeline configuration.

    Raises:
        AuditError: If no prepared raw rows exist or finals are malformed.
    """
    with _WRITE_LOCK:
        existing = {
            entity: read_final(cfg, entity)
            for entity in ("sponsor", "family", "child")
        }
        preparation = prepare_workbook(cfg, existing, report=False)
        if not any(preparation.rows.values()) and not any(existing.values()):
            raise AuditError(
                "no prepared sponsor/family/child rows;"
                " run `ccat acquire` and `ccat screen` first"
            )
        reconcile_preparation(
            cfg, preparation, "audit", refresh_existing=False, existing=existing
        )


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
        payload: Edited data columns plus ``status``/``ignore``/
            ``clarification_notes`` controls.

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
    raw, raw_is_current = _find_raw_row_state(cfg, entity, key_value)
    if raw is None:
        raise AuditError(f"unknown {entity} {key_value!r}")
    with _WRITE_LOCK:
        rows = read_final(cfg, entity)
        existing = next(
            (row for row in rows if str(row.get(key)) == str(key_value)), None
        )
        ignore_now = as_bool(payload.get("ignore"))
        ignore_before = as_bool(existing.get("ignore")) if existing else False
        baseline = _entity_baseline(
            cfg, entity, raw, is_current_raw=raw_is_current
        )

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
        # Controls. `status` is the sole audit authority; no Boolean control
        # exists. Omitted status preserves an existing audited state for
        # backward-compatible direct callers; UI always supplies it.
        requested_status = _as_text(payload.get("status"))
        audited_now = requested_status == STATUS_AUDITED or (
            requested_status is None
            and existing is not None
            and _as_text(existing.get("status")) == STATUS_AUDITED
        )
        row["ignore"] = ignore_now
        note = _as_text(payload.get("clarification_notes"))
        row["clarification_notes"] = note
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
            if existing is not None:
                for column in _module_owned_columns(entity):
                    row[column] = existing.get(column)
            row["status"] = STATUS_IGNORED
        else:
            if entity == "child":
                log_frame = read_sheet(cfg.excel_file, "dob_conflict_log")
                log_rows = log_frame.to_dicts() if log_frame is not None else []
                row["potential_anomoly"] = merge_anomoly(
                    row.get("potential_anomoly"),
                    dob_conflict_notes(log_rows, str(key_value)),
                )
            if audited_now:
                row["status"] = STATUS_AUDITED
            else:
                row["status"] = requested_status or STATUS_NEW
                row["status"] = status_of(row, baseline, entity)
        _upsert_row(rows, row, key)
        sheets: dict[str, pl.DataFrame] = {
            spec["sheet"]: build_final_frame(entity, rows)
        }
        write_sheets(
            cfg.excel_file,
            "audit",
            sheets,
            xmas=cfg.xmas if entity == "child" else None,
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
        return (
            *CHILD_MODULE_COLUMNS,
            *CHILD_ASSIGNMENT_COLUMNS,
            "screened",
            "potential_duplicate",
        )
    if entity == "family":
        return (*FAMILY_ASSIGNMENT_COLUMNS, "potential_duplicate")
    return (
        *SPONSOR_ASSIGNMENT_COLUMNS,
        *SPONSOR_DISPATCH_COLUMNS,
        "potential_duplicate",
    )


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
    cfg: Config,
    entity: str,
    raw: dict[str, Any],
    *,
    is_current_raw: bool = True,
) -> dict[str, Any]:
    """Return the entity baseline without borrowing another submission.

    Args:
        cfg: Pipeline configuration.
        entity: Entity name.
        raw: Current prepared row, or the raw-missing stored fallback.
        is_current_raw: False when ``raw`` is a raw-missing stored row.

    Returns:
        Linkage/editable baseline values.
    """
    if entity != "child":
        return {column: raw.get(column) for column in _raw_columns(entity)}
    if not is_current_raw:
        return {column: raw.get(column) for column in _raw_columns(entity)}
    group = _child_groups(cfg).get(
        (_as_text(raw.get("primary_email")), _as_text(raw.get("first_name")))
    )
    return _child_baseline(raw, group or [])


def _find_raw_row_state(
    cfg: Config, entity: str, key_value: Any
) -> tuple[dict[str, Any] | None, bool]:
    """Find a row and whether it came from current prepared raw data.

    Returns:
        (row, is_current_raw); the fallback is a raw-missing stored row.
    """
    key = ENTITY_SPECS[entity]["key"]
    for row in _entity_rows(cfg, entity):
        if str(row.get(key)) == str(key_value):
            return row, True
    for row in read_final(cfg, entity):
        if str(row.get(key)) == str(key_value):
            return row, False
    return None, False


def _find_raw_row(
    cfg: Config, entity: str, key_value: Any
) -> dict[str, Any] | None:
    """Find a current prepared row, or its raw-missing persistent row.

    The fallback deliberately uses stored effective values as the baseline;
    it never reconstructs raw-missing entities from another source.
    """
    return _find_raw_row_state(cfg, entity, key_value)[0]


def save_child(
    cfg: Config, zite_record_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Save one child's complete final row directly (§9.2).

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
        and raw.get("family_id") is None
        and _as_text(payload.get("status")) == STATUS_AUDITED
    ):
        raise AuditError(
            "orphaned child cannot be marked audited before a matching"
            " family registration exists (§8.7)"
        )
    return _save_row(cfg, "child", zite_record_id, payload)


def save_family(
    cfg: Config, family_id: int, payload: dict[str, Any]
) -> dict[str, Any]:
    """Save one family's complete final row (assignment columns preserved).

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
    """Save one sponsor's complete final row (assignment columns preserved).

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
