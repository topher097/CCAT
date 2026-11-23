"""Module 5 — child info sheet generation (§13).

For every ``final_child`` row with an **assigned sponsor**
(``assigned_sponsor_id``) and whose ``generated_info_sheet`` flag is
``False``, renders the
LibreOffice template ``info_sheet.template_file`` with the child's data
via py3o and converts the result to a PDF named
``AT<season>_<angel_id>.pdf`` in ``info_sheet.output_dir``.  Children
without an assigned sponsor are skipped (run `ccat match` first).
Orphaned children (``family_id`` null — no matching family
registration) and ignored entities are excluded by the eligible-final
readers but remain in the persistent store for later correction.
Successfully generated
rows flip their flag to ``True`` so reruns only process pending children
(§13).  Failed rows keep ``False`` and are reported as one typed error
after the flags are written, so a rerun picks them up again.

Sheets are generated on a thread pool (``info_sheet.max_workers``,
clamped to the number of pending children): the py3o render and the
headless LibreOffice conversion of different children are independent,
and each worker thread uses its own temporary LibreOffice profile so
concurrent conversions cannot block each other.  A ``tqdm`` progress
bar tracks completions (silenced when stderr is not a terminal).
Ctrl+C stops cleanly: queued children are cancelled, in-flight
LibreOffice conversions are terminated within half a second, the flags
of every child whose PDF was already written are committed in the same
atomic sheet write, and the interrupt is re-raised.

Template variables are mapped onto columns by ``info_sheet.mappings``
(config-driven).  Mapped sources are resolved against an *enriched* row:
the ``final_child`` columns (including the child's own
``assigned_sponsor_id``), plus ``gift_pickup_location`` joined from
``final_family`` by ``family_id``, plus ``family_size`` (the number of
``final_child`` rows sharing that ``family_id``).  The ``sponsor.*``
section fills from the child's assigned ``final_sponsor`` row (joined by
the child's ``assigned_sponsor_id``):
name (organization name, falling back to first + last name), email,
phone, and sponsor id; ``sponsor.angel_id`` shows the child's own
``angel_id`` (the same value as ``info.angel_id``), and
``sponsor.family_size`` maps to ``family_size`` (the number of angels
in the child's family).
A sponsor whose affiliation is listed in
``contact_placeholder_affiliations`` (season config: Organization,
Walmart — everyone but Individual) but has missing contact info
(name, email, phone) renders **all three** of "Assigned Sponsor
Name", "Sponsor Email", and "Sponsor Phone" as underscore
placeholders (length ``info_sheet.contact_placeholder_length``,
default 30) for hand-filling; the placeholder text is kept in the
converted PDF.  Individual sponsors with missing contact info render
blank, as do unassigned children.  Children without an
assigned sponsor render that section blank for hand-filling, as do
unmapped ``sponsor.*`` placeholders.  The ``generated_info_sheet``
flag is owned directly by ``final_child`` and updated in one atomic
complete-store write; the enrichment columns are never persisted.
"""

import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import polars as pl
from py3o.template import Template
from tqdm import tqdm

from ccat import sponsor_packets
from ccat.audit.store import (
    STATUS_AUDITED,
    read_eligible_final_frame,
    read_final,
    write_final_rows,
)
from ccat.config import Config
from ccat.match import canonical_location

logger = logging.getLogger(__name__)

#: Template placeholders of the sponsor hand-fill section (§13); mapped
#: sources (config ``sponsor.*`` mappings) fill them from the enriched
#: row (``sponsor.angel_id`` maps to the child's own ``angel_id``, the
#: rest to the assigned ``final_sponsor`` join), and unmapped ones
#: render blank for the sponsor to complete by hand.
SPONSOR_PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "full_name",
        "email",
        "phone",
        "sponsor_id",
        "angel_id",
        "family_size",
    }
)

#: ``final_family`` columns joined onto each child row (§13).  The
#: sponsor is the child's own ``assigned_sponsor_id`` column, not a
#: family join: the child row is the assignment grain.
FAMILY_JOIN_COLUMNS = (
    "family_id",
    "gift_pickup_location",
)

#: ``final_sponsor`` columns joined onto each child row (§13), aliased to
#: avoid collisions with same-named ``final_child`` columns.
SPONSOR_JOIN_COLUMNS: tuple[tuple[str, str], ...] = (
    ("first_name", "sponsor_first_name"),
    ("last_name", "sponsor_last_name"),
    ("organization_name", "sponsor_organization_name"),
    ("email", "sponsor_email"),
    ("phone", "sponsor_phone"),
    ("sponsor_type", "sponsor_type"),
)

#: ``sponsor.*`` template variables treated as sponsor contact info
#: (§13.2): when the assigned sponsor's sheet lacks the value, these
#: render as the underscore placeholder so the sponsor can hand-fill it.
CONTACT_PLACEHOLDERS: frozenset[str] = frozenset(
    {"full_name", "email", "phone"}
)

#: Underscore placeholder rendered for missing sponsor contact info
#: (§13.2) — a hand-fill line of a fixed length, kept visible in the
#: converted PDF.  Default length; the configured value is
#: ``info_sheet.contact_placeholder_length``.
SPONSOR_CONTACT_PLACEHOLDER = "_" * 30

#: Computed columns added on top of the ``final_child`` schema (§13).
COMPUTED_COLUMNS = ("family_size", "sponsor_name")

#: Hard ceiling for one headless LibreOffice conversion, in seconds.
CONVERT_TIMEOUT_SECONDS = 300


class InfoSheetError(Exception):
    """Raised when child info sheet generation cannot complete."""


class InfoSheetCancelledError(Exception):
    """Raised inside workers when generation is stopping (Ctrl+C).

    Never a failure: the row simply stays pending and is picked up by
    the next run.
    """


def _terminate(process: subprocess.Popen[str]) -> None:
    """Terminate a conversion subprocess tree, escalating to SIGKILL.

    ``soffice`` spawns helper processes (oosplash, ``soffice.bin``), and
    :func:`_convert_to_pdf` starts every conversion in its own session,
    so signalling the process group stops the whole tree at once.

    Args:
        process: The LibreOffice conversion process to stop.
    """
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        process.wait()


def _load_enriched_rows(cfg: Config) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Read ``final_child`` and enrich it with family and sponsor data (§13).

    Eligible readers reproduce the retired final projections: ignored
    entities are excluded, and children must have a ``family_id``.  The
    full store is not modified by enrichment.

    Args:
        cfg: The pipeline configuration.

    Returns:
        A tuple of (the eligible ``final_child`` frame, that frame plus
        family/sponsor joins and computed columns).

    Raises:
        InfoSheetError: If ``final_child`` is missing or has no eligible
            rows, if ``final_family``/``final_sponsor`` is missing or
            lacks the join columns, or if ``family_id``/``sponsor_id`` is
            duplicated or missing on the family/sponsor side.  Unmatched
            family ids (ignored families under manual matching) warn and
            keep a blank drop-off instead of failing.
    """
    frame = read_eligible_final_frame(cfg, "child")
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
        # Orphaned children (no matching family registration) cannot be
        # generated; ignore them for the user to fix later.
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
    family_frame = read_eligible_final_frame(cfg, "family")
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
    # sponsor_packets).  A genuinely missing family row degrades the
    # same way, so warn instead of failing (§13b divergence note).
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
    sponsor_frame = read_eligible_final_frame(cfg, "sponsor")
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
    return frame, enriched


def _pending_rows(
    frame: pl.DataFrame, overwrite: bool = False
) -> tuple[pl.DataFrame, int]:
    """Select rows still awaiting a generated info sheet (§13.4, §7.5).

    A row is pending when it is **audited** (``status == "audited"``,
    the readiness gate replacing the old ``audited`` Boolean), it has an
    **assigned sponsor** (its own ``assigned_sponsor_id``), and its
    ``generated_info_sheet``
    flag is False (every sponsored audited row, with ``overwrite``) --
    the sheet's purpose is to hand the sponsor their child's wish list,
    so unsponsored or unaudited children get no sheet.

    Args:
        frame: An enriched ``final_child`` frame (needs the child's own
            ``assigned_sponsor_id`` column and ``status``).
        overwrite: Ignore the ``generated_info_sheet`` column: every
            sponsored audited row is pending.

    Returns:
        A tuple of (the pending rows, how many rows were skipped
        because the child has no assigned sponsor).

    Raises:
        InfoSheetError: If a pending row lacks ``zite_record_id`` or a
            usable ``angel_id`` filename, or ``final_child.status`` is
            missing (run ``ccat audit`` first).
    """
    if "status" not in frame.columns:
        raise InfoSheetError(
            "final_child has no status column; run `ccat audit` first"
        )
    if overwrite:
        pending = frame
    else:
        pending = frame.filter(~pl.col("generated_info_sheet"))
    audited = pending.filter(pl.col("status") == STATUS_AUDITED)
    if audited.height < pending.height:
        logger.info(
            "skipping %d child(ren) not yet audited (status gate, §7.5)",
            pending.height - audited.height,
        )
    pending = audited
    sponsored = pending.filter(pl.col("assigned_sponsor_id").is_not_null())
    skipped_unsponsored = pending.height - sponsored.height
    if skipped_unsponsored:
        logger.info(
            "skipping %d child(ren) without an assigned sponsor",
            skipped_unsponsored,
        )
    pending = sponsored
    missing_records = pending.filter(pl.col("zite_record_id").is_null())
    if not missing_records.is_empty():
        raise InfoSheetError(
            "pending final_child rows without zite_record_id; run"
            " `ccat audit` first"
        )
    angel_text = pl.col("angel_id").cast(pl.String).str.strip_chars()
    bad_angels = pending.filter(
        angel_text.is_null()
        | (angel_text == "")
        | angel_text.str.contains_any(["/", "\\"])
        | angel_text.str.contains("..", literal=True)
    )
    if not bad_angels.is_empty():
        ids = bad_angels["zite_record_id"].to_list()
        raise InfoSheetError(
            "pending final_child rows without a usable angel_id filename:"
            f" {ids}"
        )
    return pending, skipped_unsponsored


def _nested_data(
    row: dict[str, Any],
    mappings: dict[str, str],
    contact_placeholder: str = SPONSOR_CONTACT_PLACEHOLDER,
    placeholder_affiliations: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Build the nested py3o data dict for one row (§13).

    Dotted template variables (``"entry.colors"``,
    ``"entry.footwear.size"``) expand into fully nested namespace dicts;
    null values render as empty strings.  ``sponsor.*`` placeholders fill
    from the row's ``sponsor_*`` sources when mapped (assigned sponsor)
    and render blank otherwise (unassigned child or unmapped
    placeholder).

    Missing sponsor contact info (``sponsor.full_name``/``email``/
    ``phone`` — null, empty, or whitespace-only) renders the underscore
    placeholder for hand-filling when the child has an assigned sponsor
    whose affiliation is in ``placeholder_affiliations`` (§13.2).
    All-or-nothing: when any of the three is missing, all three render
    the placeholder so the coordinator hand-fills the whole block.
    Everyone else — unassigned children, unlisted affiliations — keeps
    the blank section.

    Args:
        row: One enriched ``final_child`` row as a plain dict.
        mappings: Config template-variable -> column-key mappings.
        contact_placeholder: The underscore hand-fill line rendered for
            missing sponsor contact info.
        placeholder_affiliations: Affiliations receiving the
            placeholder (joined ``sponsor_type`` column).

    Returns:
        The py3o data dict, e.g. ``{"entry": {"footwear": {"size": 2}}}``.
    """
    sponsored = row.get("assigned_sponsor_id") is not None
    placeholder_sponsor = sponsored and (
        row.get("sponsor_type") in placeholder_affiliations
    )
    contact_missing = False
    if placeholder_sponsor:
        for variable, column in mappings.items():
            parts = variable.split(".")
            if parts[0] != "sponsor" or parts[-1] not in CONTACT_PLACEHOLDERS:
                continue
            value = row.get(column)
            contact_missing = (
                contact_missing
                or value is None
                or (isinstance(value, str) and not value.strip())
            )
    data: dict[str, Any] = {}
    for variable, column in mappings.items():
        parts = variable.split(".")
        node = data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        value = row.get(column)
        if (
            contact_missing
            and parts[0] == "sponsor"
            and parts[-1] in CONTACT_PLACEHOLDERS
        ):
            value = contact_placeholder
        node[parts[-1]] = "" if value is None else value
    for placeholder in SPONSOR_PLACEHOLDERS:
        data.setdefault("sponsor", {}).setdefault(placeholder, "")
    return data


def _render_one(
    template_file: Path, data: dict[str, Any], odt_path: Path
) -> None:
    """Render the template with py3o into one ODT file.

    Args:
        template_file: Path to the ``.ott`` template.
        data: The nested py3o data dict.
        odt_path: Destination ODT path (parent must exist).

    Raises:
        InfoSheetError: If the render fails.
    """
    try:
        Template(str(template_file), str(odt_path)).render(data)
    except Exception as error:  # noqa: BLE001 - py3o raises many types
        raise InfoSheetError(
            f"py3o render failed for {odt_path.name}: {error}"
        ) from error


def _convert_to_pdf(
    odt_path: Path,
    out_dir: Path,
    soffice_binary: str,
    profile: Path,
    stop: threading.Event | None = None,
) -> Path:
    """Convert one ODT to PDF with headless LibreOffice (§13).

    The subprocess is polled instead of blocking, so a set ``stop``
    event (Ctrl+C) terminates the conversion within half a second;
    the overall deadline stays :data:`CONVERT_TIMEOUT_SECONDS`.

    Args:
        odt_path: The rendered ODT file; the PDF lands in ``out_dir``
            under the same base name.
        out_dir: Directory receiving the PDF.
        soffice_binary: LibreOffice binary path.
        profile: Directory for the isolated LibreOffice user profile, so
            a running LibreOffice GUI cannot block the conversion.
        stop: When set, the conversion subprocess is terminated and
            :class:`InfoSheetCancelledError` is raised.

    Returns:
        The created PDF path.

    Raises:
        InfoSheetCancelledError: When ``stop`` is set mid-conversion.
        InfoSheetError: If the conversion fails, times out, or produces
            no non-empty PDF.
    """
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
    deadline = time.monotonic() + CONVERT_TIMEOUT_SECONDS
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
                    f"soffice conversion timed out after"
                    f" {CONVERT_TIMEOUT_SECONDS}s for {odt_path.name}"
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


def _find_binary(name: str) -> str | None:
    """Resolve the LibreOffice binary path.

    Args:
        name: Binary name or path from the configuration.

    Returns:
        The resolved path, or None when not found.
    """
    if Path(name).is_file():
        return name
    return shutil.which(name)


def _commit_generated_flags(cfg: Config, generated: list[Any]) -> None:
    """Mark actual PDF successes in the full ``final_child`` store (§7.5).

    Only ``generated_info_sheet`` changes.  Ignored, orphaned, raw-missing,
    and failed rows remain present with their existing values.  The
    complete store is written atomically through the generate-owned writer;
    its final-child formula cache receives ``cfg.xmas`` automatically.

    Args:
        cfg: Pipeline configuration.
        generated: ``zite_record_id`` values generated this run.
    """
    if not generated:
        return
    rows = read_final(cfg, "child")
    done = {str(value) for value in generated}
    for row in rows:
        if str(row.get("zite_record_id")) in done:
            row["generated_info_sheet"] = True
    write_final_rows(cfg, module="generate", entity="child", rows=rows)


def _sheet_stem(cfg: Config, angel_id: str) -> str:
    """Info-sheet filename stem for one child (§13).

    Args:
        cfg: The pipeline configuration (season labels the tree year).
        angel_id: The child's angel id.

    Returns:
        The stem, e.g. ``AT2026_3B``.
    """
    return f"AT{cfg.season}_{angel_id}"


def run(
    cfg: Config,
    test: bool = False,
    overwrite: bool = False,
    sponsor: int | None = None,
    max_workers: int | None = None,
) -> None:
    """Generate child info sheet PDFs, then sponsor packets (§13, §13b).

    Sheet phase: pending rows are children with an **assigned sponsor**
    (their own ``assigned_sponsor_id``) whose ``generated_info_sheet``
    flag is
    False (all sponsored rows when ``overwrite`` is set) --
    unsponsored children get no sheet.  Pending rows are rendered with
    py3o and converted to ``AT<season>_<angel_id>.pdf`` in
    ``info_sheet.output_dir`` on a thread pool of ``max_workers``
    workers (``info_sheet.max_workers`` by default, clamped to the
    pending row count), tracked by a ``tqdm`` progress bar.
    Successfully generated rows flip their ``generated_info_sheet``
    flag directly in ``final_child`` in one atomic complete-store write,
    so an interrupted run never leaves a half-updated workbook and failed
    rows are simply picked up again on the next run.  Ctrl+C cancels
    queued rows, terminates in-flight LibreOffice conversions, commits
    the flags of the children already generated, and re-raises the
    interrupt.

    Packet phase: after every sponsored angel's sheet exists, one
    sponsor packet directory per assigned sponsor is built under
    ``sponsor_packets.output_dir`` (email draft + combined child
    information PDF + instruction file copy, §13b).  ``--test`` stops
    after the first sheet and skips packets.

    Args:
        cfg: The pipeline configuration.
        test: Stop after the first successfully generated sheet (no
            packets).
        overwrite: Ignore the ``generated_info_sheet`` column: every
            sponsored child's PDF is regenerated (existing files are
            overwritten) and the packets are rebuilt.
        sponsor: Restrict the run to one sponsor: the sheet phase only
            renders children assigned to that ``sponsor_id`` and only
            that sponsor's packet is built (its sheets must all exist).
        max_workers: Override ``info_sheet.max_workers``: how many
            children render and convert concurrently.

    Raises:
        KeyboardInterrupt: On Ctrl+C, after cancelling the pool and
            committing the flags of the children already generated.
        InfoSheetError: After any per-row failure (all successes are
            already flagged).
    """
    spec = cfg.info_sheet
    template_file = Path(spec.template_file)
    if not template_file.is_file():
        raise InfoSheetError(f"template file not found: {template_file}")
    if _find_binary(spec.soffice_binary) is None:
        raise InfoSheetError(
            f"LibreOffice binary not found: {spec.soffice_binary!r}"
        )
    out_dir = Path(spec.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _base, enriched = _load_enriched_rows(cfg)
    pending, _skipped = _pending_rows(enriched, overwrite=overwrite)
    if sponsor is not None:
        scoped = pending.filter(pl.col("assigned_sponsor_id") == sponsor)
        logger.info(
            "--sponsor %s: %d of %d pending row(s) belong to it",
            sponsor,
            scoped.height,
            pending.height,
        )
        pending = scoped
    generated: list[Any] = []
    failed: list[str] = []
    interrupted = False
    if pending.is_empty():
        logger.info("no children pending an info sheet")
    else:
        known_sources = set(enriched.columns)
        unknown_sources = sorted(set(spec.mappings.values()) - known_sources)
        for column in unknown_sources:
            logger.warning(
                "mapping source not found in enriched row: %s", column
            )
        rows = pending.to_dicts()
        if test:
            rows = rows[:1]
            logger.info("test mode: generating one info sheet only")
        workers = min(
            spec.max_workers if max_workers is None else max_workers,
            len(rows),
        )
        with tempfile.TemporaryDirectory(
            prefix="ccat_info_sheets_", ignore_cleanup_errors=True
        ) as tmp:
            tmp_dir = Path(tmp)
            stop = threading.Event()
            workspaces: dict[int, Path] = {}
            workspace_lock = threading.Lock()

            def _workspace() -> Path:
                """One ODT scratch dir + LO profile per worker thread."""
                ident = threading.get_ident()
                with workspace_lock:
                    path = workspaces.get(ident)
                    if path is None:
                        path = tmp_dir / f"worker{len(workspaces)}"
                        (path / "lo_profile").mkdir(parents=True)
                        workspaces[ident] = path
                    return path

            def _work(row: dict[str, Any]) -> str:
                """Render and convert one child's sheet (§13.4).

                Raises:
                    InfoSheetCancelledError: When the run is stopping before
                        this row started or mid-conversion.
                """
                if stop.is_set():
                    raise InfoSheetCancelledError()
                stem = _sheet_stem(cfg, str(row["angel_id"]))
                # Sheets nest under the angel's canonical pickup
                # location bucket (e.g. output/child_info_sheets/
                # hancock/AT2026_1A.pdf); unmapped labels go to
                # "unknown".
                location = (
                    canonical_location(
                        row.get("gift_pickup_location"),
                        cfg.location_groups,
                    )
                    or "unknown"
                )
                location_dir = out_dir / location
                location_dir.mkdir(parents=True, exist_ok=True)
                workspace = _workspace()
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
                _convert_to_pdf(
                    odt_path,
                    location_dir,
                    spec.soffice_binary,
                    workspace / "lo_profile",
                    stop=stop,
                )
                return stem

            def _consume(
                future: Future[str],
                stem: str,
                zite_record_id: Any,
            ) -> None:
                """Record one finished row's outcome (main thread)."""
                try:
                    future.result()
                except InfoSheetCancelledError:
                    pass
                except Exception as error:  # noqa: BLE001 - per-row failures
                    failed.append(stem)
                    logger.error(
                        "info sheet generation failed for %s: %s",
                        stem,
                        error,
                    )
                else:
                    generated.append(zite_record_id)
                    logger.debug("generated info sheet %s", stem)

            futures: dict[Future[str], tuple[str, Any]] = {}
            handled: set[Future[str]] = set()
            progress = tqdm(
                total=len(rows),
                desc="Info sheets",
                unit="sheet",
                disable=None,  # silent when stderr is not a terminal
            )
            executor = ThreadPoolExecutor(max_workers=workers)
            try:
                for row in rows:
                    future = executor.submit(_work, row)
                    futures[future] = (
                        _sheet_stem(cfg, str(row["angel_id"])),
                        row["zite_record_id"],
                    )
                for future in as_completed(futures):
                    stem, zite_record_id = futures[future]
                    _consume(future, stem, zite_record_id)
                    handled.add(future)
                    progress.update(1)
            except KeyboardInterrupt:
                stop.set()
                # Cancel the queue; running workers see the event within
                # half a second and kill their LibreOffice subprocess.
                executor.shutdown(wait=True, cancel_futures=True)
                for future in set(futures) - handled:
                    if future.cancelled() or not future.done():
                        continue
                    stem, zite_record_id = futures[future]
                    try:
                        _consume(future, stem, zite_record_id)
                    except KeyboardInterrupt:
                        # The very future that delivered the interrupt;
                        # do not let it abort the flag write below.
                        pass
                logger.warning(
                    "interrupted: %d of %d sheet(s) were generated;"
                    " their flags are recorded, the rest stay pending",
                    len(generated),
                    len(rows),
                )
                interrupted = True
            else:
                executor.shutdown(wait=True)
            finally:
                progress.close()
    _commit_generated_flags(cfg, generated)
    logger.info(
        "generated %d info sheet(s); %d failed", len(generated), len(failed)
    )
    if interrupted:
        raise KeyboardInterrupt from None
    if failed:
        raise InfoSheetError(f"info sheet generation failed for: {failed}")
    if test:
        logger.info("test mode: skipping sponsor packet generation")
        return
    if sponsor is not None:
        # Only this sponsor's packet; its own sheets must all exist.
        sponsor_packets.validate_all_sheets_exist(cfg, sponsor_id=sponsor)
        sponsor_packets.build_packets(
            cfg, overwrite=overwrite, sponsor_id=sponsor
        )
        return
    # The packets combine every sponsored angel's sheet, so all sheets
    # must exist before the packet phase runs.
    sponsor_packets.validate_all_sheets_exist(cfg)
    sponsor_packets.build_packets(cfg, overwrite=overwrite)
