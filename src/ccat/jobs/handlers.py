"""Worker handlers: job types -> DB-mode pipeline services (§6.1).

Each handler adapts one :class:`JobContext` onto the corresponding
:mod:`ccat.services.db_pipeline` service and converts typed domain
errors into job error JSON ``{code, message, retryable}``.  Per plan
§6.1: network/provider failures (Zite transport) are retryable;
validation, ownership, and conflict failures are not.  Handlers not
yet implemented (generate/stats/clarify_*) are simply absent — the
worker fails such jobs with a clear non-retryable error instead of
guessing at semantics.
"""

import logging
from typing import TYPE_CHECKING, Any

from ccat.acquire import AcquisitionError
from ccat.match import MatchError
from ccat.preparation import PreparationError
from ccat.screening import ScreeningError
from ccat.services import db_pipeline
from ccat.zite import ZiteError
from ccat.jobs.errors import JobCancelRequested
from ccat.jobs.registry import JobContext

if TYPE_CHECKING:
    from ccat.services.results import (
        AcquireResult,
        MatchResult,
        ReconcileResult,
        ScreenResult,
    )

logger = logging.getLogger(__name__)

#: Errors worth another attempt (plan §6.1 retry budgets): provider
#: transport failures.  Everything else — malformed input, ownership,
#: conflicts, config errors — fails the job immediately.
RETRYABLE_ERRORS: tuple[type[BaseException], ...] = (
    ZiteError,
    ConnectionError,
    TimeoutError,
)


def error_json(error: BaseException) -> dict[str, Any]:
    """Render one exception as job error JSON (§6.1).

    Args:
        error: The raised exception.

    Returns:
        ``{code, message, retryable}`` — the code is the exception
        class name, retryable per :data:`RETRYABLE_ERRORS`.
    """
    return {
        "code": type(error).__name__,
        "message": str(error),
        "retryable": isinstance(error, RETRYABLE_ERRORS),
    }


def _options(ctx: JobContext) -> dict[str, Any]:
    """Return the context options (never None)."""
    return dict(ctx.options or {})


async def acquire(ctx: JobContext) -> "AcquireResult":
    """Acquire job: pull Zite, replace the raw snapshot (§9.1).

    Args:
        ctx: The job context.

    Returns:
        The typed acquire result.
    """
    return await db_pipeline.acquire(
        ctx.cfg, ctx.actor, ctx.progress, ctx.cancel_check
    )


async def screen(ctx: JobContext) -> "ScreenResult":
    """Screen job: prepare, reconcile, checks, AI (§9.2).

    Args:
        ctx: The job context.

    Returns:
        The typed screen result.
    """
    options = _options(ctx)
    return await db_pipeline.screen(
        ctx.cfg, ctx.actor, options, ctx.progress, ctx.cancel_check
    )


async def match(ctx: JobContext) -> "MatchResult":
    """Match job: pools, search, atomic commit, CSV artifact (§9.4).

    Args:
        ctx: The job context.

    Returns:
        The typed match result.
    """
    options = _options(ctx)
    return await db_pipeline.match(
        ctx.cfg, ctx.actor, options, ctx.progress, ctx.cancel_check
    )


async def reconcile(ctx: JobContext) -> "ReconcileResult":
    """Reconcile job: explicit audit-backstop seeding (§1.1, §9.3).

    Args:
        ctx: The job context.

    Returns:
        The typed reconcile result.
    """
    return await db_pipeline.reconcile(
        ctx.cfg, ctx.actor, ctx.progress, ctx.cancel_check
    )


#: job_type -> async handler(ctx).  Missing types fail non-retryably
#: in the worker ("no handler registered").
HANDLERS: dict[str, Any] = {
    "acquire": acquire,
    "screen": screen,
    "match": match,
    "reconcile": reconcile,
}

#: Domain errors carried through job error JSON without re-mapping
#: (kept explicit for the retry-budget documentation).
DOMAIN_ERRORS: tuple[type[BaseException], ...] = (
    AcquisitionError,
    PreparationError,
    ScreeningError,
    MatchError,
    JobCancelRequested,
)

__all__ = [
    "DOMAIN_ERRORS",
    "HANDLERS",
    "RETRYABLE_ERRORS",
    "acquire",
    "error_json",
    "match",
    "reconcile",
    "screen",
]
