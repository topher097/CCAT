"""Module 1 — data acquisition and validation (§5).

Pulls the sponsor/family/child tables from Zite, validates and cleans them
against the config schema, deduplicates families, assigns stable IDs
(``family_id``, ``sponsor_id``, ``angel_id``) and derived columns
(``age_at_xmas``, ``pref_age_min``/``pref_age_max``), and persists the
``raw_*`` sheets plus the ``dedup_log`` sheet recording every merged
duplicate (§5.4) and the ``dob_conflict_log`` sheet recording same-family
children born too close together (§5.6).  Never touches any other sheet
(§5.7).
"""

import json
import logging
import re
from datetime import date, datetime, UTC
from difflib import get_close_matches as _get_close_matches
from typing import Any

import pandera.polars as pap
import polars as pl
from pandera import Check
from pandera.errors import SchemaErrors
from polars._typing import PolarsDataType

from ccat import zite
from ccat.config import ColumnSpec, Config
from ccat.io import sheets_unchanged, snapshot_sheets, write_sheets

logger = logging.getLogger(__name__)


class AcquisitionError(Exception):
    """Raised when acquisition cannot complete (structural failure)."""


#: Columns excluded from dedup conflict detection (§5.4): identity and
#: ordering metadata that duplicate records necessarily differ on.
DEDUP_CONFLICT_EXCLUDED = frozenset(
    {"zite_record_id", "created", "zite_created_at"}
)


#: Exact mapping from the `age_preference` vocabulary to age bounds (§5.5).
AGE_PREF_BOUNDS: dict[str, tuple[int, int]] = {
    "Any Age": (0, 18),
    "Newborn through Toddler (0-3 yrs)": (0, 3),
    "Pre-K through Grade 5 (4-10 yrs)": (4, 10),
    "Middle School (11-13 yrs)": (11, 13),
    "High School (14-18+ yrs)": (14, 18),
    "Special Needs (any age)": (0, 18),
}

#: The age-preference label that marks a special-needs sponsor (§5.5, §8.2).
SPECIAL_NEEDS_LABEL = "Special Needs (any age)"

#: Zite system columns mapped onto config keys (§5.1).
SYSTEM_COLUMN_MAP = {"zite_record_id": "id", "zite_created_at": "createdAt"}
#: Table-specific system mappings: when the sponsor table has no
#: user-facing created column, its created timestamp is the Zite system
#: `createdAt` (§5.1). Applies to both possible config key spellings.
TABLE_SYSTEM_OVERRIDES = {
    "sponsor": {"created": "createdAt", "create": "createdAt"}
}

#: polars dtypes per declared column type (§5.2).
PL_DTYPES: dict[str, PolarsDataType] = {
    "string": pl.String,
    "name": pl.String,
    "email": pl.String,
    "phone_us": pl.String,
    "int": pl.Int64,
    "float": pl.Float64,
    "bool": pl.Boolean,
    "date": pl.String,
    "datetime": pl.String,
    "enum": pl.String,
}

#: RFC 5322 subset used for email validation (§5.3).
EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$"
)

#: Derived columns appended to each raw sheet, in sheet column order.
DERIVED_COLUMNS = {
    "sponsor": ["sponsor_id", "pref_age_min", "pref_age_max"],
    "family": ["family_id"],
    "child": ["family_id", "angel_id", "age_at_xmas"],
}


def snake_case(name: str) -> str:
    """Normalize a column name to lower snake_case (§3).

    Args:
        name: The source column name.

    Returns:
        The lower snake_case form.
    """
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()


def _find_source_column(
    sources: list[str], key: str, spec: ColumnSpec
) -> str | None:
    """Locate the source column feeding one config key.

    Matching order: exact rename, case-insensitive rename, snake_case
    rename; then a unique close fuzzy match (tolerates a corrected typo in
    the Zite form label, logged); without a rename, the source column
    whose snake_case form equals the key.

    Args:
        sources: Source column names from the pulled table.
        key: The config column key.
        spec: The column spec.

    Returns:
        The matching source column name, or None when absent.
    """
    if spec.rename:
        candidates = [spec.rename]
    else:
        candidates = [key]
    normalized_sources = {snake_case(source): source for source in sources}
    for candidate in candidates:
        if candidate in sources:
            return candidate
        lowered = {source.lower(): source for source in sources}
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
        normalized = snake_case(candidate)
        if normalized in normalized_sources:
            return normalized_sources[normalized]
    # Fuzzy fallback: one clearly-close label still maps (e.g. the config
    # says "Angels" while the live form label reads "Angles").
    close = _get_close_matches(
        normalized, list(normalized_sources), n=2, cutoff=0.85
    )
    if len(close) == 1:
        logger.info(
            "fuzzy column match: %r -> %r (config key %r)",
            candidates[0],
            normalized_sources[close[0]],
            key,
        )
        return normalized_sources[close[0]]
    return None


def import_table(
    frame: pl.DataFrame, table_name: str, cfg: Config
) -> list[dict[str, Any]]:
    """Apply the config allow-list, renames, and system mappings (§5.1).

    Args:
        frame: Raw records from ``zite.get_table_data``.
        table_name: One of "sponsor", "family", "child".
        cfg: Pipeline configuration.

    Returns:
        One dict per record, keyed by config column keys.

    Raises:
        AcquisitionError: If a required source column is missing
            (structural failure, §5.3).
    """
    specs = cfg.tables[table_name].columns
    sources = list(frame.columns)
    if frame.is_empty() and not sources:
        return []
    mapping: dict[str, str] = {}
    missing: list[str] = []
    for key, spec in specs.items():
        source: str | None = None
        if key in SYSTEM_COLUMN_MAP and SYSTEM_COLUMN_MAP[key] in sources:
            source = SYSTEM_COLUMN_MAP[key]
        else:
            source = _find_source_column(sources, key, spec)
        if source is None:
            # Fallback for the sponsor created column: the Zite system
            # createdAt (§5.1) when the user-facing column is absent.
            fallback = TABLE_SYSTEM_OVERRIDES.get(table_name, {}).get(key)
            if fallback is not None and fallback in sources:
                source = fallback
        if source is None:
            missing.append(spec.rename or key)
        else:
            mapping[key] = source
    if missing:
        raise AcquisitionError(
            f"table {table_name!r}: missing required source columns: {missing}"
        )
    rows: list[dict[str, Any]] = []
    for record in frame.to_dicts():
        rows.append(
            {key: record.get(source) for key, source in mapping.items()}
        )
    return rows


def clean_value(value: Any, spec: ColumnSpec) -> tuple[Any, str | None]:
    """Clean one cell value per its declared type (§5.3).

    Args:
        value: Raw value (may be None).
        spec: The column spec.

    Returns:
        A tuple of (cleaned value, failure reason or None).
    """
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None, None
    if spec.type == "string":
        return str(value).strip(), None
    if spec.type == "name":
        return str(value).strip().title(), None
    if spec.type == "email":
        cleaned = str(value).strip().lower()
        if EMAIL_RE.fullmatch(cleaned):
            return cleaned, None
        return value, "invalid email"
    if spec.type == "phone_us":
        digits = re.sub(r"\D", "", str(value))
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) == 10:
            return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}", None
        return value, "not a 10-digit US phone number"
    if spec.type in {"int", "float"}:
        try:
            return (int if spec.type == "int" else float)(
                str(value).strip()
            ), None
        except ValueError:
            return value, f"not a valid {spec.type}"
    if spec.type == "bool":
        if isinstance(value, bool):
            return value, None
        text = str(value).strip().lower()
        if text in {"true", "yes", "1"}:
            return True, None
        if text in {"false", "no", "0"}:
            return False, None
        return value, "not a boolean"
    if spec.type == "date":
        text = str(value).strip()
        for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
            try:
                return datetime.strptime(text, fmt).date().isoformat(), None
            except ValueError:
                continue
        return value, "not MM/DD/YYYY or ISO date"
    if spec.type == "datetime":
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return value, "not ISO 8601"
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(UTC)
        return parsed.isoformat(), None
    if spec.type == "enum":
        return value, None
    return value, None


def _parse_reported_age(value: Any) -> int | None:
    """Coerce the free-text reported age to an integer (§5.5).

    Families type values like ``4 turning 5`` or ``2.5`` into the
    optional cross-check field; the computed ``age_at_xmas`` is the
    authoritative age, so unparseable values degrade to null (logged)
    instead of rejecting the whole child row.

    Args:
        value: The raw age_reported value.

    Returns:
        The coerced integer, or None when no number is present.
    """
    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        pass
    match = re.match(r"\s*(\d+)", text)
    if match is not None:
        coerced = int(match.group(1))
        logger.info("age_reported %r coerced to %d", text, coerced)
        return coerced
    logger.info("age_reported %r is not numeric; using null", text)
    return None


def process_table(
    frame: pl.DataFrame, table_name: str, cfg: Config
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Import, clean, and validate one table (§5.1–§5.3).

    Family rows are deduplicated (§5.4) on cleaned rows before validation so
    that coalescing can fill nulls a duplicate row would otherwise fail on.

    Args:
        frame: Raw records from ``zite.get_table_data``.
        table_name: One of "sponsor", "family", "child".
        cfg: Pipeline configuration.

    Returns:
        A tuple of (kept rows, rejected-row records, dedup-log rows) where
        each rejected record has table, index, column, value, and reason
        keys; the dedup log is empty except for the family table.
    """
    rows = import_table(frame, table_name, cfg)
    specs = cfg.tables[table_name].columns
    cleaned: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        out: dict[str, Any] = {}
        failures: list[tuple[str, Any, str]] = []
        for key, spec in specs.items():
            value = row.get(key)
            # Blank strings are nulls (§5.3); normalize before the
            # default fill so empty form fields still take the default.
            if isinstance(value, str) and value.strip() == "":
                value = None
            if value is None and spec.has_default:
                value = spec.default
            if table_name == "child" and key == "age_reported":
                if value is not None:
                    value = _parse_reported_age(value)
            cleaned_value, reason = clean_value(value, spec)
            if reason is not None:
                failures.append((key, value, reason))
            out[key] = cleaned_value
        if failures:
            for key, value, reason in failures:
                rejected.append(
                    {
                        "table": table_name,
                        "index": index,
                        "column": key,
                        "value": value,
                        "reason": reason,
                    }
                )
        else:
            cleaned.append(out)
    if table_name == "family":
        cleaned, dedup_log = dedup_families(cleaned, cfg)
    else:
        dedup_log = []
    kept, schema_rejected = validate_rows(cleaned, table_name, cfg)
    rejected.extend(schema_rejected)
    return kept, rejected, dedup_log


def dedup_families(
    rows: list[dict[str, Any]], cfg: Config
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Coalesce duplicate family rows by primary_email (§5.4).

    Rows are ordered by ``created`` ascending; for each column the first
    non-null value wins, conflicting non-null values are logged, and the
    surviving row keeps the earliest ``created`` timestamp.

    Args:
        rows: Cleaned family rows.
        cfg: Pipeline configuration (unused, kept for signature symmetry).

    Returns:
        A tuple of (deduplicated rows, dedup-log rows).  One log row is
        produced per merged duplicate record, carrying the primary email,
        the surviving and duplicate zite ids, the conflicting columns,
        the conflicting values, and the full recorded contents of both
        records as JSON.
    """
    del cfg  # signature symmetry only
    ordered = sorted(
        rows,
        key=lambda row: (
            row.get("created") or "",
            row.get("zite_record_id") or "",
        ),
    )
    groups: dict[str, list[dict[str, Any]]] = {}
    no_email: list[dict[str, Any]] = []
    for row in ordered:
        email = row.get("primary_email")
        if email is None:
            # No email: nothing to deduplicate on; keep the row as-is so
            # email-less families are never merged with each other.
            no_email.append(row)
            continue
        groups.setdefault(email, []).append(row)
    merged_rows: list[dict[str, Any]] = []
    log_rows: list[dict[str, Any]] = []
    for email, group in groups.items():
        merged = dict(group[0])
        for key in merged:
            values = [row.get(key) for row in group if row.get(key) is not None]
            if not values:
                merged[key] = None
                continue
            merged[key] = values[0]
            if key in DEDUP_CONFLICT_EXCLUDED:
                continue
            distinct = {str(value) for value in values}
            if len(distinct) > 1:
                logger.warning(
                    "family dedup conflict for %s: column %s kept %r, dropped %r",
                    email,
                    key,
                    values[0],
                    values[1:],
                )
        merged_rows.append(merged)
        if len(group) > 1:
            log_rows.extend(_dedup_log_rows("family", email, merged, group[1:]))
    merged_rows.extend(no_email)
    merged_rows.sort(
        key=lambda row: (
            row.get("created") or "",
            row.get("zite_record_id") or "",
        )
    )
    return merged_rows, log_rows


def _dedup_log_rows(
    sheet: str,
    email: Any,
    merged: dict[str, Any],
    duplicates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build one dedup-log row per merged duplicate record (§5.4).

    Args:
        sheet: Name of the raw sheet the records came from.
        email: The primary_email key the group was merged on.
        merged: The surviving (coalesced) row as written to the sheet.
        duplicates: The dropped duplicate rows, in merge order.

    Returns:
        Log rows with the kept/duplicate ids and created timestamps, the
        conflicting columns (duplicate value differs from the surviving
        value, both non-null), a human-readable detail string, and the
        full recorded contents of both records as JSON.
    """
    log_rows: list[dict[str, Any]] = []
    for duplicate in duplicates:
        conflicts = [
            key
            for key in merged
            if key not in DEDUP_CONFLICT_EXCLUDED
            and merged.get(key) is not None
            and duplicate.get(key) is not None
            and str(duplicate.get(key)) != str(merged.get(key))
        ]
        details = "; ".join(
            f"{key}: kept {merged.get(key)!r} / duplicate"
            f" {duplicate.get(key)!r}"
            for key in conflicts
        )
        log_rows.append(
            {
                "sheet": sheet,
                "primary_email": email,
                "kept_zite_record_id": merged.get("zite_record_id"),
                "kept_created": merged.get("created"),
                "duplicate_zite_record_id": duplicate.get("zite_record_id"),
                "duplicate_created": duplicate.get("created"),
                "conflict_columns": ",".join(conflicts),
                "conflict_details": details,
                "kept_record": json.dumps(merged, default=str, sort_keys=True),
                "duplicate_record": json.dumps(
                    duplicate, default=str, sort_keys=True
                ),
            }
        )
    return log_rows


#: Column schema of the ``dedup_log`` sheet (§5.4).
DEDUP_LOG_SCHEMA: dict[str, PolarsDataType] = {
    "sheet": pl.String,
    "primary_email": pl.String,
    "kept_zite_record_id": pl.String,
    "kept_created": pl.String,
    "duplicate_zite_record_id": pl.String,
    "duplicate_created": pl.String,
    "conflict_columns": pl.String,
    "conflict_details": pl.String,
    "kept_record": pl.String,
    "duplicate_record": pl.String,
}


def dedup_log_frame(log_rows: list[dict[str, Any]]) -> pl.DataFrame:
    """Build the ``dedup_log`` sheet frame (§5.4).

    Args:
        log_rows: Log rows from :func:`dedup_families`.

    Returns:
        The typed frame (empty when nothing was deduplicated).
    """
    return pl.DataFrame(log_rows, schema=DEDUP_LOG_SCHEMA)


#: Same-family children whose birth dates are at most
#: ``cfg.acquire.sibling_dob_max_gap_days`` apart are recorded in
#: ``dob_conflict_log`` (§5.6).  The default (274) is ~9 months.
def _dob_gap_months(earlier: date, later: date) -> int:
    """Count completed calendar months between two dates (§5.6).

    Reporting only: the conflict threshold is day-based, from
    ``cfg.acquire.sibling_dob_max_gap_days``.

    Args:
        earlier: The earlier date.
        later: The later date.

    Returns:
        Whole months between the dates; two equal dates yield 0.
    """
    months = (later.year - earlier.year) * 12 + (later.month - earlier.month)
    if later.day < earlier.day:
        months -= 1
    return max(months, 0)


#: Column schema of the ``dob_conflict_log`` sheet (§5.6).
DOB_CONFLICT_LOG_SCHEMA: dict[str, PolarsDataType] = {
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


def sibling_dob_conflicts(
    rows: list[dict[str, Any]], cfg: Config
) -> list[dict[str, Any]]:
    """Find same-family children born too close together (§5.6).

    Every unordered pair of children sharing a ``family_id`` whose birth
    dates are at most ``cfg.acquire.sibling_dob_max_gap_days`` days
    apart (twins included) is recorded for human review -- each pair is
    compared and logged exactly once (``child_a`` is the earlier birth,
    so no swapped duplicate rows are ever written).  Children without a
    ``family_id`` (orphans) or without a ``dob`` are skipped.

    Args:
        rows: Cleaned, validated child rows with ``family_id`` and
            ``angel_id`` assigned.
        cfg: Pipeline configuration (threshold in days).

    Returns:
        One log row per conflicting pair, ordered deterministically
        (family, then birth date, then record id).
    """
    max_gap_days = cfg.acquire.sibling_dob_max_gap_days
    by_family: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("family_id") is None or row.get("dob") is None:
            continue
        by_family.setdefault(row["family_id"], []).append(row)
    seen: set[frozenset[Any]] = set()
    log_rows: list[dict[str, Any]] = []
    for family_id in sorted(by_family, key=lambda value: str(value)):
        ordered = sorted(
            by_family[family_id],
            key=lambda row: (row["dob"], row.get("zite_record_id") or ""),
        )
        for index, child_a in enumerate(ordered):
            for child_b in ordered[index + 1 :]:
                if child_a.get("zite_record_id") == child_b.get(
                    "zite_record_id"
                ):
                    continue  # duplicated submission, not two children
                dob_a = date.fromisoformat(str(child_a["dob"]))
                dob_b = date.fromisoformat(str(child_b["dob"]))
                gap_days = abs((dob_b - dob_a).days)
                if gap_days > max_gap_days:
                    continue
                # Unordered-pair key: a duplicate submission (the same
                # pair reaching the loop twice) never logs twice.
                pair = frozenset(
                    (
                        child_a.get("zite_record_id"),
                        child_b.get("zite_record_id"),
                    )
                )
                if pair in seen:
                    continue
                seen.add(pair)
                log_rows.append(
                    {
                        "family_id": family_id,
                        "primary_email": child_a.get("primary_email"),
                        "child_a_zite_record_id": child_a.get("zite_record_id"),
                        "child_a_angel_id": child_a.get("angel_id"),
                        "child_a_first_name": child_a.get("first_name"),
                        "child_a_dob": child_a.get("dob"),
                        "child_b_zite_record_id": child_b.get("zite_record_id"),
                        "child_b_angel_id": child_b.get("angel_id"),
                        "child_b_first_name": child_b.get("first_name"),
                        "child_b_dob": child_b.get("dob"),
                        "gap_months": _dob_gap_months(dob_a, dob_b),
                        "gap_days": gap_days,
                    }
                )
                logger.warning(
                    "sibling dob conflict for family %s: %s (%s, dob %s) and"
                    " %s (%s, dob %s) are %d day(s) apart",
                    family_id,
                    child_a.get("first_name"),
                    child_a.get("angel_id"),
                    child_a.get("dob"),
                    child_b.get("first_name"),
                    child_b.get("angel_id"),
                    child_b.get("dob"),
                    gap_days,
                )
    return log_rows


def dob_conflict_log_frame(log_rows: list[dict[str, Any]]) -> pl.DataFrame:
    """Build the ``dob_conflict_log`` sheet frame (§5.6).

    Args:
        log_rows: Log rows from :func:`sibling_dob_conflicts`.

    Returns:
        The typed frame (empty when no pair was flagged).
    """
    return pl.DataFrame(log_rows, schema=DOB_CONFLICT_LOG_SCHEMA)


def build_schema(table_name: str, cfg: Config) -> pap.DataFrameSchema:
    """Build the pandera schema for one table from config (§5.2).

    Args:
        table_name: One of "sponsor", "family", "child".
        cfg: Pipeline configuration.

    Returns:
        The pandera polars DataFrameSchema.
    """
    columns: dict[str, pap.Column] = {}
    for key, spec in cfg.tables[table_name].columns.items():
        checks: list[Check] = []
        if spec.type == "enum":
            values = cfg.enum_values(table_name, key)
            checks.append(Check.isin(values))
        columns[key] = pap.Column(
            PL_DTYPES[spec.type],
            nullable=True,
            checks=checks,
        )
    return pap.DataFrameSchema(columns, strict=True)


def validate_rows(
    rows: list[dict[str, Any]], table_name: str, cfg: Config
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate cleaned rows with pandera (§5.2, §5.3).

    Args:
        rows: Cleaned rows keyed by config columns.
        table_name: One of "sponsor", "family", "child".
        cfg: Pipeline configuration.

    Returns:
        A tuple of (kept rows, rejected-row records for the log).
    """
    if not rows:
        return [], []
    specs = cfg.tables[table_name].columns
    schema = build_schema(table_name, cfg)
    frame = pl.DataFrame(
        rows, schema={key: PL_DTYPES[spec.type] for key, spec in specs.items()}
    )
    try:
        schema.validate(frame, lazy=True)
        return rows, []
    except SchemaErrors as errors:
        failures = errors.failure_cases
        bad_indices = set(failures["index"].to_list())
        rejected: list[dict[str, Any]] = []
        for column, check, index in zip(
            failures["column"].to_list(),
            failures["check"].to_list(),
            failures["index"].to_list(),
            strict=False,
        ):
            rejected.append(
                {
                    "table": table_name,
                    "index": index,
                    "column": column,
                    "value": rows[index].get(column),
                    "reason": str(check),
                }
            )
        kept = [
            row for index, row in enumerate(rows) if index not in bad_indices
        ]
        return kept, rejected


def compute_age_at_xmas(dob: str, xmas: date) -> int:
    """Compute full years between dob and the configured Christmas date.

    Args:
        dob: ISO date-of-birth string.
        xmas: The configured Christmas date.

    Returns:
        The age in completed years.
    """
    born = date.fromisoformat(dob)
    return (
        xmas.year
        - born.year
        - ((xmas.month, xmas.day) < (born.month, born.day))
    )


def index_to_letters(index: int) -> str:
    """Map a zero-based sibling index to an angel letter suffix (§5.5).

    A=0 ... Z=25, AA=26, AB=27, ... (letters exceed Z bijectively).

    Args:
        index: Zero-based position within the family.

    Returns:
        The uppercase letter code.
    """
    index += 1
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _created_value(row: dict[str, Any]) -> Any:
    """Return the row's created timestamp under either config key.

    The sponsor table's created column may be keyed ``created`` or
    ``create`` depending on the config; family/child use ``created``.

    Args:
        row: One imported row.

    Returns:
        The created value (possibly None).
    """
    return (
        row.get("created")
        if row.get("created") is not None
        else row.get("create")
    )


def assign_sponsor_ids(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assign sequential sponsor_id by created then email (§5.5).

    Args:
        rows: Cleaned, validated sponsor rows.

    Returns:
        Rows with sponsor_id, pref_age_min, and pref_age_max added.
    """
    ordered = sorted(
        rows,
        key=lambda row: (_created_value(row) or "", row.get("email") or ""),
    )
    result: list[dict[str, Any]] = []
    for sponsor_id, row in enumerate(ordered, start=1):
        row = dict(row)
        row["sponsor_id"] = sponsor_id
        low, high = AGE_PREF_BOUNDS[row["age_preference"]]
        row["pref_age_min"] = low
        row["pref_age_max"] = high
        result.append(row)
    return result


def assign_family_ids(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assign sequential family_id by created then primary_email (§5.5).

    Args:
        rows: Cleaned, validated, deduplicated family rows.

    Returns:
        Rows with family_id added.
    """
    ordered = sorted(
        rows,
        key=lambda row: (
            row.get("created") or "",
            row.get("primary_email") or "",
        ),
    )
    result: list[dict[str, Any]] = []
    for family_id, row in enumerate(ordered, start=1):
        row = dict(row)
        row["family_id"] = family_id
        result.append(row)
    return result


def assign_angel_ids(
    child_rows: list[dict[str, Any]],
    family_rows: list[dict[str, Any]],
    cfg: Config,
) -> list[dict[str, Any]]:
    """Link children to families and assign angel_id letters (§5.5).

    Children link via primary_email; within a family, letters start at A for
    the oldest child (descending age_at_xmas, tie-break earliest created
    then dob).  Orphaned children keep null family_id and angel_id but stay
    in raw_child.

    Args:
        child_rows: Cleaned, validated child rows.
        family_rows: Family rows with family_id assigned.
        cfg: Pipeline configuration.

    Returns:
        Child rows with family_id, angel_id, and age_at_xmas added.
    """
    xmas = cfg.xmas
    email_to_family = {
        row["primary_email"]: row["family_id"] for row in family_rows
    }
    enriched: list[dict[str, Any]] = []
    orphans: list[dict[str, Any]] = []
    families: dict[int, list[dict[str, Any]]] = {}
    for row in child_rows:
        row = dict(row)
        row["age_at_xmas"] = compute_age_at_xmas(row["dob"], xmas)
        reported = row.get("age_reported")
        if reported is not None and reported != row["age_at_xmas"]:
            logger.warning(
                "age mismatch for child %s: age_reported=%s, computed age_at_xmas=%s",
                row["zite_record_id"],
                reported,
                row["age_at_xmas"],
            )
        family_id = email_to_family.get(row.get("primary_email"))
        if family_id is None:
            row["family_id"] = None
            row["angel_id"] = None
            orphans.append(row)
            logger.warning(
                "orphaned child %s: no family registration for %r",
                row["zite_record_id"],
                row.get("primary_email"),
            )
            continue
        row["family_id"] = family_id
        families.setdefault(family_id, []).append(row)
    for family_id in sorted(families):
        ordered = sorted(
            families[family_id],
            key=lambda row: (
                -row["age_at_xmas"],
                row.get("created") or "",
                row.get("dob") or "",
            ),
        )
        for position, row in enumerate(ordered):
            row["angel_id"] = f"{family_id}{index_to_letters(position)}"
        enriched.extend(ordered)
    enriched.extend(orphans)
    return enriched


def _cross_check_created(rows: list[dict[str, Any]], table_name: str) -> None:
    """Log divergence between user created and zite_created_at (§5.1)."""
    for row in rows:
        created = _created_value(row)
        zite_created = row.get("zite_created_at")
        if created and zite_created and created != zite_created:
            logger.info(
                "%s %s: created %s != zite_created_at %s",
                table_name,
                row.get("zite_record_id"),
                created,
                zite_created,
            )


def _rows_to_frame(
    rows: list[dict[str, Any]], table_name: str, cfg: Config
) -> pl.DataFrame:
    """Build the output sheet DataFrame with config + derived columns."""
    specs = cfg.tables[table_name].columns
    schema: dict[str, PolarsDataType] = {
        key: PL_DTYPES[spec.type] for key, spec in specs.items()
    }
    if table_name == "sponsor":
        schema.update(
            {
                "sponsor_id": pl.Int64,
                "pref_age_min": pl.Int64,
                "pref_age_max": pl.Int64,
            }
        )
    elif table_name == "family":
        schema["family_id"] = pl.Int64
    else:
        schema.update(
            {
                "family_id": pl.Int64,
                "angel_id": pl.String,
                "age_at_xmas": pl.Int64,
            }
        )
    frame = pl.DataFrame(
        [{key: row.get(key) for key in schema} for row in rows], schema=schema
    )
    return frame.select(list(schema))


def pull_tables(cfg: Config) -> dict[str, pl.DataFrame]:
    """Pull the three configured Zite tables (§5.1).

    Args:
        cfg: Pipeline configuration.

    Returns:
        Mapping of table name to its raw records.

    Raises:
        AcquisitionError: If a table reference is still a TODO placeholder
            or the Zite pull fails.
    """
    frames: dict[str, pl.DataFrame] = {}
    for table_name in ("sponsor", "family", "child"):
        reference = getattr(cfg.zite, table_name)
        if reference == "TODO":
            raise AcquisitionError(
                f"config.zite.{table_name} is still TODO; fill in the Zite"
                " table reference first (§12 item 1)"
            )
        frames[table_name] = zite.get_table_data(reference)
        logger.info(
            "pulled %d rows from zite table %s",
            frames[table_name].height,
            table_name,
        )
    return frames


def run(cfg: Config) -> None:
    """Run module 1 end to end (§5).

    Args:
        cfg: Pipeline configuration.

    Raises:
        AcquisitionError: On structural failures or final-sheet corruption.
    """
    frames = pull_tables(cfg)
    sheets: dict[str, pl.DataFrame] = {}
    dedup_log_rows: list[dict[str, Any]] = []
    dob_conflict_rows: list[dict[str, Any]] = []
    for table_name in ("sponsor", "family", "child"):
        kept, rejected, log_rows = process_table(
            frames[table_name], table_name, cfg
        )
        dedup_log_rows.extend(log_rows)
        for record in rejected:
            logger.warning(
                "rejected %s row %s column %s value %r: %s",
                record["table"],
                record["index"],
                record["column"],
                record["value"],
                record["reason"],
            )
        if table_name == "sponsor":
            kept = assign_sponsor_ids(kept)
        elif table_name == "family":
            kept = assign_family_ids(kept)
        else:
            family_rows = sheets["raw_family"].to_dicts()
            kept = assign_angel_ids(kept, family_rows, cfg)
            _cross_check_created(kept, "child")
            dob_conflict_rows.extend(sibling_dob_conflicts(kept, cfg))
        if table_name == "family":
            _cross_check_created(kept, "family")
        logger.info(
            "table %s: pulled %d, kept %d, rejected %d",
            table_name,
            frames[table_name].height,
            len(kept),
            len({(record["table"], record["index"]) for record in rejected}),
        )
        sheets[f"raw_{table_name}"] = _rows_to_frame(kept, table_name, cfg)
    sheets["dedup_log"] = dedup_log_frame(dedup_log_rows)
    sheets["dob_conflict_log"] = dob_conflict_log_frame(dob_conflict_rows)
    final_sheets = [
        "final_child",
        "final_family",
        "final_sponsor",
        "match_report",
    ]
    before = snapshot_sheets(cfg.excel_file, final_sheets)
    write_sheets(cfg.excel_file, "acquire", sheets)
    after = snapshot_sheets(cfg.excel_file, final_sheets)
    changed = sheets_unchanged(before, after)
    if changed:
        raise AcquisitionError(
            f"final sheets changed during acquisition: {changed} (§9 ground-truth violation)"
        )
