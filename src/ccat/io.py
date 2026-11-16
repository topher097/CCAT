"""Excel workbook and logging helpers.

Thin polars/openpyxl-based read/write helpers over the single pipeline
workbook (§4).  Sheet writes are atomic (write a temp file, then replace)
and the module enforces the ownership matrix from §9: a module may only
write sheets it owns.

``final_child`` writes keep ``age_at_xmas`` a live spreadsheet formula:
each age cell stores ``=DATEDIF(<dob cell>, DATE(Y,M,D), "y")`` with the
computed age embedded as its cached value, so editing a ``dob`` in
LibreOffice/Excel recalculates the age while fastexcel reads (see
:func:`read_sheet`) keep seeing the cached number.
"""

import logging
import os
import re
import sys
import xml.etree.ElementTree as ElementTree
import zipfile
from datetime import date, datetime
from pathlib import Path
from xml.sax.saxutils import escape

import polars as pl
from openpyxl import Workbook, load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet
from polars._typing import PolarsDataType

logger = logging.getLogger(__name__)


class _DtypeFallbackFilter(logging.Filter):
    """Drop fastexcel's all-null-column dtype fallback warnings.

    ``read_sheet`` reads every sheet through fastexcel, which logs a
    WARNING per column it cannot infer a dtype for -- i.e. columns that
    are entirely null in the sheet.  Falling back to String is exactly
    the behavior this module is designed around (see
    :func:`read_sheet`), so the warning carries no information.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Return False for dtype-fallback warnings, True otherwise."""
        return "Could not determine dtype" not in record.getMessage()


_FASTEXCEL = logging.getLogger("fastexcel")
_FASTEXCEL.addFilter(_DtypeFallbackFilter())
# A logger level applies to descendants (fastexcel.types.dtype), while a
# filter on this logger would not; keep both so the fallback notice is
# silenced regardless of how the record is routed.
_FASTEXCEL.setLevel(logging.ERROR)

#: Sheet names in the workbook contract (§4).
SHEETS = (
    "raw_sponsor",
    "raw_family",
    "raw_child",
    "adj_child",
    "final_child",
    "final_family",
    "final_sponsor",
    "match_report",
    "dedup_log",
    "dob_conflict_log",
)

#: Ownership matrix (§9): module -> sheets it may write.
WRITABLE_SHEETS: dict[str, frozenset[str]] = {
    "acquire": frozenset(
        {
            "raw_sponsor",
            "raw_family",
            "raw_child",
            "dedup_log",
            "dob_conflict_log",
        }
    ),
    "screen": frozenset({"adj_child"}),
    "audit": frozenset({"final_child"}),
    "match": frozenset({"final_family", "final_sponsor", "match_report"}),
    "generate": frozenset({"final_child"}),
    "stats": frozenset({"final_child", "final_family", "final_sponsor"}),
}

#: Read access matrix (§9); listed for documentation, all modules may read.
READABLE_SHEETS: dict[str, frozenset[str]] = {
    "acquire": frozenset(),
    "screen": frozenset({"raw_child", "adj_child", "final_child"}),
    "audit": frozenset({"raw_child", "adj_child", "final_child"}),
    "match": frozenset({"raw_family", "raw_sponsor", "final_child"}),
    "generate": frozenset({"final_child", "final_family", "final_sponsor"}),
}


#: Sheet whose ``age_at_xmas`` column is stored as live formulas (§4).
FINAL_CHILD_SHEET = "final_child"

#: final_child columns driving the age formula.
DOB_COLUMN = "dob"
AGE_COLUMN = "age_at_xmas"

#: OOXML namespaces of workbook.xml sheets and their relationships.
_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


class SheetOwnershipError(Exception):
    """Raised when a module tries to write a sheet it does not own."""


class SheetFormulaError(Exception):
    """Raised when the final_child age formulas cannot be written."""


def setup_logging(log_file: str | Path) -> logging.Logger:
    """Configure the pipeline logger writing to the config log file.

    Also mirrors log records to stderr so CLI runs show progress.  Safe to
    call repeatedly (handlers are replaced, not duplicated).

    Args:
        log_file: Path of the plain-text log file.

    Returns:
        The ``ccat`` logger.
    """
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("ccat")
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    file_handler = logging.FileHandler(log_path)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)
    return root


def list_sheets(path: str | Path) -> list[str]:
    """List sheet names present in the workbook.

    Args:
        path: Workbook path.

    Returns:
        Sheet names; empty list when the file does not exist.
    """
    path = Path(path)
    if not path.is_file():
        return []
    workbook = load_workbook(path, read_only=True)
    names = list(workbook.sheetnames)
    workbook.close()
    return names


def read_sheet(path: str | Path, sheet: str) -> pl.DataFrame | None:
    """Read one sheet from the workbook as a polars DataFrame.

    Columns that are entirely null come back with a String dtype instead of
    polars' Null dtype.

    Args:
        path: Workbook path.
        sheet: Sheet name.

    Returns:
        The sheet contents, or None when the workbook or sheet is missing.
    """
    path = Path(path)
    if not path.is_file() or sheet not in list_sheets(path):
        return None
    frame = pl.read_excel(path, sheet_name=sheet)
    null_casts = [
        pl.col(name).cast(pl.String)
        for name, dtype in frame.schema.items()
        if dtype == pl.Null
    ]
    if null_casts:
        frame = frame.with_columns(null_casts)
    return frame


def read_sheet_or_empty(
    path: str | Path, sheet: str, schema: dict[str, PolarsDataType]
) -> pl.DataFrame:
    """Read one sheet, returning an empty typed frame when absent.

    Args:
        path: Workbook path.
        sheet: Sheet name.
        schema: Column schema for the empty fallback frame.

    Returns:
        The sheet contents, or an empty frame with the given schema.
    """
    frame = read_sheet(path, sheet)
    if frame is None:
        return pl.DataFrame(schema=schema)
    return frame


def snapshot_sheets(
    path: str | Path, sheets: list[str]
) -> dict[str, list[tuple]]:
    """Capture raw cell values of sheets for byte-identical comparison.

    Args:
        path: Workbook path.
        sheets: Sheet names to snapshot.

    Returns:
        Mapping of sheet name to list of row-value tuples.
    """
    path = Path(path)
    snapshot: dict[str, list[tuple]] = {}
    if not path.is_file():
        return {name: [] for name in sheets}
    workbook = load_workbook(path, read_only=True)
    try:
        for name in sheets:
            rows: list[tuple] = []
            worksheet = workbook[name] if name in workbook.sheetnames else None
            if worksheet is not None:
                for row in worksheet.iter_rows(values_only=True):
                    rows.append(tuple(row))
            snapshot[name] = rows
    finally:
        workbook.close()
    return snapshot


def sheets_unchanged(
    before: dict[str, list[tuple]], after: dict[str, list[tuple]]
) -> list[str]:
    """Compare two snapshots and report changed sheet names.

    Args:
        before: Snapshot taken before a run.
        after: Snapshot taken after a run.

    Returns:
        Names of sheets whose contents differ.
    """
    changed = []
    for name in before:
        if before.get(name) != after.get(name):
            changed.append(name)
    return changed


def _df_rows(frame: pl.DataFrame) -> list[list[object]]:
    """Convert a polars DataFrame into plain python rows for openpyxl."""
    rows: list[list[object]] = []
    for row in frame.iter_rows():
        rows.append([value if value is not None else None for value in row])
    return rows


def write_sheets(
    path: str | Path,
    module: str,
    sheets: dict[str, pl.DataFrame],
    renames: dict[str, str] | None = None,
    xmas: date | None = None,
) -> None:
    """Write sheets into the workbook as one atomic operation.

    Enforces the ownership matrix (§9): ``module`` may only replace sheets
    in ``WRITABLE_SHEETS[module]``.  The whole workbook is saved to a temp
    file in the same directory and then moved into place, so an interrupted
    run never leaves a half-written workbook (§11).

    When ``final_child`` is among the sheets, ``xmas`` must be the
    configured Christmas date: every row's ``age_at_xmas`` is recomputed
    from its ``dob`` and stored as a ``=DATEDIF`` formula over the row's
    ``dob`` cell, with the age embedded as the cached value, so the
    column stays a live spreadsheet formula across pipeline runs (§4).

    Args:
        path: Workbook path; created when missing.
        module: Pipeline module name ("acquire"/"screen"/"audit"/"match").
        sheets: Mapping of sheet name to the DataFrame that replaces it.
        renames: Optional mapping of existing sheet name to a new name
            (used by module 4 to archive previous matchings, §8.6),
            applied before the new sheets are written.
        xmas: The configured Christmas date; required whenever
            ``final_child`` is written (pass ``cfg.xmas``).

    Raises:
        SheetOwnershipError: If the module may not write one of the sheets.
        SheetFormulaError: If ``final_child`` is written without ``xmas``
            or its age formulas cannot be embedded in the saved file.
    """
    if module not in WRITABLE_SHEETS:
        raise SheetOwnershipError(f"unknown module {module!r}")
    owned = WRITABLE_SHEETS[module]
    for name in sheets:
        if name not in owned:
            raise SheetOwnershipError(
                f"module {module!r} may not write sheet {name!r} (§9 ownership"
                f" matrix); owned sheets: {sorted(owned)}"
            )
    path = Path(path)
    preserved_caches: dict[str, str] = {}
    if path.is_file():
        # A rewrite that leaves final_child untouched still re-saves the
        # whole workbook through openpyxl, which drops the age formulas'
        # cached results; capture them now so they can be restored.
        if FINAL_CHILD_SHEET not in sheets:
            preserved_caches = _final_child_formula_caches(path)
        workbook = load_workbook(path)
    else:
        workbook = Workbook()
        default = workbook.active
        if default is not None and default.title == "Sheet":
            workbook.remove(default)
    formulas: dict[str, str] = {}
    try:
        for old_name, new_name in (renames or {}).items():
            if old_name not in workbook.sheetnames:
                continue
            if new_name in workbook.sheetnames:
                raise SheetOwnershipError(
                    f"cannot rename {old_name!r} to existing sheet {new_name!r}"
                )
            workbook[old_name].title = new_name
        for name, frame in sheets.items():
            if name in workbook.sheetnames:
                del workbook[name]
            worksheet = workbook.create_sheet(name)
            worksheet.append(list(frame.columns))
            for row in _df_rows(frame):
                worksheet.append(row)
            if name == FINAL_CHILD_SHEET:
                formulas = _final_child_age_formulas(worksheet, frame, xmas)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.parent / f".{path.name}.tmp"
        workbook.save(tmp_path)
        if formulas:
            _inject_age_formulas(tmp_path, formulas)
        elif preserved_caches:
            _restore_age_caches(tmp_path, preserved_caches)
        os.replace(tmp_path, path)
    finally:
        workbook.close()


def _iso_dob(value: object) -> str | None:
    """Normalize a dob cell value to an ISO date string.

    Args:
        value: Raw cell value (text, date/datetime, or None).

    Returns:
        The ISO ``YYYY-MM-DD`` string, or None when the value is empty.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _final_child_age_formulas(
    worksheet: Worksheet,
    frame: pl.DataFrame,
    xmas: date | None,
) -> dict[str, str]:
    """Recompute final_child ages and collect DATEDIF formulas (§4).

    Every row whose dob holds a parsable ISO date gets its
    ``age_at_xmas`` cell set to the age computed from ``xmas`` -- the
    cached result, so cached ages never go stale when a dob is
    adjudicated -- and contributes one ``DATEDIF`` formula referencing
    the row's dob cell.  Rows with a missing or unparsable dob keep the
    value the frame was written with and contribute no formula.

    Args:
        worksheet: The freshly written final_child worksheet.
        frame: The final_child DataFrame behind the worksheet.
        xmas: The configured Christmas date.

    Returns:
        Mapping of age cell reference (e.g. ``"AB2"``) to formula text
        without the leading ``=``.

    Raises:
        SheetFormulaError: If ``xmas`` is None.
    """
    if xmas is None:
        raise SheetFormulaError(
            f"{FINAL_CHILD_SHEET} may not be written without xmas (the"
            " configured Christmas date): pass xmas=cfg.xmas so"
            f" {AGE_COLUMN} stays a live DATEDIF formula (§4)"
        )
    # Imported lazily: ccat.acquire imports this module at load time.
    from ccat.acquire import compute_age_at_xmas

    columns = list(frame.columns)
    missing = [
        column for column in (DOB_COLUMN, AGE_COLUMN) if column not in columns
    ]
    if missing:
        logger.warning(
            "%s is missing the %s column(s); %s written as plain values",
            FINAL_CHILD_SHEET,
            "/".join(missing),
            AGE_COLUMN,
        )
        return {}
    dob_pos = columns.index(DOB_COLUMN)
    age_pos = columns.index(AGE_COLUMN)
    dob_letter = get_column_letter(dob_pos + 1)
    age_letter = get_column_letter(age_pos + 1)
    formulas: dict[str, str] = {}
    for row_no, values in enumerate(frame.iter_rows(), start=2):
        dob = _iso_dob(values[dob_pos])
        if dob is None:
            logger.warning(
                "%s row %d has no dob; %s keeps its written value",
                FINAL_CHILD_SHEET,
                row_no,
                AGE_COLUMN,
            )
            continue
        try:
            age = compute_age_at_xmas(dob, xmas)
        except ValueError:
            logger.warning(
                "%s row %d has an unparsable dob %r; %s keeps its"
                " written value",
                FINAL_CHILD_SHEET,
                row_no,
                dob,
                AGE_COLUMN,
            )
            continue
        worksheet.cell(row=row_no, column=age_pos + 1).value = age
        formulas[f"{age_letter}{row_no}"] = (
            f"DATEDIF({dob_letter}{row_no},"
            f' DATE({xmas.year},{xmas.month},{xmas.day}), "y")'
        )
    return formulas


def _worksheet_xml_path(archive: zipfile.ZipFile, sheet_name: str) -> str:
    """Resolve one sheet's worksheet XML member in a saved workbook.

    Args:
        archive: An open workbook archive.
        sheet_name: The sheet to locate.

    Returns:
        The archive member path, e.g. ``xl/worksheets/sheet3.xml``.

    Raises:
        SheetFormulaError: If the workbook structure is unreadable or
            the sheet has no relationship target.
    """
    try:
        workbook_root = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        rels_root = ElementTree.fromstring(
            archive.read("xl/_rels/workbook.xml.rels")
        )
    except (KeyError, ElementTree.ParseError) as error:
        raise SheetFormulaError(
            f"could not parse the workbook structure: {error}"
        ) from error
    targets = {
        relation.get("Id"): relation.get("Target") for relation in rels_root
    }
    for sheet in workbook_root.iter(f"{{{_NS_MAIN}}}sheet"):
        if sheet.get("name") != sheet_name:
            continue
        target = targets.get(sheet.get(f"{{{_NS_REL}}}id"))
        if target is None:
            raise SheetFormulaError(
                f"no relationship target for sheet {sheet_name!r}"
            )
        target = target.lstrip("/")
        if not target.startswith("xl/"):
            target = f"xl/{target}"
        return target
    raise SheetFormulaError(f"sheet {sheet_name!r} not found in the archive")


def _inject_age_formulas(archive_path: Path, formulas: dict[str, str]) -> None:
    """Embed age formulas beside their cached values in a saved workbook.

    openpyxl cannot store a formula and its cached result in one cell,
    so the age cells were saved as plain numbers; this helper rewrites
    the saved ``final_child`` sheet XML, inserting an ``<f>`` element in
    front of each age cell's ``<v>``.  Spreadsheet apps see a live
    formula with a correct cached result, and fastexcel (see
    :func:`read_sheet`) keeps reading the cached number.

    Args:
        archive_path: The just-saved temp workbook file.
        formulas: Mapping of cell reference to formula text without the
            leading ``=``.

    Raises:
        SheetFormulaError: If the archive cannot be read or rewritten, or
            a formula cannot be embedded in its cell.
    """
    entries, sheet_xml, sheet_text = _read_final_child_xml(archive_path)
    for ref, formula in formulas.items():
        match = re.search(rf'<c r="{ref}"([^>]*)><v>', sheet_text)
        if match is None:
            raise SheetFormulaError(
                f"could not embed the {AGE_COLUMN} formula in cell {ref}"
                f" of {FINAL_CHILD_SHEET}"
            )
        attrs = match.group(1).replace(' t="n"', "")
        replacement = f'<c r="{ref}"{attrs}><f>{escape(formula)}</f><v>'
        sheet_text = (
            sheet_text[: match.start()]
            + replacement
            + sheet_text[match.end() :]
        )
    _write_patched_archive(archive_path, entries, sheet_xml, sheet_text)


def _final_child_formula_caches(path: Path) -> dict[str, str]:
    """Collect the cached values of final_child formula cells.

    Read from the workbook on disk before it is re-saved, so the caches
    can be restored afterwards (openpyxl keeps formulas through a
    load/save cycle but drops their cached results).  Shared-formula
    cells written by LibreOffice carry self-closed ``<f/>`` elements and
    are matched too.

    Args:
        path: Workbook path on disk.

    Returns:
        Mapping of cell reference to cached value text; empty when the
        file, the sheet, or its cached values are missing.
    """
    if not path.is_file():
        return {}
    try:
        with zipfile.ZipFile(path) as archive:
            member = _worksheet_xml_path(archive, FINAL_CHILD_SHEET)
            text = archive.read(member).decode("utf-8")
    except (SheetFormulaError, OSError, zipfile.BadZipFile, KeyError):
        return {}
    return dict(
        re.findall(
            r'<c r="([A-Z]+\d+)"[^>]*>(?:<f[^>]*>[^<]*</f>|<f[^>]*/>)'
            r"<v>([^<]+)</v>",
            text,
        )
    )


def _restore_age_caches(archive_path: Path, cached: dict[str, str]) -> None:
    """Re-embed cached values for final_child formulas after a re-save.

    A write that does not include ``final_child`` still re-saves the
    whole workbook through openpyxl, which replaces the age formulas'
    cached results with empty ``<v/>`` elements; this helper puts the
    values captured beforehand (see :func:`_final_child_formula_caches`)
    back, so fastexcel reads keep seeing numbers.

    Args:
        archive_path: The just-saved temp workbook file.
        cached: Mapping of cell reference to cached value text.

    Raises:
        SheetFormulaError: If the archive cannot be read or rewritten.
    """
    entries, sheet_xml, sheet_text = _read_final_child_xml(archive_path)
    restored: list[str] = []
    orphans: list[str] = []
    for ref in re.findall(
        r'<c r="([A-Z]+\d+)"[^>]*><f[^>]*>[^<]*</f>(?:<v></v>|<v/>)?</c>',
        sheet_text,
    ):
        value = cached.get(ref)
        if value is None:
            orphans.append(ref)
            continue
        match = re.search(
            rf'<c r="{ref}"[^>]*><f[^>]*>[^<]*</f>(?:<v></v>|<v/>)?</c>',
            sheet_text,
        )
        if match is None:  # pragma: no cover - ref came from this text
            continue
        cell = match.group(0)
        stripped = cell.replace("<v></v>", "").replace("<v/>", "")
        filled = f"{stripped[:-4]}<v>{value}</v></c>"
        sheet_text = sheet_text.replace(cell, filled, 1)
        restored.append(ref)
    if restored:
        _write_patched_archive(archive_path, entries, sheet_xml, sheet_text)
    if orphans:
        logger.warning(
            "%d final_child formula cell(s) have no cached value to"
            " restore (e.g. %s); spreadsheet apps recalculate them on"
            " open, but pipeline reads see nulls until then",
            len(orphans),
            ", ".join(orphans[:5]),
        )


def _read_final_child_xml(
    archive_path: Path,
) -> tuple[list[tuple[zipfile.ZipInfo, bytes]], str, str]:
    """Load a saved workbook's entries and its final_child sheet XML.

    Args:
        archive_path: Saved workbook file.

    Returns:
        A tuple of (archive entries as (info, data) pairs, the
        final_child member path, the member's XML text).

    Raises:
        SheetFormulaError: If the archive cannot be read or the
            final_child member cannot be located.
    """
    try:
        with zipfile.ZipFile(archive_path) as archive:
            sheet_xml = _worksheet_xml_path(archive, FINAL_CHILD_SHEET)
            entries = [
                (item, archive.read(item.filename))
                for item in archive.infolist()
            ]
    except (OSError, zipfile.BadZipFile, KeyError) as error:
        raise SheetFormulaError(
            f"could not read {archive_path}: {error}"
        ) from error
    try:
        sheet_text = next(
            data for item, data in entries if item.filename == sheet_xml
        ).decode("utf-8")
    except StopIteration as error:  # pragma: no cover - same archive
        raise SheetFormulaError(
            f"{sheet_xml} not found in {archive_path}"
        ) from error
    return entries, sheet_xml, sheet_text


def _write_patched_archive(
    archive_path: Path,
    entries: list[tuple[zipfile.ZipInfo, bytes]],
    sheet_xml: str,
    sheet_text: str,
) -> None:
    """Rewrite a saved workbook with a patched final_child member.

    Args:
        archive_path: Saved workbook file, replaced atomically.
        entries: The original archive entries.
        sheet_xml: The final_child member path.
        sheet_text: The patched member XML text.

    Raises:
        SheetFormulaError: If the archive cannot be rewritten.
    """
    patched_path = archive_path.parent / f"{archive_path.name}.patched"
    try:
        with zipfile.ZipFile(
            patched_path, "w", zipfile.ZIP_DEFLATED
        ) as patched:
            for item, data in entries:
                patched.writestr(
                    item,
                    sheet_text.encode("utf-8")
                    if item.filename == sheet_xml
                    else data,
                )
        os.replace(patched_path, archive_path)
    except OSError as error:
        raise SheetFormulaError(
            f"could not rewrite {archive_path}: {error}"
        ) from error
