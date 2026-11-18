"""Alembic environment for the CCAT database.

Migrations run through the SYNC engine/dialect (Alembic does not
support async engines here); the FastAPI runtime uses the async
engine from ``ccat.db.engine.create_async_engine``.  Both must keep
working against the same schema (plan §4.5).

URL resolution: the ``CCAT_DATABASE_URL`` environment variable (sync
or async spelling) wins; otherwise the ``sqlalchemy.url`` entry from
``alembic.ini`` / a programmatically injected value; otherwise the
config default ``sqlite:///./output/ccat.sqlite3``.
"""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from ccat.db.engine import sync_url_from
from ccat.db.models import Base

# The alembic.ini loggers (root/sqlalchemy/alembic) only exist when
# alembic runs from the ini file; programmatic Config() has none.
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

_DEFAULT_URL = "sqlite:///./output/ccat.sqlite3"


def _resolve_url() -> str:
    """Pick the sync database URL (env var > config value > default)."""
    url = os.environ.get("CCAT_DATABASE_URL") or config.get_main_option(
        "sqlalchemy.url"
    )
    return sync_url_from(url or _DEFAULT_URL)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DBAPI connection."""
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live sync connection."""
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _resolve_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
