"""Request-scoped dependencies: config, season-bound repository (§4.4).

Each request gets one fresh ``UnitOfWork`` session; the season path
parameter resolves inside that session so the repository is always
season-bound (no cross-season IDOR).  Repository write methods commit
their own short transactions; the dependency exit commits nothing new.
"""

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from fastapi import Request
from sqlalchemy import select

from ccat.api.errors import ApiError
from ccat.db.models import Season
from ccat.db.repositories import SeasonStoreRepository, ensure_season_row

if TYPE_CHECKING:
    from ccat.config import Config
    from ccat.db.uow import UnitOfWork


def get_cfg(request: Request) -> "Config":
    """Return the app's configuration.

    Args:
        request: The current request.

    Returns:
        The pipeline configuration stored on ``app.state``.
    """
    return request.app.state.cfg


def get_uow(request: Request) -> "UnitOfWork":
    """Return the app's unit of work.

    Args:
        request: The current request.

    Returns:
        The shared engine/session factory wrapper on ``app.state``.
    """
    return request.app.state.uow


async def season_repo(
    season: str, request: Request
) -> AsyncIterator[SeasonStoreRepository]:
    """Yield a repository bound to the named season row.

    The configured season's row is created on demand.  That row holds
    configuration-derived data only (name, Christmas date, config
    snapshot), so a migrated database that has not been imported into
    yet still serves the web app.  Any other season is refused: it is
    not the configured one, so there is nothing to derive it from.

    Args:
        season: The season name path parameter.
        request: The current request.

    Yields:
        A :class:`SeasonStoreRepository` on one fresh session.

    Raises:
        ApiError: 404 when the season row does not exist and the
            season is not the configured one.
    """
    uow: UnitOfWork = request.app.state.uow
    cfg: Config = request.app.state.cfg
    async with uow.session() as session:
        row = await session.scalar(select(Season).where(Season.name == season))
        if row is None and season == cfg.season:
            row = await _create_season_row(uow, cfg)
        if row is None:
            raise ApiError(
                404, "season_not_found", f"season {season!r} does not exist"
            )
        yield SeasonStoreRepository(session, row)


async def _create_season_row(uow: "UnitOfWork", cfg: "Config") -> Season:
    """Create the configured season row in its own committed transaction.

    A separate transaction makes the row visible to every later
    session; the request's own session holds an older read snapshot
    and would not see the new row.

    Args:
        uow: The shared unit of work.
        cfg: The pipeline configuration (season name and date).

    Returns:
        The created season row.
    """
    async with uow.session() as session:
        return await ensure_season_row(session, cfg)


def session_factory_dep(request: Request) -> Any:
    """Return the app's async session factory (jobs service calls).

    Args:
        request: The current request.

    Returns:
        The ``async_sessionmaker`` from ``app.state.uow``.
    """
    return request.app.state.uow.session_factory


__all__ = ["get_cfg", "get_uow", "season_repo", "session_factory_dep"]
