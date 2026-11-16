"""Tests for the one-time v2 workbook bootstrap (audit_architecture.md §11)."""

from pathlib import Path
from typing import Any

import polars as pl
import pytest

from ccat.audit import AuditError
from ccat.audit.migrate import run
from ccat.audit.store import (
    FINAL_CHILD_COLUMNS,
    STATUS_AUDITED,
    STATUS_NEEDS_CLARIFICATION,
    read_adj,
)
from ccat.config import Config
from ccat.io import read_sheet, write_sheets
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


def _build_old_workbook(path: Path, cfg: Config) -> None:
    """Write an old-system workbook: raw + sparse adj + finals."""
    write_sheets(
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
    write_sheets(
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
    write_sheets(
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
    write_sheets(
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


def test_migrate_builds_dense_adj_and_projects(
    cfg: Config, old_workbook: Path
) -> None:
    """Dense adj correctness, control backfill, projections (§11.3–§11.5)."""
    run(cfg, from_path=str(old_workbook))
    children = {row["zite_record_id"]: row for row in read_adj(cfg, "child")}
    # c1: old final data columns are the dense truth (raw ⊕ adj ⊕ edits)
    c1 = children["c1"]
    assert c1["need_1"] == "winter coat that fits"
    assert c1["potential_anomoly"] == "age_reported 7 differs from 8"
    assert c1["special_needs"] == "other: unspecified"  # D4 mapping
    assert c1["audited"] is True
    assert c1["status"] == STATUS_AUDITED
    assert c1["screened"] is True  # had an old adj row
    assert c1["generated_info_sheet"] is False
    assert c1["age_at_xmas"] == 8
    assert c1["family_id"] == 1
    # c2: unaudited but carries the note and the generated flag
    c2 = children["c2"]
    assert c2["first_name"] == "Kiddo Two"
    assert c2["special_needs"] is None
    assert c2["require_clarification"] == "call about shoe size"
    assert c2["generated_info_sheet"] is True
    assert c2["screened"] is False
    assert c2["status"] == STATUS_NEEDS_CLARIFICATION
    assert c2["age_at_xmas"] == 8

    # Families/sponsors: seeded from raw, assignments carried over.
    family = read_adj(cfg, "family")[0]
    assert family["primary_email"] == "fam@example.com"
    assert family["status"] == "new"
    sponsor = read_adj(cfg, "sponsor")[0]
    assert sponsor["pref_age_min"] == 0
    assert sponsor["assigned_angel_ids"] == "1A,1B"
    assert sponsor["num_angels_assigned"] == 2
    assert family["num_angels_in_family"] == 2

    # Finals projected; submission sheets empty until the next acquire.
    final_child = read_sheet(cfg.excel_file, "final_child")
    assert final_child is not None
    assert final_child.height == 2
    assert list(final_child.columns) == list(FINAL_CHILD_COLUMNS)
    assert final_child["status"].to_list() == [
        STATUS_AUDITED,
        STATUS_NEEDS_CLARIFICATION,
    ]
    submissions = read_sheet(cfg.excel_file, "family_submissions")
    assert submissions is not None and submissions.height == 0

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
    write_sheets(
        empty, "match", {"match_report": pl.DataFrame(schema={"k": pl.String})}
    )
    with pytest.raises(AuditError, match="raw_sponsor"):
        run(cfg, from_path=str(empty))


def test_migrate_sparse_adj_only_child_gets_in_progress(
    cfg: Config, old_workbook: Path
) -> None:
    """Status is computed from the migrated record vs the raw baseline (§11.3)."""
    run(cfg, from_path=str(old_workbook))
    children = {row["zite_record_id"]: row for row in read_adj(cfg, "child")}
    # c1: audited checkbox wins
    assert children["c1"]["status"] == STATUS_AUDITED
    # c2: unaudited with a note
    assert children["c2"]["status"] == STATUS_NEEDS_CLARIFICATION


def test_migrate_marks_sponsored_children_audited(
    cfg: Config, old_workbook: Path
) -> None:
    """Cutover rule: every child with an assigned sponsor is audited."""
    from ccat.audit.store import STATUS_IN_PROGRESS
    from ccat.io import write_sheets

    # The module cfg fixture redirects the clarify store: the real
    # status store is never read or written by tests.
    run(cfg, from_path=str(old_workbook))
    children = {row["zite_record_id"]: row for row in read_adj(cfg, "child")}
    # c2 is sponsored (family 1 assigned) and carries a note, but its
    # family never replied: the outstanding note beats the audited mark.
    assert children["c2"]["status"] == STATUS_NEEDS_CLARIFICATION
    assert children["c2"]["audited"] is False

    # A child whose family replied: the sponsored mark holds.
    from ccat.clarify.status import FamilyStatus, store_statuses

    store_statuses(cfg, {1: FamilyStatus(contacted=True, replied=True)})
    run(cfg, from_path=str(old_workbook), force=True)
    children = {row["zite_record_id"]: row for row in read_adj(cfg, "child")}
    assert children["c2"]["audited"] is True
    assert children["c2"]["status"] == STATUS_AUDITED
    # never-stamped audited rows get the migration timestamp
    assert children["c2"]["audited_at"] is not None

    # an unsponsored child with screening corrections: in_progress
    import polars as pl

    write_sheets(
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
    write_sheets(
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
    children = {row["zite_record_id"]: row for row in read_adj(cfg, "child")}
    # c3: orphan (no family -> no sponsor), screened corrections only
    assert children["c3"]["audited"] is False
    assert children["c3"]["status"] == STATUS_IN_PROGRESS
