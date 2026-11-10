"""Excel workbook and logging helpers.

Thin polars/openpyxl-based read/write helpers over the single pipeline
workbook (§4).  Sheet writes are atomic (write a temp file, then replace)
and the module enforces the ownership matrix from §9: a module may only
write sheets it owns.
"""

import logging
import os
import sys
from pathlib import Path

import polars as pl
from openpyxl import Workbook, load_workbook
from polars._typing import PolarsDataType


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
)

#: Ownership matrix (§9): module -> sheets it may write.
WRITABLE_SHEETS: dict[str, frozenset[str]] = {
    "acquire": frozenset({"raw_sponsor", "raw_family", "raw_child"}),
    "screen": frozenset({"adj_child"}),
    "audit": frozenset({"final_child"}),
    "match": frozenset({"final_family", "final_sponsor", "match_report"}),
}

#: Read access matrix (§9); listed for documentation, all modules may read.
READABLE_SHEETS: dict[str, frozenset[str]] = {
    "acquire": frozenset(),
    "screen": frozenset({"raw_child", "adj_child", "final_child"}),
    "audit": frozenset({"raw_child", "adj_child", "final_child"}),
    "match": frozenset({"raw_family", "raw_sponsor", "final_child"}),
}


class SheetOwnershipError(Exception):
    """Raised when a module tries to write a sheet it does not own."""


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
) -> None:
    """Write sheets into the workbook as one atomic operation.

    Enforces the ownership matrix (§9): ``module`` may only replace sheets
    in ``WRITABLE_SHEETS[module]``.  The whole workbook is saved to a temp
    file in the same directory and then moved into place, so an interrupted
    run never leaves a half-written workbook (§11).

    Args:
        path: Workbook path; created when missing.
        module: Pipeline module name ("acquire"/"screen"/"audit"/"match").
        sheets: Mapping of sheet name to the DataFrame that replaces it.
        renames: Optional mapping of existing sheet name to a new name
            (used by module 4 to archive previous matchings, §8.6),
            applied before the new sheets are written.

    Raises:
        SheetOwnershipError: If the module may not write one of the sheets.
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
    if path.is_file():
        workbook = load_workbook(path)
    else:
        workbook = Workbook()
        default = workbook.active
        if default is not None and default.title == "Sheet":
            workbook.remove(default)
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
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.parent / f".{path.name}.tmp"
        workbook.save(tmp_path)
        os.replace(tmp_path, path)
    finally:
        workbook.close()
