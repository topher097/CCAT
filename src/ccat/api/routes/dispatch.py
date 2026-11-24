"""Dispatch routes: the sponsor packet work list, drafts, and sends.

The dispatch page owns the last mile of the program: it lists the
sponsors that still need to go out, renders their email drafts from the
packet manifest, and sends them.

Reading the list and a draft is open to any authenticated user.
Drafting needs the coordinator or admin role.  **Sending needs the
admin role**: it mails a child's information sheet to a third party, and
the program's rule is that only an admin sends it.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response

from ccat.api.auth import AuthUser, record_event, require_csrf, require_roles
from ccat.api.deps import season_repo
from ccat.api.dto import (
    DispatchChildListView,
    DispatchDraftBatchRequest,
    DispatchDraftRequest,
    DispatchDraftView,
    DispatchListView,
    DispatchSendRequest,
    DispatchSendResult,
)
from ccat.api.errors import ApiError
from ccat.db.repositories import SeasonStoreRepository
from ccat.mail import GmailTransport, MailError, ServiceAccountGmailTransport
from ccat.services import db_dispatch
from ccat.services.db_dispatch import DispatchError
from ccat.services.ports import MailTransport

if TYPE_CHECKING:
    from ccat.config import Config

router = APIRouter(prefix="/seasons/{season}/dispatch", tags=["dispatch"])

#: Service error code -> HTTP status (the service carries no numbers).
_STATUS: dict[str, int] = {
    db_dispatch.NOT_FOUND: 404,
    db_dispatch.CONFLICT: 409,
    db_dispatch.MAIL_NOT_CONFIGURED: 422,
    db_dispatch.MAIL_FAILED: 502,
    db_dispatch.INVALID_INPUT: 422,
    db_dispatch.STALE_VERSION: 412,
}


def _error(error: DispatchError) -> ApiError:
    """Translate one service refusal into the API error.

    Args:
        error: The service error.

    Returns:
        The mapped API error.
    """
    status = _STATUS.get(error.code, 409)
    return ApiError(status, error.code, str(error), **error.context)


def _transport(cfg: "Config") -> MailTransport:
    """Build the mail transport the configuration asks for.

    Args:
        cfg: Pipeline configuration.

    Returns:
        The transport.
    """
    if cfg.dispatch.credential_source == "service_account":
        return ServiceAccountGmailTransport(
            subject=cfg.dispatch.service_account_subject,
            timeout=cfg.dispatch.timeout_seconds,
        )
    return GmailTransport(
        Path(cfg.dispatch.client_file),
        Path(cfg.dispatch.token_file),
        timeout=cfg.dispatch.timeout_seconds,
    )


def _may_send(user: AuthUser) -> bool:
    """Whether the caller may send sponsor packet email.

    Args:
        user: The authenticated identity.

    Returns:
        True for an admin.
    """
    return "admin" in set(user.roles)


@router.get("", response_model=DispatchListView)
async def get_dispatch_list(
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    user: AuthUser = Depends(
        require_roles("read_only", "auditor", "coordinator", "admin")
    ),
) -> dict[str, Any]:
    """List the sponsors that still need to go out.

    Args:
        request: The current request.
        repo: The season-bound repository.
        user: The authenticated identity.

    Returns:
        The work list.
    """
    cfg: Config = request.app.state.cfg
    view = await db_dispatch.list_rows(repo, cfg)
    view["can_send"] = _may_send(user)
    return view


@router.get("/children", response_model=DispatchChildListView)
async def get_dispatch_children(
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    user: AuthUser = Depends(
        require_roles("read_only", "auditor", "coordinator", "admin")
    ),
) -> dict[str, Any]:
    """List the assigned children and their info-sheet state.

    Args:
        request: The current request.
        repo: The season-bound repository.
        user: The authenticated identity.

    Returns:
        The child rows.
    """
    cfg: Config = request.app.state.cfg
    return await db_dispatch.list_children(repo, cfg)


@router.get("/sheets.zip")
async def get_sheets_zip(
    season: str,
    request: Request,
    sponsor_id: int | None = Query(default=None, alias="sponsor"),
    repo: SeasonStoreRepository = Depends(season_repo),
    user: AuthUser = Depends(
        require_roles("read_only", "auditor", "coordinator", "admin")
    ),
) -> Response:
    """Download child information sheets as a ZIP archive.

    The archive holds the same child PII as a single ``child_sheet``
    artifact download, so the download is recorded in the security-audit
    events (§8).

    Args:
        season: The season in the route.
        request: The current request.
        sponsor_id: Optional sponsor to scope the archive to.
        repo: The season-bound repository.
        user: The authenticated identity.

    Returns:
        The ZIP archive response.

    Raises:
        ApiError: The mapped refusal when no sheets are available.
    """
    cfg: Config = request.app.state.cfg
    try:
        payload, filename = await db_dispatch.build_sheets_zip(
            repo, cfg, sponsor_id
        )
    except DispatchError as error:
        raise _error(error) from error
    uow = request.app.state.uow
    await record_event(
        uow.session_factory,
        "artifact_download",
        user_id=user.id,
        object_type="child_sheet_zip",
        object_id=str(sponsor_id) if sponsor_id is not None else "all",
        season=season,
        details={"kind": "child_sheet", "sponsor_id": sponsor_id},
    )
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.post(
    "/drafts",
    response_model=DispatchListView,
    dependencies=[Depends(require_csrf)],
)
async def post_drafts(
    body: DispatchDraftBatchRequest,
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    user: AuthUser = Depends(require_roles("coordinator", "admin")),
) -> dict[str, Any]:
    """Render and store one or more sponsors' email drafts.

    Args:
        body: Which sponsors to draft.
        request: The current request.
        repo: The season-bound repository.
        user: The authenticated identity.

    Returns:
        The refreshed work list.

    Raises:
        ApiError: The mapped refusal.
    """
    cfg: Config = request.app.state.cfg
    try:
        view = await db_dispatch.draft_sponsors(
            repo,
            cfg,
            user.username,
            sponsor_ids=list(body.sponsor_ids) or None,
            overwrite=body.overwrite,
        )
    except DispatchError as error:
        raise _error(error) from error
    view["can_send"] = _may_send(user)
    return view


@router.get("/sponsors/{sponsor_id}/draft", response_model=DispatchDraftView)
async def get_draft(
    sponsor_id: int,
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    user: AuthUser = Depends(
        require_roles("read_only", "auditor", "coordinator", "admin")
    ),
) -> dict[str, Any]:
    """Read one sponsor's email draft.

    Args:
        sponsor_id: The sponsor's key.
        request: The current request.
        repo: The season-bound repository.
        user: The authenticated identity.

    Returns:
        The draft.

    Raises:
        ApiError: 404 when the sponsor has no draft.
    """
    cfg: Config = request.app.state.cfg
    try:
        return await db_dispatch.get_draft(repo, cfg, sponsor_id)
    except DispatchError as error:
        raise _error(error) from error


@router.patch(
    "/sponsors/{sponsor_id}/draft",
    response_model=DispatchDraftView,
    dependencies=[Depends(require_csrf)],
)
async def patch_draft(
    sponsor_id: int,
    body: DispatchDraftRequest,
    request: Request,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    repo: SeasonStoreRepository = Depends(season_repo),
    user: AuthUser = Depends(require_roles("coordinator", "admin")),
) -> dict[str, Any]:
    """Change one sponsor's draft subject or note.

    Args:
        sponsor_id: The sponsor's key.
        body: The fields to change.
        request: The current request.
        if_match: The draft version the caller read.
        repo: The season-bound repository.
        user: The authenticated identity.

    Returns:
        The updated draft.

    Raises:
        ApiError: 412 without a usable If-Match; the mapped refusal.
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
    try:
        return await db_dispatch.update_draft(
            repo,
            cfg,
            user.username,
            sponsor_id,
            subject=body.subject,
            note=body.note,
            expected_version=expected,
        )
    except DispatchError as error:
        raise _error(error) from error


@router.post(
    "/sponsors/{sponsor_id}/send",
    response_model=DispatchSendResult,
    dependencies=[Depends(require_csrf)],
)
async def post_send(
    sponsor_id: int,
    body: DispatchSendRequest,
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    user: AuthUser = Depends(require_roles("admin")),
) -> dict[str, Any]:
    """Send one sponsor's packet email (admin only).

    Args:
        sponsor_id: The sponsor's key.
        body: The version and the edits being sent.
        request: The current request.
        repo: The season-bound repository.
        user: The authenticated identity.

    Returns:
        The send outcome.

    Raises:
        ApiError: The mapped refusal.
    """
    cfg: Config = request.app.state.cfg
    try:
        return await db_dispatch.send_draft(
            repo,
            cfg,
            user.username,
            user.id,
            sponsor_id,
            subject=body.subject,
            note=body.note,
            expected_version=body.version,
            transport=_transport(cfg),
        )
    except DispatchError as error:
        raise _error(error) from error
    except MailError as error:
        raise ApiError(422, "mail_not_configured", str(error)) from error


__all__ = ["router"]
