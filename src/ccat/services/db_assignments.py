"""Child-grain assignment: the canonical link and the derived summaries.

Every downstream consumer used to resolve a child's sponsor by joining
the **family** row.  That cannot express "two of a family's four
children go to one sponsor and the other two to another", so the child
row is now the assignment grain:

- ``final_child.assigned_sponsor_id`` — the child's sponsor (NULL =
  unassigned).  Canonical.
- ``final_child.assignment_source`` — ``"auto"`` or ``"manual"``.  A
  manual assignment is protected from the automatic matcher.

The family and sponsor assignment columns are **derived summaries** of
the children.  Both writers — the automatic matcher and the manual
matcher — derive them through this module, so the two can never drift.

The summary rules (plan D2):

- Family: the assigned children (``U``) that share one sponsor give the
  family its ``assigned_sponsor_id``; a family split across two
  sponsors has none, while its ``assigned_angel_ids`` still names the
  children that are placed.
- Sponsor: ``assigned_angel_ids`` lists the assigned children's angel
  ids; ``num_angels_assigned`` counts the assigned **children**, which
  is the truthful number when two children share an angel letter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ccat.audit.store import (
    ANGEL_CLAIM_NOTE,
    STATUS_NEEDS_CLARIFICATION,
    STATUS_NEW,
    as_bool,
)
from ccat.domain import (
    PLACEHOLDER_SPONSOR_TYPE as _PLACEHOLDER_SPONSOR_TYPE,
    angel_sort_key,
    parse_prior_assigned,
)
from ccat.preparation import append_note

if TYPE_CHECKING:
    from collections.abc import Iterable

#: Who placed the child.
SOURCE_AUTO = "auto"
SOURCE_MANUAL = "manual"

#: The four family assignment states (plan D11).
STATE_UNASSIGNED = "unassigned"
STATE_PARTIAL = "partial"
STATE_SPLIT = "split"
STATE_ASSIGNED = "assigned"


def _is_placeholder(sponsor: dict[str, Any]) -> bool:
    """Report whether the sponsor is a generic store placeholder.

    Args:
        sponsor: One sponsor row.

    Returns:
        True when the sponsor's type is the placeholder affiliation.
    """
    return sponsor.get("sponsor_type") == _PLACEHOLDER_SPONSOR_TYPE


def _claim_takeover(
    claimants: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Resolve a contested claim by sponsor specificity.

    A generic store placeholder holds the angel id for the store; a
    specific sponsor claiming the same id takes the slot over
    (angel_claims_plan §1).  Two specific claimants (or placeholders
    only) stay a contest the Duplications section settles.

    Args:
        claimants: The sponsors claiming one angel id (two or more).

    Returns:
        The sponsor that takes the angel over, or None when the rule
        cannot settle the contest.
    """
    specific = [
        sponsor for sponsor in claimants if not _is_placeholder(sponsor)
    ]
    if len(specific) == 1:
        return specific[0]
    return None


def apply_angel_claims(
    sponsors: list[dict[str, Any]], children: list[dict[str, Any]]
) -> list[str]:
    """Honour the sponsors' Assigned Angel ID claims.

    A sponsor's ``prior_assigned_angel_ids`` names the children it
    claims.  A claim on a child that exists and that no other sponsor
    claims is placed on that child, so the claim is visible before the
    matcher runs.  A claim that names no child cannot be honoured at
    all, so the sponsor gets a clarification note and the audit stage
    fixes it.

    A contested claim is settled by specificity: one specific sponsor
    against generic store placeholders takes the child over, even from
    a placeholder the child is already placed with.  Two specific
    claimants (or placeholders only) stay a contest the Duplications
    section settles, so nothing is placed.

    A child that is already placed with a specific sponsor keeps its
    placement, and a manual placement is never overwritten: the claim
    is a first placement or a placeholder takeover, not a move.

    Args:
        sponsors: The final sponsor rows, updated in place.
        children: The final child rows, updated in place.

    Returns:
        The angel ids that were claimed but name no child.
    """
    claims: dict[str, list[dict[str, Any]]] = {}
    for sponsor in sponsors:
        if as_bool(sponsor.get("ignore")):
            continue
        raw = sponsor.get("prior_assigned_angel_ids")
        if not raw:
            continue
        for angel_id in parse_prior_assigned(str(raw)):
            claims.setdefault(angel_id, []).append(sponsor)
    sponsors_by_id = {
        sponsor.get("sponsor_id"): sponsor for sponsor in sponsors
    }
    children_by_angel = {
        str(child["angel_id"]): child
        for child in children
        if child.get("angel_id") and not as_bool(child.get("ignore"))
    }
    unusable: list[str] = []
    for angel_id, claimants in claims.items():
        if len(claimants) > 1:
            winner = _claim_takeover(claimants)
            if winner is None:
                # A contest the rule cannot settle: the Duplications
                # section decides who keeps the child, so nothing is
                # placed here.
                continue
            child = children_by_angel.get(angel_id)
            if child is None:
                continue
            if child.get("assignment_source") == SOURCE_MANUAL:
                continue
            holder = child_sponsor(child)
            holder_row = (
                sponsors_by_id.get(holder) if holder is not None else None
            )
            if holder_row is not None and not _is_placeholder(holder_row):
                # A specific sponsor keeps its child; only a
                # placeholder's hold is taken over.
                continue
            child["assigned_sponsor_id"] = winner.get("sponsor_id")
            child["assignment_source"] = SOURCE_AUTO
            continue
        sponsor = claimants[0]
        child = children_by_angel.get(angel_id)
        if child is None:
            unusable.append(angel_id)
            sponsor["clarification_notes"] = append_note(
                sponsor.get("clarification_notes"),
                ANGEL_CLAIM_NOTE.format(angel_id=angel_id),
            )
            if sponsor.get("status") == STATUS_NEW:
                sponsor["status"] = STATUS_NEEDS_CLARIFICATION
            continue
        if child_sponsor(child) is not None:
            continue
        child["assigned_sponsor_id"] = sponsor.get("sponsor_id")
        child["assignment_source"] = SOURCE_AUTO
    return sorted(unusable)


def child_sponsor(child: dict[str, Any]) -> int | None:
    """Return the child's own sponsor id.

    The resolution rule is the child's own column.  It never falls back
    to the family column: the family column is derived *from* the child,
    so a fallback would be circular.

    Args:
        child: One final child row.

    Returns:
        The sponsor id, or None when the child is unassigned.
    """
    value = child.get("assigned_sponsor_id")
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def group_children_by_family(
    children: Iterable[dict[str, Any]],
) -> dict[Any, list[dict[str, Any]]]:
    """Group the non-ignored children by their family.

    Args:
        children: The final child rows.

    Returns:
        Mapping of family id -> the family's non-ignored children.
    """
    grouped: dict[Any, list[dict[str, Any]]] = {}
    for child in children:
        if as_bool(child.get("ignore")):
            continue
        family_id = child.get("family_id")
        if family_id is None:
            continue
        grouped.setdefault(family_id, []).append(child)
    return grouped


def apply_family_summary(
    families: list[dict[str, Any]], children: list[dict[str, Any]]
) -> None:
    """Set every family's assignment columns from its children.

    Args:
        families: The final family rows, updated in place.
        children: The complete final child store.
    """
    grouped = group_children_by_family(children)
    for family in families:
        family_children = grouped.get(family.get("family_id"), [])
        assigned = [
            child
            for child in family_children
            if child_sponsor(child) is not None
        ]
        sponsors = {child_sponsor(child) for child in assigned}
        family["assigned_sponsor_id"] = (
            next(iter(sponsors)) if len(sponsors) == 1 else None
        )
        angel_ids = sorted(
            (
                str(child["angel_id"])
                for child in assigned
                if child.get("angel_id")
            ),
            key=angel_sort_key,
        )
        family["assigned_angel_ids"] = (
            ",".join(angel_ids) if angel_ids else None
        )
        family["num_angels_in_family"] = _angel_count(family_children)


def apply_sponsor_summary(
    sponsors: list[dict[str, Any]], children: list[dict[str, Any]]
) -> None:
    """Set every sponsor's assignment columns from the children.

    Args:
        sponsors: The final sponsor rows, updated in place.
        children: The complete final child store.
    """
    grouped: dict[int, list[dict[str, Any]]] = {}
    for child in children:
        if as_bool(child.get("ignore")):
            continue
        sponsor_id = child_sponsor(child)
        if sponsor_id is None:
            continue
        grouped.setdefault(sponsor_id, []).append(child)
    for sponsor in sponsors:
        assigned = grouped.get(sponsor.get("sponsor_id"), [])
        angel_ids = sorted(
            (
                str(child["angel_id"])
                for child in assigned
                if child.get("angel_id")
            ),
            key=angel_sort_key,
        )
        sponsor["assigned_angel_ids"] = (
            ",".join(angel_ids) if angel_ids else None
        )
        sponsor["num_angels_assigned"] = len(assigned)


def assignment_state(children: list[dict[str, Any]]) -> str:
    """Classify one family's assignment state (plan D11).

    Args:
        children: The family's non-ignored children.

    Returns:
        ``"unassigned"``, ``"partial"``, ``"split"``, or ``"assigned"``.
    """
    assigned = [child for child in children if child_sponsor(child) is not None]
    if not assigned:
        return STATE_UNASSIGNED
    sponsors = {child_sponsor(child) for child in assigned}
    if len(sponsors) > 1:
        return STATE_SPLIT
    if len(assigned) == len(children):
        return STATE_ASSIGNED
    return STATE_PARTIAL


def assignable_children(
    family_rows: list[dict[str, Any]], child_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return the children that may be assigned to a sponsor.

    A child is assignable when it belongs to a live family, is not
    ignored, has an angel letter (the packet builder needs it), and is
    not in an unresolved duplicate group (its letter is not unique yet,
    so the assignment would be ambiguous).

    Args:
        family_rows: The final family rows.
        child_rows: The final child rows.

    Returns:
        The assignable children.
    """
    families = {row.get("family_id"): row for row in family_rows}
    assignable: list[dict[str, Any]] = []
    for child in child_rows:
        if as_bool(child.get("ignore")):
            continue
        family_id = child.get("family_id")
        if family_id is None:
            continue
        family = families.get(family_id)
        if family is None or as_bool(family.get("ignore")):
            continue
        if as_bool(child.get("potential_duplicate")):
            continue
        if not child.get("angel_id"):
            continue
        assignable.append(child)
    return assignable


def _angel_count(children: list[dict[str, Any]]) -> int:
    """Count a family's angels: distinct letters, letterless rows alone.

    A child that is still a pending duplicate shares its group's angel
    letter, so a family's angel count is the number of distinct
    letters; a child without a letter counts as one angel of its own.

    Args:
        children: The family's non-ignored children.

    Returns:
        The number of angels.
    """
    letters: set[str] = set()
    for child in children:
        letter = child.get("angel_id")
        letters.add(
            str(letter) if letter else f"row:{child.get('zite_record_id')}"
        )
    return len(letters)


def backfill_child_assignments(
    families: list[dict[str, Any]],
    sponsors: list[dict[str, Any]],
    children: list[dict[str, Any]],
) -> None:
    """Reconstruct the child assignments from the summary columns.

    A workbook written before the child-grain change has no child
    assignment column; the family and sponsor summary columns still
    carry the placement.  This rebuilds the child-grain truth from
    them, so an imported workbook keeps its assignments instead of
    importing as a season with no placements.

    The family column is the authority: a family names the children it
    placed through its ``assigned_angel_ids`` list, and a list shorter
    than the family means a partial placement — only the named children
    are placed.  A sponsor's list then names the children it holds that
    no family claimed.

    Only children with no sponsor are filled, so the operation is
    idempotent and never overrides a placement the child already
    carries.  The Alembic revision ``20261121_0006_manual_assignment``
    applies the same rule to an existing database.

    Args:
        families: The final family rows.
        sponsors: The final sponsor rows.
        children: The final child rows, updated in place.
    """
    grouped = group_children_by_family(children)
    assigned: set[str] = set()
    for family in families:
        sponsor_id = family.get("assigned_sponsor_id")
        if sponsor_id is None:
            continue
        family_children = grouped.get(family.get("family_id"), [])
        named = {
            token.strip().upper()
            for token in str(family.get("assigned_angel_ids") or "").split(",")
            if token.strip()
        }
        targets = (
            [
                child
                for child in family_children
                if str(child.get("angel_id") or "").upper() in named
            ]
            if named
            else list(family_children)
        )
        for child in targets:
            key = str(child.get("zite_record_id"))
            if child.get("assigned_sponsor_id") is not None:
                assigned.add(key)
                continue
            child["assigned_sponsor_id"] = int(sponsor_id)
            child["assignment_source"] = SOURCE_AUTO
            assigned.add(key)
    for sponsor in sponsors:
        named = {
            token.strip().upper()
            for token in str(sponsor.get("assigned_angel_ids") or "").split(",")
            if token.strip()
        }
        if not named:
            continue
        sponsor_id = sponsor.get("sponsor_id")
        if sponsor_id is None:
            continue
        for child in children:
            if as_bool(child.get("ignore")):
                continue
            if str(child.get("angel_id") or "").upper() not in named:
                continue
            if child.get("assigned_sponsor_id") is not None:
                continue
            child["assigned_sponsor_id"] = int(sponsor_id)
            child["assignment_source"] = SOURCE_AUTO


__all__ = [
    "SOURCE_AUTO",
    "SOURCE_MANUAL",
    "STATE_ASSIGNED",
    "STATE_PARTIAL",
    "STATE_SPLIT",
    "STATE_UNASSIGNED",
    "apply_family_summary",
    "apply_sponsor_summary",
    "assignable_children",
    "assignment_state",
    "backfill_child_assignments",
    "child_sponsor",
    "group_children_by_family",
]
