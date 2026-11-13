"""Module 5 — child info sheet generation (§13).

For every ``final_child`` row whose family has an **assigned sponsor**
and whose ``generated_info_sheet`` flag is ``False``, renders the
LibreOffice template ``info_sheet.template_file`` with the child's data
via py3o and converts the result to a PDF named
``AT<season>_<angel_id>.pdf`` in ``info_sheet.output_dir``.  Children
without an assigned sponsor are skipped (run `ccat match` first).
Successfully generated
rows flip their flag to ``True`` so reruns only process pending children
(§13).  Failed rows keep ``False`` and are reported as one typed error
after the flags are written, so a rerun picks them up again.

Template variables are mapped onto columns by ``info_sheet.mappings``
(config-driven).  Mapped sources are resolved against an *enriched* row:
the ``final_child`` columns plus ``gift_pickup_location`` and
``assigned_sponsor_id`` joined from ``final_family`` by ``family_id``,
plus ``family_size`` (the number of ``final_child`` rows sharing that
``family_id``).  The ``sponsor.*`` section fills from the child's
assigned ``final_sponsor`` row (joined by ``assigned_sponsor_id``):
name (organization name, falling back to first + last name), email,
phone, and sponsor id; ``sponsor.angel_id`` shows the child's own
``angel_id`` (the same value as ``info.angel_id``), and
``sponsor.family_size`` maps to ``family_size`` (the number of angels
in the child's family).
Children without an assigned sponsor render that section blank for
hand-filling, as do unmapped ``sponsor.*`` placeholders.  Only the
original ``final_child`` columns plus ``generated_info_sheet`` are
written back to the workbook; the enrichment columns never reach the
sheet.
"""

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import polars as pl
from py3o.template import Template

from ccat.config import Config
from ccat.io import read_sheet, write_sheets

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

#: ``final_family`` columns joined onto each child row (§13).
FAMILY_JOIN_COLUMNS = (
    "family_id",
    "gift_pickup_location",
    "assigned_sponsor_id",
)

#: ``final_sponsor`` columns joined onto each child row (§13), aliased to
#: avoid collisions with same-named ``final_child`` columns.
SPONSOR_JOIN_COLUMNS: tuple[tuple[str, str], ...] = (
    ("first_name", "sponsor_first_name"),
    ("last_name", "sponsor_last_name"),
    ("organization_name", "sponsor_organization_name"),
    ("email", "sponsor_email"),
    ("phone", "sponsor_phone"),
)

#: Computed columns added on top of the ``final_child`` schema (§13).
COMPUTED_COLUMNS = ("family_size", "sponsor_name")

#: Hard ceiling for one headless LibreOffice conversion, in seconds.
CONVERT_TIMEOUT_SECONDS = 300


class InfoSheetError(Exception):
    """Raised when child info sheet generation cannot complete."""


def _normalize_flag(frame: pl.DataFrame) -> tuple[pl.DataFrame, bool]:
    """Normalize the ``generated_info_sheet`` column to Boolean.

    Adds the column (all ``False``) when missing -- the first-run schema
    evolution (§13) -- fills nulls with ``False``, and tolerates string
    booleans ("true"/"false") coming back from the sheet.

    Args:
        frame: The ``final_child`` frame.

    Returns:
        A tuple of (normalized frame, whether the column was missing).
    """
    name = "generated_info_sheet"
    if name not in frame.columns:
        return frame.with_columns(pl.lit(False).alias(name)), True
    if frame.schema[name] == pl.Boolean:
        return frame.with_columns(pl.col(name).fill_null(False)), False
    text = pl.col(name).cast(pl.String).str.strip_chars().str.to_lowercase()
    normalized = pl.when(text == "true").then(True).otherwise(False).alias(name)
    return frame.with_columns(normalized), False


def _load_enriched_rows(cfg: Config) -> tuple[pl.DataFrame, pl.DataFrame, bool]:
    """Read ``final_child`` and enrich it with family and sponsor data (§13).

    Args:
        cfg: The pipeline configuration.

    Returns:
        A tuple of (base frame -- the normalized original
        ``final_child`` columns, enriched frame -- base plus the family
        and sponsor join and computed columns, whether
        ``generated_info_sheet`` was newly added to the schema).

    Raises:
        InfoSheetError: If ``final_child`` is missing or empty, if
            ``final_family``/``final_sponsor`` is missing or lacks the
            join columns, if ``family_id``/``sponsor_id`` is duplicated
            or missing on the family/sponsor side, or if a child row has
            no ``family_id``.
    """
    frame = read_sheet(cfg.excel_file, "final_child")
    if frame is None or frame.is_empty():
        raise InfoSheetError(
            "final_child sheet is missing or empty; run `ccat audit` first"
        )
    frame, schema_added = _normalize_flag(frame)
    if "family_id" not in frame.columns:
        raise InfoSheetError(
            "final_child has no family_id column; run `ccat match` first"
        )
    orphaned = frame.filter(pl.col("family_id").is_null())
    if not orphaned.is_empty():
        ids = orphaned["zite_record_id"].to_list()
        raise InfoSheetError(
            "final_child rows without family_id (run `ccat match` first):"
            f" {ids}"
        )
    family_frame = read_sheet(cfg.excel_file, "final_family")
    if family_frame is None or family_frame.is_empty():
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
    sponsor_frame = read_sheet(cfg.excel_file, "final_sponsor")
    if sponsor_frame is None or sponsor_frame.is_empty():
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
    return frame, enriched, schema_added


def _pending_rows(
    frame: pl.DataFrame, overwrite: bool = False
) -> tuple[pl.DataFrame, int]:
    """Select rows still awaiting a generated info sheet (§13.4).

    A row is pending when its family has an **assigned sponsor** and
    its ``generated_info_sheet`` flag is False (every sponsored row,
    with ``overwrite``) -- the sheet's purpose is to hand the sponsor
    their child's wish list, so unsponsored children get no sheet.

    Args:
        frame: An enriched ``final_child`` frame (needs the
            ``assigned_sponsor_id`` join column).
        overwrite: Ignore the ``generated_info_sheet`` column: every
            sponsored row is pending.

    Returns:
        A tuple of (the pending rows, how many rows were skipped
        because their family has no assigned sponsor).

    Raises:
        InfoSheetError: If a pending row lacks ``zite_record_id`` or a
            usable ``angel_id`` filename.
    """
    if overwrite:
        pending = frame
    else:
        pending = frame.filter(~pl.col("generated_info_sheet"))
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
    row: dict[str, Any], mappings: dict[str, str]
) -> dict[str, Any]:
    """Build the nested py3o data dict for one row (§13).

    Dotted template variables (``"entry.colors"``,
    ``"entry.footwear.size"``) expand into fully nested namespace dicts;
    null values render as empty strings.  ``sponsor.*`` placeholders fill
    from the row's ``sponsor_*`` sources when mapped (assigned sponsor)
    and render blank otherwise (unassigned child or unmapped
    placeholder).

    Args:
        row: One enriched ``final_child`` row as a plain dict.
        mappings: Config template-variable -> column-key mappings.

    Returns:
        The py3o data dict, e.g. ``{"entry": {"footwear": {"size": 2}}}``.
    """
    data: dict[str, Any] = {}
    for variable, column in mappings.items():
        parts = variable.split(".")
        node = data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        value = row.get(column)
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
    odt_path: Path, out_dir: Path, soffice_binary: str, profile: Path
) -> Path:
    """Convert one ODT to PDF with headless LibreOffice (§13).

    Args:
        odt_path: The rendered ODT file; the PDF lands in ``out_dir``
            under the same base name.
        out_dir: Directory receiving the PDF.
        soffice_binary: LibreOffice binary path.
        profile: Directory for the isolated LibreOffice user profile, so
            a running LibreOffice GUI cannot block the conversion.

    Returns:
        The created PDF path.

    Raises:
        InfoSheetError: If the conversion fails or produces no non-empty
            PDF.
    """
    result = subprocess.run(
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
        capture_output=True,
        text=True,
        timeout=CONVERT_TIMEOUT_SECONDS,
        check=False,
    )
    pdf_path = out_dir / f"{odt_path.stem}.pdf"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise InfoSheetError(
            f"soffice conversion failed for {odt_path.name} (exit"
            f" {result.returncode}): {detail[-400:]}"
        )
    if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
        raise InfoSheetError(
            f"soffice produced no PDF for {odt_path.name}:"
            f" {(result.stderr or result.stdout or '').strip()[-400:]}"
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


def _record_flags(frame: pl.DataFrame, generated: list[Any]) -> pl.DataFrame:
    """Flip ``generated_info_sheet`` to True for the generated rows.

    Args:
        frame: The base ``final_child`` frame.
        generated: ``zite_record_id`` values generated this run.

    Returns:
        The frame with updated flags (unchanged when ``generated`` is
        empty).
    """
    if not generated:
        return frame
    return frame.with_columns(
        pl.when(pl.col("zite_record_id").is_in(generated))
        .then(True)
        .otherwise(pl.col("generated_info_sheet"))
        .alias("generated_info_sheet")
    )


def _sheet_stem(cfg: Config, angel_id: str) -> str:
    """Info-sheet filename stem for one child (§13).

    Args:
        cfg: The pipeline configuration (season labels the tree year).
        angel_id: The child's angel id.

    Returns:
        The stem, e.g. ``AT2026_3B``.
    """
    return f"AT{cfg.season}_{angel_id}"


def run(cfg: Config, test: bool = False, overwrite: bool = False) -> None:
    """Generate child info sheet PDFs for all pending children (§13).

    Pending rows are children whose family has an **assigned sponsor**
    and whose ``generated_info_sheet`` flag is False (all sponsored
    rows when ``overwrite`` is set) -- unsponsored children get no
    sheet.  Each pending row is
    rendered with py3o and converted to ``AT<season>_<angel_id>.pdf``
    in ``info_sheet.output_dir``; successfully generated rows flip
    their flag to ``True`` in one atomic sheet write at the end, so an
    interrupted run never leaves a half-updated workbook and failed
    rows are simply picked up again on the next run.

    Args:
        cfg: The pipeline configuration.
        test: Stop after the first successfully generated sheet.
        overwrite: Ignore the ``generated_info_sheet`` column: every
            sponsored child's PDF is regenerated (existing files are
            overwritten).
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
    base, enriched, schema_added = _load_enriched_rows(cfg)
    pending, _skipped = _pending_rows(enriched, overwrite=overwrite)
    if pending.is_empty():
        logger.info("no children pending an info sheet")
        if schema_added:
            write_sheets(cfg.excel_file, "generate", {"final_child": base})
        return
    known_sources = set(enriched.columns)
    unknown_sources = sorted(set(spec.mappings.values()) - known_sources)
    for column in unknown_sources:
        logger.warning("mapping source not found in enriched row: %s", column)
    generated: list[Any] = []
    failed: list[str] = []
    with tempfile.TemporaryDirectory(prefix="ccat_info_sheets_") as tmp:
        tmp_dir = Path(tmp)
        profile = tmp_dir / "lo_profile"
        profile.mkdir()
        for row in pending.to_dicts():
            stem = _sheet_stem(cfg, str(row["angel_id"]))
            odt_path = tmp_dir / f"{stem}.odt"
            try:
                _render_one(
                    template_file, _nested_data(row, spec.mappings), odt_path
                )
                _convert_to_pdf(odt_path, out_dir, spec.soffice_binary, profile)
            except Exception as error:  # noqa: BLE001 - per-row failures
                failed.append(stem)
                logger.error(
                    "info sheet generation failed for %s: %s", stem, error
                )
                continue
            generated.append(row["zite_record_id"])
            logger.info("generated info sheet %s/%s.pdf", out_dir, stem)
            if test:
                logger.info("test mode: stopping after first info sheet")
                break
    base = _record_flags(base, generated)
    write_sheets(cfg.excel_file, "generate", {"final_child": base})
    logger.info(
        "generated %d info sheet(s); %d failed", len(generated), len(failed)
    )
    if failed:
        raise InfoSheetError(f"info sheet generation failed for: {failed}")
