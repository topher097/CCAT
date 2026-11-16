"""Tests for module 4 — matchmaking (§8)."""

from pathlib import Path
from typing import Any

import polars as pl
import pytest

from ccat.acquire import AGE_PREF_BOUNDS, SPECIAL_NEEDS_LABEL
from ccat.audit.store import (
    ENTITY_SPECS,
    build_adj_frame,
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
    family_id: int,
    age: int,
    gender: str = "Male",
    special: str = "",
    status: str = "audited",
    ignore: bool = False,
) -> dict[str, Any]:
    """One dense adj_child row for match tests."""
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


def _adj_frame(entity: str, partials: list[dict[str, Any]]) -> pl.DataFrame:
    """One dense adj frame from partial rows (missing columns default)."""
    columns = ENTITY_SPECS[entity]["columns"]
    rows = [
        {column: None for column in columns} | partial for partial in partials
    ]
    return build_adj_frame(entity, rows)


def _seed_match_workbook(
    cfg: Config,
    sponsors: list[dict[str, Any]],
    families: list[dict[str, Any]],
    children: list[dict[str, Any]],
) -> None:
    """Write the dense adj sheets match works from (§7.5)."""
    write_sheets(
        cfg.excel_file,
        "audit",
        {
            "adj_sponsor": _adj_frame("sponsor", sponsors),
            "adj_family": _adj_frame("family", families),
            "adj_child": _adj_frame("child", children),
        },
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
    """num_angels_in_family counts every non-ignored adj_child row (§7.5)."""
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


def test_run_final_child_never_rewritten(cfg: Config) -> None:
    """Match never writes final_child (§8.6) and keeps its formula caches."""
    from ccat.io import _final_child_formula_caches

    sponsors = [sponsor_row(1, num_children=1)]
    families = [family_row(1)]
    children = [child_row("1A", 1, 8)]
    _seed_match_workbook(cfg, sponsors, families, children)
    write_sheets(
        cfg.excel_file,
        "audit",
        {"final_child": _adj_frame("child", children)},
        xmas=cfg.xmas,
    )
    before = read_sheet(cfg.excel_file, "final_child")
    caches_before = _final_child_formula_caches(Path(cfg.excel_file))
    run(cfg)
    after = read_sheet(cfg.excel_file, "final_child")
    assert before is not None and after is not None
    assert before.equals(after)
    assert _final_child_formula_caches(Path(cfg.excel_file)) == caches_before


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


def test_run_requires_adj_sheets(cfg: Config) -> None:
    """Missing adj sheets fail with an actionable error (§7.5)."""
    from ccat.match import MatchError

    with pytest.raises(MatchError, match="ccat audit"):
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
    """--skip-match reconciles assignments inside the adj sheets (§7.2).

    Previously assigned angel lists and sponsor assignments carry over;
    num_angels_in_family refreshes from adj_child; the finals are
    re-projected; match_report is untouched.
    """
    sponsors = [sponsor_row(1, num_children=2)]
    families = [family_row(1), family_row(3)]
    children = [
        child_row("1A", 1, 8),
        child_row("3A", 3, 5),
        child_row("3B", 3, 6),
    ]
    _seed_match_workbook(cfg, sponsors, families, children)
    # a prior run's state: family 1 matched to sponsor 1, a report row
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
    from ccat.audit.store import read_adj

    adj_families = read_adj(cfg, "family")
    for row in adj_families:
        if row["family_id"] == 1:
            row["assigned_sponsor_id"] = 1
            row["assigned_angel_ids"] = "1A"
            row["num_angels_in_family"] = 1
    adj_sponsors = read_adj(cfg, "sponsor")
    adj_sponsors[0]["assigned_angel_ids"] = "1A"
    adj_sponsors[0]["num_angels_assigned"] = 1
    write_sheets(
        cfg.excel_file,
        "audit",
        {
            "adj_family": _adj_frame("family", adj_families),
            "adj_sponsor": _adj_frame("sponsor", adj_sponsors),
        },
    )

    run(cfg, skip_match=True)

    final_family = read_sheet(cfg.excel_file, "final_family")
    assert final_family is not None
    rows = {row["family_id"]: row for row in final_family.to_dicts()}
    assert sorted(rows) == [1, 3]
    # Existing assignments carry over untouched.
    assert rows[1]["assigned_sponsor_id"] == 1
    assert rows[1]["assigned_angel_ids"] == "1A"
    # New family 3 has no sponsor; angel counts refresh from adj_child.
    assert rows[3]["assigned_sponsor_id"] is None
    assert rows[3]["num_angels_in_family"] == 2
    # The adj sheet itself carries the reconciled values.
    adj_after = {row["family_id"]: row for row in read_adj(cfg, "family")}
    assert adj_after[1]["assigned_sponsor_id"] == 1
    assert adj_after[3]["num_angels_in_family"] == 2
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


def test_run_skip_match_reconciles_sponsors_without_matching(
    cfg: Config,
) -> None:
    """--skip-match carries sponsor assignments over inside adj_sponsor."""
    from ccat.audit.store import read_adj

    sponsors = [sponsor_row(1, num_children=2), sponsor_row(3, num_children=1)]
    families = [family_row(1)]
    children = [child_row("1A", 1, 8)]
    _seed_match_workbook(cfg, sponsors, families, children)
    adj_sponsors = read_adj(cfg, "sponsor")
    adj_sponsors[0]["assigned_angel_ids"] = "1A"
    adj_sponsors[0]["num_angels_assigned"] = 1
    write_sheets(
        cfg.excel_file,
        "audit",
        {"adj_sponsor": _adj_frame("sponsor", adj_sponsors)},
    )

    run(cfg, skip_match=True)

    final_sponsor = read_sheet(cfg.excel_file, "final_sponsor")
    assert final_sponsor is not None
    rows = {row["sponsor_id"]: row for row in final_sponsor.to_dicts()}
    assert sorted(rows) == [1, 3]
    # Existing assignment carries over untouched.
    assert rows[1]["assigned_angel_ids"] == "1A"
    assert rows[1]["num_angels_assigned"] == 1
    # New sponsor 3 has no angels.
    assert rows[3]["assigned_angel_ids"] is None
    assert rows[3]["num_angels_assigned"] == 0


def test_run_skip_match_propagates_prior_assigned_angel_ids(
    cfg: Config,
) -> None:
    """--skip-match propagates prior_assigned_angel_ids.

    The raw prior overrides any stale carried-over value.
    """
    from ccat.audit.store import read_adj

    sponsors = [
        sponsor_row(1, num_children=2, prior="3b, 1A"),
        sponsor_row(2, num_children=1),
    ]
    families = [family_row(1)]
    children = [child_row("1A", 1, 8)]
    _seed_match_workbook(cfg, sponsors, families, children)
    adj_sponsors = read_adj(cfg, "sponsor")
    adj_sponsors[0]["assigned_angel_ids"] = "9Z"  # stale carried-over list
    adj_sponsors[0]["num_angels_assigned"] = 1
    write_sheets(
        cfg.excel_file,
        "audit",
        {"adj_sponsor": _adj_frame("sponsor", adj_sponsors)},
    )

    run(cfg, skip_match=True)

    final_sponsor = read_sheet(cfg.excel_file, "final_sponsor")
    assert final_sponsor is not None
    rows = {row["sponsor_id"]: row for row in final_sponsor.to_dicts()}
    # Raw manual entry wins: parsed, uppercased, sorted.
    assert rows[1]["assigned_angel_ids"] == "1A,3B"
    assert rows[1]["num_angels_assigned"] == 2
    # No prior -> carry-over/none as before.
    assert rows[2]["assigned_angel_ids"] is None


def test_run_skip_match_prior_points_family_at_sponsor(cfg: Config) -> None:
    """A propagated prior angel sets its family's sponsor assignment."""
    from ccat.audit.store import read_adj

    # adj_family has families 1 (stale assignment to vanished sponsor 2)
    # and 3 (unassigned); sponsor 1 carries prior angels 1A,1B.
    sponsors = [sponsor_row(1, num_children=2, prior="1A,1B")]
    families = [family_row(1), family_row(3)]
    children = [
        child_row("1A", 1, 8),
        child_row("1B", 1, 9),
        child_row("3A", 3, 5),
    ]
    _seed_match_workbook(cfg, sponsors, families, children)
    adj_families = read_adj(cfg, "family")
    for row in adj_families:
        if row["family_id"] == 1:
            row["assigned_sponsor_id"] = 2  # stale: sponsor 2 is gone
            row["assigned_angel_ids"] = "1A"
    write_sheets(
        cfg.excel_file,
        "audit",
        {"adj_family": _adj_frame("family", adj_families)},
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
    cfg: Config, caplog
) -> None:
    """A prior touching a family assigned elsewhere warns and keeps it."""
    sponsors = [
        sponsor_row(1, num_children=2, prior="1A"),
        sponsor_row(2, num_children=1, prior="1B"),
    ]
    families = [family_row(1)]
    children = [child_row("1A", 1, 8), child_row("1B", 1, 9)]
    _seed_match_workbook(cfg, sponsors, families, children)

    with caplog.at_level("WARNING"):
        run(cfg, skip_match=True)

    # No duplicate angel ids, so no error: the split is only warned about.
    assert "keeping sponsor 1" in caplog.text
    final_family = read_sheet(cfg.excel_file, "final_family")
    assert final_family is not None
    row = final_family.to_dicts()[0]
    # Earlier sponsor_id wins deterministically.
    assert row["assigned_sponsor_id"] == 1
    assert row["assigned_angel_ids"] == "1A"
    final_sponsor = read_sheet(cfg.excel_file, "final_sponsor")
    assert final_sponsor is not None
    by_id = {r["sponsor_id"]: r for r in final_sponsor.to_dicts()}
    assert by_id[1]["assigned_angel_ids"] == "1A"
    assert by_id[2]["assigned_angel_ids"] == "1B"


def test_run_skip_match_prior_unknown_angel_warns(cfg: Config, caplog) -> None:
    """A prior angel missing from adj_child warns; family untouched."""
    sponsors = [sponsor_row(1, num_children=2, prior="9Z")]
    families = [family_row(1)]
    children = [child_row("1A", 1, 8)]
    _seed_match_workbook(cfg, sponsors, families, children)

    with caplog.at_level("WARNING"):
        run(cfg, skip_match=True)

    assert "prior angel 9Z is not in adj_child" in caplog.text
    final_family = read_sheet(cfg.excel_file, "final_family")
    assert final_family is not None
    row = final_family.to_dicts()[0]
    assert row["assigned_sponsor_id"] is None
    # The prior still propagates to the sponsor's own assigned list.
    final_sponsor = read_sheet(cfg.excel_file, "final_sponsor")
    assert final_sponsor is not None
    assert final_sponsor["assigned_angel_ids"].to_list() == ["9Z"]


def test_run_skip_match_prior_empty_string_falls_back(cfg: Config) -> None:
    """A whitespace-only prior counts as absent; carry-over applies."""
    from ccat.audit.store import read_adj

    sponsors = [sponsor_row(1, num_children=2, prior="  ")]
    families = [family_row(1)]
    children = [child_row("1A", 1, 8)]
    _seed_match_workbook(cfg, sponsors, families, children)
    adj_sponsors = read_adj(cfg, "sponsor")
    adj_sponsors[0]["assigned_angel_ids"] = "1A"  # carried over
    adj_sponsors[0]["num_angels_assigned"] = 1
    write_sheets(
        cfg.excel_file,
        "audit",
        {"adj_sponsor": _adj_frame("sponsor", adj_sponsors)},
    )

    run(cfg, skip_match=True)

    final_sponsor = read_sheet(cfg.excel_file, "final_sponsor")
    assert final_sponsor is not None
    row = final_sponsor.to_dicts()[0]
    assert row["assigned_angel_ids"] == "1A"
    assert row["num_angels_assigned"] == 1


def test_run_skip_match_prior_conflicts_with_carried_over(
    cfg: Config,
) -> None:
    """A prior id already on another sponsor's carried-over list errors."""
    from ccat.audit.store import read_adj

    sponsors = [sponsor_row(1, num_children=2, prior="1A"), sponsor_row(2)]
    families = [family_row(1)]
    children = [child_row("1A", 1, 8)]
    _seed_match_workbook(cfg, sponsors, families, children)
    adj_sponsors = read_adj(cfg, "sponsor")
    adj_sponsors[1]["assigned_angel_ids"] = "1A,2A"  # carried over
    adj_sponsors[1]["num_angels_assigned"] = 2
    write_sheets(
        cfg.excel_file,
        "audit",
        {"adj_sponsor": _adj_frame("sponsor", adj_sponsors)},
    )

    from ccat.match import MatchError

    with pytest.raises(MatchError, match="duplicate angel assignments"):
        run(cfg, skip_match=True)


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

    with pytest.raises(MatchError, match=r"1A.*sponsors \[1, 2\]"):
        run(cfg, skip_match=True)
    # Nothing written: the conflicting final_sponsor never lands.
    assert read_sheet(cfg.excel_file, "final_sponsor") is None


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
