"""Tests for module 4 — matchmaking (§8)."""

from pathlib import Path
from typing import Any

import polars as pl
import pytest

from ccat.acquire import AGE_PREF_BOUNDS, SPECIAL_NEEDS_LABEL
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
    family_id: int,
    age: int,
    gender: str = "Male",
    special: bool = False,
    audited: bool = True,
) -> dict[str, Any]:
    """One final_child row for match tests."""
    return {
        "angel_id": angel_id,
        "family_id": family_id,
        "age_at_xmas": age,
        "gender": gender,
        "special_needs": special,
        "audited": audited,
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
        1, [child_row("1A", 1, 8, special=True)], location="Calumet"
    )
    perfect_location_family = pool_family(
        2, [child_row("2A", 2, 8, special=False)], location="TSA (Hancock)"
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


def _seed_match_workbook(
    cfg: Config,
    sponsors: list[dict[str, Any]],
    families: list[dict[str, Any]],
    children: list[dict[str, Any]],
) -> None:
    """Write raw_sponsor/raw_family/final_child sheets for run() tests."""
    sponsor_schema = {
        "sponsor_id": pl.Int64,
        "email": pl.String,
        "sponsor_type": pl.String,
        "num_children": pl.Int64,
        "age_preference": pl.String,
        "gender_preference": pl.String,
        "dropoff_location": pl.String,
        "pref_age_min": pl.Int64,
        "pref_age_max": pl.Int64,
        "prior_assigned_angel_ids": pl.String,
    }
    family_schema = {
        "family_id": pl.Int64,
        "primary_email": pl.String,
        "gift_pickup_location": pl.String,
    }
    child_schema = {
        "angel_id": pl.String,
        "family_id": pl.Int64,
        "age_at_xmas": pl.Int64,
        "gender": pl.String,
        "special_needs": pl.Boolean,
        "audited": pl.Boolean,
    }
    write_sheets(
        cfg.excel_file,
        "acquire",
        {
            "raw_sponsor": pl.DataFrame(sponsors, schema=sponsor_schema),
            "raw_family": pl.DataFrame(families, schema=family_schema),
        },
    )
    write_sheets(
        cfg.excel_file,
        "audit",
        {"final_child": pl.DataFrame(children, schema=child_schema)},
    )


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
        child_row("1A", 1, 8, special=True),
        child_row("2A", 2, 8, special=False),
    ]
    _seed_match_workbook(cfg, sponsors, families, children)
    run(cfg)
    final_family = read_sheet(cfg.excel_file, "final_family")
    assert final_family is not None
    assert final_family["assigned_sponsor_id"].to_list() == [
        2,
        1,
    ]  # SN sponsor first


def test_run_group_organization_after_individuals(cfg: Config) -> None:
    """Group and Organization sponsors follow Individuals (§8.2)."""
    sponsors = [
        sponsor_row(1, num_children=1, sponsor_type="Organization"),
        sponsor_row(2, num_children=1, sponsor_type="Individual"),
    ]
    families = [family_row(1), family_row(2)]
    children = [child_row("1A", 1, 8), child_row("2A", 2, 8)]
    _seed_match_workbook(cfg, sponsors, families, children)
    run(cfg)
    final_family = read_sheet(cfg.excel_file, "final_family")
    assert final_family is not None
    # sponsor 2 (Individual) picks first even though sponsor_id is higher
    assert final_family["assigned_sponsor_id"].to_list() == [2, 1]


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


def test_run_archives_previous_sheets_and_exports_csv(cfg: Config) -> None:
    """Prior matchings are archived to timestamped sheets; CSV is written (§8.6)."""
    sponsors = [sponsor_row(1, num_children=1)]
    families = [family_row(1)]
    children = [child_row("1A", 1, 8)]
    _seed_match_workbook(cfg, sponsors, families, children)
    run(cfg)
    run(cfg)
    sheets = list_sheets(cfg.excel_file)
    archives = [name for name in sheets if name.startswith("final_family_")]
    assert archives, "expected an archived final_family sheet"
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
    run(cfg)
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
    """num_angels_in_family counts every final_child row (§8.6)."""
    sponsors = [sponsor_row(1, num_children=1)]
    families = [family_row(1)]
    children = [
        child_row("1A", 1, 8),
        child_row("1B", 1, 9, audited=False),
    ]
    _seed_match_workbook(cfg, sponsors, families, children)
    run(cfg)
    final_family = read_sheet(cfg.excel_file, "final_family")
    assert final_family is not None
    assert final_family["num_angels_in_family"].to_list() == [2]


def test_run_final_child_never_rewritten(cfg: Config) -> None:
    """Match never writes final_child (§8.6)."""
    sponsors = [sponsor_row(1, num_children=1)]
    families = [family_row(1)]
    children = [child_row("1A", 1, 8)]
    _seed_match_workbook(cfg, sponsors, families, children)
    before = read_sheet(cfg.excel_file, "final_child")
    run(cfg)
    after = read_sheet(cfg.excel_file, "final_child")
    assert before is not None and after is not None
    assert before.equals(after)


def test_run_unaudited_children_excluded(cfg: Config) -> None:
    """Only audited children participate (§8.1)."""
    sponsors = [sponsor_row(1, num_children=1)]
    families = [family_row(1), family_row(2)]
    children = [child_row("1A", 1, 8, audited=False), child_row("2A", 2, 8)]
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


def test_run_overwrite_includes_unaudited_children(cfg: Config) -> None:
    """--overwrite ignores the audited column (§8.1)."""
    sponsors = [sponsor_row(1, num_children=1)]
    families = [family_row(1), family_row(2)]
    children = [child_row("1A", 1, 8, audited=False), child_row("2A", 2, 8)]
    _seed_match_workbook(cfg, sponsors, families, children)
    run(cfg)
    final_family = read_sheet(cfg.excel_file, "final_family")
    assert final_family is not None
    assert final_family["assigned_sponsor_id"].to_list() == [None, 1]
    run(cfg, overwrite=True)
    final_family = read_sheet(cfg.excel_file, "final_family")
    assert final_family is not None
    # the unaudited family now participates and wins the sponsor by id order
    assert final_family["assigned_sponsor_id"].to_list() == [1, None]


def test_run_requires_final_child(cfg: Config) -> None:
    """A missing final_child fails with an actionable error."""
    sponsors = [sponsor_row(1, num_children=1)]
    families = [family_row(1)]
    write_sheets(
        cfg.excel_file,
        "acquire",
        {
            "raw_sponsor": pl.DataFrame(
                sponsors,
                schema={
                    "sponsor_id": pl.Int64,
                    "email": pl.String,
                    "sponsor_type": pl.String,
                    "num_children": pl.Int64,
                    "age_preference": pl.String,
                    "gender_preference": pl.String,
                    "dropoff_location": pl.String,
                    "pref_age_min": pl.Int64,
                    "pref_age_max": pl.Int64,
                    "prior_assigned_angel_ids": pl.String,
                },
            ),
            "raw_family": pl.DataFrame(
                families,
                schema={
                    "family_id": pl.Int64,
                    "primary_email": pl.String,
                    "gift_pickup_location": pl.String,
                },
            ),
        },
    )
    from ccat.match import MatchError

    with pytest.raises(MatchError, match="final_child"):
        run(cfg)


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
        child_row("1A", 1, 8, special=True),
        child_row("2A", 2, 8, special=False),
    ]
    _seed_match_workbook(cfg, sponsors, families, children)
    run(cfg, test=True)
    final_family = read_sheet(cfg.excel_file, "final_family")
    assert final_family is not None
    # special-needs sponsor 2 is first in phase order despite higher id
    assert final_family["assigned_sponsor_id"].to_list() == [2, None]
