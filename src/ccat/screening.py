"""Module 2 — pre-audit screening (§6, audit_architecture.md §7.3).

Module 2 prepares everything module 1 (acquire, now a verbatim fetch)
no longer does, then AI-screens the children:

1. **Local pre-audit (never the AI).**  The verbatim ``raw_*`` records
   are cleaned with the local trivial fixes (title-cased names,
   lowercased emails, reformatted phones, ISO dates) and validated;
   format/enum failures become ``require_clarification`` notes, never
   dropped rows.  The cleaned rows -- every submission, duplicates
   included -- are written to the ``*_submissions`` sheets with the
   derived IDs (``sponsor_id``, ``family_id``, ``angel_id``,
   ``age_at_xmas``).
2. **Duplicate detection.**  Potential duplicates are only *flagged*,
   never merged: families by ``primary_email``, children by
   (``primary_email``, ``first_name``), sponsors by ``email``.  Every
   member of a flagged group gets ``potential_duplicate=True`` on its
   adj row and a ``require_clarification`` note; the full group record
   is logged to ``duplicate_log``.  The user resolves duplicates in the
   audit tool (by ignoring the extras).
3. **Sibling DOB conflicts** (§6.6) are logged to ``dob_conflict_log``
   and noted in the children's ``potential_anomoly``.
4. **Email deliverability (ZeroBounce).**  Every new sponsor and family
   email (primary and secondary) is checked via the ZeroBounce API; the
   ENTIRE verbatim API response is stored in ``email_validation_log``.
5. **Address validation (Nominatim).**  Every new family address is
   checked against OSM Nominatim; the ENTIRE response is stored in
   ``address_validation_log``.
6. **AI screening (children only).**  Adjudicates each child row with
   an LLM via the opencode python SDK and writes **dense** ``adj_child``
   rows.  Trivial capitalization/format fixes already happened locally
   in step 1, so the model only fixes meaning-level issues (spelling,
   garbled phrasing, size contradictions).  No personal data is ever
   sent: the prompt carries no names or contact details; the child's
   ``zite_record_id`` is the sole identifier (§6.2).  Sponsors and
   families are never AI-screened -- every one of their fields is
   personal data, so their pre-audit is entirely local.

The adj rows are seeded here for all three entities (one ``adj_child``
row per child submission; one ``adj_family`` row per ``family_id``;
one ``adj_sponsor`` row per ``sponsor_id``), so ``ccat audit`` sees
complete rows without seeding them itself.  Human-edit protection
(§7.3) still applies: AI corrections land only on fields the human has
not touched, and existing rows are never re-seeded.

Batch AI screening runs each child's LLM check on a
``concurrent.futures.ThreadPoolExecutor`` -- the calls are I/O-bound, so
threads scale them.  Each worker thread owns one opencode session, and
the shared tqdm progress bar advances once per child.  Ctrl-C stops the
run promptly while every row completed so far is already persisted.
"""

import json
import logging
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
from polars._typing import PolarsDataType
from pydantic import BaseModel, ConfigDict, field_validator
from tqdm import tqdm

from ccat.acquire import (
    DERIVED_COLUMNS,
    PL_DTYPES,
    _created_value,
    _cross_check_created,
    _parse_reported_age,
    assign_angel_ids,
    assign_family_ids,
    assign_sponsor_ids,
    clean_value,
    dob_conflict_log_frame,
    sibling_dob_conflicts,
)
from ccat.audit.store import (
    ADJ_CHILD_COLUMNS,
    CHILD_EDITABLE_COLUMNS,
    CHILD_LINKAGE_COLUMNS,
    ORPHAN_NOTE,
    STATUS_NEEDS_CLARIFICATION,
    STATUS_NEW,
    as_bool,
    build_adj_frame,
    read_adj,
)
from ccat.config import Config
from ccat.io import read_sheet, write_sheets

logger = logging.getLogger(__name__)


class ScreeningError(Exception):
    """Raised when screening cannot complete."""


def _opencode_major_version() -> int | None:
    """Return the major version of the ``opencode`` CLI, or None.

    Runs ``opencode --version``; None means the binary is missing or its
    output is unparseable.  Output forms seen in the wild: ``2.2.5``
    (older builds) and ``opencode v2.2.5`` (newer builds), so the first
    ``major.`` number is matched anywhere in the text.

    Returns:
        The CLI major version, or None when opencode cannot be run.
    """
    try:
        result = subprocess.run(
            ["opencode", "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"\bv?(\d+)\.", (result.stdout or "").strip())
    return int(match.group(1)) if match else None


def _require_opencode_v2() -> None:
    """Fail fast unless opencode v2 is installed (§6.2).

    The embedded server relies on ``opencode serve --stdio``, which only
    exists in opencode 2.x; older versions print a usage message and
    exit, so screening must refuse to run.

    Raises:
        ScreeningError: If opencode is missing, unparseable, or older
            than version 2.
    """
    version = _opencode_major_version()
    if version is None:
        raise ScreeningError(
            "opencode CLI not found on PATH or `opencode --version`"
            " failed; install opencode v2 (https://opencode.ai) before"
            " running the screen module"
        )
    if version < 2:
        raise ScreeningError(
            f"opencode {version}.x is installed but the screen module"
            " needs opencode v2 (the embedded server requires"
            " `opencode serve --stdio`); run `opencode upgrade --major`"
        )


# ---------------------------------------------------------------------------
# Log-sheet schemas (§6).
# ---------------------------------------------------------------------------


#: Column schema of the ``duplicate_log`` sheet (§6.4): one row per
#: flagged duplicate submission; nothing is ever merged or dropped.
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

#: Column schema of the ``email_validation_log`` sheet (§6.7).
EMAIL_VALIDATION_LOG_SCHEMA: dict[str, PolarsDataType] = {
    "source_sheet": pl.String,
    "zite_record_id": pl.String,
    "field": pl.String,
    "email": pl.String,
    "status": pl.String,
    "sub_status": pl.String,
    "did_you_mean": pl.String,
    "response_json": pl.String,
    "checked_at": pl.String,
}

#: Column schema of the ``address_validation_log`` sheet (§6.8).
ADDRESS_VALIDATION_LOG_SCHEMA: dict[str, PolarsDataType] = {
    "source_sheet": pl.String,
    "zite_record_id": pl.String,
    "query": pl.String,
    "status": pl.String,
    "display_name": pl.String,
    "lat": pl.String,
    "lon": pl.String,
    "response_json": pl.String,
    "checked_at": pl.String,
}


# ---------------------------------------------------------------------------
# Local pre-audit: cleaning and validation-as-flags (§6.2).
# ---------------------------------------------------------------------------


def _enum_failure(
    value: Any, table_name: str, key: str, cfg: Config
) -> str | None:
    """Return a note when an enum value is not a vocabulary label (§6.2).

    Args:
        value: The cleaned enum cell value.
        table_name: One of "sponsor", "family", "child".
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


def clean_rows(
    rows: list[dict[str, Any]], table_name: str, cfg: Config
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Clean verbatim rows locally and collect validation notes (§6.2).

    Blank strings become nulls, declared defaults fill nulls, free-text
    reported ages are coerced, and each cell is normalized per its
    column type (title-cased names, lowercased emails, reformatted
    phones, ISO dates).  Failures never drop a row: every issue becomes
    a note keyed by the record's ``zite_record_id`` and lands in the
    seeded adj row's ``require_clarification``.

    Args:
        rows: Verbatim rows from :func:`ccat.acquire.import_table`.
        table_name: One of "sponsor", "family", "child".
        cfg: Pipeline configuration.

    Returns:
        A tuple of (cleaned rows, mapping of zite_record_id to the
        list of validation notes for that record).
    """
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


def submissions_frame(
    rows: list[dict[str, Any]], table_name: str, cfg: Config
) -> pl.DataFrame:
    """Build one ``{table}_submissions`` sheet frame (§6.3).

    The table's config columns plus its derived columns
    (:data:`ccat.acquire.DERIVED_COLUMNS`) -- the cleaned, ID-assigned
    rows every downstream module (audit, match, generate, stats) works
    from.  Every submission is present, duplicates included.

    Args:
        rows: Cleaned, ID-assigned rows from the pre-audit.
        table_name: "sponsor", "family", or "child".
        cfg: Pipeline configuration.

    Returns:
        The typed frame, sorted by (created, zite_record_id) ascending.
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


# ---------------------------------------------------------------------------
# Duplicate detection (§6.4): flag only, never merge.
# ---------------------------------------------------------------------------


def detect_duplicates(
    rows: list[dict[str, Any]], table_name: str
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Detect potential duplicate submissions (§6.4).

    Group keys: families by ``primary_email``, children by
    (``primary_email``, ``first_name``), sponsors by ``email``.  Rows
    missing their key are never grouped.  The survivor is the earliest
    submission for families and sponsors, the most recent for children
    (matching the old dedup semantics) -- but nothing is dropped or
    merged: every group member stays in the prepared rows and the adj
    rows, flagged for the user.

    Args:
        rows: Cleaned rows for one table.
        table_name: "sponsor", "family", or "child".

    Returns:
        A tuple of (``duplicate_log`` rows, mapping of each group
        member's zite_record_id to its survivor's row).
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
            continue  # no key: never grouped, never flagged
        groups.setdefault(values, []).append(row)
    log_rows: list[dict[str, Any]] = []
    survivor_of: dict[str, dict[str, Any]] = {}
    #: Identity/order metadata duplicate records necessarily differ on;
    #: never counted as a conflicting field (§6.4).
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
        logger.warning(
            "%s duplicate group %s: %d submission(s); survivor %s, extras %s",
            table_name,
            group_key,
            len(group),
            survivor.get("zite_record_id"),
            [row.get("zite_record_id") for row in extras],
        )
    return log_rows, survivor_of


def duplicate_log_frame(log_rows: list[dict[str, Any]]) -> pl.DataFrame:
    """Build the ``duplicate_log`` sheet frame (§6.4).

    Args:
        log_rows: Log rows from :func:`detect_duplicates`.

    Returns:
        The typed frame (empty when nothing was flagged).
    """
    return pl.DataFrame(log_rows, schema=DUPLICATE_LOG_SCHEMA)


def duplicate_note(
    record_id: str, survivor_of: dict[str, dict[str, Any]], entity: str
) -> str | None:
    """Build the ``require_clarification`` note for one flagged row (§6.4).

    Args:
        record_id: The row's zite_record_id.
        survivor_of: Mapping from :func:`detect_duplicates`.
        entity: "sponsor", "family", or "child" (note wording).

    Returns:
        The note naming the survivor submission, or None when the row
        is not part of a duplicate group.
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
    """Idempotently append a note to a ``require_clarification`` value.

    Args:
        existing: The current note text (may be null).
        addition: The note to append (may be null).

    Returns:
        The merged text (unchanged when the addition is absent or
        already present).
    """
    if not addition:
        return existing
    text = (existing or "").strip()
    if not text:
        return addition
    if addition in text:
        return text
    return f"{text}; {addition}"


# ---------------------------------------------------------------------------
# Email (ZeroBounce) and address (Nominatim) validation (§6.7/§6.8).
# ---------------------------------------------------------------------------


def _scripts_dir() -> Path:
    """Locate the repo's ``scripts/`` directory."""
    candidate = Path(__file__).resolve().parents[2] / "scripts"
    if candidate.is_dir():
        return candidate
    return Path.cwd() / "scripts"


def _email_engine() -> tuple[Callable[..., Any], type[Exception]]:
    """Import the ZeroBounce engine from ``scripts/`` (§6.7).

    Returns:
        A tuple of (:func:`scripts.zerobounce_email_check.validate_email`,
        its ``OutOfCreditsError``).

    Raises:
        ScreeningError: If the scripts directory or the zerobouncesdk
            package is unavailable.
    """
    scripts = _scripts_dir()
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    try:
        from zerobounce_email_check import (  # type: ignore[import-not-found]
            OutOfCreditsError,
            validate_email,
        )
    except ImportError as error:
        raise ScreeningError(
            "could not import the ZeroBounce engine"
            f" ({scripts}/zerobounce_email_check.py): {error}; install"
            " zerobouncesdk or set config.email_check.enabled = false"
        ) from error
    return validate_email, OutOfCreditsError


def _email_batch_engine() -> Callable[..., Any] | None:
    """Import the ZeroBounce batch (bulk) engine from ``scripts/`` (§6.7).

    Returns:
        :func:`scripts.zerobounce_email_check.bulk_validate_emails`, or
        None when the engine cannot be imported -- the email check then
        falls back to threaded per-email calls.
    """
    scripts = _scripts_dir()
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    try:
        from zerobounce_email_check import (  # type: ignore[import-not-found]
            bulk_validate_emails,
        )
    except ImportError:
        return None
    return bulk_validate_emails


def _address_engine() -> tuple[Callable[..., Any], Callable[..., str]]:
    """Import the Nominatim engine from ``scripts/`` (§6.8).

    Returns:
        A tuple of
        (:func:`scripts.nominatim_address_check.check_address`,
        :func:`scripts.nominatim_address_check.build_query`).

    Raises:
        ScreeningError: If the scripts directory is unavailable.
    """
    scripts = _scripts_dir()
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    try:
        from nominatim_address_check import (  # type: ignore[import-not-found]
            build_query,
            check_address,
        )
    except ImportError as error:
        raise ScreeningError(
            "could not import the Nominatim engine"
            f" ({scripts}/nominatim_address_check.py): {error}"
        ) from error
    return check_address, build_query


def _validation_log_rows(cfg: Config, sheet: str) -> list[dict[str, Any]]:
    """Read one validation log sheet as row dicts (empty when absent)."""
    frame = read_sheet(cfg.excel_file, sheet)
    if frame is None:
        return []
    return frame.to_dicts()


def check_emails(
    cfg: Config,
    rows_by_table: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Run the ZeroBounce bulk check over every new email (§6.7).

    Targets: ``sponsor.email``, family ``primary_email`` and
    ``secondary_email`` -- deduplicated by normalized address, skipping
    every email already recorded in ``email_validation_log`` (so re-runs
    never burn credits twice).  The ENTIRE verbatim ZeroBounce response
    is stored in the log's ``response_json``.  API failures are
    non-fatal: the affected emails are left unchecked for the next run.

    Method: the batch endpoint (one call per 100 addresses) when the
    engine is available; otherwise threaded per-email calls
    (``config.email_check.workers`` workers).

    Args:
        cfg: Pipeline configuration.
        rows_by_table: Mapping of table name to prepared rows.

    Returns:
        The new ``email_validation_log`` rows (possibly empty).
    """
    targets: list[tuple[str, str, str, str]] = []
    fields: dict[str, tuple[str, ...]] = {
        "sponsor": ("email",),
        "family": ("primary_email", "secondary_email"),
    }
    for table_name, table_fields in fields.items():
        sheet = f"{table_name}_submissions"
        for row in rows_by_table.get(table_name, []):
            record_id = str(row.get("zite_record_id") or "")
            for field in table_fields:
                email = row.get(field)
                if email is None or not str(email).strip():
                    continue
                targets.append((sheet, record_id, field, str(email).strip()))
    existing = _validation_log_rows(cfg, "email_validation_log")
    checked_emails = {
        str(row.get("email")).strip().lower()
        for row in existing
        if row.get("email")
    }
    pending = [t for t in targets if t[3].lower() not in checked_emails]
    if not pending:
        logger.info("email check: nothing new to check")
        return []
    unique_emails = list(dict.fromkeys(t[3] for t in pending))
    logger.info(
        "email check: %d new email(s) of %d target(s) via ZeroBounce",
        len(unique_emails),
        len(pending),
    )
    validate_email, out_of_credits = _email_engine()
    bulk_validate_emails = _email_batch_engine()
    checked_at = datetime.now(UTC).isoformat(timespec="seconds")
    if bulk_validate_emails is not None:
        # Bulk method: one batch call per 100 addresses (§6.7).
        try:
            bulk_results = bulk_validate_emails(unique_emails)
        except out_of_credits as error:  # type: ignore[misc]
            logger.error("%s; stopping the email check for this run", error)
            return []
        except Exception as error:  # noqa: BLE001 - fall back per-email
            logger.error(
                "ZeroBounce batch check failed (%s); falling back to"
                " threaded per-email checks",
                error,
            )
        else:
            if bulk_results is not None:
                log_rows = _email_log_rows(pending, bulk_results, checked_at)
                logger.info("email check: %d new log row(s)", len(log_rows))
                return log_rows
    else:
        logger.info("email check: batch engine unavailable; threaded checks")
    # Fallback: threaded per-email checks (I/O-bound, so threads scale).
    workers = max(1, min(cfg.email_check.workers, len(unique_emails)))
    logger.info("email check: %d worker thread(s)", workers)
    results = {}
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {
            pool.submit(validate_email, email): email for email in unique_emails
        }
        for future in as_completed(futures):
            email = futures[future]
            try:
                results[email] = future.result()
            except out_of_credits as error:  # type: ignore[misc]
                logger.error("%s; stopping the email check for this run", error)
                break
            except Exception as error:  # noqa: BLE001 - keep checking the rest
                logger.error("ZeroBounce error for %s: %s", email, error)
                results[email] = None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    log_rows = _email_log_rows(pending, results, checked_at)
    logger.info("email check: %d new log row(s)", len(log_rows))
    return log_rows


def _email_log_rows(
    pending: list[tuple[str, str, str, str]],
    results: dict[str, dict[str, Any] | None],
    checked_at: str,
) -> list[dict[str, Any]]:
    """Convert ZeroBounce results into ``email_validation_log`` rows.

    Args:
        pending: The (sheet, record_id, field, email) targets checked.
        results: Mapping of email to its ZeroBounce result dict, or None
            when the API failed (those emails stay unchecked).
        checked_at: The shared run timestamp.

    Returns:
        One log row per (record, field) occurrence with a result.
    """
    log_rows: list[dict[str, Any]] = []
    for sheet, record_id, field, email in pending:
        result = results.get(email)
        if result is None:
            continue  # API error or never reached: left unchecked
        log_rows.append(
            {
                "source_sheet": sheet,
                "zite_record_id": record_id,
                "field": field,
                "email": email,
                "status": str(result.get("status", "")),
                "sub_status": str(result.get("sub_status", "")),
                "did_you_mean": str(result.get("did_you_mean", "")),
                "response_json": str(result.get("raw_response", "")),
                "checked_at": checked_at,
            }
        )
    return log_rows


def check_addresses(
    cfg: Config,
    family_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Run the Nominatim check over every new family address (§6.8).

    Targets: each family submission's street/apt/city/state/ZIP,
    deduplicated by normalized query text, skipping every query already
    recorded in ``address_validation_log``.  The ENTIRE verbatim
    Nominatim response is stored in the log's ``response_json``.
    Failures are non-fatal: the affected addresses stay unchecked for
    the next run.

    Args:
        cfg: Pipeline configuration.
        family_rows: Prepared family rows.

    Returns:
        The new ``address_validation_log`` rows (possibly empty).
    """
    check_address, build_query = _address_engine()
    targets: list[tuple[str, str, dict[str, Any]]] = []
    for row in family_rows:
        record_id = str(row.get("zite_record_id") or "")
        parts = {
            "street": row.get("street_address"),
            "apt_floor": row.get("apt_floor"),
            "city": row.get("city"),
            "state": row.get("state"),
            "zip_code": row.get("zip_code"),
        }
        if not any(str(value or "").strip() for value in parts.values()):
            continue  # no address data at all; pre-audit flags the row
        targets.append(("family_submissions", record_id, parts))
    existing = _validation_log_rows(cfg, "address_validation_log")
    checked_queries = {
        str(row.get("query")).strip() for row in existing if row.get("query")
    }
    pending: list[tuple[str, str, dict[str, Any], str]] = []
    seen_queries: set[str] = set()
    for sheet, record_id, parts in targets:
        query = build_query(
            parts["street"],
            parts["apt_floor"],
            parts["city"],
            parts["state"],
            parts["zip_code"],
        )
        if query in checked_queries or query in seen_queries:
            continue
        seen_queries.add(query)
        pending.append((sheet, record_id, parts, query))
    if not pending:
        logger.info("address check: nothing new to check")
        return []
    logger.info("address check: %d new address(es) via Nominatim", len(pending))
    log_rows: list[dict[str, Any]] = []
    for index, (sheet, record_id, parts, query) in enumerate(pending):
        try:
            result = check_address(
                parts["street"],
                apt_floor=parts["apt_floor"],
                city=parts["city"],
                state=parts["state"],
                zip_code=parts["zip_code"],
                base_url=cfg.address_check.base_url,
                user_agent=cfg.address_check.user_agent,
                timeout=cfg.address_check.timeout,
            )
        except Exception as error:  # noqa: BLE001 - keep checking the rest
            logger.error("Nominatim error for %r: %s", query, error)
            continue
        log_rows.append(
            {
                "source_sheet": sheet,
                "zite_record_id": record_id,
                "query": str(result.get("query", query)),
                "status": str(result.get("status", "error")),
                "display_name": result.get("display_name"),
                "lat": result.get("lat"),
                "lon": result.get("lon"),
                "response_json": result.get("response_json"),
                "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        )
        if index < len(pending) - 1:
            time.sleep(cfg.address_check.delay)
    logger.info("address check: %d new log row(s)", len(log_rows))
    return log_rows


# ---------------------------------------------------------------------------
# AI screening (children only) (§6.2 output contract).
# ---------------------------------------------------------------------------


#: Model-owned adj_child fields (§6.2): the columns the model may
#: correct.  ``age_at_xmas`` is excluded (computed from the adjudicated
#: dob, D13); controls and linkage are never model-owned.
RESPONSE_COLUMNS = (
    "zite_record_id",
    "dob",
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
    "interests",
    "favorite_colors",
    "other_notes",
    "potential_anomoly",
)

#: Enum-checked size columns -> vocabulary name (§6.2 output contract).
SIZE_VOCAB_COLUMNS = {
    "shoe_size": "child_shoe_size",
    "shoe_size_type": "child_shoe_size_type",
    "pant_waist_size": "child_pant_waist_size",
    "pant_inseam": "child_pant_inseam",
    "pant_size_letter": "child_pant_size_letter",
    "pant_size_type": "child_pant_size_type",
    "shirt_size_letter": "child_shirt_size_letter",
    "shirt_size_type": "child_shirt_size_type",
}

#: Child fields included in the screening prompt (§6.2).  No names, no
#: contact details, no family information.
PROMPT_FIELDS = (
    "zite_record_id",
    "dob",
    "age_at_xmas",
    "age_reported",
    "gender",
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
    "interests",
    "favorite_colors",
    "other_notes",
)

_DOB_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class AdjChildRow(BaseModel):
    """Pydantic schema for one adjudicated adj_child row (§6.2)."""

    model_config = ConfigDict(extra="forbid")

    zite_record_id: str
    dob: str | None = None
    need_1: str | None = None
    need_2: str | None = None
    desire_1: str | None = None
    desire_2: str | None = None
    shoe_size: str | None = None
    shoe_size_type: str | None = None
    pant_waist_size: str | None = None
    pant_inseam: str | None = None
    pant_size_letter: str | None = None
    pant_size_type: str | None = None
    jean_size: str | None = None
    shirt_size_letter: str | None = None
    shirt_size_type: str | None = None
    interests: str | None = None
    favorite_colors: str | None = None
    other_notes: str | None = None
    potential_anomoly: str | None = None

    @field_validator("dob")
    @classmethod
    def _check_dob(cls, value: str | None) -> str | None:
        """Require YYYY-MM-DD when a corrected dob is proposed."""
        if value is not None and not _DOB_RE.fullmatch(value):
            raise ValueError(f"dob must be YYYY-MM-DD, got {value!r}")
        return value


def build_prompt(child: dict[str, Any], cfg: Config) -> str:
    """Build the screening prompt for one child (§6.2).

    The child's name and any contact or family information are never
    included; the record JSON after the system prompt carries only the
    pseudonymous zite_record_id and screening-relevant fields.

    Args:
        child: One prepared child row as a dict.
        cfg: Pipeline configuration.

    Returns:
        The full prompt text.
    """
    record = {field: child.get(field) for field in PROMPT_FIELDS}
    system_prompt = cfg.ai_screening.system_prompt.replace(
        "{{xmas_date}}", cfg.xmas_date
    )
    return system_prompt + "\n\n" + json.dumps(record, ensure_ascii=False)


def parse_model_output(text: str) -> dict[str, Any] | None:
    """Parse the model's response (§6.2 output contract).

    Args:
        text: Raw response text.

    Returns:
        The parsed JSON object, or None when the model returned the
        literal null ("checked, no changes").

    Raises:
        ValueError: If the response is not a JSON object or null.
    """
    stripped = text.strip()
    if stripped.lower() == "null":
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(
                f"response is not a JSON object: {stripped[:100]!r}"
            ) from None
        parsed = json.loads(stripped[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError(f"response is not a JSON object: {stripped[:100]!r}")
    return parsed


def validate_adj_response(
    parsed: dict[str, Any], child: dict[str, Any], cfg: Config
) -> AdjChildRow:
    """Validate a parsed model response against the output contract.

    Args:
        parsed: The parsed JSON object.
        child: The prepared child row it must join back to.
        cfg: Pipeline configuration.

    Returns:
        The validated :class:`AdjChildRow`.

    Raises:
        ValueError: On key-set, dob-format, id-mismatch, or enum-vocabulary
            violations.
    """
    if set(parsed) != set(RESPONSE_COLUMNS):
        raise ValueError(f"unexpected key set: {sorted(parsed)}")
    row = AdjChildRow(**parsed)
    if row.zite_record_id != child["zite_record_id"]:
        raise ValueError(
            f"zite_record_id mismatch: response {row.zite_record_id!r},"
            f" expected {child['zite_record_id']!r}"
        )
    for column, vocabulary_name in SIZE_VOCAB_COLUMNS.items():
        value = getattr(row, column)
        if value is not None and value not in cfg.vocabularies[vocabulary_name]:
            raise ValueError(
                f"{column} value {value!r} is not a valid Zite vocabulary label"
            )
    return row


def screen_child(
    child: dict[str, Any],
    cfg: Config,
    send: Callable[[str], str],
) -> dict[str, Any] | None:
    """Screen one child with one retry, then give up (§6.2).

    Args:
        child: One prepared child row as a dict.
        cfg: Pipeline configuration.
        send: Callable sending one prompt to the model session and
            returning its raw text response.

    Returns:
        The validated corrections for this child (all-null cells when
        the model returned the literal null, meaning "no changes"), or
        None when the child could not be screened (logged as
        unscreened, picked up next run).
    """
    prompt = build_prompt(child, cfg)
    last_error: Exception | None = None
    for _attempt in range(2):
        response = send(prompt)
        try:
            parsed = parse_model_output(response)
        except ValueError as error:
            last_error = error
            logger.warning(
                "malformed response for child %s: %s; retrying",
                child["zite_record_id"],
                error,
            )
            continue
        if parsed is None:
            return {column: None for column in ADJ_CHILD_COLUMNS} | {
                "zite_record_id": child["zite_record_id"]
            }
        try:
            row = validate_adj_response(parsed, child, cfg)
        except ValueError as error:
            last_error = error
            logger.warning(
                "invalid response for child %s: %s; retrying",
                child["zite_record_id"],
                error,
            )
            continue
        return row.model_dump()
    logger.error(
        "child %s left unscreened after retry: %s",
        child["zite_record_id"],
        last_error,
    )
    return None


def screen_children(
    children: list[dict[str, Any]],
    cfg: Config,
    send: Callable[[str], str],
    max_workers: int = 4,
    on_result: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Screen every given child, concurrently (§6.2–§6.3).

    Each child's LLM check runs on a ``ThreadPoolExecutor`` -- I/O-bound
    work, so threads scale it (pgi-s3 transfer pattern).  The shared
    tqdm bar advances once per child under a lock in each future's
    ``finally`` block, so the count stays correct regardless of thread
    scheduling; one failed task still counts as progress.

    Args:
        children: Prepared child rows to screen.
        cfg: Pipeline configuration.
        send: Callable sending one prompt to the model session.  Must be
            safe to call from multiple threads (each thread keeps its
            own opencode session upstream).
        max_workers: Maximum number of concurrent LLM requests; values
            above the number of children are clamped.
        on_result: Optional callback invoked from the completion loop
            (the main thread) with each successfully screened row --
            used by ``run`` to persist rows as they arrive.

    Returns:
        The adj_child rows to persist (one per successfully screened
        child, sparse cells, §6.3).

    Raises:
        KeyboardInterrupt: When interrupted (Ctrl-C); queued tasks are
            cancelled immediately and in-flight ones are abandoned, so
            the process exits after at most one in-flight LLM call.
    """
    if not children:
        return []
    rows: list[dict[str, Any]] = []
    pbar_lock = threading.Lock()
    progress = tqdm(
        total=len(children),
        desc="Screening children",
        unit="child",
        position=0,
        disable=None,  # silent when stderr is not a terminal
    )

    def _finish(child: dict[str, Any], row: dict[str, Any] | None) -> None:
        """Record one child's outcome and advance the shared bar."""
        if row is not None:
            rows.append(row)
            if on_result is not None:
                on_result(row)
        with pbar_lock:
            progress.update(1)
            progress.set_postfix_str(
                str(child["zite_record_id"])[:8], refresh=False
            )

    if max_workers <= 1:
        try:
            for child in children:
                _finish(child, screen_child(child, cfg, send))
        finally:
            progress.close()
        return rows

    executor = ThreadPoolExecutor(max_workers=min(max_workers, len(children)))
    futures: dict[Any, dict[str, Any]] = {}
    try:
        futures = {
            executor.submit(screen_child, child, cfg, send): child
            for child in children
        }
        for future in as_completed(futures):
            child = futures[future]
            try:
                row = future.result()
            except Exception as error:  # noqa: BLE001 - keep the bar moving
                logger.error(
                    "screening task failed for child %s: %s",
                    child.get("zite_record_id"),
                    error,
                )
                row = None
            _finish(child, row)
    except KeyboardInterrupt:
        queued = sum(1 for pending in futures if not pending.done())
        logger.warning(
            "interrupted: cancelling %d queued screening task(s);"
            " completed rows are already persisted",
            queued,
        )
        # Cancel everything not yet started; running threads finish
        # their single LLM call and die with the queue empty, so the
        # process exits after at most one in-flight call.
        executor.shutdown(wait=False, cancel_futures=True)
        progress.close()
        raise
    progress.close()
    executor.shutdown(wait=True)
    return rows


def _values_equal(left: Any, right: Any) -> bool:
    """String-normalized equality for human-edit protection (§7.3)."""
    if left is None or (isinstance(left, str) and left.strip() == ""):
        left = None
    if right is None or (isinstance(right, str) and right.strip() == ""):
        right = None
    if left is None or right is None:
        return left is None and right is None
    return str(left).strip() == str(right).strip()


def dense_adj_row(
    child: dict[str, Any],
    corrections: dict[str, Any],
    cfg: Config,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one dense adj_child row from a screening result (§7.3).

    The row starts from the child's cleaned values (linkage and
    adjudicable data), overlays the model's non-null corrections, and
    recomputes ``age_at_xmas`` from the adjudicated dob (D13).  When a
    row already exists (``existing``), human edits survive: a
    correction lands on a model-owned field only when that field still
    equals the baseline, other fields keep the existing adj values, and
    the existing controls and module-owned columns are preserved.

    Args:
        child: One prepared child row as a dict.
        corrections: The validated model response (sparse: non-null
            values are corrections); may be all-null ("no changes").
        cfg: Pipeline configuration.
        existing: The child's existing dense adj row, when one exists.

    Returns:
        The dense adj_child row with ``screened`` set true.
    """
    from ccat.audit.store import _computed_age  # local: private helper

    row: dict[str, Any] = {column: None for column in ADJ_CHILD_COLUMNS}
    for column in (*CHILD_LINKAGE_COLUMNS, *CHILD_EDITABLE_COLUMNS):
        row[column] = child.get(column)
    if existing is not None:
        for column in (
            *CHILD_EDITABLE_COLUMNS,
            "age_at_xmas",
            "generated_info_sheet",
            "status",
            "audited",
            "ignore",
            "require_clarification",
            "audited_at",
            "potential_duplicate",
        ):
            row[column] = existing.get(column)
    for column in CHILD_EDITABLE_COLUMNS:
        value = corrections.get(column)
        if value is None:
            continue
        if existing is not None and not _values_equal(
            existing.get(column), child.get(column)
        ):
            # The human already changed this field; leave it alone (§7.3).
            continue
        row[column] = value
    row["zite_record_id"] = child["zite_record_id"]
    row["screened"] = True
    if existing is None:
        row["status"] = STATUS_NEW
        row["audited"] = False
        row["ignore"] = False
        row["generated_info_sheet"] = False
        row["potential_duplicate"] = False
    row["age_at_xmas"] = _computed_age(row.get("dob"), cfg)
    if row.get("family_id") is None:
        # Orphaned child: no matching family registration; surface it
        # for the user to fix (§8.7).
        row["require_clarification"] = append_note(
            row.get("require_clarification"), ORPHAN_NOTE
        )
        if row.get("status") == STATUS_NEW:
            row["status"] = STATUS_NEEDS_CLARIFICATION
    return row


def _upsert_adj_row(
    adj_rows: list[dict[str, Any]], row: dict[str, Any]
) -> None:
    """Insert or replace one adj row in place, keyed by zite_record_id.

    Args:
        adj_rows: The mutable adj_child row list.
        row: The row to insert (new id) or replace (existing id).
    """
    for index, existing in enumerate(adj_rows):
        if existing.get("zite_record_id") == row["zite_record_id"]:
            adj_rows[index] = row
            return
    adj_rows.append(row)


def _write_adj(cfg: Config, adj_rows: list[dict[str, Any]]) -> pl.DataFrame:
    """Atomically write the dense adj_child sheet (§7.3).

    Args:
        cfg: Pipeline configuration.
        adj_rows: All current dense adj_child rows.

    Returns:
        The written adj_child frame.

    Raises:
        ScreeningError: If the workbook cannot be written.
    """
    frame = build_adj_frame("child", adj_rows)
    try:
        write_sheets(cfg.excel_file, "screen", {"adj_child": frame})
    except Exception as error:  # noqa: BLE001 - wrap any save failure
        raise ScreeningError(f"could not persist adj_child: {error}") from error
    return frame


def first_family_children(
    children: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return the children of the first family (test-mode scope helper).

    The first family is the one with the lowest non-null ``family_id``.
    When no child has a family (all orphans), the first child alone is
    returned so test mode still has one record to process.

    Args:
        children: Prepared child rows as dicts.

    Returns:
        The children belonging to the first family.
    """
    present = [
        child["family_id"]
        for child in children
        if child.get("family_id") is not None
    ]
    if not present:
        return children[:1]
    first = min(present)
    return [child for child in children if child.get("family_id") == first]


def _screen_targets(
    targets: list[dict[str, Any]],
    cfg: Config,
    on_result: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Screen targets over one opencode server (§6.2, §6.4).

    Connects to the server named by ``OPENCODE_BASE_URL`` when set;
    otherwise starts an embedded ``opencode serve`` child process that
    lives for the duration of the run.

    Args:
        targets: Prepared child rows to screen.
        cfg: Pipeline configuration.
        on_result: Optional per-row callback forwarded to
            :func:`screen_children` (see its docstring).

    Returns:
        One adj_child row per target.

    Raises:
        ScreeningError: If the opencode server cannot be started.
    """
    import os

    from opencode import Opencode, OpencodeServer, ServerStartError
    from opencode.types import ModelRef

    model_ref = cfg.ai_screening.model
    provider_id, separator, model_id = model_ref.partition("/")
    if not separator:
        raise ScreeningError(
            f"config.ai_screening.model {model_ref!r} is not a valid"
            " opencode model reference; use the 'provider/model' form (run"
            " `opencode models` for the list)"
        )

    def _run(client: Any) -> list[dict[str, Any]]:
        """Screen all targets, one opencode session per worker thread.

        Concurrent ``session.generate`` calls must not share a session
        (one conversation per thread), so sessions are created lazily
        and cached by thread id.
        """
        sessions: dict[int, str] = {}
        sessions_lock = threading.Lock()

        def send(prompt: str) -> str:
            thread_id = threading.get_ident()
            with sessions_lock:
                session_id = sessions.get(thread_id)
            if session_id is None:
                session_id = client.session.create(
                    model=ModelRef(id=model_id, provider_id=provider_id)
                ).id
                with sessions_lock:
                    sessions[thread_id] = session_id
            return client.session.generate(session_id, prompt=prompt).text

        return screen_children(
            targets,
            cfg,
            send,
            max_workers=cfg.ai_screening.max_workers,
            on_result=on_result,
        )

    if os.environ.get("OPENCODE_BASE_URL"):
        with Opencode() as client:
            return _run(client)
    logger.info("OPENCODE_BASE_URL not set; starting embedded opencode server")
    try:
        with OpencodeServer(cwd=Path.cwd()) as server:
            return _run(server.client())
    except ServerStartError as error:
        raise ScreeningError(
            "could not start the embedded opencode server; install opencode"
            " (https://opencode.ai) or set OPENCODE_BASE_URL to a running"
            f" server: {error}"
        ) from error


# ---------------------------------------------------------------------------
# Adj seeding for sponsors and families (§7.3, owned by screening now).
# ---------------------------------------------------------------------------


def _flagged_row(
    row: dict[str, Any],
    duplicate_note_text: str | None,
    validation_notes: list[str],
) -> dict[str, Any]:
    """Apply the ``potential_duplicate`` flag and notes to one adj row.

    Args:
        row: The dense adj row (mutated in place).
        duplicate_note_text: The duplicate note, when flagged.
        validation_notes: Validation-failure notes for this record.

    Returns:
        The same row, flagged and annotated.
    """
    flagged = duplicate_note_text is not None
    if "potential_duplicate" in row:
        row["potential_duplicate"] = bool(
            row.get("potential_duplicate") or flagged
        )
    note = "; ".join([*validation_notes, duplicate_note_text or ""]).strip("; ")
    if note:
        row["require_clarification"] = append_note(
            row.get("require_clarification"), note or None
        )
    return row


def _seed_sponsor_adj_row(
    row: dict[str, Any],
    duplicate_note_text: str | None,
    validation_notes: list[str],
) -> dict[str, Any]:
    """Build one dense adj_sponsor row from its prepared submission (§7.3).

    Args:
        row: The cleaned, ID-assigned sponsor row.
        duplicate_note_text: The duplicate note, when flagged.
        validation_notes: Validation-failure notes for this record.

    Returns:
        The dense adj_sponsor row with default controls and the flag.
    """
    from ccat.audit.store import (
        ADJ_SPONSOR_COLUMNS,
        SPONSOR_EDITABLE_COLUMNS,
        SPONSOR_LINKAGE_COLUMNS,
        AGE_PREF_BOUNDS,
    )

    adj: dict[str, Any] = {column: None for column in ADJ_SPONSOR_COLUMNS}
    for column in (*SPONSOR_LINKAGE_COLUMNS, *SPONSOR_EDITABLE_COLUMNS):
        adj[column] = row.get(column)
    adj["sponsor_id"] = row.get("sponsor_id")
    bounds = AGE_PREF_BOUNDS.get(str(row.get("age_preference") or ""))
    if bounds is not None:
        adj["pref_age_min"], adj["pref_age_max"] = bounds
    adj["status"] = STATUS_NEW
    adj["audited"] = False
    adj["ignore"] = False
    adj["potential_duplicate"] = False
    return _flagged_row(adj, duplicate_note_text, validation_notes)


def _seed_family_adj_row(
    row: dict[str, Any],
    duplicate_note_text: str | None,
    validation_notes: list[str],
) -> dict[str, Any]:
    """Build one dense adj_family row from its prepared submission (§7.3).

    Args:
        row: The cleaned, ID-assigned family row (the group survivor:
            the earliest submission for a duplicated family).
        duplicate_note_text: The duplicate note, when flagged.
        validation_notes: Validation-failure notes for this record.

    Returns:
        The dense adj_family row with default controls and the flag.
    """
    from ccat.audit.store import (
        ADJ_FAMILY_COLUMNS,
        FAMILY_EDITABLE_COLUMNS,
        FAMILY_LINKAGE_COLUMNS,
    )

    adj: dict[str, Any] = {column: None for column in ADJ_FAMILY_COLUMNS}
    for column in (*FAMILY_LINKAGE_COLUMNS, *FAMILY_EDITABLE_COLUMNS):
        adj[column] = row.get(column)
    adj["family_id"] = row.get("family_id")
    adj["status"] = STATUS_NEW
    adj["audited"] = False
    adj["ignore"] = False
    adj["potential_duplicate"] = False
    return _flagged_row(adj, duplicate_note_text, validation_notes)


def _apply_flags(
    adj: dict[str, Any],
    duplicate_note_text: str | None,
    validation_notes: list[str],
) -> None:
    """Update the duplicate flag and notes on an existing adj row (§6.4).

    Existing rows are never re-seeded (human edits are protected), but a
    newly appearing duplicate or validation issue must still surface:
    the flag is OR-ed in and the notes are appended idempotently.

    Args:
        adj: The existing dense adj row (mutated in place).
        duplicate_note_text: The duplicate note, when flagged.
        validation_notes: Validation-failure notes for this record.
    """
    _flagged_row(adj, duplicate_note_text, validation_notes)


# ---------------------------------------------------------------------------
# The run (§6).
# ---------------------------------------------------------------------------


def _read_raw(cfg: Config, sheet: str, table_name: str) -> pl.DataFrame:
    """Read one raw sheet, failing when a config column is missing (§6.1).

    The raw sheets are written verbatim by ``ccat acquire`` and already
    keyed by config column names, so screening reads them directly.

    Args:
        cfg: Pipeline configuration.
        sheet: The sheet name (``raw_sponsor`` etc.).
        table_name: The config table key ("sponsor", "family", "child").

    Returns:
        The frame.

    Raises:
        ScreeningError: If the sheet is missing or lacks a configured
            column (run ``ccat acquire`` first).
    """
    frame = read_sheet(cfg.excel_file, sheet)
    if frame is None:
        raise ScreeningError(
            f"{sheet} sheet is missing; run `ccat acquire` first"
        )
    missing = [
        key
        for key in cfg.tables[table_name].columns
        if key not in frame.columns
    ]
    if missing:
        raise ScreeningError(
            f"{sheet} lacks column(s) {missing}; run `ccat acquire` first"
        )
    return frame


def run(
    cfg: Config,
    redo: str | None = None,
    test: bool = False,
    overwrite: bool = False,
    skip_email_check: bool = False,
    skip_address_check: bool = False,
) -> None:
    """Run module 2 end to end (§6).

    Order: local pre-audit (clean + validate-as-flags + IDs) for all
    three tables -> duplicate detection and flagging -> submissions +
    ``duplicate_log`` + ``dob_conflict_log`` written -> adj rows seeded
    for sponsors, families, and children -> ZeroBounce email check ->
    Nominatim address check -> AI screening of the children (each
    completed child persisted atomically as it arrives).

    Args:
        cfg: Pipeline configuration.
        redo: Optional zite_record_id to re-screen, replacing its adj row
            (explicit opt-in only, §6.3).
        test: Stop after the first full family: only the children of the
            first family (lowest family_id) are AI-screened.
        overwrite: Re-screen every child regardless of the ``screened``
            marker, applying the §7.3 protection rules to each row.
        skip_email_check: Skip the ZeroBounce email validation.
        skip_address_check: Skip the Nominatim address validation.

    Raises:
        ScreeningError: If the raw sheets are missing, a redo target is
            unknown, or the opencode session cannot be created.
    """
    # 1. Local pre-audit of every table (§6.2): clean, validate-as-flags.
    prepared: dict[str, list[dict[str, Any]]] = {}
    validation_notes: dict[str, dict[str, list[str]]] = {}
    for table_name in ("sponsor", "family", "child"):
        frame = _read_raw(cfg, f"raw_{table_name}", table_name)
        cleaned, notes = clean_rows(frame.to_dicts(), table_name, cfg)
        prepared[table_name] = cleaned
        validation_notes[table_name] = notes
        logger.info(
            "table %s: %d submission(s) cleaned (%d flagged)",
            table_name,
            len(cleaned),
            len(notes),
        )

    # 2. Derived IDs (§6.5); duplicate detection first (§6.4) -- flags
    #    only, never merges, so every submission keeps its row.
    duplicate_log: list[dict[str, Any]] = []
    survivors: dict[str, dict[str, dict[str, Any]]] = {}
    for table_name in ("sponsor", "family", "child"):
        log_rows, survivor_of = detect_duplicates(
            prepared[table_name], table_name
        )
        duplicate_log.extend(log_rows)
        survivors[table_name] = survivor_of
    prepared["sponsor"] = assign_sponsor_ids(prepared["sponsor"])
    prepared["family"] = assign_family_ids(prepared["family"])
    prepared["child"] = assign_angel_ids(
        prepared["child"], cfg, prepared["family"]
    )
    _cross_check_created(prepared["family"], "family")
    _cross_check_created(prepared["child"], "child")

    # 3. Sibling DOB conflicts (§6.6); duplicates of the same child
    #    never form a false pair.
    dob_log_rows = sibling_dob_conflicts(prepared["child"], cfg)

    # 4. Persist the prepared layer (§6.3): submissions, duplicate log,
    #    dob conflict log.
    sheets: dict[str, pl.DataFrame] = {
        f"{table_name}_submissions": submissions_frame(
            prepared[table_name], table_name, cfg
        )
        for table_name in ("sponsor", "family", "child")
    }
    sheets["duplicate_log"] = duplicate_log_frame(duplicate_log)
    sheets["dob_conflict_log"] = dob_conflict_log_frame(dob_log_rows)
    write_sheets(cfg.excel_file, "screen", sheets)
    logger.info(
        "wrote submissions sheets (%s) + duplicate_log (%d row(s))"
        " + dob_conflict_log (%d row(s))",
        ", ".join(
            f"{name} ({sheets[name].height})"
            for name in sheets
            if name.endswith("_submissions")
        ),
        len(duplicate_log),
        len(dob_log_rows),
    )

    # 5. Seed the adj sheets (§7.3): sponsors and families per entity,
    #    children per submission; flags/notes applied to existing rows.
    _seed_adj_sponsor_family(cfg, prepared, survivors, validation_notes)
    dob_conflict_notes = _dob_notes_by_child(dob_log_rows)

    # 6. Email + address validation logs (§6.7/§6.8); non-fatal on
    #    API errors, idempotent across runs.
    log_sheets: dict[str, pl.DataFrame] = {}
    if skip_email_check:
        logger.info("email check: skipped (--skip-email-check)")
    elif not cfg.email_check.enabled:
        logger.info("email check: disabled (config.email_check.enabled)")
    else:
        email_rows = check_emails(cfg, prepared)
        if email_rows:
            existing = _validation_log_rows(cfg, "email_validation_log")
            log_sheets["email_validation_log"] = pl.DataFrame(
                [*existing, *email_rows], schema=EMAIL_VALIDATION_LOG_SCHEMA
            )
    if skip_address_check:
        logger.info("address check: skipped (--skip-address-check)")
    elif not cfg.address_check.enabled:
        logger.info("address check: disabled (config.address_check.enabled)")
    else:
        address_rows = check_addresses(cfg, prepared["family"])
        if address_rows:
            existing = _validation_log_rows(cfg, "address_validation_log")
            log_sheets["address_validation_log"] = pl.DataFrame(
                [*existing, *address_rows],
                schema=ADDRESS_VALIDATION_LOG_SCHEMA,
            )
    if log_sheets:
        write_sheets(cfg.excel_file, "screen", log_sheets)

    # 7. AI screening of the children (§6.2): the only AI step; the
    #    prompt never carries personal data.
    children = prepared["child"]
    adj_rows = read_adj(cfg, "child")
    by_id = {row.get("zite_record_id"): row for row in adj_rows}
    # Existing child rows get flag/note updates too (idempotent), so a
    # newly appearing duplicate or validation issue surfaces without a
    # re-screen.  Orphans get the orphan note the same way.
    for adj in adj_rows:
        record_id = str(adj.get("zite_record_id") or "")
        note = duplicate_note(record_id, survivors["child"], "child")
        child_notes = validation_notes["child"].get(record_id, [])
        if adj.get("family_id") is None:
            adj["require_clarification"] = append_note(
                adj.get("require_clarification"), ORPHAN_NOTE
            )
            if adj.get("status") == STATUS_NEW:
                adj["status"] = STATUS_NEEDS_CLARIFICATION
        if note is not None or child_notes:
            _apply_flags(adj, note, child_notes)

    if redo is not None:
        targets = [
            child for child in children if child["zite_record_id"] == redo
        ]
        if not targets:
            raise ScreeningError(
                f"redo target {redo!r} not found in the prepared child rows"
            )
    else:
        if overwrite:
            targets = children
            logger.info(
                "overwrite: re-screening all %d children (existing human"
                " edits and controls are preserved, §7.3)",
                len(targets),
            )
        else:
            targets = [
                child
                for child in children
                if child["zite_record_id"] not in by_id
                or not as_bool(by_id[child["zite_record_id"]].get("screened"))
            ]
        if test:
            allowed = {
                child["zite_record_id"]
                for child in first_family_children(children)
            }
            pending = len(targets)
            targets = [
                child for child in targets if child["zite_record_id"] in allowed
            ]
            logger.info(
                "test mode: screening first family only (%d of %d pending"
                " children)",
                len(targets),
                pending,
            )
    logger.info("children to AI-screen: %d", len(targets))

    # A redo target's old row is removed up front; the upsert below then
    # replaces rows in place as results arrive.
    if redo is not None:
        adj_rows = [
            row for row in adj_rows if row.get("zite_record_id") != redo
        ]

    new_rows: list[dict[str, Any]] = []

    def persist(row: dict[str, Any]) -> None:
        """Upsert one screened row and save adj_child immediately.

        Runs in the completion loop (main thread), so workbook writes
        never race each other; ``io.write_sheets`` is atomic, so an
        interrupted save never leaves a half-written workbook.
        """
        record_id = row["zite_record_id"]
        child = next(
            (
                child
                for child in children
                if child["zite_record_id"] == record_id
            ),
            None,
        )
        if child is None:  # pragma: no cover - targets come from children
            return
        dense = dense_adj_row(child, row, cfg, by_id.get(record_id))
        dense["potential_duplicate"] = bool(
            dense.get("potential_duplicate")
            or child["zite_record_id"] in survivors["child"]
        )
        dense["require_clarification"] = append_note(
            dense.get("require_clarification"),
            duplicate_note(record_id, survivors["child"], "child"),
        )
        dense["potential_anomoly"] = _merge_anomoly(
            dense.get("potential_anomoly"), dob_conflict_notes.get(record_id)
        )
        dense["require_clarification"] = append_note(
            dense.get("require_clarification"),
            "; ".join(validation_notes["child"].get(record_id, [])) or None,
        )
        _upsert_adj_row(adj_rows, dense)
        _write_adj(cfg, adj_rows)
        new_rows.append(dense)
        logger.debug(
            "persisted adj_child row for %s (%d rows in sheet)",
            record_id,
            len(adj_rows),
        )

    if targets:
        _require_opencode_v2()
        if cfg.ai_screening.model == "TODO":
            raise ScreeningError(
                "config.ai_screening.model is still TODO; fill in the model id"
                " first (§12 item 1)"
            )
        new_rows = _screen_targets(targets, cfg, on_result=persist)

    frame = _write_adj(cfg, adj_rows)
    logger.info("wrote %d adj_child rows (%d new)", frame.height, len(new_rows))


def _dob_notes_by_child(
    dob_log_rows: list[dict[str, Any]],
) -> dict[str, str]:
    """Collect one DOB-conflict note string per affected child (§6.6)."""
    from ccat.audit.store import dob_conflict_notes

    notes: dict[str, str] = {}
    for row in dob_log_rows:
        for side in ("a", "b"):
            record_id = row.get(f"child_{side}_zite_record_id")
            if record_id is None:
                continue
            note = dob_conflict_notes(dob_log_rows, str(record_id))
            if note is not None:
                notes[str(record_id)] = note
    return notes


def _merge_anomoly(existing: str | None, note: str | None) -> str | None:
    """Idempotently append a DOB-conflict note to ``potential_anomoly``."""
    if not note:
        return existing
    text = (existing or "").strip()
    if not text:
        return note
    if note in text:
        return text
    return f"{text}; {note}"


def _seed_adj_sponsor_family(
    cfg: Config,
    prepared: dict[str, list[dict[str, Any]]],
    survivors: dict[str, dict[str, dict[str, Any]]],
    validation_notes: dict[str, dict[str, list[str]]],
) -> None:
    """Seed/refresh adj_sponsor and adj_family (§7.3, idempotent).

    Missing rows are seeded from the entity's survivor submission (for
    duplicated entities: the earliest, except child duplicates where
    the latest wins -- children are handled per submission instead).
    Existing rows only get flag/note updates, never data overwrites.

    Args:
        cfg: Pipeline configuration.
        prepared: Table name -> prepared rows.
        survivors: Table name -> record-id -> survivor row.
        validation_notes: Table name -> record-id -> validation notes.
    """
    # Sponsors: one adj row per prepared submission (sponsor_id is
    # per-submission), keyed by zite_record_id.
    sponsor_by_record = {
        str(row.get("zite_record_id") or ""): row for row in prepared["sponsor"]
    }
    adj_sponsors = read_adj(cfg, "sponsor")
    adj_by_record = {
        str(row.get("zite_record_id") or ""): row for row in adj_sponsors
    }
    seeded = 0
    for record_id, row in sponsor_by_record.items():
        existing = adj_by_record.get(record_id)
        if existing is None:
            adj_sponsors.append(
                _seed_sponsor_adj_row(
                    row,
                    duplicate_note(record_id, survivors["sponsor"], "sponsor"),
                    validation_notes["sponsor"].get(record_id, []),
                )
            )
            seeded += 1
        else:
            _apply_flags(
                existing,
                duplicate_note(record_id, survivors["sponsor"], "sponsor"),
                validation_notes["sponsor"].get(record_id, []),
            )
    logger.info("adj_sponsor: %d seeded, %d total", seeded, len(adj_sponsors))

    # Families: one adj row per family_id, seeded from the group's
    # survivor submission (the earliest).
    family_adj = read_adj(cfg, "family")
    known_family_ids = {row.get("family_id") for row in family_adj}
    survivor_family_row: dict[int, dict[str, Any]] = {}
    family_record_ids: dict[int, list[str]] = {}
    for row in prepared["family"]:
        family_id = row.get("family_id")
        if family_id is None:
            continue
        record_id = str(row.get("zite_record_id") or "")
        family_record_ids.setdefault(family_id, []).append(record_id)
        current = survivor_family_row.get(family_id)
        if current is None or (_created_value(row) or "") < (
            _created_value(current) or ""
        ):
            survivor_family_row[family_id] = row
    seeded = 0
    for family_id, row in survivor_family_row.items():
        if family_id in known_family_ids:
            continue
        family_adj.append(
            _seed_family_adj_row(
                row,
                duplicate_note(
                    str(row.get("zite_record_id") or ""),
                    survivors["family"],
                    "family",
                ),
                [
                    note
                    for record_id in family_record_ids.get(family_id, [])
                    for note in validation_notes["family"].get(record_id, [])
                ],
            )
        )
        seeded += 1
    for family_id, record_ids in family_record_ids.items():
        for adj in family_adj:
            if adj.get("family_id") != family_id:
                continue
            _apply_flags(
                adj,
                duplicate_note(record_ids[0], survivors["family"], "family")
                if len(record_ids) > 1
                else None,
                [
                    note
                    for record_id in record_ids
                    for note in validation_notes["family"].get(record_id, [])
                ],
            )
    logger.info("adj_family: %d seeded, %d total", seeded, len(family_adj))

    try:
        write_sheets(
            cfg.excel_file,
            "screen",
            {
                "adj_sponsor": build_adj_frame("sponsor", adj_sponsors),
                "adj_family": build_adj_frame("family", family_adj),
            },
        )
    except Exception as error:  # noqa: BLE001 - wrap any save failure
        raise ScreeningError(
            f"could not persist adj_sponsor/adj_family: {error}"
        ) from error
