"""DB-mode generate service: child info sheets, then sponsor packets (§9.5).

Runs the legacy :mod:`ccat.child_info_generate` semantics against the
DB-era repositories instead of the workbook:

- Enrichment replicates the legacy ``_load_enriched_rows`` joins over
  repository frames; ``family_size`` counts the *enriched eligible*
  child population (never every stored child).
- Per-item pipeline (plan §6.3, decision D20 — a deliberate durability
  improvement over the legacy end-of-run batch commit): render ODT in
  a per-worker scratch directory under the artifact root, convert with
  LibreOffice honoring ``info_sheet.conversion_timeout_seconds``,
  validate the PDF by reopening it with pypdf, atomically rename it
  into ``child_sheets/<canonical_location>/AT<season>_<angel_id>.pdf``,
  register the artifact, and commit the ``generated_info_sheet`` marker
  immediately — one item at a time.  A crash between rename and commit
  leaves an orphan artifact with a false marker; a rerun regenerates it
  safely (the eligibility gate is the false marker, so the file is
  overwritten atomically).  The inverse (true marker, no file) cannot
  occur because the marker commits only after the validated rename.
- Cancellation (§6.2): the stop event is checked between render and
  convert and inside the conversion poll loop, which terminates the
  LibreOffice process group (legacy ``_terminate`` semantics).
  Completed items keep their artifacts and markers; the result reports
  failed stems while the remaining items finish (partial success).
- Packets are the second phase of the same job (unless ``test``):
  legacy eligibility without cascading filters, legacy
  ``validate_all_sheets_exist`` scope (decision D18 — every eligible
  child of every eligible sponsored family, regardless of audit
  status/markers), legacy skip predicate (HTML + combined PDF), and
  ``--overwrite`` preserving manual email edits by copying the existing
  HTML to ``<stem>.manual.html`` first (D20 feasible retention).

Pure/legacy helpers are reused verbatim: ``_pending_rows``,
``_nested_data``, ``_render_one``, ``_sheet_stem``, packet ordering and
templates.  Prompts/files never leave the configured artifact root.
"""

import asyncio
import logging
import os
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

import polars as pl
from jinja2 import Environment
from pypdf import PdfReader, PdfWriter

from ccat.audit.store import (
    build_final_frame,
    eligible_output_rows,
)
from ccat.child_info_generate import (
    FAMILY_JOIN_COLUMNS,
    SPONSOR_JOIN_COLUMNS,
    InfoSheetCancelledError,
    InfoSheetError,
    _nested_data,
    _pending_rows,
    _render_one,
    _sheet_stem,
    _terminate,
)
from ccat.db.artifacts import artifact_root, register_file
from ccat.jobs.errors import JobCancelRequested
from ccat.match import canonical_location
from ccat.services.db_pipeline import _store
from ccat.services.results import GenerateResult
from ccat.sponsor_packets import (
    _SUBJECT_TEMPLATE,
    SponsorPacketError,
    _angel_sort_key,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ccat.config import Config

logger = logging.getLogger(__name__)

#: Worker-count ceiling (plan §9.5: a web request cannot choose an
#: unbounded pool).  The legacy CLI accepted any value; DB-mode jobs
#: are bounded here (CLI parity note: the CLI path keeps legacy
#: behavior in :mod:`ccat.child_info_generate`).
MAX_WORKERS_CEILING = 16


def _eligible_frame(entity: str, rows: "list[dict[str, Any]]") -> pl.DataFrame:
    """Build one typed frame from the entity's output-eligible rows.

    Args:
        entity: "child", "family", or "sponsor".
        rows: The complete final rows from the repository.

    Returns:
        The eligible population as a typed full-schema frame.
    """
    return build_final_frame(entity, eligible_output_rows(rows, entity))


def _enrich(
    child_rows: "list[dict[str, Any]]",
    family_rows: "list[dict[str, Any]]",
    sponsor_rows: "list[dict[str, Any]]",
    cfg: "Config",
) -> pl.DataFrame:
    """Enrich eligible children with family/sponsor joins (§13.1).

    Port of the legacy ``_load_enriched_rows`` over repository rows:
    identical validations (missing/empty stores, non-unique family or
    sponsor ids, unmatched family ids), identical orphan handling, and
    ``family_size`` computed over this enriched eligible population.

    Args:
        child_rows: Complete final-child rows.
        family_rows: Complete final-family rows.
        sponsor_rows: Complete final-sponsor rows.
        cfg: Pipeline configuration.

    Returns:
        The enriched frame (child columns plus family/sponsor joins,
        ``sponsor_name``, and ``family_size``).

    Raises:
        InfoSheetError: When a store is empty/malformed for generation.
    """
    frame = _eligible_frame("child", child_rows)
    if frame.is_empty():
        raise InfoSheetError(
            "final_child sheet is missing or has no eligible rows;"
            " run `ccat audit` first"
        )
    if "family_id" not in frame.columns:
        raise InfoSheetError(
            "final_child has no family_id column; run `ccat match` first"
        )
    orphaned = frame.filter(pl.col("family_id").is_null())
    if not orphaned.is_empty():
        logger.warning(
            "ignoring %d final_child row(s) without family_id"
            " (orphaned; run `ccat screen`/`ccat audit` once the family"
            " registration exists): %s",
            orphaned.height,
            orphaned["zite_record_id"].to_list(),
        )
        frame = frame.filter(pl.col("family_id").is_not_null())
        if frame.is_empty():
            raise InfoSheetError(
                "every final_child row lacks a family_id (orphaned);"
                " nothing to generate"
            )
    family_frame = _eligible_frame("family", family_rows)
    if family_frame.is_empty():
        raise InfoSheetError(
            "final_family sheet is missing or empty; run `ccat match` first"
        )
    missing_columns = [
        column
        for column in FAMILY_JOIN_COLUMNS
        if column not in family_frame.columns
    ]
    if missing_columns:
        raise InfoSheetError(
            f"final_family lacks column(s): {missing_columns};"
            " run `ccat match` first"
        )
    duplicated = (
        family_frame.group_by("family_id").len().filter(pl.col("len") > 1)
    )
    if not duplicated.is_empty():
        raise InfoSheetError(
            "final_family family_id is not unique:"
            f" {duplicated['family_id'].to_list()}"
        )
    unmatched = sorted(
        set(frame["family_id"]) - set(family_frame["family_id"].drop_nulls())
    )
    if unmatched:
        raise InfoSheetError(
            f"final_family has no row for family_id(s): {unmatched};"
            " run `ccat match` first"
        )
    enriched = frame.join(
        family_frame.select(FAMILY_JOIN_COLUMNS),
        on="family_id",
        how="left",
    )
    sponsor_frame = _eligible_frame("sponsor", sponsor_rows)
    if sponsor_frame.is_empty():
        raise InfoSheetError(
            "final_sponsor sheet is missing or empty; run `ccat match` first"
        )
    missing_sponsor_columns = [
        source
        for source, _alias in SPONSOR_JOIN_COLUMNS
        if source not in sponsor_frame.columns
    ]
    if missing_sponsor_columns:
        raise InfoSheetError(
            f"final_sponsor lacks column(s): {missing_sponsor_columns};"
            " run `ccat match` first"
        )
    duplicated_sponsors = (
        sponsor_frame.group_by("sponsor_id").len().filter(pl.col("len") > 1)
    )
    if not duplicated_sponsors.is_empty():
        raise InfoSheetError(
            "final_sponsor sponsor_id is not unique:"
            f" {duplicated_sponsors['sponsor_id'].to_list()}"
        )
    enriched = enriched.with_columns(
        pl.col("assigned_sponsor_id").cast(pl.Int64)
    )
    sponsor_frame = sponsor_frame.with_columns(
        pl.col("sponsor_id").cast(pl.Int64)
    )
    enriched = enriched.join(
        sponsor_frame.select(
            [
                pl.col(source).alias(alias)
                for source, alias in SPONSOR_JOIN_COLUMNS
            ]
            + [pl.col("sponsor_id")]
        ),
        left_on="assigned_sponsor_id",
        right_on="sponsor_id",
        how="left",
    )
    organization = (
        pl.col("sponsor_organization_name").fill_null("").str.strip_chars()
    )
    person = pl.concat_str(
        [
            pl.col("sponsor_first_name").fill_null(""),
            pl.lit(" "),
            pl.col("sponsor_last_name").fill_null(""),
        ]
    ).str.strip_chars()
    enriched = enriched.with_columns(
        pl.when(organization != "")
        .then(organization)
        .otherwise(person)
        .alias("sponsor_name")
    )
    family_size = (
        enriched.group_by("family_id").len().rename({"len": "family_size"})
    )
    enriched = enriched.join(family_size, on="family_id", how="left")
    return enriched


def _convert_to_pdf(
    odt_path: Path,
    out_dir: Path,
    soffice_binary: str,
    profile: Path,
    timeout: float,
    stop: threading.Event | None = None,
) -> Path:
    """Convert one ODT to PDF with headless LibreOffice (§13, §9.5).

    Port of the legacy converter with a configurable deadline (the new
    ``info_sheet.conversion_timeout_seconds`` field; the legacy module
    keeps its identical built-in constant).  The subprocess is polled
    so a set ``stop`` event terminates the conversion process group
    within half a second (§6.2).

    Args:
        odt_path: The rendered ODT file; the PDF lands in ``out_dir``
            under the same base name.
        out_dir: Directory receiving the PDF.
        soffice_binary: LibreOffice binary path.
        profile: Directory for the isolated LibreOffice user profile.
        timeout: Hard ceiling for the conversion, in seconds.
        stop: When set, the conversion subprocess is terminated and
            :class:`InfoSheetCancelledError` is raised.

    Returns:
        The created PDF path.

    Raises:
        InfoSheetCancelledError: When ``stop`` is set mid-conversion.
        InfoSheetError: If the conversion fails, times out, or
            produces no PDF.
    """
    import subprocess

    try:
        process = subprocess.Popen(
            [
                soffice_binary,
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(out_dir),
                f"-env:UserInstallation={profile.as_uri()}",
                str(odt_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as error:
        raise InfoSheetError(
            f"could not start {soffice_binary} for {odt_path.name}: {error}"
        ) from error
    deadline = time.monotonic() + timeout
    while True:
        try:
            returncode = process.wait(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if stop is not None and stop.is_set():
                _terminate(process)
                raise InfoSheetCancelledError(
                    f"cancelled while converting {odt_path.name}"
                ) from None
            if time.monotonic() >= deadline:
                _terminate(process)
                raise InfoSheetError(
                    f"soffice conversion timed out after {timeout}s for"
                    f" {odt_path.name}"
                ) from None
    stdout, stderr = process.communicate()
    pdf_path = out_dir / f"{odt_path.stem}.pdf"
    if returncode != 0:
        detail = (stderr or stdout or "").strip()
        raise InfoSheetError(
            f"soffice conversion failed for {odt_path.name} (exit"
            f" {returncode}): {detail[-400:]}"
        )
    if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
        raise InfoSheetError(
            f"soffice produced no PDF for {odt_path.name}:"
            f" {(stderr or stdout or '').strip()[-400:]}"
        )
    return pdf_path


def _pdf_valid(path: Path) -> bool:
    """Validate one converted PDF by reopening it (plan §6.3).

    Args:
        path: The PDF file to check.

    Returns:
        True when pypdf can open the file and it has at least one page.
    """
    try:
        return len(PdfReader(str(path)).pages) >= 1
    except Exception:  # noqa: BLE001 - any pypdf failure means invalid
        return False


def _sheet_path(
    root: Path, cfg: "Config", angel_id: str, location: str
) -> Path:
    """Artifact-root path of one angel's generated sheet (§9.5).

    Args:
        root: The artifact root.
        cfg: Pipeline configuration (season).
        angel_id: The child's angel id.
        location: The canonical pickup-location bucket.

    Returns:
        ``<root>/child_sheets/<location>/AT<season>_<angel_id>.pdf``.
    """
    return root / "child_sheets" / location / f"AT{cfg.season}_{angel_id}.pdf"


def _outcome(
    row: dict[str, Any],
    template_file: Path,
    cfg: "Config",
    spec: Any,
    root: Path,
    workspace: Path,
    stop: threading.Event,
) -> tuple[str, dict[str, Any], str, Path | None, Exception | None]:
    """Render, convert, validate, and atomically place one sheet (D20).

    Worker-thread body.  Never raises: the outcome tuple carries the
    status so the event loop can commit per-item markers and keep
    draining the pool after cancellation (§6.2).

    Args:
        row: One enriched pending row.
        template_file: The py3o ``.ott`` template.
        cfg: Pipeline configuration.
        spec: The ``info_sheet`` config section.
        root: The artifact root.
        workspace: This worker's scratch directory (with its
            ``lo_profile``).
        stop: The cooperative stop event.

    Returns:
        ``(outcome, row, stem, placed_path, error)`` where outcome is
        ``"ok"``/``"cancelled"``/``"failed"``.
    """
    angel_id = str(row["angel_id"])
    stem = _sheet_stem(cfg, angel_id)
    try:
        if stop.is_set():
            raise InfoSheetCancelledError()
        location = (
            canonical_location(
                row.get("gift_pickup_location"), cfg.location_groups
            )
            or "unknown"
        )
        odt_path = workspace / f"{stem}.odt"
        _render_one(
            template_file,
            _nested_data(
                row,
                spec.mappings,
                "_" * spec.contact_placeholder_length,
                frozenset(spec.contact_placeholder_affiliations),
            ),
            odt_path,
        )
        if stop.is_set():
            raise InfoSheetCancelledError()
        pdf_path = _convert_to_pdf(
            odt_path,
            workspace,
            spec.soffice_binary,
            workspace / "lo_profile",
            spec.conversion_timeout_seconds,
            stop,
        )
        if not _pdf_valid(pdf_path):
            raise InfoSheetError(
                f"converted PDF failed validation for {stem} (§6.3:"
                " reopened output must contain at least one page)"
            )
        final_path = _sheet_path(root, cfg, angel_id, location)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(pdf_path, final_path)
        return "ok", row, stem, final_path, None
    except InfoSheetCancelledError as error:
        return "cancelled", row, stem, None, error
    except Exception as error:  # noqa: BLE001 - per-item partial success
        return "failed", row, stem, None, error


def _validate_sheets_exist(
    root: Path,
    cfg: "Config",
    child_frame: pl.DataFrame,
    family_frame: pl.DataFrame,
    sponsor_id: int | None = None,
) -> None:
    """Require a sheet PDF for every sponsored eligible angel (D18).

    Exact legacy :func:`ccat.sponsor_packets.validate_all_sheets_exist`
    scope: every output-eligible child of every output-eligible family
    with an assigned sponsor must have its sheet file — regardless of
    audit status, generation markers, or the sponsor's own stored
    ``assigned_angel_ids``.  This intentionally differs from the
    generation gate (documented divergence, plan §9.5): mixed-status
    data can finish sheet generation and still fail packet validation.

    Args:
        root: The artifact root.
        cfg: Pipeline configuration.
        child_frame: Eligible child frame.
        family_frame: Eligible family frame.
        sponsor_id: Restrict the check to one sponsor.

    Raises:
        SponsorPacketError: When any required sheet file is missing.
    """
    if child_frame.is_empty():
        return
    family_rows = family_frame.to_dicts()
    sponsored_family_ids = {
        row.get("family_id")
        for row in family_rows
        if row.get("assigned_sponsor_id") is not None
        and (sponsor_id is None or row.get("assigned_sponsor_id") == sponsor_id)
    }
    location_by_family = {
        row.get("family_id"): row.get("gift_pickup_location")
        for row in family_rows
    }
    missing = [
        str(row["angel_id"])
        for row in child_frame.to_dicts()
        if row.get("family_id") in sponsored_family_ids
        and row.get("angel_id")
        and not _sheet_path(
            root,
            cfg,
            str(row["angel_id"]),
            canonical_location(
                location_by_family.get(row.get("family_id")),
                cfg.location_groups,
            )
            or "unknown",
        ).is_file()
    ]
    if missing:
        raise SponsorPacketError(
            "cannot build sponsor packets: child information sheet(s)"
            f" missing for angel(s) {sorted(missing)}; run `ccat generate`"
            " first"
        )


def _sponsors_with_angels(
    cfg: "Config",
    sponsor_frame: pl.DataFrame,
    child_frame: pl.DataFrame,
    family_frame: pl.DataFrame,
    sponsor_id: int | None = None,
) -> "list[dict[str, Any]]":
    """Join eligible assigned sponsors with eligible angels (§13b).

    Port of the legacy ``_sponsors_with_angels`` over repository
    frames: every store is filtered independently, so ignoring one
    entity never cascades into the others (an ignored family's child
    keeps its blank drop-off).  Unknown angel ids in a sponsor's
    stored list are dropped with warnings.

    Args:
        cfg: Pipeline configuration.
        sponsor_frame: Eligible sponsor frame.
        child_frame: Eligible child frame.
        family_frame: Eligible family frame.
        sponsor_id: Restrict to one sponsor.

    Returns:
        One packet payload per sponsor with assigned angels, angels
        ordered by family id then angel letter.

    Raises:
        SponsorPacketError: When a required store is empty or the
            scoped sponsor has no eligible row/assigned angels.
    """
    if sponsor_frame.is_empty():
        raise SponsorPacketError(
            "final_sponsor sheet is missing or empty; run `ccat match` first"
        )
    if sponsor_id is not None:
        sponsor_frame = sponsor_frame.filter(pl.col("sponsor_id") == sponsor_id)
        if sponsor_frame.is_empty():
            raise SponsorPacketError(
                f"sponsor {sponsor_id} has no eligible row in"
                " final_sponsor; run `ccat match` first or restore the"
                " ignored sponsor"
            )
    if child_frame.is_empty():
        raise SponsorPacketError(
            "final_child sheet is missing or empty; run `ccat audit` first"
        )
    location_by_family: dict[Any, Any] = {}
    if family_frame.is_empty():
        logger.warning(
            "final_family sheet is missing or empty; angel drop-off"
            " locations render blank (run `ccat match` first)"
        )
    else:
        location_by_family = {
            row.get("family_id"): row.get("gift_pickup_location")
            for row in family_frame.to_dicts()
        }
    angels_by_id = {
        row["angel_id"]: row
        for row in child_frame.to_dicts()
        if row.get("angel_id")
    }
    packets: list[dict[str, Any]] = []
    for row in sponsor_frame.to_dicts():
        raw_ids = row.get("assigned_angel_ids") or ""
        angel_ids = [
            angel_id.strip()
            for angel_id in str(raw_ids).split(",")
            if angel_id.strip()
        ]
        if not angel_ids:
            continue
        unknown = [
            angel_id for angel_id in angel_ids if angel_id not in angels_by_id
        ]
        if unknown:
            logger.warning(
                "sponsor %s assigned unknown angel id(s) %s; skipping them",
                row.get("sponsor_id"),
                unknown,
            )
        angels = [
            {
                "angel_id": angel_id,
                "family_id": angels_by_id[angel_id].get("family_id"),
                "gender": angels_by_id[angel_id].get("gender"),
                "age": angels_by_id[angel_id].get("age_at_xmas"),
                "dropoff": location_by_family.get(
                    angels_by_id[angel_id].get("family_id")
                ),
                "location": canonical_location(
                    location_by_family.get(
                        angels_by_id[angel_id].get("family_id")
                    ),
                    cfg.location_groups,
                )
                or "unknown",
            }
            for angel_id in angel_ids
            if angel_id in angels_by_id
        ]
        angels.sort(key=_angel_sort_key)
        packets.append(
            {
                "sponsor_id": row.get("sponsor_id"),
                "first_name": row.get("first_name") or "",
                "email": row.get("email"),
                "dropoff_location": row.get("dropoff_location"),
                "angels": angels,
            }
        )
    if not packets:
        if sponsor_id is not None:
            raise SponsorPacketError(
                f"sponsor {sponsor_id} has no assigned angels;"
                " run `ccat match` first"
            )
        logger.info("no sponsor has assigned angels; no packets to build")
    return packets


def _merge_sheets_pdf(
    root: Path,
    cfg: "Config",
    packet_dir: Path,
    sponsor_id: Any,
    angels: "list[dict[str, Any]]",
) -> tuple[Path, "list[str]"]:
    """Combine the angel sheets into one packet PDF (§13b).

    Args:
        root: The artifact root (sheet lookup).
        cfg: Pipeline configuration.
        packet_dir: Destination directory (created by the caller).
        sponsor_id: The sponsor's ID (names the PDF).
        angels: The sponsor's ordered angel rows.

    Returns:
        A tuple of (the written PDF path, angel ids whose sheet was
        missing and therefore contributed no pages).
    """
    writer = PdfWriter()
    missing: list[str] = []
    for angel in angels:
        sheet = _sheet_path(
            root, cfg, str(angel["angel_id"]), str(angel["location"])
        )
        if sheet.is_file():
            writer.append(str(sheet), outline_item=f"Angel {angel['angel_id']}")
        else:
            missing.append(str(angel["angel_id"]))
            logger.warning(
                "child information sheet not found for angel %s (%s);"
                " the combined PDF skips its pages",
                angel["angel_id"],
                sheet,
            )
    pdf_path = packet_dir / (
        f"Sponsor {sponsor_id} - Child Information {cfg.season}.pdf"
    )
    with pdf_path.open("wb") as output:
        writer.write(output)
    return pdf_path, missing


def _build_packets(
    root: Path,
    cfg: "Config",
    packets: "list[dict[str, Any]]",
    overwrite: bool,
) -> "tuple[list[tuple[Path, str]], int]":
    """Write each sponsor's packet files under the artifact root (§13b).

    Skip predicate is the legacy one (HTML + combined PDF exist; the
    instructions copy is unchecked).  ``overwrite`` preserves manual
    email edits by copying the existing HTML to ``<stem>.manual.html``
    first (D20 feasible retention) and registering it as a
    ``sponsor_packet_manual`` artifact.

    Args:
        root: The artifact root.
        cfg: Pipeline configuration.
        packets: Packet payloads from :func:`_sponsors_with_angels`.
        overwrite: Regenerate packet files that already exist.

    Returns:
        A tuple of (files to register as ``(path, kind)``, count of
        sponsors skipped because their packet already existed).
    """
    spec = cfg.sponsor_packets
    template_file = Path(spec.email_template_html_file)
    if not template_file.is_file():
        raise SponsorPacketError(
            f"email template file not found: {template_file}"
        )
    environment = Environment(autoescape=True)
    template = environment.from_string(
        template_file.read_text(encoding="utf-8")
    )
    instructions = Path(spec.instructions_file)
    if not instructions.is_file():
        logger.warning(
            "sponsor instruction file not found: %s; packets are"
            " written without the instruction copy",
            instructions,
        )
    files: list[tuple[Path, str]] = []
    skipped = 0
    for packet in packets:
        packet_dir = root / "sponsor_packets" / str(packet["sponsor_id"])
        html_path = packet_dir / (
            f"{spec.filename_prefix}_{packet['sponsor_id']}.html"
        )
        sheets_pdf_name = (
            f"Sponsor {packet['sponsor_id']} - Child Information"
            f" {cfg.season}.pdf"
        )
        sheets_pdf_path = packet_dir / sheets_pdf_name
        if not overwrite and html_path.exists() and sheets_pdf_path.exists():
            logger.info(
                "packet for sponsor %s exists; skipping",
                packet["sponsor_id"],
            )
            skipped += 1
            continue
        packet_dir.mkdir(parents=True, exist_ok=True)
        if overwrite and html_path.exists():
            manual = packet_dir / f"{html_path.stem}.manual.html"
            shutil.copy2(html_path, manual)
            files.append((manual, "sponsor_packet_manual"))
        subject = environment.from_string(_SUBJECT_TEMPLATE).render(
            season=cfg.season,
            angel_count=len(packet["angels"]),
        )
        html_path.write_text(
            template.render(
                season=cfg.season,
                sponsor=packet,
                angels=packet["angels"],
                angel_count=len(packet["angels"]),
                subject=subject,
                instructions_filename=instructions.name,
                child_information_filename=sheets_pdf_name,
                bcc_emails=spec.bcc_emails,
            ),
            encoding="utf-8",
        )
        files.append((html_path, "sponsor_packet"))
        logger.info(
            "wrote email draft %s for sponsor %s (%d angel(s))",
            html_path,
            packet["sponsor_id"],
            len(packet["angels"]),
        )
        if instructions.is_file():
            copy = packet_dir / instructions.name
            shutil.copy2(instructions, copy)
            files.append((copy, "sponsor_instructions"))
            logger.info("copied sponsor instructions into %s", packet_dir.name)
        pdf_path, missing = _merge_sheets_pdf(
            root, cfg, packet_dir, packet["sponsor_id"], packet["angels"]
        )
        files.append((pdf_path, "sponsor_packet"))
        if missing:
            logger.warning(
                "child information PDF %s written without sheet(s) for %s",
                pdf_path,
                missing,
            )
    return files, skipped


async def generate(
    cfg: "Config",
    actor: str,
    options: "dict[str, Any]",
    progress: "Callable[..., None]",
    cancel: "Callable[[], None]",
) -> GenerateResult:
    """Generate child info sheets, then sponsor packets (§9.5).

    Sheet eligibility: output-eligible children, audited, assigned
    sponsor, and pending generation (all sponsored rows with
    ``overwrite``), optionally scoped to one sponsor — the exact
    legacy ``_pending_rows`` order.  Each success is committed
    per-item (D20); failures are collected in the result and the job
    continues (partial success, §6.2).  ``test`` renders the first
    pending row only and skips packets.  ``max_workers`` must be
    between 1 and :data:`MAX_WORKERS_CEILING` (plan §9.5).

    Args:
        cfg: Pipeline configuration.
        actor: Requesting user/job identity.
        options: ``test`` / ``overwrite`` / ``sponsor`` /
            ``max_workers``.
        progress: Progress reporter ``(phase, completed, failed,
            total)``.
        cancel: Cooperative cancel check; raises
            :class:`JobCancelRequested` when cancellation was
            requested.  In-flight LibreOffice conversions are
            terminated as a process group; completed items keep their
            artifacts and markers.

    Returns:
        The typed generation result.

    Raises:
        InfoSheetError: On invalid options, a missing template, or an
            unusable store.
        SponsorPacketError: From the packet phase (validation,
            template, or scoped-sponsor errors).
        JobCancelRequested: When cancellation was requested.
    """
    test = bool(options.get("test"))
    overwrite = bool(options.get("overwrite"))
    sponsor_raw = options.get("sponsor")
    sponsor = int(sponsor_raw) if sponsor_raw is not None else None
    spec = cfg.info_sheet
    workers = (
        spec.max_workers
        if options.get("max_workers") is None
        else int(options["max_workers"])
    )
    if workers < 1:
        raise InfoSheetError(
            f"max_workers must be positive, got {workers} (plan §9.5)"
        )
    if workers > MAX_WORKERS_CEILING:
        raise InfoSheetError(
            f"max_workers {workers} exceeds the ceiling of"
            f" {MAX_WORKERS_CEILING} (plan §9.5)"
        )
    template_file = Path(spec.template_file)
    if not template_file.is_file():
        raise InfoSheetError(f"template file not found: {template_file}")

    async with _store(cfg, actor) as repo:
        child_rows = await repo.final_rows("child")
        family_rows = await repo.final_rows("family")
        sponsor_rows = await repo.final_rows("sponsor")
        progress("prepare")
        enriched = await asyncio.to_thread(
            _enrich, child_rows, family_rows, sponsor_rows, cfg
        )
        pending, _skipped_unsponsored = _pending_rows(
            enriched, overwrite=overwrite
        )
        if sponsor is not None:
            pending = pending.filter(pl.col("assigned_sponsor_id") == sponsor)
            logger.info(
                "--sponsor %s: %d pending row(s) belong to it",
                sponsor,
                pending.height,
            )
        if not overwrite:
            already = enriched.filter(
                pl.col("generated_info_sheet")
                & pl.col("assigned_sponsor_id").is_not_null()
            )
            if sponsor is not None:
                already = already.filter(
                    pl.col("assigned_sponsor_id") == sponsor
                )
            skipped_stems = [
                _sheet_stem(cfg, str(angel_id))
                for angel_id in already["angel_id"].to_list()
            ]
        else:
            skipped_stems = []

        generated: list[str] = []
        failed: list[str] = []
        if pending.is_empty():
            logger.info("no children pending an info sheet")
        else:
            rows = pending.to_dicts()
            if test:
                rows = rows[:1]
                logger.info("test mode: generating one info sheet only")
            total = len(rows)
            progress("sheets", total=total)
            root = artifact_root(cfg)
            sheets_dir = root / "child_sheets"
            sheets_dir.mkdir(parents=True, exist_ok=True)
            # Scratch lives under the artifact root so the final atomic
            # rename never crosses a filesystem (plan §6.3).
            scratch = Path(tempfile.mkdtemp(prefix="scratch_", dir=sheets_dir))
            stop = threading.Event()
            workspaces: dict[int, Path] = {}
            workspace_lock = threading.Lock()

            def _workspace() -> Path:
                """One ODT scratch dir + LO profile per worker thread."""
                ident = threading.get_ident()
                with workspace_lock:
                    path = workspaces.get(ident)
                    if path is None:
                        path = scratch / f"worker{len(workspaces)}"
                        (path / "lo_profile").mkdir(parents=True)
                        workspaces[ident] = path
                    return path

            def run_one(row: dict[str, Any]) -> Any:
                """Bind one row to its per-thread workspace."""
                workspace = _workspace()
                return _outcome(
                    row, template_file, cfg, spec, root, workspace, stop
                )

            loop = asyncio.get_running_loop()
            pool = ThreadPoolExecutor(max_workers=min(workers, len(rows)))
            by_id = {str(row.get("zite_record_id")): row for row in child_rows}
            completed = 0
            cancel_error: JobCancelRequested | None = None
            try:
                futures = [
                    loop.run_in_executor(pool, run_one, row) for row in rows
                ]
                for coro in asyncio.as_completed(futures):
                    outcome, row, stem, placed, error = await coro
                    if outcome == "cancelled":
                        continue
                    if outcome == "failed":
                        failed.append(stem)
                        logger.error(
                            "info sheet generation failed for %s: %s",
                            stem,
                            error,
                        )
                        continue
                    record_id = str(row["zite_record_id"])
                    await register_file(
                        repo,
                        "child_sheet",
                        placed,
                        actor,
                        meta={
                            "zite_record_id": record_id,
                            "angel_id": row["angel_id"],
                            "sponsor_id": row.get("assigned_sponsor_id"),
                        },
                        root=root,
                    )
                    # Per-item marker commit (D20): the marker persists
                    # only after the validated file is in place.
                    target = by_id[record_id]
                    target["generated_info_sheet"] = True
                    await repo.commit_finals(
                        "generate", "child", child_rows, actor
                    )
                    child_rows = await repo.final_rows("child")
                    by_id = {
                        str(row.get("zite_record_id")): row
                        for row in child_rows
                    }
                    generated.append(stem)
                    completed += 1
                    progress("sheets", completed=completed, total=total)
                    try:
                        cancel()
                    except JobCancelRequested as requested:
                        # Stop the workers (LibreOffice process groups
                        # are terminated within the poll interval) and
                        # keep draining; completed items stay committed
                        # (§6.2).
                        cancel_error = requested
                        stop.set()
            finally:
                pool.shutdown(wait=True)
                shutil.rmtree(scratch, ignore_errors=True)
            if cancel_error is not None:
                raise cancel_error

        packets_built = 0
        packets_skipped = 0
        if test:
            logger.info("test mode: skipping sponsor packet generation")
        else:
            progress("packets")
            root = artifact_root(cfg)
            child_frame = _eligible_frame("child", child_rows)
            family_frame = _eligible_frame("family", family_rows)
            sponsor_frame = _eligible_frame("sponsor", sponsor_rows)
            # D18: exact legacy validation scope, run before building.
            _validate_sheets_exist(
                root, cfg, child_frame, family_frame, sponsor_id=sponsor
            )
            packets = _sponsors_with_angels(
                cfg,
                sponsor_frame,
                child_frame,
                family_frame,
                sponsor_id=sponsor,
            )
            files, packets_skipped = await asyncio.to_thread(
                _build_packets, root, cfg, packets, overwrite
            )
            packets_built = len(packets) - packets_skipped
            cancel()
            for path, kind in files:
                await register_file(repo, kind, path, actor, meta={}, root=root)
            progress("packets", completed=packets_built, total=len(packets))

    logger.info(
        "generated %d info sheet(s); %d failed", len(generated), len(failed)
    )
    return GenerateResult(
        sheets_generated=generated,
        sheets_failed=failed,
        sheets_skipped=skipped_stems,
        packets_built=packets_built,
        packets_skipped=packets_skipped,
        info_sheet_dir=str(artifact_root(cfg) / "child_sheets"),
        packets_dir=str(artifact_root(cfg) / "sponsor_packets"),
    )


__all__ = ["MAX_WORKERS_CEILING", "generate"]
