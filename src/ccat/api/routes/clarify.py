"""Clarification routes (§9.6, D5). List/detail/draft/contact — never send.

Extraction, drafts, and contact state come from the DB-era services
(:mod:`ccat.services.db_clarify`); there is deliberately no send
endpoint anywhere (no-autosend invariant).
"""

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, Header, Request

from ccat.api.auth import AuthUser, current_user, require_csrf
from ccat.api.auth import require_roles
from ccat.api.deps import season_repo, session_factory_dep
from ccat.api.dto import (
    ClarifyDraftView,
    ClarifyFamilyView,
    ContactRequest,
    DraftPatch,
)
from ccat.api.errors import ApiError
from ccat.db.repositories import SeasonStoreRepository
from ccat.jobs import service as jobs_service
from ccat.services import db_clarify

if TYPE_CHECKING:
    from ccat.config import Config

router = APIRouter(tags=["clarify"])

_STATUS_KEYS = (
    "contacted",
    "contacted_at",
    "replied",
    "updated_by",
    "updated_at",
)


def _status_value(state: dict[str, Any] | None, key: str) -> Any:
    """Read one contact-state key with defaults.

    Args:
        state: The contact-state dict, or None.
        key: One of :data:`_STATUS_KEYS`.

    Returns:
        The stored value or the default (False for flags).
    """
    if state is None:
        return False if key in {"contacted", "replied"} else None
    return state.get(key)


def _view(
    family_id: int,
    children: list[dict[str, Any]],
    state: dict[str, Any] | None,
    draft: dict[str, Any] | None,
) -> ClarifyFamilyView:
    """Assemble one family view.

    Args:
        family_id: The family key.
        children: The family's extract rows.
        state: The contact-state dict, or None.
        draft: The latest draft dict, or None.

    Returns:
        The family view.
    """
    view = ClarifyFamilyView(
        family_id=family_id,
        children=children,
        **{key: _status_value(state, key) for key in _STATUS_KEYS},
    )
    if draft is not None:
        view.draft = ClarifyDraftView(**draft)
    return view


async def _family_view(
    repo: SeasonStoreRepository, family_id: int
) -> ClarifyFamilyView:
    """Build one family's view (rows + contact state + latest draft).

    Args:
        repo: The season-bound repository.
        family_id: The family key.

    Returns:
        The family view.

    Raises:
        ApiError: 404 when the family has no clarification entry.
    """
    rows = await db_clarify._extract_rows_repo(repo)
    children = [
        row for row in rows if str(row.get("family_id")) == str(family_id)
    ]
    state = next(
        (
            entry
            for entry in await repo.clarification_families()
            if int(entry["family_id"]) == family_id
        ),
        None,
    )
    draft = await repo.latest_draft(family_id)
    if not children and state is None and draft is None:
        raise ApiError(
            404, "not_found", f"family {family_id} has no clarification entry"
        )
    return _view(family_id, children, state, draft)


@router.get(
    "/seasons/{season}/clarifications",
    response_model=list[ClarifyFamilyView],
)
async def list_clarifications(
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    _user: AuthUser = Depends(current_user),
) -> list[ClarifyFamilyView]:
    """List flagged families with draft + contact state (§9.6).

    Args:
        request: The current request.
        repo: The season-bound repository.
        _user: Authenticated identity.

    Returns:
        One view per family present in the extraction or state.
    """
    rows = await db_clarify._extract_rows_repo(repo)
    states = {
        int(state["family_id"]): state
        for state in await repo.clarification_families()
    }
    by_family: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_family.setdefault(int(row["family_id"]), []).append(row)
    ids = sorted(set(by_family) | set(states))
    views: list[ClarifyFamilyView] = []
    for family_id in ids:
        draft = await repo.latest_draft(family_id)
        views.append(
            _view(
                family_id,
                by_family.get(family_id, []),
                states.get(family_id),
                draft,
            )
        )
    return views


@router.get(
    "/seasons/{season}/clarifications/{family_id}",
    response_model=ClarifyFamilyView,
)
async def family_detail(
    family_id: int,
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    _user: AuthUser = Depends(current_user),
) -> ClarifyFamilyView:
    """One family's draft + status + children (§9.6).

    Args:
        family_id: The family key.
        request: The current request.
        repo: The season-bound repository.
        _user: Authenticated identity.

    Returns:
        The family view.
    """
    return await _family_view(repo, family_id)


@router.patch(
    "/seasons/{season}/clarifications/{family_id}/draft",
    response_model=ClarifyDraftView,
    dependencies=[Depends(require_csrf)],
)
async def patch_draft(
    family_id: int,
    body: DraftPatch,
    request: Request,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    repo: SeasonStoreRepository = Depends(season_repo),
    user: AuthUser = Depends(require_roles("auditor")),
) -> ClarifyDraftView:
    """Edit the draft at an explicit version (auditor; If-Match).

    Args:
        family_id: The family key.
        body: The tri-state draft fields (omitted = unchanged).
        request: The current request.
        if_match: The required draft version.
        repo: The season-bound repository.
        user: The authenticated identity.

    Returns:
        The new draft version view.

    Raises:
        ApiError: 412 missing/stale If-Match (stale carries the fresh
            draft); 404 when no draft exists.
    """
    cfg: Config = request.app.state.cfg
    if not if_match:
        raise ApiError(
            412,
            "stale_version",
            "If-Match draft version header is required for saves",
        )
    try:
        expected = int(if_match)
    except ValueError:
        raise ApiError(
            422, "invalid_input", "If-Match must be an int"
        ) from None
    fields = {field: getattr(body, field) for field in body.model_fields_set}
    try:
        draft = await db_clarify.update_draft(
            cfg, user.username, family_id, expected, fields
        )
    except db_clarify.StaleDraftError as error:
        raise ApiError(412, "stale_version", str(error)) from error
    return ClarifyDraftView(**draft)


@router.post(
    "/seasons/{season}/clarifications/{family_id}/contacted",
    response_model=ClarifyFamilyView,
    dependencies=[Depends(require_csrf)],
)
async def mark_contacted(
    family_id: int,
    body: ContactRequest,
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    user: AuthUser = Depends(require_roles("auditor")),
) -> ClarifyFamilyView:
    """Mark contacted (true stamps UTC contacted_at; false unmarks).

    Args:
        family_id: The family key.
        body: ``{"contacted": bool}`` (null rejected).
        request: The current request.
        repo: The season-bound repository.
        user: The authenticated identity.

    Returns:
        The updated family view.
    """
    cfg: Config = request.app.state.cfg
    if body.contacted is None:
        raise ApiError(422, "invalid_input", "contacted must be true or false")
    await db_clarify.set_contacted(
        cfg, user.username, family_id, contacted=body.contacted
    )
    return await _family_view(repo, family_id)


@router.post(
    "/seasons/{season}/clarifications/{family_id}/replied",
    response_model=ClarifyFamilyView,
    dependencies=[Depends(require_csrf)],
)
async def mark_replied(
    family_id: int,
    body: ContactRequest,
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    user: AuthUser = Depends(require_roles("auditor")),
) -> ClarifyFamilyView:
    """Mark replied (auditor).

    Args:
        family_id: The family key.
        body: ``{"replied": bool}``.
        request: The current request.
        repo: The season-bound repository.
        user: The authenticated identity.

    Returns:
        The updated family view.
    """
    cfg: Config = request.app.state.cfg
    if body.replied is None:
        raise ApiError(422, "invalid_input", "replied must be true or false")
    await db_clarify.set_replied(
        cfg, user.username, family_id, replied=body.replied
    )
    return await _family_view(repo, family_id)


@router.post("/seasons/{season}/clarify-export", status_code=202)
async def enqueue_export(
    request: Request,
    session_factory: Any = Depends(session_factory_dep),
    user: AuthUser = Depends(require_roles("coordinator")),
    _csrf: None = Depends(require_csrf),
) -> Any:
    """Enqueue the clarify-export job (coordinator; no autosend, §9.6).

    Args:
        request: The current request.
        session_factory: The app session factory.
        user: The authenticated coordinator.
        _csrf: CSRF guard.

    Returns:
        The queued job view.
    """
    cfg: Config = request.app.state.cfg
    job = await jobs_service.enqueue_job(
        session_factory, cfg, "clarify_export", {}, requester=user.username
    )
    from ccat.api.routes.jobs import _view

    return _view(job)


__all__ = ["router"]
