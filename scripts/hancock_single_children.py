"""List single-child families in ``final_child`` with Hancock gift pickup.

Reads ``final_child`` and ``final_family`` from the CCAT Excel workbook,
filters to families with exactly one child whose ``gift_pickup_location``
is "Hancock" (case-insensitive), and prints the resulting table.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

DEFAULT_EXCEL_FILE = Path("output/ccat_2026.xlsx")
PICKUP_LOCATION = "hancock"


def build_table(excel_file: Path) -> pl.DataFrame:
    """Build the single-child Hancock table from the workbook.

    Args:
        excel_file: Path to the CCAT Excel workbook.

    Returns:
        A DataFrame with one row per single-child Hancock family.

    Raises:
        FileNotFoundError: If ``excel_file`` does not exist.
        Exception: If the required sheets are missing or empty.
    """
    if not excel_file.exists():
        raise FileNotFoundError(f"Excel file not found: {excel_file}")

    children = pl.read_excel(excel_file, sheet_name="final_child")
    if children.is_empty():
        raise ValueError("final_child sheet is missing or empty")

    families = pl.read_excel(excel_file, sheet_name="final_family").select(
        "family_id", "gift_pickup_location"
    )
    if families.is_empty():
        raise ValueError("final_family sheet is missing or empty")

    kids_per_family = children.group_by("family_id").agg(
        pl.len().alias("n_children")
    )

    return (
        kids_per_family.join(families, on="family_id", how="left")
        .filter(
            (pl.col("n_children") == 1)
            & (
                pl.col("gift_pickup_location").str.to_lowercase()
                == PICKUP_LOCATION
            )
        )
        .join(
            children.select(
                "family_id",
                "angel_id",
                "first_name",
                "age_at_xmas",
                "gender",
                "audited",
                "require_clarification",
            ),
            on="family_id",
            how="left",
        )
        .with_columns(
            pl.col("require_clarification").str.strip_chars().fill_null("")
        )
        .select(
            "family_id",
            "angel_id",
            "first_name",
            "age_at_xmas",
            "gender",
            "audited",
            "require_clarification",
        )
        .sort("family_id")
    )


def main() -> int:
    """Run the script and print the table to stdout.

    Returns:
        Process exit code: 0 on success, 1 on failure.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Table of single-child families in final_child with "
            "gift_pickup_location 'hancock'."
        )
    )
    parser.add_argument(
        "excel_file",
        nargs="?",
        type=Path,
        default=DEFAULT_EXCEL_FILE,
        help=f"Path to the CCAT Excel workbook (default: {DEFAULT_EXCEL_FILE})",
    )
    args = parser.parse_args()

    try:
        table = build_table(args.excel_file)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    pl.Config.set_tbl_rows(100)
    pl.Config.set_tbl_width_chars(200)
    pl.Config.set_fmt_str_lengths(60)
    print(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
