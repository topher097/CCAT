"""Tests for the audit store — module 3 data layer (audit_architecture.md §5–§7)."""

from pathlib import Path
from typing import Any

import polars as pl
import pytest

from ccat.audit.store import (
    ADJ_CHILD_COLUMNS,
    ENTITY_SPECS,
    FINAL_CHILD_COLUMNS,
    FINAL_FAMILY_COLUMNS,
    STATUS_AUDITED,
    STATUS_IGNORED,
    STATUS_IN_PROGRESS,
    STATUS_NEEDS_CLARIFICATION,
    STATUS_NEW,
    as_bool,
    build_adj_frame,
    coalesce_family_group,
    combine_child_group,
    compute_status,
    ensure_adj_sheets,
    group_submissions,
    load_audit_data,
    merge_anomoly,
    dob_conflict_notes,
    project_final,
    read_adj,
    reduce_ignored,
    save_child,
    save_family,
    save_sponsor,
    status_of,
)
from ccat.config import Config
from ccat.io import read_sheet, write_sheets
from ccat.io import _final_child_formula_caches

STATUS_TABLE = [
    # ignore, audited, note, edits -> expected
    (True, True, "n", True, STATUS_IGNORED),
    (True, False, None, False, STATUS_IGNORED),
    (False, True, "n", True, STATUS_AUDITED),
    (False, True, None, False, STATUS_AUDITED),
    (False, False, "note", True, STATUS_NEEDS_CLARIFICATION),
    (False, False, "note", False, STATUS_NEEDS_CLARIFICATION),
    (False, False, None, True, STATUS_IN_PROGRESS),
    (False, False, None, False, STATUS_NEW),
]


@pytest.mark.parametrize("ignore,audited,note,edits,expected", STATUS_TABLE)
def test_compute_status_full_precedence_table(
    ignore: bool, audited: bool, note: bool, edits: bool, expected: str
) -> None:
    """The §5.3 precedence: ignored > audited > note > edits > new."""
    assert compute_status(edits, note, audited, ignore) == expected


def test_compute_status_dynamic_recompute_on_audited_deselect() -> None:
    """Unchecking audited recomputes from note/edit state (§5.3)."""
    state = {
        "has_edits": True,
        "has_note": True,
        "audited": True,
        "ignore": False,
    }
    assert compute_status(**state) == STATUS_AUDITED
    state["audited"] = False
    assert compute_status(**state) == STATUS_NEEDS_CLARIFICATION
    state["has_note"] = False
    assert compute_status(**state) == STATUS_IN_PROGRESS
    state["has_edits"] = False
    assert compute_status(**state) == STATUS_NEW


def _row(**overrides: Any) -> dict[str, Any]:
    """One adj-like row for status tests."""
    base: dict[str, Any] = {column: None for column in ADJ_CHILD_COLUMNS}
    base.update(overrides)
    return base


def test_status_of_excludes_special_needs_and_computed() -> None:
    """Special-needs selection and computed columns never set in_progress."""
    baseline = {"dob": "2018-05-10", "special_needs": None, "age_at_xmas": None}
    row = _row(
        dob="2018-05-10",
        special_needs="ADHD; other: speech delay",
        age_at_xmas=8,
        audited=False,
        ignore=False,
    )
    assert status_of(row, baseline, "child") == STATUS_NEW


def test_status_of_counts_data_edits_and_notes() -> None:
    """Adjudicable edits and notes derive in_progress/needs_clarification."""
    baseline = {"need_1": "raw", "require_clarification": None}
    row = _row(need_1="fixed", require_clarification=None)
    assert status_of(row, baseline, "child") == STATUS_IN_PROGRESS
    row = _row(need_1="raw", require_clarification="call the family")
    assert status_of(row, baseline, "child") == STATUS_NEEDS_CLARIFICATION


def test_coalesce_family_group_first_non_null_wins() -> None:
    """Family groups coalesce by created ascending (§6.1, D8)."""
    older = {"created": "2026-01-01", "first_name": "Ann", "phone": None}
    newer = {"created": "2026-02-01", "first_name": "Anna", "phone": "555"}
    merged = coalesce_family_group([newer, older])
    assert merged["first_name"] == "Ann"
    assert merged["phone"] == "555"
    assert merged["created"] == "2026-01-01"
    assert coalesce_family_group([]) == {}


def test_combine_child_group_most_recent_non_null_wins() -> None:
    """Child groups combine newest-first (§6.2, D8)."""
    latest = {"created": "2026-03-01", "need_1": "new coat", "desire_1": None}
    earlier = {"created": "2026-01-01", "need_1": None, "desire_1": "blocks"}
    combined = combine_child_group([earlier, latest])
    assert combined["need_1"] == "new coat"
    assert combined["desire_1"] == "blocks"
    assert combine_child_group([]) == {}


def test_group_submissions_keys_and_singletons() -> None:
    """Children group by (email, name); unkeyed rows stay singletons (§6)."""
    submissions = [
        {
            "primary_email": "a@x.com",
            "first_name": "Kid",
            "zite_record_id": "1",
        },
        {
            "primary_email": "a@x.com",
            "first_name": "Kid",
            "zite_record_id": "2",
        },
        {
            "primary_email": "a@x.com",
            "first_name": "Other",
            "zite_record_id": "3",
        },
        {"primary_email": None, "first_name": "Kid", "zite_record_id": "4"},
    ]
    groups = group_submissions(submissions, "child")
    assert len(groups[("a@x.com", "Kid")]) == 2
    assert len(groups[("a@x.com", "Other")]) == 1
    assert ("4",) in groups  # email-less rows are their own group
    family_groups = group_submissions(submissions, "family")
    assert len(family_groups[("a@x.com",)]) == 3


@pytest.mark.parametrize("entity", ["child", "family", "sponsor"])
def test_reduce_ignored_keeps_only_skeleton(entity: str) -> None:
    """Ignored rows keep key, zite id, names, email, and controls (§5.5)."""
    controls = (
        "status",
        "audited",
        "ignore",
        "require_clarification",
        "audited_at",
    ) + (("screened",) if entity == "child" else ())
    row: dict[str, Any] = {
        column: "keep" for column in ENTITY_SPECS[entity]["columns"]
    }
    row["ignore"] = True
    row["audited"] = False
    row["status"] = STATUS_IGNORED
    reduced = reduce_ignored(row, entity)
    retained = {
        "child": {
            "zite_record_id",
            "first_name",
            "primary_email",
            "potential_duplicate",
        },
        "family": {
            "family_id",
            "zite_record_id",
            "first_name",
            "last_name",
            "primary_email",
            "potential_duplicate",
        },
        "sponsor": {
            "sponsor_id",
            "zite_record_id",
            "first_name",
            "last_name",
            "email",
            "potential_duplicate",
        },
    }[entity]
    for column in ENTITY_SPECS[entity]["columns"]:
        if column in retained:
            assert reduced[column] == "keep", column
        elif column in controls:
            assert reduced[column] == row[column], column
        else:
            assert reduced[column] is None, column


def test_project_final_rule() -> None:
    """Projection drops ignored rows + pure controls, keeps the rest (§7.1)."""
    rows = [
        _row(
            zite_record_id="c1",
            status="audited",
            audited=True,
            ignore=False,
            screened=True,
            require_clarification=None,
            audited_at="2026-01-01T00:00:00+00:00",
            age_at_xmas=8,
        ),
        _row(zite_record_id="c2", ignore=True, audited=False, screened=False),
    ]
    frame = project_final(build_adj_frame("child", rows), FINAL_CHILD_COLUMNS)
    assert frame.height == 1
    assert frame["zite_record_id"].to_list() == ["c1"]
    assert list(frame.columns) == list(FINAL_CHILD_COLUMNS)
    assert "audited" not in frame.columns
    assert "ignore" not in frame.columns
    assert "screened" not in frame.columns
    assert "status" in frame.columns
    assert "require_clarification" in frame.columns
    assert "audited_at" in frame.columns
    assert "age_at_xmas" in frame.columns


def test_project_final_family_keeps_assignments() -> None:
    """final_family keeps assignment columns plus status (§7.1)."""
    row = {column: None for column in ENTITY_SPECS["family"]["columns"]}
    row.update(
        {
            "family_id": 1,
            "assigned_sponsor_id": 3,
            "assigned_angel_ids": "1A",
            "num_angels_in_family": 1,
            "status": "new",
            "audited": False,
            "ignore": False,
        }
    )
    frame = project_final(
        build_adj_frame("family", [row]), FINAL_FAMILY_COLUMNS
    )
    assert list(frame.columns) == list(FINAL_FAMILY_COLUMNS)
    saved = frame.to_dicts()[0]
    assert saved["assigned_sponsor_id"] == 3
    assert saved["status"] == "new"


# ---------------------------------------------------------------------------
# Workbook fixtures.
# ---------------------------------------------------------------------------


RAW_CHILD_SCHEMA = {
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
}

RAW_FAMILY_SCHEMA = {
    "family_id": pl.Int64,
    "zite_record_id": pl.String,
    "zite_created_at": pl.String,
    "created": pl.String,
    "primary_email": pl.String,
    "first_name": pl.String,
    "last_name": pl.String,
    "phone": pl.String,
    "gift_pickup_location": pl.String,
    "source": pl.String,
}

RAW_SPONSOR_SCHEMA = {
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
    "source": pl.String,
}


def raw_child_row(
    record_id: str, angel_id: str | None = "1A", family_id: int | None = 1
) -> dict[str, Any]:
    """One raw_child row for store tests."""
    return {
        "zite_record_id": record_id,
        "zite_created_at": "2026-02-01T09:00:00Z",
        "created": "2026-02-01T09:00:00Z",
        "family_id": family_id,
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


def seed_raw(
    cfg: Config,
    children: list[dict[str, Any]] | None = None,
    families: list[dict[str, Any]] | None = None,
    sponsors: list[dict[str, Any]] | None = None,
) -> None:
    """Write the three raw sheets (children default to a family of two + orphan)."""
    if children is None:
        children = [
            raw_child_row("c1"),
            raw_child_row("c2", "1B"),
            raw_child_row("orphan1", None, None),
        ]
    if families is None:
        families = [
            {
                "family_id": 1,
                "zite_record_id": "rec_f1",
                "zite_created_at": "2026-01-05T10:00:00Z",
                "created": "2026-01-05T10:00:00Z",
                "primary_email": "fam@example.com",
                "first_name": "Jane",
                "last_name": "Doe",
                "phone": "(906) 555-9876",
                "gift_pickup_location": "TSA (Hancock)",
                "source": "web",
            }
        ]
    if sponsors is None:
        sponsors = [
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
                "source": "web",
            }
        ]
    write_sheets(
        cfg.excel_file,
        "acquire",
        {
            "raw_child": pl.DataFrame(children, schema=RAW_CHILD_SCHEMA),
            "raw_family": pl.DataFrame(families, schema=RAW_FAMILY_SCHEMA),
            "raw_sponsor": pl.DataFrame(sponsors, schema=RAW_SPONSOR_SCHEMA),
        },
    )


def seed_submissions(cfg: Config) -> None:
    """Write prepared submission sheets: two family submissions, a child group.

    Mirrors what ``ccat screen`` now writes: cleaned rows with the
    derived IDs (family_id/angel_id/age_at_xmas), one row per
    submission.
    """
    family_submissions = pl.DataFrame(
        [
            {
                "family_id": 1,
                "zite_record_id": "rec_f1",
                "zite_created_at": "2026-01-05T10:00:00Z",
                "created": "2026-01-05T10:00:00Z",
                "primary_email": "fam@example.com",
                "first_name": "Jane",
                "last_name": "Doe",
            },
            {
                "family_id": 1,
                "zite_record_id": "rec_f2",
                "zite_created_at": "2026-01-20T10:00:00Z",
                "created": "2026-01-20T10:00:00Z",
                "primary_email": "fam@example.com",
                "first_name": "Jane",
                "last_name": "Doe",
                "phone": "(906) 555-0000",
            },
        ],
        schema={
            "family_id": pl.Int64,
            "zite_record_id": pl.String,
            "zite_created_at": pl.String,
            "created": pl.String,
            "primary_email": pl.String,
            "first_name": pl.String,
            "last_name": pl.String,
            "phone": pl.String,
        },
    )
    child_submissions = pl.DataFrame(
        [
            {
                "family_id": 1,
                "angel_id": "1B",
                "zite_record_id": "c2",
                "zite_created_at": "2026-02-01T09:00:00Z",
                "created": "2026-02-01T09:00:00Z",
                "primary_email": "fam@example.com",
                "first_name": "Kiddo",
                "dob": "2018-05-10",
                "need_1": "old coat",
                "desire_1": "blocks",
            },
            {
                "family_id": 1,
                "angel_id": "1A",
                "zite_record_id": "c1",
                "zite_created_at": "2026-02-10T09:00:00Z",
                "created": "2026-02-10T09:00:00Z",
                "primary_email": "fam@example.com",
                "first_name": "Kiddo",
                "dob": "2018-05-10",
                "need_1": "winter coat taht fits",
                "desire_1": None,
            },
        ],
        schema={
            "family_id": pl.Int64,
            "angel_id": pl.String,
            "zite_record_id": pl.String,
            "zite_created_at": pl.String,
            "created": pl.String,
            "primary_email": pl.String,
            "first_name": pl.String,
            "dob": pl.String,
            "need_1": pl.String,
            "desire_1": pl.String,
        },
    )
    write_sheets(
        cfg.excel_file,
        "screen",
        {
            "family_submissions": family_submissions,
            "child_submissions": child_submissions,
        },
    )


# ---------------------------------------------------------------------------
# ensure_adj_sheets (§7.4).
# ---------------------------------------------------------------------------


def test_ensure_adj_sheets_seeds_dense_rows(cfg: Config) -> None:
    """Every raw entity gets a dense adj row; controls default (§7.4)."""
    seed_raw(cfg)
    ensure_adj_sheets(cfg)
    children = read_adj(cfg, "child")
    assert {row["zite_record_id"] for row in children} == {
        "c1",
        "c2",
        "orphan1",
    }
    by_id = {row["zite_record_id"]: row for row in children}
    assert by_id["c1"]["need_1"] == "winter coat taht fits"
    assert by_id["c1"]["age_at_xmas"] == 8
    assert by_id["c1"]["status"] == STATUS_NEW
    assert by_id["c1"]["audited"] is False
    assert by_id["c1"]["screened"] is False
    assert by_id["c1"]["generated_info_sheet"] is False
    families = read_adj(cfg, "family")
    assert len(families) == 1
    assert families[0]["assigned_sponsor_id"] is None
    assert families[0]["status"] == STATUS_NEW
    sponsors = read_adj(cfg, "sponsor")
    assert len(sponsors) == 1
    assert sponsors[0]["pref_age_min"] == 0
    assert sponsors[0]["pref_age_max"] == 18


def test_ensure_adj_sheets_uses_child_group_baseline(cfg: Config) -> None:
    """Child seeds come from the combined duplicate group (§6.2)."""
    seed_raw(cfg)
    seed_submissions(cfg)
    ensure_adj_sheets(cfg)
    by_id = {row["zite_record_id"]: row for row in read_adj(cfg, "child")}
    # need_1 from the latest submission, desire_1 filled from the earlier.
    assert by_id["c1"]["need_1"] == "winter coat taht fits"
    assert by_id["c1"]["desire_1"] == "blocks"


def test_ensure_adj_sheets_idempotent(cfg: Config) -> None:
    """A second run changes nothing (§7.4)."""
    seed_raw(cfg)
    ensure_adj_sheets(cfg)
    before = read_sheet(cfg.excel_file, "adj_child")
    ensure_adj_sheets(cfg)
    after = read_sheet(cfg.excel_file, "adj_child")
    assert before is not None and after is not None
    assert before.equals(after)


def test_ensure_adj_sheets_projects_final_child(cfg: Config) -> None:
    """The startup projection writes final_child with live formulas (§7.2)."""
    seed_raw(cfg)
    ensure_adj_sheets(cfg)
    final = read_sheet(cfg.excel_file, "final_child")
    assert final is not None
    # Orphaned children (family_id null) never reach final_child.
    assert final.height == 2
    assert "orphan1" not in final["zite_record_id"].to_list()
    assert final["zite_record_id"].to_list() == ["c1", "c2"]
    assert final["status"].to_list() == [STATUS_NEW] * 2
    caches = _final_child_formula_caches(Path(cfg.excel_file))
    assert caches  # age cells are formulas with cached values


def test_ensure_adj_sheets_requires_raw(cfg: Config) -> None:
    """Missing raw sheets fail with an actionable error."""
    with pytest.raises(Exception, match="acquire"):
        ensure_adj_sheets(cfg)


def test_ensure_adj_sheets_refuses_legacy_workbook(cfg: Config) -> None:
    """A pre-v2 sparse adj_child blocks the v2 audit tool (§10.1).

    The old system-of-record workbook must never be written by the v2
    audit tool; the guard reads the sparse schema (no ``screened``
    column) and refuses before any write happens.
    """
    seed_raw(cfg)
    # the old screening's sparse sheet: no controls, no screened marker
    write_sheets(
        cfg.excel_file,
        "screen",
        {
            "adj_child": pl.DataFrame(
                [{"zite_record_id": "c1", "need_1": "coat"}],
                schema={"zite_record_id": pl.String, "need_1": pl.String},
            )
        },
    )
    with pytest.raises(Exception, match="pre-v2 sparse"):
        ensure_adj_sheets(cfg)
    # and nothing was written: no adj_family/final_child appeared
    assert read_sheet(cfg.excel_file, "adj_family") is None
    assert read_sheet(cfg.excel_file, "final_child") is None


# ---------------------------------------------------------------------------
# load_audit_data (§9.2).
# ---------------------------------------------------------------------------


def test_load_audit_data_groups_and_orphans(cfg: Config) -> None:
    """Groups, orphans, and stats are built from one read (§6, §8.1)."""
    seed_raw(cfg)
    seed_submissions(cfg)
    ensure_adj_sheets(cfg)
    data = load_audit_data(cfg)
    assert [entry.sponsor_id for entry in data.sponsors] == [1]
    assert [entry.family_id for entry in data.families] == [1]
    family = data.families[0]
    assert len(family.submissions) == 2
    by_id = {entry.zite_record_id: entry for entry in data.children}
    assert len(by_id["c1"].submissions) == 2
    assert [entry.zite_record_id for entry in data.orphans] == ["orphan1"]
    assert data.child_stats.total == 3
    assert data.child_stats.by_status[STATUS_NEW] == 3
    assert data.sponsor_stats.by_status[STATUS_NEW] == 1


# ---------------------------------------------------------------------------
# Saves (§9.2).
# ---------------------------------------------------------------------------


def test_save_child_dense_round_trip(cfg: Config) -> None:
    """A save persists the full dense row and re-projects final_child."""
    seed_raw(cfg)
    ensure_adj_sheets(cfg)
    row = save_child(
        cfg,
        "c1",
        {
            "need_1": "winter coat that fits",
            "special_needs": "ADHD; other: speech delay",
            "audited": True,
            "ignore": False,
            "require_clarification": None,
        },
    )
    assert row["need_1"] == "winter coat that fits"
    assert row["special_needs"] == "ADHD; other: speech delay"
    assert row["status"] == STATUS_AUDITED
    assert row["age_at_xmas"] == 8
    by_id = {r["zite_record_id"]: r for r in read_adj(cfg, "child")}
    assert by_id["c1"]["audited"] is True
    assert by_id["c1"]["special_needs"] == "ADHD; other: speech delay"
    final = read_sheet(cfg.excel_file, "final_child")
    assert final is not None
    saved = {r["zite_record_id"]: r for r in final.to_dicts()}
    assert saved["c1"]["status"] == STATUS_AUDITED
    assert saved["c1"]["special_needs"] == "ADHD; other: speech delay"
    assert saved["c2"]["status"] == STATUS_NEW


def test_save_child_recomputes_age_from_adjudicated_dob(cfg: Config) -> None:
    """An adjudicated dob fix carries its matching age (D13)."""
    seed_raw(cfg)
    ensure_adj_sheets(cfg)
    row = save_child(cfg, "c1", {"dob": "2014-01-01", "audited": True})
    assert row["age_at_xmas"] == 12
    final = read_sheet(cfg.excel_file, "final_child")
    assert final is not None
    saved = {r["zite_record_id"]: r for r in final.to_dicts()}
    assert saved["c1"]["age_at_xmas"] == 12
    # the final's age cell is a formula whose cached value matches
    caches = _final_child_formula_caches(Path(cfg.excel_file))
    assert "12" in caches.values()


def test_save_child_orphan_cannot_be_audited(cfg: Config) -> None:
    """Orphaned children cannot be marked audited (§8.7)."""
    seed_raw(cfg)
    ensure_adj_sheets(cfg)
    with pytest.raises(Exception, match="orphaned child"):
        save_child(cfg, "orphan1", {"audited": True})
    row = save_child(
        cfg,
        "orphan1",
        {"audited": False, "require_clarification": "find family"},
    )
    assert row["status"] == STATUS_NEEDS_CLARIFICATION


def test_save_child_unknown_fields_rejected(cfg: Config) -> None:
    """Only known columns are accepted."""
    seed_raw(cfg)
    ensure_adj_sheets(cfg)
    with pytest.raises(Exception, match="unknown child fields"):
        save_child(cfg, "c1", {"nickname": "x"})
    with pytest.raises(Exception, match="unknown child"):
        save_child(cfg, "nope", {"audited": False})


def test_save_child_ignore_skeletonizes_and_excludes(cfg: Config) -> None:
    """Ignore reduces the row to its skeleton and drops the final row (§5.5)."""
    seed_raw(cfg)
    ensure_adj_sheets(cfg)
    save_child(cfg, "c1", {"need_1": "edited", "audited": False})
    save_child(cfg, "c1", {"ignore": True, "audited": False})
    by_id = {r["zite_record_id"]: r for r in read_adj(cfg, "child")}
    ignored = by_id["c1"]
    assert ignored["ignore"] is True
    assert ignored["status"] == STATUS_IGNORED
    assert ignored["need_1"] is None
    assert ignored["age_at_xmas"] is None
    assert ignored["first_name"] == "Kiddo"
    assert ignored["primary_email"] == "fam@example.com"
    final = read_sheet(cfg.excel_file, "final_child")
    assert final is not None
    assert "c1" not in final["zite_record_id"].to_list()


def test_save_child_unignore_reseeds_from_baseline(cfg: Config) -> None:
    """Un-ignore re-seeds from the baseline; edits are lost (§5.5)."""
    seed_raw(cfg)
    seed_submissions(cfg)
    ensure_adj_sheets(cfg)
    save_child(cfg, "c1", {"need_1": "human edit", "audited": False})
    save_child(cfg, "c1", {"ignore": True, "audited": False})
    row = save_child(
        cfg,
        "c1",
        {"ignore": False, "audited": False, "require_clarification": None},
    )
    assert row["ignore"] is False
    # back to the combined-group baseline, not the lost human edit
    assert row["need_1"] == "winter coat taht fits"
    assert row["desire_1"] == "blocks"
    assert row["status"] == STATUS_NEW
    final = read_sheet(cfg.excel_file, "final_child")
    assert final is not None
    assert "c1" in final["zite_record_id"].to_list()


def test_save_child_preserves_generated_info_sheet_and_screened(
    cfg: Config,
) -> None:
    """Module-owned columns survive audit saves (§4.4)."""
    seed_raw(cfg)
    ensure_adj_sheets(cfg)
    # module 2 screened the child; module 5 generated its sheet
    rows = read_adj(cfg, "child")
    for row in rows:
        if row["zite_record_id"] == "c1":
            row["screened"] = True
            row["generated_info_sheet"] = True
    write_sheets(
        cfg.excel_file,
        "generate",
        {"adj_child": build_adj_frame("child", rows)},
    )
    row = save_child(cfg, "c1", {"need_1": "new edit", "audited": True})
    assert row["screened"] is True
    assert row["generated_info_sheet"] is True


def test_save_child_merges_dob_conflict_note_idempotently(
    cfg: Config,
) -> None:
    """Conflict notes merge into potential_anomoly exactly once (§9.2)."""
    seed_raw(cfg)
    ensure_adj_sheets(cfg)
    write_sheets(
        cfg.excel_file,
        "screen",
        {
            "dob_conflict_log": pl.DataFrame(
                [
                    {
                        "family_id": 1,
                        "primary_email": "fam@example.com",
                        "child_a_zite_record_id": "c2",
                        "child_a_angel_id": "1B",
                        "child_a_first_name": "Kiddo",
                        "child_a_dob": "2018-05-10",
                        "child_b_zite_record_id": "c1",
                        "child_b_angel_id": "1A",
                        "child_b_first_name": "Kiddo",
                        "child_b_dob": "2018-12-15",
                        "gap_months": 7,
                        "gap_days": 219,
                    }
                ],
                schema={
                    "family_id": pl.Int64,
                    "primary_email": pl.String,
                    "child_a_zite_record_id": pl.String,
                    "child_a_angel_id": pl.String,
                    "child_a_first_name": pl.String,
                    "child_a_dob": pl.String,
                    "child_b_zite_record_id": pl.String,
                    "child_b_angel_id": pl.String,
                    "child_b_first_name": pl.String,
                    "child_b_dob": pl.String,
                    "gap_months": pl.Int64,
                    "gap_days": pl.Int64,
                },
            )
        },
    )
    save_child(cfg, "c1", {"audited": True, "potential_anomoly": "hand note"})
    by_id = {r["zite_record_id"]: r for r in read_adj(cfg, "child")}
    anomoly = by_id["c1"]["potential_anomoly"] or ""
    assert anomoly.count("Sibling DOB conflict") == 1
    assert anomoly.startswith("hand note")
    # re-save with the merged text already present: still once
    save_child(cfg, "c1", {"audited": True, "potential_anomoly": anomoly})
    by_id = {r["zite_record_id"]: r for r in read_adj(cfg, "child")}
    assert (by_id["c1"]["potential_anomoly"] or "").count(
        "Sibling DOB conflict"
    ) == 1


def test_save_child_status_transitions(cfg: Config) -> None:
    """Status flows new -> in_progress -> needs_clarification -> audited."""
    seed_raw(cfg)
    ensure_adj_sheets(cfg)
    assert read_adj(cfg, "child")[0]["status"] == STATUS_NEW
    save_child(cfg, "c1", {"need_1": "edited"})
    by_id = {r["zite_record_id"]: r for r in read_adj(cfg, "child")}
    assert by_id["c1"]["status"] == STATUS_IN_PROGRESS
    save_child(cfg, "c1", {"need_1": "edited", "require_clarification": "call"})
    by_id = {r["zite_record_id"]: r for r in read_adj(cfg, "child")}
    assert by_id["c1"]["status"] == STATUS_NEEDS_CLARIFICATION
    save_child(
        cfg,
        "c1",
        {"need_1": "edited", "require_clarification": "call", "audited": True},
    )
    by_id = {r["zite_record_id"]: r for r in read_adj(cfg, "child")}
    assert by_id["c1"]["status"] == STATUS_AUDITED
    # audited is revocable: unchecking recomputes from note/edit state
    save_child(
        cfg,
        "c1",
        {"need_1": "edited", "require_clarification": "call", "audited": False},
    )
    by_id = {r["zite_record_id"]: r for r in read_adj(cfg, "child")}
    assert by_id["c1"]["status"] == STATUS_NEEDS_CLARIFICATION


def test_save_family_and_sponsor_preserve_assignments(cfg: Config) -> None:
    """Family/sponsor saves preserve assignment columns and project nothing."""
    seed_raw(cfg)
    ensure_adj_sheets(cfg)
    # module 4 wrote assignments into the adj sheets
    families = read_adj(cfg, "family")
    families[0]["assigned_sponsor_id"] = 1
    families[0]["assigned_angel_ids"] = "1A,1B"
    families[0]["num_angels_in_family"] = 2
    sponsors = read_adj(cfg, "sponsor")
    sponsors[0]["assigned_angel_ids"] = "1A,1B"
    sponsors[0]["num_angels_assigned"] = 2
    write_sheets(
        cfg.excel_file,
        "audit",
        {
            "adj_family": build_adj_frame("family", families),
            "adj_sponsor": build_adj_frame("sponsor", sponsors),
        },
    )
    row = save_family(cfg, 1, {"phone": "(906) 555-1111", "audited": True})
    assert row["assigned_sponsor_id"] == 1
    assert row["assigned_angel_ids"] == "1A,1B"
    assert row["num_angels_in_family"] == 2
    assert row["status"] == STATUS_AUDITED
    saved = read_adj(cfg, "family")[0]
    assert saved["phone"] == "(906) 555-1111"
    assert saved["assigned_sponsor_id"] == 1
    # family saves do not write final_family (match projects it, §7.2)
    assert read_sheet(cfg.excel_file, "final_family") is None
    row = save_sponsor(cfg, 1, {"age_preference": "Middle School (11-13 yrs)"})
    assert row["pref_age_min"] == 11
    assert row["pref_age_max"] == 13
    assert row["assigned_angel_ids"] == "1A,1B"


def test_adj_only_save_keeps_final_child_formula_caches(cfg: Config) -> None:
    """A sponsor/family save restores the final_child caches (§7.2)."""
    seed_raw(cfg)
    ensure_adj_sheets(cfg)
    before = _final_child_formula_caches(Path(cfg.excel_file))
    assert before
    save_sponsor(cfg, 1, {"phone": "(906) 555-2222"})
    after = _final_child_formula_caches(Path(cfg.excel_file))
    assert after == before


def test_as_bool_tolerates_string_round_trips() -> None:
    """Boolean cells coming back as text still coerce."""
    assert as_bool(True) and as_bool("true") and as_bool(" True ")
    assert not as_bool(False) and not as_bool("false") and not as_bool(None)
    assert not as_bool("junk")


def test_dob_conflict_notes_and_merge_helpers() -> None:
    """The moved helpers keep their behavior (§5.6)."""
    log_rows = [
        {
            "child_a_zite_record_id": "c2",
            "child_a_angel_id": "1B",
            "child_a_first_name": "Kiddo",
            "child_a_dob": "2018-05-10",
            "child_b_zite_record_id": "c1",
            "child_b_angel_id": "1A",
            "child_b_first_name": "Kiddo",
            "child_b_dob": "2018-12-15",
            "gap_months": 7,
            "gap_days": 219,
        }
    ]
    note = dob_conflict_notes(log_rows, "c1")
    assert note is not None and "Sibling DOB conflict" in note
    assert dob_conflict_notes(log_rows, "other") is None
    assert merge_anomoly(None, note) == note
    assert merge_anomoly("AI text", note) == f"AI text; {note}"
    assert merge_anomoly(f"AI text; {note}", note) == f"AI text; {note}"
    assert merge_anomoly("AI text", None) == "AI text"


def test_build_adj_frame_normalizes_string_booleans() -> None:
    """String booleans and float ints coerce to the schema types."""
    rows = [
        {
            "zite_record_id": "c1",
            "audited": "true",
            "ignore": "false",
            "screened": None,
            "family_id": 1.0,
        }
    ]
    frame = build_adj_frame("child", rows)
    assert frame.schema["audited"] == pl.Boolean
    assert frame["audited"].to_list() == [True]
    assert frame["ignore"].to_list() == [False]
    assert frame["screened"].to_list() == [False]
    assert frame["family_id"].to_list() == [1]
