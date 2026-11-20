"""Module 2 — local preparation and resumable AI screening.

Every run cleans all verbatim ``raw_*`` rows in memory, assigns/reconciles
stable IDs, refreshes duplicate and sibling-DOB diagnostics, and performs
only previously unlogged ZeroBounce/Nominatim checks. Prepared submissions
are never persisted as a second entity store.

The full ``final_*`` sheets are the durable truth. Genuinely new entities
are seeded; existing rows receive only source-metadata, stable-linkage,
machine-diagnostic, and computed-field merges. Human edits, audit controls,
assignments, generation state, ignored/orphan rows, and raw-missing rows
survive. New children start unscreened.

Ordinary AI work reads the final-child ``screened`` marker and skips completed
rows. ``--redo`` and ``--overwrite`` deliberately clear only target markers
before work, so malformed/failed/interrupted attempts remain retryable.
Each successful result updates AI-eligible fields under the existing human-
baseline protection and persists ``screened=True`` with the complete child
row on the main thread. Prompts contain no names or contact information.
"""

import json
import logging
import os
import re
import subprocess
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

from ccat.acquire import dob_conflict_log_frame
from ccat.audit.store import (
    FINAL_CHILD_COLUMNS,
    CHILD_EDITABLE_COLUMNS,
    CHILD_LINKAGE_COLUMNS,
    ORPHAN_NOTE,
    STATUS_NEEDS_CLARIFICATION,
    STATUS_NEW,
    as_bool,
    _computed_age,
    build_final_frame,
    prepare_persistent_store,
    reconcile_preparation,
    write_final_rows,
)
from ccat.config import Config
from ccat.io import read_sheet, write_sheets
from ccat.preparation import (
    append_note,
    dob_notes_by_child,
    duplicate_log_frame,
    duplicate_note,
)

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
    exit, so screening must refuse to run.  A remote server named by
    ``OPENCODE_BASE_URL`` needs no local CLI, so the check is skipped.

    Raises:
        ScreeningError: If opencode is missing, unparseable, or older
            than version 2.
    """
    if os.environ.get("OPENCODE_BASE_URL"):
        return
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
# Email (ZeroBounce) and address (Nominatim) validation (§6.7/§6.8).
# ---------------------------------------------------------------------------


def _email_engine() -> tuple[Callable[..., Any], type[Exception]]:
    """Import the ZeroBounce engine (§6.7).

    Returns:
        A tuple of
        (:func:`ccat.engines.zerobounce_email_check.validate_email`,
        its ``OutOfCreditsError``).

    Raises:
        ScreeningError: If the zerobouncesdk package is unavailable.
    """
    try:
        from ccat.engines.zerobounce_email_check import (
            OutOfCreditsError,
            validate_email,
        )
    except ImportError as error:
        raise ScreeningError(
            "could not import the ZeroBounce engine"
            f" (ccat/engines/zerobounce_email_check.py): {error}; install"
            " zerobouncesdk or set config.email_check.enabled = false"
        ) from error
    return validate_email, OutOfCreditsError


def _email_batch_engine() -> Callable[..., Any] | None:
    """Import the ZeroBounce batch (bulk) engine (§6.7).

    Returns:
        :func:`ccat.engines.zerobounce_email_check.bulk_validate_emails`,
        or None when the engine cannot be imported -- the email check
        then falls back to threaded per-email calls.
    """
    try:
        from ccat.engines.zerobounce_email_check import bulk_validate_emails
    except ImportError:
        return None
    return bulk_validate_emails


def _address_engine() -> tuple[Callable[..., Any], Callable[..., str]]:
    """Import the Nominatim engine (§6.8).

    Returns:
        A tuple of
        (:func:`ccat.engines.nominatim_address_check.check_address`,
        :func:`ccat.engines.nominatim_address_check.build_query`).

    Raises:
        ScreeningError: If the requests package is unavailable.
    """
    try:
        from ccat.engines.nominatim_address_check import (
            build_query,
            check_address,
        )
    except ImportError as error:
        raise ScreeningError(
            "could not import the Nominatim engine"
            f" (ccat/engines/nominatim_address_check.py): {error}"
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


#: Model-owned final-child fields (§6.2): the columns the model may
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
    """Pydantic schema for one AI screening response (§6.2)."""

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
            return {column: None for column in FINAL_CHILD_COLUMNS} | {
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
        The final-child rows to persist (one per successfully screened
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


def dense_final_row(
    child: dict[str, Any],
    corrections: dict[str, Any],
    cfg: Config,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one dense final-child row from a screening result (§7.3).

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
        existing: The child's existing full final row, when one exists.

    Returns:
        The dense final-child row with ``screened`` set true.
    """
    row: dict[str, Any] = {column: None for column in FINAL_CHILD_COLUMNS}
    for column in (*CHILD_LINKAGE_COLUMNS, *CHILD_EDITABLE_COLUMNS):
        row[column] = child.get(column)
    if existing is not None and as_bool(existing.get("ignore")):
        # AI never targets ignored rows; this defensive path preserves the
        # complete skeleton and records only successful screening.
        row = dict(existing)
        row["screened"] = True
        return row
    if existing is not None:
        for column in (
            *CHILD_EDITABLE_COLUMNS,
            "age_at_xmas",
            "generated_info_sheet",
            "status",
            "ignore",
            "clarification_notes",
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
        row["ignore"] = False
        row["generated_info_sheet"] = False
        row["potential_duplicate"] = False
    row["age_at_xmas"] = _computed_age(row.get("dob"), cfg)
    if row.get("family_id") is None:
        # Orphaned child: no matching family registration; surface it
        # for the user to fix (§8.7).
        row["clarification_notes"] = append_note(
            row.get("clarification_notes"), ORPHAN_NOTE
        )
        if row.get("status") == STATUS_NEW:
            row["status"] = STATUS_NEEDS_CLARIFICATION
    return row


def _upsert_final_row(
    final_rows: list[dict[str, Any]], row: dict[str, Any]
) -> None:
    """Insert or replace one final row in place, keyed by zite_record_id.

    Args:
        final_rows: The mutable final-child row list.
        row: The row to insert (new id) or replace (existing id).
    """
    for index, existing in enumerate(final_rows):
        if existing.get("zite_record_id") == row["zite_record_id"]:
            final_rows[index] = row
            return
    final_rows.append(row)


def _write_final_children(
    cfg: Config, final_rows: list[dict[str, Any]]
) -> pl.DataFrame:
    """Atomically write the complete persistent final-child store.

    Args:
        cfg: Pipeline configuration.
        final_rows: Every persistent child, including raw-missing rows.

    Returns:
        The validated written frame.

    Raises:
        ScreeningError: If the workbook cannot be written.
    """
    frame = build_final_frame("child", final_rows)
    try:
        write_final_rows(cfg, "screen", "child", final_rows)
    except Exception as error:  # noqa: BLE001 - wrap any save failure
        raise ScreeningError(
            f"could not persist final_child: {error}"
        ) from error
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
        One final-child row per target.

    Raises:
        ScreeningError: If the opencode server cannot be started.
    """
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
# Persistent final seeding for sponsors and families (shared contract).
# ---------------------------------------------------------------------------


def _flagged_row(
    row: dict[str, Any],
    duplicate_note_text: str | None,
    validation_notes: list[str],
) -> dict[str, Any]:
    """Apply the duplicate flag and notes to one persistent row.

    Args:
        row: The full final row (mutated in place).
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
        row["clarification_notes"] = append_note(
            row.get("clarification_notes"), note or None
        )
    return row


def _apply_flags(
    final_row: dict[str, Any],
    duplicate_note_text: str | None,
    validation_notes: list[str],
) -> None:
    """Merge duplicate/validation diagnostics into one persistent row.

    Existing rows are never re-seeded; a newly appearing duplicate or
    validation issue still surfaces idempotently.

    Args:
        final_row: Existing full row, mutated in place.
        duplicate_note_text: The duplicate note, when flagged.
        validation_notes: Validation-failure notes for this record.
    """
    _flagged_row(final_row, duplicate_note_text, validation_notes)


# ---------------------------------------------------------------------------
# The run (§6).
# ---------------------------------------------------------------------------


def run(
    cfg: Config,
    redo: str | None = None,
    test: bool = False,
    overwrite: bool = False,
    skip_email_check: bool = False,
    skip_address_check: bool = False,
) -> None:
    """Prepare persistent finals, run external checks, and resume AI work.

    Local preparation and global diagnostics run for every raw record. Only
    children whose final row is unscreened enter ordinary AI work. Explicit
    redo/overwrite runs clear the target success marker first so failures or
    interruption remain retryable without deleting any entity state.

    Args:
        cfg: Pipeline configuration.
        redo: Optional current child ``zite_record_id`` to re-screen.
        test: Restrict AI candidates to the first stable family.
        overwrite: Deliberately re-screen every current child.
        skip_email_check: Skip ZeroBounce work for this run.
        skip_address_check: Skip Nominatim work for this run.

    Raises:
        ScreeningError: If a redo key is unknown or AI setup/save fails.
    """
    preparation = prepare_persistent_store(cfg)
    children = preparation.rows["child"]
    if redo is not None and not any(
        child.get("zite_record_id") == redo for child in children
    ):
        raise ScreeningError(
            f"redo target {redo!r} not found in the prepared child rows"
        )

    stores = reconcile_preparation(
        cfg, preparation, "screen", refresh_existing=True
    )
    log_sheets: dict[str, pl.DataFrame] = {
        "duplicate_log": duplicate_log_frame(preparation.duplicate_log),
        "dob_conflict_log": dob_conflict_log_frame(preparation.dob_log),
    }
    write_sheets(cfg.excel_file, "screen", log_sheets)
    logger.info(
        "refreshed duplicate_log (%d) and dob_conflict_log (%d)",
        len(preparation.duplicate_log),
        len(preparation.dob_log),
    )

    if skip_email_check:
        logger.info("email check: skipped (--skip-email-check)")
    elif not cfg.email_check.enabled:
        logger.info("email check: disabled (config.email_check.enabled)")
    else:
        email_rows = check_emails(cfg, preparation.rows)
        if email_rows:
            existing = _validation_log_rows(cfg, "email_validation_log")
            # Commit billable successes before any later phase can fail.
            write_sheets(
                cfg.excel_file,
                "screen",
                {
                    "email_validation_log": pl.DataFrame(
                        [*existing, *email_rows],
                        schema=EMAIL_VALIDATION_LOG_SCHEMA,
                    )
                },
            )
            logger.info(
                "persisted %d new email validation row(s)",
                len(email_rows),
            )
    if skip_address_check:
        logger.info("address check: skipped (--skip-address-check)")
    elif not cfg.address_check.enabled:
        logger.info("address check: disabled (config.address_check.enabled)")
    else:
        address_rows = check_addresses(cfg, preparation.rows["family"])
        if address_rows:
            existing = _validation_log_rows(cfg, "address_validation_log")
            write_sheets(
                cfg.excel_file,
                "screen",
                {
                    "address_validation_log": pl.DataFrame(
                        [*existing, *address_rows],
                        schema=ADDRESS_VALIDATION_LOG_SCHEMA,
                    )
                },
            )
            logger.info(
                "persisted %d new address validation row(s)",
                len(address_rows),
            )

    final_rows = stores["child"]
    by_id = {str(row.get("zite_record_id") or ""): row for row in final_rows}
    if redo is not None:
        if as_bool(by_id[redo].get("ignore")):
            raise ScreeningError(
                f"redo target {redo!r} is ignored; un-ignore it first"
            )
        targets = [
            child for child in children if child.get("zite_record_id") == redo
        ]
    elif overwrite:
        targets = [
            child
            for child in children
            if not as_bool(
                by_id[str(child.get("zite_record_id") or "")].get("ignore")
            )
        ]
    else:
        targets = [
            child
            for child in children
            if not as_bool(
                by_id[str(child.get("zite_record_id") or "")].get("screened")
            )
            and not as_bool(
                by_id[str(child.get("zite_record_id") or "")].get("ignore")
            )
        ]
    if test and redo is None:
        allowed = {
            child.get("zite_record_id")
            for child in first_family_children(children)
        }
        targets = [
            child for child in targets if child.get("zite_record_id") in allowed
        ]
    logger.info("children to AI-screen: %d", len(targets))

    if (redo is not None or overwrite) and targets:
        for target in targets:
            existing = by_id[str(target.get("zite_record_id") or "")]
            existing["screened"] = False
        _write_final_children(cfg, final_rows)

    conflict_notes = dob_notes_by_child(preparation.dob_log)

    def persist(corrections: dict[str, Any]) -> None:
        """Persist one completed child on the main completion thread."""
        record_id = str(corrections["zite_record_id"])
        child = next(
            current
            for current in children
            if current.get("zite_record_id") == record_id
        )
        dense = dense_final_row(child, corrections, cfg, by_id.get(record_id))
        _apply_flags(
            dense,
            duplicate_note(record_id, preparation.survivors["child"], "child"),
            preparation.validation_notes["child"].get(record_id, []),
        )
        dense["potential_anomoly"] = _merge_anomoly(
            dense.get("potential_anomoly"), conflict_notes.get(record_id)
        )
        _upsert_final_row(final_rows, dense)
        by_id[record_id] = dense
        _write_final_children(cfg, final_rows)
        logger.debug("persisted final_child row for %s", record_id)

    completed: list[dict[str, Any]] = []
    if targets:
        _require_opencode_v2()
        if cfg.ai_screening.model == "TODO":
            raise ScreeningError(
                "config.ai_screening.model is still TODO; fill in the model id"
                " first (§12 item 1)"
            )
        completed = _screen_targets(targets, cfg, on_result=persist)
    logger.info(
        "final_child holds %d row(s); %d AI result(s) completed this run",
        len(final_rows),
        len(completed),
    )


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
