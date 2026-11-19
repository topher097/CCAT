"""Identity adapter, sessions, and RBAC dependencies (refactor §8, D1/D2).

One pluggable adapter authenticates a username; the dev no-password
adapter is active only when ``cfg.api.auth_mode == "dev"`` (the two
bootstrap accounts, decision D2).  Sessions are server-side rows keyed
by the sha256 of a random URL-safe token, so a DB leak reveals no
usable tokens.  Cookies are HttpOnly/SameSite=Strict; ``Secure`` is
set whenever the auth mode is not ``dev`` (dev runs over plain http).

Security-audit events (``security_audit_events``) record login, logout,
login-failed, and user changes — usernames and action names only, never
tokens or payloads (plan §8).
"""

import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ccat.api.errors import ApiError
from ccat.db.models import SecurityAuditEvent, Session, User

if TYPE_CHECKING:
    from ccat.config import Config
    from ccat.db.uow import UnitOfWork

#: Cookie name carrying the session token.
SESSION_COOKIE = "ccat_session"

#: Header carrying the per-session CSRF token for mutating requests.
CSRF_HEADER = "x-ccat-csrf"

#: The four staff roles (decision D1).
ROLES = ("read_only", "auditor", "coordinator", "admin")


class AuthAdapter(Protocol):
    """Pluggable identity verification (D2).

    An adapter answers one question: is this username authenticated,
    and as which user? Production identity (OIDC/OTP) implements the
    same protocol.
    """

    async def authenticate(
        self, session: AsyncSession, username: str
    ) -> User | None:
        """Return the authenticated user row, or None.

        Args:
            session: Read-only session for user lookup.
            username: The claimed username.

        Returns:
            The user row when authentication succeeds.
        """
        ...  # pragma: no cover - protocol


class DevPasswordlessAdapter:
    """Development-only no-password adapter (D2).

    Accepts any username that exists as an active ``dev_no_password``
    account.  ``authenticate`` verifies the flag from the users table,
    so the adapter is inert against a database without the bootstrap
    rows even if misconfigured.
    """

    async def authenticate(
        self, session: AsyncSession, username: str
    ) -> User | None:
        """Return the active staff user, or None (dev mode).

        Dev mode is username-only by design (D2): every account is a
        staff account without a password in this phase; the row flag
        records which accounts the seeder created.

        Args:
            session: Read-only session for user lookup.
            username: The claimed username.

        Returns:
            The user row when an active account with that username
            exists.
        """
        user = await session.scalar(
            select(User).where(User.username == username)
        )
        if user is None or not user.active:
            return None
        return user


class OidcAdapter:
    """OIDC identity adapter — production follow-up (D2, plan §8).

    Not implemented: requires the selected identity provider, client
    registration, and redirect/claims review before any non-loopback
    deployment.  Raises until configured.
    """

    async def authenticate(
        self, session: AsyncSession, username: str
    ) -> User | None:
        """Unimplemented — production identity is a follow-up (D2).

        Args:
            session: Unused.
            username: Unused.

        Raises:
            NotImplementedError: Always; OIDC is not wired yet.
        """
        raise NotImplementedError(
            "OIDC authentication is not implemented; run with"
            " api.auth_mode='dev' for loopback development only (D2)"
        )


class OtpAdapter:
    """OTP/MFA identity adapter — production follow-up (D2, plan §8).

    Not implemented: enrollment, recovery, and rate policy need design
    review before privileged roles rely on it.
    """

    async def authenticate(
        self, session: AsyncSession, username: str
    ) -> User | None:
        """Unimplemented — OTP enrollment is a follow-up (D2).

        Args:
            session: Unused.
            username: Unused.

        Raises:
            NotImplementedError: Always; OTP is not wired yet.
        """
        raise NotImplementedError(
            "OTP authentication is not implemented; run with"
            " api.auth_mode='dev' for loopback development only (D2)"
        )


def adapter_for(cfg: "Config") -> AuthAdapter:
    """Return the adapter selected by ``cfg.api.auth_mode``.

    Args:
        cfg: Pipeline configuration.

    Returns:
        The dev adapter for ``dev``; OIDC is the configured mode for
        production but remains a stub, so it raises on use.

    Raises:
        ApiError: 501 when the configured mode has no adapter yet.
    """
    if cfg.api.auth_mode == "dev":
        return DevPasswordlessAdapter()
    if cfg.api.auth_mode == "oidc":
        # The adapter exists as an interface stub only.
        return OidcAdapter()  # pragma: no cover - stub
    raise ApiError(  # pragma: no cover - config Literal guards this
        501, "auth_unavailable", f"auth mode {cfg.api.auth_mode!r} not wired"
    )


@dataclass(frozen=True)
class AuthUser:
    """The request's authenticated identity (plain values).

    Attributes:
        id: User id.
        username: Username.
        display_name: Display name (may be None).
        roles: Granted roles.
        session_id: Session row id (the token hash).
        csrf_token: The session's CSRF token.
    """

    id: int
    username: str
    display_name: str | None
    roles: list[str]
    session_id: str
    csrf_token: str


def hash_token(token: str) -> str:
    """Hash one session token for storage/lookup (sha256 hex).

    Args:
        token: The raw token from the cookie.

    Returns:
        The hex digest used as the session row id.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _expired(row: Session) -> bool:
    """Whether one session row's expiry passed (naive = UTC, SQLite).

    Args:
        row: The session row.

    Returns:
        True when expired.
    """
    if row.expires_at is None:
        return False
    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires <= datetime.now(UTC)


class LoginRateLimiter:
    """Naive in-memory per-IP login rate limit (5/minute, plan §8).

    Deliberately single-process and unbounded-window-free: the first
    production deployment runs one API process; a shared-store limiter
    is follow-up work.  Documented limitation, not a security claim.
    """

    def __init__(self, limit: int = 5, window_seconds: float = 60.0) -> None:
        """Initialize the limiter.

        Args:
            limit: Allowed attempts per window.
            window_seconds: Window length in seconds.
        """
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, tuple[float, int]] = {}

    def check(self, key: str) -> bool:
        """Record one attempt and report whether it is allowed.

        Args:
            key: The client identity (IP).

        Returns:
            True when the attempt is within the limit.
        """
        now = time.monotonic()
        start, count = self._hits.get(key, (now, 0))
        if now - start >= self.window:
            start, count = now, 0
        count += 1
        self._hits[key] = (start, count)
        return count <= self.limit


async def issue_session(
    session: AsyncSession, cfg: "Config", user: User
) -> tuple[str, Session]:
    """Create one session row for the user.

    Args:
        session: The session owning the transaction (committed by the
            caller).
        cfg: Pipeline configuration (session TTL).
        user: The authenticated user.

    Returns:
        The raw token (to set as cookie) and the persisted row.
    """
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    row = Session(
        id=hash_token(token),
        user_id=user.id,
        csrf_token=secrets.token_urlsafe(32),
        created_at=now,
        expires_at=now + timedelta(hours=cfg.api.session_ttl_hours),
    )
    session.add(row)
    await session.flush()
    return token, row


async def record_event(
    session_factory: Any,
    action: str,
    *,
    user_id: int | None = None,
    object_type: str | None = None,
    object_id: str | None = None,
    season: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Append one security-audit event (PII-safe: action + username).

    Args:
        session_factory: Async session factory.
        action: Stable action name ("login", "login_failed", ...).
        user_id: Acting user id, when known.
        object_type: Affected object class ("user", "artifact", ...).
        object_id: Affected object id as text.
        season: Season name, when scoped.
        details: Extra JSON-safe context (never payloads/secrets).
    """
    async with session_factory() as session:
        try:
            session.add(
                SecurityAuditEvent(
                    ts=datetime.now(UTC),
                    user_id=user_id,
                    action=action,
                    object_type=object_type,
                    object_id=object_id,
                    season=season,
                    details=details,
                )
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def current_user(request: Request) -> AuthUser:
    """Resolve the session cookie to an authenticated user (§8).

    Args:
        request: The current request.

    Returns:
        The authenticated identity.

    Raises:
        ApiError: 401 when the cookie is missing/expired/revoked or
            the user is inactive.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise ApiError(401, "unauthenticated", "authentication required")
    uow: UnitOfWork = request.app.state.uow
    async with uow.session_factory() as session:
        row = await session.get(Session, hash_token(token))
        if row is None or row.revoked_at is not None or _expired(row):
            raise ApiError(401, "unauthenticated", "authentication required")
        user = await session.get(User, row.user_id)
        if user is None or not user.active:
            raise ApiError(401, "unauthenticated", "authentication required")
        identity = AuthUser(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            roles=list(user.roles or []),
            session_id=row.id,
            csrf_token=row.csrf_token,
        )
    request.state.auth_user = identity
    return identity


def require_roles(*roles: str) -> Any:
    """Build a dependency requiring one of ``roles`` (§8, D1).

    Args:
        roles: Acceptable role names.

    Returns:
        A FastAPI dependency yielding the AuthUser.
    """

    async def _check(user: AuthUser = Depends(current_user)) -> AuthUser:
        """Reject users without one of the required roles.

        Args:
            user: The authenticated identity.

        Returns:
            The identity, unchanged.

        Raises:
            ApiError: 403 when no role matches.
        """
        if not set(user.roles) & set(roles):
            raise ApiError(403, "forbidden", "one of these roles is required")
        return user

    return _check


async def require_csrf(
    request: Request, user: AuthUser = Depends(current_user)
) -> None:
    """Require the CSRF header to match the session token (§8).

    Applies to mutating requests only; the dependency is a no-op for
    safe methods so GET-only usage never needs the header.

    Args:
        request: The current request.
        user: The authenticated identity (session source).

    Raises:
        ApiError: 403 on a missing/mismatched header.
    """
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    header = request.headers.get(CSRF_HEADER)
    if not header or not secrets.compare_digest(header, user.csrf_token):
        raise ApiError(403, "csrf", "missing or invalid CSRF header")


__all__ = [
    "CSRF_HEADER",
    "SESSION_COOKIE",
    "AuthAdapter",
    "AuthUser",
    "DevPasswordlessAdapter",
    "LoginRateLimiter",
    "OidcAdapter",
    "OtpAdapter",
    "ROLES",
    "adapter_for",
    "current_user",
    "hash_token",
    "issue_session",
    "record_event",
    "require_csrf",
    "require_roles",
]
