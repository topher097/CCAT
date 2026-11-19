"""Controlled schema-migration entry point for operators.

``ccat db-upgrade`` is the one-shot release step that moves the SQLite
database to the Alembic head (plan §13: migrations run as a controlled
step, not concurrently from every API/worker replica).

The command is idempotent: running it against an up-to-date database is a
no-op.  It never touches season data -- schema migrations and data
imports stay separate (plan §4.5).
"""

from __future__ import annotations

import logging
from pathlib import Path

from ccat.db.engine import run_migrations, sync_url_from
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ccat.config import Config

logger = logging.getLogger(__name__)


def _ensure_parent_dir(url: str) -> None:
    """Create the SQLite file's parent directory when missing.

    Args:
        url: Sync SQLAlchemy URL; only local SQLite paths are handled.
    """
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return
    path = Path(url[len(prefix) :])
    if path.parent and str(path.parent) not in {"", "."}:
        path.parent.mkdir(parents=True, exist_ok=True)


def run(cfg: Config) -> None:
    """Upgrade the configured database to the Alembic head revision.

    Args:
        cfg: Season configuration; ``cfg.storage.database_url`` selects
            the database (async URLs are converted to their sync form).

    Raises:
        ccat.db.errors.DbStoreError: When the migration fails.
    """
    url = sync_url_from(cfg.storage.database_url)
    _ensure_parent_dir(url)
    logger.info("upgrading database schema to head: %s", url)
    run_migrations(url)
    logger.info("database schema is at head")
