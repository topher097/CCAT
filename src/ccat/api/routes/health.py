"""Health and readiness probes (refactor §5.2).

``/healthz`` is liveness only.  ``/readyz`` checks the database, the
migration head, and artifact-root writability — never external
providers, and no internals leak into the body.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from sqlalchemy import text

from ccat.api.dto import HealthView
from ccat.db.engine import ALEMBIC_DIR

if TYPE_CHECKING:
    from ccat.config import Config

router = APIRouter(tags=["health"])


def _alembic_head() -> str | None:
    """Return the alembic script head revision.

    Returns:
        The head revision string, or None when unreadable.
    """
    try:
        from alembic.script import ScriptDirectory

        return ScriptDirectory(str(ALEMBIC_DIR)).get_current_head()
    except Exception:  # noqa: BLE001 - readiness must not 500
        return None


def _artifact_root_writable(cfg: "Config") -> bool:
    """Probe the artifact root with a create/remove touch.

    Args:
        cfg: Pipeline configuration (artifact root).

    Returns:
        True when a probe file can be created and removed.
    """
    try:
        root = Path(cfg.storage.artifact_root)
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".readyz-probe"
        probe.touch()
        probe.unlink()
    except OSError:
        return False
    return True


@router.get("/healthz", response_model=HealthView)
async def healthz() -> HealthView:
    """Liveness: the process serves requests; no dependency checks."""
    return HealthView(status="ok")


@router.get("/readyz")
async def readyz(request: Request) -> dict[str, Any]:
    """Readiness: DB reachable, migration at head, artifact root writable.

    Args:
        request: The current request.

    Returns:
        ``{"status": "ok", ...checks}`` or a 503 body with the failed
        checks (check names only, no internals).
    """
    cfg: Config = request.app.state.cfg
    checks = {
        "database": False,
        "migration_head": False,
        "artifact_root": False,
    }
    try:
        uow = request.app.state.uow
        async with uow.session_factory() as session:
            stored = (
                await session.execute(
                    text("SELECT version_num FROM alembic_version LIMIT 1")
                )
            ).scalar_one_or_none()
        checks["database"] = True
        head = _alembic_head()
        checks["migration_head"] = head is not None and stored == head
    except Exception:  # noqa: BLE001 - readiness probe
        checks["database"] = False
    checks["artifact_root"] = _artifact_root_writable(cfg)
    if all(checks.values()):
        return {"status": "ok", "checks": checks}
    return _not_ready(checks)


def _not_ready(checks: dict[str, bool]) -> Any:
    """Render the not-ready body (503, check names only).

    Args:
        checks: The per-check results.

    Returns:
        A 503 JSON response.
    """
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=503,
        content={
            "detail": {
                "code": "not_ready",
                "message": "readiness checks failed",
                "checks": checks,
            }
        },
    )


__all__ = ["router"]
