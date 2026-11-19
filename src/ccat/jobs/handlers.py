"""Worker handlers: job types -> DB-mode pipeline services (§6.1).

Each handler adapts one :class:`JobContext` onto the corresponding
DB-mode service (:mod:`ccat.services.db_pipeline`, :mod:`...`.
db_generate, :mod:`...db_stats`, :mod:`...db_clarify`) and converts
typed domain errors into job error JSON ``{code, message, retryable}``.
Per plan §6.1: network/provider failures (Zite transport, OpenCode
connect) are retryable; validation, ownership, malformed-output, and
conflict failures are not.

Handlers not yet implemented are simply absent — the worker fails such
jobs with a clear non-retryable error instead of guessing at
semantics.
"""

import logging
from typing import TYPE_CHECKING, Any

from ccat.acquire import AcquisitionError
from ccat.child_info_generate import InfoSheetError
from ccat.clarify.extract import ClarifyError
from ccat.match import MatchError
from ccat.preparation import PreparationError
from ccat.screening import ScreeningError
from ccat.services import db_clarify, db_generate, db_pipeline, db_stats
from ccat.services.db_clarify import OpencodeTransportError
from ccat.sponsor_packets import SponsorPacketError
from ccat.stats import StatsError
from ccat.zite import ZiteError
from ccat.jobs.errors import JobCancelRequested
from ccat.jobs.registry import JobContext

if TYPE_CHECKING:
    from ccat.services.results import (
        AcquireResult,
        ClarifyExportResult,
        ClarifyGenerateResult,
        GenerateResult,
        MatchResult,
        ReconcileResult,
        ScreenResult,
        StatsResult,
    )

logger = logging.getLogger(__name__)

#: Errors worth another attempt (plan §6.1 retry budgets): provider
#: transport failures.  Everything else — malformed input/output,
#: ownership, conflicts, config errors — fails the job immediately.
RETRYABLE_ERRORS: tuple[type[BaseException], ...] = (
    ZiteError,
    OpencodeTransportError,
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


async def generate(ctx: JobContext) -> "GenerateResult":
    """Generate job: info sheets, then sponsor packets (§9.5).

    Args:
        ctx: The job context.

    Returns:
        The typed generation result.
    """
    return await db_generate.generate(
        ctx.cfg, ctx.actor, _options(ctx), ctx.progress, ctx.cancel_check
    )


async def stats(ctx: JobContext) -> "StatsResult":
    """Stats job: dated statistics report artifact (§9.6).

    Args:
        ctx: The job context.

    Returns:
        The typed stats result.
    """
    return await db_stats.stats(
        ctx.cfg, ctx.actor, _options(ctx), ctx.progress, ctx.cancel_check
    )


async def clarify_generate(ctx: JobContext) -> "ClarifyGenerateResult":
    """Clarify-generate job: missing email drafts, never sending (§9.6).

    Args:
        ctx: The job context.

    Returns:
        The typed clarify-generation result.
    """
    return await db_clarify.generate_drafts(
        ctx.cfg, ctx.actor, _options(ctx), ctx.progress, ctx.cancel_check
    )


async def clarify_export(ctx: JobContext) -> "ClarifyExportResult":
    """Clarify-export job: tracked CSV artifact (§9.6, D16).

    Args:
        ctx: The job context.

    Returns:
        The typed clarify-export result.
    """
    return await db_clarify.export_csv(ctx.cfg, ctx.actor)


#: job_type -> async handler(ctx).  Missing types fail non-retryably
#: in the worker ("no handler registered").
HANDLERS: dict[str, Any] = {
    "acquire": acquire,
    "screen": screen,
    "match": match,
    "reconcile": reconcile,
    "generate": generate,
    "stats": stats,
    "clarify_generate": clarify_generate,
    "clarify_export": clarify_export,
}

#: Domain errors carried through job error JSON without re-mapping
#: (kept explicit for the retry-budget documentation).
DOMAIN_ERRORS: tuple[type[BaseException], ...] = (
    AcquisitionError,
    PreparationError,
    ScreeningError,
    MatchError,
    InfoSheetError,
    SponsorPacketError,
    StatsError,
    ClarifyError,
    JobCancelRequested,
)

__all__ = [
    "DOMAIN_ERRORS",
    "HANDLERS",
    "RETRYABLE_ERRORS",
    "acquire",
    "clarify_export",
    "clarify_generate",
    "error_json",
    "generate",
    "match",
    "reconcile",
    "screen",
    "stats",
]
