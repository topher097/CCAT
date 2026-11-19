"""Auth routes: dev login, logout, me (refactor §5.2, §8, D2)."""

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict

from ccat.api.auth import (
    CSRF_HEADER,
    SESSION_COOKIE,
    AuthUser,
    adapter_for,
    current_user,
    issue_session,
    record_event,
    require_csrf,
)
from ccat.api.dto import MeView
from ccat.api.errors import ApiError
from ccat.db.models import Session as SessionRow

if TYPE_CHECKING:
    from ccat.db.uow import UnitOfWork

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """Dev login body: username only (no-password accounts, D2)."""

    model_config = ConfigDict(extra="forbid")

    username: str


async def _client_ip(request: Request) -> str:
    """Return the client host for rate limiting.

    Args:
        request: The current request.

    Returns:
        The client host string.
    """
    return request.client.host if request.client else "unknown"


@router.post("/login", status_code=204)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
) -> Response:
    """Log in via the dev no-password adapter (204 + session cookie).

    No CSRF guard: there is no session yet (the rate limiter protects
    the endpoint); the session cookie issued here is authenticated on
    later mutations via :func:`require_csrf`.

    Args:
        body: The username.
        request: The current request.
        response: The response receiving the cookie.

    Returns:
        An empty 204 response.

    Raises:
        ApiError: 403 when auth mode is not ``dev`` (with guidance);
            401 for unknown/inactive/non-dev users; 429 over the rate
            limit.
    """
    cfg = request.app.state.cfg
    uow: UnitOfWork = request.app.state.uow
    if cfg.api.auth_mode != "dev":
        raise ApiError(
            403,
            "auth_mode",
            "passwordless login is enabled only for development;"
            " configure a production identity adapter (D2)",
        )
    limiter = request.app.state.login_limiter
    if not limiter.check(await _client_ip(request)):
        raise ApiError(429, "rate_limited", "too many login attempts")
    adapter = adapter_for(cfg)
    user_id: int | None = None
    username = body.username.strip()
    token: str | None = None
    async with uow.session() as session:
        user = await adapter.authenticate(session, username)
        if user is not None:
            user_id = user.id
            username = user.username
            token, _row = await issue_session(session, cfg, user)
    # Events write on their own connection only after the request
    # session committed (SQLite: one writer at a time).
    await record_event(
        uow.session_factory,
        "login" if token else "login_failed",
        user_id=user_id,
        object_type="user",
        object_id=username,
        details=None if token else {"reason": "unknown_or_inactive"},
    )
    if token is None:
        raise ApiError(401, "unauthenticated", "unknown or inactive user")
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="strict",
        secure=cfg.api.auth_mode != "dev",
        path="/",
    )
    response.status_code = 204
    return response


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    user: AuthUser = Depends(current_user),
    _csrf: None = Depends(require_csrf),
) -> Response:
    """Revoke the session and clear the cookie (204).

    Args:
        request: The current request.
        response: The response clearing the cookie.
        user: The authenticated identity.
        _csrf: CSRF guard.

    Returns:
        An empty 204 response.
    """
    uow: UnitOfWork = request.app.state.uow
    async with uow.session() as session:
        row = await session.get(SessionRow, user.session_id)
        if row is not None and row.revoked_at is None:
            from datetime import UTC, datetime

            row.revoked_at = datetime.now(UTC)
    await record_event(
        uow.session_factory,
        "logout",
        user_id=user.id,
        object_type="user",
        object_id=user.username,
    )
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.status_code = 204
    return response


@router.get("/me", response_model=MeView)
async def me(user: AuthUser = Depends(current_user)) -> MeView:
    """Return the authenticated identity and CSRF token.

    Args:
        user: The authenticated identity.

    Returns:
        The identity view (the CSRF token is same-origin-readable by
        design; the session cookie itself is HttpOnly).
    """
    return MeView(
        username=user.username,
        display_name=user.display_name,
        roles=user.roles,
        csrf_token=user.csrf_token,
    )


__all__ = ["CSRF_HEADER", "router"]
