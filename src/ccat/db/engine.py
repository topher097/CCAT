"""Engine/session construction and migration runner (plan §4.4).

SQLite connections get WAL journaling, foreign-key enforcement, a
busy timeout, and NORMAL synchronous mode on every connect (both the
async runtime engine and the sync Alembic/CLI engine), set through
the SQLAlchemy event API only.  Migrations run through the SYNC
engine; the FastAPI runtime uses the async engine — both must keep
working against the same schema (see ``alembic/env.py``).
"""

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import (
    Engine,
    create_engine as _sa_create_engine,
    event,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine as _sa_create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

#: Repository root (``src/ccat/db/engine.py`` -> three levels up).
ROOT = Path(__file__).resolve().parents[3]

#: Location of the Alembic environment, relative to the repo root.
ALEMBIC_DIR = ROOT / "alembic"

#: Connection PRAGMAs applied to every SQLite connection (§4.4).
_PRAGMAS = (
    "PRAGMA foreign_keys=ON",
    "PRAGMA journal_mode=WAL",
    "PRAGMA busy_timeout=15000",
    "PRAGMA synchronous=NORMAL",
)


def sync_url_from(async_url: str) -> str:
    """Derive the sync SQLAlchemy URL from an async one.

    Args:
        async_url: URL using the ``sqlite+aiosqlite`` dialect.

    Returns:
        The equivalent ``sqlite`` dialect URL; a non-aiosqlite URL
        passes through unchanged.
    """
    return async_url.replace("sqlite+aiosqlite://", "sqlite://", 1)


def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:
    """Apply the SQLite PRAGMAs on one new DBAPI connection.

    Args:
        dbapi_connection: The raw DBAPI connection — a plain
            ``sqlite3.Connection`` on the sync engine, or SQLAlchemy's
            synchronous aiosqlite adapter on the async engine; both
            expose a working ``cursor()`` from this event context.
        _record: SQLAlchemy's connection record (unused).
    """
    cursor = dbapi_connection.cursor()
    try:
        for pragma in _PRAGMAS:
            cursor.execute(pragma)
    finally:
        cursor.close()


def create_async_engine(url: str) -> AsyncEngine:
    """Create the async engine with PRAGMAs on every connection.

    Args:
        url: Async SQLAlchemy URL (``sqlite+aiosqlite``).

    Returns:
        The engine; pair with :func:`async_session_factory`.
    """
    engine = _sa_create_async_engine(url)
    # The "connect" event fires on the sync engine inside the greenlet
    # that establishes the connection; the adapted aiosqlite cursor is
    # synchronous from that context.
    event.listen(engine.sync_engine, "connect", _set_pragmas)
    return engine


def async_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    """Create the request-scoped async session factory.

    Args:
        engine: An async engine from :func:`create_async_engine`.

    Returns:
        An ``async_sessionmaker`` with ``expire_on_commit=False`` so
        returned rows survive past the transaction boundary.
    """
    return async_sessionmaker(engine, expire_on_commit=False)


def create_sync_engine(url: str) -> Engine:
    """Create the sync engine (Alembic, CLI importer) with PRAGMAs.

    Args:
        url: Sync SQLAlchemy URL (``sqlite``).

    Returns:
        The engine with the same PRAGMAs as the async runtime path.
    """
    engine = _sa_create_engine(url)
    event.listen(engine, "connect", _set_pragmas)
    return engine


def sync_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create the sync session factory for CLI/importer code paths.

    Args:
        engine: A sync engine from :func:`create_sync_engine`.

    Returns:
        A ``sessionmaker`` with ``expire_on_commit=False``.
    """
    return sessionmaker(engine, expire_on_commit=False)


def run_migrations(url: str) -> None:
    """Programmatically upgrade the database at ``url`` to head.

    Uses the sync engine/dialect (Alembic's requirement) and the
    checked-in ``alembic`` environment under the repo root.

    Args:
        url: Sync SQLAlchemy URL; async URLs are converted.
    """
    config = AlembicConfig()
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    config.set_main_option("sqlalchemy.url", sync_url_from(url))
    command.upgrade(config, "head")
