"""Tests for module 4 — matchmaking (§8)."""

from pathlib import Path
from typing import Any

import polars as pl
import pytest

from ccat.acquire import AGE_PREF_BOUNDS, SPECIAL_NEEDS_LABEL
from ccat.audit.store import (
    CHILD_ASSIGNMENT_COLUMNS,
    ENTITY_SPECS,
    FAMILY_ASSIGNMENT_COLUMNS,
    SPONSOR_ASSIGNMENT_COLUMNS,
    build_final_frame,
    read_final,
)
from ccat.config import Config
from ccat.io import list_sheets, read_sheet, write_sheets
from ccat.match import (
    PoolFamily,
    SponsorPlan,
    best_subset,
    canonical_location,
    family_score,
    greedy_subset,
    parse_prior_assigned,
    reconcile_pre_assignments,
    run,
    special_needs_weight,
)


def sponsor_row(
    sponsor_id: int,
    num_children: int = 2,
    sponsor_type: str = "Individual",
    age_preference: str = "Any Age",
    gender_preference: str = "Any Gender",
    dropoff: str | None = None,
    prior: str | None = None,
) -> dict[str, Any]:
    """One raw_sponsor row for match tests."""
    low, high = AGE_PREF_BOUNDS[age_preference]
    return {
        "sponsor_id": sponsor_id,
        "email": f"s{sponsor_id}@x.com",
        "sponsor_type": sponsor_type,
        "num_children": num_children,
        "age_preference": age_preference,
        "gender_preference": gender_preference,
        "dropoff_location": dropoff,
        "pref_age_min": low,
        "pref_age_max": high,
        "prior_assigned_angel_ids": prior,
    }


def family_row(
    family_id: int, location: str = "TSA (Hancock)"
) -> dict[str, Any]:
    """One raw_family row for match tests."""
    return {
        "family_id": family_id,
        "primary_email": f"f{family_id}@x.com",
        "gift_pickup_location": location,
    }


def child_row(
    angel_id: str,
    family_id: int | None,
    age: int,
    gender: str = "Male",
    special: str = "",
    status: str = "audited",
    ignore: bool = False,
) -> dict[str, Any]:
    """One sparse persistent-final child row for match tests."""
    return {
        "zite_record_id": f"rec_{angel_id}",
        "angel_id": angel_id,
        "family_id": family_id,
        "age_at_xmas": age,
        "gender": gender,
        "special_needs": special or None,
        "status": status,
        "audited": status == "audited",
        "ignore": ignore,
    }


def plan(row: dict[str, Any]) -> SponsorPlan:
    """Build a pool SponsorPlan from a raw sponsor row."""
    return SponsorPlan(
        sponsor_row=row,
        sponsor_id=row["sponsor_id"],
        num_children=int(row["num_children"]),
        is_special_needs=row["age_preference"] == SPECIAL_NEEDS_LABEL,
        is_pre_assigned=False,
    )


def pool_family(
    family_id: int,
    children: list[dict[str, Any]],
    location: str = "TSA (Hancock)",
) -> PoolFamily:
    """Build a PoolFamily from child rows."""
    return PoolFamily(
        family_id=family_id,
        family_row=family_row(family_id, location),
        children=children,
    )


def test_parse_prior_assigned() -> None:
    """Free-text angel lists split, strip, and uppercase (§8.5)."""
    assert parse_prior_assigned("3b, 4a  5c") == ["3B", "4A", "5C"]
    assert parse_prior_assigned("  ") == []


def test_canonical_location(cfg: Config) -> None:
    """Locations reduce to hancock/calumet buckets (§8.3)."""
    groups = cfg.location_groups
    assert canonical_location("TSA (Hancock)", groups) == "hancock"
    assert canonical_location("Hancock", groups) == "hancock"
    assert canonical_location("CCAM (Calumet)", groups) == "calumet"
    assert canonical_location(None, groups) is None


def test_family_score_nonbinary_hard_rule(cfg: Config) -> None:
    """Non-binary children pair only with Any Gender sponsors (§8.3)."""
    sponsor = plan(sponsor_row(1, gender_preference="Male"))
    family = pool_family(1, [child_row("1A", 1, 8, gender="Non-binary")])
    assert family_score(sponsor, family, cfg, 1_000_000.0) is None
    any_gender = plan(sponsor_row(1))
    assert family_score(any_gender, family, cfg, 1_000_000.0) is not None


def test_family_score_location_tier(cfg: Config) -> None:
    """Location match scores the location tier; indifference never penalizes (§8.3)."""
    sponsor = plan(sponsor_row(1, dropoff="Hancock"))
    matching = pool_family(1, [child_row("1A", 1, 8)], location="TSA (Hancock)")
    mismatching = pool_family(
        2, [child_row("2A", 2, 8)], location="CCAM (Calumet)"
    )
    indifferent = plan(sponsor_row(2, dropoff="Any Location"))
    sn_weight = 1_000_000.0
    score_match = family_score(sponsor, matching, cfg, sn_weight)
    score_mismatch = family_score(sponsor, mismatching, cfg, sn_weight)
    assert score_match is not None and score_mismatch is not None
    assert score_match > score_mismatch
    assert family_score(indifferent, matching, cfg, sn_weight) == family_score(
        indifferent,
        pool_family(3, [child_row("3A", 3, 8)], location="Calumet"),
        cfg,
        sn_weight,
    )


def test_family_score_age_and_gender_fractions(cfg: Config) -> None:
    """Age and gender tiers score by in-range / matching fractions (§8.3)."""
    sponsor = plan(
        sponsor_row(
            1,
            age_preference="Middle School (11-13 yrs)",
            gender_preference="Female",
        )
    )
    family = pool_family(
        1,
        [
            child_row("1A", 1, 12, gender="Male"),
            child_row("1B", 1, 8, gender="Female"),
        ],
    )
    score = family_score(sponsor, family, cfg, 1_000_000.0)
    # location tier: sponsor indifferent (no dropoff) scores full weight
    expected = (
        cfg.matchmaking.location_weight
        + cfg.matchmaking.age_weight * 0.5
        + cfg.matchmaking.gender_weight * 0.5
    )
    assert score == pytest.approx(expected)


def test_special_needs_tier_dominates(cfg: Config) -> None:
    """Tier 0 outweighs all lower tiers combined (§8.3)."""
    sn_sponsor = plan(
        sponsor_row(1, age_preference=SPECIAL_NEEDS_LABEL, dropoff="Calumet")
    )
    sn_weight = special_needs_weight(cfg, 25)
    special_family = pool_family(
        1, [child_row("1A", 1, 8, special="Autism")], location="Calumet"
    )
    perfect_location_family = pool_family(
        2, [child_row("2A", 2, 8)], location="TSA (Hancock)"
    )
    score_special = family_score(sn_sponsor, special_family, cfg, sn_weight)
    score_location = family_score(
        sn_sponsor, perfect_location_family, cfg, sn_weight
    )
    assert score_special is not None and score_location is not None
    assert score_special > score_location


def test_best_subset_exact_counts_and_tiebreak() -> None:
    """Subsets must sum exactly; ties break to lowest family ids (§8.4)."""
    families = [
        pool_family(1, [child_row("1A", 1, 8)]),
        pool_family(2, [child_row("2A", 2, 8)]),
        pool_family(3, [child_row("3A", 3, 8), child_row("3B", 3, 9)]),
    ]
    candidates = [(family, 10.0) for family in families]
    subset = best_subset(candidates, 2)
    assert subset is not None
    assert [family.family_id for family in subset] == [
        1,
        2,
    ]  # tie-break lowest ids
    assert best_subset(candidates, 5) is None  # no exact combination
    assert [
        family.family_id for family in (best_subset(candidates, 3) or [])
    ] == [1, 3]


def test_best_subset_prefers_higher_score() -> None:
    """Higher-scoring subsets win regardless of ids (§8.4)."""
    families = [
        pool_family(1, [child_row("1A", 1, 8)]),
        pool_family(2, [child_row("2A", 2, 8)]),
    ]
    subset = best_subset([(families[0], 1.0), (families[1], 50.0)], 1)
    assert subset is not None and subset[0].family_id == 2


def test_greedy_subset_finds_exact_combination() -> None:
    """The greedy fallback still honors exact counts (§8.4)."""
    families = [
        pool_family(1, [child_row("1A", 1, 8)]),
        pool_family(2, [child_row("2A", 2, 8)]),
        pool_family(3, [child_row("3A", 3, 8), child_row("3B", 3, 9)]),
    ]
    candidates = sorted(
        [(family, 10.0) for family in families],
        key=lambda pair: (-pair[1], pair[0].family_id),
    )
    subset = greedy_subset(candidates, 3)
    assert subset is not None
    assert sum(family.child_count for family in subset) == 3
    assert greedy_subset(candidates, 5) is None


def test_reconcile_pre_assignments_happy_path() -> None:
    """Pre-assigned angels are honored verbatim (§8.5)."""
    sponsors = [sponsor_row(1, num_children=2, prior="1A")]
    children = [child_row("1A", 1, 8), child_row("1B", 1, 9)]
    plans, report = reconcile_pre_assignments(sponsors, children)
    assert plans[1].assigned_angel_ids == ["1A", "1B"]  # sibling added
    assert report == []


def test_reconcile_pre_assignments_unknown_id_dropped() -> None:
    """Unknown angel ids are dropped and reported (§8.5)."""
    sponsors = [sponsor_row(1, num_children=2, prior="1A, 9Z")]
    children = [child_row("1A", 1, 8), child_row("1B", 1, 9)]
    plans, report = reconcile_pre_assignments(sponsors, children)
    assert plans[1].assigned_angel_ids == [
        "1A",
        "1B",
    ]  # 1B sibling fills the slot
    assert any("unknown angel_id 9Z" in entry["reason"] for entry in report)


def test_reconcile_pre_assignments_duplicate_earlier_sponsor_wins() -> None:
    """Duplicate angel ids: earlier sponsor_id keeps the angel (§8.5)."""
    sponsors = [
        sponsor_row(1, num_children=1, prior="1A"),
        sponsor_row(2, num_children=1, prior="1A"),
    ]
    children = [child_row("1A", 1, 8), child_row("2A", 2, 9)]
    plans, report = reconcile_pre_assignments(sponsors, children)
    assert plans[1].assigned_angel_ids == ["1A"]
    assert plans[2].assigned_angel_ids == []
    assert any("duplicate angel_id 1A" in entry["reason"] for entry in report)
    assert {
        entry["ref"] for entry in report if "duplicate" in entry["reason"]
    } == {"1", "2"}


def test_reconcile_pre_assignments_split_family_flagged() -> None:
    """Siblings beyond num_children flag a split family (§8.5)."""
    sponsors = [sponsor_row(1, num_children=1, prior="1A")]
    children = [child_row("1A", 1, 8), child_row("1B", 1, 9)]
    plans, report = reconcile_pre_assignments(sponsors, children)
    assert plans[1].assigned_angel_ids == ["1A"]
    assert any("would be split" in entry["reason"] for entry in report)


def _final_frame(entity: str, partials: list[dict[str, Any]]) -> pl.DataFrame:
    """Build a complete persistent-final frame from sparse input rows."""
    return build_final_frame(entity, partials)


def _seed_match_workbook(
    cfg: Config,
    sponsors: list[dict[str, Any]],
    families: list[dict[str, Any]],
    children: list[dict[str, Any]],
) -> None:
    """Write complete persistent-final stores from sparse match inputs."""
    write_sheets(
        cfg.excel_file,
        "audit",
        {
            "final_sponsor": _final_frame("sponsor", sponsors),
            "final_family": _final_frame("family", families),
            "final_child": _final_frame("child", children),
        },
        xmas=cfg.xmas,
    )


def _assert_only_owned_fields_changed(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    entity: str,
    owned_columns: tuple[str, ...],
) -> None:
    """Assert complete key preservation and no writes outside owned fields."""
    key = ENTITY_SPECS[entity]["key"]
    assert len(after) == len(before)
    assert [row[key] for row in after] == [row[key] for row in before]
    before_by_key = {row[key]: row for row in before}
    for actual in after:
        expected = before_by_key[actual[key]]
        assert {
            column: value
            for column, value in actual.items()
            if column not in owned_columns
        } == {
            column: value
            for column, value in expected.items()
            if column not in owned_columns
        }


def test_run_patches_only_assignment_fields_on_complete_stores(
    cfg: Config,
) -> None:
    """Ignored and raw-missing rows survive with every other field intact."""
    ignored_family = family_row(90) | {
        "first_name": "KEEP-FAMILY-NAME",
        "source": "KEEP-FAMILY-SOURCE",
        "potential_duplicate": True,
        "accepts_disclaimer": True,
        "status": "ignored",
        "audited_at": "KEEP-FAMILY-AUDIT",
        "clarification_notes": "KEEP-FAMILY-NOTE",
        "ignore": True,
        "assigned_sponsor_id": 777,
        "assigned_angel_ids": "KEEP-FAMILY-ASSIGNMENT",
        "num_angels_in_family": 777,
    }
    raw_missing_family = family_row(91) | {
        "zite_record_id": None,
        "source": "PERSISTED-FAMILY-ONLY",
        "potential_duplicate": True,
        "status": "needs_clarification",
        "clarification_notes": "KEEP-RAW-MISSING-FAMILY",
        "assigned_sponsor_id": 888,
        "assigned_angel_ids": "OLD-FAMILY-ASSIGNMENT",
        "num_angels_in_family": 888,
    }
    ignored_sponsor = sponsor_row(2, num_children=1, prior="1A") | {
        "first_name": "KEEP-SPONSOR-NAME",
        "source": "KEEP-SPONSOR-SOURCE",
        "potential_duplicate": True,
        "status": "ignored",
        "audited_at": "KEEP-SPONSOR-AUDIT",
        "clarification_notes": "KEEP-SPONSOR-NOTE",
        "ignore": True,
        "assigned_angel_ids": "KEEP-SPONSOR-ASSIGNMENT",
        "num_angels_assigned": 777,
    }
    raw_missing_sponsor = sponsor_row(3, num_children=1) | {
        "zite_record_id": None,
        "source": "PERSISTED-SPONSOR-ONLY",
        "potential_duplicate": True,
        "clarification_notes": "KEEP-RAW-MISSING-SPONSOR",
        "assigned_angel_ids": "OLD-SPONSOR-ASSIGNMENT",
        "num_angels_assigned": 888,
    }
    families = [family_row(1), ignored_family, raw_missing_family]
    sponsors = [
        sponsor_row(1, num_children=1),
        ignored_sponsor,
        raw_missing_sponsor,
    ]
    children = [child_row("1A", 1, 8)]
    _seed_match_workbook(cfg, sponsors, families, children)
    before_family = read_final(cfg, "family")
    before_sponsor = read_final(cfg, "sponsor")

    run(cfg)

    after_family = read_final(cfg, "family")
    after_sponsor = read_final(cfg, "sponsor")
    _assert_only_owned_fields_changed(
        before_family, after_family, "family", FAMILY_ASSIGNMENT_COLUMNS
    )
    _assert_only_owned_fields_changed(
        before_sponsor, after_sponsor, "sponsor", SPONSOR_ASSIGNMENT_COLUMNS
    )
    families_by_id = {row["family_id"]: row for row in after_family}
    sponsors_by_id = {row["sponsor_id"]: row for row in after_sponsor}
    assert families_by_id[90] == before_family[1]
    assert sponsors_by_id[2] == before_sponsor[1]
    assert families_by_id[1]["assigned_sponsor_id"] == 1
    assert families_by_id[1]["assigned_angel_ids"] == "1A"
    assert families_by_id[91]["assigned_sponsor_id"] is None
    assert families_by_id[91]["assigned_angel_ids"] is None
    assert families_by_id[91]["num_angels_in_family"] == 0
    assert sponsors_by_id[3]["assigned_angel_ids"] is None
    assert sponsors_by_id[3]["num_angels_assigned"] == 0
    assert not [
        name
        for name in list_sheets(cfg.excel_file)
        if name.startswith("adj_") or "submissions" in name
    ]


def test_run_excludes_ignored_and_unlinked_rows_from_match_pools(
    cfg: Config,
) -> None:
    """Ignore, family-linkage, and angel-linkage gates preserve stored rows."""
    ignored_sponsor = sponsor_row(2, num_children=1, prior="2A") | {
        "ignore": True,
        "status": "ignored",
        "assigned_angel_ids": "KEEP-IGNORED-SPONSOR",
        "num_angels_assigned": 99,
    }
    ignored_family = family_row(2) | {
        "ignore": True,
        "status": "ignored",
        "assigned_sponsor_id": 99,
        "assigned_angel_ids": "KEEP-IGNORED-FAMILY",
        "num_angels_in_family": 99,
    }
    sponsors = [sponsor_row(1, num_children=1), ignored_sponsor]
    families = [family_row(1), ignored_family, family_row(3), family_row(4)]
    children = [
        child_row("1A", 1, 8, ignore=True, status="ignored"),
        child_row("2A", 2, 8),
        child_row("3A", 3, 8),
        child_row("4A", 4, 8) | {"angel_id": None},
        child_row("9A", None, 8),
    ]
    _seed_match_workbook(cfg, sponsors, families, children)
    before_ignored_sponsor = read_final(cfg, "sponsor")[1]
    before_ignored_family = read_final(cfg, "family")[1]

    run(cfg)

    final_families = read_final(cfg, "family")
    families_by_id = {row["family_id"]: row for row in final_families}
    assert len(final_families) == 4
    assert families_by_id[1]["assigned_sponsor_id"] is None
    assert families_by_id[1]["num_angels_in_family"] == 0
    assert families_by_id[2] == before_ignored_family
    assert families_by_id[3]["assigned_sponsor_id"] == 1
    assert families_by_id[3]["assigned_angel_ids"] == "3A"
    assert families_by_id[4]["assigned_sponsor_id"] is None
    assert families_by_id[4]["num_angels_in_family"] == 1
    final_sponsors = read_final(cfg, "sponsor")
    sponsors_by_id = {row["sponsor_id"]: row for row in final_sponsors}
    assert sponsors_by_id[1]["assigned_angel_ids"] == "3A"
    assert sponsors_by_id[2] == before_ignored_sponsor
    report = read_sheet(cfg.excel_file, "match_report")
    assert report is not None
    assert not [
        row
        for row in report.to_dicts()
        if row["kind"] in {"family", "sponsor"} and row["ref"] == "2"
    ]


def test_run_matches_families_exactly_and_indivisibly(cfg: Config) -> None:
    """Whole families go to one sponsor with exact counts (§8.2)."""
    sponsors = [sponsor_row(1, num_children=2), sponsor_row(2, num_children=1)]
    families = [family_row(1), family_row(2)]
    children = [
        child_row("1A", 1, 10),
        child_row("1B", 1, 8, gender="Female"),
        child_row("2A", 2, 5),
    ]
    _seed_match_workbook(cfg, sponsors, families, children)
    run(cfg)
    final_family = read_sheet(cfg.excel_file, "final_family")
    final_sponsor = read_sheet(cfg.excel_file, "final_sponsor")
    assert final_family is not None and final_sponsor is not None
    assert final_family["assigned_sponsor_id"].to_list() == [1, 2]
    assert final_family["assigned_angel_ids"].to_list() == ["1A,1B", "2A"]
    by_id = {row["sponsor_id"]: row for row in final_sponsor.to_dicts()}
    assert by_id[1]["assigned_angel_ids"] == "1A,1B"
    assert by_id[2]["assigned_angel_ids"] == "2A"


def test_run_phase_order_special_needs_first(cfg: Config) -> None:
    """Special-needs sponsors match before Individuals (§8.2)."""
    sponsors = [
        sponsor_row(1, num_children=1),  # Individual, lower id
        sponsor_row(2, num_children=1, age_preference=SPECIAL_NEEDS_LABEL),
    ]
    families = [family_row(1), family_row(2)]
    children = [
        child_row("1A", 1, 8, special="Autism"),
        child_row("2A", 2, 8),
    ]
    _seed_match_workbook(cfg, sponsors, families, children)
    run(cfg)
    final_family = read_sheet(cfg.excel_file, "final_family")
    assert final_family is not None
    assert final_family["assigned_sponsor_id"].to_list() == [
        2,
        1,
    ]  # SN sponsor first


def test_run_organization_walmart_after_individuals(cfg: Config) -> None:
    """Organization and Walmart sponsors follow Individuals (§8.2)."""
    sponsors = [
        sponsor_row(1, num_children=1, sponsor_type="Walmart"),
        sponsor_row(2, num_children=1, sponsor_type="Organization"),
        sponsor_row(3, num_children=1, sponsor_type="Individual"),
    ]
    families = [family_row(1), family_row(2), family_row(3)]
    children = [
        child_row("1A", 1, 8),
        child_row("2A", 2, 8),
        child_row("3A", 3, 8),
    ]
    _seed_match_workbook(cfg, sponsors, families, children)
    run(cfg)
    final_family = read_sheet(cfg.excel_file, "final_family")
    assert final_family is not None
    # sponsor 3 (Individual) picks first (lowest family_id), then
    # Organization/Walmart phase in sponsor_id order
    assert final_family["assigned_sponsor_id"].to_list() == [3, 1, 2]


def test_run_nonbinary_family_unmatched_with_reason(cfg: Config) -> None:
    """Families with Non-binary children stay unmatched for gender-specific sponsors.

    Covers the §8.3 hard rule.
    """
    sponsors = [sponsor_row(1, num_children=1, gender_preference="Male")]
    families = [family_row(1)]
    children = [child_row("1A", 1, 8, gender="Non-binary")]
    _seed_match_workbook(cfg, sponsors, families, children)
    run(cfg)
    report = read_sheet(cfg.excel_file, "match_report")
    final_family = read_sheet(cfg.excel_file, "final_family")
    assert final_family is not None and final_family[
        "assigned_sponsor_id"
    ].to_list() == [None]
    assert report is not None
    reasons = report["reason"].to_list()
    assert any("non-binary" in reason for reason in reasons)


def test_run_no_exact_count_combination_reported(cfg: Config) -> None:
    """Sponsors that cannot be filled exactly are reported (§8.2, §8.6)."""
    sponsors = [sponsor_row(1, num_children=2)]
    families = [family_row(1), family_row(2)]
    children = [
        child_row("1A", 1, 8),
        child_row("2A", 2, 8),
        child_row("2B", 2, 9),
        child_row("2C", 2, 10),
    ]
    _seed_match_workbook(cfg, sponsors, families, children)
    run(cfg)
    report = read_sheet(cfg.excel_file, "match_report")
    assert report is not None
    kinds = list(
        zip(report["kind"].to_list(), report["ref"].to_list(), strict=False)
    )
    assert ("sponsor", "1") in kinds
    assert ("family", "1") in kinds and ("family", "2") in kinds


def test_run_pre_assigned_sponsor_excluded_from_pool(cfg: Config) -> None:
    """Pre-assigned sponsors keep manual lists and skip the pool (§8.5)."""
    sponsors = [
        sponsor_row(1, num_children=1, prior="1A"),
        sponsor_row(2, num_children=2),
    ]
    families = [family_row(1), family_row(2)]
    children = [
        child_row("1A", 1, 8),
        child_row("2A", 2, 8),
        child_row("2B", 2, 9),
    ]
    _seed_match_workbook(cfg, sponsors, families, children)
    run(cfg)
    final_sponsor = read_sheet(cfg.excel_file, "final_sponsor")
    final_family = read_sheet(cfg.excel_file, "final_family")
    assert final_sponsor is not None and final_family is not None
    by_id = {row["sponsor_id"]: row for row in final_sponsor.to_dicts()}
    assert by_id[1]["assigned_angel_ids"] == "1A"
    assert by_id[2]["assigned_angel_ids"] == "2A,2B"
    assert final_family["assigned_sponsor_id"].to_list() == [1, 2]


def test_run_prior_fills_unassigned_partial_family(cfg: Config) -> None:
    """Full run: a partially covered prior family gets assigned.

    §8.5 reconciliation leaves it unassigned; propagation fills it in.
    """
    sponsors = [sponsor_row(1, num_children=1, prior="1A")]
    families = [family_row(1)]
    children = [child_row("1A", 1, 8), child_row("1B", 1, 9)]
    _seed_match_workbook(cfg, sponsors, families, children)

    run(cfg)

    final_family = read_sheet(cfg.excel_file, "final_family")
    assert final_family is not None
    row = final_family.to_dicts()[0]
    # Sibling 1B exceeds num_children, so the §8.5 reconciliation left
    # the family unassigned; the prior propagation fills it in.
    assert row["assigned_sponsor_id"] == 1
    assert row["assigned_angel_ids"] == "1A"
    assert row["num_angels_in_family"] == 2
    # match_report keeps the split flag for manual follow-up.
    report = read_sheet(cfg.excel_file, "match_report")
    assert report is not None
    assert any("would be split" in r["reason"] for r in report.to_dicts())
    final_sponsor = read_sheet(cfg.excel_file, "final_sponsor")
    assert final_sponsor is not None
    assert final_sponsor["assigned_angel_ids"].to_list() == ["1A"]


def test_run_prior_leaves_fully_covered_family_untouched(cfg: Config) -> None:
    """Full run: a fully covered family keeps its all-children list."""
    sponsors = [sponsor_row(1, num_children=2, prior="1A")]
    families = [family_row(1)]
    children = [child_row("1A", 1, 8), child_row("1B", 1, 9)]
    _seed_match_workbook(cfg, sponsors, families, children)

    run(cfg)

    final_family = read_sheet(cfg.excel_file, "final_family")
    assert final_family is not None
    row = final_family.to_dicts()[0]
    # Sibling integrity: 1B was added, list stays the full family.
    assert row["assigned_sponsor_id"] == 1
    assert row["assigned_angel_ids"] == "1A,1B"


def test_run_archives_previous_sheets_and_exports_csv(cfg: Config) -> None:
    """Archives snapshot complete stores; match CSV is exported (§8.6)."""
    sponsors = [sponsor_row(1, num_children=1)]
    families = [
        family_row(1),
        family_row(90)
        | {
            "ignore": True,
            "status": "ignored",
            "source": "KEEP-ARCHIVE",
            "assigned_sponsor_id": 777,
        },
    ]
    children = [child_row("1A", 1, 8)]
    _seed_match_workbook(cfg, sponsors, families, children)
    run(cfg)
    previous_family = read_final(cfg, "family")
    previous_sponsor = read_final(cfg, "sponsor")
    archives_before = set(list_sheets(cfg.excel_file))

    run(cfg)

    sheets = list_sheets(cfg.excel_file)
    new_archives = set(sheets) - archives_before
    family_archives = [
        name for name in new_archives if name.startswith("final_family_")
    ]
    sponsor_archives = [
        name for name in new_archives if name.startswith("final_sponsor_")
    ]
    assert len(family_archives) == len(sponsor_archives) == 1
    archived_family = read_sheet(cfg.excel_file, family_archives[0])
    archived_sponsor = read_sheet(cfg.excel_file, sponsor_archives[0])
    assert archived_family is not None and archived_sponsor is not None
    assert archived_family.to_dicts() == previous_family
    assert archived_sponsor.to_dicts() == previous_sponsor
    assert archived_family.height == 2
    assert archived_family.to_dicts()[1]["source"] == "KEEP-ARCHIVE"
    assert not [
        name
        for name in sheets
        if name.startswith("adj_") or "submissions" in name
    ]
    report_csv = Path(cfg.match_report_csv)
    assert report_csv.is_file()
    content = report_csv.read_text().splitlines()
    assert content[0] == "kind,ref,reason"


def test_run_overwrite_replaces_without_archives(cfg: Config) -> None:
    """--overwrite replaces prior match outputs in place, no archives (§8.6)."""
    sponsors = [sponsor_row(1, num_children=1)]
    families = [family_row(1)]
    children = [child_row("1A", 1, 8)]
    _seed_match_workbook(cfg, sponsors, families, children)
    run(cfg, overwrite=True)
    run(cfg, overwrite=True)
    sheets = list_sheets(cfg.excel_file)
    assert not [name for name in sheets if name.startswith("final_family_")], (
        "overwrite must not archive previous sheets"
    )
    final_family = read_sheet(cfg.excel_file, "final_family")
    assert final_family is not None
    assert final_family["assigned_sponsor_id"].to_list() == [1]


def test_run_writes_family_and_sponsor_angel_counts(cfg: Config) -> None:
    """final_family.num_angels_in_family counts final_child rows per family.

    final_sponsor.num_angels_assigned counts the ids in
    ``assigned_angel_ids`` (§8.6).
    """
    sponsors = [
        sponsor_row(1, num_children=2),
        sponsor_row(2, num_children=1),
        sponsor_row(3, num_children=2),
    ]
    families = [family_row(1), family_row(2), family_row(3)]
    children = [
        child_row("1A", 1, 10),
        child_row("1B", 1, 8, gender="Female"),
        child_row("2A", 2, 5),
    ]
    _seed_match_workbook(cfg, sponsors, families, children)
    run(cfg)
    final_family = read_sheet(cfg.excel_file, "final_family")
    assert final_family is not None
    counts = dict(
        zip(
            final_family["family_id"].to_list(),
            final_family["num_angels_in_family"].to_list(),
            strict=True,
        )
    )
    assert counts == {1: 2, 2: 1, 3: 0}
    final_sponsor = read_sheet(cfg.excel_file, "final_sponsor")
    assert final_sponsor is not None
    by_id = {row["sponsor_id"]: row for row in final_sponsor.to_dicts()}
    assert by_id[1]["num_angels_assigned"] == 2
    assert by_id[2]["num_angels_assigned"] == 1
    assert by_id[3]["num_angels_assigned"] == 0  # unmatched sponsor
    assert "num_angels_in_fmaily" not in final_sponsor.columns


def test_run_family_count_includes_unaudited_rows(cfg: Config) -> None:
    """num_angels_in_family counts every non-ignored final_child row (§7.5)."""
    sponsors = [sponsor_row(1, num_children=1)]
    families = [family_row(1)]
    children = [
        child_row("1A", 1, 8),
        child_row("1B", 1, 9, status="new"),
    ]
    _seed_match_workbook(cfg, sponsors, families, children)
    run(cfg)
    final_family = read_sheet(cfg.excel_file, "final_family")
    assert final_family is not None
    assert final_family["num_angels_in_family"].to_list() == [2]


def test_run_rewrites_only_child_assignment_columns(cfg: Config) -> None:
    """Match writes the two child assignment columns and nothing else.

    The child row is the assignment grain (D1): every other final-child
    column stays byte-identical across a run.
    """
    sponsors = [sponsor_row(1, num_children=1)]
    families = [family_row(1)]
    children = [
        child_row("1A", 1, 8)
        | {
            "dob": "2018-01-02",
            "source": "KEEP-CHILD-SOURCE",
            "generated_info_sheet": True,
            "clarification_notes": "KEEP-CHILD-NOTE",
        }
    ]
    _seed_match_workbook(cfg, sponsors, families, children)
    before = read_final(cfg, "child")

    run(cfg)

    after = read_final(cfg, "child")
    assert [row["zite_record_id"] for row in after] == [
        row["zite_record_id"] for row in before
    ]
    before_by_key = {row["zite_record_id"]: row for row in before}
    for actual in after:
        expected = before_by_key[actual["zite_record_id"]]
        assert {
            column: value
            for column, value in actual.items()
            if column not in CHILD_ASSIGNMENT_COLUMNS
        } == {
            column: value
            for column, value in expected.items()
            if column not in CHILD_ASSIGNMENT_COLUMNS
        }
    # The participating child carries the sponsor and the automatic source.
    assert after[0]["assigned_sponsor_id"] == 1
    assert after[0]["assignment_source"] == "auto"


def test_run_clears_stale_automatic_assignments(cfg: Config) -> None:
    """A previous automatic run's assignments are recomputed from scratch.

    The family that no sponsor takes keeps its children unassigned, and
    their stale ``auto`` markers are cleared (D3).
    """
    sponsors = [sponsor_row(1, num_children=1)]
    families = [family_row(1), family_row(2)]
    children = [child_row("1A", 1, 8), child_row("2A", 2, 8)]
    _seed_match_workbook(cfg, sponsors, families, children)
    final_children = read_final(cfg, "child")
    for row in final_children:
        row["assigned_sponsor_id"] = 9
        row["assignment_source"] = "auto"
    write_sheets(
        cfg.excel_file,
        "audit",
        {"final_child": _final_frame("child", final_children)},
        xmas=cfg.xmas,
    )

    run(cfg)

    after = {row["angel_id"]: row for row in read_final(cfg, "child")}
    assert after["1A"]["assigned_sponsor_id"] == 1
    assert after["1A"]["assignment_source"] == "auto"
    assert after["2A"]["assigned_sponsor_id"] is None
    assert after["2A"]["assignment_source"] is None


def test_run_manual_assignment_protects_its_whole_family(
    cfg: Config,
) -> None:
    """A manual child keeps its family out of the automatic pool (D3).

    The manual assignment survives, the family's other children are left
    alone, and the report says "manual assignment".
    """
    sponsors = [sponsor_row(1, num_children=1), sponsor_row(2, num_children=1)]
    families = [family_row(1), family_row(2)]
    children = [
        child_row("1A", 1, 8),
        child_row("1B", 1, 9),
        child_row("2A", 2, 8),
    ]
    _seed_match_workbook(cfg, sponsors, families, children)
    final_children = read_final(cfg, "child")
    for row in final_children:
        if row["angel_id"] == "1A":
            row["assigned_sponsor_id"] = 2
            row["assignment_source"] = "manual"
    write_sheets(
        cfg.excel_file,
        "audit",
        {"final_child": _final_frame("child", final_children)},
        xmas=cfg.xmas,
    )

    run(cfg)

    after = {row["angel_id"]: row for row in read_final(cfg, "child")}
    assert after["1A"]["assigned_sponsor_id"] == 2
    assert after["1A"]["assignment_source"] == "manual"
    assert after["1B"]["assigned_sponsor_id"] is None
    assert after["1B"]["assignment_source"] is None
    assert after["2A"]["assigned_sponsor_id"] == 1
    families_by_id = {
        row["family_id"]: row for row in read_final(cfg, "family")
    }
    assert families_by_id[1]["assigned_sponsor_id"] == 2
    assert families_by_id[1]["assigned_angel_ids"] == "1A"
    report = read_sheet(cfg.excel_file, "match_report")
    assert report is not None
    reasons = {
        row["ref"]: row["reason"]
        for row in report.to_dicts()
        if row["kind"] == "family"
    }
    assert reasons["1"] == "manual assignment"


def test_run_unaudited_children_excluded(cfg: Config) -> None:
    """Only audited children participate (§8.1)."""
    sponsors = [sponsor_row(1, num_children=1)]
    families = [family_row(1), family_row(2)]
    children = [child_row("1A", 1, 8, status="new"), child_row("2A", 2, 8)]
    _seed_match_workbook(cfg, sponsors, families, children)
    run(cfg)
    final_family = read_sheet(cfg.excel_file, "final_family")
    assert final_family is not None
    assert final_family["assigned_sponsor_id"].to_list() == [None, 1]
    report = read_sheet(cfg.excel_file, "match_report")
    assert report is not None
    assert any(
        "no audited children" in reason for reason in report["reason"].to_list()
    )


def test_run_requires_persistent_final_stores(cfg: Config) -> None:
    """Missing persistent finals fail with final-sheet guidance."""
    from ccat.match import MatchError

    with pytest.raises(MatchError, match="final_family/final_sponsor") as error:
        run(cfg)
    assert "adj" not in str(error.value)


def test_run_rejects_incomplete_persistent_schema(cfg: Config) -> None:
    """A sparse final store fails before match can replace or remove state."""
    from ccat.audit import AuditError
    from openpyxl import load_workbook

    _seed_match_workbook(
        cfg,
        [sponsor_row(1, num_children=1)],
        [family_row(1)],
        [child_row("1A", 1, 8)],
    )
    workbook = load_workbook(cfg.excel_file)
    sheet = workbook["final_family"]
    source_column = next(
        cell.column for cell in sheet[1] if cell.value == "source"
    )
    sheet.delete_cols(source_column)
    workbook.save(cfg.excel_file)
    workbook.close()
    before = Path(cfg.excel_file).read_bytes()

    with pytest.raises(
        AuditError, match="final_family.*persistent full schema"
    ):
        run(cfg)
    assert Path(cfg.excel_file).read_bytes() == before


@pytest.mark.parametrize(
    ("replacement", "message"),
    [(None, "missing family_id"), (1, "duplicate family_id")],
)
def test_run_rejects_invalid_persistent_keys(
    cfg: Config, replacement: int | None, message: str
) -> None:
    """Null and duplicate full-store keys fail before match writes."""
    from ccat.audit import AuditError
    from openpyxl import load_workbook

    _seed_match_workbook(
        cfg,
        [sponsor_row(1, num_children=1)],
        [family_row(1), family_row(2)],
        [child_row("1A", 1, 8), child_row("2A", 2, 8)],
    )
    workbook = load_workbook(cfg.excel_file)
    sheet = workbook["final_family"]
    key_column = next(
        cell.column for cell in sheet[1] if cell.value == "family_id"
    )
    sheet.cell(row=3, column=key_column).value = replacement
    workbook.save(cfg.excel_file)
    workbook.close()
    before = Path(cfg.excel_file).read_bytes()

    with pytest.raises(AuditError, match=message):
        run(cfg)
    assert Path(cfg.excel_file).read_bytes() == before


def test_run_test_mode_processes_only_first_sponsor(cfg: Config) -> None:
    """--test stops after the first full sponsor (§ match test mode)."""
    sponsors = [sponsor_row(1, num_children=1), sponsor_row(2, num_children=1)]
    families = [family_row(1), family_row(2)]
    children = [child_row("1A", 1, 8), child_row("2A", 2, 8)]
    _seed_match_workbook(cfg, sponsors, families, children)
    run(cfg, test=True)
    final_family = read_sheet(cfg.excel_file, "final_family")
    final_sponsor = read_sheet(cfg.excel_file, "final_sponsor")
    assert final_family is not None and final_sponsor is not None
    assert final_family["assigned_sponsor_id"].to_list() == [1, None]
    by_id = {row["sponsor_id"]: row for row in final_sponsor.to_dicts()}
    assert by_id[1]["assigned_angel_ids"] == "1A"
    assert by_id[2]["assigned_angel_ids"] is None  # never processed
    report = read_sheet(cfg.excel_file, "match_report")
    assert report is not None
    assert "test" in report["kind"].to_list()  # partial run self-documented
    csv = Path(cfg.match_report_csv).read_text()
    assert "partial test run" in csv


def test_run_test_mode_follows_phase_order(cfg: Config) -> None:
    """--test picks the first sponsor in phase order, not lowest id."""
    sponsors = [
        sponsor_row(1, num_children=1),
        sponsor_row(2, num_children=1, age_preference=SPECIAL_NEEDS_LABEL),
    ]
    families = [family_row(1), family_row(2)]
    children = [
        child_row("1A", 1, 8, special="Autism"),
        child_row("2A", 2, 8),
    ]
    _seed_match_workbook(cfg, sponsors, families, children)
    run(cfg, test=True)
    final_family = read_sheet(cfg.excel_file, "final_family")
    assert final_family is not None
    # special-needs sponsor 2 is first in phase order despite higher id
    assert final_family["assigned_sponsor_id"].to_list() == [2, None]


def test_run_skip_match_reconciles_without_matching(cfg: Config) -> None:
    """--skip-match patches assignment fields without running the algorithm.

    Previously assigned angel lists and sponsor assignments carry over;
    ``num_angels_in_family`` refreshes from the complete final-child store;
    match_report is untouched.
    """
    sponsors = [sponsor_row(1, num_children=2)]
    families = [family_row(1), family_row(3)]
    children = [
        child_row("1A", 1, 8),
        child_row("3A", 3, 5),
        child_row("3B", 3, 6),
    ]
    _seed_match_workbook(cfg, sponsors, families, children)
    # A prior run's state: family 1 matched to sponsor 1, with report output.
    report_csv = Path(cfg.match_report_csv)
    report_csv.write_text("kind,ref,reason\nfamily,2,unmatched\n")
    write_sheets(
        cfg.excel_file,
        "match",
        {
            "match_report": pl.DataFrame(
                [{"kind": "family", "ref": "2", "reason": "unmatched"}],
                schema={
                    "kind": pl.String,
                    "ref": pl.String,
                    "reason": pl.String,
                },
            )
        },
    )
    final_families = read_final(cfg, "family")
    for row in final_families:
        if row["family_id"] == 1:
            row["assigned_sponsor_id"] = 1
            row["assigned_angel_ids"] = "1A"
            row["num_angels_in_family"] = 1
    final_sponsors = read_final(cfg, "sponsor")
    final_sponsors[0]["assigned_angel_ids"] = "1A"
    final_sponsors[0]["num_angels_assigned"] = 1
    # The child row is the assignment: the carried-over state lives there.
    final_children = read_final(cfg, "child")
    for row in final_children:
        if row["angel_id"] == "1A":
            row["assigned_sponsor_id"] = 1
            row["assignment_source"] = "auto"
    write_sheets(
        cfg.excel_file,
        "audit",
        {
            "final_family": _final_frame("family", final_families),
            "final_sponsor": _final_frame("sponsor", final_sponsors),
            "final_child": _final_frame("child", final_children),
        },
        xmas=cfg.xmas,
    )

    run(cfg, skip_match=True)

    final_family = read_sheet(cfg.excel_file, "final_family")
    assert final_family is not None
    rows = {row["family_id"]: row for row in final_family.to_dicts()}
    assert sorted(rows) == [1, 3]
    # Existing assignments carry over untouched.
    assert rows[1]["assigned_sponsor_id"] == 1
    assert rows[1]["assigned_angel_ids"] == "1A"
    # New family 3 has no sponsor; angel counts refresh from final_child.
    assert rows[3]["assigned_sponsor_id"] is None
    assert rows[3]["num_angels_in_family"] == 2
    # The persistent final store carries the reconciled values.
    final_after = {row["family_id"]: row for row in read_final(cfg, "family")}
    assert final_after[1]["assigned_sponsor_id"] == 1
    assert final_after[3]["num_angels_in_family"] == 2
    # final_sponsor reconciles the same way.
    final_sponsor = read_sheet(cfg.excel_file, "final_sponsor")
    assert final_sponsor is not None
    sponsors_by_id = {
        row["sponsor_id"]: row for row in final_sponsor.to_dicts()
    }
    assert sponsors_by_id[1]["assigned_angel_ids"] == "1A"
    assert sponsors_by_id[1]["num_angels_assigned"] == 1
    # match_report is untouched.
    report = read_sheet(cfg.excel_file, "match_report")
    assert report is not None
    assert report.to_dicts() == [
        {"kind": "family", "ref": "2", "reason": "unmatched"}
    ]
    assert report_csv.read_text() == "kind,ref,reason\nfamily,2,unmatched\n"
    assert not [
        name
        for name in list_sheets(cfg.excel_file)
        if name.startswith("match_report_")
    ]


def test_run_skip_match_reconciles_sponsors_without_matching(
    cfg: Config,
) -> None:
    """--skip-match derives sponsor summaries from the child assignments.

    A stale ``final_sponsor`` summary is not an input any more: the child
    row carries the assignment and the summary is recomputed from it.
    """
    sponsors = [sponsor_row(1, num_children=2), sponsor_row(3, num_children=1)]
    families = [family_row(1)]
    children = [child_row("1A", 1, 8)]
    _seed_match_workbook(cfg, sponsors, families, children)
    final_sponsors = read_final(cfg, "sponsor")
    final_sponsors[0]["assigned_angel_ids"] = "9Z"  # stale summary
    final_sponsors[0]["num_angels_assigned"] = 1
    final_children = read_final(cfg, "child")
    final_children[0]["assigned_sponsor_id"] = 1
    final_children[0]["assignment_source"] = "auto"
    write_sheets(
        cfg.excel_file,
        "audit",
        {
            "final_sponsor": _final_frame("sponsor", final_sponsors),
            "final_child": _final_frame("child", final_children),
        },
        xmas=cfg.xmas,
    )

    run(cfg, skip_match=True)

    final_sponsor = read_sheet(cfg.excel_file, "final_sponsor")
    assert final_sponsor is not None
    rows = {row["sponsor_id"]: row for row in final_sponsor.to_dicts()}
    assert sorted(rows) == [1, 3]
    # The summary comes from the child, so the stale value is replaced.
    assert rows[1]["assigned_angel_ids"] == "1A"
    assert rows[1]["num_angels_assigned"] == 1
    # New sponsor 3 has no angels.
    assert rows[3]["assigned_angel_ids"] is None
    assert rows[3]["num_angels_assigned"] == 0


def test_run_skip_match_propagates_prior_assigned_angel_ids(
    cfg: Config,
) -> None:
    """--skip-match applies the priors at child grain.

    The raw prior is authoritative; unknown ids drop out of the summary.
    """
    sponsors = [
        sponsor_row(1, num_children=2, prior="3b, 1A"),
        sponsor_row(2, num_children=1),
    ]
    families = [family_row(1)]
    children = [child_row("1A", 1, 8)]
    _seed_match_workbook(cfg, sponsors, families, children)
    final_sponsors = read_final(cfg, "sponsor")
    final_sponsors[0]["assigned_angel_ids"] = "9Z"  # stale carried-over list
    final_sponsors[0]["num_angels_assigned"] = 1
    write_sheets(
        cfg.excel_file,
        "audit",
        {"final_sponsor": _final_frame("sponsor", final_sponsors)},
    )

    run(cfg, skip_match=True)

    final_sponsor = read_sheet(cfg.excel_file, "final_sponsor")
    assert final_sponsor is not None
    rows = {row["sponsor_id"]: row for row in final_sponsor.to_dicts()}
    # Only the prior ids that resolve to a child count; 3B has no child.
    assert rows[1]["assigned_angel_ids"] == "1A"
    assert rows[1]["num_angels_assigned"] == 1
    # No prior -> no assigned child.
    assert rows[2]["assigned_angel_ids"] is None
    final_child = read_final(cfg, "child")
    assert final_child[0]["assigned_sponsor_id"] == 1
    assert final_child[0]["assignment_source"] == "manual"


def test_run_skip_match_prior_points_family_at_sponsor(cfg: Config) -> None:
    """A propagated prior angel sets its family's sponsor assignment."""
    # final_family has families 1 (stale assignment to vanished sponsor 2)
    # and 3 (unassigned); sponsor 1 carries prior angels 1A,1B.
    sponsors = [sponsor_row(1, num_children=2, prior="1A,1B")]
    families = [family_row(1), family_row(3)]
    children = [
        child_row("1A", 1, 8),
        child_row("1B", 1, 9),
        child_row("3A", 3, 5),
    ]
    _seed_match_workbook(cfg, sponsors, families, children)
    final_families = read_final(cfg, "family")
    for row in final_families:
        if row["family_id"] == 1:
            row["assigned_sponsor_id"] = 2  # stale: sponsor 2 is gone
            row["assigned_angel_ids"] = "1A"
    write_sheets(
        cfg.excel_file,
        "audit",
        {"final_family": _final_frame("family", final_families)},
    )

    run(cfg, skip_match=True)

    final_family = read_sheet(cfg.excel_file, "final_family")
    assert final_family is not None
    rows = {row["family_id"]: row for row in final_family.to_dicts()}
    # Prior is authoritative: family 1 points at sponsor 1 with the
    # prior angels; family 3 stays unassigned.
    assert rows[1]["assigned_sponsor_id"] == 1
    assert rows[1]["assigned_angel_ids"] == "1A,1B"
    assert rows[3]["assigned_sponsor_id"] is None


def test_run_skip_match_duplicate_prior_angels_raise(cfg: Config) -> None:
    """The same angel claimed by two sponsors fails (§8.5)."""
    from ccat.match import MatchError

    sponsors = [
        sponsor_row(1, num_children=1, prior="1A"),
        sponsor_row(2, num_children=1, prior="1A"),
    ]
    families = [family_row(1)]
    children = [child_row("1A", 1, 8)]
    _seed_match_workbook(cfg, sponsors, families, children)
    with pytest.raises(MatchError, match="duplicate angel"):
        run(cfg, skip_match=True)


def test_run_skip_match_prior_split_family_keeps_existing(
    cfg: Config,
) -> None:
    """Competing priors split the family at child grain (D11).

    Each prior child keeps its own sponsor; the family has no single
    sponsor, and its derived list names both placed angels.
    """
    sponsors = [
        sponsor_row(1, num_children=2, prior="1A"),
        sponsor_row(2, num_children=1, prior="1B"),
    ]
    families = [family_row(1)]
    children = [child_row("1A", 1, 8), child_row("1B", 1, 9)]
    _seed_match_workbook(cfg, sponsors, families, children)

    run(cfg, skip_match=True)

    final_family = read_sheet(cfg.excel_file, "final_family")
    assert final_family is not None
    row = final_family.to_dicts()[0]
    assert row["assigned_sponsor_id"] is None
    assert row["assigned_angel_ids"] == "1A,1B"
    final_child = read_final(cfg, "child")
    by_angel = {child["angel_id"]: child for child in final_child}
    assert by_angel["1A"]["assigned_sponsor_id"] == 1
    assert by_angel["1B"]["assigned_sponsor_id"] == 2
    assert by_angel["1A"]["assignment_source"] == "manual"
    assert by_angel["1B"]["assignment_source"] == "manual"
    final_sponsor = read_sheet(cfg.excel_file, "final_sponsor")
    assert final_sponsor is not None
    by_id = {r["sponsor_id"]: r for r in final_sponsor.to_dicts()}
    assert by_id[1]["assigned_angel_ids"] == "1A"
    assert by_id[2]["assigned_angel_ids"] == "1B"


def test_run_skip_match_prior_unknown_angel_warns(cfg: Config, caplog) -> None:
    """A prior angel missing from final_child warns; family untouched."""
    sponsors = [sponsor_row(1, num_children=2, prior="9Z")]
    families = [family_row(1)]
    children = [child_row("1A", 1, 8)]
    _seed_match_workbook(cfg, sponsors, families, children)

    with caplog.at_level("WARNING"):
        run(cfg, skip_match=True)

    assert "prior angel 9Z is not in final_child" in caplog.text
    final_family = read_sheet(cfg.excel_file, "final_family")
    assert final_family is not None
    row = final_family.to_dicts()[0]
    assert row["assigned_sponsor_id"] is None
    # No child resolves, so the sponsor summary is empty too.
    final_sponsor = read_sheet(cfg.excel_file, "final_sponsor")
    assert final_sponsor is not None
    assert final_sponsor["assigned_angel_ids"].to_list() == [None]
    assert final_sponsor["num_angels_assigned"].to_list() == [0]


def test_run_skip_match_prior_empty_string_falls_back(cfg: Config) -> None:
    """A whitespace-only prior counts as absent; child state carries over."""
    sponsors = [sponsor_row(1, num_children=2, prior="  ")]
    families = [family_row(1)]
    children = [child_row("1A", 1, 8)]
    _seed_match_workbook(cfg, sponsors, families, children)
    final_children = read_final(cfg, "child")
    final_children[0]["assigned_sponsor_id"] = 1  # carried over
    final_children[0]["assignment_source"] = "auto"
    write_sheets(
        cfg.excel_file,
        "audit",
        {"final_child": _final_frame("child", final_children)},
        xmas=cfg.xmas,
    )

    run(cfg, skip_match=True)

    final_sponsor = read_sheet(cfg.excel_file, "final_sponsor")
    assert final_sponsor is not None
    row = final_sponsor.to_dicts()[0]
    assert row["assigned_angel_ids"] == "1A"
    assert row["num_angels_assigned"] == 1


def test_run_skip_match_prior_ignores_stale_sponsor_summary(
    cfg: Config,
) -> None:
    """A stale sponsor summary is not an input: only the priors conflict."""
    sponsors = [sponsor_row(1, num_children=2, prior="1A"), sponsor_row(2)]
    families = [family_row(1)]
    children = [child_row("1A", 1, 8)]
    _seed_match_workbook(cfg, sponsors, families, children)
    final_sponsors = read_final(cfg, "sponsor")
    final_sponsors[1]["assigned_angel_ids"] = "1A,2A"  # stale summary
    final_sponsors[1]["num_angels_assigned"] = 2
    write_sheets(
        cfg.excel_file,
        "audit",
        {"final_sponsor": _final_frame("sponsor", final_sponsors)},
    )

    run(cfg, skip_match=True)

    final_child = read_final(cfg, "child")
    assert final_child[0]["assigned_sponsor_id"] == 1
    assert final_child[0]["assignment_source"] == "manual"
    final_sponsor = read_sheet(cfg.excel_file, "final_sponsor")
    assert final_sponsor is not None
    by_id = {r["sponsor_id"]: r for r in final_sponsor.to_dicts()}
    assert by_id[1]["assigned_angel_ids"] == "1A"
    assert by_id[2]["assigned_angel_ids"] is None
    assert by_id[2]["num_angels_assigned"] == 0


def test_run_skip_match_prior_conflicts_with_prior(cfg: Config) -> None:
    """Two sponsors claiming the same prior id error out."""
    sponsors = [
        sponsor_row(1, num_children=2, prior="1A"),
        sponsor_row(2, num_children=1, prior="1a"),  # case-insensitive
    ]
    families = [family_row(1)]
    children = [child_row("1A", 1, 8)]
    _seed_match_workbook(cfg, sponsors, families, children)

    from ccat.match import MatchError

    before = Path(cfg.excel_file).read_bytes()
    with pytest.raises(MatchError, match=r"1A.*sponsors \[1, 2\]"):
        run(cfg, skip_match=True)
    # Nothing written: the pre-existing complete stores remain byte-identical.
    assert Path(cfg.excel_file).read_bytes() == before


def test_run_skip_match_without_previous_final_family(cfg: Config) -> None:
    """--skip-match on a fresh workbook writes all-null assignments."""
    _seed_match_workbook(
        cfg,
        [sponsor_row(1, num_children=1)],
        [family_row(1)],
        [child_row("1A", 1, 8)],
    )
    run(cfg, skip_match=True)
    final_family = read_sheet(cfg.excel_file, "final_family")
    assert final_family is not None
    assert final_family.height == 1
    assert final_family["assigned_sponsor_id"].to_list() == [None]
    assert final_family["num_angels_in_family"].to_list() == [1]
    final_sponsor = read_sheet(cfg.excel_file, "final_sponsor")
    assert final_sponsor is not None
    assert final_sponsor["assigned_angel_ids"].to_list() == [None]
    assert final_sponsor["num_angels_assigned"].to_list() == [0]
