"""Tests for the persistent-final audit store and its output filters."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import polars as pl
import pytest
from openpyxl import load_workbook

import ccat.audit.store as audit_store
from ccat.audit import AuditError
from ccat.audit.store import (
    ENTITY_SPECS,
    FINAL_CHILD_COLUMNS,
    FINAL_CHILD_SCHEMA,
    FINAL_FAMILY_COLUMNS,
    FINAL_FAMILY_SCHEMA,
    FINAL_SPONSOR_COLUMNS,
    FINAL_SPONSOR_SCHEMA,
    STATUS_AUDITED,
    STATUS_IGNORED,
    STATUS_IN_PROGRESS,
    STATUS_NEEDS_CLARIFICATION,
    STATUS_NEW,
    as_bool,
    build_final_frame,
    coalesce_family_group,
    combine_child_group,
    compute_status,
    dob_conflict_notes,
    eligible_output_rows,
    ensure_final_sheets,
    group_submissions,
    load_audit_data,
    merge_anomoly,
    read_eligible_final,
    read_final,
    reduce_ignored,
    save_child,
    save_family,
    save_sponsor,
    status_of,
)
from ccat.config import Config
from ccat.io import (
    _final_child_formula_caches,
    list_sheets,
    read_sheet,
    write_sheets,
)

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
    """One full-final child row for store tests."""
    base: dict[str, Any] = {column: None for column in FINAL_CHILD_COLUMNS}
    base.update(overrides)
    return base


def test_status_of_excludes_special_needs_and_computed() -> None:
    """Special-needs selection and computed columns never set in_progress."""
    baseline = {"dob": "2018-05-10", "special_needs": None, "age_at_xmas": None}
    row = _row(
        dob="2018-05-10",
        special_needs="ADHD; other: speech delay",
        age_at_xmas=8,
        status="new",
        ignore=False,
    )
    assert status_of(row, baseline, "child") == STATUS_NEW


def test_status_of_counts_data_edits_and_notes() -> None:
    """Adjudicable edits and notes derive in_progress/needs_clarification."""
    baseline = {"need_1": "raw", "clarification_notes": None}
    row = _row(need_1="fixed", clarification_notes=None)
    assert status_of(row, baseline, "child") == STATUS_IN_PROGRESS
    row = _row(need_1="raw", clarification_notes="call the family")
    assert status_of(row, baseline, "child") == STATUS_NEEDS_CLARIFICATION


def test_status_of_ignore_outranks_stored_audited() -> None:
    """D11: a live pseudo-row just marked ignored shows ignored.

    The live evaluator previously returned ``audited`` before testing
    the ignore flag while persisted saves forced ``ignored``; the
    evaluator is unified toward the persisted rule.
    """
    baseline = {"need_1": "raw"}
    row = _row(status=STATUS_AUDITED, ignore=True, need_1="raw")
    assert status_of(row, baseline, "child") == STATUS_IGNORED


def test_status_of_stored_audited_and_ignored_row_reads_ignored(
    cfg: Config,
) -> None:
    """D11: legacy stored audited+ignored rows read back as ignored."""
    seed_final_sheets(cfg)
    rows = read_final(cfg, "child")
    for row in rows:
        if row["zite_record_id"] == "c1":
            row["status"] = STATUS_AUDITED
            row["ignore"] = True
    write_sheets(
        cfg.excel_file,
        "audit",
        {"final_child": build_final_frame("child", rows)},
        xmas=cfg.xmas,
    )
    stored = next(
        row for row in read_final(cfg, "child") if row["zite_record_id"] == "c1"
    )
    assert stored["status"] == STATUS_AUDITED
    assert status_of(stored, {"need_1": stored["need_1"]}, "child") == (
        STATUS_IGNORED
    )


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


def test_group_prepared_rows_keys_and_singletons() -> None:
    """Prepared children group by email/name; unkeyed rows stay singletons."""
    prepared = [
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
    groups = group_submissions(prepared, "child")
    assert len(groups[("a@x.com", "Kid")]) == 2
    assert len(groups[("a@x.com", "Other")]) == 1
    assert ("4",) in groups
    family_groups = group_submissions(prepared, "family")
    assert len(family_groups[("a@x.com",)]) == 3


@pytest.mark.parametrize("entity", ["child", "family", "sponsor"])
def test_reduce_ignored_keeps_only_skeleton(entity: str) -> None:
    """Ignored rows keep identity fields, duplicate state, and controls."""
    controls = (
        "status",
        "audited",
        "ignore",
        "clarification_notes",
        "audited_at",
    ) + (("screened",) if entity == "child" else ())
    row: dict[str, Any] = {
        column: "keep" for column in ENTITY_SPECS[entity]["columns"]
    }
    row["ignore"] = True
    row["audited"] = False
    row["status"] = STATUS_IGNORED
    reduced = reduce_ignored(row, entity)
    module_owned = {
        "child": {"generated_info_sheet"},
        "family": {
            "assigned_sponsor_id",
            "assigned_angel_ids",
            "num_angels_in_family",
        },
        "sponsor": {"assigned_angel_ids", "num_angels_assigned"},
    }[entity]
    retained = {
        "child": {
            "zite_record_id",
            "family_id",
            "angel_id",
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
        if column in module_owned:
            continue
        if column in retained:
            assert reduced[column] == "keep", column
        elif column in controls:
            assert reduced[column] == row[column], column
        else:
            assert reduced[column] is None, column


def test_eligible_output_rows_match_retired_child_projection(
    cfg: Config,
) -> None:
    """Output filtering excludes ignored/orphans without changing final storage."""
    rows = [
        _row(
            zite_record_id="c1",
            family_id=1,
            angel_id="1A",
            status=STATUS_AUDITED,
            ignore=False,
            screened=True,
            generated_info_sheet=True,
            clarification_notes=None,
            age_at_xmas=8,
        ),
        _row(
            zite_record_id="ignored",
            family_id=1,
            status=STATUS_IGNORED,
            ignore=True,
            screened=False,
        ),
        _row(
            zite_record_id="orphan",
            family_id=None,
            angel_id=None,
            status=STATUS_NEEDS_CLARIFICATION,
            ignore=False,
            screened=False,
        ),
        _row(
            zite_record_id="letterless-duplicate",
            family_id=1,
            angel_id=None,
            status=STATUS_NEW,
            ignore=False,
            screened=False,
        ),
    ]
    write_sheets(
        cfg.excel_file,
        "audit",
        {"final_child": build_final_frame("child", rows)},
        xmas=cfg.xmas,
    )
    before_bytes = Path(cfg.excel_file).read_bytes()
    before = read_final(cfg, "child")

    eligible = read_eligible_final(cfg, "child")

    assert [row["zite_record_id"] for row in eligible] == [
        "c1",
        "letterless-duplicate",
    ]
    assert read_final(cfg, "child") == before
    assert {row["zite_record_id"] for row in before} == {
        "c1",
        "ignored",
        "orphan",
        "letterless-duplicate",
    }
    assert Path(cfg.excel_file).read_bytes() == before_bytes
    frame = build_final_frame("child", before)
    assert list(frame.columns) == list(FINAL_CHILD_COLUMNS)
    for control in ("ignore", "screened", "status", "clarification_notes"):
        assert control in frame.columns
    assert "audited" not in frame.columns


@pytest.mark.parametrize("entity", ["family", "sponsor"])
def test_eligible_output_rows_keep_full_assignments(entity: str) -> None:
    """Family/sponsor output filters only ignore rows, never project columns."""
    columns = (
        FINAL_FAMILY_COLUMNS if entity == "family" else FINAL_SPONSOR_COLUMNS
    )
    key = ENTITY_SPECS[entity]["key"]
    kept = {column: None for column in columns}
    kept.update({key: 1, "status": STATUS_NEW, "ignore": False})
    ignored = dict(kept)
    ignored.update({key: 2, "status": STATUS_IGNORED, "ignore": True})
    if entity == "family":
        kept.update(
            assigned_sponsor_id=3,
            assigned_angel_ids="1A",
            num_angels_in_family=1,
        )
    else:
        kept.update(assigned_angel_ids="1A", num_angels_assigned=1)

    eligible = eligible_output_rows([kept, ignored], entity)

    assert eligible == [kept]
    assert set(eligible[0]) == set(columns)
    assert eligible[0]["assigned_angel_ids"] == "1A"
    assert ignored["ignore"] is True


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([_row(zite_record_id="c1"), _row(zite_record_id="c1")], "duplicate"),
        ([_row(zite_record_id=None)], "missing zite_record_id"),
    ],
)
def test_build_final_frame_rejects_duplicate_and_null_keys(
    rows: list[dict[str, Any]], message: str
) -> None:
    """Persistent entity identities must be present and unique before writes."""
    with pytest.raises(AuditError, match=message):
        build_final_frame("child", rows)


# ---------------------------------------------------------------------------
# Raw + complete persistent-final workbook fixtures.
# ---------------------------------------------------------------------------


RAW_CHILD_SCHEMA = {
    column: FINAL_CHILD_SCHEMA[column]
    for column in (
        "zite_record_id",
        "zite_created_at",
        "created",
        "family_id",
        "angel_id",
        "first_name",
        "primary_email",
        "dob",
        "age_reported",
        "gender",
        "need_1",
        "need_2",
        "desire_1",
        "desire_2",
        "shoe_size",
        "shoe_size_type",
        "pant_waist_size",
        "pant_inseam",
        "pant_size_letter",
        "pant_size_type",
        "jean_size",
        "shirt_size_letter",
        "shirt_size_type",
        "favorite_colors",
        "interests",
        "other_notes",
        "age_at_xmas",
        "source",
    )
}

RAW_FAMILY_SCHEMA = {
    column: FINAL_FAMILY_SCHEMA[column]
    for column in (
        "family_id",
        "zite_record_id",
        "zite_created_at",
        "created",
        "primary_email",
        "accepts_disclaimer",
        "first_name",
        "last_name",
        "phone",
        "street_address",
        "apt_floor",
        "city",
        "zip_code",
        "state",
        "secondary_first_name",
        "secondary_last_name",
        "secondary_email",
        "secondary_phone",
        "gift_pickup_location",
        "source",
    )
}

RAW_SPONSOR_SCHEMA = {
    column: FINAL_SPONSOR_SCHEMA[column]
    for column in (
        "sponsor_id",
        "zite_record_id",
        "create",
        "email",
        "accepts_disclaimer",
        "sponsor_type",
        "organization_name",
        "prior_assigned_angel_ids",
        "num_children",
        "age_preference",
        "gender_preference",
        "dropoff_location",
        "first_name",
        "last_name",
        "phone",
        "pref_age_min",
        "pref_age_max",
        "source",
    )
}


def raw_child_row(
    record_id: str, angel_id: str | None = "1A", family_id: int | None = 1
) -> dict[str, Any]:
    """One raw_child row; null family linkage gets an unmatched email."""
    return {
        "zite_record_id": record_id,
        "zite_created_at": "2026-02-01T09:00:00Z",
        "created": "2026-02-01T09:00:00Z",
        "family_id": family_id,
        "angel_id": angel_id,
        "first_name": "Kiddo",
        "primary_email": (
            "fam@example.com" if family_id is not None else "orphan@example.com"
        ),
        "dob": "2018-05-10",
        "age_reported": 7,
        "gender": "Male",
        "need_1": "winter coat taht fits",
        "shoe_size": "13",
        "age_at_xmas": 8,
        "source": "web",
    }


def default_raw_families() -> list[dict[str, Any]]:
    """One raw family matching the default children's primary email."""
    return [
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


def default_raw_sponsors() -> list[dict[str, Any]]:
    """One raw sponsor with explicit age-preference bounds in its raw fixture."""
    return [
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


def duplicate_raw_children() -> list[dict[str, Any]]:
    """Two prepared child duplicates plus one genuinely orphaned child."""
    earlier = raw_child_row("c2", None)
    earlier.update(
        {
            "zite_created_at": "2026-02-01T09:00:00Z",
            "created": "2026-02-01T09:00:00Z",
            "need_1": "old coat",
            "desire_1": "blocks",
        }
    )
    latest = raw_child_row("c1", "1A")
    latest.update(
        {
            "zite_created_at": "2026-02-10T09:00:00Z",
            "created": "2026-02-10T09:00:00Z",
            "need_1": "winter coat taht fits",
            "desire_1": None,
        }
    )
    return [latest, earlier, raw_child_row("orphan1", None, None)]


def default_raw_children() -> list[dict[str, Any]]:
    """Duplicate pair plus unmatched-email orphan as the child source."""
    return duplicate_raw_children()


def seed_raw(
    cfg: Config,
    children: list[dict[str, Any]] | None = None,
    families: list[dict[str, Any]] | None = None,
    sponsors: list[dict[str, Any]] | None = None,
) -> None:
    """Write only raw sheets; preparation remains shared and in memory."""
    if children is None:
        children = default_raw_children()
    if families is None:
        families = default_raw_families()
    if sponsors is None:
        sponsors = default_raw_sponsors()
    write_sheets(
        cfg.excel_file,
        "acquire",
        {
            "raw_child": pl.DataFrame(children, schema=RAW_CHILD_SCHEMA),
            "raw_family": pl.DataFrame(families, schema=RAW_FAMILY_SCHEMA),
            "raw_sponsor": pl.DataFrame(sponsors, schema=RAW_SPONSOR_SCHEMA),
        },
    )


def seed_final_sheets(
    cfg: Config,
    children: list[dict[str, Any]] | None = None,
    families: list[dict[str, Any]] | None = None,
    sponsors: list[dict[str, Any]] | None = None,
) -> None:
    """Create a raw + full-final fixture without submissions or adj sheets."""
    seed_raw(cfg, children, families, sponsors)
    ensure_final_sheets(cfg)


def assert_no_legacy_entity_sheets(cfg: Config) -> None:
    """Fail if a test writer recreates retired entity persistence layers."""
    sheets = list_sheets(cfg.excel_file)
    assert not any(sheet.startswith("adj_") for sheet in sheets)
    assert not any(sheet.endswith("_submissions") for sheet in sheets)


# ---------------------------------------------------------------------------
# ensure_final_sheets: guarded initial seed and append-only audit backstop.
# ---------------------------------------------------------------------------


def test_ensure_final_sheets_seeds_dense_rows(cfg: Config) -> None:
    """Every prepared entity gets a full final row with default controls."""
    seed_raw(cfg)
    ensure_final_sheets(cfg)

    children = read_final(cfg, "child")
    assert {row["zite_record_id"] for row in children} == {
        "c1",
        "c2",
        "orphan1",
    }
    by_id = {row["zite_record_id"]: row for row in children}
    assert by_id["c1"]["need_1"] == "winter coat taht fits"
    assert by_id["c1"]["age_at_xmas"] == 8
    assert by_id["c1"]["status"] == STATUS_NEW
    assert by_id["c1"]["status"] != STATUS_AUDITED
    assert by_id["c1"]["screened"] is False
    assert by_id["c1"]["generated_info_sheet"] is False
    families = read_final(cfg, "family")
    assert len(families) == 1
    assert families[0]["assigned_sponsor_id"] is None
    assert families[0]["status"] == STATUS_NEW
    sponsors = read_final(cfg, "sponsor")
    assert len(sponsors) == 1
    assert sponsors[0]["pref_age_min"] == 0
    assert sponsors[0]["pref_age_max"] == 18
    assert_no_legacy_entity_sheets(cfg)


def test_ensure_final_sheets_uses_raw_child_group_baseline(cfg: Config) -> None:
    """A new child seed combines its in-memory raw duplicate group."""
    seed_raw(cfg)
    ensure_final_sheets(cfg)

    by_id = {row["zite_record_id"]: row for row in read_final(cfg, "child")}
    assert by_id["c1"]["need_1"] == "winter coat taht fits"
    assert by_id["c1"]["desire_1"] == "N/A"


def test_ensure_final_sheets_no_change_is_byte_and_row_identical(
    cfg: Config,
) -> None:
    """Startup never refreshes existing finals or strips ignored/orphans."""
    seed_final_sheets(cfg)
    save_child(cfg, "c1", {"ignore": True, "status": "new"})
    before_rows = {
        entity: read_final(cfg, entity)
        for entity in ("sponsor", "family", "child")
    }
    before_bytes = Path(cfg.excel_file).read_bytes()

    ensure_final_sheets(cfg)

    assert Path(cfg.excel_file).read_bytes() == before_bytes
    for entity, expected in before_rows.items():
        assert read_final(cfg, entity) == expected
    stored_ids = {row["zite_record_id"] for row in before_rows["child"]}
    assert stored_ids == {"c1", "c2", "orphan1"}
    assert {
        row["zite_record_id"] for row in read_eligible_final(cfg, "child")
    } == {"c2"}
    assert_no_legacy_entity_sheets(cfg)


def test_ensure_final_sheets_only_appends_genuinely_missing_rows(
    cfg: Config,
) -> None:
    """A new prepared child appends; every pre-existing final row stays exact."""
    original = raw_child_row("c1")
    seed_final_sheets(
        cfg, children=[original, raw_child_row("orphan1", None, None)]
    )
    save_child(
        cfg,
        "c1",
        {
            "need_1": "human edit survives startup",
            "status": "audited",
            "ignore": False,
            "clarification_notes": None,
        },
    )
    c1_before = next(
        row for row in read_final(cfg, "child") if row["zite_record_id"] == "c1"
    )
    before = {
        entity: read_final(cfg, entity)
        for entity in ("sponsor", "family", "child")
    }
    added = raw_child_row("c2", "1B")
    added["first_name"] = "Sibling"
    seed_raw(
        cfg, children=[original, added, raw_child_row("orphan1", None, None)]
    )

    ensure_final_sheets(cfg)

    after_children = read_final(cfg, "child")
    assert {row["zite_record_id"] for row in after_children} == {
        "c1",
        "c2",
        "orphan1",
    }
    assert len(after_children) == len(before["child"]) + 1
    assert (
        next(row for row in after_children if row["zite_record_id"] == "c1")
        == c1_before
    )
    for entity in ("sponsor", "family"):
        assert read_final(cfg, entity) == before[entity]
    assert_no_legacy_entity_sheets(cfg)


def test_ensure_final_sheets_full_final_keeps_orphan_and_xmas_formulas(
    cfg: Config,
) -> None:
    """Initial final storage includes orphans and live age formula caches."""
    seed_raw(cfg)
    ensure_final_sheets(cfg)

    final = read_sheet(cfg.excel_file, "final_child")
    assert final is not None
    assert final.height == 3
    assert final["zite_record_id"].to_list() == ["c2", "c1", "orphan1"]
    assert list(final.columns) == list(FINAL_CHILD_COLUMNS)
    assert final["status"].to_list() == [
        STATUS_NEW,
        STATUS_NEW,
        STATUS_NEEDS_CLARIFICATION,
    ]
    caches = _final_child_formula_caches(Path(cfg.excel_file))
    assert caches
    assert all(value == "8" for value in caches.values())
    assert [
        row["zite_record_id"] for row in read_eligible_final(cfg, "child")
    ] == [
        "c2",
        "c1",
    ]
    assert read_sheet(cfg.excel_file, "final_child") is not None
    stored_final = read_sheet(cfg.excel_file, "final_child")
    assert stored_final is not None and stored_final.height == 3


def test_ensure_final_sheets_requires_raw(cfg: Config) -> None:
    """Missing raw sheets fail with actionable acquire guidance."""
    with pytest.raises(Exception, match="acquire"):
        ensure_final_sheets(cfg)


def test_legacy_projected_final_requires_consolidate_migrate(
    cfg: Config,
) -> None:
    """A projected final fails closed and is never expanded in place."""
    seed_raw(cfg)
    workbook = load_workbook(cfg.excel_file)
    legacy = workbook.create_sheet("final_child")
    legacy.append(["zite_record_id", "first_name", "status"])
    legacy.append(["legacy-c1", "Legacy", STATUS_AUDITED])
    workbook.save(cfg.excel_file)
    workbook.close()

    with pytest.raises(AuditError) as caught:
        ensure_final_sheets(cfg)

    message = str(caught.value)
    assert "persistent full schema" in message
    assert "consolidate-migrate" in message
    legacy_after = read_sheet(cfg.excel_file, "final_child")
    assert legacy_after is not None
    assert legacy_after.to_dicts() == [
        {
            "zite_record_id": "legacy-c1",
            "first_name": "Legacy",
            "status": STATUS_AUDITED,
        }
    ]
    assert read_sheet(cfg.excel_file, "final_family") is None
    assert read_sheet(cfg.excel_file, "final_sponsor") is None


# ---------------------------------------------------------------------------
# load_audit_data: final truth plus current in-memory prepared baselines.
# ---------------------------------------------------------------------------


def test_load_audit_data_groups_and_keeps_orphans(cfg: Config) -> None:
    """Prepared groups and final orphan/ignored workflows share one load."""
    seed_final_sheets(cfg)
    data = load_audit_data(cfg)

    assert [entry.sponsor_id for entry in data.sponsors] == [1]
    assert [entry.family_id for entry in data.families] == [1]
    family = data.families[0]
    assert len(family.submissions) == 1
    by_id = {entry.zite_record_id: entry for entry in data.children}
    assert len(by_id["c1"].submissions) == 2
    assert by_id["c1"].baseline["desire_1"] == "N/A"
    assert [entry.zite_record_id for entry in data.orphans] == ["orphan1"]
    assert data.child_stats.total == 3
    assert data.child_stats.by_status[STATUS_NEW] == 2
    assert data.child_stats.by_status[STATUS_NEEDS_CLARIFICATION] == 1
    assert data.sponsor_stats.by_status[STATUS_NEW] == 1


def test_raw_missing_final_retains_edits_loads_and_saves(cfg: Config) -> None:
    """Stored rows supply the raw-missing baseline instead of an unknown-ID error."""
    seed_final_sheets(cfg)
    save_child(
        cfg,
        "c1",
        {
            "need_1": "kept human edit",
            "status": "new",
            "ignore": False,
            "clarification_notes": None,
        },
    )
    current_children = default_raw_children()
    seed_raw(
        cfg,
        children=[
            row for row in current_children if row["zite_record_id"] != "c1"
        ],
    )
    before_keys = {row["zite_record_id"] for row in read_final(cfg, "child")}

    ensure_final_sheets(cfg)
    data = load_audit_data(cfg)
    by_id = {entry.zite_record_id: entry for entry in data.children}
    assert "c1" in by_id
    assert by_id["c1"].adj["need_1"] == "kept human edit"
    assert by_id["c1"].baseline["need_1"] == "kept human edit"
    assert {
        row["zite_record_id"] for row in read_final(cfg, "child")
    } == before_keys

    saved = save_child(
        cfg,
        "c1",
        {
            "need_1": "second raw-missing edit",
            "status": "new",
            "ignore": False,
            "clarification_notes": None,
        },
    )
    assert saved["need_1"] == "second raw-missing edit"
    stored = {row["zite_record_id"]: row for row in read_final(cfg, "child")}[
        "c1"
    ]
    assert stored["need_1"] == "second raw-missing edit"
    assert stored["status"] == STATUS_IN_PROGRESS

    save_child(cfg, "c1", {"ignore": True, "status": "new"})
    restored = save_child(
        cfg,
        "c1",
        {"ignore": False, "status": "new", "clarification_notes": None},
    )
    assert restored["ignore"] is False
    assert restored["zite_record_id"] == "c1"
    assert "c1" in {row["zite_record_id"] for row in read_final(cfg, "child")}


# ---------------------------------------------------------------------------
# Direct full-final saves.
# ---------------------------------------------------------------------------


def test_save_child_dense_round_trip(cfg: Config) -> None:
    """A save persists the full dense row directly to final_child."""
    seed_final_sheets(cfg)
    row = save_child(
        cfg,
        "c1",
        {
            "need_1": "winter coat that fits",
            "special_needs": "ADHD; other: speech delay",
            "status": "audited",
            "ignore": False,
            "clarification_notes": None,
        },
    )
    assert row["need_1"] == "winter coat that fits"
    assert row["special_needs"] == "ADHD; other: speech delay"
    assert row["status"] == STATUS_AUDITED
    assert row["age_at_xmas"] == 8
    by_id = {r["zite_record_id"]: r for r in read_final(cfg, "child")}
    assert by_id["c1"]["status"] == STATUS_AUDITED
    assert by_id["c1"]["special_needs"] == "ADHD; other: speech delay"
    assert by_id["c1"]["status"] == STATUS_AUDITED
    assert by_id["c2"]["status"] == STATUS_NEW
    assert set(by_id) == {"c1", "c2", "orphan1"}


def test_save_child_recomputes_age_from_adjudicated_dob(cfg: Config) -> None:
    """An adjudicated dob fix carries its matching formula-cached age."""
    seed_final_sheets(cfg)
    row = save_child(cfg, "c1", {"dob": "2014-01-01", "status": "audited"})
    assert row["age_at_xmas"] == 12
    saved = {r["zite_record_id"]: r for r in read_final(cfg, "child")}
    assert saved["c1"]["age_at_xmas"] == 12
    caches = _final_child_formula_caches(Path(cfg.excel_file))
    assert "12" in caches.values()


def test_save_child_orphan_gate_uses_family_not_angel(cfg: Config) -> None:
    """Letterless family children audit; only null family_id blocks approval."""
    seed_final_sheets(cfg)
    letterless = next(
        row
        for row in read_final(cfg, "child")
        if row["family_id"] is not None and row["angel_id"] is None
    )
    approved = save_child(
        cfg,
        letterless["zite_record_id"],
        {
            "status": "audited",
            "ignore": False,
            "clarification_notes": None,
        },
    )
    assert approved["family_id"] == 1
    assert approved["angel_id"] is None
    assert approved["status"] == STATUS_AUDITED

    with pytest.raises(Exception, match="orphaned child"):
        save_child(cfg, "orphan1", {"status": "audited"})
    row = save_child(
        cfg,
        "orphan1",
        {"status": "new", "clarification_notes": "find family"},
    )
    assert row["status"] == STATUS_NEEDS_CLARIFICATION
    assert "orphan1" in {
        row["zite_record_id"] for row in read_final(cfg, "child")
    }


def test_save_child_unknown_fields_rejected(cfg: Config) -> None:
    """Only known full-final save fields and stored identities are accepted."""
    seed_final_sheets(cfg)
    with pytest.raises(Exception, match="unknown child fields"):
        save_child(cfg, "c1", {"nickname": "x"})
    with pytest.raises(Exception, match="unknown child"):
        save_child(cfg, "nope", {"status": "new"})


def test_save_child_ignore_skeletonizes_but_stays_stored(cfg: Config) -> None:
    """Ignore clears editable data while retaining identity and module state."""
    seed_final_sheets(cfg)
    before_keys = {row["zite_record_id"] for row in read_final(cfg, "child")}
    children = read_final(cfg, "child")
    for stored in children:
        if stored["zite_record_id"] == "c1":
            stored["screened"] = True
            stored["generated_info_sheet"] = True
            stored["potential_duplicate"] = True
    write_sheets(
        cfg.excel_file,
        "generate",
        {"final_child": build_final_frame("child", children)},
        xmas=cfg.xmas,
    )
    save_child(cfg, "c1", {"need_1": "edited", "status": "new"})
    save_child(cfg, "c1", {"ignore": True, "status": "new"})

    by_id = {r["zite_record_id"]: r for r in read_final(cfg, "child")}
    ignored = by_id["c1"]
    assert ignored["ignore"] is True
    assert ignored["status"] == STATUS_IGNORED
    assert ignored["need_1"] is None
    assert ignored["age_at_xmas"] is None
    assert ignored["first_name"] == "Kiddo"
    assert ignored["primary_email"] == "fam@example.com"
    assert ignored["screened"] is True
    assert ignored["generated_info_sheet"] is True
    assert ignored["potential_duplicate"] is True
    assert set(by_id) == before_keys
    assert "c1" not in {
        row["zite_record_id"] for row in read_eligible_final(cfg, "child")
    }


def test_save_child_unignore_reseeds_from_prepared_group_baseline(
    cfg: Config,
) -> None:
    """Un-ignore restores the current raw-derived group baseline, not edits."""
    seed_final_sheets(cfg)
    children = read_final(cfg, "child")
    for stored in children:
        if stored["zite_record_id"] == "c1":
            stored["screened"] = True
            stored["generated_info_sheet"] = True
            stored["potential_duplicate"] = True
    write_sheets(
        cfg.excel_file,
        "generate",
        {"final_child": build_final_frame("child", children)},
        xmas=cfg.xmas,
    )
    save_child(cfg, "c1", {"need_1": "human edit", "status": "new"})
    save_child(cfg, "c1", {"ignore": True, "status": "new"})
    row = save_child(
        cfg,
        "c1",
        {"ignore": False, "status": "new", "clarification_notes": None},
    )
    assert row["ignore"] is False
    assert row["need_1"] == "winter coat taht fits"
    assert row["desire_1"] == "N/A"
    assert row["status"] == STATUS_NEW
    assert row["screened"] is True
    assert row["generated_info_sheet"] is True
    assert row["potential_duplicate"] is True
    assert "c1" in {row["zite_record_id"] for row in read_final(cfg, "child")}
    assert "c1" in {
        row["zite_record_id"] for row in read_eligible_final(cfg, "child")
    }


def test_saves_preserve_child_generation_and_match_assignments(
    cfg: Config,
) -> None:
    """Generation, screening, family, and sponsor module fields survive saves."""
    seed_final_sheets(cfg)
    children = read_final(cfg, "child")
    for row in children:
        if row["zite_record_id"] == "c1":
            row["screened"] = True
            row["generated_info_sheet"] = True
    write_sheets(
        cfg.excel_file,
        "generate",
        {"final_child": build_final_frame("child", children)},
        xmas=cfg.xmas,
    )
    families = read_final(cfg, "family")
    families[0]["assigned_sponsor_id"] = 1
    families[0]["assigned_angel_ids"] = "1A"
    families[0]["num_angels_in_family"] = 1
    sponsors = read_final(cfg, "sponsor")
    sponsors[0]["assigned_angel_ids"] = "1A"
    sponsors[0]["num_angels_assigned"] = 1
    write_sheets(
        cfg.excel_file,
        "match",
        {
            "final_family": build_final_frame("family", families),
            "final_sponsor": build_final_frame("sponsor", sponsors),
        },
    )

    child = save_child(cfg, "c1", {"need_1": "new edit", "status": "audited"})
    family = save_family(
        cfg,
        1,
        {"phone": "(906) 555-1111", "status": "audited"},
    )
    sponsor = save_sponsor(
        cfg, 1, {"age_preference": "Middle School (11-13 yrs)"}
    )

    assert child["screened"] is True
    assert child["generated_info_sheet"] is True
    stored_child = {
        row["zite_record_id"]: row for row in read_final(cfg, "child")
    }["c1"]
    assert stored_child["screened"] is True
    assert stored_child["generated_info_sheet"] is True
    assert family["assigned_sponsor_id"] == 1
    assert family["assigned_angel_ids"] == "1A"
    assert family["num_angels_in_family"] == 1
    assert family["status"] == STATUS_AUDITED
    stored_family = read_final(cfg, "family")[0]
    assert stored_family["phone"] == "(906) 555-1111"
    assert stored_family["assigned_sponsor_id"] == 1
    assert stored_family["assigned_angel_ids"] == "1A"
    assert stored_family["num_angels_in_family"] == 1
    assert sponsor["pref_age_min"] == 11
    assert sponsor["pref_age_max"] == 13
    assert sponsor["assigned_angel_ids"] == "1A"
    stored_sponsor = read_final(cfg, "sponsor")[0]
    assert stored_sponsor["assigned_angel_ids"] == "1A"
    assert stored_sponsor["num_angels_assigned"] == 1


def test_save_child_uses_write_lock(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every read-modify-write final save serializes through _WRITE_LOCK."""
    seed_final_sheets(cfg)
    lock = MagicMock()
    monkeypatch.setattr(audit_store, "_WRITE_LOCK", lock)

    save_child(cfg, "c1", {"need_1": "locked edit", "status": "new"})

    lock.__enter__.assert_called_once_with()
    lock.__exit__.assert_called_once_with(None, None, None)


def test_save_child_merges_dob_conflict_note_idempotently(
    cfg: Config,
) -> None:
    """Conflict notes merge into potential_anomoly exactly once (§9.2)."""
    seed_final_sheets(cfg)
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
                        "child_a_angel_id": None,
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
    save_child(
        cfg, "c1", {"status": "audited", "potential_anomoly": "hand note"}
    )
    by_id = {r["zite_record_id"]: r for r in read_final(cfg, "child")}
    anomoly = by_id["c1"]["potential_anomoly"] or ""
    assert anomoly.count("Sibling DOB conflict") == 1
    assert anomoly.startswith("hand note")
    save_child(cfg, "c1", {"status": "audited", "potential_anomoly": anomoly})
    by_id = {r["zite_record_id"]: r for r in read_final(cfg, "child")}
    assert (by_id["c1"]["potential_anomoly"] or "").count(
        "Sibling DOB conflict"
    ) == 1


def test_save_child_status_transitions(cfg: Config) -> None:
    """Status flows new -> in_progress -> needs_clarification -> audited."""
    seed_final_sheets(cfg)
    assert read_final(cfg, "child")[0]["status"] == STATUS_NEW
    save_child(cfg, "c1", {"need_1": "edited"})
    by_id = {r["zite_record_id"]: r for r in read_final(cfg, "child")}
    assert by_id["c1"]["status"] == STATUS_IN_PROGRESS
    save_child(cfg, "c1", {"need_1": "edited", "clarification_notes": "call"})
    by_id = {r["zite_record_id"]: r for r in read_final(cfg, "child")}
    assert by_id["c1"]["status"] == STATUS_NEEDS_CLARIFICATION
    save_child(
        cfg,
        "c1",
        {
            "need_1": "edited",
            "clarification_notes": "call",
            "status": "audited",
        },
    )
    by_id = {r["zite_record_id"]: r for r in read_final(cfg, "child")}
    assert by_id["c1"]["status"] == STATUS_AUDITED
    save_child(
        cfg,
        "c1",
        {"need_1": "edited", "clarification_notes": "call", "status": "new"},
    )
    by_id = {r["zite_record_id"]: r for r in read_final(cfg, "child")}
    assert by_id["c1"]["status"] == STATUS_NEEDS_CLARIFICATION


def test_non_child_save_keeps_final_child_formula_caches(cfg: Config) -> None:
    """A family/sponsor write does not strip final-child formula caches."""
    seed_final_sheets(cfg)
    before = _final_child_formula_caches(Path(cfg.excel_file))
    assert before
    save_sponsor(cfg, 1, {"phone": "(906) 555-2222"})
    after_sponsor = _final_child_formula_caches(Path(cfg.excel_file))
    assert after_sponsor == before
    save_family(cfg, 1, {"phone": "(906) 555-3333"})
    after_family = _final_child_formula_caches(Path(cfg.excel_file))
    assert after_family == before


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
            "child_a_angel_id": None,
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


def test_build_final_frame_normalizes_string_booleans() -> None:
    """String booleans and float ints coerce to full-final schema types."""
    rows = [
        {
            "zite_record_id": "c1",
            "status": "audited",
            "ignore": "false",
            "screened": None,
            "family_id": 1.0,
        }
    ]
    frame = build_final_frame("child", rows)
    assert frame.schema["status"] == pl.String
    assert frame["status"].to_list() == ["audited"]
    assert frame["ignore"].to_list() == [False]
    assert frame["screened"].to_list() == [False]
    assert frame["family_id"].to_list() == [1]
