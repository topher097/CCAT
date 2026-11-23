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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ccat.audit import AuditError, RowAuditedError
from ccat.audit.store import (
    ENTITY_SPECS,
    STATUS_AUDITED,
    STATUS_NEW,
    _as_int,
    _as_text,
    _default_row,
    _raw_columns,
    as_bool,
)
from ccat.domain import SPONSOR_CLAIM_SHEET, index_to_letters
from ccat.services import db_audit
from ccat.services.audit import SavePatch
from ccat.services.db_assignments import (
    apply_family_summary,
    apply_sponsor_summary,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from ccat.config import Config
    from ccat.db.repositories import SeasonStoreRepository

_ENTITIES = ("child", "family", "sponsor")

#: Duplicate-log sheet label -> entity name.
_SHEET_ENTITY = {
    "child_submissions": "child",
    "family_submissions": "family",
    "sponsor_submissions": "sponsor",
    SPONSOR_CLAIM_SHEET: "sponsor",
}

#: Duplicate-log sheet label -> the kind of group the sheet holds.
_SHEET_KIND = {
    "child_submissions": "duplicate_submission",
    "family_submissions": "duplicate_submission",
    "sponsor_submissions": "duplicate_submission",
    SPONSOR_CLAIM_SHEET: "angel_claim_conflict",
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
        classified = _classify_members(group, decisions)
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
                "kind": _SHEET_KIND.get(group.sheet, "duplicate_submission"),
                "group_key": group.group_key,
                "member_count": len(group.members),
                "decided_count": decided,
                "survivor_count": len(classified["surviving"]),
                "released_count": len(classified["released"]),
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
    groups: list[dict[str, Any]],
    q: str = "",
    entity: str | None = None,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    """Return the groups the duplication list shows.

    The list and the neighbour lookup share this filter, so "next" and
    "previous" always mean the adjacent row of the table the user is
    looking at.

    Args:
        groups: Every group summary, in the list's order.
        q: The list's search query.
        entity: Restrict to "child", "family", or "sponsor".
        kind: Restrict to "duplicate_submission" or "angel_claim_conflict".

    Returns:
        The filtered groups, in the list's order.
    """
    needle = q.strip().lower()
    return [
        group
        for group in groups
        if (entity is None or group["entity"] == entity)
        and (kind is None or group["kind"] == kind)
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
        family_row = _group_family_row(group, finals["family"], decisions)
        entry = {
            "id": group.id,
            "group_key": group.group_key,
            "status": status,
        }
        key_column = keys[group.entity]
        for member in group.members:
            row = _member_row(group, member, finals, family_row, decisions)
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
    kind: str | None = None,
) -> tuple[dict[str, str] | None, dict[str, str] | None]:
    """Return the groups before and after one group in the list's order.

    Args:
        repo: The season-bound repository.
        wanted_id: The group's id.
        q: The list's search query.
        entity: Restrict to "child", "family", or "sponsor".
        kind: Restrict to "duplicate_submission" or "angel_claim_conflict".

    Returns:
        A (previous, next) pair of ``{"key": ..., "label": ...}`` dicts;
        each side is None when the group sits at the list's edge.
    """
    groups = filter_groups(await list_groups(repo), q, entity, kind)
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
    classified = _classify_members(group, decisions)
    member_states = {
        str(member.get("zite_record_id") or ""): state
        for state, members in classified.items()
        for member in members
    }
    key_column = ENTITY_SPECS[group.entity]["key"]
    family_row = _group_family_row(group, finals["family"], decisions)
    children_by_entry = _family_children_by_entry(group, decisions, finals)
    members: list[dict[str, Any]] = []
    for member in group.members:
        record_id = str(member.get("zite_record_id") or "")
        screened = _member_row(group, member, finals, family_row, decisions)
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
                "state": member_states.get(record_id),
                "row_key": (
                    str(screened.get(key_column)) if screened else None
                ),
                "row_status": screened.get("status") if screened else None,
                "decision": decision["decision"] if decision else None,
                "assigned_id": decision["assigned_id"] if decision else None,
                "decided_by": decision["actor"] if decision else None,
                "decided_at": decision["created_at"] if decision else None,
                "row_version": (
                    screened.get("_row_version") if screened else None
                ),
                "children": children_by_entry.get(record_id, []),
            }
        )
    states = await _states(repo)
    state = states.get((group.sheet, group.group_key))
    bound = _bound_row(group, classified, finals, family_row, decisions)
    entry_key = str((state or {}).get("entry_key") or "")
    return {
        "id": group.id,
        "sheet": group.sheet,
        "entity": group.entity,
        "kind": _SHEET_KIND.get(group.sheet, "duplicate_submission"),
        "group_key": group.group_key,
        "status": _group_status(group, decisions, states),
        "survivor_count": len(classified["surviving"]),
        "audited_at": (state or {}).get("audited_at"),
        "members": members,
        "children": _group_children(group, decisions, finals),
        "current": _merged_current(group.entity, group.members),
        "current_row_key": entry_key
        or (
            str(bound.get(ENTITY_SPECS[group.entity]["key"]))
            if bound is not None
            else ""
        ),
    }


def _bound_row(
    group: _Group,
    classified: dict[str, list[dict[str, Any]]],
    finals: dict[str, list[dict[str, Any]]],
    family_row: dict[str, Any] | None,
    decisions: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the row the group's merged entry is written to.

    The surviving entry owns it; while the group is still unresolved the
    row the screening run flagged as the survivor does.  This is the
    same rule the detail page binds its Current column to.

    Args:
        group: The duplicate group.
        classified: The group's members by state.
        finals: Every final store.
        family_row: The group's shared family row, when the group is a
            family.
        decisions: The member decisions.

    Returns:
        The bound row, or None when the group has none.
    """
    surviving = classified["surviving"]
    candidates = (
        surviving
        if len(surviving) == 1
        else [
            member
            for member in group.members
            if as_bool(member.get("survivor"))
        ]
    )
    for member in candidates:
        row = _member_row(group, member, finals, family_row, decisions)
        if row is not None:
            return row
    return None


def _merge_submissions(members: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Fold a group's submissions into one row.

    A field's merged value is the newest member's non-blank value; where
    the newest member leaves the field blank, an older member's value
    fills it.  The fold therefore runs newest first and only fills a
    field that is still empty.

    Args:
        members: The group's members, oldest first.

    Returns:
        The merged submission record.
    """
    merged: dict[str, Any] = {}
    for member in reversed(list(members)):
        for key, value in _raw_record(member).items():
            if value is None or value == "":
                continue
            if merged.get(key) is None:
                merged[key] = value
    return merged


def _merged_current(
    entity: str,
    members: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Return the merged editable fields of one group.

    The fold covers the group's submissions; the result is limited to the
    entity's editable fields, because everything else (linkage,
    assignment, controls, and the row's own key) belongs to the row the
    merged entry is written to and must not be merged onto it.

    Args:
        entity: The group's entity name.
        members: The group's members, oldest first.

    Returns:
        The merged editable fields.
    """
    editable = set(ENTITY_SPECS[entity]["editable"])
    return {
        key: value
        for key, value in _merge_submissions(members).items()
        if key in editable
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
    decisions = await _decisions(repo)
    standing = _decision_for(decisions, group, member)
    if standing is not None and standing["decision"] == decision:
        # The same decision already stands: a retry after a lost response
        # changes nothing and adds no trail row.
        detail = await group_detail(repo, wanted_id)
        assert detail is not None
        return detail
    if decision == DECISION_IGNORE:
        await _ignore_member(
            repo, cfg, group, member, actor, expected_version, decisions
        )
        assigned_id = None
    else:
        assigned_id = await _release_member(
            repo, cfg, group, member, actor, expected_version, decisions
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
    values: dict[str, Any] | None = None,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Finalize a duplicate group on its surviving entry.

    The surviving entry is the one submission the auditor left
    undecided; every other member is closed out per its decision.
    The surviving entry is saved once, with the carried values and the
    audited status, and it is saved last.  A released member's row is
    forced back to ``new`` so it awaits its own audit; a discarded
    member's row stays ignored and untouched.  The affected angel
    columns are re-derived, and the group's state row is recorded last.

    Args:
        repo: The season-bound repository.
        cfg: Pipeline configuration.
        wanted_id: The group's id.
        actor: Requesting user identity.
        values: The reviewed ``Current (editable)`` values for the
            surviving entry's row; keys are legacy editable columns.
        expected_version: The caller's expected ``row_version`` for the
            surviving entry's row.

    Returns:
        The updated group detail.

    Raises:
        AuditError: For an unknown group, a group without exactly one
            surviving entry, an unknown field, or a missing stored row.
        RowAuditedError: When values would change an already audited
            surviving entry.
        StaleRowVersionError: When the surviving row changed since the
            caller's read.
    """
    group = next((g for g in await _groups(repo) if g.id == wanted_id), None)
    if group is None:
        raise AuditError(f"unknown duplication group {wanted_id!r}")
    decisions = await _decisions(repo)
    classified = _classify_members(group, decisions)
    surviving = classified["surviving"]
    if not surviving:
        raise AuditError(
            "this group has no surviving entry; discard or release every"
            " submission before marking it audited"
        )
    if len(surviving) > 1:
        raise AuditError(
            "this group has more than one surviving entry; discard or"
            " release all but one submission before marking it audited"
        )
    entity = group.entity
    spec = ENTITY_SPECS[entity]
    key_column = spec["key"]
    supplied = dict(values or {})
    picked = supplied.pop("sponsor_id", None) if entity == "sponsor" else None
    carried = {
        field: value for field, value in supplied.items() if field != "status"
    }
    unknown = sorted(
        set(carried) - set(spec["editable"]) - {"clarification_notes"}
    )
    if unknown:
        raise AuditError(f"unknown {entity} fields: {unknown}")
    finals = await _final_rows(repo)
    family_row = _group_family_row(group, finals["family"], decisions)
    surviving_row = _member_row(
        group, surviving[0], finals, family_row, decisions
    )
    if surviving_row is None:
        raise AuditError(
            f"no stored {entity} row for the surviving entry"
            f" {surviving[0].get('zite_record_id')!r}"
        )
    surviving_key = str(surviving_row.get(key_column))
    target_row = surviving_row
    recorded = await _recorded_entry_key(repo, group)
    if recorded is not None and picked is not None and str(picked) != recorded:
        raise AuditError(
            f"this group's entry is already row {recorded}; its sponsor"
            " number cannot be changed"
        )
    if recorded is not None:
        target_row = _member_row_by_key(
            group, recorded, finals, family_row, decisions
        )
        if target_row is None:
            raise AuditError(
                f"this group's recorded entry {recorded!r} has no stored row"
            )
    elif picked is not None:
        target_row = _member_row_by_sponsor_id(
            group, picked, finals, family_row, decisions
        )
        if target_row is None:
            raise AuditError(
                f"sponsor id {picked!r} is not one of this group's members"
            )
        if as_bool(target_row.get("ignore")):
            raise AuditError(
                f"sponsor id {picked!r} is ignored; an ignored row cannot"
                " become this group's entry"
            )
    target_key = str(target_row.get(key_column))
    if carried and target_row.get("status") == STATUS_AUDITED:
        raise RowAuditedError(
            "the surviving entry is already audited; its values are final"
        )
    # A family group's members share one row, so resolve every member
    # first and close each distinct row out once.
    rows: dict[str, tuple[dict[str, Any], str]] = {}
    for state in ("released", "discarded"):
        for member in classified[state]:
            row = _member_row(group, member, finals, family_row, decisions)
            if row is not None:
                rows.setdefault(str(row.get(key_column)), (row, state))
    rows[target_key] = (target_row, "surviving")
    if surviving_key != target_key:
        # The auditor moved the merged entry onto another member's row;
        # the entry it left behind awaits its own audit.
        rows[surviving_key] = (surviving_row, "released")
    for key, (row, state) in rows.items():
        if key == target_key or as_bool(row.get("ignore")):
            continue
        if state == "released":
            await _override_status(
                repo, entity, row[key_column], STATUS_NEW, actor
            )
    if not as_bool(target_row.get("ignore")):
        await db_audit.save_entity_db(
            repo,
            cfg,
            entity,
            target_row[key_column],
            SavePatch(**carried, status=STATUS_AUDITED),
            actor,
            expected_version=(
                expected_version
                if expected_version is not None
                else target_row.get("_row_version")
            ),
        )
    await _refresh_angel_columns(
        repo, actor, [_as_int(row.get("family_id")) for row, _ in rows.values()]
    )
    await repo.record_duplication_state(
        group.sheet,
        group.group_key,
        actor,
        entry_key=str(target_row.get(key_column)),
    )
    detail = await group_detail(repo, wanted_id)
    assert detail is not None
    return detail


async def assign_group_children(
    repo: SeasonStoreRepository,
    wanted_id: str,
    actor: str,
    assignments: dict[str, int],
) -> dict[str, Any]:
    """Move a split family's children to the family the auditor chose.

    Args:
        repo: The season-bound repository.
        wanted_id: The family group's id.
        actor: Requesting user identity.
        assignments: Child ``zite_record_id`` -> target ``family_id``.

    Returns:
        The updated group detail.

    Raises:
        AuditError: For an unknown group or child, a group that is not a
            family, a target family outside the group, or a child that
            is not in the group.
    """
    group = next((g for g in await _groups(repo) if g.id == wanted_id), None)
    if group is None:
        raise AuditError(f"unknown duplication group {wanted_id!r}")
    if group.entity != "family":
        raise AuditError(
            f"duplication group {wanted_id!r} is not a family group"
        )
    decisions = await _decisions(repo)
    family_ids = _family_ids(group, decisions, await repo.final_rows("family"))
    if not family_ids:
        raise AuditError(
            f"no stored family row for duplication group {group.group_key!r}"
        )
    child_rows = await repo.final_rows("child")
    by_record = {
        str(row.get("zite_record_id") or ""): row for row in child_rows
    }
    for record_id, family_id in assignments.items():
        row = by_record.get(str(record_id))
        if row is None:
            raise AuditError(f"unknown child {record_id!r}")
        if family_id not in family_ids:
            raise AuditError(f"family {family_id!r} is not part of this group")
        current = _as_int(row.get("family_id"))
        if current not in family_ids:
            raise AuditError(f"child {record_id!r} is not in this group")
        if current == family_id:
            continue
        row["family_id"] = family_id
        row["angel_id"] = _free_letter(
            family_id,
            _used_letters(family_id, child_rows, exclude=str(record_id)),
        )
    await repo.commit_finals("duplication", "child", child_rows, actor)
    await _refresh_angel_columns(repo, actor, family_ids)
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
    decisions: dict[tuple[str, str, str], dict[str, Any]],
) -> None:
    """Ignore one group member through the ordinary ignore action.

    Args:
        repo: The season-bound repository.
        cfg: Pipeline configuration.
        group: The member's duplicate group.
        member: The audited group member.
        actor: Requesting user identity.
        expected_version: The caller's expected ``row_version``.
        decisions: The member decisions.

    Raises:
        AuditError: When the member has no stored row to ignore.
    """
    finals = await _final_rows(repo)
    family_row = _group_family_row(group, finals["family"], decisions)
    row = _member_row(group, member, finals, family_row, decisions)
    if row is None:
        raise AuditError(
            f"no stored row to ignore for {member.get('zite_record_id')!r}"
        )
    key = ENTITY_SPECS[group.entity]["key"]
    if _is_group_shared_row(group.entity, key, row, family_row):
        # The shared family row belongs to the surviving entry; a discarded
        # registration only records its decision.
        return
    await db_audit.ignore_entity_db(
        repo,
        cfg,
        group.entity,
        row[key],
        actor,
        confirm=True,
        expected_version=expected_version,
    )


def _is_group_shared_row(
    entity: str,
    key_column: str,
    row: dict[str, Any],
    shared_row: dict[str, Any] | None,
) -> bool:
    """Whether a member's row is the row the whole group shares.

    Args:
        entity: The group's entity name.
        key_column: The entity's key column.
        row: The member's resolved final row.
        shared_row: The group's shared family row, when the group is a
            family.

    Returns:
        True for the family group's shared row; False otherwise.
    """
    if entity != "family" or shared_row is None:
        return False
    return str(row.get(key_column)) == str(shared_row.get(key_column))


async def _release_member(
    repo: SeasonStoreRepository,
    cfg: Config,
    group: _Group,
    member: dict[str, Any],
    actor: str,
    expected_version: int | None,
    decisions: dict[tuple[str, str, str], dict[str, Any]],
) -> str | None:
    """Give one group member a unique id of its own.

    Args:
        repo: The season-bound repository.
        cfg: Pipeline configuration.
        group: The member's duplicate group.
        member: The audited group member.
        actor: Requesting user identity.
        expected_version: The caller's expected ``row_version``.
        decisions: The member decisions.

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
        repo, cfg, group, member, actor, expected_version, decisions
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
    decisions: dict[tuple[str, str, str], dict[str, Any]],
) -> str:
    """Release a duplicate family submission as its own family.

    The group's submissions share one ``family_id``, so a released
    submission becomes a new family row with a new id, seeded from its
    own raw record.  The group's children stay with the original
    family; the auditor moves any of them to the new family afterwards.

    Args:
        repo: The season-bound repository.
        cfg: Pipeline configuration.
        group: The member's duplicate group.
        member: The audited group member.
        actor: Requesting user identity.
        expected_version: The caller's expected ``row_version``.
        decisions: The member decisions.

    Returns:
        The new family id.

    Raises:
        AuditError: When the group has no stored family row.
    """
    family_rows = await repo.final_rows("family")
    existing = _group_family_row(group, family_rows, decisions)
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


async def _override_status(
    repo: SeasonStoreRepository,
    entity: str,
    key: str | int,
    status: str,
    actor: str,
) -> None:
    """Force one member's row status (the group's adjudication overrides it).

    ``db_audit.save_entity_db`` re-derives every non-audited status
    through ``status_of``, so the released member's ``new`` status is
    written directly.  ``status`` is a control column, which the audit
    module may write.

    Args:
        repo: The season-bound repository.
        entity: "child", "family", or "sponsor".
        key: The entity's key.
        status: The status to write.
        actor: Requesting user identity.

    Raises:
        AuditError: When the row does not exist.
    """
    rows = await repo.final_rows(entity)
    key_column = ENTITY_SPECS[entity]["key"]
    row = next(
        (item for item in rows if str(item.get(key_column)) == str(key)), None
    )
    if row is None:
        raise AuditError(f"unknown {entity} {key!r}")
    row["status"] = status
    await repo.commit_finals("audit", entity, rows, actor)


async def _refresh_angel_columns(
    repo: SeasonStoreRepository,
    actor: str,
    family_ids: Iterable[int | None],
) -> None:
    """Re-derive the assignment summaries a duplication decision changed.

    The child's own assignment is the truth; the family's and the
    sponsor's summaries follow the children through the shared
    derivation, so the duplication workflow cannot drift from the
    matcher.

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
    apply_family_summary(family_rows, child_rows)
    apply_sponsor_summary(sponsor_rows, child_rows)
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


def _classify_members(
    group: _Group,
    decisions: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Split a group's members by their decision.

    Args:
        group: The duplicate group.
        decisions: The member decisions.

    Returns:
        ``{"surviving": [...], "released": [...], "discarded": [...]}``;
        a member with no decision is a candidate to survive.
    """
    classified: dict[str, list[dict[str, Any]]] = {
        "surviving": [],
        "released": [],
        "discarded": [],
    }
    for member in group.members:
        decision = _decision_for(decisions, group, member)
        if decision is None:
            classified["surviving"].append(member)
        elif decision["decision"] == DECISION_IGNORE:
            classified["discarded"].append(member)
        else:
            classified["released"].append(member)
    return classified


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
        The group's state; a group is resolved once exactly one
        submission is still undecided.
    """
    state = states.get((group.sheet, group.group_key))
    if state is not None:
        return str(state.get("status") or "audited")
    surviving = _classify_members(group, decisions)["surviving"]
    return "resolved" if len(surviving) == 1 else "pending"


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
    decisions: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the final row that represents one group member.

    Children and sponsors own their row; a family group's submissions
    share the group's single family row, except a released submission,
    which owns the new family row its decision created.

    Args:
        group: The member's duplicate group.
        member: The audited group member.
        finals: Every final store.
        family_row: The group's shared family row, when the group is a
            family.
        decisions: The member decisions.

    Returns:
        The member's final row, or None when it has none.
    """
    record_id = str(member.get("zite_record_id") or "")
    if group.entity == "child":
        entity = "child"
    elif group.entity == "sponsor":
        entity = "sponsor"
    else:
        decision = _decision_for(decisions, group, member)
        if decision is not None and decision["decision"] == (
            DECISION_NOT_DUPLICATE
        ):
            family_id = _as_int(decision.get("assigned_id"))
            if family_id is not None:
                return next(
                    (
                        row
                        for row in finals["family"]
                        if _as_int(row.get("family_id")) == family_id
                    ),
                    None,
                )
        return family_row
    return next(
        (
            row
            for row in finals[entity]
            if str(row.get("zite_record_id") or "") == record_id
        ),
        None,
    )


async def _recorded_entry_key(
    repo: SeasonStoreRepository,
    group: _Group,
) -> str | None:
    """Return the row key the group's adjudication landed on.

    The group's state row records it, so a later read of the group binds
    to the row the auditor chose rather than to the surviving member.

    Args:
        repo: The season-bound repository.
        group: The duplicate group.

    Returns:
        The recorded entry key, or None when the group has no audited
        entry yet.
    """
    states = await _states(repo)
    state = states.get((group.sheet, group.group_key))
    if state is None:
        return None
    key = str(state.get("entry_key") or "")
    return key or None


def _member_row_by_key(
    group: _Group,
    key: str,
    finals: dict[str, list[dict[str, Any]]],
    family_row: dict[str, Any] | None,
    decisions: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the group member's row that carries one row key.

    Args:
        group: The duplicate group.
        key: The row key.
        finals: Every final store.
        family_row: The group's shared family row, when the group is a
            family.
        decisions: The member decisions.

    Returns:
        The member's row, or None when no member carries that key.
    """
    key_column = ENTITY_SPECS[group.entity]["key"]
    for member in group.members:
        row = _member_row(group, member, finals, family_row, decisions)
        if row is not None and str(row.get(key_column)) == str(key):
            return row
    return None


def _member_row_by_sponsor_id(
    group: _Group,
    sponsor_id: Any,
    finals: dict[str, list[dict[str, Any]]],
    family_row: dict[str, Any] | None,
    decisions: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the group member's row that owns one sponsor id.

    Args:
        group: The duplicate group.
        sponsor_id: The sponsor id the auditor chose.
        finals: Every final store.
        family_row: The group's shared family row, when the group is a
            family.
        decisions: The member decisions.

    Returns:
        The member's row, or None when no member owns that sponsor id.
    """
    for member in group.members:
        row = _member_row(group, member, finals, family_row, decisions)
        if row is not None and str(row.get("sponsor_id")) == str(sponsor_id):
            return row
    return None


def _group_family_row(
    group: _Group,
    family_rows: list[dict[str, Any]],
    decisions: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the single final family row a family group shares.

    A released family submission owns a new row whose ``family_id`` its
    decision recorded; excluding it is what separates the two rows
    when the flagged survivor is the released one (both rows then carry
    the same ``zite_record_id``).

    Args:
        group: The duplicate group.
        family_rows: Every final family row.
        decisions: The member decisions.

    Returns:
        The group's shared family row, or None when it has none.
    """
    if group.entity != "family":
        return None
    released = {
        family_id
        for member in _classify_members(group, decisions)["released"]
        for family_id in [
            _as_int(
                (_decision_for(decisions, group, member) or {}).get(
                    "assigned_id"
                )
            )
        ]
        if family_id is not None
    }
    candidates = [
        row
        for row in family_rows
        if _as_int(row.get("family_id")) not in released
    ]
    record_ids = {
        str(member.get("zite_record_id") or "") for member in group.members
    }
    for row in candidates:
        if str(row.get("zite_record_id") or "") in record_ids:
            return row
    for row in candidates:
        if str(row.get("primary_email") or "") == group.group_key:
            return row
    return None


def _family_ids(
    group: _Group,
    decisions: dict[tuple[str, str, str], dict[str, Any]],
    family_rows: list[dict[str, Any]],
) -> list[int]:
    """Return the family ids a family group's submissions now span.

    Args:
        group: The duplicate group.
        decisions: The member decisions.
        family_rows: Every final family row.

    Returns:
        The shared family's id plus every released family's id; empty
        for a child or sponsor group.
    """
    if group.entity != "family":
        return []
    family_ids: list[int] = []
    shared = _group_family_row(group, family_rows, decisions)
    shared_id = _as_int((shared or {}).get("family_id"))
    if shared_id is not None:
        family_ids.append(shared_id)
    for member in _classify_members(group, decisions)["released"]:
        assigned = _as_int(
            (_decision_for(decisions, group, member) or {}).get("assigned_id")
        )
        if assigned is not None and assigned not in family_ids:
            family_ids.append(assigned)
    return family_ids


def _timestamp(value: Any) -> datetime | None:
    """Parse one stored timestamp to an aware UTC datetime.

    The stores mix ``...Z`` and ``...+00:00`` forms, so the strings are
    parsed rather than compared.

    Args:
        value: The stored timestamp text.

    Returns:
        The aware UTC datetime, or None when the value is absent or
        unparseable.
    """
    text = _as_text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _family_children_by_entry(
    group: _Group,
    decisions: dict[tuple[str, str, str], dict[str, Any]],
    finals: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Attribute a family group's children to the entries they arrived with.

    A child belongs to the latest member whose submission time is at or
    before the child's own time; a child that predates every member, or
    has no usable timestamp, belongs to the first member.

    Args:
        group: The duplicate group.
        decisions: The member decisions.
        finals: Every final store.

    Returns:
        ``{member record id: [child entries]}``; empty for a child or
        sponsor group.
    """
    by_entry: dict[str, list[dict[str, Any]]] = {
        str(member.get("zite_record_id") or ""): [] for member in group.members
    }
    if group.entity != "family" or not group.members:
        return by_entry
    entries = [
        (
            str(member.get("zite_record_id") or ""),
            _timestamp(member.get("created")),
        )
        for member in group.members
    ]
    first = entries[0][0]
    family_ids = _family_ids(group, decisions, finals["family"])
    for row in finals["child"]:
        if _as_int(row.get("family_id")) not in family_ids:
            continue
        stamp = _timestamp(row.get("created")) or _timestamp(
            row.get("zite_created_at")
        )
        entry = first
        if stamp is not None:
            for record_id, member_time in entries:
                if member_time is not None and member_time <= stamp:
                    entry = record_id
        by_entry[entry].append(
            {
                "zite_record_id": str(row.get("zite_record_id") or ""),
                "first_name": row.get("first_name"),
                "dob": row.get("dob"),
                "family_id": _as_int(row.get("family_id")),
                "angel_id": row.get("angel_id"),
            }
        )
    return by_entry


def _group_children(
    group: _Group,
    decisions: dict[tuple[str, str, str], dict[str, Any]],
    finals: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Return a family group's children, across all its families.

    Args:
        group: The duplicate group.
        decisions: The member decisions.
        finals: Every final store.

    Returns:
        One entry per child of the shared or a released family; empty
        for a child or sponsor group.
    """
    family_ids = _family_ids(group, decisions, finals["family"])
    return [
        {
            "zite_record_id": str(row.get("zite_record_id") or ""),
            "first_name": row.get("first_name"),
            "dob": row.get("dob"),
            "family_id": _as_int(row.get("family_id")),
            "angel_id": row.get("angel_id"),
        }
        for row in finals["child"]
        if _as_int(row.get("family_id")) in family_ids
    ]


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
    "assign_group_children",
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
