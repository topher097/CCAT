"""Duplication-audit service: list, inspect, and resolve duplicates.

The Duplications section of the audit tool works on the duplicate groups
the last screening run detected (``duplicate_log``) and lets a human
resolve every member of a group:

- ``not_duplicate`` -- the member is a unique entity.  It receives a
  unique id of its own (an angel letter for a child, a new family id
  for a family submission, the sponsor id it already owns for a
  sponsor) and its duplicate flag is cleared.
- ``ignore`` -- the member is ignored, exactly like the entity ignore
  action in the audit tool.

Every decision is appended to the ``duplication_audit`` table, so what
was decided and by whom survives.

While a child duplicate group is still pending, every member carries
the group's single angel letter: the duplicate is one angel until the
audit says otherwise, so the family's angel count and the generated
sheets stay correct.  A member marked "not a duplicate" leaves the
group and receives a letter of its own.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ccat.audit import AuditError
from ccat.audit.store import (
    ENTITY_SPECS,
    STATUS_AUDITED,
    _as_int,
    _as_text,
    _default_row,
    _raw_columns,
    as_bool,
)
from ccat.domain import index_to_letters
from ccat.match import _children_per_family
from ccat.services import db_audit
from ccat.services.audit import SavePatch

if TYPE_CHECKING:
    from ccat.config import Config
    from ccat.db.repositories import SeasonStoreRepository

_ENTITIES = ("child", "family", "sponsor")

#: Duplicate-log sheet label -> entity name.
_SHEET_ENTITY = {
    "child_submissions": "child",
    "family_submissions": "family",
    "sponsor_submissions": "sponsor",
}

#: The member is a unique entity and receives an id of its own.
DECISION_NOT_DUPLICATE = "not_duplicate"
#: The member is ignored, exactly like the audit tool's ignore action.
DECISION_IGNORE = "ignore"

_DECISIONS = (DECISION_NOT_DUPLICATE, DECISION_IGNORE)


def group_id(sheet: str, group_key: str) -> str:
    """Return one duplicate group's stable, URL-safe id.

    The id is derived from the group's identity, so it survives a new
    screening run that redetects the same group.

    Args:
        sheet: The duplicate-log sheet label.
        group_key: The duplicate group's key.

    Returns:
        A 16-character hex id.
    """
    digest = hashlib.sha256(f"{sheet}\x00{group_key}".encode()).hexdigest()
    return digest[:16]


@dataclass(frozen=True)
class _Group:
    """One detected duplicate group."""

    sheet: str
    entity: str
    group_key: str
    members: tuple[dict[str, Any], ...]

    @property
    def id(self) -> str:
        """The group's stable, URL-safe id."""
        return group_id(self.sheet, self.group_key)


async def list_groups(repo: SeasonStoreRepository) -> list[dict[str, Any]]:
    """List every duplicate group the last screening run detected.

    Args:
        repo: The season-bound repository.

    Returns:
        One summary per group, ordered by sheet and group key.
    """
    groups = await _groups(repo)
    decisions = await _decisions(repo)
    states = await _states(repo)
    summaries: list[dict[str, Any]] = []
    for group in groups:
        decided = sum(
            1
            for member in group.members
            if _decision_for(decisions, group, member) is not None
        )
        survivor = next(
            (m for m in group.members if as_bool(m.get("survivor"))), None
        )
        summaries.append(
            {
                "id": group.id,
                "sheet": group.sheet,
                "entity": group.entity,
                "group_key": group.group_key,
                "member_count": len(group.members),
                "decided_count": decided,
                "status": _group_status(group, decisions, states),
                "survivor_record_id": str(
                    (survivor or {}).get("zite_record_id") or ""
                ),
                "conflict_columns": _conflict_columns(group),
                "created": _group_created(group),
            }
        )
    return summaries


def filter_groups(
    groups: list[dict[str, Any]], q: str = "", entity: str | None = None
) -> list[dict[str, Any]]:
    """Return the groups the duplication list shows.

    The list and the neighbour lookup share this filter, so "next" and
    "previous" always mean the adjacent row of the table the user is
    looking at.

    Args:
        groups: Every group summary, in the list's order.
        q: The list's search query.
        entity: Restrict to "child", "family", or "sponsor".

    Returns:
        The filtered groups, in the list's order.
    """
    needle = q.strip().lower()
    return [
        group
        for group in groups
        if (entity is None or group["entity"] == entity)
        and (
            not needle
            or needle in str(group["group_key"]).lower()
            or needle in str(group["survivor_record_id"]).lower()
        )
    ]


async def pending_groups_by_row(
    repo: SeasonStoreRepository,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Map each audit row key to the duplicate group that still needs audit.

    Args:
        repo: The season-bound repository.

    Returns:
        ``{entity: {row_key: {"id": ..., "group_key": ..., "status": ...}}}``
        for every group whose status is not ``audited``.  The row key is
        the entity's own audit key (``zite_record_id`` for children and
        sponsors, ``family_id`` for families).  A member with no final
        row contributes nothing.
    """
    groups = await _groups(repo)
    decisions = await _decisions(repo)
    states = await _states(repo)
    finals = await _final_rows(repo)
    keys = {entity: ENTITY_SPECS[entity]["key"] for entity in _ENTITIES}
    pending: dict[str, dict[str, dict[str, Any]]] = {
        entity: {} for entity in _ENTITIES
    }
    for group in groups:
        status = _group_status(group, decisions, states)
        if status == STATUS_AUDITED:
            continue
        family_row = _group_family_row(group, finals["family"])
        entry = {
            "id": group.id,
            "group_key": group.group_key,
            "status": status,
        }
        key_column = keys[group.entity]
        for member in group.members:
            row = _member_row(group, member, finals, family_row)
            if row is None:
                continue
            pending[group.entity][str(row.get(key_column))] = entry
    return pending


async def status_counts(repo: SeasonStoreRepository) -> dict[str, int]:
    """Count the duplicate groups per status.

    Args:
        repo: The season-bound repository.

    Returns:
        ``{"pending": n, "resolved": n, "audited": n}``; every key is
        present, zero when no group has that status.
    """
    counts = {"pending": 0, "resolved": 0, "audited": 0}
    for group in await list_groups(repo):
        status = str(group["status"])
        counts[status] = counts.get(status, 0) + 1
    return counts


async def group_neighbours(
    repo: SeasonStoreRepository,
    wanted_id: str,
    q: str = "",
    entity: str | None = None,
) -> tuple[dict[str, str] | None, dict[str, str] | None]:
    """Return the groups before and after one group in the list's order.

    Args:
        repo: The season-bound repository.
        wanted_id: The group's id.
        q: The list's search query.
        entity: Restrict to "child", "family", or "sponsor".

    Returns:
        A (previous, next) pair of ``{"key": ..., "label": ...}`` dicts;
        each side is None when the group sits at the list's edge.
    """
    groups = filter_groups(await list_groups(repo), q, entity)
    index = next(
        (
            position
            for position, group in enumerate(groups)
            if group["id"] == wanted_id
        ),
        None,
    )
    if index is None:
        return None, None
    return (
        _group_neighbour(groups[index - 1]) if index > 0 else None,
        _group_neighbour(groups[index + 1])
        if index + 1 < len(groups)
        else None,
    )


def _group_neighbour(group: dict[str, Any]) -> dict[str, str]:
    """Render one neighbouring group for the wire.

    Args:
        group: The neighbouring group summary.

    Returns:
        The neighbour's id and label.
    """
    return {"key": str(group["id"]), "label": str(group["group_key"])}


async def group_detail(
    repo: SeasonStoreRepository, wanted_id: str
) -> dict[str, Any] | None:
    """Return one duplicate group's raw and screened results.

    Args:
        repo: The season-bound repository.
        wanted_id: The group's id.

    Returns:
        The group detail, or None when no group carries that id.
    """
    group = next((g for g in await _groups(repo) if g.id == wanted_id), None)
    if group is None:
        return None
    finals = await _final_rows(repo)
    decisions = await _decisions(repo)
    family_row = _group_family_row(group, finals["family"])
    members: list[dict[str, Any]] = []
    for member in group.members:
        record_id = str(member.get("zite_record_id") or "")
        screened = _member_row(group, member, finals, family_row)
        decision = _decision_for(decisions, group, member)
        members.append(
            {
                "zite_record_id": record_id,
                "created": member.get("created"),
                "survivor": as_bool(member.get("survivor")),
                "conflict_columns": [
                    column
                    for column in str(
                        member.get("conflict_columns") or ""
                    ).split(",")
                    if column
                ],
                "conflict_details": member.get("conflict_details"),
                "raw": _raw_record(member),
                "screened": screened,
                "decision": decision["decision"] if decision else None,
                "assigned_id": decision["assigned_id"] if decision else None,
                "decided_by": decision["actor"] if decision else None,
                "decided_at": decision["created_at"] if decision else None,
                "row_version": (
                    screened.get("_row_version") if screened else None
                ),
            }
        )
    states = await _states(repo)
    state = states.get((group.sheet, group.group_key))
    return {
        "id": group.id,
        "sheet": group.sheet,
        "entity": group.entity,
        "group_key": group.group_key,
        "status": _group_status(group, decisions, states),
        "audited_at": (state or {}).get("audited_at"),
        "members": members,
    }


async def decide(
    repo: SeasonStoreRepository,
    cfg: Config,
    wanted_id: str,
    record_id: str,
    decision: str,
    actor: str,
    *,
    confirm: bool,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Apply one duplication-audit decision to one group member.

    Args:
        repo: The season-bound repository.
        cfg: Pipeline configuration.
        wanted_id: The group's id.
        record_id: The audited group member.
        decision: ``not_duplicate`` or ``ignore``.
        actor: Requesting user identity.
        confirm: Must be True; anything else refuses the action.
        expected_version: The caller's expected ``row_version``.

    Returns:
        The updated group detail.

    Raises:
        AuditError: Without confirmation, for an unknown decision, an
            unknown group or member, or an unusable member.
    """
    if not confirm:
        raise AuditError("a duplication decision requires confirm=True")
    if decision not in _DECISIONS:
        raise AuditError(f"unknown duplication decision {decision!r}")
    groups = await _groups(repo)
    group = next((g for g in groups if g.id == wanted_id), None)
    if group is None:
        raise AuditError(f"unknown duplication group {wanted_id!r}")
    member = next(
        (
            item
            for item in group.members
            if str(item.get("zite_record_id") or "") == str(record_id)
        ),
        None,
    )
    if member is None:
        raise AuditError(
            f"unknown duplication member {record_id!r} in group {wanted_id!r}"
        )
    standing = _decision_for(await _decisions(repo), group, member)
    if standing is not None and standing["decision"] == decision:
        # The same decision already stands: a retry after a lost response
        # changes nothing and adds no trail row.
        detail = await group_detail(repo, wanted_id)
        assert detail is not None
        return detail
    if decision == DECISION_IGNORE:
        await _ignore_member(repo, cfg, group, member, actor, expected_version)
        assigned_id = None
    else:
        assigned_id = await _release_member(
            repo, cfg, group, member, actor, expected_version
        )
    await repo.record_duplication_audit(
        group.sheet,
        group.group_key,
        str(member.get("zite_record_id") or ""),
        decision,
        actor,
        assigned_id,
    )
    detail = await group_detail(repo, wanted_id)
    assert detail is not None
    return detail


async def mark_audited(
    repo: SeasonStoreRepository,
    cfg: Config,
    wanted_id: str,
    actor: str,
) -> dict[str, Any]:
    """Mark a resolved duplication audited and reconcile its final rows.

    The group's members become audited (``status`` and ``audited_at``)
    and the affected family's and sponsor's angel columns are
    re-derived, so the ``final_*`` tables agree with the adjudication.
    The recorded group state stays the flag that says so.

    Args:
        repo: The season-bound repository.
        cfg: Pipeline configuration.
        wanted_id: The group's id.
        actor: Requesting user identity.

    Returns:
        The updated group detail.

    Raises:
        AuditError: For an unknown group, a group with an undecided
            member, or a member the audit refuses to audit.
    """
    groups = await _groups(repo)
    group = next((g for g in groups if g.id == wanted_id), None)
    if group is None:
        raise AuditError(f"unknown duplication group {wanted_id!r}")
    decisions = await _decisions(repo)
    undecided = [
        member
        for member in group.members
        if _decision_for(decisions, group, member) is None
    ]
    if undecided:
        raise AuditError(
            "the duplication still has undecided members; resolve every"
            " member before marking it audited"
        )
    finals = await _final_rows(repo)
    family_row = _group_family_row(group, finals["family"])
    key_column = ENTITY_SPECS[group.entity]["key"]
    affected: list[int | None] = []
    for member in group.members:
        row = _member_row(group, member, finals, family_row)
        if row is None:
            continue
        if as_bool(row.get("ignore")):
            # An ignored row stays ignored; the audit save would only
            # reseed it.
            continue
        await db_audit.save_entity_db(
            repo,
            cfg,
            group.entity,
            row[key_column],
            SavePatch(status=STATUS_AUDITED),
            actor,
            expected_version=row.get("_row_version"),
        )
        affected.append(_as_int(row.get("family_id")))
    await _refresh_angel_columns(repo, actor, affected)
    await repo.record_duplication_state(group.sheet, group.group_key, actor)
    detail = await group_detail(repo, wanted_id)
    assert detail is not None
    return detail


async def sync_pending_angel_letters(
    repo: SeasonStoreRepository, actor: str
) -> int:
    """Give every pending duplicate child its group's single angel letter.

    A duplicate group is one angel until the audit says otherwise, so
    every member of a pending group carries the same letter.  Members
    the audit already resolved keep their own letter.

    Args:
        repo: The season-bound repository.
        actor: The acting identity.

    Returns:
        The number of children whose letter changed.
    """
    groups = await _groups(repo)
    decisions = await _decisions(repo)
    child_rows = await repo.final_rows("child")
    by_record = {
        str(row.get("zite_record_id") or ""): row for row in child_rows
    }
    changed = 0
    for group in groups:
        if group.entity != "child":
            continue
        pending = [
            member
            for member in group.members
            if _decision_for(decisions, group, member) is None
        ]
        if not pending:
            continue
        rows = [
            by_record[str(member.get("zite_record_id") or "")]
            for member in pending
        ]
        rows = [row for row in rows if row is not None]
        if not rows:
            continue
        family_id = next(
            (
                _as_int(row.get("family_id"))
                for row in rows
                if row.get("family_id")
            ),
            None,
        )
        if family_id is None:
            continue
        letter = _group_letter(rows, family_id, child_rows)
        if letter is None:
            continue
        for row in rows:
            if row.get("angel_id") != letter:
                row["angel_id"] = letter
                changed += 1
    if changed:
        await repo.commit_finals("duplication", "child", child_rows, actor)
    return changed


# ---------------------------------------------------------------------------
# Decision helpers.
# ---------------------------------------------------------------------------


async def _ignore_member(
    repo: SeasonStoreRepository,
    cfg: Config,
    group: _Group,
    member: dict[str, Any],
    actor: str,
    expected_version: int | None,
) -> None:
    """Ignore one group member through the ordinary ignore action.

    Args:
        repo: The season-bound repository.
        cfg: Pipeline configuration.
        group: The member's duplicate group.
        member: The audited group member.
        actor: Requesting user identity.
        expected_version: The caller's expected ``row_version``.

    Raises:
        AuditError: When the member has no stored row to ignore.
    """
    finals = await _final_rows(repo)
    family_row = _group_family_row(group, finals["family"])
    row = _member_row(group, member, finals, family_row)
    if row is None:
        raise AuditError(
            f"no stored row to ignore for {member.get('zite_record_id')!r}"
        )
    key = ENTITY_SPECS[group.entity]["key"]
    await db_audit.ignore_entity_db(
        repo,
        cfg,
        group.entity,
        row[key],
        actor,
        confirm=True,
        expected_version=expected_version,
    )


async def _release_member(
    repo: SeasonStoreRepository,
    cfg: Config,
    group: _Group,
    member: dict[str, Any],
    actor: str,
    expected_version: int | None,
) -> str | None:
    """Give one group member a unique id of its own.

    Args:
        repo: The season-bound repository.
        cfg: Pipeline configuration.
        group: The member's duplicate group.
        member: The audited group member.
        actor: Requesting user identity.
        expected_version: The caller's expected ``row_version``.

    Returns:
        The unique id the member received, when it received one.

    Raises:
        AuditError: When the member has no stored row to release.
    """
    if group.entity == "child":
        return await _release_child(
            repo, group, member, actor, expected_version
        )
    if group.entity == "sponsor":
        return await _release_sponsor(repo, member, actor, expected_version)
    return await _release_family(
        repo, cfg, group, member, actor, expected_version
    )


async def _release_child(
    repo: SeasonStoreRepository,
    group: _Group,
    member: dict[str, Any],
    actor: str,
    expected_version: int | None,
) -> str:
    """Give a duplicate child its own angel letter.

    Args:
        repo: The season-bound repository.
        group: The member's duplicate group.
        member: The audited group member.
        actor: Requesting user identity.
        expected_version: The caller's expected ``row_version``.

    Returns:
        The new angel letter.

    Raises:
        AuditError: When the child has no stored row or no family.
    """
    child_rows = await repo.final_rows("child")
    record_id = str(member.get("zite_record_id") or "")
    row = next(
        (
            item
            for item in child_rows
            if str(item.get("zite_record_id") or "") == record_id
        ),
        None,
    )
    if row is None:
        raise AuditError(f"no stored row for child {record_id!r}")
    family_id = _as_int(row.get("family_id"))
    if family_id is None:
        raise AuditError(
            f"child {record_id!r} has no family; run the audit to attach it"
            " before resolving its duplication"
        )
    letter = _free_letter(
        family_id,
        _used_letters(family_id, child_rows, exclude=record_id),
        avoid=_as_text(row.get("angel_id")),
    )
    row["angel_id"] = letter
    row["potential_duplicate"] = False
    await repo.commit_finals(
        "duplication",
        "child",
        child_rows,
        actor,
        expected_versions=(
            {record_id: expected_version}
            if expected_version is not None
            else None
        ),
    )
    await _refresh_angel_columns(repo, actor, [family_id])
    return letter


async def _release_sponsor(
    repo: SeasonStoreRepository,
    member: dict[str, Any],
    actor: str,
    expected_version: int | None,
) -> str:
    """Release a duplicate sponsor submission as its own sponsor.

    Every sponsor submission already owns a unique ``sponsor_id``, so
    only the duplicate flag changes.

    Args:
        repo: The season-bound repository.
        member: The audited group member.
        actor: Requesting user identity.
        expected_version: The caller's expected ``row_version``.

    Returns:
        The sponsor id the member owns.

    Raises:
        AuditError: When the member has no stored row.
    """
    sponsor_rows = await repo.final_rows("sponsor")
    record_id = str(member.get("zite_record_id") or "")
    row = next(
        (
            item
            for item in sponsor_rows
            if str(item.get("zite_record_id") or "") == record_id
        ),
        None,
    )
    if row is None:
        raise AuditError(f"no stored row for sponsor {record_id!r}")
    row["potential_duplicate"] = False
    await repo.commit_finals(
        "duplication",
        "sponsor",
        sponsor_rows,
        actor,
        expected_versions=(
            {str(row["sponsor_id"]): expected_version}
            if expected_version is not None
            else None
        ),
    )
    return str(row["sponsor_id"])


async def _release_family(
    repo: SeasonStoreRepository,
    cfg: Config,
    group: _Group,
    member: dict[str, Any],
    actor: str,
    expected_version: int | None,
) -> str:
    """Release a duplicate family submission as its own family.

    The group's submissions share one ``family_id``, so a released
    submission becomes a new family row with a new id, seeded from its
    own raw record.  The group's children stay with the original
    family: their email matches both registrations, so no child can be
    attributed to the new one without a human decision.

    Args:
        repo: The season-bound repository.
        cfg: Pipeline configuration.
        group: The member's duplicate group.
        member: The audited group member.
        actor: Requesting user identity.
        expected_version: The caller's expected ``row_version``.

    Returns:
        The new family id.

    Raises:
        AuditError: When the group has no stored family row.
    """
    family_rows = await repo.final_rows("family")
    existing = _group_family_row(group, family_rows)
    if existing is None:
        raise AuditError(
            f"no stored family row for duplication group {group.group_key!r}"
        )
    new_family_id = (
        max(
            (_as_int(row.get("family_id")) or 0 for row in family_rows),
            default=0,
        )
        + 1
    )
    raw = _raw_record(member)
    row = _default_row("family", new_family_id)
    for column in _raw_columns("family"):
        row[column] = raw.get(column)
    row["zite_record_id"] = str(member.get("zite_record_id") or "")
    row["potential_duplicate"] = False
    family_rows.append(row)
    await repo.commit_finals(
        "duplication",
        "family",
        family_rows,
        actor,
        expected_versions=(
            {str(existing["family_id"]): expected_version}
            if expected_version is not None
            else None
        ),
    )
    await _refresh_angel_columns(repo, actor, [_as_int(existing["family_id"])])
    return str(new_family_id)


async def _refresh_angel_columns(
    repo: SeasonStoreRepository,
    actor: str,
    family_ids: list[int | None],
) -> None:
    """Re-derive the angel columns a duplication decision changed.

    The family's assigned angels and angel count, and the assigned
    sponsor's angel list, follow the children that carry a letter.

    Args:
        repo: The season-bound repository.
        actor: The acting identity.
        family_ids: The affected families.
    """
    wanted = {family_id for family_id in family_ids if family_id is not None}
    if not wanted:
        return
    family_rows = await repo.final_rows("family")
    sponsor_rows = await repo.final_rows("sponsor")
    child_rows = await repo.final_rows("child")
    sponsors: set[int] = set()
    for row in family_rows:
        family_id = _as_int(row.get("family_id"))
        if family_id not in wanted:
            continue
        letters = _family_letters(family_id, child_rows)
        row["assigned_angel_ids"] = ",".join(letters) if letters else None
        row["num_angels_in_family"] = _children_per_family(child_rows).get(
            family_id, 0
        )
        sponsor_id = _as_int(row.get("assigned_sponsor_id"))
        if sponsor_id is not None:
            sponsors.add(sponsor_id)
    if sponsors:
        for row in sponsor_rows:
            sponsor_id = _as_int(row.get("sponsor_id"))
            if sponsor_id not in sponsors:
                continue
            letters: list[str] = []
            for family in family_rows:
                if _as_int(family.get("assigned_sponsor_id")) != sponsor_id:
                    continue
                family_id = _as_int(family.get("family_id"))
                if family_id is None:
                    continue
                for letter in _family_letters(family_id, child_rows):
                    if letter not in letters:
                        letters.append(letter)
            letters.sort(key=_angel_sort_key)
            row["assigned_angel_ids"] = ",".join(letters) if letters else None
            row["num_angels_assigned"] = len(letters)
    await repo.commit_finals("duplication", "family", family_rows, actor)
    await repo.commit_finals("duplication", "sponsor", sponsor_rows, actor)


# ---------------------------------------------------------------------------
# Group and row helpers.
# ---------------------------------------------------------------------------


async def _groups(repo: SeasonStoreRepository) -> list[_Group]:
    """Group the latest screening run's duplicate-log rows.

    Args:
        repo: The season-bound repository.

    Returns:
        The duplicate groups, ordered by sheet and group key.
    """
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in await repo.duplicate_log_rows():
        sheet = str(row.get("sheet") or "")
        key = str(row.get("group_key") or "")
        if sheet not in _SHEET_ENTITY or not key:
            continue
        grouped.setdefault((sheet, key), []).append(row)
    groups: list[_Group] = []
    for (sheet, key), members in grouped.items():
        members.sort(
            key=lambda item: (
                str(item.get("created") or ""),
                str(item.get("zite_record_id") or ""),
            )
        )
        groups.append(
            _Group(
                sheet=sheet,
                entity=_SHEET_ENTITY[sheet],
                group_key=key,
                members=tuple(members),
            )
        )
    groups.sort(key=lambda group: (group.sheet, group.group_key))
    return groups


async def _decisions(
    repo: SeasonStoreRepository,
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Return the latest decision per group member.

    Args:
        repo: The season-bound repository.

    Returns:
        ``(sheet, group_key, record id) -> decision row``.
    """
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in await repo.duplication_audit_rows():
        key = (
            str(row.get("sheet") or ""),
            str(row.get("group_key") or ""),
            str(row.get("zite_record_id") or ""),
        )
        latest[key] = row
    return latest


async def _states(
    repo: SeasonStoreRepository,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Return each duplicate group's recorded state.

    Args:
        repo: The season-bound repository.

    Returns:
        ``(sheet, group_key) -> state row``.
    """
    return {
        (str(row.get("sheet") or ""), str(row.get("group_key") or "")): row
        for row in await repo.duplication_state_rows()
    }


def _group_status(
    group: _Group,
    decisions: dict[tuple[str, str, str], dict[str, Any]],
    states: dict[tuple[str, str], dict[str, Any]],
) -> str:
    """Return one group's state: audited, resolved, or pending.

    Args:
        group: The duplicate group.
        decisions: The member decisions.
        states: The recorded group states.

    Returns:
        The group's state.
    """
    state = states.get((group.sheet, group.group_key))
    if state is not None:
        return str(state.get("status") or "audited")
    decided = sum(
        1
        for member in group.members
        if _decision_for(decisions, group, member) is not None
    )
    return "resolved" if decided == len(group.members) else "pending"


def _decision_for(
    decisions: dict[tuple[str, str, str], dict[str, Any]],
    group: _Group,
    member: dict[str, Any],
) -> dict[str, Any] | None:
    """Return one member's latest decision, when it has one."""
    key = (
        group.sheet,
        group.group_key,
        str(member.get("zite_record_id") or ""),
    )
    return decisions.get(key)


async def _final_rows(
    repo: SeasonStoreRepository,
) -> dict[str, list[dict[str, Any]]]:
    """Read every final store under legacy dict names."""
    return {entity: await repo.final_rows(entity) for entity in _ENTITIES}


def _member_row(
    group: _Group,
    member: dict[str, Any],
    finals: dict[str, list[dict[str, Any]]],
    family_row: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the final row that represents one group member.

    Children and sponsors own their row; a family group's submissions
    share the group's single family row.

    Args:
        group: The member's duplicate group.
        member: The audited group member.
        finals: Every final store.
        family_row: The group's family row, when the group is a family.

    Returns:
        The member's final row, or None when it has none.
    """
    record_id = str(member.get("zite_record_id") or "")
    if group.entity == "child":
        entity = "child"
    elif group.entity == "sponsor":
        entity = "sponsor"
    else:
        return family_row
    return next(
        (
            row
            for row in finals[entity]
            if str(row.get("zite_record_id") or "") == record_id
        ),
        None,
    )


def _group_family_row(
    group: _Group, family_rows: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Return the single final family row a family group shares.

    Args:
        group: The duplicate group.
        family_rows: Every final family row.

    Returns:
        The group's family row, or None when it has none.
    """
    if group.entity != "family":
        return None
    survivor = next(
        (m for m in group.members if as_bool(m.get("survivor"))), None
    )
    record_id = str((survivor or {}).get("zite_record_id") or "")
    for row in family_rows:
        if str(row.get("zite_record_id") or "") == record_id:
            return row
    for row in family_rows:
        if str(row.get("primary_email") or "") == group.group_key:
            return row
    return None


def _raw_record(member: dict[str, Any]) -> dict[str, Any]:
    """Return one member's raw submission record.

    Args:
        member: The duplicate-log row.

    Returns:
        The stored raw record, or an empty dict when it is unreadable.
    """
    raw = member.get("record")
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _conflict_columns(group: _Group) -> list[str]:
    """Return the columns whose values differ across the group."""
    for member in group.members:
        columns = [
            column
            for column in str(member.get("conflict_columns") or "").split(",")
            if column
        ]
        if columns:
            return columns
    return []


def _group_created(group: _Group) -> str | None:
    """Return the group's earliest submission timestamp."""
    created = [str(member.get("created") or "") for member in group.members]
    return min(created) or None


def _used_letters(
    family_id: int,
    child_rows: list[dict[str, Any]],
    exclude: str | None = None,
) -> set[str]:
    """Return every angel letter the family's children hold.

    Args:
        family_id: The family.
        child_rows: Every final child row.
        exclude: One child's record id whose own letter is left out, so
            the member can be given a letter of its own.

    Returns:
        The letters in use, ignored rows included.
    """
    return {
        str(row.get("angel_id"))
        for row in child_rows
        if _as_int(row.get("family_id")) == family_id
        and row.get("angel_id")
        and str(row.get("zite_record_id") or "") != (exclude or "")
    }


def _free_letter(
    family_id: int, used: set[str], avoid: str | None = None
) -> str:
    """Return the lowest unused angel letter for a family.

    Args:
        family_id: The family.
        used: The letters already in use.
        avoid: A letter the result must not be, such as the letter the
            member shared with its duplicate group.

    Returns:
        The letter to assign.
    """
    index = 0
    while True:
        candidate = f"{family_id}{index_to_letters(index)}"
        if candidate not in used and candidate != avoid:
            return candidate
        index += 1


def _group_letter(
    rows: list[dict[str, Any]],
    family_id: int,
    child_rows: list[dict[str, Any]],
) -> str | None:
    """Return the single angel letter a pending group shares.

    Args:
        rows: The group's stored child rows.
        family_id: The group's family.
        child_rows: Every final child row.

    Returns:
        The shared letter, or None when the family is unknown.
    """
    for row in rows:
        letter = _as_text(row.get("angel_id"))
        if letter:
            return letter
    return _free_letter(family_id, _used_letters(family_id, child_rows))


def _family_letters(
    family_id: int, child_rows: list[dict[str, Any]]
) -> list[str]:
    """Return the distinct letters a family's participating children hold.

    Args:
        family_id: The family.
        child_rows: Every final child row.

    Returns:
        The letters, ordered by family then letter.
    """
    letters: list[str] = []
    for row in child_rows:
        if _as_int(row.get("family_id")) != family_id:
            continue
        if as_bool(row.get("ignore")):
            continue
        if str(row.get("status") or "") != STATUS_AUDITED:
            continue
        letter = _as_text(row.get("angel_id"))
        if letter and letter not in letters:
            letters.append(letter)
    letters.sort(key=_angel_sort_key)
    return letters


def _angel_sort_key(angel_id: str) -> tuple[int, str]:
    """Order one family's angel letters by length then alphabetically.

    Args:
        angel_id: The angel id (family number plus letters).

    Returns:
        A comparable sort key, so ``1A`` < ``1B`` < ``1AA``.
    """
    match = re.fullmatch(r"(\d+)([A-Z]+)", str(angel_id))
    if match:
        return (len(match.group(2)), match.group(2))
    return (len(str(angel_id)), str(angel_id))


__all__ = [
    "DECISION_IGNORE",
    "DECISION_NOT_DUPLICATE",
    "decide",
    "filter_groups",
    "group_detail",
    "group_id",
    "group_neighbours",
    "list_groups",
    "mark_audited",
    "pending_groups_by_row",
    "status_counts",
    "sync_pending_angel_letters",
]
