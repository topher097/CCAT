"""Rewrite ``final_child`` ``age_at_xmas`` as live spreadsheet formulas.

Thin wrapper over ``ccat.io.write_sheets``: rewrites the ``final_child``
sheet so every ``age_at_xmas`` cell holds ``=DATEDIF(<dob cell>,
DATE(Y,M,D), "y")`` with the age re-derived from the row's ``dob`` and the
config's ``xmas_date`` embedded as the cached value (§4).  Every other
cell of every sheet is verified unchanged before the workbook is replaced.

Retrofits workbooks written before the pipeline did this automatically;
ordinary pipeline runs keep the formulas on their own, so this script is
only needed for old or hand-edited workbooks.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import tempfile
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.exceptions import InvalidFileException

from ccat.acquire import compute_age_at_xmas
from ccat.config import ConfigError, load_config
from ccat.io import (
    AGE_COLUMN,
    DOB_COLUMN,
    FINAL_CHILD_SHEET,
    read_sheet,
    write_sheets,
)

if TYPE_CHECKING:
    from typing import Any

    from ccat.config import Config

logger = logging.getLogger("final_child_age_formula")

#: Default config path, relative to the repository root.
DEFAULT_CONFIG = "config/ccat_2026.json"


class FormulaError(Exception):
    """Raised when the formula rewrite cannot complete safely."""


def _dob_text(value: Any) -> str | None:
    """Normalize a dob cell value to an ISO date string.

    Args:
        value: Raw cell value (text, date/datetime, or None).

    Returns:
        The ISO ``YYYY-MM-DD`` string, or None for empty cells.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _normalize_cell(value: Any) -> Any:
    """Fold LibreOffice's ``=TRUE()``/``=FALSE()`` formulas to plain bools.

    LibreOffice exports boolean cells as tiny formulas with a cached
    boolean; a workbook round-tripped through openpyxl stores plain
    booleans instead.  The two forms are equivalent for comparison.

    Args:
        value: Cell value as openpyxl reports it.

    Returns:
        The value with ``=TRUE()``/``=FALSE()`` mapped to True/False.
    """
    if isinstance(value, str):
        upper = value.strip().upper()
        if upper == "=TRUE()":
            return True
        if upper == "=FALSE()":
            return False
    return value


def _cells_equal(old: Any, new: Any) -> bool:
    """Compare two cell values, tolerating benign representation drift.

    Args:
        old: Snapshot cell value.
        new: Rewritten cell value.

    Returns:
        True when the values are equivalent.
    """
    old = _normalize_cell(old)
    new = _normalize_cell(new)
    if isinstance(old, bool) or isinstance(new, bool):
        return old is new
    if isinstance(old, int | float) and isinstance(new, int | float):
        return old == new
    if type(old) is not type(new):
        return False
    return old == new


def snapshot_workbook(path: Path) -> dict[str, list[list[Any]]]:
    """Capture every cell value of every sheet, formulas as their text.

    Args:
        path: Workbook path.

    Returns:
        Mapping of sheet name (in sheet order) to rows of cell values.

    Raises:
        FormulaError: If the workbook cannot be opened.
    """
    try:
        workbook = load_workbook(path, read_only=True)
    except (InvalidFileException, zipfile.BadZipFile, OSError) as error:
        raise FormulaError(f"could not open {path}: {error}") from error
    try:
        return {
            name: [
                list(row) for row in workbook[name].iter_rows(values_only=True)
            ]
            for name in workbook.sheetnames
        }
    finally:
        workbook.close()


def verify_result(
    before: dict[str, list[list[Any]]], path: Path, xmas: date
) -> None:
    """Verify the rewritten workbook against the pre-change snapshot.

    Args:
        before: Snapshot of the original workbook.
        path: Rewritten workbook path.
        xmas: The configured Christmas date.

    Raises:
        FormulaError: If any unrelated cell changed, a dob row lost its
            formula, or a cached age disagrees with ``compute_age_at_xmas``.
    """
    try:
        formulas = load_workbook(path, read_only=True)
        values = load_workbook(path, read_only=True, data_only=True)
    except (InvalidFileException, zipfile.BadZipFile, OSError) as error:
        raise FormulaError(f"could not reload {path}: {error}") from error
    try:
        if formulas.sheetnames != list(before):
            raise FormulaError(
                f"sheet names/order changed: {list(before)}"
                f" -> {formulas.sheetnames}"
            )
        for name in formulas.sheetnames:
            sheet = formulas[name]
            header = next(
                sheet.iter_rows(min_row=1, max_row=1, values_only=True)
            )
            age_index = (
                header.index(AGE_COLUMN)
                if name == FINAL_CHILD_SHEET and AGE_COLUMN in header
                else None
            )
            new_rows = [list(row) for row in sheet.iter_rows(values_only=True)]
            old_rows = before[name]
            if len(new_rows) != len(old_rows):
                raise FormulaError(
                    f"sheet {name}: row count {len(old_rows)}"
                    f" -> {len(new_rows)}"
                )
            for row_no, (old_row, new_row) in enumerate(
                zip(old_rows, new_rows, strict=False), start=1
            ):
                width = max(len(old_row), len(new_row))
                old_row += [None] * (width - len(old_row))
                new_row += [None] * (width - len(new_row))
                for col_no, (old, new) in enumerate(
                    zip(old_row, new_row, strict=False)
                ):
                    if age_index is not None and col_no == age_index:
                        continue
                    if not _cells_equal(old, new):
                        raise FormulaError(
                            f"{name}!"
                            f"{get_column_letter(col_no + 1)}{row_no}:"
                            f" {old!r} -> {new!r}"
                        )
        _verify_formulas_and_ages(
            formulas[FINAL_CHILD_SHEET], values[FINAL_CHILD_SHEET], xmas
        )
    finally:
        formulas.close()
        values.close()


def _verify_formulas_and_ages(
    formulas_sheet: Any, values_sheet: Any, xmas: date
) -> None:
    """Check that every dob row kept a formula with the right cached age.

    Args:
        formulas_sheet: The rewritten ``final_child`` sheet with formulas
            visible.
        values_sheet: The same sheet loaded with cached values
            (``data_only=True``).
        xmas: The configured Christmas date.

    Raises:
        FormulaError: On a missing formula, an unreadable dob, or a
            cached age that disagrees with ``compute_age_at_xmas``.
    """
    header = list(
        next(values_sheet.iter_rows(min_row=1, max_row=1, values_only=True))
    )
    if DOB_COLUMN not in header or AGE_COLUMN not in header:
        raise FormulaError(
            f"{FINAL_CHILD_SHEET} is missing the {DOB_COLUMN} or"
            f" {AGE_COLUMN} column"
        )
    dob_index = header.index(DOB_COLUMN)
    age_index = header.index(AGE_COLUMN)
    dob_letter = get_column_letter(dob_index + 1)
    for row_no, values_row in enumerate(
        values_sheet.iter_rows(min_row=2, values_only=True), start=2
    ):
        values_row = list(values_row)
        values_row += [None] * (len(header) - len(values_row))
        dob = _dob_text(values_row[dob_index])
        if dob is None:
            continue
        try:
            expected = compute_age_at_xmas(dob, xmas)
        except ValueError as error:
            raise FormulaError(
                f"{FINAL_CHILD_SHEET} row {row_no}: unreadable dob"
                f" {dob!r} ({error})"
            ) from error
        cached = values_row[age_index]
        if (
            isinstance(cached, bool)
            or not isinstance(cached, int | float)
            or cached != expected
        ):
            raise FormulaError(
                f"{FINAL_CHILD_SHEET} row {row_no}: cached age"
                f" {cached!r} != expected {expected} (dob {dob})"
            )
        formula_row = list(
            next(
                formulas_sheet.iter_rows(
                    min_row=row_no, max_row=row_no, values_only=True
                )
            )
        )
        formula_row += [None] * (len(header) - len(formula_row))
        formula = formula_row[age_index]
        wanted = (
            f"=DATEDIF({dob_letter}{row_no},"
            f' DATE({xmas.year},{xmas.month},{xmas.day}), "y")'
        )
        if formula != wanted:
            raise FormulaError(
                f"{FINAL_CHILD_SHEET} row {row_no}: age cell is"
                f" {formula!r}, expected {wanted!r}"
            )


def _apply(target: Path, cfg: Config) -> None:
    """Run the rewrite on one workbook path, in place.

    Args:
        target: Workbook copy to rewrite.
        cfg: Pipeline configuration (provides the Christmas date).

    Raises:
        FormulaError: If any step fails.
    """
    frame = read_sheet(target, FINAL_CHILD_SHEET)
    if frame is None:
        raise FormulaError(f"{target} has no {FINAL_CHILD_SHEET} sheet")
    old_ages = (
        frame[AGE_COLUMN].to_list() if AGE_COLUMN in frame.columns else []
    )
    before = snapshot_workbook(target)
    write_sheets(target, "audit", {FINAL_CHILD_SHEET: frame}, xmas=cfg.xmas)
    verify_result(before, target, cfg.xmas)
    new_frame = read_sheet(target, FINAL_CHILD_SHEET)
    assert new_frame is not None
    changed = sum(
        1
        for old, new in zip(old_ages, new_frame[AGE_COLUMN], strict=False)
        if old != new
    )
    logger.info(
        "%s: %d %s formulas verified%s",
        target,
        new_frame.height,
        AGE_COLUMN,
        f" ({changed} age(s) corrected from dob)" if changed else "",
    )


def run(config_path: Path, excel_file: str | None, dry_run: bool) -> None:
    """Apply the formula rewrite to the configured workbook.

    Args:
        config_path: Pipeline config path; ``xmas_date`` and the default
            workbook location come from it.
        excel_file: Workbook path override; ``None`` uses the config's
            ``excel_file``.
        dry_run: Run everything on a temporary copy and leave the real
            workbook untouched.

    Raises:
        FormulaError: If the config or workbook is unusable, the
            workbook is open in LibreOffice, or any step fails.
    """
    try:
        cfg = load_config(config_path)
    except ConfigError as error:
        raise FormulaError(
            f"could not load config {config_path}: {error}"
        ) from error
    workbook = Path(excel_file) if excel_file else Path(cfg.excel_file)
    if not workbook.is_file():
        raise FormulaError(f"workbook not found: {workbook}")
    lock = workbook.parent / f".~lock.{workbook.name}#"
    if lock.exists():
        if dry_run:
            logger.warning(
                "%s is open in LibreOffice; the dry run reads it as-is"
                " (close it before the real run)",
                workbook,
            )
        else:
            raise FormulaError(
                f"{workbook} is open in LibreOffice ({lock.name});"
                f" close it first"
            )
    if dry_run:
        with tempfile.TemporaryDirectory(prefix="ccat-age-dry-") as tmp:
            copy = Path(tmp) / workbook.name
            try:
                shutil.copyfile(workbook, copy)
            except OSError as error:
                raise FormulaError(
                    f"could not copy {workbook}: {error}"
                ) from error
            _apply(copy, cfg)
            logger.info("dry run succeeded on a copy; %s untouched", workbook)
        return
    _apply(workbook, cfg)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the rewrite.

    Args:
        argv: Command-line arguments (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code: 0 on success, 1 on any failure.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Rewrite final_child age_at_xmas as DATEDIF formulas that"
            " update when dob changes."
        )
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help="pipeline config file (default: %(default)s)",
    )
    parser.add_argument(
        "--excel-file",
        default=None,
        help="workbook to edit (default: excel_file from the config)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="verify the whole rewrite on a temporary copy; edit nothing",
    )
    arguments = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        run(Path(arguments.config), arguments.excel_file, arguments.dry_run)
    except FormulaError as error:
        logger.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
