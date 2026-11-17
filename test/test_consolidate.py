"""End-to-end tests for full-adj to persistent-final consolidation."""

import hashlib
import json
from pathlib import Path
import shutil
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.utils import get_column_letter
import polars as pl
import pytest

from ccat.audit.store import build_final_frame, read_final
from ccat.config import Config
from ccat.consolidate import ConsolidationError, run
from ccat.io import list_sheets, read_sheet
from conftest import load_test_cfg

_AUXILIARY_SHEETS = (
    "duplicate_log",
    "dob_conflict_log",
    "email_validation_log",
    "address_validation_log",
    "match_report",
)


def _write_workbook(path: Path, sheets: dict[str, pl.DataFrame]) -> None:
    """Create an arbitrary legacy workbook, including disallowed old layers."""
    workbook = Workbook()
    try:
        default = workbook.active
        if default is not None and default.title == "Sheet":
            workbook.remove(default)
        for name, frame in sheets.items():
            worksheet = workbook.create_sheet(name)
            worksheet.append(list(frame.columns))
            for row in frame.iter_rows():
                worksheet.append(list(row))
        workbook.save(path)
    finally:
        workbook.close()


def _golden_frames() -> dict[str, pl.DataFrame]:
    """Build dense stores with duplicates, exceptions, and raw-missing rows."""
    sponsors = build_final_frame(
        "sponsor",
        [
            {
                "sponsor_id": 1,
                "zite_record_id": "s1",
                "create": "2026-01-01T10:00:00Z",
                "email": "assigned@example.com",
                "first_name": "Assigned",
                "last_name": "Sponsor",
                "sponsor_type": "Individual",
                "num_children": 2,
                "age_preference": "Any Age",
                "gender_preference": "Any Gender",
                "dropoff_location": "Main",
                "source": "web",
                "pref_age_min": 0,
                "pref_age_max": 18,
                "assigned_angel_ids": "A1,A6",
                "num_angels_assigned": 2,
                "accepts_disclaimer": True,
                "status": "audited",
                "audited_at": "2026-11-01T00:00:00+00:00",
            },
            {
                "sponsor_id": 2,
                "zite_record_id": "s2",
                "email": "ignored@example.com",
                "first_name": "Ignored",
                "last_name": "Sponsor",
                "num_children": 1,
                "age_preference": "Any Age",
                "status": "ignored",
                "ignore": True,
            },
            {
                "sponsor_id": 3,
                "zite_record_id": "s3-raw-missing",
                "email": "kept@example.com",
                "first_name": "Raw Missing",
                "num_children": 0,
                "status": "new",
            },
        ],
    )
    families = build_final_frame(
        "family",
        [
            {
                "family_id": 1,
                "zite_record_id": "f1",
                "created": "2026-01-02T10:00:00Z",
                "primary_email": "one@example.com",
                "first_name": "One",
                "last_name": "Family",
                "street_address": "1 Main St",
                "city": "Boston",
                "zip_code": "02108",
                "state": "MA",
                "gift_pickup_location": "Main",
                "source": "web",
                "accepts_disclaimer": True,
                "assigned_sponsor_id": 1,
                "assigned_angel_ids": "A1",
                "num_angels_in_family": 1,
                "status": "audited",
                "audited_at": "2026-11-02T00:00:00+00:00",
            },
            {
                "family_id": 2,
                "zite_record_id": "f2-earliest",
                "created": "2026-01-03T10:00:00Z",
                "primary_email": "duplicate@example.com",
                "first_name": "Duplicate",
                "last_name": "Family",
                "phone": "617-555-0102",
                "potential_duplicate": True,
                "assigned_sponsor_id": None,
                "num_angels_in_family": 0,
                "status": "needs_clarification",
                "clarification_notes": "resolve duplicate family submissions",
            },
            {
                "family_id": 3,
                "zite_record_id": "f3",
                "primary_email": "ignored-family@example.com",
                "first_name": "Ignored",
                "last_name": "Family",
                "status": "ignored",
                "ignore": True,
            },
            {
                "family_id": 4,
                "zite_record_id": "f4-raw-missing",
                "primary_email": "preserved-family@example.com",
                "first_name": "Preserved",
                "last_name": "Family",
                "assigned_sponsor_id": 1,
                "assigned_angel_ids": "A6",
                "num_angels_in_family": 1,
                "status": "audited",
            },
        ],
    )
    children = build_final_frame(
        "child",
        [
            {
                "zite_record_id": "c1",
                "zite_created_at": "2026-01-04T10:00:00Z",
                "created": "2026-01-04T10:00:00Z",
                "family_id": 1,
                "angel_id": "A1",
                "first_name": "Twin",
                "primary_email": "one@example.com",
                "dob": "2018-05-10",
                "age_reported": 8,
                "age_at_xmas": 8,
                "gender": "Female",
                "special_needs": "other: unspecified",
                "need_1": "winter coat",
                "need_2": "boots",
                "desire_1": "art set",
                "shoe_size": "2",
                "shoe_size_type": "US",
                "favorite_colors": "purple",
                "interests": "painting",
                "other_notes": "human edited",
                "potential_anomoly": "Sibling DOB conflict preserved",
                "source": "web",
                "generated_info_sheet": True,
                "screened": True,
                "potential_duplicate": True,
                "status": "audited",
                "audited_at": "2026-11-03T00:00:00+00:00",
            },
            {
                "zite_record_id": "c2",
                "created": "2026-01-05T10:00:00Z",
                "family_id": 1,
                "angel_id": None,
                "first_name": "Twin",
                "primary_email": "one@example.com",
                "dob": "2018-06-01",
                "age_reported": 8,
                "age_at_xmas": 8,
                "gender": "Female",
                "need_1": "duplicate child survives",
                "potential_anomoly": "DOB conflict requiring review",
                "generated_info_sheet": False,
                "screened": False,
                "potential_duplicate": True,
                "status": "needs_clarification",
                "clarification_notes": "duplicate child and sibling DOB",
            },
            {
                "zite_record_id": "c3",
                "family_id": 2,
                "angel_id": None,
                "first_name": "Pending",
                "primary_email": "duplicate@example.com",
                "dob": "2020-12-26",
                "age_at_xmas": 5,
                "special_needs": "wheelchair access",
                "screened": False,
                "generated_info_sheet": False,
                "status": "new",
            },
            {
                "zite_record_id": "c4-orphan",
                "family_id": None,
                "angel_id": None,
                "first_name": "Orphan",
                "primary_email": "missing-family@example.com",
                "dob": None,
                "age_at_xmas": None,
                "other_notes": "orphan survives without an age formula",
                "screened": True,
                "generated_info_sheet": False,
                "status": "needs_clarification",
                "clarification_notes": "No matching family registration",
            },
            {
                "zite_record_id": "c5-ignored",
                "family_id": 3,
                "angel_id": None,
                "first_name": "Ignored",
                "primary_email": "ignored-family@example.com",
                "dob": "2019-01-01",
                "age_at_xmas": 7,
                "screened": True,
                "generated_info_sheet": True,
                "status": "ignored",
                "ignore": True,
            },
            {
                "zite_record_id": "c6-raw-missing",
                "family_id": 4,
                "angel_id": "A6",
                "first_name": "Preserved",
                "primary_email": "preserved-family@example.com",
                "dob": "2017-02-03",
                "age_at_xmas": 9,
                "need_1": "raw-missing generated child",
                "screened": True,
                "generated_info_sheet": True,
                "status": "audited",
                "audited_at": "2026-11-04T00:00:00+00:00",
            },
        ],
    )
    raw_sponsors = pl.DataFrame(
        [
            {
                "sponsor_id": 1,
                "zite_record_id": "s1",
                "email": "assigned@example.com",
                "first_name": "Assigned",
            },
            {
                "sponsor_id": 2,
                "zite_record_id": "s2",
                "email": "ignored@example.com",
                "first_name": "Ignored",
            },
        ]
    )
    raw_families = pl.DataFrame(
        [
            {
                "family_id": 1,
                "zite_record_id": "f1",
                "primary_email": "one@example.com",
                "created": "2026-01-02T10:00:00Z",
            },
            {
                "family_id": 2,
                "zite_record_id": "f2-earliest",
                "primary_email": "duplicate@example.com",
                "created": "2026-01-03T10:00:00Z",
            },
            {
                "family_id": 2,
                "zite_record_id": "f2-later",
                "primary_email": "duplicate@example.com",
                "created": "2026-01-03T11:00:00Z",
            },
            {
                "family_id": 3,
                "zite_record_id": "f3",
                "primary_email": "ignored-family@example.com",
                "created": "2026-01-03T12:00:00Z",
            },
        ]
    )
    raw_children = pl.DataFrame(
        [
            {
                "zite_record_id": record_id,
                "family_id": family_id,
                "first_name": first_name,
                "primary_email": email,
                "dob": dob,
            }
            for record_id, family_id, first_name, email, dob in (
                ("c1", 1, "Twin", "one@example.com", "2018-05-10"),
                ("c2", 1, "Twin", "one@example.com", "2018-06-01"),
                (
                    "c3",
                    2,
                    "Pending",
                    "duplicate@example.com",
                    "2020-12-26",
                ),
                (
                    "c4-orphan",
                    None,
                    "Orphan",
                    "missing-family@example.com",
                    None,
                ),
                (
                    "c5-ignored",
                    3,
                    "Ignored",
                    "ignored-family@example.com",
                    "2019-01-01",
                ),
            )
        ]
    )
    return {
        "raw_sponsor": raw_sponsors,
        "raw_family": raw_families,
        "raw_child": raw_children,
        "adj_sponsor": sponsors,
        "adj_family": families,
        "adj_child": children,
        # Projected finals are decoys: conversion must never source from them.
        "final_sponsor": pl.DataFrame(
            {"sponsor_id": [999], "email": ["projection@example.com"]}
        ),
        "final_family": pl.DataFrame(
            {"family_id": [999], "primary_email": ["projection@example.com"]}
        ),
        "final_child": pl.DataFrame(
            {"zite_record_id": ["not-source"], "dob": ["2000-01-01"]}
        ),
        "duplicate_log": pl.DataFrame(
            [
                {
                    "entity": "family",
                    "left_id": "f2-earliest",
                    "right_id": "f2-later",
                },
                {"entity": "child", "left_id": "c1", "right_id": "c2"},
            ]
        ),
        "dob_conflict_log": pl.DataFrame(
            [
                {
                    "child_a_zite_record_id": "c1",
                    "child_b_zite_record_id": "c2",
                    "gap_months": 0,
                    "gap_days": 22,
                }
            ]
        ),
        "email_validation_log": pl.DataFrame(
            [
                {"email": "one@example.com", "result": "valid"},
                {"email": "duplicate@example.com", "result": "unknown"},
            ]
        ),
        "address_validation_log": pl.DataFrame(
            [{"family_id": 1, "normalized": "1 Main St, Boston MA"}]
        ),
        "match_report": pl.DataFrame(
            [{"family_id": 1, "sponsor_id": 1, "angel_ids": "A1"}]
        ),
        "family_submissions": raw_families,
        "child_submissions": raw_children,
        "match_report_2026_archive": pl.DataFrame({"old": [True]}),
    }


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """Return a config whose normal workbook path is also isolated."""
    return load_test_cfg(
        excel_file=str(tmp_path / "unrelated-config-target.xlsx"),
        log_file=str(tmp_path / "ccat.log"),
        match_report_csv=str(tmp_path / "report.csv"),
    )


@pytest.fixture
def source_workbook(tmp_path: Path) -> Path:
    """Return the golden dense-adj source workbook."""
    path = tmp_path / "legacy-dense.xlsx"
    _write_workbook(path, _golden_frames())
    return path


def _sha256(path: Path) -> str:
    """Hash one test file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copied_frame_values(frame: pl.DataFrame) -> dict[str, Any]:
    """Compare copied cells independent of Polars dtype inference."""
    return {"columns": frame.columns, "rows": list(frame.iter_rows())}


def test_consolidate_golden_preserves_every_layer_field_and_formula(
    cfg: Config, source_workbook: Path, tmp_path: Path
) -> None:
    """Migrate all values, controls, logs, formulas, and report metrics."""
    output = tmp_path / "persistent.xlsx"
    before_hash = _sha256(source_workbook)
    before_mtime = source_workbook.stat().st_mtime_ns
    assert run(cfg, str(source_workbook), str(output)) == output

    assert _sha256(source_workbook) == before_hash
    assert source_workbook.stat().st_mtime_ns == before_mtime
    expected_names = {
        "raw_sponsor",
        "raw_family",
        "raw_child",
        "final_sponsor",
        "final_family",
        "final_child",
        *_AUXILIARY_SHEETS,
    }
    assert set(list_sheets(output)) == expected_names
    assert not any(
        name.startswith("adj_") or name.endswith("_submissions")
        for name in expected_names
    )

    source_frames = _golden_frames()
    for entity in ("sponsor", "family", "child"):
        expected = build_final_frame(
            entity, source_frames[f"adj_{entity}"].to_dicts()
        ).to_dicts()
        actual = read_final(cfg, entity, workbook=output)
        assert actual == expected, (
            f"every normalized {entity} field must survive"
        )
    assert "not-source" not in {
        row["zite_record_id"]
        for row in read_final(cfg, "child", workbook=output)
    }

    for sheet in (
        "raw_sponsor",
        "raw_family",
        "raw_child",
        *_AUXILIARY_SHEETS,
    ):
        actual = read_sheet(output, sheet)
        assert actual is not None
        assert _copied_frame_values(actual) == _copied_frame_values(
            source_frames[sheet]
        )

    children = read_final(cfg, "child", workbook=output)
    formula_book = load_workbook(output, read_only=True, data_only=False)
    value_book = load_workbook(output, read_only=True, data_only=True)
    try:
        assert formula_book.read_only and value_book.read_only
        worksheet = formula_book["final_child"]
        values = value_book["final_child"]
        headers = [cell.value for cell in next(worksheet.iter_rows())]
        dob_column = headers.index("dob") + 1
        age_column = headers.index("age_at_xmas") + 1
        for row_number, child in enumerate(children, start=2):
            formula = worksheet.cell(row_number, age_column).value
            cached = values.cell(row_number, age_column).value
            if not child["dob"]:
                assert formula is None
                assert cached is None
                continue
            assert formula == (
                f"=DATEDIF({get_column_letter(dob_column)}{row_number}, "
                'DATE(2026,12,25), "y")'
            )
            assert cached == child["age_at_xmas"]
    finally:
        formula_book.close()
        value_book.close()

    report_path = Path(f"{output}.migration.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["input"]["sha256_before"] == before_hash
    assert report["input"]["sha256_after"] == before_hash
    assert report["output"]["sha256"] == _sha256(output)
    assert report["entities"]["child"]["rows"] == 6
    assert report["entities"]["child"]["orphans"] == 1
    assert report["entities"]["child"]["ignored"] == 1
    assert report["entities"]["child"]["raw_missing"] == 1
    assert report["entities"]["family"]["rows"] == 4
    assert report["entities"]["family"]["ignored"] == 1
    assert report["entities"]["sponsor"]["raw_missing"] == 1
    assert report["entities"]["child"]["generated"]["info_sheet_true"] == 3
    assert report["entities"]["family"]["assignments"]["with_sponsor"] == 2
    assert (
        report["entities"]["sponsor"]["assignments"][
            "num_angels_assigned_total"
        ]
        == 2
    )
    assert report["entities"]["child"]["controls"]["status"] == {
        "audited": 2,
        "ignored": 1,
        "needs_clarification": 2,
        "new": 1,
    }
    assert report["pending_work"]["ai_children"] == 2
    assert set(report["pending_work"]) == {
        "ai_children",
        "email_addresses",
        "family_addresses",
    }
    assert report["backup"] is None
    generated_files = report["entities"]["child"]["generated_files"]
    assert generated_files["marked"] == 3
    assert generated_files["files_present"] == 0
    assert generated_files["files_missing"] == 3
    assert report["logs"]["duplicate_log"]["rows"] == 2
    for sheet in _AUXILIARY_SHEETS[:-1]:
        assert len(report["logs"][sheet]["sha256"]) == 64
    assert len(report["validation"]) >= 10


def test_repeated_runs_force_backup_and_row_removal_guard(
    cfg: Config, source_workbook: Path, tmp_path: Path
) -> None:
    """Refuse repeats, verify force backups, and retain final-row safety."""
    output = tmp_path / "persistent.xlsx"
    run(cfg, str(source_workbook), str(output))
    output_hash = _sha256(output)
    with pytest.raises(ConsolidationError, match="output already exists"):
        run(cfg, str(source_workbook), str(output))
    assert _sha256(output) == output_hash

    with pytest.raises(ConsolidationError, match="requires --backup"):
        run(cfg, str(source_workbook), str(output), force=True)
    empty_backup = tmp_path / "empty.xlsx"
    empty_backup.write_bytes(b"")
    with pytest.raises(ConsolidationError, match="not a readable workbook"):
        run(
            cfg,
            str(source_workbook),
            str(output),
            force=True,
            backup_path=str(empty_backup),
        )
    with pytest.raises(ConsolidationError, match="backup path must differ"):
        run(
            cfg,
            str(source_workbook),
            str(output),
            force=True,
            backup_path=str(source_workbook),
        )

    backup = tmp_path / "verified-copy.xlsx"
    shutil.copy2(output, backup)
    custom_report = tmp_path / "reports" / "conversion.json"
    assert (
        run(
            cfg,
            str(source_workbook),
            str(output),
            force=True,
            backup_path=str(backup),
            report_path=str(custom_report),
        )
        == output
    )
    assert custom_report.is_file()
    assert _sha256(source_workbook) == _sha256(source_workbook)

    current_backup = tmp_path / "current-output.xlsx"
    shutil.copy2(output, current_backup)
    reduced = tmp_path / "reduced.xlsx"
    reduced_frames = _golden_frames()
    reduced_frames["adj_sponsor"] = build_final_frame(
        "sponsor",
        [
            row
            for row in reduced_frames["adj_sponsor"].to_dicts()
            if row["sponsor_id"] != 3
        ],
    )
    _write_workbook(reduced, reduced_frames)
    failed_report = tmp_path / "failed.json"
    before_failed_overwrite = _sha256(output)
    with pytest.raises(ConsolidationError, match="row-removal guard"):
        run(
            cfg,
            str(reduced),
            str(output),
            force=True,
            backup_path=str(current_backup),
            report_path=str(failed_report),
        )
    assert _sha256(output) == before_failed_overwrite
    assert not failed_report.exists()


def test_force_on_new_output_still_requires_verified_backup(
    cfg: Config, source_workbook: Path, tmp_path: Path
) -> None:
    """Force never serves as a backup-free overwrite shortcut."""
    output = tmp_path / "new.xlsx"
    with pytest.raises(ConsolidationError, match="requires --backup"):
        run(cfg, str(source_workbook), str(output), force=True)
    assert not output.exists()


def test_projected_only_and_sparse_sources_fail_without_output(
    cfg: Config, tmp_path: Path
) -> None:
    """Never speculate from projected finals or incomplete dense rows."""
    projected = tmp_path / "projected.xlsx"
    projected_frames = _golden_frames()
    for entity in ("sponsor", "family", "child"):
        del projected_frames[f"adj_{entity}"]
    _write_workbook(projected, projected_frames)
    projected_output = tmp_path / "projected-output.xlsx"
    with pytest.raises(ConsolidationError, match="audit-migrate"):
        run(cfg, str(projected), str(projected_output))
    assert not projected_output.exists()
    assert not Path(f"{projected_output}.migration.json").exists()

    sparse = tmp_path / "sparse.xlsx"
    sparse_frames = _golden_frames()
    sparse_frames["adj_child"] = sparse_frames["adj_child"].select(
        "zite_record_id", "first_name", "dob"
    )
    _write_workbook(sparse, sparse_frames)
    sparse_output = tmp_path / "sparse-output.xlsx"
    with pytest.raises(ConsolidationError, match="not a full dense store"):
        run(cfg, str(sparse), str(sparse_output))
    assert not sparse_output.exists()
    assert not Path(f"{sparse_output}.migration.json").exists()


def test_source_output_alias_and_required_raw_fail_closed(
    cfg: Config, source_workbook: Path, tmp_path: Path
) -> None:
    """Protect input aliases and require the complete raw layer."""
    with pytest.raises(ConsolidationError, match="different paths"):
        run(cfg, str(source_workbook), str(source_workbook))

    missing_raw = tmp_path / "missing-raw.xlsx"
    frames = _golden_frames()
    del frames["raw_family"]
    _write_workbook(missing_raw, frames)
    output = tmp_path / "missing-raw-output.xlsx"
    with pytest.raises(ConsolidationError, match="requires full raw_family"):
        run(cfg, str(missing_raw), str(output))
    assert not output.exists()
