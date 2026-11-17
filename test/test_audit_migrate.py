"""Tests for the one-time v2 workbook bootstrap (audit_architecture.md §11)."""

from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.utils import get_column_letter
import polars as pl
import pytest

from ccat.audit import AuditError
from ccat.audit.migrate import run
from ccat.audit.store import (
    FINAL_CHILD_COLUMNS,
    STATUS_AUDITED,
    STATUS_NEEDS_CLARIFICATION,
    read_final,
)
from ccat.config import Config
from ccat.io import list_sheets, read_sheet
from conftest import load_test_cfg


def _old_raw_child(record_id: str, angel_id: str | None) -> dict[str, Any]:
    return {
        "zite_record_id": record_id,
        "zite_created_at": "2026-02-01T09:00:00Z",
        "created": "2026-02-01T09:00:00Z",
        "family_id": 1 if angel_id else None,
        "angel_id": angel_id,
        "first_name": "Kiddo",
        "primary_email": "fam@example.com",
        "dob": "2018-05-10",
        "age_reported": 7,
        "gender": "Male",
        "need_1": "winter coat taht fits",
        "shoe_size": "13",
        "age_at_xmas": 8,
        "source": "web",
    }


def _write_legacy_sheets(
    path: Path,
    _module: str,
    sheets: dict[str, pl.DataFrame],
    xmas: object = None,
) -> None:
    """Write arbitrary v1 sheets without current persistent-store guards."""
    del xmas
    workbook = load_workbook(path) if path.is_file() else Workbook()
    try:
        default = workbook.active
        if default is not None and default.title == "Sheet":
            workbook.remove(default)
        for name, frame in sheets.items():
            if name in workbook.sheetnames:
                del workbook[name]
            worksheet = workbook.create_sheet(name)
            worksheet.append(list(frame.columns))
            for row in frame.iter_rows():
                worksheet.append(list(row))
        workbook.save(path)
    finally:
        workbook.close()


def _build_old_workbook(path: Path, cfg: Config) -> None:
    """Write an old-system workbook: raw + sparse adj + finals."""
    _write_legacy_sheets(
        path,
        "acquire",
        {
            "raw_child": pl.DataFrame(
                [
                    _old_raw_child("c1", "1A"),
                    _old_raw_child("c2", "1B"),
                ],
                schema={
                    "zite_record_id": pl.String,
                    "zite_created_at": pl.String,
                    "created": pl.String,
                    "family_id": pl.Int64,
                    "angel_id": pl.String,
                    "first_name": pl.String,
                    "primary_email": pl.String,
                    "dob": pl.String,
                    "age_reported": pl.Int64,
                    "gender": pl.String,
                    "need_1": pl.String,
                    "shoe_size": pl.String,
                    "age_at_xmas": pl.Int64,
                    "source": pl.String,
                },
            ),
            "raw_family": pl.DataFrame(
                [
                    {
                        "family_id": 1,
                        "zite_record_id": "rec_f1",
                        "primary_email": "fam@example.com",
                        "first_name": "Jane",
                        "last_name": "Doe",
                        "gift_pickup_location": "TSA (Hancock)",
                        "created": "2026-01-05T10:00:00Z",
                    }
                ],
                schema={
                    "family_id": pl.Int64,
                    "zite_record_id": pl.String,
                    "primary_email": pl.String,
                    "first_name": pl.String,
                    "last_name": pl.String,
                    "gift_pickup_location": pl.String,
                    "created": pl.String,
                },
            ),
            "raw_sponsor": pl.DataFrame(
                [
                    {
                        "sponsor_id": 1,
                        "zite_record_id": "rec_s1",
                        "create": "2026-01-01T10:00:00Z",
                        "email": "sponsor@example.com",
                        "sponsor_type": "Individual",
                        "num_children": 2,
                        "age_preference": "Any Age",
                        "gender_preference": "Any Gender",
                        "dropoff_location": None,
                        "first_name": "Mary",
                        "last_name": "Smith",
                        "pref_age_min": 0,
                        "pref_age_max": 18,
                    }
                ],
                schema={
                    "sponsor_id": pl.Int64,
                    "zite_record_id": pl.String,
                    "create": pl.String,
                    "email": pl.String,
                    "sponsor_type": pl.String,
                    "num_children": pl.Int64,
                    "age_preference": pl.String,
                    "gender_preference": pl.String,
                    "dropoff_location": pl.String,
                    "first_name": pl.String,
                    "last_name": pl.String,
                    "pref_age_min": pl.Int64,
                    "pref_age_max": pl.Int64,
                },
            ),
        },
    )
    # Sparse adj_child from the old screening: null = "same as raw".
    _write_legacy_sheets(
        path,
        "screen",
        {
            "adj_child": pl.DataFrame(
                [
                    {
                        "zite_record_id": "c1",
                        "need_1": "winter coat that fits",
                        "age_at_xmas": "8",
                        "potential_anomoly": "age_reported 7 differs from 8",
                    }
                ],
                schema={
                    "zite_record_id": pl.String,
                    "need_1": pl.String,
                    "age_at_xmas": pl.String,
                    "potential_anomoly": pl.String,
                },
            )
        },
    )
    # Old final_child: raw ⊕ adj ⊕ human audit edits, Boolean controls.
    _write_legacy_sheets(
        path,
        "audit",
        {
            "final_child": pl.DataFrame(
                [
                    {
                        "zite_record_id": "c1",
                        "family_id": 1,
                        "angel_id": "1A",
                        "first_name": "Kiddo",
                        "primary_email": "fam@example.com",
                        "dob": "2018-05-10",
                        "age_at_xmas": 8,
                        "gender": "Male",
                        "need_1": "winter coat that fits",
                        "shoe_size": "13",
                        "potential_anomoly": "age_reported 7 differs from 8",
                        "special_needs": True,
                        "audited": True,
                        "require_clarification": None,
                        "audited_at": "2026-11-01T00:00:00+00:00",
                        "generated_info_sheet": False,
                    },
                    {
                        "zite_record_id": "c2",
                        "family_id": 1,
                        "angel_id": "1B",
                        "first_name": "Kiddo Two",
                        "primary_email": "fam@example.com",
                        "dob": "2018-05-10",
                        "age_at_xmas": 8,
                        "gender": "Female",
                        "need_1": None,
                        "shoe_size": None,
                        "potential_anomoly": None,
                        "special_needs": False,
                        "audited": False,
                        "require_clarification": "call about shoe size",
                        "audited_at": "2026-11-02T00:00:00+00:00",
                        "generated_info_sheet": True,
                    },
                ],
                schema={
                    "zite_record_id": pl.String,
                    "family_id": pl.Int64,
                    "angel_id": pl.String,
                    "first_name": pl.String,
                    "primary_email": pl.String,
                    "dob": pl.String,
                    "age_at_xmas": pl.Int64,
                    "gender": pl.String,
                    "need_1": pl.String,
                    "shoe_size": pl.String,
                    "potential_anomoly": pl.String,
                    "special_needs": pl.Boolean,
                    "audited": pl.Boolean,
                    "require_clarification": pl.String,
                    "audited_at": pl.String,
                    "generated_info_sheet": pl.Boolean,
                },
            )
        },
        xmas=cfg.xmas,
    )
    _write_legacy_sheets(
        path,
        "match",
        {
            "final_family": pl.DataFrame(
                [
                    {
                        "family_id": 1,
                        "primary_email": "fam@example.com",
                        "assigned_sponsor_id": 1,
                        "assigned_angel_ids": "1A,1B",
                    }
                ],
                schema={
                    "family_id": pl.Int64,
                    "primary_email": pl.String,
                    "assigned_sponsor_id": pl.Int64,
                    "assigned_angel_ids": pl.String,
                },
            ),
            "final_sponsor": pl.DataFrame(
                [
                    {
                        "sponsor_id": 1,
                        "email": "sponsor@example.com",
                        "assigned_angel_ids": "1A,1B",
                        "num_angels_assigned": 2,
                    }
                ],
                schema={
                    "sponsor_id": pl.Int64,
                    "email": pl.String,
                    "assigned_angel_ids": pl.String,
                    "num_angels_assigned": pl.Int64,
                },
            ),
        },
    )


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """Like the conftest cfg, with the clarify status store redirected.

    The migration reads the clarify status store for the cutover
    marking rules; tests must never touch the real ``output/`` store.
    """
    base = load_test_cfg(
        excel_file=str(tmp_path / "wb.xlsx"),
        log_file=str(tmp_path / "ccat.log"),
        match_report_csv=str(tmp_path / "report.csv"),
    )
    return base.model_copy(
        update={
            "clarify": base.clarify.model_copy(
                update={
                    "output_dir": str(tmp_path / "emails"),
                    "clarifications_csv": str(tmp_path / "clarifications.csv"),
                }
            )
        }
    )


@pytest.fixture
def old_workbook(cfg: Config, tmp_path: Path) -> Path:
    """One old-system workbook with formulas in final_child."""
    path = tmp_path / "old.xlsx"
    _build_old_workbook(path, cfg)
    return path


def test_migrate_builds_direct_full_persistent_finals(
    cfg: Config, old_workbook: Path
) -> None:
    """Migrate data and controls directly without legacy output layers."""
    run(cfg, from_path=str(old_workbook))
    children = {row["zite_record_id"]: row for row in read_final(cfg, "child")}
    # c1: old final data columns are the dense truth (raw ⊕ adj ⊕ edits)
    c1 = children["c1"]
    assert c1["need_1"] == "winter coat that fits"
    assert c1["potential_anomoly"] == "age_reported 7 differs from 8"
    assert c1["special_needs"] == "other: unspecified"  # D4 mapping
    assert c1["status"] == STATUS_AUDITED
    assert "audited" not in c1
    assert c1["screened"] is True  # had an old adj row
    assert c1["generated_info_sheet"] is False
    assert c1["age_at_xmas"] == 8
    assert c1["family_id"] == 1
    # c2: unaudited but carries the note and the generated flag
    c2 = children["c2"]
    assert c2["first_name"] == "Kiddo Two"
    assert c2["special_needs"] is None
    assert c2["clarification_notes"] == "call about shoe size"
    assert c2["generated_info_sheet"] is True
    assert c2["screened"] is False
    assert c2["status"] == STATUS_NEEDS_CLARIFICATION
    assert c2["age_at_xmas"] == 8

    # Families/sponsors: seeded from raw, assignments carried over.
    family = read_final(cfg, "family")[0]
    assert family["primary_email"] == "fam@example.com"
    assert family["status"] == "new"
    sponsor = read_final(cfg, "sponsor")[0]
    assert sponsor["pref_age_min"] == 0
    assert sponsor["assigned_angel_ids"] == "1A,1B"
    assert sponsor["num_angels_assigned"] == 2
    assert family["num_angels_in_family"] == 2

    final_child = read_sheet(cfg.excel_file, "final_child")
    assert final_child is not None
    assert final_child.height == 2
    assert list(final_child.columns) == list(FINAL_CHILD_COLUMNS)
    assert final_child["status"].to_list() == [
        STATUS_AUDITED,
        STATUS_NEEDS_CLARIFICATION,
    ]
    names = set(list_sheets(cfg.excel_file))
    assert names == {
        "raw_sponsor",
        "raw_family",
        "raw_child",
        "dob_conflict_log",
        "final_child",
        "final_family",
        "final_sponsor",
    }
    assert not any(
        name.startswith("adj_") or name.endswith("_submissions")
        for name in names
    )

    formula_book = load_workbook(
        cfg.excel_file, read_only=True, data_only=False
    )
    value_book = load_workbook(cfg.excel_file, read_only=True, data_only=True)
    try:
        worksheet = formula_book["final_child"]
        headers = [cell.value for cell in next(worksheet.iter_rows())]
        age_column = headers.index("age_at_xmas") + 1
        dob_column = headers.index("dob") + 1
        assert worksheet.cell(2, age_column).value == (
            f'=DATEDIF({get_column_letter(dob_column)}2, DATE(2026,12,25), "y")'
        )
        assert value_book["final_child"].cell(2, age_column).value == 8
    finally:
        formula_book.close()
        value_book.close()

    # The raw layer was copied verbatim.
    old_raw = read_sheet(old_workbook, "raw_child")
    new_raw = read_sheet(cfg.excel_file, "raw_child")
    assert old_raw is not None and new_raw is not None
    assert old_raw.equals(new_raw)


def test_migrate_refuses_non_empty_target(
    cfg: Config, old_workbook: Path
) -> None:
    """The bootstrap only writes a fresh workbook (§11.1)."""
    run(cfg, from_path=str(old_workbook))
    with pytest.raises(AuditError, match="already contains"):
        run(cfg, from_path=str(old_workbook))
    # --force re-creates them from the source
    run(cfg, from_path=str(old_workbook), force=True)


def test_migrate_never_writes_source(cfg: Config, old_workbook: Path) -> None:
    """The --from workbook is never modified (§11.6)."""
    from ccat.io import snapshot_sheets

    before = snapshot_sheets(
        old_workbook, ["raw_child", "adj_child", "final_child"]
    )
    stamp = old_workbook.stat().st_mtime_ns
    run(cfg, from_path=str(old_workbook))
    after = snapshot_sheets(
        old_workbook, ["raw_child", "adj_child", "final_child"]
    )
    assert before == after
    assert old_workbook.stat().st_mtime_ns == stamp


def test_migrate_requires_source_and_raw(cfg: Config, tmp_path: Path) -> None:
    """Missing source file or raw sheets fail with actionable errors."""
    with pytest.raises(AuditError, match="source workbook not found"):
        run(cfg, from_path=str(tmp_path / "missing.xlsx"))
    empty = tmp_path / "empty.xlsx"
    _write_legacy_sheets(
        empty, "match", {"match_report": pl.DataFrame(schema={"k": pl.String})}
    )
    with pytest.raises(AuditError, match="raw_sponsor"):
        run(cfg, from_path=str(empty))


def test_migrate_sparse_adj_only_child_gets_in_progress(
    cfg: Config, old_workbook: Path
) -> None:
    """Status is computed from the migrated record vs the raw baseline (§11.3)."""
    run(cfg, from_path=str(old_workbook))
    children = {row["zite_record_id"]: row for row in read_final(cfg, "child")}
    # c1: audited checkbox wins
    assert children["c1"]["status"] == STATUS_AUDITED
    # c2: unaudited with a note
    assert children["c2"]["status"] == STATUS_NEEDS_CLARIFICATION


def test_migrate_marks_sponsored_children_audited(
    cfg: Config, old_workbook: Path
) -> None:
    """Cutover rule: every child with an assigned sponsor is audited."""
    # The module cfg fixture redirects the clarify store: the real
    # status store is never read or written by tests.
    run(cfg, from_path=str(old_workbook))
    children = {row["zite_record_id"]: row for row in read_final(cfg, "child")}
    # c2 is sponsored (family 1 assigned) and carries a note, but its
    # family never replied: the outstanding note beats the audited mark.
    assert children["c2"]["status"] == STATUS_NEEDS_CLARIFICATION
    assert children["c2"]["status"] != STATUS_AUDITED

    # A child whose family replied: the sponsored mark holds.
    from ccat.clarify.status import FamilyStatus, store_statuses

    store_statuses(cfg, {1: FamilyStatus(contacted=True, replied=True)})
    run(cfg, from_path=str(old_workbook), force=True)
    children = {row["zite_record_id"]: row for row in read_final(cfg, "child")}
    assert children["c2"]["status"] == STATUS_AUDITED
    assert children["c2"]["status"] == STATUS_AUDITED
    # never-stamped audited rows get the migration timestamp
    assert children["c2"]["audited_at"] is not None

    # an unsponsored child with screening corrections: in_progress
    _write_legacy_sheets(
        old_workbook,
        "acquire",
        {
            "raw_child": pl.DataFrame(
                [
                    _old_raw_child("c1", "1A"),
                    _old_raw_child("c2", "1B"),
                    {**_old_raw_child("c3", None), "family_id": None},
                ],
                schema={
                    "zite_record_id": pl.String,
                    "zite_created_at": pl.String,
                    "created": pl.String,
                    "family_id": pl.Int64,
                    "angel_id": pl.String,
                    "first_name": pl.String,
                    "primary_email": pl.String,
                    "dob": pl.String,
                    "age_reported": pl.Int64,
                    "gender": pl.String,
                    "need_1": pl.String,
                    "shoe_size": pl.String,
                    "age_at_xmas": pl.Int64,
                    "source": pl.String,
                },
            )
        },
    )
    _write_legacy_sheets(
        old_workbook,
        "screen",
        {
            "adj_child": pl.DataFrame(
                [
                    {
                        "zite_record_id": "c3",
                        "need_1": "corrected coat",
                        "age_at_xmas": "8",
                    }
                ],
                schema={
                    "zite_record_id": pl.String,
                    "need_1": pl.String,
                    "age_at_xmas": pl.String,
                },
            )
        },
    )
    run(cfg, from_path=str(old_workbook), force=True)
    children = {row["zite_record_id"]: row for row in read_final(cfg, "child")}
    # c3: orphan with screening corrections; orphan clarification wins.
    assert children["c3"]["status"] != STATUS_AUDITED
    assert children["c3"]["status"] == STATUS_NEEDS_CLARIFICATION
    assert "No matching family registration" in (
        children["c3"]["clarification_notes"] or ""
    )


def test_migrate_groups_multi_submission_family(
    cfg: Config, old_workbook: Path
) -> None:
    """Duplicate raw family submissions become one stable family entity."""
    _write_legacy_sheets(
        old_workbook,
        "acquire",
        {
            "raw_family": pl.DataFrame(
                [
                    {
                        "family_id": 1,
                        "zite_record_id": "rec_f1",
                        "primary_email": "fam@example.com",
                        "first_name": "Jane",
                        "last_name": "Doe",
                        "gift_pickup_location": "TSA (Hancock)",
                        "created": "2026-01-05T10:00:00Z",
                    },
                    {
                        "family_id": 1,
                        "zite_record_id": "rec_f2",
                        "primary_email": "fam@example.com",
                        "first_name": "Jane",
                        "last_name": "Doe",
                        "gift_pickup_location": "TSA (Hancock)",
                        "created": "2026-01-04T10:00:00Z",
                    },
                ]
            )
        },
    )
    run(cfg, from_path=str(old_workbook), force=True)
    families = read_final(cfg, "family")
    assert len(families) == 1
    assert families[0]["family_id"] == 1
    assert families[0]["zite_record_id"] == "rec_f2"


def test_migrate_preserves_edits_raw_missing_and_logs(
    cfg: Config, old_workbook: Path
) -> None:
    """Legacy edits, raw-missing entities, logs, and report all survive."""
    _write_legacy_sheets(
        old_workbook,
        "audit",
        {
            "final_family": pl.DataFrame(
                [
                    {
                        "family_id": 1,
                        "zite_record_id": "rec_f1",
                        "primary_email": "corrected@example.com",
                        "phone": "(906) 555-0000",
                        "assigned_sponsor_id": 1,
                        "assigned_angel_ids": "1A,1B",
                    },
                    {
                        "family_id": 9,
                        "zite_record_id": "family-missing",
                        "primary_email": "missing@example.com",
                        "first_name": "Historical",
                        "assigned_sponsor_id": 1,
                        "num_angels_in_family": 3,
                        "audited": True,
                    },
                ]
            ),
            "final_sponsor": pl.DataFrame(
                [
                    {
                        "sponsor_id": 1,
                        "zite_record_id": "rec_s1",
                        "email": "corrected-sponsor@example.com",
                        "age_preference": "Middle School (11-13 yrs)",
                        "pref_age_min": 11,
                        "pref_age_max": 13,
                        "assigned_angel_ids": "1A,1B",
                        "num_angels_assigned": 2,
                    },
                    {
                        "sponsor_id": 9,
                        "zite_record_id": "sponsor-missing",
                        "email": "missing-sponsor@example.com",
                        "assigned_angel_ids": "9Z",
                        "num_angels_assigned": 1,
                        "audited": True,
                    },
                ]
            ),
            "final_child": pl.DataFrame(
                [
                    {
                        "zite_record_id": "c-raw-missing",
                        "family_id": 9,
                        "angel_id": "9Z",
                        "first_name": "Historical",
                        "dob": "2012-01-01",
                        "age_at_xmas": 14,
                        "generated_info_sheet": True,
                        "audited": True,
                        "audited_at": "2026-11-03T00:00:00+00:00",
                    }
                ]
            ),
        },
    )
    _write_legacy_sheets(
        old_workbook,
        "match",
        {
            "duplicate_log": pl.DataFrame([{"zite_record_id": "rec_f2"}]),
            "email_validation_log": pl.DataFrame(
                [{"email": "corrected@example.com", "status": "valid"}]
            ),
            "address_validation_log": pl.DataFrame(
                [{"query": "1 Main St", "status": "found"}]
            ),
            "match_report": pl.DataFrame([{"family_id": 1, "reason": "none"}]),
        },
    )
    run(cfg, from_path=str(old_workbook), force=True)
    families = {row["family_id"]: row for row in read_final(cfg, "family")}
    sponsors = {row["sponsor_id"]: row for row in read_final(cfg, "sponsor")}
    children = {row["zite_record_id"]: row for row in read_final(cfg, "child")}
    assert families[1]["phone"] == "(906) 555-0000"
    assert families[1]["primary_email"] == "corrected@example.com"
    assert families[9]["assigned_sponsor_id"] == 1
    assert sponsors[1]["pref_age_min"] == 11
    assert sponsors[9]["assigned_angel_ids"] == "9Z"
    assert children["c-raw-missing"]["generated_info_sheet"] is True
    assert children["c-raw-missing"]["status"] == "audited"
    for sheet in (
        "duplicate_log",
        "email_validation_log",
        "address_validation_log",
        "match_report",
    ):
        frame = read_sheet(cfg.excel_file, sheet)
        assert frame is not None and frame.height == 1


def test_migrate_rejects_generated_angel_conflict(
    cfg: Config, old_workbook: Path
) -> None:
    """Raw/final angel mismatch with generated state is explicit, not silent."""
    _write_legacy_sheets(
        old_workbook,
        "acquire",
        {
            "raw_child": pl.DataFrame(
                [
                    _old_raw_child("c1", "1A"),
                    {**_old_raw_child("c2", "9Z")},
                ]
            )
        },
    )
    with pytest.raises(AuditError, match="conflicting raw angel_id"):
        run(cfg, from_path=str(old_workbook), force=True)


def test_migrate_rejects_mixed_legacy_target(cfg: Config) -> None:
    """A target containing adj/submissions never receives new finals."""
    target = Path(cfg.excel_file)
    _write_legacy_sheets(
        target,
        "migrate",
        {
            "adj_child": pl.DataFrame({"zite_record_id": ["stale"]}),
            "family_submissions": pl.DataFrame({"family_id": [1]}),
        },
    )
    with pytest.raises(AuditError, match="retired legacy layer"):
        run(
            cfg,
            from_path=str(target.parent / "missing-source.xlsx"),
        )
