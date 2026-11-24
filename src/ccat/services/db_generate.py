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
  legacy eligibility without cascading filters, sheet validation for
  audited assigned children only (decision D18), legacy skip predicate
  (HTML + combined PDF), and
  ``--overwrite`` preserving manual email edits by copying the existing
  HTML to ``<stem>.manual.html`` first (D20 feasible retention).

Pure/legacy helpers are reused verbatim: ``_pending_rows``,
``_nested_data``, ``_render_one``, ``_sheet_stem``, packet ordering and
templates.  Prompts/files never leave the configured artifact root.
"""

import asyncio
import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

import polars as pl
from jinja2 import Environment
from pypdf import PdfReader, PdfWriter

from ccat.audit.store import (
    STATUS_AUDITED,
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
from ccat.services.db_assignments import child_sponsor
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
    sponsor ids), identical orphan handling, and ``family_size``
    computed over this enriched eligible population.  Unmatched family
    ids (ignored families under manual matching) warn and keep a blank
    drop-off instead of failing.

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
    # Manual matching keeps ignored families out of the eligible family
    # set; their children stay in final_child and must still flow
    # through, rendering a blank drop-off location (see
    # _sponsors_with_angels).  A genuinely missing family row degrades
    # the same way, so warn instead of failing (§13b divergence note).
    unmatched = sorted(
        set(frame["family_id"]) - set(family_frame["family_id"].drop_nulls())
    )
    if unmatched:
        logger.warning(
            "final_family has no eligible row for family_id(s): %s;"
            " their children keep a blank drop-off location",
            unmatched,
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
        enriched.select(["family_id", "angel_id"])
        .unique()
        .group_by("family_id")
        .len()
        .rename({"len": "family_size"})
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


def _sponsors_missing_sheets(
    root: Path,
    cfg: "Config",
    child_frame: pl.DataFrame,
    family_frame: pl.DataFrame,
    sponsor_id: int | None = None,
) -> "list[dict[str, Any]]":
    """Find sponsors missing sheets for audited, assigned children.

    Sponsors missing an audited child's sheet are skipped instead of
    failing the whole job. Unaudited assigned children are expected to
    have no sheet and do not block their sponsor.

    Args:
        root: The artifact root.
        cfg: Pipeline configuration.
        child_frame: Eligible child frame.
        family_frame: Eligible family frame.
        sponsor_id: Restrict to one sponsor.

    Returns:
        One entry per blocked sponsor: its id and the missing angel ids.
    """
    if child_frame.is_empty():
        return []
    location_by_family: dict[Any, Any] = {
        row.get("family_id"): row.get("gift_pickup_location")
        for row in family_frame.to_dicts()
    }
    missing_by_sponsor: dict[int, list[str]] = {}
    for row in child_frame.to_dicts():
        owner = child_sponsor(row)
        if owner is None or not row.get("angel_id"):
            continue
        if sponsor_id is not None and owner != sponsor_id:
            continue
        if row.get("status") != STATUS_AUDITED:
            logger.warning(
                "child %s for sponsor %s is not audited (status: %s);"
                " no info sheet, the packet skips its pages",
                row.get("angel_id"),
                owner,
                row.get("status"),
            )
            continue
        sheet = _sheet_path(
            root,
            cfg,
            str(row["angel_id"]),
            canonical_location(
                location_by_family.get(row.get("family_id")),
                cfg.location_groups,
            )
            or "unknown",
        )
        if not sheet.is_file():
            missing_by_sponsor.setdefault(int(owner), []).append(
                str(row["angel_id"])
            )
    return [
        {"sponsor_id": owner, "angel_ids": sorted(angel_ids)}
        for owner, angel_ids in sorted(missing_by_sponsor.items())
    ]


def _sponsors_with_angels(
    cfg: "Config",
    sponsor_frame: pl.DataFrame,
    child_frame: pl.DataFrame,
    family_frame: pl.DataFrame,
    sponsor_id: int | None = None,
) -> "list[dict[str, Any]]":
    """Join eligible sponsors with their children's details (§13b).

    Port of the legacy ``_sponsors_with_angels`` over repository
    frames: membership is the child's own ``assigned_sponsor_id``, so a
    shared angel letter cannot drop a child.  Every store is filtered
    independently, so ignoring one entity never cascades into the others
    (an ignored family's child keeps its blank drop-off).

    Args:
        cfg: Pipeline configuration.
        sponsor_frame: Eligible sponsor frame.
        child_frame: Eligible child frame.
        family_frame: Eligible family frame.
        sponsor_id: Restrict to one sponsor.

    Returns:
        One packet payload per sponsor with assigned children, angels
        ordered by family id then angel letter.

    Raises:
        SponsorPacketError: When a required store is empty or the
            scoped sponsor has no eligible row/assigned children.
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
    children_by_sponsor: dict[int, list[dict[str, Any]]] = {}
    for child in child_frame.to_dicts():
        owner = child_sponsor(child)
        if owner is None or not child.get("angel_id"):
            continue
        children_by_sponsor.setdefault(owner, []).append(child)
    packets: list[dict[str, Any]] = []
    for row in sponsor_frame.to_dicts():
        owner = row.get("sponsor_id")
        sponsor_children = (
            children_by_sponsor.get(int(owner)) if owner is not None else None
        )
        if not sponsor_children:
            continue
        angels = [
            {
                "angel_id": child["angel_id"],
                "child_id": child.get("zite_record_id"),
                "family_id": child.get("family_id"),
                "gender": child.get("gender"),
                "age": child.get("age_at_xmas"),
                "dropoff": location_by_family.get(child.get("family_id")),
                "location": canonical_location(
                    location_by_family.get(child.get("family_id")),
                    cfg.location_groups,
                )
                or "unknown",
            }
            for child in sponsor_children
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
    temporary = pdf_path.with_name(pdf_path.name + ".tmp")
    with temporary.open("wb") as output:
        writer.write(output)
    os.replace(temporary, pdf_path)
    return pdf_path, missing


def _sha256_file(path: Path) -> str:
    """Hash one file's bytes.

    Args:
        path: The file to hash.

    Returns:
        The hex sha256 digest.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_atomic(path: Path, payload: bytes) -> None:
    """Write a file through a temporary name, then rename it into place.

    A reader never sees a half-written file, and a crash leaves the
    previous file intact.

    Args:
        path: The destination.
        payload: The bytes to write.
    """
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _copy_atomic(source: Path, destination: Path) -> None:
    """Copy a file through a temporary name, then rename it into place.

    Args:
        source: The file to copy.
        destination: The destination path.
    """
    temporary = destination.with_name(destination.name + ".tmp")
    shutil.copy2(source, temporary)
    os.replace(temporary, destination)


def _require_writable_packet_dir(directory: Path) -> None:
    """Make sure the packet directory can be written, or say why not.

    A packet directory left behind by a container that ran as another
    user cannot be written, and the failure would otherwise surface as a
    bare ``PermissionError`` deep in the build.

    Args:
        directory: The packet directory.

    Raises:
        SponsorPacketError: When the directory cannot be written.
    """
    probe = directory / f".write-probe-{os.getpid()}"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe.touch()
        probe.unlink()
    except OSError as error:
        raise SponsorPacketError(
            f"the packet directory {directory} is not writable by this"
            " process; the artifacts volume must be owned by the runtime"
            " user"
        ) from error


def _packet_manifest(
    cfg: "Config",
    packet: "dict[str, Any]",
    subject: str,
    items: "list[dict[str, Any]]",
) -> dict[str, Any]:
    """Describe one packet build.

    The manifest is what makes the email and its attachments agree: it
    names the children the packet was built from, the files it contains
    with their hashes, and a fingerprint over both.  The dispatch page
    renders the draft from it and refuses to send when the current
    assignment or the files no longer match.

    Args:
        cfg: Pipeline configuration.
        packet: The packet payload.
        subject: The subject the email carries.
        items: The packet's files with their roles and hashes.

    Returns:
        The manifest payload.
    """
    children = [
        {"child_id": angel.get("child_id"), "angel_id": angel.get("angel_id")}
        for angel in packet["angels"]
    ]
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "sponsor_id": packet["sponsor_id"],
                "children": children,
                "items": {item["role"]: item["sha256"] for item in items},
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "build_id": uuid.uuid4().hex,
        "sponsor_id": packet["sponsor_id"],
        "season": cfg.season,
        "subject": subject,
        "sponsor": {
            "first_name": packet.get("first_name") or "",
            "email": packet.get("email"),
        },
        "children": children,
        "angels": [
            {
                "angel_id": angel.get("angel_id"),
                "child_id": angel.get("child_id"),
                "gender": angel.get("gender"),
                "age": angel.get("age"),
                "dropoff": angel.get("dropoff"),
            }
            for angel in packet["angels"]
        ],
        "items": items,
        "fingerprint": fingerprint,
    }


def _build_packet_batch(
    root: Path,
    cfg: "Config",
    packets: "list[dict[str, Any]]",
    overwrite: bool,
) -> "tuple[list[tuple[Path, str, dict[str, Any]]], int]":
    """Write each sponsor's packet files under the artifact root (§13b).

    Every packet gets a ``manifest.json`` naming its children and its
    files with their hashes, so the email draft and the attached PDFs
    always describe the same children.  The skip predicate requires that
    manifest: a packet built without one is rebuilt.

    ``overwrite`` preserves manual email edits by copying the existing
    HTML to ``<stem>.manual.html`` first (D20 feasible retention) and
    registering it as a ``sponsor_packet_manual`` artifact.

    Args:
        root: The artifact root.
        cfg: Pipeline configuration.
        packets: Packet payloads from :func:`_sponsors_with_angels`.
        overwrite: Regenerate packet files that already exist.

    Returns:
        A tuple of (files to register as ``(path, kind, meta)``, count of
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
    files: list[tuple[Path, str, dict[str, Any]]] = []
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
        manifest_path = packet_dir / "manifest.json"
        complete = (
            html_path.exists()
            and sheets_pdf_path.exists()
            and manifest_path.exists()
        )
        if not overwrite and complete:
            logger.info(
                "packet for sponsor %s exists; skipping",
                packet["sponsor_id"],
            )
            skipped += 1
            continue
        _require_writable_packet_dir(packet_dir)
        if html_path.exists():
            manual = packet_dir / f"{html_path.stem}.manual.html"
            _write_atomic(manual, html_path.read_bytes())
            files.append(
                (
                    manual,
                    "sponsor_packet_manual",
                    {
                        "sponsor_id": packet["sponsor_id"],
                        "role": "email_html_manual",
                    },
                )
            )
        subject = environment.from_string(_SUBJECT_TEMPLATE).render(
            season=cfg.season,
            angel_count=len(packet["angels"]),
        )
        _write_atomic(
            html_path,
            template.render(
                season=cfg.season,
                sponsor=packet,
                angels=packet["angels"],
                angel_count=len(packet["angels"]),
                subject=subject,
                instructions_filename=instructions.name,
                child_information_filename=sheets_pdf_name,
                bcc_emails=spec.bcc_emails,
                manual=True,
                note="",
            ).encode("utf-8"),
        )
        logger.info(
            "wrote email draft %s for sponsor %s (%d angel(s))",
            html_path,
            packet["sponsor_id"],
            len(packet["angels"]),
        )
        items: list[dict[str, Any]] = [
            {
                "role": "email_html",
                "filename": html_path.name,
                "sha256": _sha256_file(html_path),
                "size_bytes": html_path.stat().st_size,
            }
        ]
        if instructions.is_file():
            copy = packet_dir / instructions.name
            _copy_atomic(instructions, copy)
            items.append(
                {
                    "role": "instructions_pdf",
                    "filename": copy.name,
                    "sha256": _sha256_file(copy),
                    "size_bytes": copy.stat().st_size,
                }
            )
            logger.info("copied sponsor instructions into %s", packet_dir.name)
        pdf_path, missing = _merge_sheets_pdf(
            root, cfg, packet_dir, packet["sponsor_id"], packet["angels"]
        )
        items.append(
            {
                "role": "packet_pdf",
                "filename": pdf_path.name,
                "sha256": _sha256_file(pdf_path),
                "size_bytes": pdf_path.stat().st_size,
                "child_ids": [
                    angel.get("child_id") for angel in packet["angels"]
                ],
                "angel_ids": [
                    angel.get("angel_id") for angel in packet["angels"]
                ],
            }
        )
        if missing:
            logger.warning(
                "child information PDF %s written without sheet(s) for %s",
                pdf_path,
                missing,
            )
        manifest = _packet_manifest(cfg, packet, subject, items)
        _write_atomic(
            manifest_path,
            json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
        )
        build_id = str(manifest["build_id"])
        for item in items:
            path = packet_dir / str(item["filename"])
            files.append(
                (
                    path,
                    "sponsor_packet"
                    if item["role"] != "instructions_pdf"
                    else "sponsor_instructions",
                    {
                        "sponsor_id": packet["sponsor_id"],
                        "role": item["role"],
                        "build_id": build_id,
                        "sha256": item["sha256"],
                    },
                )
            )
        files.append(
            (
                manifest_path,
                "sponsor_packet",
                {
                    "sponsor_id": packet["sponsor_id"],
                    "role": "manifest",
                    "build_id": build_id,
                    "sha256": _sha256_file(manifest_path),
                },
            )
        )
    return files, skipped


def _build_packets(
    root: Path,
    cfg: "Config",
    packets: "list[dict[str, Any]]",
    overwrite: bool,
) -> "tuple[list[tuple[Path, str, dict[str, Any]]], int, list[dict[str, Any]]]":
    """Build sponsor packets independently; collect per-sponsor failures.

    Args:
        root: The artifact root.
        cfg: Pipeline configuration.
        packets: Packet payloads from :func:`_sponsors_with_angels`.
        overwrite: Regenerate packet files that already exist.

    Returns:
        Files to register, count of already-complete packets, and failures.
    """
    files: list[tuple[Path, str, dict[str, Any]]] = []
    skipped = 0
    failed: list[dict[str, Any]] = []
    for packet in packets:
        sponsor_id = int(packet["sponsor_id"])
        try:
            packet_files, packet_skipped = _build_packet_batch(
                root, cfg, [packet], overwrite
            )
        except Exception as error:  # noqa: BLE001 - sponsor-level partial success
            message = str(error)
            logger.error(
                "packet build failed for sponsor %s: %s", sponsor_id, message
            )
            failed.append({"sponsor_id": sponsor_id, "error": message})
            continue
        files.extend(packet_files)
        skipped += packet_skipped
    return files, skipped, failed


def _one_row_per_angel(frame: pl.DataFrame) -> pl.DataFrame:
    """Keep one row per angel letter.

    A child that is still a pending duplicate shares its group's angel
    letter, so the family's angel gets one information sheet, not one
    per submission.

    Args:
        frame: The pending rows.

    Returns:
        The pending rows with one row per angel letter.
    """
    if frame.is_empty():
        return frame
    seen: set[str] = set()
    keep: list[bool] = []
    dropped: list[str] = []
    for angel_id in frame["angel_id"].to_list():
        key = str(angel_id)
        if key in seen:
            keep.append(False)
            dropped.append(key)
            continue
        seen.add(key)
        keep.append(True)
    if dropped:
        logger.info(
            "skipping %d duplicate submission(s) that share an angel"
            " letter: %s",
            len(dropped),
            sorted(set(dropped)),
        )
    return frame.filter(pl.Series(keep))


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
            ``max_workers`` / ``only`` ("sheets", "packets", "both") /
            ``child`` (one child's record id).
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
        JobCancelRequested: When cancellation was requested.
    """
    test = bool(options.get("test"))
    overwrite = bool(options.get("overwrite"))
    sponsor_raw = options.get("sponsor")
    sponsor = int(sponsor_raw) if sponsor_raw is not None else None
    only = str(options.get("only") or "both").strip().lower()
    if only not in ("sheets", "packets", "both"):
        raise InfoSheetError(
            f"only must be sheets, packets, or both, got {only!r}"
        )
    child_raw = options.get("child")
    child = str(child_raw).strip() if child_raw is not None else ""
    if child and only == "packets":
        raise InfoSheetError(
            'a child scope generates info sheets only; use only="sheets"'
        )
    if child and sponsor is not None:
        raise InfoSheetError(
            "a child scope cannot be combined with a sponsor scope"
        )
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
        if child:
            scoped = enriched.filter(pl.col("zite_record_id") == child)
            if scoped.is_empty():
                by_angel = enriched.filter(pl.col("angel_id") == child)
                if by_angel.height > 1:
                    raise InfoSheetError(
                        f"angel id {child!r} matches {by_angel.height} children;"
                        " use the child's record id instead"
                    )
                scoped = by_angel
            if scoped.is_empty():
                raise InfoSheetError(f"no eligible child matches {child!r}")
            # The scope regenerates its sheet even when the marker is set.
            pending, _skipped_unsponsored = _pending_rows(
                scoped, overwrite=True
            )
            if pending.is_empty():
                raise InfoSheetError(
                    f"child {child!r} cannot get an info sheet: it needs an"
                    " audited status and an assigned sponsor"
                )
        elif only == "packets":
            scope = (
                enriched.filter(pl.col("assigned_sponsor_id") == sponsor)
                if sponsor is not None
                else enriched
            )
            pending, _skipped_unsponsored = _pending_rows(
                scope, overwrite=overwrite
            )
        else:
            pending, _skipped_unsponsored = _pending_rows(
                enriched, overwrite=overwrite
            )
        pending = _one_row_per_angel(pending)
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
        packets_blocked: list[dict[str, Any]] = []
        packets_failed: list[dict[str, Any]] = []
        if test:
            logger.info("test mode: skipping sponsor packet generation")
        elif only == "sheets":
            logger.info("sheets-only run: skipping sponsor packet generation")
        else:
            progress("packets")
            root = artifact_root(cfg)
            child_frame = _eligible_frame("child", child_rows)
            family_frame = _eligible_frame("family", family_rows)
            sponsor_frame = _eligible_frame("sponsor", sponsor_rows)
            # Missing audited-child sheets block only that sponsor. An
            # unaudited assigned child is expected to have no sheet.
            packets_blocked = _sponsors_missing_sheets(
                root, cfg, child_frame, family_frame, sponsor
            )
            packets = _sponsors_with_angels(
                cfg,
                sponsor_frame,
                child_frame,
                family_frame,
                sponsor_id=sponsor,
            )
            if packets_blocked:
                blocked_ids = {
                    int(item["sponsor_id"]) for item in packets_blocked
                }
                packets = [
                    packet
                    for packet in packets
                    if int(packet["sponsor_id"]) not in blocked_ids
                ]
                for item in packets_blocked:
                    logger.warning(
                        "skipping sponsor %s: no info sheet for angel(s) %s",
                        item["sponsor_id"],
                        item["angel_ids"],
                    )
            files, packets_skipped, packets_failed = await asyncio.to_thread(
                _build_packets, root, cfg, packets, overwrite
            )
            packets_built = len(packets) - packets_skipped - len(packets_failed)
            cancel()
            for path, kind, meta in files:
                await register_file(
                    repo, kind, path, actor, meta=meta, root=root
                )
            progress(
                "packets",
                completed=packets_built,
                failed=len(packets_failed),
                total=len(packets),
            )

    logger.info(
        "generated %d info sheet(s); %d failed", len(generated), len(failed)
    )
    return GenerateResult(
        sheets_generated=generated,
        sheets_failed=failed,
        sheets_skipped=skipped_stems,
        packets_built=packets_built,
        packets_skipped=packets_skipped,
        packets_blocked=packets_blocked,
        packets_failed=packets_failed,
        info_sheet_dir=str(artifact_root(cfg) / "child_sheets"),
        packets_dir=str(artifact_root(cfg) / "sponsor_packets"),
    )


__all__ = ["MAX_WORKERS_CEILING", "generate"]
