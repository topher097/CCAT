"""Run-settings routes: read and persist a command's run flags.

The automatic matcher and the generation command are started from a
button with a pop-up menu.  The menu is seeded from the configuration
file, and a changed flag is written back to it, so the flags live in
the configuration file next to the command's other settings.

The screening command's AI flag follows the same rule: the ``/screen``
page reads it from the configuration file and writes a change back to
it, so the flag is one global setting rather than a per-run option.

The route owns the command-to-section mapping, so it cannot be talked
into writing an arbitrary part of the configuration tree.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ValidationError

from ccat.api.auth import (
    AuthUser,
    current_user,
    record_event,
    require_csrf,
    require_roles,
)
from ccat.api.dto import (
    GenerateRunSettings,
    MatchRunSettings,
    RunSettingsView,
    ScreenRunSettings,
)
from ccat.api.errors import ApiError
from ccat.api.routes.config import _config_path
from ccat.config import (
    AIScreeningSpec,
    ConfigError,
    GenerateRunSpec,
    MatchRunSpec,
    effective_config,
    save_config,
)

if TYPE_CHECKING:
    from ccat.config import Config

logger = logging.getLogger("ccat.api")

router = APIRouter(prefix="/runs", tags=["runs"])

#: Roles allowed to change the stored run flags (D7, D13).
_EDITOR_ROLES = frozenset({"coordinator", "admin"})


@dataclass(frozen=True)
class RunSettingsSpec:
    """One command's configuration section and request contract.

    Attributes:
        section: The configuration path holding the run flags.
        model: The configuration contract for that section.
        payload: The request DTO; it rejects keys outside the flags.
        per_run: Fields accepted by the job options but never written
            to the configuration file.
        leaf: When set, the write replaces this one key inside
            ``section`` instead of the whole section; the payload's
            single value is written to it.
    """

    section: tuple[str, ...]
    model: type[BaseModel]
    payload: type[BaseModel]
    per_run: tuple[str, ...] = ()
    leaf: str | None = None


#: The backend's command-to-section mapping; never derived from input.
RUN_SETTINGS: dict[str, RunSettingsSpec] = {
    "match": RunSettingsSpec(
        section=("matchmaking", "run"),
        model=MatchRunSpec,
        payload=MatchRunSettings,
    ),
    "generate": RunSettingsSpec(
        section=("generate", "run"),
        model=GenerateRunSpec,
        payload=GenerateRunSettings,
        per_run=("sponsor",),
    ),
    "screen": RunSettingsSpec(
        section=("ai_screening",),
        model=AIScreeningSpec,
        payload=ScreenRunSettings,
        leaf="overwrite_new",
    ),
}


def _spec(command: str) -> RunSettingsSpec:
    """Resolve a command to its settings spec.

    Args:
        command: The command name from the path.

    Returns:
        The command's settings spec.

    Raises:
        ApiError: 404 for a command the menu does not serve.
    """
    spec = RUN_SETTINGS.get(command)
    if spec is None:
        raise ApiError(404, "not_found", f"unknown run command {command!r}")
    return spec


def _section(cfg: "Config", section: tuple[str, ...]) -> BaseModel:
    """Resolve one nested configuration model by its section path.

    Args:
        cfg: The running configuration.
        section: The dotted path of keys to follow.

    Returns:
        The configuration model at that path.
    """
    node: Any = cfg
    for key in section:
        node = getattr(node, key)
    return node


def _may_edit(user: AuthUser) -> bool:
    """Whether the caller holds a role allowed to write the flags.

    Args:
        user: The authenticated identity.

    Returns:
        True when the menu may be offered for editing.
    """
    return bool(set(user.roles) & _EDITOR_ROLES)


def _view(
    command: str,
    spec: RunSettingsSpec,
    cfg: "Config",
    path: Path,
    editable: bool,
    *,
    saved: bool = False,
) -> RunSettingsView:
    """Build one command's run-settings response.

    Args:
        command: The command name.
        spec: The command's settings spec.
        cfg: The configuration the flags were read from.
        path: The configuration file path.
        editable: Whether the caller may change the flags.
        saved: True when the response follows a successful write.

    Returns:
        The response view.
    """
    weights: dict[str, Any] | None = None
    if command == "match":
        weights = {
            key: getattr(cfg.matchmaking, key)
            for key in ("location_weight", "age_weight", "gender_weight")
        }
    if spec.leaf is not None:
        # A leaf spec owns one flag; the view carries that flag alone.
        run: dict[str, Any] = {
            spec.leaf: getattr(_section(cfg, spec.section), spec.leaf)
        }
    else:
        section = spec.model.model_validate(_section(cfg, spec.section))
        run = section.model_dump()
    return RunSettingsView(
        command=command,
        run=run,
        weights=weights,
        config_path=str(path),
        editable=editable,
        saved=saved,
    )


def _effective_tree(
    path: Path, cfg: "Config"
) -> tuple[dict[str, Any], list[str]]:
    """Read the effective configuration tree.

    Args:
        path: The configuration file path.
        cfg: The running configuration, used as the fallback.

    Returns:
        The merged tree and the dropped keys.

    Raises:
        ApiError: 500 when the file cannot be read.
    """
    try:
        return effective_config(path, cfg)
    except ConfigError as error:
        logger.error("configuration file unreadable: %s", error)
        raise ApiError(
            500,
            "config_unreadable",
            "the configuration file could not be read",
        ) from error


def _replace_section(
    tree: dict[str, Any], section: tuple[str, ...], values: dict[str, Any]
) -> Any:
    """Replace one section of the tree and return its previous value.

    Args:
        tree: The effective configuration tree (mutated in place).
        section: The dotted path of keys to follow.
        values: The replacement value for the final key.

    Returns:
        The value the final key held before the replacement.
    """
    node: Any = tree
    for key in section[:-1]:
        node = node[key]
    before = node.get(section[-1])
    node[section[-1]] = values
    return before


def _save_tree(path: Path, tree: dict[str, Any]) -> "Config":
    """Validate and atomically save a configuration tree.

    Args:
        path: The configuration file path.
        tree: The complete configuration mapping.

    Returns:
        The validated configuration.

    Raises:
        ApiError: 422 when the tree violates the contract; 500 when the
            file cannot be written.
    """
    try:
        return save_config(path, tree)
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


@router.get(
    "/{command}/settings",
    response_model=RunSettingsView,
    response_model_exclude_none=True,
)
async def get_run_settings(
    command: str,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> RunSettingsView:
    """Return one command's run settings from the running configuration.

    Args:
        command: ``match``, ``generate``, or ``screen``.
        request: The current request.
        user: The authenticated identity (drives ``editable``).

    Returns:
        The command's effective run settings.
    """
    spec = _spec(command)
    return _view(
        command,
        spec,
        request.app.state.cfg,
        _config_path(request),
        _may_edit(user),
    )


@router.patch(
    "/{command}/settings",
    response_model=RunSettingsView,
    response_model_exclude_none=True,
    dependencies=[Depends(require_csrf)],
)
async def patch_run_settings(
    command: str,
    body: dict[str, Any],
    request: Request,
    user: AuthUser = Depends(require_roles("coordinator", "admin")),
) -> RunSettingsView:
    """Persist one command's run flags to the configuration file.

    Args:
        command: ``match``, ``generate``, or ``screen``.
        body: The command's run flags.
        request: The current request.
        user: The authenticated editor.

    Returns:
        The saved run settings.

    Raises:
        ApiError: 404 for an unknown command; 422 when the body or the
            resulting tree violates the contract; 500 when the file
            cannot be read or written.
    """
    spec = _spec(command)
    try:
        payload = spec.payload.model_validate(body)
    except ValidationError as error:
        raise ApiError(
            422,
            "invalid_input",
            "request body or parameters are invalid",
            errors=[
                {
                    "path": ".".join(str(part) for part in item["loc"]),
                    "message": item["msg"],
                }
                for item in error.errors()
            ],
        ) from error
    values = payload.model_dump()
    for field in spec.per_run:
        values.pop(field, None)
    path = _config_path(request)
    tree, _dropped = _effective_tree(path, request.app.state.cfg)
    if spec.leaf is not None:
        # A leaf spec replaces one key inside its section; the rest of
        # the section (model, prompt, other flags) stays as it is.
        section = tree
        for key in spec.section:
            section = section[key]
        before = section.get(spec.leaf)
        section[spec.leaf] = values[spec.leaf]
        after: Any = {spec.leaf: values[spec.leaf]}
    else:
        before = _replace_section(tree, spec.section, values)
        after = values
    cfg = _save_tree(path, tree)
    request.app.state.cfg = cfg
    await record_event(
        request.app.state.uow.session_factory,
        "run_settings_update",
        user_id=user.id,
        object_type="config",
        object_id=path.name,
        details={"command": command, "before": before, "after": after},
    )
    return _view(command, spec, cfg, path, True, saved=True)


__all__ = ["RUN_SETTINGS", "RunSettingsSpec", "router"]
