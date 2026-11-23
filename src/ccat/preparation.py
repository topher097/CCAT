"""Pure local preparation of raw sponsor, family, and child records.

This module owns the lower-level work shared by screening, the persistent
entity store, and statistics: local cleaning and validation notes, duplicate
diagnostics, stable source/entity linkage, angel allocation, and sibling DOB
checks. It neither reads nor writes through the audit store and performs no AI
or other external work.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any

import polars as pl
from polars._typing import PolarsDataType

from ccat.acquire import PL_DTYPES
from ccat.config import Config
from ccat.domain import (
    DERIVED_COLUMNS,
    SPONSOR_CLAIM_SHEET,
    _created_value,
    _cross_check_created,
    _parse_reported_age,
    assign_angel_ids,
    assign_family_ids,
    assign_sponsor_ids,
    clean_value,
    index_to_letters,
    parse_prior_assigned,
    sibling_dob_conflicts,
)
from ccat.io import read_sheet

logger = logging.getLogger(__name__)

_TABLE_NAMES = ("sponsor", "family", "child")


class PreparationError(Exception):
    """Raised when raw input cannot be prepared safely."""


@dataclass
class Preparation:
    """Complete in-memory local-preparation output.

    Attributes:
        rows: Prepared submission rows keyed by sponsor/family/child.
        validation_notes: Per-table, per-record local validation notes.
        duplicate_log: Legacy-compatible duplicate diagnostic payloads.
        survivors: Per-table mappings from duplicate IDs to survivor rows.
        dob_log: Sibling DOB-conflict diagnostic rows using effective IDs.
    """

    rows: dict[str, list[dict[str, Any]]]
    validation_notes: dict[str, dict[str, list[str]]]
    duplicate_log: list[dict[str, Any]]
    survivors: dict[str, dict[str, dict[str, Any]]]
    dob_log: list[dict[str, Any]]


#: Column schema retained for duplicate-log payload compatibility.
DUPLICATE_LOG_SCHEMA: dict[str, PolarsDataType] = {
    "sheet": pl.String,
    "group_key": pl.String,
    "zite_record_id": pl.String,
    "created": pl.String,
    "survivor": pl.Boolean,
    "conflict_columns": pl.String,
    "conflict_details": pl.String,
    "record": pl.String,
}


def _enum_failure(
    value: Any, table_name: str, key: str, cfg: Config
) -> str | None:
    """Return a note when an enum value is not a vocabulary label.

    Args:
        value: The cleaned enum cell value.
        table_name: One of sponsor, family, or child.
        key: The config column key.
        cfg: Pipeline configuration.

    Returns:
        The note text, or None when the value is absent or valid.
    """
    if value is None:
        return None
    spec = cfg.tables[table_name].columns[key]
    if spec.type != "enum" or spec.values is None:
        return None
    allowed = cfg.enum_values(table_name, key)
    if str(value) in allowed:
        return None
    return (
        f"{key} {str(value)!r} is not one of the allowed options"
        f" ({', '.join(allowed)})"
    )


def _validate_raw_ids(rows: list[dict[str, Any]], table_name: str) -> None:
    """Require nonblank, unique source IDs before any row can be cleaned.

    Args:
        rows: Verbatim rows for one entity table.
        table_name: One of sponsor, family, or child.

    Raises:
        PreparationError: If any ID is missing/blank or duplicated after
            surrounding whitespace is removed.
    """
    seen: set[str] = set()
    duplicate_ids: set[str] = set()
    for index, row in enumerate(rows, start=1):
        value = row.get("zite_record_id")
        record_id = "" if value is None else str(value).strip()
        if not record_id:
            raise PreparationError(
                f"raw_{table_name} row {index} has a missing or blank"
                " zite_record_id; every raw row must have a unique ID"
            )
        if record_id in seen:
            duplicate_ids.add(record_id)
        seen.add(record_id)
    if duplicate_ids:
        duplicates = sorted(duplicate_ids)
        raise PreparationError(
            f"raw_{table_name} contains duplicate zite_record_id value(s)"
            f" {duplicates}; every raw row must have a unique ID"
        )


def clean_rows(
    rows: list[dict[str, Any]], table_name: str, cfg: Config
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Clean verbatim rows locally and collect validation notes.

    Blank strings become nulls, declared defaults fill nulls, free-text
    reported ages are coerced, and each cell is normalized per its column
    type. Failures other than structural source-ID errors never drop a row:
    every issue becomes a note keyed by zite_record_id.

    Args:
        rows: Verbatim rows from acquisition.
        table_name: One of sponsor, family, or child.
        cfg: Pipeline configuration.

    Returns:
        A tuple of cleaned rows and record-ID validation-note lists.

    Raises:
        PreparationError: If a source ID is missing, blank, or duplicated.
    """
    _validate_raw_ids(rows, table_name)
    specs = cfg.tables[table_name].columns
    cleaned: list[dict[str, Any]] = []
    notes: dict[str, list[str]] = {}
    for row in rows:
        out: dict[str, Any] = {}
        record_id = str(row.get("zite_record_id") or "")
        row_notes: list[str] = []
        for key, spec in specs.items():
            value = row.get(key)
            if isinstance(value, str) and value.strip() == "":
                value = None
            if value is None and spec.has_default:
                value = spec.default
            if table_name == "child" and key == "age_reported":
                if value is not None:
                    value = _parse_reported_age(value)
            cleaned_value, reason = clean_value(value, spec)
            if reason is not None:
                row_notes.append(f"{key}: {reason} (value {value!r})")
            out[key] = cleaned_value
            enum_note = _enum_failure(cleaned_value, table_name, key, cfg)
            if enum_note is not None:
                row_notes.append(enum_note)
        if record_id:
            if row_notes:
                notes[record_id] = row_notes
            cleaned.append(out)
    return cleaned, notes


def prepared_frame(
    rows: list[dict[str, Any]], table_name: str, cfg: Config
) -> pl.DataFrame:
    """Build the typed in-memory prepared-row frame used by statistics.

    This is the old submissions-frame projection without any persistence.
    The table's config columns and derived columns are included and rows are
    sorted by created timestamp and zite_record_id ascending.

    Args:
        rows: Cleaned, ID-assigned prepared rows.
        table_name: One of sponsor, family, or child.
        cfg: Pipeline configuration.

    Returns:
        A typed polars DataFrame; no workbook write occurs.
    """
    specs = cfg.tables[table_name].columns
    schema: dict[str, PolarsDataType] = {
        key: PL_DTYPES[spec.type] for key, spec in specs.items()
    }
    for column in DERIVED_COLUMNS[table_name]:
        schema[column] = (
            pl.Int64
            if column in {"sponsor_id", "family_id", "age_at_xmas"}
            else pl.String
        )
    ordered = sorted(
        rows,
        key=lambda row: (
            _created_value(row) or "",
            row.get("zite_record_id") or "",
        ),
    )
    return pl.DataFrame(
        [{key: row.get(key) for key in schema} for row in ordered],
        schema=schema,
    )


def detect_duplicates(
    rows: list[dict[str, Any]], table_name: str, report: bool = True
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Detect duplicate submissions without merging or dropping any row.

    Families group by primary_email, children by primary_email and first_name,
    and sponsors by email. Earliest family/sponsor submissions survive; latest
    child submissions survive. Log payloads, including legacy sheet labels,
    remain byte-for-byte compatible with screening's original implementation.

    Args:
        rows: Cleaned rows for one table.
        table_name: One of sponsor, family, or child.
        report: Emit one warning per duplicate group when True; call sites
            that only need the log rows (such as audit startup) pass False.

    Returns:
        Duplicate-log rows and a mapping of each member ID to its survivor.
    """
    if table_name == "family":
        key_fields: tuple[str, ...] = ("primary_email",)
        latest_wins = False
    elif table_name == "child":
        key_fields = ("primary_email", "first_name")
        latest_wins = True
    else:
        key_fields = ("email",)
        latest_wins = False
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in rows:
        values = tuple(
            str(row.get(field)).strip() if row.get(field) is not None else ""
            for field in key_fields
        )
        if any(value == "" for value in values):
            continue
        groups.setdefault(values, []).append(row)
    log_rows: list[dict[str, Any]] = []
    survivor_of: dict[str, dict[str, Any]] = {}
    excluded = {"zite_record_id", "created", "zite_created_at", "create"}
    for key, group in groups.items():
        if len(group) < 2:
            continue
        ordered = sorted(
            group,
            key=lambda row: (
                _created_value(row) or "",
                row.get("zite_record_id") or "",
            ),
            reverse=latest_wins,
        )
        survivor, extras = ordered[0], ordered[1:]
        group_key = "|".join(key)
        conflicts: list[str] = []
        for column in survivor:
            if column in excluded:
                continue
            values_seen = {
                str(row.get(column))
                for row in group
                if row.get(column) is not None
            }
            if len(values_seen) > 1:
                conflicts.append(column)
        details = "; ".join(
            f"{column}: survivor {survivor.get(column)!r} vs"
            f" {[row.get(column) for row in extras]}"
            for column in conflicts
        )
        for row in group:
            record_id = str(row.get("zite_record_id") or "")
            survivor_of[record_id] = survivor
            log_rows.append(
                {
                    "sheet": f"{table_name}_submissions",
                    "group_key": group_key,
                    "zite_record_id": record_id,
                    "created": _created_value(row),
                    "survivor": row is survivor,
                    "conflict_columns": ",".join(conflicts),
                    "conflict_details": details,
                    "record": json.dumps(row, default=str, sort_keys=True),
                }
            )
        if report:
            logger.warning(
                "%s duplicate group %s: %d submission(s); survivor %s, extras %s",
                table_name,
                group_key,
                len(group),
                survivor.get("zite_record_id"),
                [row.get("zite_record_id") for row in extras],
            )
    return log_rows, survivor_of


def detect_sponsor_claim_contests(
    sponsor_rows: list[dict[str, Any]],
    child_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Detect sponsors claiming the same Assigned Angel ID.

    A sponsor's ``prior_assigned_angel_ids`` is the free-text list of
    children it claims (the Zite "Assigned Angel ID Code").  When two
    sponsors claim the same angel id, only one of them can hold that
    child, so the claim is a contest the audit has to settle: the
    surviving sponsor keeps the claim and the loser is ignored.

    Claims that name no known child are left alone (the matcher already
    drops them), and claimants that all share one email are the same
    person twice, which the email duplicate group already owns.

    Args:
        sponsor_rows: Cleaned sponsor rows.
        child_rows: Child rows carrying their angel ids.

    Returns:
        Duplicate-log rows, one per claimant of a contested angel id.
    """
    known_angels = {
        str(row.get("angel_id")) for row in child_rows if row.get("angel_id")
    }
    claimants: dict[str, list[dict[str, Any]]] = {}
    for row in sponsor_rows:
        raw = row.get("prior_assigned_angel_ids")
        if not raw:
            continue
        for angel_id in parse_prior_assigned(str(raw)):
            if angel_id in known_angels:
                claimants.setdefault(angel_id, []).append(row)
    log_rows: list[dict[str, Any]] = []
    for angel_id, group in sorted(claimants.items()):
        if len(group) < 2:
            continue
        emails = {str(row.get("email") or "").strip().lower() for row in group}
        if len(emails) < 2:
            # The same person twice; the email duplicate group owns it.
            continue
        ordered = sorted(
            group,
            key=lambda row: (
                _created_value(row) or "",
                str(row.get("zite_record_id") or ""),
            ),
            reverse=True,
        )
        survivor = ordered[0]
        claimant_ids = ", ".join(
            sorted(str(row.get("zite_record_id") or "") for row in group)
        )
        details = (
            f"prior_assigned_angel_ids: angel {angel_id} claimed by"
            f" {len(group)} sponsors: {claimant_ids}"
        )
        for row in group:
            log_rows.append(
                {
                    "sheet": SPONSOR_CLAIM_SHEET,
                    "group_key": angel_id,
                    "zite_record_id": str(row.get("zite_record_id") or ""),
                    "created": _created_value(row),
                    "survivor": row is survivor,
                    "conflict_columns": "prior_assigned_angel_ids",
                    "conflict_details": details,
                    "record": json.dumps(row, default=str, sort_keys=True),
                }
            )
    return log_rows


def duplicate_log_frame(log_rows: list[dict[str, Any]]) -> pl.DataFrame:
    """Build a typed duplicate-log frame without writing it.

    Args:
        log_rows: Log rows from detect_duplicates.

    Returns:
        The typed frame, including a correctly typed empty frame.
    """
    return pl.DataFrame(log_rows, schema=DUPLICATE_LOG_SCHEMA)


def duplicate_note(
    record_id: str, survivor_of: dict[str, dict[str, Any]], entity: str
) -> str | None:
    """Build the legacy clarification note for one duplicate submission.

    Args:
        record_id: The row's zite_record_id.
        survivor_of: Mapping returned by detect_duplicates.
        entity: Sponsor, family, or child for note wording.

    Returns:
        The survivor-naming note, or None for a nonduplicate row.
    """
    survivor = survivor_of.get(record_id)
    if survivor is None:
        return None
    return (
        f"Potential duplicate {entity} submission; resolve against"
        f" submission {survivor.get('zite_record_id')} in the audit"
        " tool (see duplicate_log)"
    )


def append_note(existing: str | None, addition: str | None) -> str | None:
    """Append a clarification note once, preserving existing text.

    Args:
        existing: Current note text, possibly null.
        addition: Note to append, possibly null.

    Returns:
        The idempotently merged note text.
    """
    if not addition:
        return existing
    text = (existing or "").strip()
    if not text:
        return addition
    if addition in text:
        return text
    return f"{text}; {addition}"


def _record_id(row: dict[str, Any]) -> str:
    """Return a normalized source-record join key.

    Args:
        row: A raw, prepared, or final row.

    Returns:
        The stripped zite_record_id, or an empty string when absent.
    """
    value = row.get("zite_record_id")
    return "" if value is None else str(value).strip()


def _assigned(value: Any) -> bool:
    """Report whether a nullable stable-ID cell contains an assignment.

    Args:
        value: Candidate ID value.

    Returns:
        False for None and blank strings, True for every other value.
    """
    return value is not None and (
        not isinstance(value, str) or bool(value.strip())
    )


def _numeric_id(value: Any) -> int | None:
    """Convert an integral workbook ID to an integer when possible.

    Args:
        value: Candidate numeric entity ID.

    Returns:
        The integer value, or None for null, blank, or nonintegral values.
    """
    if not _assigned(value) or isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip())
    except ValueError:
        return None
    return int(number) if number.is_integer() else None


def _stable_entity_id(value: Any) -> Any:
    """Normalize integral workbook IDs while leaving other values untouched.

    Args:
        value: Existing sponsor_id or family_id.

    Returns:
        An integer for an integral numeric value, otherwise the original value.
    """
    numeric = _numeric_id(value)
    return numeric if numeric is not None else value


def _entity_id_sort_key(value: Any) -> tuple[int, Any]:
    """Build a deterministic cross-type entity-ID ordering key.

    Args:
        value: Existing sponsor_id or family_id.

    Returns:
        Numeric IDs ordered numerically before nonnumeric IDs ordered as text.
    """
    numeric = _numeric_id(value)
    if numeric is not None:
        return (0, numeric)
    return (1, str(value))


def _existing_by_record(
    rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Index existing source-backed rows by normalized zite_record_id.

    Args:
        rows: Existing final rows.

    Returns:
        The first row for each nonblank record ID. Full final-key/schema
        validation remains the persistent store's responsibility.
    """
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        record_id = _record_id(row)
        if record_id:
            indexed.setdefault(record_id, row)
    return indexed


def _maximum_existing_id(rows: list[dict[str, Any]], column: str) -> int:
    """Find the greatest existing numeric entity ID.

    Args:
        rows: Existing final rows, including raw-missing rows.
        column: Sponsor_id or family_id.

    Returns:
        The maximum integer ID, or zero when no numeric ID exists.
    """
    values = [_numeric_id(row.get(column)) for row in rows]
    return max((value for value in values if value is not None), default=0)


def _reconcile_sponsor_ids(
    provisional: list[dict[str, Any]], existing: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Retain source-matched sponsor IDs and append genuinely new IDs.

    Args:
        provisional: Rows returned by acquire.assign_sponsor_ids.
        existing: Existing final sponsors, including raw-missing rows.

    Returns:
        Prepared sponsor rows with stable IDs; existing rows are not mutated.
    """
    existing_by_record = _existing_by_record(existing)
    reconciled = [dict(row) for row in provisional]
    new_rows: list[dict[str, Any]] = []
    for row in reconciled:
        prior = existing_by_record.get(_record_id(row))
        if prior is not None and _assigned(prior.get("sponsor_id")):
            row["sponsor_id"] = _stable_entity_id(prior.get("sponsor_id"))
        else:
            new_rows.append(row)
    next_id = _maximum_existing_id(existing, "sponsor_id") + 1
    for row in sorted(
        new_rows,
        key=lambda item: (
            _created_value(item) or "",
            _record_id(item),
        ),
    ):
        row["sponsor_id"] = next_id
        next_id += 1
    return reconciled


def _family_survivor(group: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the deterministic earliest family submission in one group.

    Args:
        group: Current prepared family submissions sharing a provisional ID.

    Returns:
        The earliest created/zite_record_id submission.
    """
    return min(
        group,
        key=lambda row: (_created_value(row) or "", _record_id(row)),
    )


def _normalized_email(value: Any) -> str:
    """Normalize an email for stable family fallback matching.

    Args:
        value: Raw or existing primary_email value.

    Returns:
        Lowercased and stripped email text, or an empty string when absent.
    """
    return "" if value is None else str(value).strip().lower()


def _family_group_sort_key(
    group: list[dict[str, Any]],
) -> tuple[str, str, str]:
    """Build deterministic creation/source order for a family group.

    Args:
        group: Current duplicate/singleton family submission group.

    Returns:
        Earliest created timestamp, survivor source ID, then normalized email.
    """
    survivor = _family_survivor(group)
    return (
        str(_created_value(survivor) or ""),
        _record_id(survivor),
        _normalized_email(survivor.get("primary_email")),
    )


def _reconcile_family_ids(
    provisional: list[dict[str, Any]], existing: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Retain one existing entity ID per current family submission group.

    Source-ID matches take precedence, with the current survivor's entity
    preferred when several existing entities match. Normalized-email fallback
    cannot consume an existing source row belonging to another current group.
    New groups receive IDs above the maximum existing ID.

    Args:
        provisional: Rows returned by acquire.assign_family_ids.
        existing: Existing final families, including unmatched rows.

    Returns:
        Prepared family rows with stable group-wide family IDs.
    """
    groups_by_provisional: dict[Any, list[dict[str, Any]]] = {}
    for row in provisional:
        groups_by_provisional.setdefault(row.get("family_id"), []).append(
            dict(row)
        )
    groups = sorted(groups_by_provisional.values(), key=_family_group_sort_key)
    group_of_record = {
        _record_id(row): index
        for index, group in enumerate(groups)
        for row in group
        if _record_id(row)
    }
    matched: dict[int, Any] = {}
    claimed_family_ids: set[Any] = set()

    # Direct source-ID reconciliation. A group maps to exactly one existing
    # family even if legacy state contains candidates for several submissions.
    for index, group in enumerate(groups):
        current_ids = {_record_id(row) for row in group}
        survivor_id = _record_id(_family_survivor(group))
        candidates: list[tuple[dict[str, Any], Any]] = []
        for prior in existing:
            family_id = prior.get("family_id")
            if not _assigned(family_id):
                continue
            stable_id = _stable_entity_id(family_id)
            if stable_id in claimed_family_ids:
                continue
            if _record_id(prior) in current_ids:
                candidates.append((prior, stable_id))
        if not candidates:
            continue
        preferred = [
            candidate
            for candidate in candidates
            if _record_id(candidate[0]) == survivor_id
        ]
        selected = min(
            preferred or candidates,
            key=lambda candidate: _entity_id_sort_key(candidate[1]),
        )[1]
        matched[index] = selected
        claimed_family_ids.add(selected)

    # Email fallback is deliberately narrower: a final row whose source now
    # belongs to another current group cannot be pulled into this group.
    for index, group in enumerate(groups):
        if index in matched:
            continue
        email = _normalized_email(_family_survivor(group).get("primary_email"))
        if not email:
            continue
        candidates: list[Any] = []
        for prior in existing:
            family_id = prior.get("family_id")
            if not _assigned(family_id):
                continue
            stable_id = _stable_entity_id(family_id)
            if stable_id in claimed_family_ids:
                continue
            if _normalized_email(prior.get("primary_email")) != email:
                continue
            current_group = group_of_record.get(_record_id(prior))
            if current_group is not None and current_group != index:
                continue
            candidates.append(stable_id)
        if candidates:
            selected = min(candidates, key=_entity_id_sort_key)
            matched[index] = selected
            claimed_family_ids.add(selected)

    next_id = _maximum_existing_id(existing, "family_id") + 1
    for index, group in enumerate(groups):
        family_id = matched.get(index)
        if family_id is None:
            family_id = next_id
            next_id += 1
        for row in group:
            row["family_id"] = family_id
    return [row for group in groups for row in group]


def _child_group_key(row: dict[str, Any]) -> tuple[Any, str, str] | None:
    """Return the effective duplicate identity used for angel reservations.

    Args:
        row: Existing or prepared child with an effective family_id.

    Returns:
        Family/email/name identity, or None for an orphan or unkeyed child.
        Unkeyed children are never duplicate-grouped by acquire.
    """
    family_id = row.get("family_id")
    email = row.get("primary_email")
    first_name = row.get("first_name")
    if not _assigned(family_id) or not _assigned(email):
        return None
    if not _assigned(first_name):
        return None
    return (_stable_entity_id(family_id), str(email), str(first_name))


def _reconcile_child_ids(
    provisional: list[dict[str, Any]], existing: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Stabilize child family linkage and angel IDs without transfers.

    Existing non-null family and angel assignments remain on their original
    submissions. Every existing angel, including raw-missing and old duplicate
    members, is reserved. Only provisional duplicate survivors with no angel
    owner elsewhere in their effective duplicate group receive a new lowest
    unused family/letter ID.

    Args:
        provisional: Rows returned by acquire.assign_angel_ids after stable
            sponsor/family allocation.
        existing: Existing final children, including raw-missing rows.

    Returns:
        Prepared children with effective stable linkage and angel IDs.
    """
    existing_by_record = _existing_by_record(existing)
    reconciled: list[dict[str, Any]] = []
    provisional_survivor: dict[str, bool] = {}
    for source in provisional:
        row = dict(source)
        record_id = _record_id(row)
        provisional_survivor[record_id] = _assigned(source.get("angel_id"))
        prior = existing_by_record.get(record_id)
        if prior is not None:
            if _assigned(prior.get("family_id")):
                row["family_id"] = _stable_entity_id(prior.get("family_id"))
            if _assigned(prior.get("angel_id")):
                # Never move or re-letter an existing angel, even if the
                # current duplicate-survivor calculation chose another row.
                row["angel_id"] = prior.get("angel_id")
                reconciled.append(row)
                continue
        # A provisional string is eligibility evidence only. Allocation below
        # uses the effective stable family and global existing reservations.
        row["angel_id"] = None
        reconciled.append(row)

    reserved_angels = {
        str(row.get("angel_id")).strip()
        for row in existing
        if _assigned(row.get("angel_id"))
    }
    current_by_record = {
        _record_id(row): row for row in reconciled if _record_id(row)
    }
    # Angel ownership follows the current raw submission for a stored owner,
    # even when a human corrected the owner's email/name. Stored identity is
    # used only when that owner is absent from current raw.
    owned_groups: set[tuple[Any, str, str]] = set()
    for prior in existing:
        if not _assigned(prior.get("angel_id")):
            continue
        current = current_by_record.get(_record_id(prior))
        ownership_row = current if current is not None else prior
        group_key = _child_group_key(ownership_row)
        if group_key is not None:
            owned_groups.add(group_key)

    candidates_by_family: dict[Any, list[dict[str, Any]]] = {}
    for row in reconciled:
        record_id = _record_id(row)
        if _assigned(row.get("angel_id")):
            continue
        if not provisional_survivor.get(record_id, False):
            continue
        if not _assigned(row.get("family_id")):
            continue
        group_key = _child_group_key(row)
        if group_key is not None and group_key in owned_groups:
            # A new duplicate cannot steal the established group's angel.
            continue
        candidates_by_family.setdefault(row["family_id"], []).append(row)

    for family_id in sorted(candidates_by_family, key=_entity_id_sort_key):
        candidates = sorted(
            candidates_by_family[family_id],
            key=lambda row: (
                -(row.get("age_at_xmas") or 0),
                row.get("created") or "",
                row.get("dob") or "",
                _record_id(row),
            ),
        )
        letter_index = 0
        for row in candidates:
            while True:
                candidate = f"{family_id}{index_to_letters(letter_index)}"
                letter_index += 1
                if candidate not in reserved_angels:
                    break
            row["angel_id"] = candidate
            reserved_angels.add(candidate)
    return reconciled


def prepare_rows(
    raw_rows: dict[str, list[dict[str, Any]]],
    cfg: Config,
    existing: dict[str, list[dict[str, Any]]] | None = None,
    report: bool = True,
) -> Preparation:
    """Run pure local preparation and optional persistent-ID reconciliation.

    All raw IDs are validated before cleaning starts. Without existing finals,
    this reproduces screening's original clean, duplicate, provisional-ID,
    created-cross-check, angel, and DOB sequence. With existing finals, stable
    sponsor/family/child linkage is reconciled before the final DOB diagnostics
    are computed. Inputs are never mutated and no workbook or external service
    is touched.

    Args:
        raw_rows: Verbatim rows keyed by sponsor, family, and child.
        cfg: Pipeline configuration.
        existing: Optional existing final rows keyed the same way.
        report: Emit duplicate/DOB-conflict warnings when True; call sites
            that reuse the diagnostics silently pass False.

    Returns:
        Prepared rows and all local diagnostics.

    Raises:
        PreparationError: If a required table is absent or any raw source ID is
            missing, blank, or duplicated.
    """
    for table_name in _TABLE_NAMES:
        if table_name not in raw_rows:
            raise PreparationError(
                f"raw_{table_name} rows are missing; run `ccat acquire` first"
            )
        _validate_raw_ids(raw_rows[table_name], table_name)

    prepared: dict[str, list[dict[str, Any]]] = {}
    validation_notes: dict[str, dict[str, list[str]]] = {}
    for table_name in _TABLE_NAMES:
        cleaned, notes = clean_rows(raw_rows[table_name], table_name, cfg)
        prepared[table_name] = cleaned
        validation_notes[table_name] = notes
        logger.info(
            "table %s: %d submission(s) cleaned (%d flagged)",
            table_name,
            len(cleaned),
            len(notes),
        )

    duplicate_log: list[dict[str, Any]] = []
    survivors: dict[str, dict[str, dict[str, Any]]] = {}
    for table_name in _TABLE_NAMES:
        log_rows, survivor_of = detect_duplicates(
            prepared[table_name], table_name, report=report
        )
        duplicate_log.extend(log_rows)
        survivors[table_name] = survivor_of

    prepared["sponsor"] = assign_sponsor_ids(prepared["sponsor"])
    prepared["family"] = assign_family_ids(prepared["family"])
    if existing is not None:
        prepared["sponsor"] = _reconcile_sponsor_ids(
            prepared["sponsor"], existing.get("sponsor", [])
        )
        prepared["family"] = _reconcile_family_ids(
            prepared["family"], existing.get("family", [])
        )
    prepared["child"] = assign_angel_ids(
        prepared["child"], cfg, prepared["family"]
    )
    if existing is not None:
        prepared["child"] = _reconcile_child_ids(
            prepared["child"], existing.get("child", [])
        )
    duplicate_log.extend(
        detect_sponsor_claim_contests(prepared["sponsor"], prepared["child"])
    )
    _cross_check_created(prepared["family"], "family")
    _cross_check_created(prepared["child"], "child")
    dob_log = sibling_dob_conflicts(prepared["child"], cfg, report=report)
    return Preparation(
        rows=prepared,
        validation_notes=validation_notes,
        duplicate_log=duplicate_log,
        survivors=survivors,
        dob_log=dob_log,
    )


def _workbook_sheet(cfg: Config, sheet: str) -> pl.DataFrame | None:
    """Read one workbook sheet and normalize read failures.

    Args:
        cfg: Pipeline configuration containing excel_file.
        sheet: Workbook sheet name.

    Returns:
        The untyped sheet frame, or None when workbook/sheet is absent.

    Raises:
        PreparationError: If the workbook reader itself fails.
    """
    try:
        return read_sheet(cfg.excel_file, sheet)
    except Exception as error:
        raise PreparationError(f"could not read {sheet}: {error}") from error


def prepare_workbook(
    cfg: Config,
    existing: dict[str, list[dict[str, Any]]] | None = None,
    report: bool = True,
) -> Preparation:
    """Read required workbook inputs and prepare them without any writes.

    Required raw sheets and configured columns are checked explicitly. If
    existing is None, present final sheets are loaded as untyped dictionaries.
    Legacy adj rows cannot stand in for persistent finals: an adj sheet without
    its corresponding final sheet requires the explicit consolidation migration
    first. A supplied existing mapping bypasses all final/adj reads.

    Args:
        cfg: Pipeline configuration containing excel_file and table schemas.
        existing: Optional existing final rows forwarded to prepare_rows.
        report: Forwarded to prepare_rows; False silences conflict warnings.

    Returns:
        The pure preparation result for the workbook's current raw/final rows.

    Raises:
        PreparationError: If a raw sheet/config column or raw ID is invalid, or
            an adj sheet exists while its final counterpart is absent.
    """
    raw_rows: dict[str, list[dict[str, Any]]] = {}
    for table_name in _TABLE_NAMES:
        sheet = f"raw_{table_name}"
        frame = _workbook_sheet(cfg, sheet)
        if frame is None:
            raise PreparationError(
                f"{sheet} sheet is missing; run `ccat acquire` first"
            )
        missing = [
            key
            for key in cfg.tables[table_name].columns
            if key not in frame.columns
        ]
        if "zite_record_id" not in frame.columns and (
            "zite_record_id" not in missing
        ):
            missing.append("zite_record_id")
        if missing:
            raise PreparationError(
                f"{sheet} lacks column(s) {missing}; run `ccat acquire` first"
            )
        raw_rows[table_name] = frame.to_dicts()

    loaded_existing = existing
    if loaded_existing is None:
        loaded_existing = {}
        for table_name in _TABLE_NAMES:
            final_sheet = f"final_{table_name}"
            final = _workbook_sheet(cfg, final_sheet)
            if final is not None:
                loaded_existing[table_name] = final.to_dicts()
                continue
            adj = _workbook_sheet(cfg, f"adj_{table_name}")
            if adj is not None:
                raise PreparationError(
                    f"adj_{table_name} exists but {final_sheet} is absent;"
                    " run `ccat consolidate-migrate` before preparation so"
                    " legacy audit rows are consolidated into persistent"
                    " final sheets"
                )
            loaded_existing[table_name] = []
    return prepare_rows(raw_rows, cfg, loaded_existing, report=report)


def _dob_conflict_note(
    log_rows: list[dict[str, Any]] | None, zite_record_id: str
) -> str | None:
    """Format all DOB notes for one child using legacy store wording.

    Args:
        log_rows: Sibling-conflict log rows, or None.
        zite_record_id: The child's source record ID.

    Returns:
        One note per distinct conflicting sibling joined by semicolons, or None.
    """
    notes: list[str] = []
    seen_siblings: set[Any] = set()
    for row in log_rows or []:
        for side in ("a", "b"):
            if row.get(f"child_{side}_zite_record_id") != zite_record_id:
                continue
            other = "b" if side == "a" else "a"
            sibling_id = row.get(f"child_{other}_zite_record_id")
            if sibling_id in seen_siblings:
                break
            seen_siblings.add(sibling_id)
            notes.append(
                "Sibling DOB conflict: born"
                f" {row.get('gap_months')} month(s)"
                f" ({row.get('gap_days')} day(s)) apart from sibling"
                f" {row.get(f'child_{other}_first_name')}"
                f" ({row.get(f'child_{other}_angel_id')},"
                f" dob {row.get(f'child_{other}_dob')})"
            )
            break
    return "; ".join(notes) if notes else None


def dob_notes_by_child(dob_log_rows: list[dict[str, Any]]) -> dict[str, str]:
    """Collect one legacy-formatted DOB-conflict note per affected child.

    Args:
        dob_log_rows: Sibling-conflict rows from sibling_dob_conflicts.

    Returns:
        Mapping of child zite_record_id to the exact old audit-store note text.
    """
    notes: dict[str, str] = {}
    for row in dob_log_rows:
        for side in ("a", "b"):
            record_id = row.get(f"child_{side}_zite_record_id")
            if record_id is None:
                continue
            note = _dob_conflict_note(dob_log_rows, str(record_id))
            if note is not None:
                notes[str(record_id)] = note
    return notes
