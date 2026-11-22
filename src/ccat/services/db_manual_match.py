"""Manual matching: the pool, the sponsor picker, assign, release, holds.

The manual matcher places children with sponsors by hand.  It reads the
three final stores once per request and derives every card, summary and
conflict from them; nothing here touches HTTP shapes beyond the DTOs the
routes return.

The child row is the assignment grain.  A request therefore writes the
complete child, family and sponsor stores through one
:meth:`SeasonStoreRepository.commit_assignments` transaction, with only
the assignment columns differing from storage, and the family and
sponsor summaries derived by :mod:`ccat.services.db_assignments`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ccat.api.dto import (
    ManualAssignmentEntry,
    ManualAssignmentFamilySummary,
    ManualAssignmentListItem,
    ManualAssignmentListPage,
    ManualAssignmentResult,
    ManualAssignmentSponsorSummary,
    ManualHoldListPage,
    ManualHoldResult,
    ManualMatchAutomaticRun,
    ManualMatchChildCard,
    ManualMatchFamilyCard,
    ManualMatchHold,
    ManualMatchPoolPage,
    ManualMatchSponsor,
    ManualMatchSponsorPage,
)
from ccat.audit.store import as_bool
from ccat.domain import sibling_dob_conflicts
from ccat.match import (
    _angel_sort_key,
    PoolFamily,
    SponsorPlan,
    canonical_location,
    family_score,
    special_needs_weight,
)
from ccat.services.db_assignments import (
    SOURCE_MANUAL,
    apply_family_summary,
    apply_sponsor_summary,
    assignable_children,
    assignment_state,
    child_sponsor,
    group_children_by_family,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from ccat.api.dto import (
        ManualAssignmentRequest,
        ManualHoldReleaseRequest,
        ManualHoldRequest,
        ManualReleaseRequest,
    )
    from ccat.config import Config
    from ccat.db.repositories import SeasonStoreRepository


#: Codes a :class:`ManualMatchError` may carry.  The routes map them to
#: HTTP statuses; the service itself has no status numbers.
NOT_FOUND = "not_found"
CONFLICT = "conflict"
SPONSOR_FULL = "sponsor_full"
MATCH_RUNNING = "match_running"
STALE_VERSION = "stale_version"
INVALID_INPUT = "invalid_input"


class ManualMatchError(Exception):
    """A refusal the manual-matching routes turn into an HTTP error.

    Attributes:
        code: Stable machine-readable code.
        context: Extra JSON-safe context fields.
    """

    def __init__(self, code: str, message: str, **context: Any) -> None:
        """Initialize the error.

        Args:
            code: Stable machine-readable code.
            message: Safe human-readable message.
            context: Extra JSON-safe context fields.
        """
        self.code = code
        self.context = context
        super().__init__(message)


@dataclass
class PoolParams:
    """The ``GET /pool`` query values (validated by the route)."""

    page: int = 1
    page_size: int = 25
    q: str = ""
    assignment: str = "unassigned"
    child_gender: str = "any"
    age_min: int | None = None
    age_max: int | None = None
    special_needs: str = "any"
    location: str = "any"
    family_size_min: int | None = None
    family_size_max: int | None = None
    child_status: str = "any"
    hold: str = "not_held"
    sponsor_id: int | None = None


@dataclass
class SponsorParams:
    """The ``GET /sponsors`` query values (validated by the route)."""

    page: int = 1
    page_size: int = 25
    q: str = ""
    affiliation: str | None = None
    has_capacity: bool | None = None


@dataclass
class AssignmentParams:
    """The ``GET /assignments`` query values (validated by the route)."""

    page: int = 1
    page_size: int = 25
    sponsor_id: int | None = None
    source: str | None = None
    family_id: int | None = None


@dataclass
class HoldParams:
    """The ``GET /holds`` query values (validated by the route)."""

    page: int = 1
    page_size: int = 25


# ---------------------------------------------------------------------------
# Small shared helpers.
# ---------------------------------------------------------------------------


async def _stores(
    repo: SeasonStoreRepository,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Read the complete child, family and sponsor stores.

    Args:
        repo: The season-bound repository.

    Returns:
        The three stores, children first.
    """
    return (
        await repo.final_rows("child"),
        await repo.final_rows("family"),
        await repo.final_rows("sponsor"),
    )


def _live_children(
    grouped: dict[Any, list[dict[str, Any]]], family_id: Any
) -> list[dict[str, Any]]:
    """Return one family's live children.

    Args:
        grouped: Non-ignored children grouped by family.
        family_id: The family id.

    Returns:
        The children that are neither ignored nor status-ignored.
    """
    return [
        child
        for child in grouped.get(family_id, [])
        if str(child.get("status") or "") != "ignored"
    ]


def _text(value: Any) -> str | None:
    """Render a stored value as stripped text, or None.

    Args:
        value: The stored value.

    Returns:
        The text, or None when blank.
    """
    text = str(value).strip() if value is not None else ""
    return text or None


def _int(value: Any) -> int | None:
    """Render a stored value as an int, or None.

    Args:
        value: The stored value.

    Returns:
        The integer, or None when absent or unparsable.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sizes(child: dict[str, Any]) -> dict[str, str | None]:
    """Group the child's size columns into one display map.

    Args:
        child: One final child row.

    Returns:
        The ``shoe`` / ``pant`` / ``shirt`` / ``jean`` values.
    """

    def join(*values: Any) -> str | None:
        parts = [text for text in (_text(value) for value in values) if text]
        return " ".join(parts) or None

    return {
        "shoe": join(child.get("shoe_size"), child.get("shoe_size_type")),
        "pant": join(
            child.get("pant_waist_size"),
            child.get("pant_inseam"),
            child.get("pant_size_letter"),
            child.get("pant_size_type"),
        ),
        "shirt": join(
            child.get("shirt_size_letter"), child.get("shirt_size_type")
        ),
        "jean": _text(child.get("jean_size")),
    }


def _child_card(child: dict[str, Any]) -> ManualMatchChildCard:
    """Build one child's card line.

    Args:
        child: One final child row.

    Returns:
        The card line.
    """
    angel_id = _text(child.get("angel_id"))
    duplicate = as_bool(child.get("potential_duplicate"))
    reason = None
    if duplicate:
        reason = "pending duplicate group"
    elif angel_id is None:
        reason = "no angel id"
    return ManualMatchChildCard(
        zite_record_id=str(child.get("zite_record_id") or ""),
        angel_id=angel_id,
        first_name=_text(child.get("first_name")),
        age_at_xmas=_int(child.get("age_at_xmas")),
        gender=_text(child.get("gender")),
        special_needs=bool(_text(child.get("special_needs"))),
        need_1=_text(child.get("need_1")),
        need_2=_text(child.get("need_2")),
        desire_1=_text(child.get("desire_1")),
        desire_2=_text(child.get("desire_2")),
        interests=_text(child.get("interests")),
        other_notes=_text(child.get("other_notes")),
        favorite_colors=_text(child.get("favorite_colors")),
        sizes=_sizes(child),
        status=_text(child.get("status")),
        assigned_sponsor_id=child_sponsor(child),
        assignment_source=_text(child.get("assignment_source")),
        generated_info_sheet=as_bool(child.get("generated_info_sheet")),
        row_version=_int(child.get("_row_version")) or 0,
        selectable=reason is None,
        unselectable_reason=reason,
    )


def _hold_dto(hold: dict[str, Any] | None) -> ManualMatchHold:
    """Build one card hold section.

    Args:
        hold: The active hold row, or None.

    Returns:
        The hold DTO.
    """
    if hold is None:
        return ManualMatchHold()
    return ManualMatchHold(
        held=True,
        note=hold.get("note"),
        created_by=hold.get("created_by"),
        created_at=hold.get("created_at"),
    )


def _child_matches(
    child: dict[str, Any],
    params: PoolParams,
) -> bool:
    """Apply the existential child-level filters to one child.

    Args:
        child: One live child row.
        params: The pool filters.

    Returns:
        True when the child satisfies every active child filter.
    """
    if params.child_gender != "any" and child.get("gender") != (
        params.child_gender
    ):
        return False
    age = _int(child.get("age_at_xmas"))
    if params.age_min is not None and (age is None or age < params.age_min):
        return False
    if params.age_max is not None and (age is None or age > params.age_max):
        return False
    needs = bool(_text(child.get("special_needs")))
    if params.special_needs == "yes" and not needs:
        return False
    if params.special_needs == "no" and needs:
        return False
    if params.child_status != "any" and child.get("status") != (
        params.child_status
    ):
        return False
    return True


def _family_matches(
    family: dict[str, Any],
    children: list[dict[str, Any]],
    params: PoolParams,
    state: str,
    held: bool,
    cfg: Config,
) -> bool:
    """Apply every pool filter to one candidate family.

    Args:
        family: The family row.
        children: The family's live children.
        params: The pool filters.
        state: The family's derived assignment state.
        held: Whether the family carries an active hold.
        cfg: Pipeline configuration.

    Returns:
        True when the family belongs in the filtered pool.
    """
    if params.assignment != "any" and state != params.assignment:
        return False
    if params.hold == "held" and not held:
        return False
    if params.hold == "not_held" and held:
        return False
    bucket = canonical_location(
        _text(family.get("gift_pickup_location")), cfg.location_groups
    )
    if params.location != "any" and bucket != params.location:
        return False
    size = len(children)
    if params.family_size_min is not None and size < params.family_size_min:
        return False
    if params.family_size_max is not None and size > params.family_size_max:
        return False
    if params.q:
        needle = params.q.strip().lower()
        haystack = [
            family.get("first_name"),
            family.get("last_name"),
            family.get("primary_email"),
            family.get("city"),
            family.get("zip_code"),
            *(child.get("first_name") for child in children),
        ]
        if not any(
            needle in str(value).lower()
            for value in haystack
            if value is not None
        ):
            return False
    child_filters = (
        params.child_gender != "any"
        or params.age_min is not None
        or params.age_max is not None
        or params.special_needs != "any"
        or params.child_status != "any"
    )
    if child_filters and not any(
        _child_matches(child, params) for child in children
    ):
        return False
    return True


def _family_card(
    family: dict[str, Any],
    children: list[dict[str, Any]],
    hold: dict[str, Any] | None,
    conflicts: set[Any],
) -> ManualMatchFamilyCard:
    """Build one family card from rows already in memory.

    Args:
        family: The family row.
        children: The family's live children.
        hold: The active hold, or None.
        conflicts: Family ids carrying a sibling DOB conflict.

    Returns:
        The card.
    """
    cards = [_child_card(child) for child in children]
    assigned = [child for child in children if child_sponsor(child) is not None]
    warnings: list[str] = []
    if not any(card.selectable for card in cards):
        warnings.append("no selectable child")
    if family.get("family_id") in conflicts:
        warnings.append("sibling DOB conflict")
    return ManualMatchFamilyCard(
        family_id=int(family.get("family_id") or 0),
        row_version=_int(family.get("_row_version")) or 0,
        first_name=_text(family.get("first_name")),
        last_name=_text(family.get("last_name")),
        primary_email=_text(family.get("primary_email")),
        phone=_text(family.get("phone")),
        city=_text(family.get("city")),
        zip_code=_text(family.get("zip_code")),
        state=_text(family.get("state")),
        gift_pickup_location=_text(family.get("gift_pickup_location")),
        status=_text(family.get("status")),
        family_size=len(children),
        assigned_count=len(assigned),
        assigned_sponsor_ids=sorted(
            {
                sponsor
                for sponsor in (child_sponsor(child) for child in assigned)
                if sponsor is not None
            }
        ),
        assignment_state=assignment_state(children),
        children=cards,
        hold=_hold_dto(hold),
        warnings=warnings,
    )


def _conflict_family_ids(
    children: list[dict[str, Any]], cfg: Config
) -> set[Any]:
    """Return the family ids carrying a sibling DOB conflict.

    Args:
        children: The non-ignored child rows.
        cfg: Pipeline configuration.

    Returns:
        The conflicting family ids (empty when none).
    """
    try:
        rows = sibling_dob_conflicts(children, cfg, report=False)
    except ValueError:
        return set()
    return {row.get("family_id") for row in rows}


def _sponsor_plan(row: dict[str, Any]) -> SponsorPlan:
    """Build a scoring plan for one sponsor row.

    Args:
        row: The sponsor row.

    Returns:
        The plan (a missing age bound is widened for scoring).
    """
    sponsor_row = dict(row)
    if _int(sponsor_row.get("pref_age_min")) is None or (
        _int(sponsor_row.get("pref_age_max")) is None
    ):
        sponsor_row["pref_age_min"] = 0
        sponsor_row["pref_age_max"] = 999
    return SponsorPlan(
        sponsor_row=sponsor_row,
        sponsor_id=int(row.get("sponsor_id") or 0),
        num_children=_int(row.get("num_children")) or 0,
        is_special_needs=row.get("age_preference") == "Special Needs",
        is_pre_assigned=False,
    )


def _score_family(
    plan: SponsorPlan,
    family: dict[str, Any],
    children: list[dict[str, Any]],
    cfg: Config,
    sn_weight: float,
) -> float | None:
    """Score one family against one sponsor, at child grain.

    Args:
        plan: The sponsor's scoring plan.
        family: The family row.
        children: The family's assignable children.
        cfg: Pipeline configuration.
        sn_weight: The tier-0 special-needs weight.

    Returns:
        The score, or None when the pairing is forbidden or has no
        age-scorable child.
    """
    scoring = [
        dict(child, age_at_xmas=_int(child.get("age_at_xmas")) or -1)
        for child in children
    ]
    if not scoring:
        return None
    return family_score(
        plan,
        PoolFamily(
            family_id=int(family.get("family_id") or 0),
            family_row=family,
            children=scoring,
        ),
        cfg,
        sn_weight,
    )


# ---------------------------------------------------------------------------
# Read: the pool.
# ---------------------------------------------------------------------------


async def pool(
    repo: SeasonStoreRepository, cfg: Config, params: PoolParams
) -> ManualMatchPoolPage:
    """Build one filtered, paginated page of family cards.

    Args:
        repo: The season-bound repository.
        cfg: Pipeline configuration (location groups and weights).
        params: The query filters.

    Returns:
        The pool page, with the automatic matcher's state.

    Raises:
        ManualMatchError: 404 when ``sponsor_id`` names no sponsor.
    """
    child_rows, family_rows, sponsor_rows = await _stores(repo)
    grouped = group_children_by_family(child_rows)
    held_ids = await repo.active_hold_family_ids()
    job = await repo.active_match_job()

    sponsor_plan: SponsorPlan | None = None
    sn_weight = 0.0
    scores: dict[Any, float] = {}
    if params.sponsor_id is not None:
        sponsor = next(
            (
                row
                for row in sponsor_rows
                if row.get("sponsor_id") == params.sponsor_id
                and not as_bool(row.get("ignore"))
            ),
            None,
        )
        if sponsor is None:
            raise ManualMatchError(
                NOT_FOUND, f"unknown sponsor {params.sponsor_id}"
            )
        sponsor_plan = _sponsor_plan(sponsor)
        sn_weight = special_needs_weight(
            cfg,
            max(
                (
                    _int(row.get("num_children")) or 0
                    for row in sponsor_rows
                    if not as_bool(row.get("ignore"))
                ),
                default=25,
            ),
        )

    conflicts = _conflict_family_ids(
        [
            child
            for family_id in grouped
            for child in _live_children(grouped, family_id)
        ],
        cfg,
    )
    candidates: list[
        tuple[int, dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]
    ] = []
    for family in family_rows:
        if as_bool(family.get("ignore")):
            continue
        if str(family.get("status") or "") == "ignored":
            continue
        family_id = family.get("family_id")
        children = _live_children(grouped, family_id)
        if not children:
            continue
        if not any(
            not as_bool(child.get("potential_duplicate")) for child in children
        ):
            continue
        state = assignment_state(children)
        if not _family_matches(
            family, children, params, state, family_id in held_ids, cfg
        ):
            continue
        if sponsor_plan is not None:
            score = _score_family(
                sponsor_plan,
                family,
                assignable_children(family_rows, children),
                cfg,
                sn_weight,
            )
            if score is None:
                continue
            scores[family_id] = score
        candidates.append((int(family_id or 0), family, children, None))

    if sponsor_plan is not None:
        candidates.sort(key=lambda item: (-scores[item[0]], item[0]))
    else:
        candidates.sort(key=lambda item: item[0])

    total = len(candidates)
    start = (params.page - 1) * params.page_size
    page_items = candidates[start : start + params.page_size]
    holds = await repo.family_holds(
        [int(family.get("family_id") or 0) for _, family, _, _ in page_items]
    )
    cards = [
        _family_card(
            family,
            children,
            holds.get(int(family.get("family_id") or 0)),
            conflicts,
        )
        for _, family, children, _ in page_items
    ]
    return ManualMatchPoolPage(
        items=cards,
        total=total,
        page=params.page,
        page_size=params.page_size,
        automatic_run=ManualMatchAutomaticRun(
            active=job is not None,
            job_id=job.get("job_id") if job else None,
            status=job.get("status") if job else None,
            started_at=job.get("started_at") if job else None,
        ),
    )


# ---------------------------------------------------------------------------
# Read: the sponsor picker.
# ---------------------------------------------------------------------------


def _sponsor_assigned_counts(
    children: list[dict[str, Any]],
) -> dict[int, int]:
    """Count the children placed with each sponsor.

    Args:
        children: The complete child store.

    Returns:
        Mapping of sponsor id -> assigned child count.
    """
    counts: dict[int, int] = {}
    for child in children:
        if as_bool(child.get("ignore")):
            continue
        sponsor_id = child_sponsor(child)
        if sponsor_id is None:
            continue
        counts[sponsor_id] = counts.get(sponsor_id, 0) + 1
    return counts


def _sponsor_dto(
    row: dict[str, Any], assigned: dict[int, int]
) -> ManualMatchSponsor:
    """Build one sponsor picker row.

    Args:
        row: The sponsor row.
        assigned: Sponsor id -> assigned child count.

    Returns:
        The picker row.
    """
    sponsor_id = int(row.get("sponsor_id") or 0)
    count = assigned.get(sponsor_id, 0)
    limit = _int(row.get("num_children"))
    name = _text(row.get("organization_name"))
    if name is None:
        name = " ".join(
            part
            for part in (
                _text(row.get("first_name")),
                _text(row.get("last_name")),
            )
            if part
        )
    return ManualMatchSponsor(
        sponsor_id=sponsor_id,
        row_version=_int(row.get("_row_version")) or 0,
        display_name=name,
        email=_text(row.get("email")),
        phone=_text(row.get("phone")),
        sponsor_type=_text(row.get("sponsor_type")),
        num_children=limit,
        assigned_count=count,
        remaining=None if limit is None else max(limit - count, 0),
        age_preference=_text(row.get("age_preference")),
        gender_preference=_text(row.get("gender_preference")),
        dropoff_location=_text(row.get("dropoff_location")),
        pref_age_min=_int(row.get("pref_age_min")),
        pref_age_max=_int(row.get("pref_age_max")),
        status=_text(row.get("status")),
    )


async def sponsors(
    repo: SeasonStoreRepository, cfg: Config, params: SponsorParams
) -> ManualMatchSponsorPage:
    """Build one filtered, paginated page of the sponsor picker.

    Args:
        repo: The season-bound repository.
        cfg: Pipeline configuration (unused; kept for symmetry).
        params: The query filters.

    Returns:
        The sponsor page.
    """
    child_rows, _families, sponsor_rows = await _stores(repo)
    assigned = _sponsor_assigned_counts(child_rows)
    items: list[ManualMatchSponsor] = []
    for row in sponsor_rows:
        if as_bool(row.get("ignore")):
            continue
        dto = _sponsor_dto(row, assigned)
        if params.affiliation and dto.sponsor_type != params.affiliation:
            continue
        if params.has_capacity is not None:
            has_room = dto.remaining is None or dto.remaining > 0
            if has_room != params.has_capacity:
                continue
        if params.q:
            needle = params.q.strip().lower()
            haystack = (
                dto.display_name,
                dto.email,
                _text(row.get("organization_name")),
            )
            if not any(
                needle in value.lower()
                for value in haystack
                if value is not None
            ):
                continue
        items.append(dto)
    items.sort(key=lambda item: (item.display_name.lower(), item.sponsor_id))
    total = len(items)
    start = (params.page - 1) * params.page_size
    return ManualMatchSponsorPage(
        items=items[start : start + params.page_size],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


# ---------------------------------------------------------------------------
# Write: assign.
# ---------------------------------------------------------------------------


def _row_index(rows: Iterable[dict[str, Any]], key: str) -> dict[Any, dict]:
    """Index rows by one key column.

    Args:
        rows: The rows.
        key: The key column.

    Returns:
        Mapping of key -> row.
    """
    return {row.get(key): row for row in rows}


def _resolve_sponsor(
    sponsor_rows: list[dict[str, Any]], sponsor_id: int
) -> dict[str, Any]:
    """Resolve a selected sponsor, refusing unknown and ignored rows.

    Args:
        sponsor_rows: The complete sponsor store.
        sponsor_id: The selected sponsor.

    Returns:
        The sponsor row.

    Raises:
        ManualMatchError: 404 unknown; 409 ignored.
    """
    row = _row_index(sponsor_rows, "sponsor_id").get(sponsor_id)
    if row is None:
        raise ManualMatchError(NOT_FOUND, f"unknown sponsor {sponsor_id}")
    if as_bool(row.get("ignore")) or str(row.get("status") or "") == "ignored":
        raise ManualMatchError(CONFLICT, f"sponsor {sponsor_id} is ignored")
    return row


def _expand_selection(
    request: ManualAssignmentRequest,
    child_rows: list[dict[str, Any]],
    family_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Resolve the request's family and child ids to selected children.

    Family ids expand against the live store to their eligible children;
    explicit child ids are validated strictly.

    Args:
        request: The assign request.
        child_rows: The complete child store.
        family_rows: The complete family store.

    Returns:
        The deduplicated selected children.

    Raises:
        ManualMatchError: 422 duplicate or empty selection; 404 unknown;
            409 ignored, orphan, letterless, duplicate-group, or
            ignored-family refusals.
    """
    if len(set(request.child_ids)) != len(request.child_ids):
        raise ManualMatchError(
            INVALID_INPUT, "the request lists the same child twice"
        )
    by_id = _row_index(child_rows, "zite_record_id")
    families = _row_index(family_rows, "family_id")
    grouped = group_children_by_family(child_rows)
    eligible = {
        child.get("zite_record_id")
        for child in assignable_children(family_rows, child_rows)
        if str(child.get("status") or "") != "ignored"
    }
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for family_id in dict.fromkeys(request.family_ids):
        family = families.get(family_id)
        if family is None:
            raise ManualMatchError(NOT_FOUND, f"unknown family {family_id}")
        if (
            as_bool(family.get("ignore"))
            or str(family.get("status") or "") == "ignored"
        ):
            raise ManualMatchError(CONFLICT, f"family {family_id} is ignored")
        for child in grouped.get(family_id, []):
            record_id = str(child.get("zite_record_id") or "")
            if record_id in eligible and record_id not in seen:
                seen.add(record_id)
                selected.append(child)
    for record_id in request.child_ids:
        child = by_id.get(record_id)
        if child is None:
            raise ManualMatchError(NOT_FOUND, f"unknown child {record_id!r}")
        if as_bool(child.get("ignore")) or (
            str(child.get("status") or "") == "ignored"
        ):
            raise ManualMatchError(CONFLICT, f"child {record_id!r} is ignored")
        if child.get("family_id") is None:
            raise ManualMatchError(
                CONFLICT, f"child {record_id!r} is an orphan"
            )
        family = families.get(child.get("family_id"))
        if (
            family is None
            or as_bool(family.get("ignore"))
            or str(family.get("status") or "") == "ignored"
        ):
            raise ManualMatchError(
                CONFLICT, f"family of child {record_id!r} is ignored"
            )
        if not _text(child.get("angel_id")):
            raise ManualMatchError(
                CONFLICT, f"child {record_id!r} has no angel id"
            )
        if as_bool(child.get("potential_duplicate")):
            raise ManualMatchError(
                CONFLICT,
                f"child {record_id!r} is in an unresolved duplicate group",
            )
        if record_id not in seen:
            seen.add(record_id)
            selected.append(child)
    if not selected:
        raise ManualMatchError(INVALID_INPUT, "the selection is empty")
    return selected


def _stale_context(
    entity: str, key: Any, row: dict[str, Any]
) -> dict[str, Any]:
    """Build the 412 context for one stale row (audit.py's shape).

    Args:
        entity: The entity name.
        key: The row key.
        row: The current stored row.

    Returns:
        The context fields.
    """
    return {
        "entity": entity,
        "key": str(key),
        "current_version": row.get("_row_version"),
        "row": {
            column: value
            for column, value in row.items()
            if not column.startswith("_")
        },
        "last_updated_by": row.get("_last_updated_by"),
        "last_updated_at": row.get("_last_updated_at"),
    }


def _check_expected_versions(
    expected: dict[str, int],
    child_rows: list[dict[str, Any]],
    family_rows: list[dict[str, Any]],
    sponsor_rows: list[dict[str, Any]],
) -> None:
    """Refuse a stale expected version before any write.

    Args:
        expected: The flat prefixed expectation map.
        child_rows: The complete child store.
        family_rows: The complete family store.
        sponsor_rows: The complete sponsor store.

    Raises:
        ManualMatchError: 412 on a mismatch, with the current row.
    """
    indexes = {
        "child": (_row_index(child_rows, "zite_record_id"), "zite_record_id"),
        "family": (_row_index(family_rows, "family_id"), "family_id"),
        "sponsor": (_row_index(sponsor_rows, "sponsor_id"), "sponsor_id"),
    }
    for prefixed, version in expected.items():
        entity, _, key = prefixed.partition(":")
        entry = indexes.get(entity)
        if entry is None:
            continue
        index, key_column = entry
        row = index.get(key if entity == "child" else _int(key))
        if row is None:
            continue
        if _int(row.get("_row_version")) != version:
            raise ManualMatchError(
                STALE_VERSION,
                f"{entity} {key!r} changed since it was read; reload",
                **_stale_context(entity, key, row),
            )


def _require_versions(
    selected: Sequence[dict[str, Any]],
    sponsor_id: int,
    expected: dict[str, int],
) -> None:
    """Require an expected version for every selected child and the sponsor.

    Args:
        selected: The selected children.
        sponsor_id: The selected sponsor.
        expected: The flat prefixed expectation map.

    Raises:
        ManualMatchError: 422 when an entry is missing.
    """
    for child in selected:
        key = f"child:{child.get('zite_record_id')}"
        if key not in expected:
            raise ManualMatchError(
                INVALID_INPUT,
                f"expected_versions is missing {key}; re-read the pool",
            )
    key = f"sponsor:{sponsor_id}"
    if key not in expected:
        raise ManualMatchError(
            INVALID_INPUT,
            f"expected_versions is missing {key}; re-read the pool",
        )


def _assignment_warnings(
    selected: Sequence[dict[str, Any]],
    sponsor: dict[str, Any],
    family_rows: list[dict[str, Any]],
    child_rows: list[dict[str, Any]],
    cfg: Config,
) -> list[str]:
    """Compute the advisory warnings for one assignment (D8).

    Args:
        selected: The children being assigned.
        sponsor: The sponsor row.
        family_rows: The complete family store.
        child_rows: The complete child store.
        cfg: Pipeline configuration.

    Returns:
        The warnings; they never block.
    """
    warnings: list[str] = []
    gender = _text(sponsor.get("gender_preference")) or "Any Gender"
    low = _int(sponsor.get("pref_age_min"))
    high = _int(sponsor.get("pref_age_max"))
    sponsor_bucket = canonical_location(
        _text(sponsor.get("dropoff_location")), cfg.location_groups
    )
    families = _row_index(family_rows, "family_id")
    groups = group_children_by_family(child_rows)
    for child in selected:
        label = _text(child.get("angel_id")) or str(
            child.get("zite_record_id") or ""
        )
        if gender != "Any Gender" and child.get("gender") != gender:
            warnings.append(
                f"sponsor prefers {gender}; child {label} is"
                f" {_text(child.get('gender')) or 'unknown'}"
            )
        age = _int(child.get("age_at_xmas"))
        if (
            age is not None
            and low is not None
            and high is not None
            and not low <= age <= high
        ):
            warnings.append(
                f"child {label} is age {age}, outside the sponsor's"
                f" {low}-{high} preference"
            )
        if str(child.get("status") or "") != "audited":
            warnings.append(f"child {label} is not audited")
        if as_bool(child.get("generated_info_sheet")):
            warnings.append(
                f"child {label} already has a generated info sheet;"
                " regenerate it after assigning"
            )
        family_id = child.get("family_id")
        family = families.get(family_id)
        if family is not None:
            bucket = canonical_location(
                _text(family.get("gift_pickup_location")), cfg.location_groups
            )
            if (
                sponsor_bucket is not None
                and bucket is not None
                and sponsor_bucket != bucket
            ):
                warnings.append(
                    f"family {family_id} pickup location does not match the"
                    " sponsor's dropoff location"
                )
        state = assignment_state(_live_children(groups, family_id))
        if state in {"partial", "split"}:
            warnings.append(f"family {family_id} would be {state}")
    conflicts = _conflict_family_ids(
        [child for group in groups.values() for child in group], cfg
    )
    for family_id in {child.get("family_id") for child in selected} & conflicts:
        warnings.append(f"family {family_id} has a sibling DOB conflict")
    return list(dict.fromkeys(warnings))


def _entry(
    child: dict[str, Any], sponsor: int | None = None
) -> ManualAssignmentEntry:
    """Build one assignment outcome entry.

    Args:
        child: The child row.
        sponsor: The previous sponsor, when the child moved.

    Returns:
        The entry DTO.
    """
    return ManualAssignmentEntry(
        zite_record_id=str(child.get("zite_record_id") or ""),
        angel_id=_text(child.get("angel_id")),
        family_id=_int(child.get("family_id")),
        from_sponsor_id=sponsor,
    )


def _family_summaries(
    family_ids: Iterable[Any],
    family_rows: list[dict[str, Any]],
    child_rows: list[dict[str, Any]],
    released: set[Any],
) -> list[ManualAssignmentFamilySummary]:
    """Build the affected families' post-write summaries.

    Args:
        family_ids: The affected family ids.
        family_rows: The current family store.
        child_rows: The current child store.
        released: Families whose hold the write released.

    Returns:
        The family summaries, ordered by family id.
    """
    families = _row_index(family_rows, "family_id")
    grouped = group_children_by_family(child_rows)
    summaries: list[ManualAssignmentFamilySummary] = []
    for family_id in sorted(set(family_ids), key=str):
        family = families.get(family_id)
        if family is None:
            continue
        children = _live_children(grouped, family_id)
        assigned = [
            child for child in children if child_sponsor(child) is not None
        ]
        summaries.append(
            ManualAssignmentFamilySummary(
                family_id=int(family_id or 0),
                assigned_sponsor_id=_int(family.get("assigned_sponsor_id")),
                assigned_sponsor_ids=sorted(
                    {
                        sponsor
                        for sponsor in (
                            child_sponsor(child) for child in assigned
                        )
                        if sponsor is not None
                    }
                ),
                assigned_count=len(assigned),
                family_size=len(children),
                assignment_state=assignment_state(children),
                hold_released=family_id in released,
            )
        )
    return summaries


def _sponsor_summary(
    sponsor_id: int,
    sponsor_rows: list[dict[str, Any]],
    child_rows: list[dict[str, Any]],
) -> ManualAssignmentSponsorSummary | None:
    """Build one sponsor's post-write summary.

    Args:
        sponsor_id: The sponsor id.
        sponsor_rows: The current sponsor store.
        child_rows: The current child store.

    Returns:
        The summary, or None when the sponsor row is absent.
    """
    row = _row_index(sponsor_rows, "sponsor_id").get(sponsor_id)
    if row is None:
        return None
    count = _sponsor_assigned_counts(child_rows).get(sponsor_id, 0)
    limit = _int(row.get("num_children"))
    return ManualAssignmentSponsorSummary(
        sponsor_id=sponsor_id,
        assigned_count=count,
        remaining=None if limit is None else max(limit - count, 0),
    )


def _versions(
    child_rows: Iterable[dict[str, Any]],
    family_rows: Iterable[dict[str, Any]],
    sponsor_rows: Iterable[dict[str, Any]],
) -> dict[str, int]:
    """Build the prefixed row-version map for the response.

    Args:
        child_rows: The child rows to report.
        family_rows: The family rows to report.
        sponsor_rows: The sponsor rows to report.

    Returns:
        The ``<entity>:<key>`` -> row version map.
    """
    versions: dict[str, int] = {}
    for child in child_rows:
        versions[f"child:{child.get('zite_record_id')}"] = (
            _int(child.get("_row_version")) or 0
        )
    for family in family_rows:
        versions[f"family:{family.get('family_id')}"] = (
            _int(family.get("_row_version")) or 0
        )
    for sponsor in sponsor_rows:
        versions[f"sponsor:{sponsor.get('sponsor_id')}"] = (
            _int(sponsor.get("_row_version")) or 0
        )
    return versions


def _audit_event(
    action: str,
    object_id: str,
    user_id: int | None,
    season: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    """Build one ``security_audit_events`` insert row.

    Args:
        action: The stable action name.
        object_id: The first affected child's key.
        user_id: The acting user's id.
        season: The season name.
        details: The JSON-safe detail payload.

    Returns:
        The insert kwargs.
    """
    return {
        "ts": datetime.now(UTC),
        "user_id": user_id,
        "action": action,
        "object_type": "child",
        "object_id": object_id,
        "season": season,
        "details": details,
    }


async def _guard_match_running(repo: SeasonStoreRepository) -> None:
    """Refuse a manual write while the automatic matcher is active.

    Args:
        repo: The season-bound repository.

    Raises:
        ManualMatchError: 409 ``match_running`` with the job context.
    """
    job = await repo.active_match_job()
    if job is not None:
        raise ManualMatchError(
            MATCH_RUNNING,
            f"automatic match job {job.get('job_id')} is {job.get('status')};"
            " manual changes are paused",
            job_id=job.get("job_id"),
            job_status=job.get("status"),
            started_at=job.get("started_at"),
        )


async def assign(
    repo: SeasonStoreRepository,
    cfg: Config,
    actor: str,
    user_id: int | None,
    request: ManualAssignmentRequest,
) -> ManualAssignmentResult:
    """Assign the requested selection to one sponsor.

    Validation happens against the live store, then the complete child,
    family and sponsor stores are rewritten in one transaction with the
    derived summaries and the security-audit row.

    Args:
        repo: The season-bound repository.
        cfg: Pipeline configuration.
        actor: The acting identity's username.
        user_id: The acting user's id (None for a job/system actor).
        request: The assign request.

    Returns:
        The outcome, with the new summaries and row versions.

    Raises:
        ManualMatchError: 404/409/412/422 per the blocking rules.
    """
    await _guard_match_running(repo)
    child_rows, family_rows, sponsor_rows = await _stores(repo)
    sponsor = _resolve_sponsor(sponsor_rows, request.sponsor_id)
    selected = _expand_selection(request, child_rows, family_rows)
    _require_versions(selected, request.sponsor_id, request.expected_versions)
    _check_expected_versions(
        request.expected_versions, child_rows, family_rows, sponsor_rows
    )

    current = _sponsor_assigned_counts(child_rows).get(request.sponsor_id, 0)
    limit = _int(sponsor.get("num_children"))
    moved = [
        child
        for child in selected
        if child_sponsor(child) != request.sponsor_id
    ]
    if limit is not None and current + len(moved) > limit:
        raise ManualMatchError(
            SPONSOR_FULL,
            f"sponsor {request.sponsor_id} is at {current} of {limit}"
            " children; the cap is hard",
            sponsor_id=request.sponsor_id,
            limit=limit,
            assigned_count=current,
        )

    configured = [
        child
        for child in selected
        if child_sponsor(child) != request.sponsor_id
    ]
    for child in configured:
        if child_sponsor(child) is not None and not request.confirm:
            raise ManualMatchError(
                CONFLICT,
                f"child {child.get('zite_record_id')!r} is already assigned to"
                f" sponsor {child_sponsor(child)}; pass confirm=True to move it",
                zite_record_id=child.get("zite_record_id"),
                sponsor_id=child_sponsor(child),
            )

    assigned: list[ManualAssignmentEntry] = []
    reassigned: list[ManualAssignmentEntry] = []
    unchanged: list[ManualAssignmentEntry] = []
    for child in selected:
        old = child_sponsor(child)
        if old == request.sponsor_id:
            unchanged.append(_entry(child))
        elif old is None:
            assigned.append(_entry(child))
        else:
            reassigned.append(_entry(child, old))

    family_ids: set[int] = {
        int(child["family_id"])
        for child in selected
        if child.get("family_id") is not None
    }
    holds = await repo.family_holds(family_ids)
    child_by_id = _row_index(child_rows, "zite_record_id")
    for child in configured:
        row = child_by_id.get(child.get("zite_record_id"))
        if row is not None:
            row["assigned_sponsor_id"] = request.sponsor_id
            row["assignment_source"] = SOURCE_MANUAL
    warnings = _assignment_warnings(
        selected, sponsor, family_rows, child_rows, cfg
    )
    apply_family_summary(family_rows, child_rows)
    apply_sponsor_summary(sponsor_rows, child_rows)

    details = {
        "actor": actor,
        "sponsor_id": request.sponsor_id,
        "family_ids": sorted(int(family_id) for family_id in family_ids),
        "angel_ids": sorted(
            (
                str(child.get("angel_id"))
                for child in selected
                if child.get("angel_id")
            ),
            key=_angel_sort_key,
        ),
        "assigned": sorted(
            (str(entry.angel_id) for entry in assigned if entry.angel_id),
            key=_angel_sort_key,
        ),
        "released": [],
        "reassigned_from": {
            str(entry.angel_id): entry.from_sponsor_id
            for entry in reassigned
            if entry.angel_id
        },
        "holds_released": sorted(int(family_id) for family_id in holds),
        "note": request.note,
        "warnings": warnings,
    }
    object_id = str(selected[0].get("zite_record_id") or "")
    await repo.commit_assignments(
        "manual_match",
        child_rows,
        family_rows,
        sponsor_rows,
        actor,
        request.expected_versions,
        [
            _audit_event(
                "manual_assignment", object_id, user_id, cfg.season, details
            )
        ],
        release_holds=list(holds),
    )

    sponsor_ids = {request.sponsor_id}
    sponsor_ids.update(
        entry.from_sponsor_id
        for entry in reassigned
        if entry.from_sponsor_id is not None
    )
    return await _result(
        repo,
        assigned,
        reassigned,
        [],
        unchanged,
        warnings,
        family_ids,
        set(holds),
        sorted(sponsor_ids),
    )


async def release(
    repo: SeasonStoreRepository,
    cfg: Config,
    actor: str,
    user_id: int | None,
    request: ManualReleaseRequest,
) -> ManualAssignmentResult:
    """Release the requested children and families' assignments.

    Args:
        repo: The season-bound repository.
        cfg: Pipeline configuration.
        actor: The acting identity's username.
        user_id: The acting user's id.
        request: The release request (``confirm`` is checked by the route).

    Returns:
        The outcome, with the new summaries and row versions.

    Raises:
        ManualMatchError: 404/409/412/422 per the blocking rules.
    """
    await _guard_match_running(repo)
    child_rows, family_rows, sponsor_rows = await _stores(repo)
    if len(set(request.child_ids)) != len(request.child_ids):
        raise ManualMatchError(
            INVALID_INPUT, "the request lists the same child twice"
        )
    families = _row_index(family_rows, "family_id")
    grouped = group_children_by_family(child_rows)
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for family_id in dict.fromkeys(request.family_ids):
        if family_id not in families:
            raise ManualMatchError(NOT_FOUND, f"unknown family {family_id}")
        for child in grouped.get(family_id, []):
            record_id = str(child.get("zite_record_id") or "")
            if child_sponsor(child) is not None and record_id not in seen:
                seen.add(record_id)
                selected.append(child)
    by_id = _row_index(child_rows, "zite_record_id")
    for record_id in request.child_ids:
        child = by_id.get(record_id)
        if child is None:
            raise ManualMatchError(NOT_FOUND, f"unknown child {record_id!r}")
        if record_id not in seen:
            seen.add(record_id)
            selected.append(child)
    if not selected:
        raise ManualMatchError(INVALID_INPUT, "the selection is empty")
    _check_expected_versions(
        request.expected_versions, child_rows, family_rows, sponsor_rows
    )

    released: list[ManualAssignmentEntry] = []
    family_ids: set[int] = set()
    old_sponsors: set[int] = set()
    for child in selected:
        old = child_sponsor(child)
        if old is None:
            continue
        released.append(_entry(child, old))
        old_sponsors.add(old)
        if child.get("family_id") is not None:
            family_ids.add(int(child["family_id"]))
        child["assigned_sponsor_id"] = None
        child["assignment_source"] = None
    apply_family_summary(family_rows, child_rows)
    apply_sponsor_summary(sponsor_rows, child_rows)
    details = {
        "actor": actor,
        "family_ids": sorted(int(family_id) for family_id in family_ids),
        "angel_ids": sorted(
            (str(entry.angel_id) for entry in released if entry.angel_id),
            key=_angel_sort_key,
        ),
        "assigned": [],
        "released": sorted(
            (str(entry.angel_id) for entry in released if entry.angel_id),
            key=_angel_sort_key,
        ),
        "reassigned_from": {},
        "note": request.note,
        "warnings": [],
    }
    object_id = str(selected[0].get("zite_record_id") or "")
    await repo.commit_assignments(
        "manual_match",
        child_rows,
        family_rows,
        sponsor_rows,
        actor,
        request.expected_versions,
        [
            _audit_event(
                "manual_release", object_id, user_id, cfg.season, details
            )
        ],
    )
    return await _result(
        repo,
        [],
        [],
        released,
        [],
        [],
        family_ids,
        set(),
        sorted(old_sponsors),
    )


async def _result(
    repo: SeasonStoreRepository,
    assigned: list[ManualAssignmentEntry],
    reassigned: list[ManualAssignmentEntry],
    released: list[ManualAssignmentEntry],
    unchanged: list[ManualAssignmentEntry],
    warnings: list[str],
    family_ids: set[Any],
    hold_released: set[Any],
    sponsor_ids: list[int],
) -> ManualAssignmentResult:
    """Re-read the stores and assemble the write response.

    Args:
        repo: The season-bound repository.
        assigned: Newly assigned children.
        reassigned: Moved children.
        released: Released children.
        unchanged: Idempotent no-ops.
        warnings: The advisories.
        family_ids: The affected families.
        hold_released: Families whose hold the write released.
        sponsor_ids: The sponsors to summarize.

    Returns:
        The response DTO.
    """
    child_rows, family_rows, sponsor_rows = await _stores(repo)
    touched = assigned + reassigned + released + unchanged
    touched_ids = {entry.zite_record_id for entry in touched}
    sponsors = [
        summary
        for sponsor_id in sorted(set(sponsor_ids))
        if (summary := _sponsor_summary(sponsor_id, sponsor_rows, child_rows))
        is not None
    ]
    return ManualAssignmentResult(
        assigned=assigned,
        reassigned=reassigned,
        released=released,
        unchanged=unchanged,
        warnings=warnings,
        families=_family_summaries(
            family_ids, family_rows, child_rows, hold_released
        ),
        sponsors=sponsors,
        versions=_versions(
            [
                row
                for row in child_rows
                if row.get("zite_record_id") in touched_ids
            ],
            [row for row in family_rows if row.get("family_id") in family_ids],
            [
                row
                for row in sponsor_rows
                if row.get("sponsor_id") in sponsor_ids
            ],
        ),
    )


# ---------------------------------------------------------------------------
# Read: the current assignment list.
# ---------------------------------------------------------------------------


async def assignments(
    repo: SeasonStoreRepository, cfg: Config, params: AssignmentParams
) -> ManualAssignmentListPage:
    """List the current assignments, derived from the children.

    Args:
        repo: The season-bound repository.
        cfg: Pipeline configuration (unused; kept for symmetry).
        params: The query filters.

    Returns:
        The assignment list page.
    """
    child_rows, _families, _sponsors = await _stores(repo)
    items: list[ManualAssignmentListItem] = []
    for child in child_rows:
        if as_bool(child.get("ignore")):
            continue
        sponsor_id = child_sponsor(child)
        if sponsor_id is None:
            continue
        source = _text(child.get("assignment_source"))
        if params.sponsor_id is not None and sponsor_id != params.sponsor_id:
            continue
        if params.source is not None and source != params.source:
            continue
        if (
            params.family_id is not None
            and child.get("family_id") != params.family_id
        ):
            continue
        items.append(
            ManualAssignmentListItem(
                zite_record_id=str(child.get("zite_record_id") or ""),
                angel_id=_text(child.get("angel_id")),
                family_id=_int(child.get("family_id")),
                sponsor_id=sponsor_id,
                assignment_source=source,
                last_updated_by=_text(child.get("_last_updated_by")),
                last_updated_at=_text(child.get("_last_updated_at")),
            )
        )
    items.sort(key=lambda item: item.zite_record_id)
    total = len(items)
    start = (params.page - 1) * params.page_size
    return ManualAssignmentListPage(
        items=items[start : start + params.page_size],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


# ---------------------------------------------------------------------------
# Holds.
# ---------------------------------------------------------------------------


def _manual_assignment_family_ids(
    child_rows: list[dict[str, Any]],
) -> set[Any]:
    """Return the families carrying a manual assignment.

    Args:
        child_rows: The complete child store.

    Returns:
        The family ids with at least one manually assigned child.
    """
    return {
        child.get("family_id")
        for child in child_rows
        if _text(child.get("assignment_source")) == SOURCE_MANUAL
        and child.get("family_id") is not None
    }


def _hold_result(
    family_id: int, hold: dict[str, Any] | None
) -> ManualHoldResult:
    """Build one hold result from a hold row.

    Args:
        family_id: The family id.
        hold: The hold row, or None.

    Returns:
        The result.
    """
    if hold is None:
        return ManualHoldResult(family_id=family_id, held=False)
    return ManualHoldResult(
        family_id=family_id,
        held=hold.get("released_at") is None,
        note=hold.get("note"),
        created_by=hold.get("created_by"),
        created_at=hold.get("created_at"),
        released_at=hold.get("released_at"),
        released_by=hold.get("released_by"),
    )


async def hold_family(
    repo: SeasonStoreRepository,
    cfg: Config,
    actor: str,
    user_id: int | None,
    request: ManualHoldRequest,
) -> ManualHoldResult:
    """Set a hold on a family, replacing any existing hold.

    Args:
        repo: The season-bound repository.
        cfg: Pipeline configuration (unused; kept for symmetry).
        actor: The acting identity's username.
        user_id: The acting user's id (unused; holds carry the actor).
        request: The hold request.

    Returns:
        The new hold.

    Raises:
        ManualMatchError: 409 ``match_running``; 422 blank note; 404
            unknown family; 409 ignored family or manual assignment.
    """
    await _guard_match_running(repo)
    if not request.note.strip():
        raise ManualMatchError(
            INVALID_INPUT, "a family hold requires a non-blank note"
        )
    child_rows, family_rows, _sponsors = await _stores(repo)
    family = _row_index(family_rows, "family_id").get(request.family_id)
    if family is None:
        raise ManualMatchError(NOT_FOUND, f"unknown family {request.family_id}")
    if (
        as_bool(family.get("ignore"))
        or str(family.get("status") or "") == "ignored"
    ):
        raise ManualMatchError(
            CONFLICT, f"family {request.family_id} is ignored"
        )
    if request.family_id in _manual_assignment_family_ids(child_rows):
        raise ManualMatchError(
            CONFLICT,
            "release the assignment before holding this family",
        )
    hold = await repo.record_family_hold(request.family_id, request.note, actor)
    return _hold_result(request.family_id, hold)


async def release_hold(
    repo: SeasonStoreRepository,
    cfg: Config,
    actor: str,
    request: ManualHoldReleaseRequest,
) -> ManualHoldResult:
    """Release a family's hold; a missing hold is a no-op success.

    Args:
        repo: The season-bound repository.
        cfg: Pipeline configuration (unused; kept for symmetry).
        actor: The acting identity's username.
        request: The release request.

    Returns:
        The released hold, or ``held=False`` when there was none.
    """
    hold = await repo.release_family_hold(request.family_id, actor)
    return _hold_result(request.family_id, hold)


async def list_holds(
    repo: SeasonStoreRepository, cfg: Config, params: HoldParams
) -> ManualHoldListPage:
    """List the active holds, with their notes.

    Args:
        repo: The season-bound repository.
        cfg: Pipeline configuration (unused; kept for symmetry).
        params: The paging values.

    Returns:
        The hold page.
    """
    family_ids = sorted(await repo.active_hold_family_ids())
    total = len(family_ids)
    start = (params.page - 1) * params.page_size
    page_ids = family_ids[start : start + params.page_size]
    holds = await repo.family_holds(page_ids)
    return ManualHoldListPage(
        items=[
            _hold_result(family_id, holds.get(family_id))
            for family_id in page_ids
        ],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


__all__ = [
    "AssignmentParams",
    "CONFLICT",
    "HoldParams",
    "INVALID_INPUT",
    "MATCH_RUNNING",
    "ManualMatchError",
    "NOT_FOUND",
    "PoolParams",
    "SPONSOR_FULL",
    "STALE_VERSION",
    "SponsorParams",
    "assign",
    "assignments",
    "hold_family",
    "list_holds",
    "pool",
    "release",
    "release_hold",
    "sponsors",
]
