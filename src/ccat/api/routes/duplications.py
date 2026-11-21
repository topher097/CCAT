"""Duplication routes: list duplicate groups, inspect and resolve them.

The Duplications section of the audit tool lists the duplicate groups
the last screening run detected, shows each group's raw submissions
beside the screened results, and records one decision per member:
``not_duplicate`` gives the member a unique id of its own, ``ignore``
ignores it exactly like the audit tool's ignore action.  Every decision
is appended to the ``duplication_audit`` table.
"""

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Query, Request

from ccat.api.auth import AuthUser, current_user, require_csrf, require_roles
from ccat.api.deps import season_repo
from ccat.api.dto import (
    DuplicationAuditRequest,
    DuplicationChild,
    DuplicationChildrenRequest,
    DuplicationDecisionRequest,
    DuplicationDetail,
    DuplicationListPage,
    DuplicationMember,
    NeighbourView,
    canonicalize,
    decanonicalize_patch,
)
from ccat.api.errors import ApiError
from ccat.audit import RowAuditedError
from ccat.db.errors import StaleRowVersionError
from ccat.db.repositories import SeasonStoreRepository
from ccat.services import db_duplications

if TYPE_CHECKING:
    from ccat.config import Config

router = APIRouter(
    prefix="/seasons/{season}/duplications", tags=["duplications"]
)


def _member_dto(entity: str, member: dict[str, Any]) -> DuplicationMember:
    """Render one group member as its DTO (canonical screened names).

    The raw record is canonicalized too, so a raw column and the
    current column of the same field carry the same name.

    Args:
        entity: The group's entity name.
        member: The member payload.

    Returns:
        The member DTO.
    """
    screened = member.get("screened")
    public = (
        canonicalize(
            entity,
            {
                key: value
                for key, value in screened.items()
                if not key.startswith("_")
            },
        )
        if isinstance(screened, dict)
        else None
    )
    return DuplicationMember(
        zite_record_id=str(member.get("zite_record_id") or ""),
        created=member.get("created"),
        survivor=bool(member.get("survivor")),
        conflict_columns=list(member.get("conflict_columns") or []),
        conflict_details=member.get("conflict_details"),
        raw=canonicalize(entity, dict(member.get("raw") or {})),
        screened=public,
        decision=member.get("decision"),
        assigned_id=member.get("assigned_id"),
        decided_by=member.get("decided_by"),
        decided_at=member.get("decided_at"),
        row_version=member.get("row_version"),
        state=str(member.get("state") or "surviving"),
        row_key=str(member.get("row_key") or ""),
        row_status=member.get("row_status"),
    )


def _detail_dto(
    detail: dict[str, Any],
    previous: dict[str, str] | None = None,
    next_row: dict[str, str] | None = None,
) -> DuplicationDetail:
    """Render one group detail as its DTO.

    Args:
        detail: The service payload.
        previous: The group before this one in the list's order.
        next_row: The group after this one in the list's order.

    Returns:
        The detail DTO.
    """
    entity = str(detail.get("entity") or "")
    return DuplicationDetail(
        id=str(detail.get("id") or ""),
        sheet=str(detail.get("sheet") or ""),
        entity=entity,
        group_key=str(detail.get("group_key") or ""),
        status=str(detail.get("status") or ""),
        audited_at=detail.get("audited_at"),
        members=[
            _member_dto(entity, member)
            for member in detail.get("members") or []
        ],
        survivor_count=int(detail.get("survivor_count") or 0),
        children=[
            DuplicationChild(**child) for child in detail.get("children") or []
        ],
        previous=NeighbourView(**previous) if previous else None,
        next=NeighbourView(**next_row) if next_row else None,
    )


@router.get("", response_model=DuplicationListPage)
async def list_duplications(
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    q: str = "",
    entity: str | None = None,
    _user: AuthUser = Depends(current_user),
) -> DuplicationListPage:
    """List the detected duplicate groups.

    Args:
        request: The current request.
        repo: The season-bound repository.
        page: 1-based page number.
        page_size: Page size (max 200).
        q: Search on the group key.
        entity: Restrict to "child", "family", or "sponsor".
        _user: Authenticated identity.

    Returns:
        One page of duplicate groups.
    """
    groups = db_duplications.filter_groups(
        await db_duplications.list_groups(repo), q, entity
    )
    start = (page - 1) * page_size
    return DuplicationListPage(
        items=groups[start : start + page_size],
        total=len(groups),
        page=page,
        page_size=page_size,
    )


@router.get("/{group_id}", response_model=DuplicationDetail)
async def get_duplication(
    group_id: str,
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    q: str = "",
    entity: str | None = None,
    _user: AuthUser = Depends(current_user),
) -> DuplicationDetail:
    """Read one duplicate group's raw and screened results.

    Args:
        group_id: The group's id.
        request: The current request.
        repo: The season-bound repository.
        q: The list's search query (for the neighbours).
        entity: Restrict to "child", "family", or "sponsor" (for the
            neighbours).
        _user: Authenticated identity.

    Returns:
        The group detail.

    Raises:
        ApiError: 404 when no group carries that id.
    """
    detail = await db_duplications.group_detail(repo, group_id)
    if detail is None:
        raise ApiError(
            404, "not_found", f"unknown duplication group {group_id!r}"
        )
    previous, next_row = await db_duplications.group_neighbours(
        repo, group_id, q, entity
    )
    return _detail_dto(detail, previous, next_row)


@router.post(
    "/{group_id}/members/{record_id}",
    response_model=DuplicationDetail,
    dependencies=[Depends(require_csrf)],
)
async def decide_duplication(
    group_id: str,
    record_id: str,
    body: DuplicationDecisionRequest,
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    q: str = "",
    entity: str | None = None,
    user: AuthUser = Depends(require_roles("auditor")),
) -> Any:
    """Resolve one duplicate group member (auditor; confirmed).

    Args:
        group_id: The group's id.
        record_id: The audited group member.
        body: The decision.
        request: The current request.
        repo: The season-bound repository.
        q: The list's search query (for the neighbours).
        entity: Restrict to "child", "family", or "sponsor" (for the
            neighbours).
        user: The authenticated identity.

    Returns:
        The updated group detail.

    Raises:
        ApiError: 422 without confirmation; 404 for an unknown group or
            member; 412 when the stored row changed since the read.
    """
    cfg: Config = request.app.state.cfg
    if not body.confirm:
        raise ApiError(
            422,
            "invalid_input",
            "a duplication decision requires confirm=True",
        )
    try:
        detail = await db_duplications.decide(
            repo,
            cfg,
            group_id,
            record_id,
            body.decision,
            user.username,
            confirm=True,
            expected_version=body.expected_version,
        )
    except StaleRowVersionError as error:
        raise ApiError(
            412,
            "stale_version",
            str(error),
            entity=error.entity,
            key=str(error.key),
            current_version=error.current_version,
        ) from error
    previous, next_row = await db_duplications.group_neighbours(
        repo, group_id, q, entity
    )
    return _detail_dto(detail, previous, next_row)


@router.post(
    "/{group_id}/audit",
    response_model=DuplicationDetail,
    dependencies=[Depends(require_csrf)],
)
async def audit_duplication(
    group_id: str,
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    q: str = "",
    entity: str | None = None,
    user: AuthUser = Depends(require_roles("auditor")),
    body: DuplicationAuditRequest | None = None,
) -> Any:
    """Mark a resolved duplication audited (auditor).

    The group's surviving entry (its single undecided submission) is
    audited with the carried ``Current (editable)`` values, and every
    member's row status is overridden by the group's adjudication.  The
    body is optional: without it the surviving entry's stored row is
    finalized as it stands.

    Args:
        group_id: The group's id.
        request: The current request.
        repo: The season-bound repository.
        q: The list's search query (for the neighbours).
        entity: Restrict to "child", "family", or "sponsor" (for the
            neighbours).
        user: The authenticated identity.
        body: The reviewed values under canonical DTO names, and the
            surviving entry's expected ``row_version``.

    Returns:
        The updated group detail.

    Raises:
        ApiError: 404 for an unknown group; 409 when the group has zero
            or more than one surviving entry; 409 when the surviving
            entry is already audited and values were carried; 412 when a
            stored row changed since the read.
    """
    cfg: Config = request.app.state.cfg
    values = body.values if body is not None else None
    if values is not None:
        values = decanonicalize_patch(values)
    try:
        detail = await db_duplications.mark_audited(
            repo,
            cfg,
            group_id,
            user.username,
            values=values,
            expected_version=body.expected_version if body else None,
        )
    except RowAuditedError as error:
        raise ApiError(409, "row_audited", str(error)) from error
    except StaleRowVersionError as error:
        raise ApiError(
            412,
            "stale_version",
            str(error),
            entity=error.entity,
            key=str(error.key),
            current_version=error.current_version,
        ) from error
    previous, next_row = await db_duplications.group_neighbours(
        repo, group_id, q, entity
    )
    return _detail_dto(detail, previous, next_row)


@router.post(
    "/{group_id}/children",
    response_model=DuplicationDetail,
    dependencies=[Depends(require_csrf)],
)
async def assign_duplication_children(
    group_id: str,
    body: DuplicationChildrenRequest,
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    q: str = "",
    entity: str | None = None,
    user: AuthUser = Depends(require_roles("auditor")),
) -> Any:
    """Assign a split family's children to their families (auditor).

    Args:
        group_id: The group's id.
        body: Child record id -> target family id.
        request: The current request.
        repo: The season-bound repository.
        q: The list's search query (for the neighbours).
        entity: Restrict to "child", "family", or "sponsor" (for the
            neighbours).
        user: The authenticated identity.

    Returns:
        The updated group detail.

    Raises:
        ApiError: 404 for an unknown group or child; 412 when a stored
            row changed since the read.
    """
    try:
        detail = await db_duplications.assign_group_children(
            repo, group_id, user.username, body.assignments
        )
    except StaleRowVersionError as error:
        raise ApiError(
            412,
            "stale_version",
            str(error),
            entity=error.entity,
            key=str(error.key),
            current_version=error.current_version,
        ) from error
    previous, next_row = await db_duplications.group_neighbours(
        repo, group_id, q, entity
    )
    return _detail_dto(detail, previous, next_row)


__all__ = ["router"]
