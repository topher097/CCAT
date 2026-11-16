"""Tests for module 7 — clarification-table extraction."""

from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from ccat.clarify.extract import (
    CHILD_COLUMNS,
    EMAIL_FILE_COLUMN,
    OUTPUT_COLUMNS,
    ClarifyError,
    build_table,
    write_csv,
)
from ccat.clarify.status import (
    CONTACTED_AT_COLUMN,
    CONTACTED_COLUMN,
    REPLIED_COLUMN,
    FamilyStatus,
    store_statuses,
)
from ccat.config import Config
from ccat.io import write_sheets

RAW_CHILD_SCHEMA: dict[str, Any] = {
    "zite_record_id": pl.String,
    "dob": pl.String,
    "first_name": pl.String,
}

FAMILY_SCHEMA: dict[str, Any] = {
    "family_id": pl.Int64,
    "first_name": pl.String,
    "last_name": pl.String,
    "phone": pl.String,
    "primary_email": pl.String,
}


def child_row(
    zite_record_id: str,
    angel_id: str,
    family_id: int,
    first_name: str,
    note: str | None,
    need_1: str = "coats",
) -> dict[str, Any]:
    """One final_child row; every uninteresting column is null."""
    row: dict[str, Any] = {column: None for column in CHILD_COLUMNS}
    row |= {
        "zite_record_id": zite_record_id,
        "angel_id": angel_id,
        "family_id": family_id,
        "first_name": first_name,
        "require_clarification": note,
        "need_1": need_1,
    }
    return row


def family_row(
    family_id: int,
    first_name: str,
    last_name: str,
    email: str,
) -> dict[str, Any]:
    """One final_family row."""
    return {
        "family_id": family_id,
        "first_name": first_name,
        "last_name": last_name,
        "phone": "555-0100",
        "primary_email": email,
    }


def raw_child_row(zite_record_id: str, dob: str) -> dict[str, Any]:
    """One raw_child row (only the join keys matter)."""
    return {"zite_record_id": zite_record_id, "dob": dob, "first_name": "X"}


def write_workbook(
    path: Path,
    children: list[dict[str, Any]],
    families: list[dict[str, Any]],
    raw_children: list[dict[str, Any]],
    xmas: date = date(2026, 12, 25),
) -> None:
    """Write a minimal three-sheet workbook for extraction tests."""
    write_sheets(
        path,
        "audit",
        {"final_child": pl.DataFrame(children)},
        xmas=xmas,
    )
    write_sheets(
        path,
        "match",
        {"final_family": pl.DataFrame(families)},
    )
    write_sheets(
        path,
        "acquire",
        {"raw_child": pl.DataFrame(raw_children)},
    )


@pytest.fixture
def workbook(tmp_path: Path) -> Path:
    """Workbook with one flagged and one clean child of one family."""
    path = tmp_path / "wb.xlsx"
    write_workbook(
        path,
        children=[
            child_row("rec-a", "1A", 1, "Aspen", "confirm mattress size"),
            child_row("rec-b", "1B", 1, "Birch", None),
        ],
        families=[family_row(1, "Caroline", "Herrala", "caroline@example.com")],
        raw_children=[
            raw_child_row("rec-a", "2022-03-08"),
            raw_child_row("rec-b", "2019-11-30"),
        ],
    )
    return path


class TestBuildTable:
    """``build_table`` sheet/column handling and joins."""

    def test_flags_only_noted_rows(self, workbook: Path) -> None:
        """Only the child with a non-empty note comes back."""
        table = build_table(workbook)
        assert table.height == 1
        assert table["angel_id"].to_list() == ["1A"]
        assert table.columns == list(OUTPUT_COLUMNS)

    def test_joins_family_and_raw_dob(self, workbook: Path) -> None:
        """Family contact info and raw-form dob are joined in."""
        row = build_table(workbook).to_dicts()[0]
        assert row["family_email"] == "caroline@example.com"
        assert row["family_last_name"] == "Herrala"
        assert row["raw_dob"] == "2022-03-08"
        assert row["zite_record_id"] == "rec-a"
        assert row["require_clarification"] == "confirm mattress size"

    def test_blank_note_is_not_flagged(self, tmp_path: Path) -> None:
        """A whitespace-only note row is not flagged."""
        path = tmp_path / "wb.xlsx"
        write_workbook(
            path,
            children=[
                child_row("rec-a", "1A", 1, "Aspen", "   "),
                child_row("rec-b", "1B", 1, "Birch", ""),
            ],
            families=[family_row(1, "C", "H", "c@example.com")],
            raw_children=[raw_child_row("rec-a", "2020-01-01")],
        )
        table = build_table(path)
        assert table.is_empty()

    def test_no_notes_yields_typed_empty_frame(self, workbook: Path) -> None:
        """With no flagged rows the empty frame keeps the columns."""
        path = workbook.parent / "clean.xlsx"
        write_workbook(
            path,
            children=[child_row("rec-b", "1B", 1, "Birch", None)],
            families=[family_row(1, "C", "H", "c@example.com")],
            raw_children=[raw_child_row("rec-b", "2019-11-30")],
        )
        table = build_table(path)
        assert table.is_empty()
        assert table.columns == list(OUTPUT_COLUMNS)

    def test_missing_workbook_raises(self, tmp_path: Path) -> None:
        """A missing workbook file raises ClarifyError."""
        with pytest.raises(ClarifyError, match="not found"):
            build_table(tmp_path / "missing.xlsx")

    def test_missing_sheet_raises(self, tmp_path: Path) -> None:
        """A workbook without final_family raises ClarifyError."""
        path = tmp_path / "wb.xlsx"
        write_sheets(
            path,
            "audit",
            {
                "final_child": pl.DataFrame(
                    [child_row("rec-a", "1A", 1, "Aspen", "note")]
                )
            },
            xmas=date(2026, 12, 25),
        )
        with pytest.raises(ClarifyError, match="final_family"):
            build_table(path)

    def test_missing_column_raises(self, tmp_path: Path) -> None:
        """A final_child missing a required column raises ClarifyError."""
        path = tmp_path / "wb.xlsx"
        write_sheets(
            path,
            "audit",
            {
                "final_child": pl.DataFrame(
                    [{"zite_record_id": "rec-a", "angel_id": "1A"}]
                )
            },
            xmas=date(2026, 12, 25),
        )
        with pytest.raises(ClarifyError, match="lacks column"):
            build_table(path)


class TestWriteCsv:
    """``write_csv`` output, defaults, and merged columns."""

    def test_writes_csv_with_all_columns(
        self, cfg: Config, workbook: Path, tmp_path: Path
    ) -> None:
        """The CSV carries the full column set plus the extra ones."""
        cfg = cfg.model_copy(
            update={
                "excel_file": str(workbook),
                "clarify": cfg.clarify.model_copy(
                    update={"output_dir": str(tmp_path / "emails")}
                ),
            }
        )
        out = workbook.parent / "nested" / "clarifications.csv"
        csv_path = write_csv(cfg, output=out)
        assert csv_path == out
        assert csv_path.is_file()
        frame = pl.read_csv(csv_path)
        assert frame.height == 1
        assert frame.columns == [
            *OUTPUT_COLUMNS,
            EMAIL_FILE_COLUMN,
            CONTACTED_COLUMN,
            CONTACTED_AT_COLUMN,
            REPLIED_COLUMN,
        ]
        assert frame[EMAIL_FILE_COLUMN][0] is None
        assert frame[CONTACTED_COLUMN][0] is False
        assert frame[CONTACTED_AT_COLUMN][0] is None
        assert frame[REPLIED_COLUMN][0] is False

    def test_contact_status_merged(
        self, cfg: Config, workbook: Path, tmp_path: Path
    ) -> None:
        """Status entries flow into the contacted/contacted_at/replied columns."""
        out_dir = tmp_path / "emails"
        out_dir.mkdir(parents=True)
        cfg = cfg.model_copy(
            update={
                "excel_file": str(workbook),
                "clarify": cfg.clarify.model_copy(
                    update={
                        "output_dir": str(out_dir),
                        "clarifications_csv": str(
                            tmp_path / "clarifications.csv"
                        ),
                    }
                ),
            }
        )
        store_statuses(
            cfg,
            {
                1: FamilyStatus(
                    contacted=True,
                    contacted_at="2026-11-14T21:30:00",
                    replied=True,
                )
            },
        )
        frame = pl.read_csv(write_csv(cfg))
        assert frame[CONTACTED_COLUMN][0] is True
        assert frame[CONTACTED_AT_COLUMN][0] == "2026-11-14T21:30:00"
        assert frame[REPLIED_COLUMN][0] is True

    def test_draft_reference_column_filled(
        self, cfg: Config, workbook: Path, tmp_path: Path
    ) -> None:
        """Existing saved drafts are referenced in the new column."""
        emails_dir = tmp_path / "emails"
        emails_dir.mkdir(parents=True)
        draft = emails_dir / "family_1.html"
        draft.write_text("<p>saved</p>")
        cfg = cfg.model_copy(
            update={
                "excel_file": str(workbook),
                "clarify": cfg.clarify.model_copy(
                    update={
                        "output_dir": str(emails_dir),
                        "clarifications_csv": str(
                            tmp_path / "clarifications.csv"
                        ),
                    }
                ),
            }
        )
        csv_path = write_csv(cfg)
        frame = pl.read_csv(csv_path)
        # Row belongs to family 1, whose draft exists on disk.
        assert frame[EMAIL_FILE_COLUMN][0] == str(draft)

    def test_default_output_uses_config_path(
        self, cfg: Config, workbook: Path
    ) -> None:
        """Without an explicit output the configured CSV is used."""
        csv_path = workbook.parent / "default.csv"
        cfg = cfg.model_copy(
            update={
                "excel_file": str(workbook),
                "clarify": cfg.clarify.model_copy(
                    update={"clarifications_csv": str(csv_path)}
                ),
            }
        )
        assert write_csv(cfg) == csv_path
        assert pl.read_csv(csv_path).height == 1
