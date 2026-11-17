"""Module 7 — clarification-table extraction (moved from ``scripts/``).

Reads output-eligible children and families independently from the complete
persistent ``final_child`` and ``final_family`` stores, selects children whose
``clarification_notes`` note is set, joins the family contact info (name,
phone number, email) and the date of birth as provided in the source
``raw_child`` form, and exposes the
result both as a DataFrame (consumed by the web app directly) and as
a CSV file (``clarify.clarifications_csv``, default
``output/clarifications.csv``) whose ``clarification_email_file``
column references each family's saved email draft when one exists,
and whose ``contacted``/``contacted_at``/``replied`` columns carry
the per-family contact status maintained by the web app.
"""

import logging
from pathlib import Path
from typing import cast

import polars as pl

from ccat.audit import AuditError
from ccat.audit.store import build_final_frame, eligible_output_rows, read_final
from ccat.config import Config
from ccat.io import read_sheet

logger = logging.getLogger(__name__)


class ClarifyError(Exception):
    """Raised when the clarification tool cannot complete a request."""


#: final_child columns read for each flagged Angel.
CHILD_COLUMNS = (
    "zite_record_id",
    "family_id",
    "angel_id",
    "first_name",
    "clarification_notes",
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
    "other_notes",
)

#: final_family columns read for the family join.
FAMILY_COLUMNS = (
    "first_name",
    "last_name",
    "phone",
    "primary_email",
)

#: Output column order of the clarification table.
OUTPUT_COLUMNS = (
    "angel_id",
    "first_name",
    "raw_dob",
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
    "other_notes",
    "family_id",
    "clarification_notes",
    "family_first_name",
    "family_last_name",
    "family_phone",
    "family_email",
    "zite_record_id",
)


#: CSV-only column (not part of ``build_table`` output): path of the
#: family's saved email draft under ``clarify.output_dir`` when one
#: exists, else empty.
EMAIL_FILE_COLUMN = "clarification_email_file"


def _email_file_series(cfg: Config, table: pl.DataFrame) -> pl.Series:
    """Build each row's saved-draft reference, when a draft exists.

    Args:
        cfg: Pipeline configuration.
        table: The clarification table.

    Returns:
        A String series parallel to ``table``'s rows holding the
        saved-draft HTML path for the row's family, or null.
    """
    out_dir = Path(cfg.clarify.output_dir)
    existing = {
        path.stem: str(path)
        for path in out_dir.glob("family_*.html")
        if path.is_file()
    }
    family_ids = (
        table["family_id"].to_list() if "family_id" in table.columns else []
    )
    values = [
        None if fid is None else existing.get(f"family_{fid}")
        for fid in family_ids
    ]
    return pl.Series(EMAIL_FILE_COLUMN, values, dtype=pl.String)


def _contact_status_columns(
    cfg: Config, table: pl.DataFrame
) -> list[pl.Series]:
    """Build the per-row contact-status CSV columns.

    Merged from the JSON status store (see
    :mod:`ccat.clarify.status`); imported lazily to avoid a circular
    module dependency (``status`` imports this module's
    ``ClarifyError``).

    Args:
        cfg: Pipeline configuration.
        table: The clarification table.

    Returns:
        Three series parallel to ``table``'s rows: ``contacted``
        (Boolean), ``contacted_at`` (String, ISO 8601), ``replied``
        (Boolean).
    """
    from ccat.clarify import status as contact_status

    statuses = contact_status.load_statuses(cfg)
    family_ids = (
        table["family_id"].to_list() if "family_id" in table.columns else []
    )
    entries = [
        None if fid is None else statuses.get(int(fid)) for fid in family_ids
    ]
    return [
        pl.Series(
            contact_status.CONTACTED_COLUMN,
            [entry.contacted if entry else False for entry in entries],
            dtype=pl.Boolean,
        ),
        pl.Series(
            contact_status.CONTACTED_AT_COLUMN,
            [entry.contacted_at if entry else None for entry in entries],
            dtype=pl.String,
        ),
        pl.Series(
            contact_status.REPLIED_COLUMN,
            [entry.replied if entry else False for entry in entries],
            dtype=pl.Boolean,
        ),
    ]


def _read_required(
    excel_file: Path, sheet: str, required: tuple[str, ...]
) -> pl.DataFrame:
    """Read one required source and apply final-output filtering when needed.

    Args:
        excel_file: Path to the CCAT Excel workbook.
        sheet: Sheet name to read.
        required: Columns that must be present.

    Returns:
        Raw sheet contents, or the independently eligible population of a
        complete persistent final store with its full typed schema.

    Raises:
        ClarifyError: If the workbook or source is missing or empty, a final
            store is malformed, or a required column is absent.
    """
    if not excel_file.exists():
        raise ClarifyError(f"Excel file not found: {excel_file}")
    if sheet in {"final_child", "final_family"}:
        entity = sheet.removeprefix("final_")
        try:
            # An explicit workbook makes the config argument unused by the
            # store reader; build_table intentionally remains path-based.
            rows = read_final(cast("Config", None), entity, workbook=excel_file)
            frame = build_final_frame(
                entity, eligible_output_rows(rows, entity)
            )
        except AuditError as error:
            raise ClarifyError(str(error)) from error
    else:
        frame = read_sheet(excel_file, sheet)
    if frame is None or frame.is_empty():
        if sheet.startswith("final_"):
            raise ClarifyError(
                f"{sheet} has no output-eligible rows or is missing"
            )
        raise ClarifyError(f"{sheet} sheet is missing or empty")
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ClarifyError(f"{sheet} lacks column(s): {missing}")
    return frame


def build_table(excel_file: Path) -> pl.DataFrame:
    """Build the clarification table from full finals and source DOBs.

    Eligible children are selected before notes, family contact joins, and raw
    DOB joins.  Families are independently filtered, so a non-ignored child of
    an ignored family remains present with blank contact fields, exactly as in
    the legacy output population.  Source ``raw_child.dob`` is never replaced
    by an adjudicated final DOB.

    Args:
        excel_file: Path to the CCAT Excel workbook.

    Returns:
        A DataFrame with one row per eligible angel needing clarification, in
        :data:`OUTPUT_COLUMNS` order: the angel's id and first name,
        the family id, the ``clarification_notes`` note, the child's
        needs/desires, clothing and shoe sizes, interests and other
        notes, the date of birth as provided in the ``raw_child``
        form, and the family contact info (first/last name, phone
        number, primary email), sorted by ``family_id`` then
        ``angel_id``.  An empty frame (no order guaranteed) means no
        angel needs clarification.

    Raises:
        ClarifyError: If the workbook is missing, a required source is
            missing or empty, a final store is malformed, or a required
            column is absent.
    """
    children = _read_required(excel_file, "final_child", CHILD_COLUMNS)
    note = pl.col("clarification_notes").cast(pl.String).str.strip_chars()
    flagged = children.filter(
        note.is_not_null() & (note.str.strip_chars() != "")
    ).select(CHILD_COLUMNS)
    if flagged.is_empty():
        logger.info("no eligible final_child row carries a clarification note")
        return pl.DataFrame(
            schema={column: pl.String for column in OUTPUT_COLUMNS}
        )

    families = _read_required(excel_file, "final_family", FAMILY_COLUMNS)
    # The date of birth exactly as typed into the registration form.
    raw_child = _read_required(
        excel_file, "raw_child", ("dob", "zite_record_id")
    )
    raw_dob = raw_child.select(
        pl.col("zite_record_id"), pl.col("dob").cast(pl.String).alias("raw_dob")
    )

    family_contact = families.select(
        pl.col("family_id"),
        pl.col("first_name").alias("family_first_name"),
        pl.col("last_name").alias("family_last_name"),
        pl.col("phone").alias("family_phone"),
        pl.col("primary_email").alias("family_email"),
    )
    return (
        flagged.join(family_contact, on="family_id", how="left")
        .join(raw_dob, on="zite_record_id", how="left")
        .sort("family_id", "angel_id")
        .select(OUTPUT_COLUMNS)
    )


def write_csv(
    cfg: Config,
    output: Path | None = None,
    excel_file: Path | None = None,
) -> Path:
    """Build the clarification table and write it to a CSV.

    The CSV carries three extra columns beyond the workbook join:
    ``clarification_email_file`` referencing each row's family's saved
    email draft (``clarify.output_dir/family_<id>.html``) when one
    exists, and the per-family contact status ``contacted``,
    ``contacted_at`` (when the operator marked the email sent), and
    ``replied`` merged from the JSON status store.

    Args:
        cfg: Pipeline configuration.
        output: CSV path; defaults to
            ``clarify.clarifications_csv``.
        excel_file: Workbook path; defaults to ``cfg.excel_file``.

    Returns:
        The CSV path written.

    Raises:
        ClarifyError: If the workbook is missing, a required source is
            missing or empty, a final store is malformed, or a required
            column is absent.
    """
    csv_path = (
        Path(output)
        if output is not None
        else Path(cfg.clarify.clarifications_csv)
    )
    table = build_table(
        Path(excel_file) if excel_file is not None else Path(cfg.excel_file)
    )
    if table.is_empty():
        logger.info("no angels need clarification; writing an empty CSV")
    table = table.with_columns(_email_file_series(cfg, table)).with_columns(
        _contact_status_columns(cfg, table)
    )
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    table.write_csv(csv_path)
    logger.info("wrote %d clarification row(s) to %s", table.height, csv_path)
    return csv_path
