"""Admin user management routes (refactor §5.2, §8, D1)."""

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from ccat.api.auth import ROLES, AuthUser, record_event, require_csrf
from ccat.api.auth import require_roles
from ccat.api.dto import UserCreate, UserPatch, UserView
from ccat.api.errors import ApiError
from ccat.db.models import User

if TYPE_CHECKING:
    from ccat.db.uow import UnitOfWork

router = APIRouter(
    prefix="/users",
    tags=["users"],
    dependencies=[Depends(require_roles("admin"))],
)


def _view(user: User) -> UserView:
    """Render one user row.

    Args:
        user: The user ORM row.

    Returns:
        The user view.
    """
    return UserView(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        active=user.active,
        roles=list(user.roles or []),
        dev_no_password=user.dev_no_password,
    )


async def _get_user(session: Any, user_id: int) -> User:
    """Load one user row or raise 404.

    Args:
        session: The async session.
        user_id: The user id.

    Returns:
        The user row.

    Raises:
        ApiError: 404 when absent.
    """
    row = await session.get(User, user_id)
    if row is None:
        raise ApiError(404, "user_not_found", f"user {user_id} not found")
    return row


@router.get("", response_model=list[UserView])
async def list_users(request: Request) -> list[UserView]:
    """List all staff accounts (admin only).

    Args:
        request: The current request.

    Returns:
        Every user.
    """
    uow: UnitOfWork = request.app.state.uow
    async with uow.session() as session:
        rows = (await session.scalars(select(User).order_by(User.id))).all()
        return [_view(row) for row in rows]


@router.post("", response_model=UserView, status_code=201)
async def create_user(
    body: UserCreate,
    request: Request,
    _admin: AuthUser = Depends(require_roles("admin")),
    _csrf: None = Depends(require_csrf),
) -> UserView:
    """Create one staff account (admin only; audited).

    Args:
        body: The account fields.
        request: The current request.
        _admin: Admin guard.
        _csrf: CSRF guard.

    Returns:
        The created account view.
    """
    uow: UnitOfWork = request.app.state.uow
    async with uow.session() as session:
        exists = await session.scalar(
            select(User).where(User.username == body.username)
        )
        if exists is not None:
            raise ApiError(
                409, "conflict", f"user {body.username!r} already exists"
            )
        if any(role not in ROLES for role in body.roles):
            raise ApiError(422, "invalid_input", "unknown role name")
        user = User(
            username=body.username,
            display_name=body.display_name,
            roles=list(body.roles),
            active=body.active,
            dev_no_password=False,
        )
        session.add(user)
        await session.flush()
        view = _view(user)
        new_username = user.username
    await record_event(
        uow.session_factory,
        "user_create",
        user_id=_admin.id,
        object_type="user",
        object_id=new_username,
        details={"roles": list(body.roles)},
    )
    return view


@router.patch("/{user_id}", response_model=UserView)
async def patch_user(
    user_id: int,
    body: UserPatch,
    request: Request,
    _admin: AuthUser = Depends(require_roles("admin")),
    _csrf: None = Depends(require_csrf),
) -> UserView:
    """Patch one account's display name, active flag, or roles (admin).

    An admin cannot deactivate their own account (lockout guard).

    Args:
        user_id: The target user id.
        body: The patch (omitted fields unchanged).
        request: The current request.
        _admin: Admin guard.
        _csrf: CSRF guard.

    Returns:
        The updated account view.

    Raises:
        ApiError: 404 unknown user; 409 self-deactivation.
    """
    uow: UnitOfWork = request.app.state.uow
    async with uow.session() as session:
        user = await _get_user(session, user_id)
        provided = body.model_fields_set
        if "display_name" in provided:
            user.display_name = body.display_name
        if "roles" in provided and body.roles is not None:
            if any(role not in ROLES for role in body.roles):
                raise ApiError(422, "invalid_input", "unknown role name")
            user.roles = list(body.roles)
        if "active" in provided and body.active is not None:
            if not body.active and user.id == _admin.id:
                raise ApiError(
                    409, "conflict", "you cannot deactivate your own account"
                )
            user.active = body.active
        session.add(user)
        view = _view(user)
        target_username = user.username
    await record_event(
        uow.session_factory,
        "user_update",
        user_id=_admin.id,
        object_type="user",
        object_id=target_username,
        details={"fields": sorted(provided)},
    )
    return view


__all__ = ["router"]
