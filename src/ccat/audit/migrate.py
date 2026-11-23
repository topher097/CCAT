"""One-time v1-to-persistent-final migration (audit_architecture.md §11).

``ccat audit-migrate --config config/ccat_2026.json --from
./output/ccat_2026.xlsx`` copies raw, complete legacy effective rows, all
operational logs, and the current match report into persistent ``final_*``
stores in ``cfg.excel_file``.  Legacy-only reads explicitly use
``read_sheet`` because old finals are projections and old adj rows are
sparse; they are migration inputs, never active current-format stores.  The
source is never written, and no adj or submissions sheets are emitted.
"""

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from ccat.audit import AuditError
from ccat.audit.store import (
    CHILD_EDITABLE_COLUMNS,
    CHILD_LINKAGE_COLUMNS,
    FAMILY_ASSIGNMENT_COLUMNS,
    FAMILY_EDITABLE_COLUMNS,
    FAMILY_LINKAGE_COLUMNS,
    FINAL_CHILD_COLUMNS,
    FINAL_FAMILY_COLUMNS,
    FINAL_SPONSOR_COLUMNS,
    ORPHAN_NOTE,
    SPONSOR_ASSIGNMENT_COLUMNS,
    SPONSOR_COMPUTED_COLUMNS,
    SPONSOR_EDITABLE_COLUMNS,
    SPONSOR_LINKAGE_COLUMNS,
    STATUS_AUDITED,
    STATUS_NEEDS_CLARIFICATION,
    STATUS_NEW,
    _computed_age,
    as_bool,
    build_final_frame,
    normalize_row,
    status_of,
)
from ccat.config import Config
from ccat.domain import AGE_PREF_BOUNDS
from ccat.io import list_sheets, read_sheet, write_sheets

logger = logging.getLogger(__name__)

#: Active persistent sheets whose presence in the target blocks migration.
_BLOCKING_SHEETS = ("final_child", "final_family", "final_sponsor")

#: Forbidden retired layers; a migrated target must never mix formats.
_FORBIDDEN_PREFIXES = ("adj_",)
_FORBIDDEN_SUFFIXES = ("_submissions",)

#: Legacy raw plus operational sheets that survive conversion.
_RAW_SHEETS = ("raw_sponsor", "raw_family", "raw_child")
_LOG_SHEETS = (
    "duplicate_log",
    "dob_conflict_log",
    "email_validation_log",
    "address_validation_log",
)
_COPIED_SHEETS = (*_RAW_SHEETS, *_LOG_SHEETS, "match_report")

#: Old-final columns overlaid onto raw child data.
_FINAL_TAKEOVER_COLUMNS = (
    *CHILD_EDITABLE_COLUMNS,
    "age_at_xmas",
    "generated_info_sheet",
)

#: The D4 mapping for the old Boolean special_needs column.
_SPECIAL_NEEDS_LEGACY = "other: unspecified"


def _require_unique_keys(
    frame: pl.DataFrame | None,
    key: str,
    label: str,
) -> None:
    """Reject missing, blank, or duplicate structural identity keys."""
    if frame is None:
        return
    if key not in frame.columns:
        raise AuditError(f"{label} lacks required identity column {key!r}")
    values: list[str] = []
    for value in frame.get_column(key).to_list():
        text = str(value).strip() if value is not None else ""
        if not text:
            raise AuditError(f"{label} contains a null or blank {key}")
        values.append(text)
    if len(values) != len(set(values)):
        raise AuditError(f"{label} contains duplicate {key} keys")


def _keyed_by(
    frame: pl.DataFrame | None, key: str, label: str
) -> dict[str, dict[str, Any]]:
    """Index one old frame by a validated unique key."""
    _require_unique_keys(frame, key, label)
    if frame is None:
        return {}
    return {
        str(row.get(key)): row
        for row in frame.to_dicts()
        if row.get(key) is not None
    }


def _overlay(
    row: dict[str, Any],
    old_final: dict[str, Any] | None,
    columns: tuple[str, ...],
) -> None:
    """Copy applicable old-final values, including intentional nulls."""
    if old_final is None:
        return
    for column in columns:
        if column in old_final:
            row[column] = old_final.get(column)


def run(
    cfg: Config,
    from_path: str,
    force: bool = False,
) -> None:
    """Bootstrap complete persistent finals from a legacy v1 workbook.

    Args:
        cfg: Pipeline configuration; ``cfg.excel_file`` is the new workbook.
        from_path: The old workbook, opened read-only.
        force: Re-create active final sheets when the target contains them.

    Raises:
        AuditError: If source/target structure is invalid or identities are
            ambiguous.
    """
    # Target structure is checked before touching source data so an unsafe
    # destination is never diagnosed as merely "source missing".
    existing = set(list_sheets(cfg.excel_file))
    forbidden = sorted(
        {
            name
            for name in existing
            if name.startswith(_FORBIDDEN_PREFIXES)
            or name.endswith(_FORBIDDEN_SUFFIXES)
        }
    )
    if forbidden:
        raise AuditError(
            "target workbook contains retired legacy layer(s) "
            f"{forbidden}; migration requires a fresh target with no "
            "adj/submissions sheets"
        )
    blocking = sorted(set(_BLOCKING_SHEETS) & existing)
    if blocking and not force:
        raise AuditError(
            f"target workbook {cfg.excel_file} already contains"
            f" {blocking}; the bootstrap only writes a fresh workbook"
            " (pass --force to re-create them from the source)"
        )
    source = Path(from_path)
    if not source.is_file():
        raise AuditError(f"source workbook not found: {source}")
    if str(source.resolve()) == str(Path(cfg.excel_file).resolve()):
        raise AuditError("the --from workbook must differ from excel_file")

    frames: dict[str, pl.DataFrame | None] = {
        sheet: read_sheet(source, sheet) for sheet in _COPIED_SHEETS
    }
    frames.update(
        {
            sheet: read_sheet(source, sheet)
            for sheet in (
                "adj_child",
                "final_child",
                "final_family",
                "final_sponsor",
            )
        }
    )
    for sheet in _RAW_SHEETS:
        if frames[sheet] is None:
            raise AuditError(
                f"source workbook has no {sheet} sheet; run `ccat acquire`"
                " on the old system first"
            )
    raw_child_frame = frames["raw_child"]
    raw_family_frame = frames["raw_family"]
    raw_sponsor_frame = frames["raw_sponsor"]
    assert raw_child_frame is not None
    assert raw_family_frame is not None
    assert raw_sponsor_frame is not None
    raw_children = raw_child_frame.to_dicts()
    raw_families = raw_family_frame.to_dicts()
    raw_sponsors = raw_sponsor_frame.to_dicts()
    _require_unique_keys(frames["raw_child"], "zite_record_id", "raw_child")
    _require_unique_keys(frames["raw_family"], "zite_record_id", "raw_family")
    _require_unique_keys(frames["raw_sponsor"], "zite_record_id", "raw_sponsor")
    _require_unique_keys(
        frames["raw_sponsor"], "sponsor_id", "raw_sponsor entity"
    )

    old_adj_by_id = _keyed_by(
        frames["adj_child"], "zite_record_id", "adj_child"
    )
    old_final_by_id = _keyed_by(
        frames["final_child"], "zite_record_id", "final_child"
    )
    old_final_family = _keyed_by(
        frames["final_family"], "family_id", "final_family"
    )
    old_final_sponsor = _keyed_by(
        frames["final_sponsor"], "sponsor_id", "final_sponsor"
    )

    from ccat.clarify.status import load_statuses

    statuses = load_statuses(cfg)
    # The legacy family column names the sponsor and the children it
    # placed; the child row is now the assignment grain, so carry that
    # pairing onto each child.
    family_assignments = _family_assignments(old_final_family)
    replied_families = {
        str(family_id)
        for family_id, status in statuses.items()
        if status.replied
    }
    marked_at = datetime.now(UTC).isoformat()

    # Children: every raw and every legacy effective identity survives.
    raw_child_ids = {str(row.get("zite_record_id")) for row in raw_children}
    legacy_child_ids = set(old_adj_by_id) | set(old_final_by_id)
    final_children = [
        _carry_child_assignment(
            _migrated_child_row(
                raw,
                old_adj_by_id.get(str(raw.get("zite_record_id"))),
                old_final_by_id.get(str(raw.get("zite_record_id"))),
                cfg,
                sponsored=_carried_sponsor(
                    raw.get("family_id"),
                    raw.get("angel_id"),
                    family_assignments,
                )
                is not None,
                replied=str(raw.get("family_id")) in replied_families,
                marked_at=marked_at,
            ),
            family_assignments,
        )
        for raw in raw_children
    ]
    for record_id in sorted(legacy_child_ids - raw_child_ids):
        final_children.append(
            _carry_child_assignment(
                _legacy_only_child_row(
                    old_adj_by_id.get(record_id),
                    old_final_by_id.get(record_id),
                    cfg,
                ),
                family_assignments,
            )
        )

    # Families: one entity per stable ID, from the earliest raw submission.
    families_by_id: dict[str, list[dict[str, Any]]] = {}
    for raw in raw_families:
        families_by_id.setdefault(str(raw.get("family_id")), []).append(raw)

    def family_sort(row: dict[str, Any]) -> tuple[str, str]:
        return (
            str(row.get("created") or ""),
            str(row.get("zite_record_id") or ""),
        )

    final_families = []
    for family_id in sorted(families_by_id, key=lambda value: int(value)):
        survivor = min(families_by_id[family_id], key=family_sort)
        row = _seeded_family(survivor)
        prior = old_final_family.get(family_id)
        _overlay(
            row, prior, (*FAMILY_EDITABLE_COLUMNS, *FAMILY_ASSIGNMENT_COLUMNS)
        )
        _overlay(
            row,
            prior,
            (
                "potential_duplicate",
                "status",
                "require_clarification",
                "audited_at",
            ),
        )
        if prior is not None:
            row["status"] = (
                STATUS_AUDITED
                if _legacy_bool(prior.get("audited"))
                else row.get("status")
            )
            row["ignore"] = as_bool(prior.get("ignore"))
        row["num_angels_in_family"] = (
            prior.get("num_angels_in_family")
            if prior is not None
            and prior.get("num_angels_in_family") is not None
            else sum(
                child.get("family_id") is not None
                and str(child.get("family_id")) == family_id
                for child in final_children
            )
        )
        final_families.append(row)
    for family_id in sorted(
        set(old_final_family) - set(families_by_id),
        key=lambda value: int(value),
    ):
        final_families.append(
            _legacy_only_entity_row(
                old_final_family[family_id],
                "family",
                (*FAMILY_EDITABLE_COLUMNS, *FAMILY_ASSIGNMENT_COLUMNS),
                (
                    *FAMILY_LINKAGE_COLUMNS,
                    "potential_duplicate",
                    "status",
                    "require_clarification",
                    "audited_at",
                ),
            )
        )

    final_sponsors = []
    for raw in raw_sponsors:
        sponsor_id = str(raw.get("sponsor_id"))
        row = _seeded_sponsor(raw)
        prior = old_final_sponsor.get(sponsor_id)
        _overlay(
            row,
            prior,
            (
                *SPONSOR_EDITABLE_COLUMNS,
                *SPONSOR_COMPUTED_COLUMNS,
                *SPONSOR_ASSIGNMENT_COLUMNS,
            ),
        )
        _overlay(
            row,
            prior,
            (
                "potential_duplicate",
                "status",
                "require_clarification",
                "audited_at",
            ),
        )
        if prior is not None:
            row["status"] = (
                STATUS_AUDITED
                if _legacy_bool(prior.get("audited"))
                else row.get("status")
            )
            row["ignore"] = as_bool(prior.get("ignore"))
        final_sponsors.append(row)
    for sponsor_id in sorted(
        set(old_final_sponsor)
        - {str(row.get("sponsor_id")) for row in raw_sponsors},
        key=lambda value: int(value),
    ):
        final_sponsors.append(
            _legacy_only_entity_row(
                old_final_sponsor[sponsor_id],
                "sponsor",
                (
                    *SPONSOR_EDITABLE_COLUMNS,
                    *SPONSOR_COMPUTED_COLUMNS,
                    *SPONSOR_ASSIGNMENT_COLUMNS,
                ),
                (
                    *SPONSOR_LINKAGE_COLUMNS,
                    "potential_duplicate",
                    "status",
                    "require_clarification",
                    "audited_at",
                ),
            )
        )

    sheets: dict[str, pl.DataFrame] = {}
    for sheet in _COPIED_SHEETS:
        frame = frames[sheet]
        if frame is not None:
            sheets[sheet] = frame
    sheets["dob_conflict_log"] = sheets.get("dob_conflict_log", pl.DataFrame())
    sheets["final_child"] = build_final_frame("child", final_children)
    sheets["final_family"] = build_final_frame("family", final_families)
    sheets["final_sponsor"] = build_final_frame("sponsor", final_sponsors)
    write_sheets(cfg.excel_file, "migrate", sheets, xmas=cfg.xmas)

    audited = sum(row["status"] == STATUS_AUDITED for row in final_children)
    notes = sum(
        row["status"] == STATUS_NEEDS_CLARIFICATION for row in final_children
    )
    assignments = sum(
        row.get("assigned_sponsor_id") is not None for row in final_children
    )
    logger.info(
        "migration complete: %d children (%d audited, %d needing"
        " clarification, %d with carried-over assignments), %d families,"
        " %d sponsors; source never written",
        len(final_children),
        audited,
        notes,
        assignments,
        len(final_families),
        len(final_sponsors),
    )


def _family_assignments(
    old_final_family: dict[str, dict[str, Any]],
) -> dict[str, tuple[int, set[str]]]:
    """Map each legacy family to its sponsor and the angel ids it names.

    Args:
        old_final_family: The old workbook's final family rows by id.

    Returns:
        Mapping of family id -> (sponsor id, the named angel ids).  An
        empty set means the whole family was placed (the legacy column
        was not narrowed).
    """
    assignments: dict[str, tuple[int, set[str]]] = {}
    for family_id, row in old_final_family.items():
        sponsor_id = _as_int_or_none(row.get("assigned_sponsor_id"))
        if sponsor_id is None:
            continue
        assignments[str(family_id)] = (
            sponsor_id,
            _assigned_angel_ids(row.get("assigned_angel_ids")),
        )
    return assignments


def _assigned_angel_ids(value: Any) -> set[str]:
    """Parse one comma-joined legacy angel id list into uppercase tokens.

    Args:
        value: The stored cell, or anything else.

    Returns:
        The stripped, uppercased angel ids; empty when the cell is blank.
    """
    if value is None:
        return set()
    return {
        token.strip().upper()
        for token in str(value).replace(";", ",").split(",")
        if token.strip()
    }


def _carried_sponsor(
    family_id: Any,
    angel_id: Any,
    family_assignments: dict[str, tuple[int, set[str]]],
) -> int | None:
    """Return the sponsor the legacy family assignment gives one child.

    Args:
        family_id: The child's family id.
        angel_id: The child's angel id.
        family_assignments: The legacy family assignment map.

    Returns:
        The sponsor id, or None when the child was not placed.
    """
    entry = family_assignments.get(str(family_id))
    if entry is None:
        return None
    sponsor_id, named = entry
    if named and str(angel_id or "").strip().upper() not in named:
        return None
    return sponsor_id


def _carry_child_assignment(
    row: dict[str, Any],
    family_assignments: dict[str, tuple[int, set[str]]],
) -> dict[str, Any]:
    """Carry the legacy family assignment onto the child row.

    Args:
        row: One normalized final child row.
        family_assignments: The legacy family assignment map.

    Returns:
        The same row with its own assignment columns set when placed.
    """
    from ccat.services.db_assignments import SOURCE_AUTO

    sponsor_id = _carried_sponsor(
        row.get("family_id"), row.get("angel_id"), family_assignments
    )
    if sponsor_id is not None:
        row["assigned_sponsor_id"] = sponsor_id
        row["assignment_source"] = SOURCE_AUTO
    return row


def _legacy_only_child_row(
    old_adj: dict[str, Any] | None,
    old_final: dict[str, Any] | None,
    cfg: Config,
) -> dict[str, Any]:
    """Build one full child from its legacy effective row and controls."""
    effective = old_final or old_adj
    if effective is None or effective.get("zite_record_id") is None:
        raise AuditError(
            "raw-missing legacy child lacks an effective final row; explicit"
            " conflict resolution is required before migration"
        )
    row: dict[str, Any] = {column: None for column in FINAL_CHILD_COLUMNS}
    legacy_audited = _legacy_bool((old_final or {}).get("audited"))
    _overlay(row, effective, tuple(FINAL_CHILD_COLUMNS))
    row["clarification_notes"] = effective.get(
        "clarification_notes", effective.get("require_clarification")
    )
    row["clarification_notes"] = row.get("clarification_notes")
    row["ignore"] = as_bool(row.get("ignore"))
    row["screened"] = old_adj is not None or as_bool(row.get("screened"))
    row["generated_info_sheet"] = as_bool(row.get("generated_info_sheet"))
    if legacy_audited or _legacy_bool(row.pop("audited", False)):
        row["status"] = STATUS_AUDITED
    _apply_orphan_state(row)
    if row.get("status") is None:
        row["status"] = status_of(row, row, "child")
    return normalize_row("child", row)


def _legacy_bool(value: Any) -> bool:
    """Interpret Boolean/string/numeric legacy control cells safely."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, int | float) and not isinstance(value, bool):
        return value == 1
    return as_bool(value)


def _legacy_only_entity_row(
    old_final: dict[str, Any],
    entity: str,
    data_columns: tuple[str, ...],
    preserved_columns: tuple[str, ...],
) -> dict[str, Any]:
    """Build one full raw-missing family/sponsor from its old final."""
    columns = (
        FINAL_FAMILY_COLUMNS if entity == "family" else FINAL_SPONSOR_COLUMNS
    )
    row: dict[str, Any] = {column: None for column in columns}
    _overlay(row, old_final, (*data_columns, *preserved_columns))
    key = "family_id" if entity == "family" else "sponsor_id"
    row[key] = old_final.get(key)
    row["clarification_notes"] = old_final.get(
        "clarification_notes", old_final.get("require_clarification")
    )
    row["ignore"] = as_bool(row.get("ignore"))
    row["potential_duplicate"] = as_bool(row.get("potential_duplicate"))
    if _legacy_bool(row.pop("audited", False)):
        row["status"] = STATUS_AUDITED
    if row.get("status") is None:
        row["status"] = status_of(row, row, entity)
    return normalize_row(entity, row)


def _seeded_family(raw: dict[str, Any]) -> dict[str, Any]:
    """Build one complete persistent family row from legacy raw data."""
    row: dict[str, Any] = {column: None for column in FINAL_FAMILY_COLUMNS}
    for column in (*FAMILY_LINKAGE_COLUMNS, *FAMILY_EDITABLE_COLUMNS):
        row[column] = raw.get(column)
    row["family_id"] = raw.get("family_id")
    row["status"] = STATUS_NEW
    row["ignore"] = False
    return row


def _seeded_sponsor(raw: dict[str, Any]) -> dict[str, Any]:
    """Build one complete persistent sponsor row from legacy raw data."""
    row: dict[str, Any] = {column: None for column in FINAL_SPONSOR_COLUMNS}
    for column in (*SPONSOR_LINKAGE_COLUMNS, *SPONSOR_EDITABLE_COLUMNS):
        row[column] = raw.get(column)
    row["sponsor_id"] = raw.get("sponsor_id")
    bounds = AGE_PREF_BOUNDS.get(str(raw.get("age_preference") or ""))
    if bounds is not None:
        row["pref_age_min"], row["pref_age_max"] = bounds
    row["status"] = STATUS_NEW
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
    """Build one full persistent child row from legacy sheets."""
    row: dict[str, Any] = {column: None for column in FINAL_CHILD_COLUMNS}
    for column in (*CHILD_LINKAGE_COLUMNS, *CHILD_EDITABLE_COLUMNS):
        row[column] = raw.get(column)
    if old_final is not None:
        raw_angel = str(raw.get("angel_id") or "")
        final_angel = str(old_final.get("angel_id") or "")
        if final_angel and raw_angel != final_angel:
            raise AuditError(
                f"child {raw.get('zite_record_id')!r} has conflicting raw"
                f" angel_id {raw_angel!r} and historical final angel_id"
                f" {final_angel!r}; resolve the explicit identity conflict"
                " before migration"
            )
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
        row["special_needs"] = (
            _SPECIAL_NEEDS_LEGACY if as_bool(special) else None
        )
        row["clarification_notes"] = old_final.get(
            "clarification_notes", old_final.get("require_clarification")
        )
        row["audited_at"] = old_final.get("audited_at")
        row["generated_info_sheet"] = as_bool(
            old_final.get("generated_info_sheet")
        )
        if _legacy_bool(old_final.get("audited")):
            row["status"] = STATUS_AUDITED
    row["screened"] = old_adj is not None
    row["ignore"] = False
    has_note = (
        row.get("clarification_notes") is not None
        and str(row.get("clarification_notes")).strip() != ""
    )
    if sponsored and not (has_note and not replied):
        row["status"] = STATUS_AUDITED
        if not row.get("audited_at"):
            row["audited_at"] = marked_at
    if has_note and not replied:
        row["status"] = STATUS_NEEDS_CLARIFICATION
    age = _computed_age(row.get("dob"), cfg)
    if age is None and old_final is not None:
        age = _as_int_or_none(old_final.get("age_at_xmas"))
    row["age_at_xmas"] = age
    baseline = {
        **{column: raw.get(column) for column in CHILD_LINKAGE_COLUMNS},
        **{column: raw.get(column) for column in CHILD_EDITABLE_COLUMNS},
    }
    row["status"] = status_of(row, baseline, "child")
    _apply_orphan_state(row)
    return normalize_row("child", row)


def _apply_orphan_state(row: dict[str, Any]) -> None:
    """Apply current orphan clarification semantics to a null-family child."""
    if row.get("family_id") is not None:
        return
    note = str(row.get("clarification_notes") or "").strip()
    if not note:
        row["clarification_notes"] = ORPHAN_NOTE
        row["status"] = STATUS_NEEDS_CLARIFICATION


def _as_int_or_none(value: Any) -> int | None:
    """Coerce a legacy age cell to int, tolerating text round-trips."""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
