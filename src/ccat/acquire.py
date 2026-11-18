"""Module 1 — data acquisition (§5).

Pulls the sponsor/family/child tables from Zite and writes them to the
``raw_*`` sheets **verbatim** (§5.1): the config allow-list, renames, and
system-column mappings are applied so every sheet has a stable schema,
but values are never cleaned, validated, deduplicated, or enriched.
Every submission survives, duplicates included -- duplicate detection
and flagging is module 2's job (§6).

The shared column helpers (``clean_value``, ID assignment, sibling DOB
conflicts) live in the neutral :mod:`ccat.domain` module and are
re-exported here for backward compatibility; the acquire **stage**
itself never calls them.  Never touches any other sheet (§5.7).
"""

import json
import logging
from difflib import get_close_matches as _get_close_matches
from typing import Any

import polars as pl
from polars._typing import PolarsDataType

from ccat import zite
from ccat.config import ColumnSpec, Config

# Re-exported from ccat.domain for backward compatibility (Phase 1a).
from ccat.domain import (  # noqa: F401
    AGE_PREF_BOUNDS,
    DERIVED_COLUMNS,
    EMAIL_RE,
    SPECIAL_NEEDS_LABEL,
    _child_identity,
    _created_value,
    _cross_check_created,
    _dob_gap_months,
    _parse_reported_age,
    assign_angel_ids,
    assign_family_ids,
    assign_sponsor_ids,
    clean_value,
    compute_age_at_xmas,
    index_to_letters,
    sibling_dob_conflicts,
    snake_case,
)
from ccat.io import sheets_unchanged, snapshot_sheets, write_sheets

logger = logging.getLogger(__name__)


class AcquisitionError(Exception):
    """Raised when acquisition cannot complete (structural failure)."""


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

    Values are copied verbatim; no cleaning or validation happens here.

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


# ---------------------------------------------------------------------------
# Sibling DOB conflicts (§6.6, written by the screening stage).
# ---------------------------------------------------------------------------


#: Column schema of the ``dob_conflict_log`` sheet (§6.6).
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


def dob_conflict_log_frame(log_rows: list[dict[str, Any]]) -> pl.DataFrame:
    """Build the ``dob_conflict_log`` sheet frame (§6.6).

    Args:
        log_rows: Log rows from :func:`sibling_dob_conflicts`.

    Returns:
        The typed frame (empty when no pair was flagged).
    """
    return pl.DataFrame(log_rows, schema=DOB_CONFLICT_LOG_SCHEMA)


# ---------------------------------------------------------------------------
# Verbatim fetch (the whole acquire stage).
# ---------------------------------------------------------------------------


def _verbatim_cell(value: Any) -> Any:
    """Verbatim scalar form of one cell value (§5.1).

    Zite may return a structured cell value -- e.g. the sponsor ``Source``
    provenance object ``{"type": "form_submission", "sessionId": ...,
    "flowPublicId": ...}``.  A deterministic JSON serialization preserves
    the whole value verbatim inside the sheet's declared string column;
    scalars pass through untouched.

    Args:
        value: The raw cell value (may be None).

    Returns:
        The value as-is when scalar, else its JSON string form.
    """
    if isinstance(value, dict):
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    if isinstance(value, list | tuple):
        return json.dumps(value, ensure_ascii=False)
    return value


def _raw_frame(
    rows: list[dict[str, Any]], table_name: str, cfg: Config
) -> pl.DataFrame:
    """Build the verbatim raw sheet frame: config columns, untouched values.

    Column dtypes follow the declared column types where every value
    fits (strict cast); a column holding values that cannot be cast --
    e.g. free-text ``age_reported`` against an ``int`` declaration --
    keeps its inferred dtype so the verbatim text survives (§5.1).

    Args:
        rows: Imported rows from :func:`import_table`.
        table_name: One of "sponsor", "family", "child".
        cfg: Pipeline configuration.

    Returns:
        The typed frame (dtypes per the declared column types).
    """
    specs = cfg.tables[table_name].columns
    schema: dict[str, PolarsDataType] = {
        key: PL_DTYPES[spec.type] for key, spec in specs.items()
    }
    cells = [
        {key: _verbatim_cell(row.get(key)) for key in schema} for row in rows
    ]
    if not cells:
        return pl.DataFrame(schema=schema)
    frame = pl.DataFrame(cells)
    for key, dtype in schema.items():
        column = frame.get_column(key)
        if column.dtype == dtype:
            continue
        try:
            frame = frame.with_columns(column.cast(dtype, strict=True))
        except pl.exceptions.PolarsError:
            logger.warning(
                "table %s column %s: value(s) not %s; column kept verbatim",
                table_name,
                key,
                dtype,
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
    """Run module 1 end to end (§5): fetch only.

    Pulls the three Zite tables and writes each one verbatim to its
    ``raw_*`` sheet -- no cleaning, no validation, no deduplication, no
    ID assignment, no duplicate removal.  Every downstream preparation
    (cleaning, validation-as-flags, IDs, duplicate flags, email/address
    checks, AI screening) happens in ``ccat screen``.

    Args:
        cfg: Pipeline configuration.

    Raises:
        AcquisitionError: On structural failures or final-sheet corruption.
    """
    frames = pull_tables(cfg)
    sheets: dict[str, pl.DataFrame] = {}
    for table_name in ("sponsor", "family", "child"):
        rows = import_table(frames[table_name], table_name, cfg)
        sheets[f"raw_{table_name}"] = _raw_frame(rows, table_name, cfg)
        logger.info(
            "table %s: pulled %d rows verbatim",
            table_name,
            frames[table_name].height,
        )
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
