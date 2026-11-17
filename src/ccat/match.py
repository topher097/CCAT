"""Module 4 — matchmaking (§8, audit_architecture.md §4.4/§7.5).

Assigns whole families to sponsors: special-needs sponsors first, then
Individuals, then Organization/Walmart sponsors, honoring exact child counts
and lexicographic preference tiers.  Reads the dense ``adj_*`` sheets
directly (never ``raw_*``): the child pool is every ``adj_child`` row with
``status == "audited"``, a non-null ``angel_id``, and ``ignore != true``;
family/sponsor pools are the non-ignored ``adj_family``/``adj_sponsor``
rows.  Writes assignments into the assignment columns of
``adj_family``/``adj_sponsor`` (all other columns preserved), projects
``final_family``/``final_sponsor`` from the adj sheets (archiving previous
finals to timestamped sheets), and writes ``match_report``.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Any

import polars as pl

from ccat.acquire import SPECIAL_NEEDS_LABEL
from ccat.audit.store import (
    FINAL_FAMILY_COLUMNS,
    FINAL_SPONSOR_COLUMNS,
    STATUS_AUDITED,
    as_bool,
    build_adj_frame,
    project_final,
    read_adj,
)
from ccat.config import Config
from ccat.io import list_sheets, write_sheets

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
            1
            for child in children
            if str(child.get("special_needs") or "").strip() != ""
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


def _load_adj_sheets(
    cfg: Config,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Load the dense adj sheets match works from (§7.5).

    Args:
        cfg: Pipeline configuration.

    Returns:
        A tuple of (adj_family rows sorted by family_id, adj_sponsor
        rows sorted by sponsor_id, adj_child rows).

    Raises:
        MatchError: If the adj sheets are missing or empty (run
            ``ccat audit`` first, which seeds them).
    """
    adj_family = sorted(
        (
            row
            for row in read_adj(cfg, "family")
            if not as_bool(row.get("ignore"))
        ),
        key=lambda row: row["family_id"],
    )
    adj_sponsor = sorted(
        (
            row
            for row in read_adj(cfg, "sponsor")
            if not as_bool(row.get("ignore"))
        ),
        key=lambda row: row["sponsor_id"],
    )
    if not adj_family or not adj_sponsor:
        raise MatchError(
            "adj_family/adj_sponsor sheets are missing or empty; run"
            " `ccat audit` first (it seeds the adj sheets from raw)"
        )
    adj_child = read_adj(cfg, "child")
    return adj_family, adj_sponsor, adj_child


def _participating_children(
    adj_child: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Select adj_child rows that participate in matchmaking (§7.5).

    Only rows with ``status == "audited"``, ``ignore != true``, and a
    non-null ``angel_id``.

    Args:
        adj_child: All dense adj_child rows.

    Returns:
        The participating child rows.
    """
    return [
        row
        for row in adj_child
        if row.get("status") == STATUS_AUDITED
        and not as_bool(row.get("ignore"))
        and row.get("angel_id")
    ]


def _children_per_family(
    adj_child: list[dict[str, Any]],
) -> dict[Any, int]:
    """Count non-ignored ``adj_child`` rows per ``family_id`` (§7.5).

    Args:
        adj_child: All dense adj_child rows.

    Returns:
        Mapping of ``family_id`` to the number of non-ignored child
        rows sharing it.
    """
    counts: dict[Any, int] = {}
    for row in adj_child:
        if as_bool(row.get("ignore")):
            continue
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
        sponsor_rows: Dense adj_sponsor rows.
        children: Participating adj_child rows.

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
        and plan.sponsor_row.get("sponsor_type") in {"Organization", "Walmart"}
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
        ("organization/walmart sponsors", organizations + other),
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


def _reconcile_sponsor_assignments(
    adj_sponsor: list[dict[str, Any]],
) -> list[str]:
    """Propagate prior angel assignments inside ``adj_sponsor`` (§7.2).

    A non-empty ``prior_assigned_angel_ids`` is propagated to the
    sponsor's ``assigned_angel_ids`` (the raw manual entry wins over the
    carried-over list); otherwise the existing assignment carries over.

    Args:
        adj_sponsor: All dense adj_sponsor rows, updated in place.

    Returns:
        Warning messages for anomalies the auditor should know about.

    Raises:
        MatchError: If the same angel_id ends up assigned to more than
            one sponsor.
    """
    warnings: list[str] = []
    owners: dict[str, set[int]] = {}
    for row in adj_sponsor:
        sponsor_id = row["sponsor_id"]
        prior_text = row.get("prior_assigned_angel_ids")
        prior_ids = parse_prior_assigned(str(prior_text)) if prior_text else []
        if prior_ids:
            # Raw manual entry wins over the carried-over list (§8.5).
            assigned: str | None = ",".join(
                sorted(prior_ids, key=_angel_sort_key)
            )
        else:
            assigned = row.get("assigned_angel_ids")
        row["assigned_angel_ids"] = assigned
        row["num_angels_assigned"] = (
            len(parse_prior_assigned(assigned)) if assigned else 0
        )
        for angel_id in parse_prior_assigned(assigned) if assigned else []:
            owners.setdefault(angel_id, set()).add(sponsor_id)
    duplicates = {
        angel_id: sorted(sponsor_ids)
        for angel_id, sponsor_ids in owners.items()
        if len(sponsor_ids) > 1
    }
    if duplicates:
        detail = "; ".join(
            f"angel {angel_id} assigned to sponsors {sponsor_ids}"
            for angel_id, sponsor_ids in sorted(duplicates.items())
        )
        raise MatchError(
            "duplicate angel assignments in adj_sponsor:"
            f" {detail}; resolve manually and rerun"
        )
    return warnings


def _reconcile_family_assignments(
    adj_family: list[dict[str, Any]],
    children_per_family: dict[Any, int],
) -> list[str]:
    """Refresh ``num_angels_in_family`` on every adj_family row (§7.5).

    Args:
        adj_family: All dense adj_family rows, updated in place.
        children_per_family: Non-ignored adj_child row count per family.

    Returns:
        Warning messages (families whose assigned sponsor vanished are
        reported by the caller via the sponsor pool).
    """
    for row in adj_family:
        row["num_angels_in_family"] = children_per_family.get(
            row["family_id"], 0
        )
    return []


def _angel_family_map(
    adj_child: list[dict[str, Any]],
) -> dict[str, Any]:
    """Map ``angel_id`` to ``family_id`` from ``adj_child`` (§8.5).

    Includes every non-ignored row with a non-null ``angel_id``,
    regardless of audit state — reconciliation syncs assignments, it
    does not matchmake.

    Args:
        adj_child: All dense adj_child rows.

    Returns:
        Mapping of ``angel_id`` to the child's ``family_id``.
    """
    mapping: dict[str, Any] = {}
    for row in adj_child:
        if as_bool(row.get("ignore")):
            continue
        angel_id = row.get("angel_id")
        family_id = row.get("family_id")
        if angel_id and family_id is not None:
            mapping[str(angel_id)] = family_id
    return mapping


def _apply_prior_to_family_rows(
    family_rows: list[dict[str, Any]],
    sponsor_rows: list[dict[str, Any]],
    angel_family: dict[str, Any],
    valid_sponsor_ids: set[int],
    unassigned_only: bool = False,
) -> list[str]:
    """Point families at sponsors via propagated prior angels (§8.5).

    For every sponsor with a non-empty ``prior_assigned_angel_ids``, each
    prior angel's family gets ``assigned_sponsor_id`` set to that sponsor
    and ``assigned_angel_ids`` set to that sponsor's prior angels of the
    family.  Sibling carry-over lists are replaced, not merged — the raw
    manual entry is authoritative.  Unknown angel ids and families absent
    from ``adj_family`` warn.  A family assigned to another current
    ``adj_sponsor`` sponsor keeps that assignment and warns (split
    family); iterating in ``sponsor_id`` order makes the earlier sponsor
    win deterministically.  A family assigned to a sponsor no longer in
    ``adj_sponsor`` is a stale assignment: the prior wins.

    With ``unassigned_only`` (the full-match path), families that already
    carry an assignment are skipped entirely — the §8.5 machinery and
    pool matching stay authoritative — and only previously unassigned
    families are filled in.

    Args:
        family_rows: ``final_family`` rows, updated in place.
        sponsor_rows: Dense adj_sponsor rows, sorted by
            ``sponsor_id``.
        angel_family: ``angel_id`` to ``family_id`` map from
            ``adj_child``.
        valid_sponsor_ids: ``sponsor_id``s present in ``adj_sponsor``.
        unassigned_only: Only fill families with no current assignment.

    Returns:
        Warning messages for anomalies the auditor should know about.
    """
    by_family = {row["family_id"]: row for row in family_rows}
    warnings: list[str] = []
    for row in sponsor_rows:
        sponsor_id = row["sponsor_id"]
        prior_text = row.get("prior_assigned_angel_ids")
        prior_ids = parse_prior_assigned(str(prior_text)) if prior_text else []
        if not prior_ids:
            continue
        per_family: dict[Any, list[str]] = {}
        for angel_id in prior_ids:
            family_id = angel_family.get(angel_id)
            if family_id is None:
                message = (
                    f"sponsor {sponsor_id} prior angel {angel_id} is not"
                    " in adj_child; family assignment skipped"
                )
                warnings.append(message)
                logger.warning("%s", message)
                continue
            per_family.setdefault(family_id, []).append(angel_id)
        for family_id, angel_ids in per_family.items():
            family_row = by_family.get(family_id)
            if family_row is None:
                message = (
                    f"sponsor {sponsor_id} prior angel(s) {angel_ids}"
                    f" reference family {family_id}, which is not in"
                    " adj_family; skipped"
                )
                warnings.append(message)
                logger.warning("%s", message)
                continue
            current = family_row.get("assigned_sponsor_id")
            if unassigned_only:
                if current is not None:
                    # §8.5/pool assignment already in place; leave it.
                    continue
            elif current is not None and current != sponsor_id:
                if current not in valid_sponsor_ids:
                    # Stale pointer to a vanished sponsor; prior wins.
                    message = (
                        f"family {family_id} had stale assignment to"
                        f" sponsor {current}, who is no longer in"
                        f" adj_sponsor; reassigned to sponsor"
                        f" {sponsor_id} via prior angels {angel_ids}"
                    )
                    warnings.append(message)
                    logger.warning("%s", message)
                else:
                    message = (
                        f"family {family_id} is assigned to sponsor"
                        f" {current} but sponsor {sponsor_id}'s prior"
                        f" includes its angels {angel_ids}; keeping"
                        f" sponsor {current}"
                    )
                    warnings.append(message)
                    logger.warning("%s", message)
                    continue
            family_row["assigned_sponsor_id"] = sponsor_id
            family_row["assigned_angel_ids"] = ",".join(
                sorted(angel_ids, key=_angel_sort_key)
            )
    return warnings


def _duplicate_pool_warnings(
    adj_family: list[dict[str, Any]],
    adj_sponsor: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Flag unresolved duplicate entities left in the pools (§8.7).

    Screening flags potential duplicates but never merges; the user
    resolves them by ignoring the extras in the audit tool.  A
    duplicate pair that reaches the pools unresolved (both rows
    non-ignored with the same primary email) can double-match, so it is
    reported here.

    Args:
        adj_family: The non-ignored adj_family rows.
        adj_sponsor: The non-ignored adj_sponsor rows.

    Returns:
        match_report rows (kind ``duplicate``), one per unresolved pair.
    """
    report: list[dict[str, str]] = []
    by_email: dict[str, list[str]] = {}
    for row in adj_family:
        email = row.get("primary_email")
        if email:
            by_email.setdefault(str(email).strip().lower(), []).append(
                str(row["family_id"])
            )
    for email, family_ids in sorted(by_email.items()):
        if len(family_ids) > 1:
            report.append(
                {
                    "kind": "duplicate",
                    "ref": ",".join(family_ids),
                    "reason": (
                        f"unresolved potential duplicate families sharing"
                        f" primary email {email}; resolve in the audit"
                        " tool before matching"
                    ),
                }
            )
    by_sponsor_email: dict[str, list[str]] = {}
    for row in adj_sponsor:
        email = row.get("email")
        if email:
            by_sponsor_email.setdefault(str(email).strip().lower(), []).append(
                str(row["sponsor_id"])
            )
    for email, sponsor_ids in sorted(by_sponsor_email.items()):
        if len(sponsor_ids) > 1:
            report.append(
                {
                    "kind": "duplicate",
                    "ref": ",".join(sponsor_ids),
                    "reason": (
                        f"unresolved potential duplicate sponsors sharing"
                        f" email {email}; resolve in the audit tool"
                        " before matching"
                    ),
                }
            )
    return report


def run(
    cfg: Config,
    test: bool = False,
    overwrite: bool = False,
    skip_match: bool = False,
) -> None:
    """Run module 4 end to end (§8, audit_architecture.md §7.5).

    Pools come straight from the dense adj sheets: children with
    ``status == "audited"`` and a non-null ``angel_id``, non-ignored
    families and sponsors.  Assignments are written into the assignment
    columns of ``adj_family``/``adj_sponsor`` (every other column is
    preserved), then ``final_family``/``final_sponsor`` are projected
    from the adj sheets and ``match_report`` written (previous finals
    archived to timestamped sheets unless ``overwrite``).

    Args:
        cfg: Pipeline configuration.
        test: Stop after the first full sponsor: only the first sponsor
            in phase order is matched; the match_report records that this
            is a partial test run.  Has no effect with ``skip_match``.
        overwrite: Replace previous ``final_family``/``final_sponsor``/
            ``match_report`` outputs in place instead of archiving them
            (§8.6).  The child pool gate (``status == "audited"``) is
            structural and never bypassed.
        skip_match: Reconcile the assignment columns inside the adj
            sheets without running the matching algorithm: prior
            assignments carry over, a non-empty
            ``prior_assigned_angel_ids`` propagates to the sponsor's
            ``assigned_angel_ids`` and points each prior angel's family
            at that sponsor in ``adj_family``;
            ``num_angels_in_family`` is recomputed from ``adj_child``;
            duplicate angel ids across sponsors raise ``MatchError``.
            The finals are re-projected; ``match_report`` is untouched.

    Raises:
        MatchError: If the adj sheets are missing (run ``ccat audit``
            first) or duplicate angel assignments are found.
    """
    adj_family, adj_sponsor, adj_child = _load_adj_sheets(cfg)
    children_per_family = _children_per_family(adj_child)

    if skip_match:
        if test:
            logger.warning("--test has no effect with --skip-match")
        _reconcile_sponsor_assignments(adj_sponsor)
        _reconcile_family_assignments(adj_family, children_per_family)
        _apply_prior_to_family_rows(
            adj_family,
            adj_sponsor,
            _angel_family_map(adj_child),
            {row["sponsor_id"] for row in adj_sponsor},
        )
        _write_outputs(
            cfg, adj_family, adj_sponsor, None, archive=not overwrite
        )
        logger.info(
            "skip-match: reconciled %d adj_family rows;"
            " reconciled %d adj_sponsor rows; matching algorithm skipped,"
            " match_report untouched",
            len(adj_family),
            len(adj_sponsor),
        )
        return

    children = _participating_children(adj_child)
    logger.info("participating audited children: %d", len(children))

    pre_assigned, report = reconcile_pre_assignments(adj_sponsor, children)
    pre_assigned_angels = {
        angel_id
        for plan in pre_assigned.values()
        for angel_id in plan.assigned_angel_ids
    }
    children_by_family: dict[int, list[dict[str, Any]]] = {}
    for child in children:
        children_by_family.setdefault(child["family_id"], []).append(child)

    family_by_id = {row["family_id"]: row for row in adj_family}
    pool_families = [
        PoolFamily(
            family_id=family_id,
            family_row=family_by_id[family_id],
            children=family_children,
        )
        for family_id, family_children in sorted(children_by_family.items())
        if family_id in family_by_id
        and not any(
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
        for row in adj_sponsor
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
    for family_row in adj_family:
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

    for row in adj_family:
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
        row["assigned_sponsor_id"] = sponsor_id
        row["assigned_angel_ids"] = assigned_angels
        row["num_angels_in_family"] = children_per_family.get(family_id, 0)
    # Prior angels also point their family at the sponsor when the §8.5
    # machinery left the family unassigned (e.g. partially covered).
    _apply_prior_to_family_rows(
        adj_family,
        adj_sponsor,
        _angel_family_map(adj_child),
        {row["sponsor_id"] for row in adj_sponsor},
        unassigned_only=True,
    )
    sponsor_plans = {
        plan.sponsor_id: plan
        for plan in pool_sponsors + list(pre_assigned.values())
    }
    for row in adj_sponsor:
        plan = sponsor_plans.get(row["sponsor_id"])
        angel_ids = (
            sorted(plan.assigned_angel_ids, key=_angel_sort_key) if plan else []
        )
        row["assigned_angel_ids"] = ",".join(angel_ids) if angel_ids else None
        row["num_angels_assigned"] = len(angel_ids)

    report.extend(_duplicate_pool_warnings(adj_family, adj_sponsor))
    report_frame = pl.DataFrame(
        report,
        schema={"kind": pl.String, "ref": pl.String, "reason": pl.String},
    )
    _write_outputs(
        cfg, adj_family, adj_sponsor, report_frame, archive=not overwrite
    )
    report_frame.write_csv(cfg.match_report_csv)
    logger.info(
        "matched %d families; report has %d entries; csv at %s",
        len(matched_family_ids),
        report_frame.height,
        cfg.match_report_csv,
    )


def _write_outputs(
    cfg: Config,
    adj_family: list[dict[str, Any]],
    adj_sponsor: list[dict[str, Any]],
    report_frame: pl.DataFrame | None,
    archive: bool,
) -> None:
    """Write adj assignments, projected finals, and the report (§7.2).

    Assignments land in the assignment columns of the adj sheets (all
    other columns preserved by the dense upsert);
    ``final_family``/``final_sponsor`` are projections of the adj sheets;
    previous finals (and the report, when written) are archived to
    timestamped sheets first unless ``archive`` is false.

    Args:
        cfg: Pipeline configuration.
        adj_family: Updated dense adj_family rows.
        adj_sponsor: Updated dense adj_sponsor rows.
        report_frame: The match_report frame, or None to leave the
            sheet untouched.
        archive: Archive previous finals in timestamped sheets.
    """
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    existing = set(list_sheets(cfg.excel_file))
    archivable = ("final_family", "final_sponsor", "match_report")
    renames = (
        (
            {
                name: _archive_name(existing, name, timestamp)
                for name in archivable
                if name in existing
            }
            if archive
            else {}
        )
        if report_frame is not None
        else (
            {
                name: _archive_name(existing, name, timestamp)
                for name in ("final_family", "final_sponsor")
                if name in existing
            }
            if archive
            else {}
        )
    )
    sheets: dict[str, pl.DataFrame] = {
        "adj_family": build_adj_frame("family", adj_family),
        "adj_sponsor": build_adj_frame("sponsor", adj_sponsor),
        "final_family": project_final(
            build_adj_frame("family", adj_family), FINAL_FAMILY_COLUMNS
        ),
        "final_sponsor": project_final(
            build_adj_frame("sponsor", adj_sponsor), FINAL_SPONSOR_COLUMNS
        ),
    }
    if report_frame is not None:
        sheets["match_report"] = report_frame
    write_sheets(cfg.excel_file, "match", sheets, renames=renames)


def _archive_name(existing: set[str], sheet: str, timestamp: str) -> str:
    """Build a non-colliding timestamped archive sheet name (§8.6)."""
    candidate = f"{sheet}_{timestamp}"
    suffix = 1
    while candidate in existing:
        candidate = f"{sheet}_{timestamp}_{suffix}"
        suffix += 1
    existing.add(candidate)
    return candidate
