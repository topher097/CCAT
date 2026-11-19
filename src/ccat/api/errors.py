"""Exception → HTTP error mapping (refactor §5.2).

Every error response carries the same shape::

    {"detail": {"code": ..., "message": ..., ...context}}

No stack traces, local paths, or provider payloads ever reach the body;
unexpected exceptions log server-side and return a safe generic 500.
"""

import logging
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ccat.audit import AuditError
from ccat.clarify.extract import ClarifyError
from ccat.db.artifacts import ArtifactError
from ccat.db.errors import (
    DbStoreError,
    EntityRemovalError,
    StaleRowVersionError,
)
from ccat.jobs.errors import JobConflictError, JobError, JobNotFound
from ccat.screening import ScreeningError
from ccat.services.db_clarify import StaleDraftError
from ccat.zite import ZiteError

logger = logging.getLogger("ccat.api")


class ApiError(Exception):
    """A deliberate HTTP-mapped failure raised by routes/deps.

    Attributes:
        status: HTTP status code.
        code: Stable machine-readable code.
        message: Safe human-readable message.
        context: Extra JSON-safe context fields.
    """

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        **context: Any,
    ) -> None:
        """Initialize the error.

        Args:
            status: HTTP status code.
            code: Stable machine-readable code.
            message: Safe human-readable message.
            context: Extra JSON-safe context fields.
        """
        self.status = status
        self.code = code
        self.message = message
        self.context = context
        super().__init__(message)


def _body(code: str, message: str, context: dict[str, Any]) -> dict[str, Any]:
    """Assemble the ``{"detail": ...}`` response body."""
    detail: dict[str, Any] = {"code": code, "message": message}
    detail.update(context)
    return {"detail": detail}


def _response(
    status: int, code: str, message: str, context: dict[str, Any]
) -> JSONResponse:
    """Render one mapped error response."""
    return JSONResponse(
        status_code=status, content=_body(code, message, context)
    )


def _domain_status(error: Exception) -> tuple[int, str]:
    """Map one typed domain error to (status, code).

    Version mismatches use 412 (documented choice over 428/409, plan
    §4.4/§5.2); state/job conflicts 409; absent objects 404; invalid
    input 422.
    """
    if isinstance(error, StaleRowVersionError | StaleDraftError):
        return 412, "stale_version"
    if isinstance(error, EntityRemovalError | JobConflictError):
        return 409, "conflict"
    if isinstance(error, JobNotFound):
        return 404, "job_not_found"
    if isinstance(error, JobError):
        return 422, "invalid_job"
    if isinstance(error, ScreeningError):
        # Inline screening errors are rejected request options (§9.2).
        return 422, "invalid_input"
    if isinstance(error, ArtifactError):
        # Unknown/missing files are 404; a path-escape attempt is a
        # forbidden request (and never echoed back verbatim).
        if "escapes the artifact root" in str(error):
            return 403, "forbidden"
        return 404, "artifact_not_found"
    message = str(error)
    # Service errors phrased "unknown <entity> <key>" mean an absent
    # object; everything else is a state refusal (409).
    if message.startswith("unknown ") or "has no draft" in message:
        return 404, "not_found"
    return 409, "conflict"


def register_error_handlers(app: Any) -> None:
    """Install the exception handlers on one FastAPI app.

    Args:
        app: The FastAPI application.
    """

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, error: ApiError) -> JSONResponse:
        """Render a deliberate API failure."""
        return _response(error.status, error.code, error.message, error.context)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(
        request: Request, error: StarletteHTTPException
    ) -> JSONResponse:
        """Normalize framework HTTP errors (404 routing, 405, ...)."""
        return _response(error.status_code, "http_error", str(error.detail), {})

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        """Map pydantic/fastapi validation failures to 422 (§5.2)."""
        return _response(
            422,
            "invalid_input",
            "request body or parameters are invalid",
            {"errors": error.errors()},
        )

    @app.exception_handler(DbStoreError)
    async def _store_error(
        request: Request, error: DbStoreError
    ) -> JSONResponse:
        """Map repository errors (404/409/412 by class)."""
        status, code = _domain_status(error)
        context: dict[str, Any] = {}
        if isinstance(error, StaleRowVersionError):
            context["entity"] = error.entity
            context["key"] = str(error.key)
            context["current_version"] = error.current_version
        return _response(status, code, str(error), context)

    @app.exception_handler(AuditError)
    async def _audit_error(request: Request, error: AuditError) -> JSONResponse:
        """Map audit domain errors (404/409 per message)."""
        status, code = _domain_status(error)
        return _response(status, code, str(error), {})

    @app.exception_handler(ClarifyError)
    async def _clarify_error(
        request: Request, error: ClarifyError
    ) -> JSONResponse:
        """Map clarify domain errors (404/409 per message)."""
        status, code = _domain_status(error)
        return _response(status, code, str(error), {})

    @app.exception_handler(ScreeningError)
    async def _screening_error(
        request: Request, error: ScreeningError
    ) -> JSONResponse:
        """Map screening domain errors (invalid input → 409/422)."""
        status, code = _domain_status(error)
        return _response(status, code, str(error), {})

    @app.exception_handler(JobError)
    async def _job_error(request: Request, error: JobError) -> JSONResponse:
        """Map job-layer errors (404/409/422 by class, §6.1)."""
        status, code = _domain_status(error)
        return _response(status, code, str(error), {})

    @app.exception_handler(ZiteError)
    async def _provider_error(
        request: Request, error: ZiteError
    ) -> JSONResponse:
        """Map upstream provider failures to 502 (§5.2)."""
        return _response(502, "upstream_error", "upstream provider failure", {})

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, error: Exception) -> JSONResponse:
        """Log and hide unexpected failures (safe generic 500)."""
        logger.exception(
            "unhandled error on %s %s", request.method, request.url.path
        )
        return _response(500, "internal_error", "internal server error", {})
