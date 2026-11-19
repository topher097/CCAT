"""DB-mode clarification services (plan §9.6, decisions D5/D16/D22).

Extraction, AI draft generation, versioned draft edits, contact
status, and CSV export against the DB-era repositories:

- Extraction (§9.6): independently eligible non-ignored/non-orphan
  children with nonblank ``clarification_notes`` — audited status is
  **not** required.  Family contacts join independently from the
  eligible family frame, so an ignored family's child stays in the
  table with blank contacts.  The DOB is the verbatim raw
  ``raw_child.dob`` joined by source record id, never the adjudicated
  value.  Column order/names reuse :mod:`ccat.clarify.extract`.
- Draft generation (D5): prompts come from
  :func:`ccat.clarify.email_gen.build_prompt` (family names/contacts
  deliberately included per decision D22 — **never logged**).  One
  fresh opencode session per prompt (the same pattern as
  :class:`ccat.clarify.email_gen.EmailSender`); malformed model output
  is retried once (legacy policy), transport errors are not retried
  per family — they abort the job retryably so the worker's retry
  budget applies (§6.1).  Completed drafts stay saved; a rerun resumes
  with the remaining families.  Paragraphs are keyed per child
  (``zite_record_id``/``angel_id``/``order``), never positional.
  Email is **never sent**.
- Edits bump the draft version through ``repo.save_draft`` with an
  explicit expected version (stale edits raise :class:`StaleDraftError`).
- Contact status stamps UTC ``contacted_at``; the repository's
  tri-state upsert preserves manual nulls/timestamps.
- CSV export applies the D16 formula-injection escaping (cells
  beginning ``=``, ``+``, ``-``, ``@`` get an apostrophe prefix).

Artifacts (rendered draft HTML + preview fragment, the export CSV)
live under the artifact root ``clarify/``.
"""

import asyncio
import csv as csv_module
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import polars as pl

from ccat.audit.store import build_final_frame, eligible_output_rows
from ccat.clarify.email_gen import (
    ClarifyEmail,
    EmailSender,
    default_subject,
    generate_email,
    group_families,
    render_email,
    render_email_fragment,
)
from ccat.clarify.extract import (
    CHILD_COLUMNS,
    ClarifyError,
    EMAIL_FILE_COLUMN,
    FAMILY_COLUMNS,
    OUTPUT_COLUMNS,
)
from ccat.clarify.status import (
    CONTACTED_AT_COLUMN,
    CONTACTED_COLUMN,
    REPLIED_COLUMN,
)
from ccat.db.artifacts import artifact_root, register_file
from ccat.services.db_pipeline import _store
from ccat.services.results import ClarifyExportResult, ClarifyGenerateResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from ccat.config import Config
    from ccat.db.repositories import SeasonStoreRepository

logger = logging.getLogger(__name__)


class StaleDraftError(Exception):
    """The expected draft version is not the latest (plan §4.4)."""


class OpencodeTransportError(ClarifyError):
    """OpenCode connect/session transport failure — retryable (§6.1)."""


#: D16: cell prefixes a spreadsheet evaluates as a formula.
_ESCAPABLE_PREFIXES = ("=", "+", "-", "@")


def escape_csv_cell(value: Any) -> Any:
    """Escape one CSV cell against spreadsheet formula injection (D16).

    Args:
        value: The cell value.

    Returns:
        The value with an apostrophe prefix when it begins with ``=``.
        ``+``, ``-``, or ``@``; otherwise unchanged (non-strings pass
        through).
    """
    if isinstance(value, str) and value.startswith(_ESCAPABLE_PREFIXES):
        return "'" + value
    return value


def _empty_table() -> pl.DataFrame:
    """Return an empty clarification table with the legacy output schema."""
    return pl.DataFrame(schema={column: pl.String for column in OUTPUT_COLUMNS})


def _build_table(
    child_rows: "list[dict[str, Any]]",
    family_rows: "list[dict[str, Any]]",
    raw_child_rows: "list[dict[str, Any]]",
) -> "list[dict[str, Any]]":
    """Build the clarification table rows from repository data (§9.6).

    Port of the legacy ``build_table`` joins with DB eligibility: the
    child/family stores are filtered independently, so ignoring one
    entity never cascades into the other.

    Args:
        child_rows: Complete final-child rows.
        family_rows: Complete final-family rows.
        raw_child_rows: The active raw-child snapshot rows.

    Returns:
        One dict per flagged angel in :data:`OUTPUT_COLUMNS` order,
        sorted by ``family_id`` then ``angel_id``.

    Raises:
        ClarifyError: When a required store/column is missing.
    """
    children = build_final_frame(
        "child", eligible_output_rows(child_rows, "child")
    )
    if children.is_empty():
        raise ClarifyError(
            "final_child has no output-eligible rows or is missing"
        )
    missing = [
        column for column in CHILD_COLUMNS if column not in children.columns
    ]
    if missing:
        raise ClarifyError(f"final_child lacks column(s): {missing}")
    note = pl.col("clarification_notes").cast(pl.String).str.strip_chars()
    flagged = children.filter(
        note.is_not_null() & (note.str.strip_chars() != "")
    ).select(CHILD_COLUMNS)
    if flagged.is_empty():
        logger.info("no eligible final_child row carries a clarification note")
        return []
    families = build_final_frame(
        "family", eligible_output_rows(family_rows, "family")
    )
    if families.is_empty():
        raise ClarifyError("final_family sheet is missing or empty")
    missing_family = [
        column for column in FAMILY_COLUMNS if column not in families.columns
    ]
    if missing_family:
        raise ClarifyError(f"final_family lacks column(s): {missing_family}")
    family_contact = families.select(
        pl.col("family_id"),
        pl.col("first_name").alias("family_first_name"),
        pl.col("last_name").alias("family_last_name"),
        pl.col("phone").alias("family_phone"),
        pl.col("primary_email").alias("family_email"),
    )
    table = flagged.join(family_contact, on="family_id", how="left")
    if raw_child_rows:
        raw_child = pl.DataFrame(raw_child_rows)
        raw_dob = raw_child.select(
            pl.col("zite_record_id"),
            pl.col("dob").cast(pl.String).alias("raw_dob"),
        )
        table = table.join(raw_dob, on="zite_record_id", how="left")
    else:
        table = table.with_columns(
            pl.lit(None, dtype=pl.String).alias("raw_dob")
        )
        logger.info("no raw_child snapshot; raw DOB columns are blank")
    return table.sort("family_id", "angel_id").select(OUTPUT_COLUMNS).to_dicts()


async def _extract_rows_repo(
    repo: "SeasonStoreRepository",
) -> "list[dict[str, Any]]":
    """Read the clarification table through one repository.

    Args:
        repo: Season repository (read-only).

    Returns:
        The flagged-angel rows (see :func:`_build_table`).
    """
    child_rows = await repo.final_rows("child")
    family_rows = await repo.final_rows("family")
    raw_child_rows = await repo.raw_rows("child")
    return _build_table(child_rows, family_rows, raw_child_rows)


async def extract_rows(cfg: "Config") -> "list[dict[str, Any]]":
    """Extract the clarification table from the season's stores (§9.6).

    Args:
        cfg: Pipeline configuration.

    Returns:
        One dict per eligible flagged angel (possibly empty), with
        family contacts, raw DOB, and :data:`OUTPUT_COLUMNS` order.

    Raises:
        ClarifyError: When a required store/column is missing.
    """
    async with _store(cfg, "system") as repo:
        return await _extract_rows_repo(repo)


def _table_frame(rows: "list[dict[str, Any]]") -> pl.DataFrame:
    """Frame the extracted rows for ``group_families``.

    Args:
        rows: The extracted table rows.

    Returns:
        A frame; empty (with the output schema) when no angel is
        flagged.
    """
    if rows:
        return pl.DataFrame(rows)
    return _empty_table()


def keyed_paragraphs(
    family: Any, email: ClarifyEmail
) -> "list[dict[str, Any]]":
    """Key one email's paragraphs per child (decision D5).

    Args:
        family: The family whose angels the paragraphs address.
        email: The validated prose fragments (one paragraph per angel,
            in family order).

    Returns:
        One ``{zite_record_id, angel_id, order, text}`` dict per
        angel.
    """
    return [
        {
            "zite_record_id": angel.zite_record_id,
            "angel_id": angel.angel_id,
            "order": order,
            "text": text,
        }
        for order, (angel, text) in enumerate(
            zip(family.angels, email.angel_paragraphs, strict=True)
        )
    ]


def _default_sender(cfg: "Config") -> Any:
    """Build the prompt sender (opencode, one session per prompt).

    Args:
        cfg: Pipeline configuration.

    Returns:
        An :class:`ccat.clarify.email_gen.EmailSender`.
    """
    return EmailSender(cfg)


def _transport_send(sender: Any) -> "Callable[[str], str]":
    """Wrap one sender so transport failures classify retryable (§6.1).

    Args:
        sender: Any object with a blocking ``send(prompt) -> str``.

    Returns:
        A sync ``send`` callable raising
        :class:`OpencodeTransportError` for provider failures; model
        output problems never raise here (they surface at parse time).
    """
    send = sender.send

    def send_one(prompt: str) -> str:
        try:
            return send(prompt)
        except ClarifyError as error:
            raise OpencodeTransportError(str(error)) from error

    return send_one


async def _write_draft_artifacts(
    repo: "SeasonStoreRepository",
    cfg: "Config",
    family: Any,
    email: ClarifyEmail,
    subject: str,
    actor: str,
) -> None:
    """Render and register one family's draft HTML artifacts (§9.6).

    Args:
        repo: Season repository (artifact registration).
        cfg: Pipeline configuration.
        family: The family the draft addresses.
        email: The prose fragments.
        subject: The draft's subject line.
        actor: Requesting user/job identity.
    """
    _subject, html = render_email(family, email, cfg, subject)
    fragment = render_email_fragment(family, email, cfg, subject)
    out_dir = artifact_root(cfg) / "clarify"
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {"family_id": family.family_id}
    html_file = out_dir / f"family_{family.family_id}.html"
    fragment_file = out_dir / f"family_{family.family_id}.fragment.html"
    html_file.write_text(html, encoding="utf-8")
    fragment_file.write_text(fragment, encoding="utf-8")
    await register_file(
        repo,
        "clarify_draft",
        html_file,
        actor,
        meta=meta,
        root=artifact_root(cfg),
    )
    await register_file(
        repo,
        "clarify_draft_fragment",
        fragment_file,
        actor,
        meta=meta,
        root=artifact_root(cfg),
    )


async def generate_drafts(
    cfg: "Config",
    actor: str,
    options: "dict[str, Any]",
    progress: "Callable[..., None]",
    cancel: "Callable[[], None]",
) -> ClarifyGenerateResult:
    """Generate missing clarification drafts, one family at a time (§9.6).

    Targets are flagged families without a current draft (all of them
    with ``overwrite``).  Malformed model output retries once per
    family and then reports that family as failed while the job
    continues (partial success); a transport error aborts the job
    retryably — completed drafts stay saved and a rerun resumes.

    Args:
        cfg: Pipeline configuration.
        actor: Requesting user/job identity.
        options: ``overwrite`` (regenerate families that already have
            a draft).
        progress: Progress reporter.
        cancel: Cooperative cancel check (honored between families).

    Returns:
        The generated and failed family ids.

    Raises:
        ClarifyError: When extraction fails, or (transport errors)
            propagated for the job's retry budget (§6.1).
        JobCancelRequested: When cancellation was requested.
    """
    overwrite = bool(options.get("overwrite"))
    rows = await extract_rows(cfg)
    families = group_families(_table_frame(rows))
    progress("generate", total=len(families))
    async with _store(cfg, actor) as repo:
        targets: list[Any] = []
        for family in families:
            if overwrite or await repo.latest_draft(family.family_id) is None:
                targets.append(family)
        if not targets:
            logger.info("no families await a clarification draft")
            return ClarifyGenerateResult()
        logger.info(
            "generating %d of %d flagged family draft(s)",
            len(targets),
            len(families),
        )
        sender = _default_sender(cfg)
        send = _transport_send(sender)
        generated: list[int] = []
        failed: list[int] = []
        try:
            for index, family in enumerate(targets, start=1):
                cancel()
                try:
                    email = await asyncio.to_thread(
                        generate_email, family, cfg, send
                    )
                except OpencodeTransportError:
                    # Provider down: fail the job retryably (§6.1);
                    # completed drafts stay saved and a rerun resumes.
                    raise
                except ClarifyError as error:
                    failed.append(family.family_id)
                    logger.error(
                        "family %d: draft generation failed: %s",
                        family.family_id,
                        error,
                    )
                    progress("generate", completed=index, total=len(targets))
                    continue
                subject = default_subject(cfg)
                await repo.save_draft(
                    family.family_id,
                    subject,
                    email.greeting,
                    email.intro,
                    email.closing,
                    keyed_paragraphs(family, email),
                    actor,
                    generated_by=actor,
                )
                await _write_draft_artifacts(
                    repo, cfg, family, email, subject, actor
                )
                generated.append(family.family_id)
                progress("generate", completed=index, total=len(targets))
        finally:
            close = getattr(sender, "close", None)
            if callable(close):
                await asyncio.to_thread(close)
    return ClarifyGenerateResult(
        generated_family_ids=generated, failed_family_ids=failed
    )


async def update_draft(
    cfg: "Config",
    actor: str,
    family_id: int,
    expected_version: int,
    fields: "dict[str, Any]",
) -> "dict[str, Any]":
    """Edit the latest draft at an explicit version (plan §4.4, D5).

    Saves a new version through ``repo.save_draft`` (``edited_by``/
    ``edited_at`` stamped) and re-renders the HTML artifacts when the
    family still has flagged angels.

    Args:
        cfg: Pipeline configuration.
        actor: Requesting user identity.
        family_id: The family's key.
        expected_version: The version the caller edited.
        fields: Supplied fields (``subject``/``greeting``/``intro``/
            ``paragraphs``/``closing``); missing keys keep the current
            value.

    Returns:
        The persisted new draft version dict.

    Raises:
        ClarifyError: When the family has no draft at all.
        StaleDraftError: When ``expected_version`` is not the latest.
    """
    async with _store(cfg, actor) as repo:
        latest = await repo.latest_draft(family_id)
        if latest is None:
            raise ClarifyError(f"family {family_id} has no draft to edit")
        if latest["version"] != expected_version:
            raise StaleDraftError(
                f"family {family_id} draft is at version"
                f" {latest['version']}, not {expected_version}"
            )
        merged = {
            "subject": fields.get("subject", latest["subject"]),
            "greeting": fields.get("greeting", latest["greeting"]),
            "intro": fields.get("intro", latest["intro"]),
            "paragraphs": fields.get("paragraphs", latest["paragraphs"]),
            "closing": fields.get("closing", latest["closing"]),
        }
        draft = await repo.save_draft(
            family_id,
            merged["subject"],
            merged["greeting"],
            merged["intro"],
            merged["closing"],
            merged["paragraphs"],
            actor,
            generated_by=latest["generated_by"],
        )
        # Best-effort artifact refresh: the DB row is the truth; the
        # HTML is a render of the family context.
        rows = await _extract_rows_repo(repo)
        family = next(
            (
                entry
                for entry in group_families(_table_frame(rows))
                if entry.family_id == family_id
            ),
            None,
        )
        if family is None:
            logger.info(
                "family %d no longer has flagged angels; draft HTML"
                " artifact not re-rendered",
                family_id,
            )
        else:
            paragraphs = sorted(
                draft["paragraphs"] or [],
                key=lambda paragraph: paragraph.get("order", 0),
            )
            email = ClarifyEmail(
                greeting=draft["greeting"] or "",
                intro=draft["intro"] or "",
                angel_paragraphs=[
                    str(paragraph.get("text") or "") for paragraph in paragraphs
                ],
                closing=draft["closing"] or "",
            )
            await _write_draft_artifacts(
                repo,
                cfg,
                family,
                email,
                draft["subject"] or "",
                actor,
            )
    return draft


async def set_contacted(
    cfg: "Config",
    actor: str,
    family_id: int,
    contacted: bool = True,
) -> "dict[str, Any]":
    """Mark one family contacted, stamping UTC ``contacted_at`` (§9.6).

    Args:
        cfg: Pipeline configuration.
        actor: Requesting user identity.
        family_id: The family's key.
        contacted: The new contacted flag.

    Returns:
        The persisted contact-state dict.
    """
    async with _store(cfg, actor) as repo:
        return await repo.set_contact_status(
            family_id,
            contacted=contacted,
            contacted_at=(
                datetime.now(UTC).isoformat(timespec="seconds")
                if contacted
                else None
            ),
            actor=actor,
        )


async def set_replied(
    cfg: "Config",
    actor: str,
    family_id: int,
    replied: bool = True,
) -> "dict[str, Any]":
    """Mark one family replied (§9.6).

    Args:
        cfg: Pipeline configuration.
        actor: Requesting user identity.
        family_id: The family's key.
        replied: The new replied flag.

    Returns:
        The persisted contact-state dict.
    """
    async with _store(cfg, actor) as repo:
        return await repo.set_contact_status(
            family_id, replied=replied, actor=actor
        )


async def export_csv(cfg: "Config", actor: str) -> ClarifyExportResult:
    """Write the clarifications CSV as a tracked artifact (§9.6, D16).

    Reconstructs the legacy export: the extraction columns plus the
    family's current draft artifact path reference and the per-family
    contact status.  Every cell is formula-escaped (D16).

    Args:
        cfg: Pipeline configuration.
        actor: Requesting user/job identity.

    Returns:
        The export result (artifact path + data-row count).

    Raises:
        ClarifyError: When a required store/column is missing.
    """
    async with _store(cfg, actor) as repo:
        table_rows = await _extract_rows_repo(repo)
        statuses = {
            status["family_id"]: status
            for status in await repo.clarification_families()
        }
        draft_by_family: dict[int, dict[str, Any]] = {}
        for artifact in await repo.artifact_rows("clarify_draft"):
            name = str(artifact.get("filename") or "")
            if not (name.startswith("family_") and name.endswith(".html")):
                continue
            try:
                family_id = int(name[len("family_") : -len(".html")])
            except ValueError:
                continue
            # artifact_rows are id-ordered; the last write wins, so the
            # loop ends holding each family's current draft artifact.
            draft_by_family[family_id] = artifact
    header = (
        *OUTPUT_COLUMNS,
        EMAIL_FILE_COLUMN,
        CONTACTED_COLUMN,
        CONTACTED_AT_COLUMN,
        REPLIED_COLUMN,
    )
    out_dir = artifact_root(cfg) / "clarify"
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "clarifications.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv_module.writer(handle)
        writer.writerow(header)
        for row in table_rows:
            family_raw = row.get("family_id")
            status = (
                statuses.get(int(family_raw))
                if family_raw is not None
                else None
            )
            artifact = (
                draft_by_family.get(int(family_raw))
                if family_raw is not None
                else None
            )
            values = [row.get(column) for column in OUTPUT_COLUMNS]
            values.append(
                artifact.get("path") if artifact is not None else None
            )
            values.extend(
                [
                    bool(status[CONTACTED_COLUMN]) if status else False,
                    status.get(CONTACTED_AT_COLUMN) if status else None,
                    bool(status[REPLIED_COLUMN]) if status else False,
                ]
            )
            writer.writerow([escape_csv_cell(value) for value in values])
    async with _store(cfg, actor) as repo:
        await register_file(
            repo,
            "clarify_export",
            csv_path,
            actor,
            meta={"rows": len(table_rows)},
            root=artifact_root(cfg),
        )
    logger.info(
        "wrote %d clarification row(s) to %s", len(table_rows), csv_path
    )
    return ClarifyExportResult(csv_path=str(csv_path), rows=len(table_rows))


__all__ = [
    "OpencodeTransportError",
    "StaleDraftError",
    "escape_csv_cell",
    "export_csv",
    "extract_rows",
    "generate_drafts",
    "keyed_paragraphs",
    "set_contacted",
    "set_replied",
    "update_draft",
]
