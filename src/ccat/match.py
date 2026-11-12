"""Module 4 — matchmaking (§8).

Assigns whole families to sponsors: special-needs sponsors first, then
Individuals, then Group/Organization sponsors, honoring exact child counts
and lexicographic preference tiers.  Writes ``final_family``,
``final_sponsor``, and ``match_report`` (archiving previous versions to
timestamped sheets), and exports the report as CSV.  ``final_child`` is
read-only here (§8.6).
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Any

import polars as pl
from polars._typing import PolarsDataType

from ccat.acquire import SPECIAL_NEEDS_LABEL
from ccat.config import Config
from ccat.io import list_sheets, read_sheet, write_sheets

logger = logging.getLogger(__name__)

#: Node budget for the exhaustive subset search before greedy fallback.
SEARCH_BUDGET = 200_000

#: Node budget for the greedy backtracking fallback.
GREEDY_BUDGET = 50_000


class MatchError(Exception):
    """Raised when matchmaking cannot complete."""


@dataclass
class PoolFamily:
    """One family available to pool matchmaking (§8.4)."""

    family_id: int
    family_row: dict[str, Any]
    children: list[dict[str, Any]]

    @property
    def child_count(self) -> int:
        """Number of children in the family."""
        return len(self.children)

    @property
    def angel_ids(self) -> list[str]:
        """Sorted angel ids of the family's children."""
        return sorted(
            (child["angel_id"] for child in self.children), key=_angel_sort_key
        )


@dataclass
class SponsorPlan:
    """Matchmaking working state for one sponsor."""

    sponsor_row: dict[str, Any]
    sponsor_id: int
    num_children: int
    is_special_needs: bool
    is_pre_assigned: bool
    assigned_angel_ids: list[str] = field(default_factory=list)


def _as_bool(value: Any) -> bool:
    """Coerce sheet values (bool or string) to bool."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def _angel_sort_key(angel_id: str) -> tuple[int, str]:
    """Sort key for angel ids: family number, then letter code."""
    match = re.fullmatch(r"(\d+)([A-Z]+)", angel_id)
    if match is None:
        return (10**9, angel_id)
    return (int(match.group(1)), match.group(2))


def parse_prior_assigned(value: str) -> list[str]:
    """Parse the Assigned Angel ID Code free text (§8.5).

    Splits on commas/whitespace, strips, and uppercases so ids like ``3b``
    normalize to ``3B``.

    Args:
        value: The raw `prior_assigned_angel_ids` string.

    Returns:
        The parsed, normalized angel ids.
    """
    return [
        token.upper() for token in re.split(r"[,\s]+", value.strip()) if token
    ]


def canonical_location(
    value: str | None, groups: dict[str, list[str]]
) -> str | None:
    """Reduce one location label to its canonical bucket (§8.3).

    Args:
        value: The raw location label (or None).
        groups: The ``config.location_groups`` mapping.

    Returns:
        The bucket name, or None when the label maps to no bucket.
    """
    if value is None:
        return None
    for bucket, labels in groups.items():
        if value in labels:
            return bucket
    return None


def special_needs_weight(cfg: Config, max_children: int) -> float:
    """Compute the tier-0 weight guaranteed to dominate all lower tiers.

    Tier weights are lexicographic (§8.3): the special-needs weight is
    sized so no combination of lower-tier scores can outweigh the smallest
    possible special-needs difference.

    Args:
        cfg: Pipeline configuration.
        max_children: Largest sponsor `num_children` in the pool.

    Returns:
        The tier-0 weight.
    """
    mm = cfg.matchmaking
    lower_max = (mm.location_weight + mm.age_weight + mm.gender_weight) * (
        max_children + 1
    )
    return lower_max * 1024


def family_score(
    sponsor: SponsorPlan, family: PoolFamily, cfg: Config, sn_weight: float
) -> float | None:
    """Score one sponsor-family pairing (§8.3).

    Args:
        sponsor: The sponsor being matched.
        family: The candidate family.
        cfg: Pipeline configuration.
        sn_weight: The tier-0 (special needs) weight.

    Returns:
        The weighted score, or None when the pairing is forbidden
        (Non-binary child with a gender-specific sponsor, hard rule §8.3).
    """
    children = family.children
    count = len(children)
    gender_preference = (
        sponsor.sponsor_row.get("gender_preference") or "Any Gender"
    )
    if gender_preference != "Any Gender" and any(
        child.get("gender") == "Non-binary" for child in children
    ):
        return None
    score = 0.0
    if sponsor.is_special_needs:
        special = sum(
            1 for child in children if _as_bool(child.get("special_needs"))
        )
        score += (special / count) * sn_weight
    sponsor_bucket = canonical_location(
        sponsor.sponsor_row.get("dropoff_location"), cfg.location_groups
    )
    family_bucket = canonical_location(
        family.family_row.get("gift_pickup_location"), cfg.location_groups
    )
    if sponsor_bucket is None or sponsor_bucket == family_bucket:
        score += cfg.matchmaking.location_weight
    low = sponsor.sponsor_row.get("pref_age_min")
    high = sponsor.sponsor_row.get("pref_age_max")
    in_range = sum(
        1 for child in children if low <= child["age_at_xmas"] <= high
    )
    score += (in_range / count) * cfg.matchmaking.age_weight
    if gender_preference == "Any Gender":
        score += cfg.matchmaking.gender_weight
    else:
        matching = sum(
            1 for child in children if child.get("gender") == gender_preference
        )
        score += (matching / count) * cfg.matchmaking.gender_weight
    return score


class _BudgetExceededError(Exception):
    """Raised when the exhaustive search exceeds its node budget."""


def best_subset(
    candidates: list[tuple[PoolFamily, float]], target: int
) -> list[PoolFamily] | None:
    """Search subsets whose child counts sum exactly to target (§8.4).

    Exhaustive depth-first with child-count pruning; picks the maximum
    total score, tie-breaking by the lowest sorted family_id tuple.

    Args:
        candidates: (family, score) pairs sorted by family_id ascending.
        target: The sponsor's exact `num_children`.

    Returns:
        The best family subset, or None when no exact combination exists.

    Raises:
        _BudgetExceededError: When the search space is too large for exhaustive
            search (the caller falls back to greedy, §8.4).
    """
    best_ids: tuple[int, ...] | None = None
    best_score = 0.0
    best_subset_: list[PoolFamily] = []
    budget = SEARCH_BUDGET

    def dfs(
        index: int, count: int, score: float, chosen: list[PoolFamily]
    ) -> None:
        nonlocal best_ids, best_score, best_subset_, budget
        budget -= 1
        if budget < 0:
            raise _BudgetExceededError
        if count == target:
            ids = tuple(family.family_id for family in chosen)
            if (
                best_ids is None
                or score > best_score
                or (score == best_score and ids < best_ids)
            ):
                best_ids = ids
                best_score = score
                best_subset_ = list(chosen)
            return
        for next_index in range(index, len(candidates)):
            family, family_score_value = candidates[next_index]
            if count + family.child_count > target:
                continue
            chosen.append(family)
            dfs(
                next_index + 1,
                count + family.child_count,
                score + family_score_value,
                chosen,
            )
            chosen.pop()

    dfs(0, 0, 0.0, [])
    return best_subset_ if best_ids is not None else None


def greedy_subset(
    candidates: list[tuple[PoolFamily, float]], target: int
) -> list[PoolFamily] | None:
    """Deterministic greedy fallback with backtracking (§8.4).

    Args:
        candidates: (family, score) pairs sorted by score descending then
            family_id ascending.
        target: The sponsor's exact `num_children`.

    Returns:
        A family subset summing exactly to target, or None.
    """
    budget = GREEDY_BUDGET

    def backtrack(
        index: int, count: int, chosen: list[PoolFamily]
    ) -> list[PoolFamily] | None:
        nonlocal budget
        budget -= 1
        if budget < 0:
            return None
        if count == target:
            return list(chosen)
        for next_index in range(index, len(candidates)):
            family, _score = candidates[next_index]
            if count + family.child_count > target:
                continue
            chosen.append(family)
            result = backtrack(
                next_index + 1, count + family.child_count, chosen
            )
            if result is not None:
                return result
            chosen.pop()
        return None

    return backtrack(0, 0, [])


def _participating_children(
    cfg: Config, overwrite: bool = False
) -> list[dict[str, Any]]:
    """Load final_child rows that participate in matchmaking (§8.1).

    Only rows with ``audited == true`` and a non-null ``angel_id``.  With
    ``overwrite`` set, the ``audited`` column is ignored.

    Args:
        cfg: Pipeline configuration.
        overwrite: Ignore the ``audited`` column when selecting rows.

    Returns:
        The participating child rows.

    Raises:
        MatchError: If final_child or raw sheets are missing.
    """
    final_child = read_sheet(cfg.excel_file, "final_child")
    if final_child is None:
        raise MatchError(
            "final_child sheet is missing; run `ccat audit` before matching"
        )
    rows = final_child.to_dicts()
    return [
        row
        for row in rows
        if (overwrite or _as_bool(row.get("audited"))) and row.get("angel_id")
    ]


def _children_per_family(cfg: Config) -> dict[Any, int]:
    """Count ``final_child`` rows per ``family_id`` (§8.6).

    Args:
        cfg: Pipeline configuration.

    Returns:
        Mapping of ``family_id`` to the number of ``final_child`` rows
        sharing it.  Families with no ``final_child`` rows are absent
        (callers default to 0).
    """
    final_child = read_sheet(cfg.excel_file, "final_child")
    if final_child is None or "family_id" not in final_child.columns:
        return {}
    counts: dict[Any, int] = {}
    for row in final_child.to_dicts():
        family_id = row.get("family_id")
        if family_id is not None:
            counts[family_id] = counts.get(family_id, 0) + 1
    return counts


def reconcile_pre_assignments(
    sponsor_rows: list[dict[str, Any]],
    children: list[dict[str, Any]],
) -> tuple[dict[int, SponsorPlan], list[dict[str, str]]]:
    """Parse and reconcile prior_assigned_angel_ids (§8.5).

    Honors manual assignments verbatim after checking existence, duplicates
    (earlier sponsor_id wins), and family integrity (siblings added when
    the sponsor's num_children allows).

    Args:
        sponsor_rows: raw_sponsor rows.
        children: Participating final_child rows.

    Returns:
        A tuple of (sponsor_id -> SponsorPlan for pre-assigned sponsors,
        match_report rows describing pre-assignment problems).
    """
    children_by_angel = {child["angel_id"]: child for child in children}
    children_by_family: dict[int, list[dict[str, Any]]] = {}
    for child in children:
        children_by_family.setdefault(child["family_id"], []).append(child)
    ordered = sorted(sponsor_rows, key=lambda row: row["sponsor_id"])
    plans: dict[int, SponsorPlan] = {}
    report: list[dict[str, str]] = []
    seen: dict[str, int] = {}
    for row in ordered:
        raw_list = row.get("prior_assigned_angel_ids")
        if raw_list is None or str(raw_list).strip() == "":
            continue
        plan = SponsorPlan(
            sponsor_row=row,
            sponsor_id=row["sponsor_id"],
            num_children=int(row["num_children"]),
            is_special_needs=row.get("age_preference") == SPECIAL_NEEDS_LABEL,
            is_pre_assigned=True,
        )
        for angel_id in parse_prior_assigned(str(raw_list)):
            if angel_id not in children_by_angel:
                report.append(
                    {
                        "kind": "pre_assignment",
                        "ref": str(row["sponsor_id"]),
                        "reason": f"unknown angel_id {angel_id} dropped",
                    }
                )
                logger.warning(
                    "sponsor %s: unknown angel_id %s",
                    row["sponsor_id"],
                    angel_id,
                )
                continue
            if angel_id in seen and seen[angel_id] != plan.sponsor_id:
                for ref in (str(seen[angel_id]), str(plan.sponsor_id)):
                    report.append(
                        {
                            "kind": "pre_assignment",
                            "ref": ref,
                            "reason": (
                                f"duplicate angel_id {angel_id} claimed by sponsors"
                                f" {seen[angel_id]} and {plan.sponsor_id}; kept by"
                                " earlier sponsor, manual resolution needed"
                            ),
                        }
                    )
                logger.warning(
                    "duplicate angel_id %s: sponsor %s keeps it, sponsor %s loses it",
                    angel_id,
                    seen[angel_id],
                    plan.sponsor_id,
                )
                continue
            seen[angel_id] = plan.sponsor_id
            plan.assigned_angel_ids.append(angel_id)
        plans[plan.sponsor_id] = plan
    # Family integrity: siblings of pre-assigned angels join the same
    # sponsor when num_children allows (§8.5).
    for plan in plans.values():
        covered_families = {
            children_by_angel[angel_id]["family_id"]
            for angel_id in plan.assigned_angel_ids
        }
        for family_id in sorted(covered_families):
            family_children = children_by_family.get(family_id, [])
            missing = [
                child["angel_id"]
                for child in family_children
                if child["angel_id"] not in plan.assigned_angel_ids
            ]
            if not missing:
                continue
            if len(plan.assigned_angel_ids) + len(missing) <= plan.num_children:
                for angel_id in missing:
                    seen[angel_id] = plan.sponsor_id
                plan.assigned_angel_ids.extend(missing)
            else:
                report.append(
                    {
                        "kind": "pre_assignment",
                        "ref": str(plan.sponsor_id),
                        "reason": (
                            f"family {family_id} would be split: siblings"
                            f" {','.join(missing)} exceed num_children"
                            f" {plan.num_children}"
                        ),
                    }
                )
                logger.warning(
                    "sponsor %s: family %d would be split (%s left out)",
                    plan.sponsor_id,
                    family_id,
                    ",".join(missing),
                )
        if len(plan.assigned_angel_ids) > plan.num_children:
            report.append(
                {
                    "kind": "pre_assignment",
                    "ref": str(plan.sponsor_id),
                    "reason": (
                        f"pre-assigned {len(plan.assigned_angel_ids)} angels"
                        f" exceeds num_children {plan.num_children}; honored verbatim"
                    ),
                }
            )
    return plans, report


def match_sponsors(
    sponsor_plans: list[SponsorPlan],
    pool_families: list[PoolFamily],
    cfg: Config,
    sn_weight: float,
    test_mode: bool = False,
) -> tuple[dict[int, int], list[dict[str, str]]]:
    """Run the phased sponsor matching loop (§8.2, §8.4).

    Args:
        sponsor_plans: Pool sponsors (pre-assigned excluded), each with
            `is_pre_assigned == False`.
        pool_families: Families available to the pool.
        cfg: Pipeline configuration.
        sn_weight: Tier-0 weight.
        test_mode: Stop after the first full sponsor: only the first
            sponsor in phase order is processed.

    Returns:
        A tuple of (family_id -> sponsor_id assignments, match_report rows
        for unmatched sponsors).
    """
    remaining = list(pool_families)
    assignments: dict[int, int] = {}
    report: list[dict[str, str]] = []
    special_ids = {
        plan.sponsor_id for plan in sponsor_plans if plan.is_special_needs
    }
    individuals = [
        plan
        for plan in sponsor_plans
        if not plan.is_special_needs
        and plan.sponsor_row.get("sponsor_type") == "Individual"
    ]
    organizations = [
        plan
        for plan in sponsor_plans
        if not plan.is_special_needs
        and plan.sponsor_row.get("sponsor_type") in {"Group", "Organization"}
    ]
    individual_ids = {plan.sponsor_id for plan in individuals}
    organization_ids = {plan.sponsor_id for plan in organizations}
    other = [
        plan
        for plan in sponsor_plans
        if plan.sponsor_id not in special_ids
        and plan.sponsor_id not in individual_ids
        and plan.sponsor_id not in organization_ids
    ]
    phases = [
        (
            "special-needs sponsors",
            [p for p in sponsor_plans if p.is_special_needs],
        ),
        ("individual sponsors", individuals),
        ("group/organization sponsors", organizations + other),
    ]
    ordered: list[tuple[str, SponsorPlan]] = []
    for phase_name, phase in phases:
        ordered.extend(
            (phase_name, plan)
            for plan in sorted(phase, key=lambda item: item.sponsor_id)
        )
    if test_mode:
        if ordered:
            logger.info(
                "test mode: matching only sponsor %d (first in phase order)",
                ordered[0][1].sponsor_id,
            )
        ordered = ordered[:1]
    for phase_name, plan in ordered:
        candidates = [
            (family, score)
            for family in remaining
            if (score := family_score(plan, family, cfg, sn_weight)) is not None
        ]
        if not candidates:
            report.append(
                {
                    "kind": "sponsor",
                    "ref": str(plan.sponsor_id),
                    "reason": "no eligible families (gender rule or empty pool)",
                }
            )
            continue
        candidates.sort(key=lambda pair: pair[0].family_id)
        try:
            subset = best_subset(candidates, plan.num_children)
        except _BudgetExceededError:
            logger.warning(
                "sponsor %d: pool too large for exhaustive search;"
                " falling back to greedy",
                plan.sponsor_id,
            )
            subset = greedy_subset(
                sorted(
                    candidates,
                    key=lambda pair: (-pair[1], pair[0].family_id),
                ),
                plan.num_children,
            )
        if subset is None:
            report.append(
                {
                    "kind": "sponsor",
                    "ref": str(plan.sponsor_id),
                    "reason": "no exact count combination",
                }
            )
            continue
        for family in subset:
            assignments[family.family_id] = plan.sponsor_id
            plan.assigned_angel_ids.extend(family.angel_ids)
            remaining.remove(family)
        logger.info(
            "%s (sponsor %d, phase %s): matched families %s",
            plan.sponsor_row.get("email"),
            plan.sponsor_id,
            phase_name,
            [family.family_id for family in subset],
        )
    return assignments, report


def _reconcile_family_rows(
    family_rows: list[dict[str, Any]],
    sponsor_ids: set[int],
    children_per_family: dict[Any, int],
    existing_final: pl.DataFrame | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Reconcile ``final_family`` with the current raw rows (§8.6).

    Every current ``raw_family`` row gets a ``final_family`` row.
    Previously assigned sponsors and angel lists carry over unchanged;
    new families get no sponsor.  This is the ``--skip-match`` path:
    sync the sheet without running the matching algorithm.

    Args:
        family_rows: All ``raw_family`` rows, sorted by ``family_id``.
        sponsor_ids: Valid ``sponsor_id``s in ``raw_sponsor`` (used only
            for warning when an assignment references a vanished
            sponsor).
        children_per_family: ``final_child`` row count per family.
        existing_final: The previous ``final_family`` frame (or None
            when the sheet has never been written).

    Returns:
        A tuple of (reconciled final_family rows, warning messages for
        anomalies the auditor should know about).
    """
    existing_by_id: dict[Any, dict[str, Any]] = {}
    if existing_final is not None:
        for row in existing_final.to_dicts():
            existing_by_id[row.get("family_id")] = row
    warnings: list[str] = []
    dropped = sorted(
        set(existing_by_id) - {row["family_id"] for row in family_rows}
    )
    if dropped:
        message = (
            "final_family families no longer in raw_family (dropped):"
            f" {dropped}"
        )
        warnings.append(message)
        logger.warning("%s", message)
    rows: list[dict[str, Any]] = []
    for row in family_rows:
        family_id = row["family_id"]
        prior = existing_by_id.get(family_id, {})
        sponsor_id = prior.get("assigned_sponsor_id")
        if sponsor_id is not None and sponsor_id not in sponsor_ids:
            message = (
                f"family {family_id} has assigned_sponsor_id {sponsor_id},"
                " who is no longer in raw_sponsor; keeping the assignment"
            )
            warnings.append(message)
            logger.warning("%s", message)
        rows.append(
            {
                **row,
                "assigned_sponsor_id": sponsor_id,
                "assigned_angel_ids": prior.get("assigned_angel_ids"),
                "num_angels_in_family": children_per_family.get(family_id, 0),
            }
        )
    return rows, warnings


def run(
    cfg: Config,
    test: bool = False,
    overwrite: bool = False,
    skip_match: bool = False,
) -> None:
    """Run module 4 end to end (§8).

    Args:
        cfg: Pipeline configuration.
        test: Stop after the first full sponsor: only the first sponsor
            in phase order is matched; the match_report records that this
            is a partial test run.  Has no effect with ``skip_match``.
        overwrite: Ignore the ``audited`` column of ``final_child`` (all
            children with an angel_id participate) and replace any
            previous ``final_family``/``final_sponsor``/``match_report``
            outputs in place instead of archiving them (§8.6).
        skip_match: Reconcile ``final_family`` without running the
            matching algorithm: every current ``raw_family`` family gets
            a row, previously assigned sponsors and angel lists carry
            over, new families get no sponsor.  ``final_sponsor`` and
            ``match_report`` are left untouched.

    Raises:
        MatchError: If required sheets are missing.
    """
    raw_family = read_sheet(cfg.excel_file, "raw_family")
    raw_sponsor = read_sheet(cfg.excel_file, "raw_sponsor")
    if raw_family is None or raw_sponsor is None:
        raise MatchError(
            "raw_family/raw_sponsor sheets are missing; run `ccat acquire` first"
        )
    family_rows = sorted(
        raw_family.to_dicts(), key=lambda row: row["family_id"]
    )
    sponsor_rows = sorted(
        raw_sponsor.to_dicts(), key=lambda row: row["sponsor_id"]
    )
    if skip_match:
        if test:
            logger.warning("--test has no effect with --skip-match")
        existing_final = read_sheet(cfg.excel_file, "final_family")
        reconciled, _warnings = _reconcile_family_rows(
            family_rows,
            {row["sponsor_id"] for row in sponsor_rows},
            _children_per_family(cfg),
            existing_final,
        )
        final_family = _augmented_frame(
            raw_family,
            reconciled,
            {
                "assigned_sponsor_id": pl.Int64,
                "assigned_angel_ids": pl.String,
                "num_angels_in_family": pl.Int64,
            },
        )
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        existing = set(list_sheets(cfg.excel_file))
        renames = (
            {
                name: _archive_name(existing, name, timestamp)
                for name in ("final_family",)
                if name in existing
            }
            if not overwrite
            else {}
        )
        write_sheets(
            cfg.excel_file,
            "match",
            {"final_family": final_family},
            renames=renames,
        )
        logger.info(
            "skip-match: reconciled %d final_family rows (%s);"
            " matching algorithm skipped, final_sponsor and match_report"
            " untouched",
            final_family.height,
            "archived previous sheet" if renames else "first write",
        )
        return
    children = _participating_children(cfg, overwrite=overwrite)
    children_per_family = _children_per_family(cfg)
    logger.info("participating audited children: %d", len(children))

    pre_assigned, report = reconcile_pre_assignments(sponsor_rows, children)
    pre_assigned_angels = {
        angel_id
        for plan in pre_assigned.values()
        for angel_id in plan.assigned_angel_ids
    }
    children_by_family: dict[int, list[dict[str, Any]]] = {}
    for child in children:
        children_by_family.setdefault(child["family_id"], []).append(child)

    family_by_id = {row["family_id"]: row for row in family_rows}
    pool_families = [
        PoolFamily(
            family_id=family_id,
            family_row=family_by_id[family_id],
            children=family_children,
        )
        for family_id, family_children in sorted(children_by_family.items())
        if not any(
            child["angel_id"] in pre_assigned_angels
            for child in family_children
        )
    ]
    pool_sponsors = [
        SponsorPlan(
            sponsor_row=row,
            sponsor_id=row["sponsor_id"],
            num_children=int(row["num_children"]),
            is_special_needs=row.get("age_preference") == SPECIAL_NEEDS_LABEL,
            is_pre_assigned=False,
        )
        for row in sponsor_rows
        if row["sponsor_id"] not in pre_assigned
    ]
    sn_weight = special_needs_weight(
        cfg, max((plan.num_children for plan in pool_sponsors), default=25)
    )
    assignments, sponsor_report = match_sponsors(
        pool_sponsors, pool_families, cfg, sn_weight, test_mode=test
    )
    report.extend(sponsor_report)
    if test:
        report.append(
            {
                "kind": "test",
                "ref": "-",
                "reason": (
                    "partial test run: only the first sponsor in phase"
                    " order was matched"
                ),
            }
        )

    # Pre-assigned families map to their sponsor (fully covered only).
    pre_family_assignments: dict[int, int] = {}
    for plan in pre_assigned.values():
        covered = {
            child["family_id"]
            for child in children
            if child["angel_id"] in plan.assigned_angel_ids
        }
        for family_id in covered:
            family_children = children_by_family.get(family_id, [])
            fully = all(
                child["angel_id"] in plan.assigned_angel_ids
                for child in family_children
            )
            if fully and family_id not in pre_family_assignments:
                pre_family_assignments[family_id] = plan.sponsor_id

    matched_family_ids = set(assignments) | set(pre_family_assignments)
    for family in pool_families:
        if family.family_id not in matched_family_ids:
            if any(
                child.get("gender") == "Non-binary" for child in family.children
            ):
                reason = "non-binary children with no Any Gender sponsor"
            else:
                reason = "no exact count combination"
            report.append(
                {
                    "kind": "family",
                    "ref": str(family.family_id),
                    "reason": reason,
                }
            )
    for family_row in family_rows:
        family_id = family_row["family_id"]
        if (
            family_id not in children_by_family
            and family_id not in matched_family_ids
        ):
            report.append(
                {
                    "kind": "family",
                    "ref": str(family_id),
                    "reason": "no audited children (not in pool)",
                }
            )

    final_family_rows: list[dict[str, Any]] = []
    for row in family_rows:
        family_id = row["family_id"]
        sponsor_id = assignments.get(family_id) or pre_family_assignments.get(
            family_id
        )
        family_children = children_by_family.get(family_id, [])
        assigned_angels = (
            ",".join(
                sorted(
                    (child["angel_id"] for child in family_children),
                    key=_angel_sort_key,
                )
            )
            if sponsor_id is not None and family_children
            else None
        )
        final_family_rows.append(
            {
                **row,
                "assigned_sponsor_id": sponsor_id,
                "assigned_angel_ids": assigned_angels,
                "num_angels_in_family": children_per_family.get(family_id, 0),
            }
        )
    final_sponsor_rows: list[dict[str, Any]] = []
    sponsor_plans = {
        plan.sponsor_id: plan
        for plan in pool_sponsors + list(pre_assigned.values())
    }
    for row in sponsor_rows:
        plan = sponsor_plans.get(row["sponsor_id"])
        angel_ids = (
            sorted(plan.assigned_angel_ids, key=_angel_sort_key) if plan else []
        )
        final_sponsor_rows.append(
            {
                **row,
                "assigned_angel_ids": ",".join(angel_ids)
                if angel_ids
                else None,
                "num_angels_assigned": len(angel_ids),
            }
        )

    report_frame = pl.DataFrame(
        report,
        schema={"kind": pl.String, "ref": pl.String, "reason": pl.String},
    )
    final_family = _augmented_frame(
        raw_family,
        final_family_rows,
        {
            "assigned_sponsor_id": pl.Int64,
            "assigned_angel_ids": pl.String,
            "num_angels_in_family": pl.Int64,
        },
    )
    final_sponsor = _augmented_frame(
        raw_sponsor,
        final_sponsor_rows,
        {
            "assigned_angel_ids": pl.String,
            "num_angels_assigned": pl.Int64,
        },
    )

    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    existing = set(list_sheets(cfg.excel_file))
    renames = (
        {
            name: _archive_name(existing, name, timestamp)
            for name in ("final_family", "final_sponsor", "match_report")
            if name in existing
        }
        if not overwrite
        else {}
    )
    write_sheets(
        cfg.excel_file,
        "match",
        {
            "final_family": final_family,
            "final_sponsor": final_sponsor,
            "match_report": report_frame,
        },
        renames=renames,
    )
    report_frame.write_csv(cfg.match_report_csv)
    logger.info(
        "matched %d families; report has %d entries; csv at %s",
        len(matched_family_ids),
        report_frame.height,
        cfg.match_report_csv,
    )


def _archive_name(existing: set[str], sheet: str, timestamp: str) -> str:
    """Build a non-colliding timestamped archive sheet name (§8.6)."""
    candidate = f"{sheet}_{timestamp}"
    suffix = 1
    while candidate in existing:
        candidate = f"{sheet}_{timestamp}_{suffix}"
        suffix += 1
    existing.add(candidate)
    return candidate


def _augmented_frame(
    raw: pl.DataFrame,
    rows: list[dict[str, Any]],
    extra: dict[str, PolarsDataType],
) -> pl.DataFrame:
    """Build an output frame preserving raw sheet order/dtypes plus extras."""
    schema: dict[str, PolarsDataType] = dict(raw.schema)
    schema.update(extra)
    frame = pl.DataFrame(
        [{key: row.get(key) for key in schema} for row in rows], schema=schema
    )
    return frame.select(list(schema))
