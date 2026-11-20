"""The FastAPI application (refactor §5, Phase 3).

DB mode only (plan §2): the API never touches the workbook.
``create_app(cfg)`` builds the versioned app (``/api/v1``) with
request-scoped async sessions, the pluggable auth adapter, consistent
error mapping, redacted request logging, and the static-frontend hook.
``lifespan`` disposes the engine on shutdown; there is deliberately no
startup reconciliation — reconciliation is an explicit job (plan
§1.1/§9.3).
"""

import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from collections.abc import AsyncGenerator, Callable

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ccat.api.auth import LoginRateLimiter
from ccat.api.errors import register_error_handlers
from ccat.api.routes import (
    artifacts as artifacts_routes,
)
from ccat.api.routes import (
    audit as audit_routes,
)
from ccat.api.routes import (
    auth as auth_routes,
)
from ccat.api.routes import (
    clarify as clarify_routes,
)
from ccat.api.routes import (
    config as config_routes,
)
from ccat.api.routes import (
    dbconsole as dbconsole_routes,
)
from ccat.api.routes import (
    diagnostics as diagnostics_routes,
)
from ccat.api.routes import (
    health as health_routes,
)
from ccat.api.routes import (
    jobs as jobs_routes,
)
from ccat.api.routes import (
    seasons as seasons_routes,
)
from ccat.api.routes import (
    users as users_routes,
)
from ccat.db.engine import ROOT, create_async_engine
from ccat.db.uow import UnitOfWork

if TYPE_CHECKING:
    from ccat.config import Config

logger = logging.getLogger("ccat.api")

#: Frontend build directory under the deployment root (Phase 4 fills it).
FRONTEND_DIST = ROOT / "frontend" / "dist"

API_PREFIX = "/api/v1"


def create_app(cfg: "Config", config_path: str | Path | None = None) -> FastAPI:
    """Build the FastAPI application for one configuration.

    Args:
        cfg: The pipeline configuration (season, storage, api specs).
        config_path: The configuration file the app was started with.
            The admin settings UI reads and writes that file; without
            it the configuration endpoints refuse to run.

    Returns:
        The configured app (docs at ``/api/v1/docs``); the shared
        engine lives on ``app.state`` and is disposed at shutdown.
    """
    from ccat.api.deps import get_cfg  # noqa: F401 - route dependency

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        """Seed bootstrap users; dispose the engine at shutdown.

        Seeding is idempotent and best-effort (an unmigrated database
        surfaces its migration error on first use instead); there is
        deliberately no startup reconciliation (§1.1).
        """
        try:
            from ccat.db.engine import create_sync_engine
            from ccat.db.seed import seed_bootstrap

            seed_bootstrap(
                create_sync_engine(
                    cfg.storage.database_url.replace(
                        "sqlite+aiosqlite", "sqlite"
                    )
                )
            )
        except Exception:  # noqa: BLE001 - unmigrated DB fails later
            logger.warning(
                "bootstrap user seeding skipped (migrations not run?)",
                exc_info=True,
            )
        yield
        engine = getattr(app.state.uow, "engine", None)
        if engine is not None:
            await engine.dispose()

    app = FastAPI(
        title="CCAT API",
        version="0.1.0",
        docs_url=f"{API_PREFIX}/docs",
        openapi_url=f"{API_PREFIX}/openapi.json",
        lifespan=lifespan,
    )
    app.state.cfg = cfg
    app.state.config_path = str(config_path) if config_path else None
    # Created eagerly (lazy-connecting engine) so ASGI transports that
    # skip lifespan still have it; disposal happens on shutdown.
    app.state.uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
    app.state.login_limiter = LoginRateLimiter()
    app.middleware("http")(_redact_and_log)
    register_error_handlers(app)
    from ccat.api.auth import current_user

    for router, guard in (
        (health_routes.router, None),
        (auth_routes.router, None),
        (users_routes.router, current_user),
        (seasons_routes.router, current_user),
        (diagnostics_routes.router, current_user),
        (jobs_routes.router, current_user),
        (artifacts_routes.router, current_user),
        (clarify_routes.router, current_user),
        (audit_routes.router, current_user),
        (config_routes.router, current_user),
        (dbconsole_routes.router, current_user),
    ):
        # Router-level auth runs before route dependencies (season
        # resolution), so an anonymous request is always 401 (§8).
        # The auth router is excluded: login itself is unauthenticated;
        # logout/me carry their own current_user dependencies.
        dependencies = [Depends(guard)] if guard else []
        app.include_router(router, prefix=API_PREFIX, dependencies=dependencies)
    _mount_frontend(app)
    return app


async def _redact_and_log(
    request: Request, call_next: Callable[..., Any]
) -> Any:
    """Log one request without any payload content (plan §8).

    Assigns a request id and logs method/path/status/duration/user/
    roles.  Never logs bodies, entity payloads, emails, or AI content.

    Args:
        request: The current request.
        call_next: The rest of the stack.

    Returns:
        The downstream response.
    """
    request_id = uuid.uuid4().hex[:12]
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "%s %s request_id=%s unhandled",
            request.method,
            request.url.path,
            request_id,
        )
        raise
    duration_ms = (time.perf_counter() - started) * 1000
    user = getattr(request.state, "auth_user", None)
    logger.info(
        "%s %s status=%s duration_ms=%.1f request_id=%s user=%s roles=%s",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
        request_id,
        getattr(user, "username", None),
        getattr(user, "roles", None),
    )
    response.headers["X-Request-Id"] = request_id
    return response


def _mount_frontend(app: FastAPI) -> None:
    """Mount the built SPA when ``frontend/dist`` exists (Phase 4 hook).

    Without a build, ``/`` answers a 404 placeholder.  With one,
    non-``/api`` paths fall back to ``index.html`` (SPA routing).

    Args:
        app: The FastAPI application.
    """
    if FRONTEND_DIST.is_dir():
        app.mount(
            "/assets",
            StaticFiles(directory=FRONTEND_DIST / "assets"),
            name="assets",
        )

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str) -> Any:
            """Serve a dist file or the SPA shell.

            Args:
                path: The requested path.

            Returns:
                The file response or index.html.
            """
            from fastapi.responses import FileResponse

            candidate = (FRONTEND_DIST / path).resolve()
            if candidate.is_file() and candidate.is_relative_to(
                FRONTEND_DIST.resolve()
            ):
                return FileResponse(candidate)
            return FileResponse(FRONTEND_DIST / "index.html")

    else:

        @app.get("/", include_in_schema=False)
        async def root_placeholder() -> JSONResponse:
            """Answer the bare root until Phase 4 ships the SPA.

            Returns:
                A 404 JSON placeholder.
            """
            return JSONResponse(
                status_code=404,
                content={
                    "detail": {
                        "code": "no_frontend",
                        "message": "frontend build not present (Phase 4)",
                    }
                },
            )


def serve(cfg: "Config", config_path: str | Path | None = None) -> None:
    """Run the API server with uvicorn (``ccat serve``).

    Args:
        cfg: Pipeline configuration (api.host/api.port).
        config_path: The configuration file the server was started
            with; the admin settings UI edits it.
    """
    import uvicorn

    uvicorn.run(
        create_app(cfg, config_path=config_path),
        host=cfg.api.host,
        port=cfg.api.port,
        reload=False,
    )


__all__ = ["create_app", "serve"]
