"""Module 2 — AI screening (§6).

Adjudicates each child row with an LLM via the opencode python SDK, flags
anomalies, and appends sparse rows to the ``adj_child`` sheet for
zite_record_ids not already present.  The child's name is never passed to
the LLM; ``zite_record_id`` is the sole identifier in the prompt (§6.2).

Batch screening runs each child's LLM check on a
``concurrent.futures.ThreadPoolExecutor`` -- the calls are I/O-bound, so
threads scale them without GIL issues (same pattern as the pgi-s3
transfers).  Each worker thread owns one opencode session so concurrent
generates never share a conversation, and the shared tqdm progress bar
advances once per child under a lock as futures complete.  Ctrl-C stops
the run promptly: queued tasks are cancelled and abandoned in-flight
calls die with their threads, while every row completed so far is
already persisted (see below).

``--redo`` re-screens one child; ``--overwrite`` re-screens every child,
replacing all existing ``adj_child`` rows (§6.3).
"""

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from collections.abc import Callable

import polars as pl
from polars._typing import PolarsDataType
from pydantic import BaseModel, ConfigDict, field_validator
from tqdm import tqdm

from ccat.config import Config
from ccat.io import read_sheet, read_sheet_or_empty, write_sheets

logger = logging.getLogger(__name__)


class ScreeningError(Exception):
    """Raised when screening cannot complete."""


#: adj_child columns (§6.3), in sheet order.
ADJ_CHILD_COLUMNS = (
    "zite_record_id",
    "dob",
    "age_at_xmas",
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

#: Key set the model must return (§6.2).  ``age_at_xmas`` is excluded: it
#: is computed by acquire (§5.5) and copied into adj_child verbatim; the
#: model never owns it.
RESPONSE_COLUMNS = tuple(
    column for column in ADJ_CHILD_COLUMNS if column != "age_at_xmas"
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
        child: One raw_child row as a dict.
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
        child: The raw_child row it must join back to.
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
        child: One raw_child row as a dict.
        cfg: Pipeline configuration.
        send: Callable sending one prompt to the model session and
            returning its raw text response.

    Returns:
        The adj_child row for this child (all-null adjudication cells when
        the model returned the literal null), or None when the child could
        not be screened (logged as unscreened, picked up next run).
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
        children: raw_child rows to screen.
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


def _adj_schema() -> dict[str, PolarsDataType]:
    """Sheet schema for adj_child: every column is a string."""
    return {column: pl.String for column in ADJ_CHILD_COLUMNS}


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


def _write_adj(
    cfg: Config, adj_rows: list[dict[str, Any]], raw_child: pl.DataFrame
) -> pl.DataFrame:
    """Atomically write the adj_child sheet with ages filled in (§5.5).

    Args:
        cfg: Pipeline configuration.
        adj_rows: All current adj_child rows.
        raw_child: The raw_child frame joined for computed ages.

    Returns:
        The written adj_child frame.

    Raises:
        ScreeningError: If the workbook cannot be written.
    """
    frame = pl.DataFrame(
        _with_computed_age(adj_rows, raw_child), schema=_adj_schema()
    )
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
        children: raw_child rows as dicts.

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
        targets: raw_child rows to screen.
        cfg: Pipeline configuration.
        on_result: Optional per-row callback forwarded to
            :func:`screen_children` (see its docstring).

    Returns:
        One adj_child row per target.

    Raises:
        ScreeningError: If the opencode server cannot be started.
    """
    import os
    from pathlib import Path

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


def _with_computed_age(
    adj_rows: list[dict[str, Any]], raw_child: pl.DataFrame
) -> list[dict[str, Any]]:
    """Fill ``age_at_xmas`` on every adj row from raw_child (§5.5).

    The computed age is authoritative; the model never owns this column,
    so it is copied in verbatim (stored as text, matching the all-string
    sheet schema).  Rows screened before the column existed are
    backfilled on the next run.

    Args:
        adj_rows: adj_child rows as dicts.
        raw_child: The raw_child frame to join on.

    Returns:
        The same rows with ``age_at_xmas`` set.
    """
    ages = {
        row["zite_record_id"]: row.get("age_at_xmas")
        for row in raw_child.to_dicts()
    }
    for row in adj_rows:
        age = ages.get(row["zite_record_id"])
        row["age_at_xmas"] = None if age is None else str(age)
    return adj_rows


def _audited_count(final_child: pl.DataFrame) -> int:
    """Count audited children in final_child (§6).

    Tolerates the sheet's Boolean columns reading back as strings
    ("true"/"false") after another module rewrites the workbook.

    Args:
        final_child: The final_child frame (or any frame).

    Returns:
        The number of rows with ``audited`` true; 0 when the column is
        missing or empty.
    """
    if "audited" not in final_child.columns:
        return 0
    audited_column = final_child["audited"]
    if audited_column.dtype == pl.String:
        text = audited_column.str.strip_chars().str.to_lowercase()
        audited_column = text == "true"
    return int(audited_column.fill_null(False).sum())


def run(
    cfg: Config,
    redo: str | None = None,
    test: bool = False,
    overwrite: bool = False,
) -> None:
    """Run module 2 end to end (§6).

    Successfully screened rows are persisted to ``adj_child`` as they
    arrive (one atomic workbook write per completed child), so an
    interrupted run keeps every completed result; the final write
    reconciles the whole sheet.

    Args:
        cfg: Pipeline configuration.
        redo: Optional zite_record_id to re-screen, replacing its adj row
            (explicit opt-in only, §6.3).
        test: Stop after the first full family: only the children of the
            first family (lowest family_id) are screened.
        overwrite: Ignore the existing ``adj_child`` sheet: every child
            is re-screened and its adj row replaced (§6.3).

    Raises:
        ScreeningError: If raw_child is missing, a redo target is unknown,
            or the opencode session cannot be created.
    """
    if cfg.ai_screening.model == "TODO":
        raise ScreeningError(
            "config.ai_screening.model is still TODO; fill in the model id"
            " first (§12 item 1)"
        )
    raw_child = read_sheet(cfg.excel_file, "raw_child")
    if raw_child is None or raw_child.is_empty():
        raise ScreeningError(
            "raw_child sheet is missing or empty; run `ccat acquire` first"
        )
    children = raw_child.to_dicts()
    adj = read_sheet_or_empty(cfg.excel_file, "adj_child", _adj_schema())
    adj_rows = adj.to_dicts()
    existing_ids = {row.get("zite_record_id") for row in adj_rows}
    final_child = read_sheet(cfg.excel_file, "final_child")
    audited = _audited_count(final_child) if final_child is not None else 0
    logger.info("children already audited in final_child: %d", audited)

    if redo is not None:
        targets = [
            child for child in children if child["zite_record_id"] == redo
        ]
        if not targets:
            raise ScreeningError(f"redo target {redo!r} not found in raw_child")
    else:
        if overwrite:
            targets = children
            logger.info(
                "overwrite: re-screening all %d children (existing adj rows"
                " are replaced)",
                len(targets),
            )
        else:
            targets = [
                child
                for child in children
                if child["zite_record_id"] not in existing_ids
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
    logger.info("children to screen: %d", len(targets))

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
        _upsert_adj_row(adj_rows, row)
        _write_adj(cfg, adj_rows, raw_child)
        new_rows.append(row)
        logger.debug(
            "persisted adj_child row for %s (%d rows in sheet)",
            row["zite_record_id"],
            len(adj_rows),
        )

    if targets:
        new_rows = _screen_targets(targets, cfg, on_result=persist)

    frame = _write_adj(cfg, adj_rows, raw_child)
    logger.info("wrote %d adj_child rows (%d new)", frame.height, len(new_rows))
