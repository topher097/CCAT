"""Tests for final_child age-formula persistence in io.write_sheets."""

from typing import Any

import polars as pl
import pytest
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from ccat.config import Config
from ccat.io import SheetFormulaError, read_sheet, write_sheets

FINAL_SCHEMA: dict[str, Any] = {
    "zite_record_id": pl.String,
    "dob": pl.String,
    "age_at_xmas": pl.Int64,
}


def final_frame(rows: list[dict[str, Any]] | None = None) -> pl.DataFrame:
    """Build a small final_child frame (age column may be stale)."""
    if rows is None:
        rows = [
            {"zite_record_id": "c1", "dob": "2018-05-10", "age_at_xmas": 8},
            {"zite_record_id": "c2", "dob": "2020-12-26", "age_at_xmas": 5},
        ]
    return pl.DataFrame(rows, schema=FINAL_SCHEMA)


def age_cells(cfg: Config, sheet: str = "final_child") -> list[str | None]:
    """Read the age column as openpyxl sees it (formulas included)."""
    workbook = load_workbook(cfg.excel_file)
    try:
        worksheet = workbook[sheet]
        header = [
            cell.value
            for cell in next(worksheet.iter_rows(min_row=1, max_row=1))
        ]
        column = get_column_letter(header.index("age_at_xmas") + 1)
        return [
            worksheet[f"{column}{row}"].value
            for row in range(2, worksheet.max_row + 1)
        ]
    finally:
        workbook.close()


def read_ages(cfg: Config, sheet: str = "final_child") -> pl.Series:
    """Read a sheet's age_at_xmas column through the pipeline reader."""
    frame = read_sheet(cfg.excel_file, sheet)
    assert frame is not None
    return frame["age_at_xmas"]


class TestAgeFormulas:
    """write_sheets turns final_child ages into live DATEDIF formulas."""

    def test_ages_become_formulas_with_cached_values(self, cfg: Config) -> None:
        """Every age cell holds a DATEDIF formula over the row's dob."""
        write_sheets(
            cfg.excel_file,
            "audit",
            {"final_child": final_frame()},
            xmas=cfg.xmas,
        )
        assert age_cells(cfg) == [
            '=DATEDIF(B2, DATE(2026,12,25), "y")',
            '=DATEDIF(B3, DATE(2026,12,25), "y")',
        ]
        assert read_ages(cfg).to_list() == [8, 5]

    def test_stale_age_is_recomputed_from_dob(self, cfg: Config) -> None:
        """A frame age that disagrees with dob is corrected on write."""
        write_sheets(
            cfg.excel_file,
            "audit",
            {
                "final_child": final_frame(
                    [
                        {
                            "zite_record_id": "c1",
                            "dob": "2012-01-05",
                            "age_at_xmas": 99,
                        }
                    ]
                )
            },
            xmas=cfg.xmas,
        )
        assert read_ages(cfg).to_list() == [14]

    def test_missing_xmas_is_rejected(self, cfg: Config) -> None:
        """Writing final_child without xmas raises; nothing is written."""
        with pytest.raises(SheetFormulaError, match="without xmas"):
            write_sheets(
                cfg.excel_file, "audit", {"final_child": final_frame()}
            )
        assert read_sheet(cfg.excel_file, "final_child") is None

    def test_frame_without_dob_writes_plain_values(self, cfg: Config) -> None:
        """A final_child frame lacking dob keeps plain age values."""
        frame = pl.DataFrame(
            {"zite_record_id": ["c1"], "age_at_xmas": [8]},
            schema={"zite_record_id": pl.String, "age_at_xmas": pl.Int64},
        )
        write_sheets(
            cfg.excel_file, "audit", {"final_child": frame}, xmas=cfg.xmas
        )
        assert age_cells(cfg) == [8]
        assert read_ages(cfg).to_list() == [8]

    def test_unparsable_dob_keeps_written_age(self, cfg: Config) -> None:
        """Rows whose dob is missing or unparsable keep their value."""
        write_sheets(
            cfg.excel_file,
            "audit",
            {
                "final_child": final_frame(
                    [
                        {
                            "zite_record_id": "c1",
                            "dob": "not-a-date",
                            "age_at_xmas": 7,
                        },
                        {"zite_record_id": "c2", "dob": None, "age_at_xmas": 6},
                        {
                            "zite_record_id": "c3",
                            "dob": "2018-05-10",
                            "age_at_xmas": 8,
                        },
                    ]
                )
            },
            xmas=cfg.xmas,
        )
        # only the parsable row carries a formula
        assert age_cells(cfg) == [
            7,
            6,
            '=DATEDIF(B4, DATE(2026,12,25), "y")',
        ]
        assert read_ages(cfg).to_list() == [7, 6, 8]

    def test_other_sheets_never_get_formulas(self, cfg: Config) -> None:
        """adj_child carries dob/age columns but stays plain values."""
        write_sheets(cfg.excel_file, "screen", {"adj_child": final_frame()})
        workbook = load_workbook(cfg.excel_file)
        try:
            assert workbook["adj_child"]["C2"].value == 8
        finally:
            workbook.close()
        assert read_ages(cfg, "adj_child").to_list() == [8, 5]


class TestFormulaPersistence:
    """Rewrites that skip final_child keep its formulas and caches."""

    def test_other_module_rewrite_keeps_cached_ages(self, cfg: Config) -> None:
        """A match write leaves final_child ages readable and live."""
        write_sheets(
            cfg.excel_file,
            "audit",
            {"final_child": final_frame()},
            xmas=cfg.xmas,
        )
        write_sheets(
            cfg.excel_file,
            "match",
            {
                "final_family": pl.DataFrame(
                    {"family_id": [1], "assigned_sponsor_id": [None]},
                    schema={
                        "family_id": pl.Int64,
                        "assigned_sponsor_id": pl.Int64,
                    },
                )
            },
        )
        ages = read_ages(cfg)
        assert ages.dtype == pl.Int64
        assert ages.to_list() == [8, 5]
        assert age_cells(cfg) == [
            '=DATEDIF(B2, DATE(2026,12,25), "y")',
            '=DATEDIF(B3, DATE(2026,12,25), "y")',
        ]

    def test_caches_survive_repeated_rewrites(self, cfg: Config) -> None:
        """Multiple non-final_child writes keep restoring the caches."""
        write_sheets(
            cfg.excel_file,
            "audit",
            {"final_child": final_frame()},
            xmas=cfg.xmas,
        )
        for module, sheet in (
            ("match", "final_family"),
            ("screen", "adj_child"),
            ("match", "final_sponsor"),
        ):
            write_sheets(cfg.excel_file, module, {sheet: pl.DataFrame()})
        assert read_ages(cfg).to_list() == [8, 5]

    def test_rewritten_final_child_refreshes_formulas(
        self, cfg: Config
    ) -> None:
        """A second audit write re-derives ages and formulas from dob."""
        write_sheets(
            cfg.excel_file,
            "audit",
            {"final_child": final_frame()},
            xmas=cfg.xmas,
        )
        frame = final_frame(
            [
                {"zite_record_id": "c1", "dob": "2012-01-05", "age_at_xmas": 8},
            ]
        )
        write_sheets(
            cfg.excel_file, "audit", {"final_child": frame}, xmas=cfg.xmas
        )
        assert age_cells(cfg) == ['=DATEDIF(B2, DATE(2026,12,25), "y")']
        assert read_ages(cfg).to_list() == [14]
