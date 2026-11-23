"""Manual-matching routes: the pool, the sponsors, assignments, holds.

The manual matcher places children with sponsors by hand: a coordinator
reads a pool of family cards, narrows it with filters, selects whole
families or individual children, and assigns them to a sponsor.

Every mutating route requires the coordinator or admin role and a CSRF
header; any authenticated user may read.
"""

from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from ccat.api.auth import AuthUser, require_csrf, require_roles
from ccat.api.deps import season_repo
from ccat.api.dto import (
    ManualAssignmentListPage,
    ManualAssignmentRequest,
    ManualAssignmentResult,
    ManualDispatchRequest,
    ManualHoldListPage,
    ManualHoldReleaseRequest,
    ManualHoldRequest,
    ManualHoldResult,
    ManualMatchPoolPage,
    ManualMatchSponsor,
    ManualMatchSponsorPage,
    ManualReleaseRequest,
)
from ccat.api.errors import ApiError
from ccat.db.errors import StaleRowVersionError
from ccat.db.repositories import SeasonStoreRepository
from ccat.services import db_manual_match
from ccat.services.db_manual_match import ManualMatchError

if TYPE_CHECKING:
    from ccat.config import Config

router = APIRouter(
    prefix="/seasons/{season}/manual-match", tags=["manual-match"]
)

#: Service error code -> HTTP status (the service carries no numbers).
_STATUS: dict[str, int] = {
    db_manual_match.NOT_FOUND: 404,
    db_manual_match.CONFLICT: 409,
    db_manual_match.SPONSOR_FULL: 409,
    db_manual_match.MATCH_RUNNING: 409,
    db_manual_match.PACKETS_DISPATCHED: 409,
    db_manual_match.STALE_VERSION: 412,
    db_manual_match.INVALID_INPUT: 422,
}

#: Entity name -> its key column, for the 412 body.
_KEY_COLUMN: dict[str, str] = {
    "child": "zite_record_id",
    "family": "family_id",
    "sponsor": "sponsor_id",
}

AssignmentFilter = Literal["unassigned", "partial", "split", "assigned", "any"]
HoldFilter = Literal["any", "held", "not_held"]
GenderFilter = Literal["any", "Male", "Female", "Non-binary"]
SpecialNeedsFilter = Literal["any", "yes", "no"]
LocationFilter = Literal["any", "hancock", "calumet"]
ChildStatusFilter = Literal[
    "any", "audited", "in_progress", "needs_clarification", "new"
]
InfoSheetFilter = Literal["any", "missing", "generated"]
SourceFilter = Literal["auto", "manual"]


def _error(error: ManualMatchError) -> ApiError:
    """Translate one service refusal into the API error.

    Args:
        error: The service error.

    Returns:
        The mapped API error.
    """
    status = _STATUS.get(error.code, 409)
    return ApiError(status, error.code, str(error), **error.context)


async def _stale_response(
    repo: SeasonStoreRepository, error: StaleRowVersionError
) -> JSONResponse:
    """Build the 412 body with the fresh server row.

    Args:
        repo: The season-bound repository (usable after rollback).
        error: The raised stale-version error.

    Returns:
        The 412 JSON response.
    """
    key_column = _KEY_COLUMN.get(error.entity, "zite_record_id")
    rows = await repo.final_rows(error.entity)
    existing = next(
        (row for row in rows if str(row.get(key_column)) == str(error.key)),
        None,
    )
    fresh = (
        {
            key: value
            for key, value in existing.items()
            if not key.startswith("_")
        }
        if existing
        else None
    )
    return JSONResponse(
        status_code=412,
        content={
            "detail": {
                "code": "stale_version",
                "message": str(error),
                "entity": error.entity,
                "key": str(error.key),
                "current_version": error.current_version,
                "row": fresh,
                "last_updated_by": (
                    existing.get("_last_updated_by") if existing else None
                ),
                "last_updated_at": (
                    existing.get("_last_updated_at") if existing else None
                ),
            }
        },
    )


@router.get("/pool", response_model=ManualMatchPoolPage)
async def get_pool(
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    q: str = "",
    assignment: AssignmentFilter = "unassigned",
    child_gender: GenderFilter = "any",
    age_min: int | None = None,
    age_max: int | None = None,
    special_needs: SpecialNeedsFilter = "any",
    location: LocationFilter = "any",
    family_size_min: int | None = None,
    family_size_max: int | None = None,
    child_status: ChildStatusFilter = "any",
    all_audited: bool = False,
    info_sheet: InfoSheetFilter = "any",
    hold: HoldFilter = "not_held",
    sponsor_id: int | None = None,
) -> Any:
    """List the filtered family cards of the manual pool.

    Args:
        request: The current request.
        repo: The season-bound repository.
        page: The 1-based page number.
        page_size: The page size (1-200).
        q: Free text over family name, email, city, zip, child name.
        assignment: The family assignment state filter.
        child_gender: The child gender filter.
        age_min: The lowest child age at Christmas.
        age_max: The highest child age at Christmas.
        special_needs: Whether a child has stated special needs.
        location: The canonical pickup-location bucket.
        family_size_min: The smallest non-ignored family.
        family_size_max: The largest non-ignored family.
        child_status: The child audit-lifecycle filter.
        all_audited: Keep only families whose every live child is audited.
        info_sheet: Whether a child's info sheet is missing or generated.
        hold: Whether the family carries an active hold.
        sponsor_id: Order by fit for this sponsor; drop forbidden families.

    Returns:
        The pool page.

    Raises:
        ApiError: 404 for an unknown sponsor.
    """
    cfg: Config = request.app.state.cfg
    try:
        return await db_manual_match.pool(
            repo,
            cfg,
            db_manual_match.PoolParams(
                page=page,
                page_size=page_size,
                q=q,
                assignment=assignment,
                child_gender=child_gender,
                age_min=age_min,
                age_max=age_max,
                special_needs=special_needs,
                location=location,
                family_size_min=family_size_min,
                family_size_max=family_size_max,
                child_status=child_status,
                all_audited=all_audited,
                info_sheet=info_sheet,
                hold=hold,
                sponsor_id=sponsor_id,
            ),
        )
    except ManualMatchError as error:
        raise _error(error) from error


@router.get("/sponsors", response_model=ManualMatchSponsorPage)
async def get_sponsors(
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    q: str = "",
    affiliation: str | None = None,
    has_capacity: bool | None = None,
) -> Any:
    """List the sponsor picker with capacity.

    Args:
        request: The current request.
        repo: The season-bound repository.
        page: The 1-based page number.
        page_size: The page size (1-200).
        q: Free text over name, organization, and email.
        affiliation: The exact sponsor type filter.
        has_capacity: True for sponsors with room, False for full ones.

    Returns:
        The sponsor page.
    """
    cfg: Config = request.app.state.cfg
    return await db_manual_match.sponsors(
        repo,
        cfg,
        db_manual_match.SponsorParams(
            page=page,
            page_size=page_size,
            q=q,
            affiliation=affiliation,
            has_capacity=has_capacity,
        ),
    )


@router.get("/assignments", response_model=ManualAssignmentListPage)
async def get_assignments(
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    sponsor_id: int | None = None,
    source: SourceFilter | None = None,
    family_id: int | None = None,
) -> Any:
    """List the current assignments, derived from the children.

    Args:
        request: The current request.
        repo: The season-bound repository.
        page: The 1-based page number.
        page_size: The page size (1-200).
        sponsor_id: Only this sponsor's children.
        source: Only "auto" or "manual" assignments.
        family_id: Only this family's children.

    Returns:
        The assignment list page.
    """
    cfg: Config = request.app.state.cfg
    return await db_manual_match.assignments(
        repo,
        cfg,
        db_manual_match.AssignmentParams(
            page=page,
            page_size=page_size,
            sponsor_id=sponsor_id,
            source=source,
            family_id=family_id,
        ),
    )


@router.post(
    "/assignments",
    response_model=ManualAssignmentResult,
    dependencies=[Depends(require_csrf)],
)
async def post_assignments(
    body: ManualAssignmentRequest,
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    user: AuthUser = Depends(require_roles("coordinator", "admin")),
) -> Any:
    """Assign the selection to a sponsor (coordinator/admin; confirmed).

    Args:
        body: The assign request.
        request: The current request.
        repo: The season-bound repository.
        user: The authenticated identity.

    Returns:
        The outcome, or a 412 body with the fresh row.

    Raises:
        ApiError: The mapped 404/409/412/422 refusal.
    """
    cfg: Config = request.app.state.cfg
    try:
        return await db_manual_match.assign(
            repo, cfg, user.username, user.id, body
        )
    except ManualMatchError as error:
        raise _error(error) from error
    except StaleRowVersionError as error:
        return await _stale_response(repo, error)


@router.post(
    "/release",
    response_model=ManualAssignmentResult,
    dependencies=[Depends(require_csrf)],
)
async def post_release(
    body: ManualReleaseRequest,
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    user: AuthUser = Depends(require_roles("coordinator", "admin")),
) -> Any:
    """Release assignments (coordinator/admin; confirmed).

    Args:
        body: The release request.
        request: The current request.
        repo: The season-bound repository.
        user: The authenticated identity.

    Returns:
        The outcome, or a 412 body with the fresh row.

    Raises:
        ApiError: 422 without confirmation; mapped refusals otherwise.
    """
    if not body.confirm:
        raise ApiError(422, "invalid_input", "releasing requires confirm=True")
    cfg: Config = request.app.state.cfg
    try:
        return await db_manual_match.release(
            repo, cfg, user.username, user.id, body
        )
    except ManualMatchError as error:
        raise _error(error) from error
    except StaleRowVersionError as error:
        return await _stale_response(repo, error)


@router.post(
    "/dispatch",
    response_model=ManualMatchSponsor,
    dependencies=[Depends(require_csrf)],
)
async def post_dispatch(
    body: ManualDispatchRequest,
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    user: AuthUser = Depends(require_roles("coordinator", "admin")),
) -> Any:
    """Record a sponsor's packet dispatch (coordinator/admin).

    Args:
        body: The dispatch request.
        request: The current request.
        repo: The season-bound repository.
        user: The authenticated identity.

    Returns:
        The sponsor's fresh row, or a 412 body with the fresh row.

    Raises:
        ApiError: The mapped 404/409/412/422 refusal.
    """
    cfg: Config = request.app.state.cfg
    try:
        return await db_manual_match.dispatch_packets(
            repo, cfg, user.username, user.id, body
        )
    except ManualMatchError as error:
        raise _error(error) from error
    except StaleRowVersionError as error:
        return await _stale_response(repo, error)


@router.get("/holds", response_model=ManualHoldListPage)
async def get_holds(
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
) -> Any:
    """List the active family holds, with their notes.

    Args:
        request: The current request.
        repo: The season-bound repository.
        page: The 1-based page number.
        page_size: The page size (1-200).

    Returns:
        The hold page.
    """
    cfg: Config = request.app.state.cfg
    return await db_manual_match.list_holds(
        repo, cfg, db_manual_match.HoldParams(page=page, page_size=page_size)
    )


@router.post(
    "/holds",
    response_model=ManualHoldResult,
    dependencies=[Depends(require_csrf)],
)
async def post_hold(
    body: ManualHoldRequest,
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    user: AuthUser = Depends(require_roles("coordinator", "admin")),
) -> Any:
    """Hold a family, with the reason (coordinator/admin).

    Args:
        body: The hold request (the note is required).
        request: The current request.
        repo: The season-bound repository.
        user: The authenticated identity.

    Returns:
        The new hold.

    Raises:
        ApiError: The mapped 404/409/422 refusal.
    """
    cfg: Config = request.app.state.cfg
    try:
        return await db_manual_match.hold_family(
            repo, cfg, user.username, user.id, body
        )
    except ManualMatchError as error:
        raise _error(error) from error


@router.post(
    "/holds/release",
    response_model=ManualHoldResult,
    dependencies=[Depends(require_csrf)],
)
async def post_release_hold(
    body: ManualHoldReleaseRequest,
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    user: AuthUser = Depends(require_roles("coordinator", "admin")),
) -> Any:
    """Release a family's hold (coordinator/admin; idempotent).

    Args:
        body: The release request.
        request: The current request.
        repo: The season-bound repository.
        user: The authenticated identity.

    Returns:
        The released hold, or ``held=False`` when there was none.
    """
    cfg: Config = request.app.state.cfg
    return await db_manual_match.release_hold(repo, cfg, user.username, body)


__all__ = ["router"]
