"""Async unit-of-work facade over one engine (plan §4.4).

Request/job code opens a :meth:`UnitOfWork.session` context for one
transaction — success commits, any exception rolls back — or the
:meth:`UnitOfWork.store` convenience for a repository bound to a fresh
session with the season already ensured.  Repository write methods
commit their own short transactions, so a context exit commit is a
no-op there; the rollback path still guards staged-but-uncommitted
work (for example a season ensure followed by a failing read).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING


from ccat.db.engine import async_session_factory
from ccat.db.repositories import SeasonStoreRepository, ensure_season_row

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from collections.abc import AsyncGenerator
    from sqlalchemy.ext.asyncio import AsyncEngine

    from ccat.config import Config


class UnitOfWork:
    """One async engine plus its session factory."""

    def __init__(self, engine: AsyncEngine) -> None:
        """Initialize the unit of work.

        Args:
            engine: An async engine from
                :func:`ccat.db.engine.create_async_engine`.
        """
        self.engine = engine
        self.session_factory = async_session_factory(engine)

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession]:
        """Yield one session committed on success, rolled back on error.

        Yields:
            A fresh ``AsyncSession``; the surrounding transaction
            commits when the block exits normally and rolls back on
            any exception.
        """
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    @asynccontextmanager
    async def store(
        self, cfg: Config, actor: str
    ) -> AsyncGenerator[SeasonStoreRepository]:
        """Yield a repository on a fresh session with the season ensured.

        Args:
            cfg: Pipeline configuration (season identity and snapshot).
            actor: Attribution identity for the session's writes.

        Yields:
            A :class:`SeasonStoreRepository` bound to the session; the
            season row is created/refreshed idempotently first.
        """
        async with self.session() as session:
            season = await ensure_season_row(session, cfg)
            yield SeasonStoreRepository(session, season)


__all__ = ["UnitOfWork"]
