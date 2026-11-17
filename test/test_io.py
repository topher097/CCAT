"""Tests for persistent finals, ownership, formulas, and atomic writes."""

from pathlib import Path
from typing import Any

import polars as pl
import pytest
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from ccat.audit import AuditError
from ccat.audit.store import (
    FINAL_CHILD_COLUMNS,
    FINAL_CHILD_SCHEMA,
    FINAL_FAMILY_COLUMNS,
    FINAL_FAMILY_SCHEMA,
    FINAL_SPONSOR_COLUMNS,
    FINAL_SPONSOR_SCHEMA,
    build_final_frame,
)
from ccat.config import Config
from ccat.io import (
    SHEETS,
    WRITABLE_SHEETS,
    SheetFormulaError,
    SheetOwnershipError,
    SheetSchemaError,
    list_sheets,
    read_sheet,
    write_sheets,
)

RAW_SHEETS = frozenset({"raw_sponsor", "raw_family", "raw_child"})
FINAL_SHEETS = frozenset({"final_child", "final_family", "final_sponsor"})
LOG_SHEETS = frozenset(
    {
        "duplicate_log",
        "dob_conflict_log",
        "email_validation_log",
        "address_validation_log",
    }
)
LEGACY_SHEETS = frozenset(
    {
        "sponsor_submissions",
        "family_submissions",
        "child_submissions",
        "adj_child",
        "adj_family",
        "adj_sponsor",
    }
)
ENTITY_COLUMNS = {
    "child": FINAL_CHILD_COLUMNS,
    "family": FINAL_FAMILY_COLUMNS,
    "sponsor": FINAL_SPONSOR_COLUMNS,
}
ENTITY_SCHEMAS = {
    "child": FINAL_CHILD_SCHEMA,
    "family": FINAL_FAMILY_SCHEMA,
    "sponsor": FINAL_SPONSOR_SCHEMA,
}
ENTITY_KEYS = {
    "child": "zite_record_id",
    "family": "family_id",
    "sponsor": "sponsor_id",
}
ENTITY_WRITERS = {"child": "generate", "family": "match", "sponsor": "match"}
DEFAULT_AGES = [8, 5, 14]


def final_child_frame(
    rows: list[dict[str, Any]] | None = None,
) -> pl.DataFrame:
    """Build a full persistent child store, including ignored/orphan rows."""
    if rows is None:
        rows = [
            {
                "zite_record_id": "c1",
                "family_id": 1,
                "first_name": "One",
                "dob": "2018-05-10",
                "age_at_xmas": 8,
            },
            {
                "zite_record_id": "c2",
                "family_id": 1,
                "first_name": "Ignored",
                "dob": "2020-12-26",
                "age_at_xmas": 5,
                "ignore": True,
            },
            {
                "zite_record_id": "c3",
                "family_id": None,
                "first_name": "Orphan",
                "dob": "2012-01-05",
                "age_at_xmas": 13,
            },
        ]
    return build_final_frame("child", rows)


def final_family_frame(
    rows: list[dict[str, Any]] | None = None,
) -> pl.DataFrame:
    """Build a full persistent family store."""
    if rows is None:
        rows = [
            {"family_id": 1, "primary_email": "one@example.com"},
            {"family_id": 2, "primary_email": "two@example.com"},
        ]
    return build_final_frame("family", rows)


def final_sponsor_frame(
    rows: list[dict[str, Any]] | None = None,
) -> pl.DataFrame:
    """Build a full persistent sponsor store."""
    if rows is None:
        rows = [
            {"sponsor_id": 1, "email": "one@example.com"},
            {"sponsor_id": 2, "email": "two@example.com"},
        ]
    return build_final_frame("sponsor", rows)


def entity_frame(
    entity: str, rows: list[dict[str, Any]] | None = None
) -> pl.DataFrame:
    """Build one of the three full persistent frames."""
    if entity == "child":
        return final_child_frame(rows)
    if entity == "family":
        return final_family_frame(rows)
    if entity == "sponsor":
        return final_sponsor_frame(rows)
    raise AssertionError(f"unknown test entity {entity}")


def persistent_frames() -> dict[str, pl.DataFrame]:
    """Return valid defaults for all three persistent final sheets."""
    return {
        "final_child": final_child_frame(),
        "final_family": final_family_frame(),
        "final_sponsor": final_sponsor_frame(),
    }


def raw_child_frame() -> pl.DataFrame:
    """Build a raw child frame with age columns but no final-store schema."""
    return pl.DataFrame(
        {
            "zite_record_id": ["raw1", "raw2"],
            "dob": ["2018-05-10", "2020-12-26"],
            "age_at_xmas": [8, 5],
        },
        schema={
            "zite_record_id": pl.String,
            "dob": pl.String,
            "age_at_xmas": pl.Int64,
        },
    )


def match_report_frame(reason: str = "seed") -> pl.DataFrame:
    """Build a small match report."""
    return pl.DataFrame(
        [{"kind": "family", "ref": "1", "reason": reason}],
        schema={"kind": pl.String, "ref": pl.String, "reason": pl.String},
    )


def seed_workbook(cfg: Config) -> None:
    """Seed raw/final/report fixtures through the migrate writer identity."""
    write_sheets(
        cfg.excel_file,
        "migrate",
        {
            "raw_child": raw_child_frame(),
            **persistent_frames(),
            "match_report": match_report_frame(),
        },
        xmas=cfg.xmas,
    )


def workbook_bytes(cfg: Config) -> bytes:
    """Return the exact workbook bytes for atomic-failure assertions."""
    return Path(cfg.excel_file).read_bytes()


def age_formula(row: int) -> str:
    """Return the expected formula using the full final_child column order."""
    dob_column = get_column_letter(FINAL_CHILD_COLUMNS.index("dob") + 1)
    return f'=DATEDIF({dob_column}{row}, DATE(2026,12,25), "y")'


def default_age_formulas() -> list[str]:
    """Return formulas for every default child, including ignored/orphan rows."""
    return [age_formula(row) for row in range(2, 2 + len(DEFAULT_AGES))]


def age_cells(cfg: Config, sheet: str = "final_child") -> list[Any]:
    """Read the age column as openpyxl sees it, including formulas."""
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
    """Full persistent final_child rows retain live formulas and caches."""

    def test_all_children_get_formulas_with_cached_values(
        self, cfg: Config
    ) -> None:
        """Ignored and orphaned children are neither projected nor dropped."""
        seed_workbook(cfg)

        assert age_cells(cfg) == default_age_formulas()
        assert read_ages(cfg).to_list() == DEFAULT_AGES
        stored = read_sheet(cfg.excel_file, "final_child")
        assert stored is not None
        assert stored.columns == list(FINAL_CHILD_COLUMNS)
        assert stored["zite_record_id"].to_list() == ["c1", "c2", "c3"]
        assert stored["ignore"].to_list() == [False, True, False]
        assert stored["family_id"].to_list() == [1, 1, None]

    def test_stale_age_is_recomputed_from_dob(self, cfg: Config) -> None:
        """A full frame age that disagrees with dob is corrected on write."""
        frame = final_child_frame(
            [
                {
                    "zite_record_id": "c1",
                    "family_id": 1,
                    "dob": "2012-01-05",
                    "age_at_xmas": 99,
                }
            ]
        )
        write_sheets(
            cfg.excel_file,
            "migrate",
            {"final_child": frame},
            xmas=cfg.xmas,
        )

        assert age_cells(cfg) == [age_formula(2)]
        assert read_ages(cfg).to_list() == [14]

    def test_missing_xmas_is_rejected_without_workbook_changes(
        self, cfg: Config
    ) -> None:
        """The xmas guard fires before an existing workbook can be replaced."""
        seed_workbook(cfg)
        before = workbook_bytes(cfg)

        with pytest.raises(SheetFormulaError, match="without xmas"):
            write_sheets(
                cfg.excel_file, "audit", {"final_child": final_child_frame()}
            )

        assert workbook_bytes(cfg) == before
        assert read_ages(cfg).to_list() == DEFAULT_AGES

    def test_unparsable_dob_keeps_written_age(self, cfg: Config) -> None:
        """Only rows with valid dob values receive formulas."""
        frame = final_child_frame(
            [
                {
                    "zite_record_id": "c1",
                    "family_id": 1,
                    "dob": "not-a-date",
                    "age_at_xmas": 7,
                },
                {
                    "zite_record_id": "c2",
                    "family_id": 1,
                    "dob": None,
                    "age_at_xmas": 6,
                    "ignore": True,
                },
                {
                    "zite_record_id": "c3",
                    "family_id": None,
                    "dob": "2018-05-10",
                    "age_at_xmas": 8,
                },
            ]
        )
        write_sheets(
            cfg.excel_file,
            "migrate",
            {"final_child": frame},
            xmas=cfg.xmas,
        )

        assert age_cells(cfg) == [7, 6, age_formula(4)]
        assert read_ages(cfg).to_list() == [7, 6, 8]

    def test_raw_child_never_gets_formulas(self, cfg: Config) -> None:
        """Raw dob/age columns remain plain values without a final schema."""
        write_sheets(
            cfg.excel_file, "acquire", {"raw_child": raw_child_frame()}
        )

        assert age_cells(cfg, "raw_child") == [8, 5]
        assert read_ages(cfg, "raw_child").to_list() == [8, 5]


class TestFormulaPersistence:
    """Rewrites that skip final_child keep its formulas and caches."""

    def test_other_module_rewrite_keeps_cached_ages(self, cfg: Config) -> None:
        """A full match write leaves every final_child age readable and live."""
        seed_workbook(cfg)
        write_sheets(
            cfg.excel_file,
            "match",
            {
                "final_family": final_family_frame(
                    [
                        {"family_id": 1, "assigned_sponsor_id": 1},
                        {"family_id": 2},
                    ]
                )
            },
        )

        ages = read_ages(cfg)
        assert ages.dtype == pl.Int64
        assert ages.to_list() == DEFAULT_AGES
        assert age_cells(cfg) == default_age_formulas()

    def test_caches_survive_repeated_rewrites(self, cfg: Config) -> None:
        """Logs, reports, and full finals repeatedly restore age caches."""
        seed_workbook(cfg)
        writes = (
            ("match", "final_family", final_family_frame()),
            (
                "screen",
                "duplicate_log",
                pl.DataFrame(
                    {"message": ["unchanged"]}, schema={"message": pl.String}
                ),
            ),
            ("match", "final_sponsor", final_sponsor_frame()),
            ("match", "match_report", match_report_frame("rerun")),
        )
        for module, sheet, frame in writes:
            write_sheets(cfg.excel_file, module, {sheet: frame})

        assert read_ages(cfg).to_list() == DEFAULT_AGES
        assert age_cells(cfg) == default_age_formulas()

    def test_rewritten_final_child_refreshes_formulas_without_row_removal(
        self, cfg: Config
    ) -> None:
        """Generate updates one dob while retaining every persisted child."""
        seed_workbook(cfg)
        frame = final_child_frame().with_columns(
            pl.when(pl.col("zite_record_id") == "c1")
            .then(pl.lit("2012-01-05"))
            .otherwise(pl.col("dob"))
            .alias("dob")
        )
        write_sheets(
            cfg.excel_file, "generate", {"final_child": frame}, xmas=cfg.xmas
        )

        assert age_cells(cfg) == default_age_formulas()
        assert read_ages(cfg).to_list() == [14, 5, 14]
        stored = read_sheet(cfg.excel_file, "final_child")
        assert stored is not None
        assert stored["zite_record_id"].to_list() == ["c1", "c2", "c3"]

    def test_formula_injection_failure_preserves_original_workbook(
        self, cfg: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A post-save formula failure never replaces the atomic target."""
        seed_workbook(cfg)
        before = workbook_bytes(cfg)

        def fail_injection(
            archive_path: Path, formulas: dict[str, str]
        ) -> None:
            """Simulate an OOXML formula injection failure."""
            raise SheetFormulaError("injected formula failure")

        monkeypatch.setattr("ccat.io._inject_age_formulas", fail_injection)
        frame = final_child_frame().with_columns(
            pl.lit("2012-01-05").alias("dob")
        )
        with pytest.raises(SheetFormulaError, match="injected formula failure"):
            write_sheets(
                cfg.excel_file,
                "audit",
                {"final_child": frame},
                xmas=cfg.xmas,
            )

        assert workbook_bytes(cfg) == before
        assert age_cells(cfg) == default_age_formulas()
        assert read_ages(cfg).to_list() == DEFAULT_AGES


class TestPersistentFinalValidation:
    """Malformed projected or destructive final writes fail before saving."""

    @pytest.mark.parametrize("entity", ["child", "family", "sponsor"])
    def test_old_projected_final_is_rejected_without_repair(
        self, cfg: Config, entity: str
    ) -> None:
        """Sparse legacy projections are not silently expanded to full rows."""
        seed_workbook(cfg)
        projected = {
            "child": pl.DataFrame(
                {"zite_record_id": ["c1"], "dob": ["2018-05-10"]}
            ),
            "family": pl.DataFrame(
                {"family_id": [1], "assigned_sponsor_id": [1]}
            ),
            "sponsor": pl.DataFrame(
                {"sponsor_id": [1], "assigned_angel_ids": ["1A"]}
            ),
        }[entity]
        candidate_before = projected.clone()
        before = workbook_bytes(cfg)

        with pytest.raises(AuditError, match="persistent full schema"):
            write_sheets(
                cfg.excel_file,
                "migrate",
                {f"final_{entity}": projected},
                xmas=cfg.xmas,
            )

        assert projected.equals(candidate_before)
        assert workbook_bytes(cfg) == before

    @pytest.mark.parametrize("entity", ["child", "family", "sponsor"])
    @pytest.mark.parametrize("mutation", ["missing", "extra"])
    def test_missing_or_extra_full_columns_are_rejected(
        self, cfg: Config, entity: str, mutation: str
    ) -> None:
        """Schema guards neither fill missing columns nor drop extra columns."""
        seed_workbook(cfg)
        candidate = entity_frame(entity).head(1)
        if mutation == "missing":
            candidate = candidate.drop(ENTITY_COLUMNS[entity][1])
        else:
            candidate = candidate.with_columns(
                pl.lit("legacy").alias("unexpected_column")
            )
        candidate_before = candidate.clone()
        before = workbook_bytes(cfg)

        with pytest.raises(AuditError, match="persistent full schema"):
            write_sheets(
                cfg.excel_file,
                "audit",
                {f"final_{entity}": candidate},
                xmas=cfg.xmas,
            )

        assert candidate.equals(candidate_before)
        assert workbook_bytes(cfg) == before

    @pytest.mark.parametrize("entity", ["child", "family", "sponsor"])
    def test_duplicate_keys_are_rejected_atomically(
        self, cfg: Config, entity: str
    ) -> None:
        """A full schema does not make duplicate persistent identities valid."""
        seed_workbook(cfg)
        row = entity_frame(entity).head(1).to_dicts()[0]
        candidate = pl.DataFrame(
            [row, row], schema=ENTITY_SCHEMAS[entity]
        ).select(list(ENTITY_COLUMNS[entity]))
        before = workbook_bytes(cfg)
        key = ENTITY_KEYS[entity]

        with pytest.raises(AuditError, match=f"duplicate {key}"):
            write_sheets(
                cfg.excel_file,
                "migrate",
                {f"final_{entity}": candidate},
                xmas=cfg.xmas,
            )

        assert workbook_bytes(cfg) == before

    @pytest.mark.parametrize("entity", ["child", "family", "sponsor"])
    def test_null_keys_are_rejected_atomically(
        self, cfg: Config, entity: str
    ) -> None:
        """Null keys cannot be repaired or silently discarded during writes."""
        seed_workbook(cfg)
        key = ENTITY_KEYS[entity]
        candidate = (
            entity_frame(entity)
            .head(1)
            .with_columns(
                pl.lit(None, dtype=ENTITY_SCHEMAS[entity][key]).alias(key)
            )
        )
        before = workbook_bytes(cfg)

        with pytest.raises(AuditError, match=f"missing {key}"):
            write_sheets(
                cfg.excel_file,
                "migrate",
                {f"final_{entity}": candidate},
                xmas=cfg.xmas,
            )

        assert workbook_bytes(cfg) == before

    def test_blank_child_key_is_rejected_atomically(self, cfg: Config) -> None:
        """Whitespace-only child identities are missing keys, not new rows."""
        seed_workbook(cfg)
        candidate = (
            final_child_frame()
            .head(1)
            .with_columns(pl.lit("  ").alias("zite_record_id"))
        )
        before = workbook_bytes(cfg)

        with pytest.raises(AuditError, match="missing zite_record_id"):
            write_sheets(
                cfg.excel_file,
                "audit",
                {"final_child": candidate},
                xmas=cfg.xmas,
            )

        assert workbook_bytes(cfg) == before

    @pytest.mark.parametrize("entity", ["child", "family", "sponsor"])
    def test_existing_entity_rows_cannot_be_removed(
        self, cfg: Config, entity: str
    ) -> None:
        """Writers may update/append full rows, never replace with projections."""
        seed_workbook(cfg)
        sheet = f"final_{entity}"
        candidate = entity_frame(entity).head(1)
        before = workbook_bytes(cfg)

        with pytest.raises(
            SheetSchemaError, match="would remove persisted entity"
        ):
            write_sheets(
                cfg.excel_file,
                ENTITY_WRITERS[entity],
                {sheet: candidate},
                xmas=cfg.xmas if entity == "child" else None,
            )

        assert workbook_bytes(cfg) == before
        stored = read_sheet(cfg.excel_file, sheet)
        assert stored is not None
        assert stored.height == entity_frame(entity).height

    def test_invalid_second_sheet_preserves_entire_batch(
        self, cfg: Config
    ) -> None:
        """One destructive final prevents every sibling write in the batch."""
        seed_workbook(cfg)
        before = workbook_bytes(cfg)

        with pytest.raises(SheetSchemaError, match="remove persisted entity"):
            write_sheets(
                cfg.excel_file,
                "migrate",
                {
                    "match_report": match_report_frame("must not appear"),
                    "final_child": final_child_frame(),
                    "final_family": final_family_frame().head(1),
                },
                xmas=cfg.xmas,
            )

        assert workbook_bytes(cfg) == before
        report = read_sheet(cfg.excel_file, "match_report")
        assert report is not None
        assert report["reason"].to_list() == ["seed"]


class TestOwnershipMatrix:
    """The persistent-final workbook has no legacy submissions/adj writers."""

    def test_matrix_writers_exact(self) -> None:
        """Every writable identity and sheet matches the new contract exactly."""
        assert WRITABLE_SHEETS == {
            "acquire": RAW_SHEETS,
            "screen": FINAL_SHEETS | LOG_SHEETS,
            "audit": FINAL_SHEETS,
            "match": frozenset(
                {"final_family", "final_sponsor", "match_report"}
            ),
            "generate": frozenset({"final_child"}),
            "migrate": RAW_SHEETS
            | FINAL_SHEETS
            | LOG_SHEETS
            | frozenset({"match_report"}),
        }
        assert "stats" not in WRITABLE_SHEETS
        assert all(
            LEGACY_SHEETS.isdisjoint(owned)
            for owned in WRITABLE_SHEETS.values()
        )
        assert WRITABLE_SHEETS["migrate"] == frozenset(SHEETS)

    @pytest.mark.parametrize("module", ["screen", "audit"])
    def test_final_writers_accept_full_frames(
        self, cfg: Config, module: str
    ) -> None:
        """Screen and audit own all three finals when schemas are complete."""
        write_sheets(
            cfg.excel_file,
            module,
            persistent_frames(),
            xmas=cfg.xmas,
        )

        assert FINAL_SHEETS <= set(list_sheets(cfg.excel_file))
        for entity in ("child", "family", "sponsor"):
            stored = read_sheet(cfg.excel_file, f"final_{entity}")
            assert stored is not None
            assert stored.columns == list(ENTITY_COLUMNS[entity])

    def test_migrate_writer_bootstraps_fresh_workbook(
        self, cfg: Config
    ) -> None:
        """Migrate creates exactly raw/finals/logs/report with full finals."""
        sheets = {
            name: pl.DataFrame({"value": ["seed"]}, schema={"value": pl.String})
            for name in SHEETS
        }
        sheets.update(persistent_frames())
        write_sheets(cfg.excel_file, "migrate", sheets, xmas=cfg.xmas)

        names = set(list_sheets(cfg.excel_file))
        assert names == set(SHEETS)
        assert names.isdisjoint(LEGACY_SHEETS)
        for entity in ("child", "family", "sponsor"):
            stored = read_sheet(cfg.excel_file, f"final_{entity}")
            assert stored is not None
            assert stored.columns == list(ENTITY_COLUMNS[entity])

    @pytest.mark.parametrize(
        ("module", "sheet"),
        [
            ("acquire", "final_child"),
            ("screen", "child_submissions"),
            ("screen", "adj_child"),
            ("audit", "adj_family"),
            ("match", "adj_sponsor"),
            ("generate", "adj_child"),
            ("migrate", "family_submissions"),
            ("migrate", "adj_sponsor"),
        ],
    )
    def test_legacy_permissions_are_rejected_atomically(
        self, cfg: Config, module: str, sheet: str
    ) -> None:
        """No writer retains submissions/adj or acquire-final permissions."""
        seed_workbook(cfg)
        before = workbook_bytes(cfg)

        with pytest.raises(SheetOwnershipError, match="ownership"):
            write_sheets(
                cfg.excel_file, module, {sheet: pl.DataFrame()}, xmas=cfg.xmas
            )

        assert workbook_bytes(cfg) == before

    def test_stats_is_not_a_writer(self, cfg: Config) -> None:
        """The stats reader identity cannot create or replace workbook sheets."""
        seed_workbook(cfg)
        before = workbook_bytes(cfg)

        with pytest.raises(SheetOwnershipError, match="unknown module 'stats'"):
            write_sheets(
                cfg.excel_file,
                "stats",
                {"match_report": match_report_frame("stats")},
            )

        assert workbook_bytes(cfg) == before

    def test_match_archives_only_owned_matching_prefixes(
        self, cfg: Config
    ) -> None:
        """Match can archive its three sheets while writing their replacements."""
        seed_workbook(cfg)
        updated_families = final_family_frame(
            [
                {"family_id": 1, "assigned_sponsor_id": 1},
                {"family_id": 2},
            ]
        )
        write_sheets(
            cfg.excel_file,
            "match",
            {
                "final_family": updated_families,
                "final_sponsor": final_sponsor_frame(),
                "match_report": match_report_frame("rerun"),
            },
            renames={
                "final_family": "final_family_2026_01",
                "final_sponsor": "final_sponsor_2026_01",
                "match_report": "match_report_2026_01",
            },
        )

        names = set(list_sheets(cfg.excel_file))
        assert {
            "final_family_2026_01",
            "final_sponsor_2026_01",
            "match_report_2026_01",
        } <= names
        archived_family = read_sheet(cfg.excel_file, "final_family_2026_01")
        active_family = read_sheet(cfg.excel_file, "final_family")
        archived_report = read_sheet(cfg.excel_file, "match_report_2026_01")
        active_report = read_sheet(cfg.excel_file, "match_report")
        assert archived_family is not None and active_family is not None
        assert archived_family["assigned_sponsor_id"].to_list() == [None, None]
        assert active_family["assigned_sponsor_id"].to_list() == [1, None]
        assert archived_report is not None and active_report is not None
        assert archived_report["reason"].to_list() == ["seed"]
        assert active_report["reason"].to_list() == ["rerun"]
        assert age_cells(cfg) == default_age_formulas()

    @pytest.mark.parametrize(
        ("module", "old_name", "new_name"),
        [
            ("match", "final_child", "final_child_2026"),
            ("match", "raw_child", "raw_child_2026"),
            ("match", "final_family", "archive_2026"),
            ("match", "match_report", "final_family_2026"),
            ("audit", "final_family", "final_family_2026"),
            ("generate", "final_child", "final_child_2026"),
            ("migrate", "raw_child", "raw_child_2026"),
        ],
    )
    def test_non_match_or_mismatched_archives_are_rejected(
        self, cfg: Config, module: str, old_name: str, new_name: str
    ) -> None:
        """Cross-module and mismatched-prefix renames leave bytes unchanged."""
        seed_workbook(cfg)
        before = workbook_bytes(cfg)

        with pytest.raises(SheetOwnershipError, match="rename|archives"):
            write_sheets(
                cfg.excel_file,
                module,
                {},
                renames={old_name: new_name},
            )

        assert workbook_bytes(cfg) == before

    def test_existing_archive_destination_is_rejected_atomically(
        self, cfg: Config
    ) -> None:
        """An archive collision cannot replace either active or archived data."""
        seed_workbook(cfg)
        archive = {"final_family": "final_family_2026_01"}
        write_sheets(
            cfg.excel_file,
            "match",
            {"final_family": final_family_frame()},
            renames=archive,
        )
        before = workbook_bytes(cfg)

        with pytest.raises(SheetOwnershipError, match="existing sheet"):
            write_sheets(
                cfg.excel_file,
                "match",
                {"final_family": final_family_frame()},
                renames=archive,
            )

        assert workbook_bytes(cfg) == before
