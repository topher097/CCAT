"""Admin configuration editing routes (admin only).

The pipeline configuration lives in one JSON file.  This router hands
the settings UI the effective tree (the file's values over the
model's defaults), validates an edited copy against the configuration
contract, writes it back atomically, and reloads the running
configuration so a change takes effect without a restart.

Reading and writing the file is deliberately separate from the
in-memory configuration: the file is the artifact operators edit, and
the in-memory copy is what the request handlers use.  The reload step
is what connects the two.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Request

from ccat.api.auth import AuthUser, record_event, require_csrf
from ccat.api.auth import require_roles
from ccat.api.errors import ApiError
from ccat.config import ConfigError, effective_config, save_config
from ccat.db.engine import create_async_engine
from ccat.db.uow import UnitOfWork

if TYPE_CHECKING:
    from ccat.config import Config

logger = logging.getLogger("ccat.api")

router = APIRouter(
    prefix="/config",
    tags=["config"],
    dependencies=[Depends(require_roles("admin"))],
)


def _config_path(request: Request) -> Path:
    """Return the configuration file the running app was started with.

    Args:
        request: The current request.

    Returns:
        The configuration file path.

    Raises:
        ApiError: 500 when the app was started without a config path.
    """
    path = getattr(request.app.state, "config_path", None)
    if not path:
        raise ApiError(
            500,
            "config_unavailable",
            "the running server has no configuration file path",
        )
    return Path(path)


async def _reload_database(
    request: Request, previous: "Config", current: "Config"
) -> None:
    """Point the app at the configured database after a URL change.

    Every other setting is read from ``app.state.cfg`` per request, so
    only the database needs its engine rebuilt.

    Args:
        request: The current request.
        previous: The configuration in force before the save.
        current: The configuration in force after the save.
    """
    if current.storage.database_url == previous.storage.database_url:
        return
    old_uow = request.app.state.uow
    request.app.state.uow = UnitOfWork(
        create_async_engine(current.storage.database_url)
    )
    await old_uow.engine.dispose()
    logger.info("configuration changed the database URL; engine swapped")


@router.get("")
async def get_config(request: Request) -> dict[str, Any]:
    """Return the configuration tree the settings UI edits.

    The tree is the file's own values over the model's defaults, so
    every known setting appears — including the ones the file leaves
    implicit — and any setting added to the file later appears without
    a frontend change.

    Args:
        request: The current request.

    Returns:
        ``{"path": ..., "config": {...}}`` with the merged tree.
    """
    path = _config_path(request)
    try:
        tree, dropped = effective_config(path, request.app.state.cfg)
    except ConfigError as error:
        logger.error("configuration file unreadable: %s", error)
        raise ApiError(
            500,
            "config_unreadable",
            "the configuration file could not be read",
        ) from error
    if dropped:
        logger.warning(
            "configuration file holds keys the contract does not know; they"
            " are not editable: %s",
            ", ".join(dropped),
        )
    return {"path": str(path), "config": tree}


@router.put("")
async def put_config(
    body: dict[str, Any],
    request: Request,
    _admin: AuthUser = Depends(require_roles("admin")),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    """Validate, save, and reload the configuration file.

    A rejected configuration leaves the file untouched: the contract is
    checked before anything is written.

    Args:
        body: The complete configuration mapping.
        request: The current request.
        _admin: Admin guard.
        _csrf: CSRF guard.

    Returns:
        ``{"path": ..., "config": {...}}`` with the saved mapping.

    Raises:
        ApiError: 422 when the configuration violates the contract;
            500 when the file cannot be written.
    """
    path = _config_path(request)
    try:
        cfg = save_config(path, body)
    except ConfigError as error:
        if error.errors:
            raise ApiError(
                422, "invalid_config", str(error), errors=error.errors
            ) from error
        logger.error("configuration file not writable: %s", error)
        raise ApiError(
            500,
            "config_write_failed",
            "the configuration file could not be written",
        ) from error
    previous = request.app.state.cfg
    request.app.state.cfg = cfg
    await record_event(
        request.app.state.uow.session_factory,
        "config_update",
        user_id=_admin.id,
        object_type="config",
        object_id=path.name,
        details={"sections": sorted(body)},
    )
    await _reload_database(request, previous, cfg)
    logger.info("configuration reloaded from %s", path.name)
    return {"path": str(path), "config": body}


__all__ = ["router"]
