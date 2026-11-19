"""Job-type registry and handler context (plan §6.1, decision D7).

Job types mirror the pipeline commands plus the explicit service
operations that replace hidden startup work (§1.1: the audit startup
reconciliation becomes the ``reconcile`` job type).

Conflict matrix (D7): one active *pipeline-class* job —
:data:`PIPELINE_CLASS` — per season at a time; non-pipeline-class jobs
(``clarify_export``, ``reconcile``) coexist with everything.

Handlers are async callables taking one :class:`JobContext`.  The
worker fails a job whose type has no registered handler with a
non-retryable error, so entry points can enqueue every declared type
before its handler ships.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from ccat.config import Config

    #: Async context-manager factory yielding one season repository
    #: (exact type: ``asynccontextmanager``-wrapped repository
    #: factory); typed loosely for checker compatibility.
    RepoFactory = Callable[..., Any]

#: Every enqueueable job type.
JOB_TYPES: frozenset[str] = frozenset(
    {
        "acquire",
        "screen",
        "match",
        "generate",
        "stats",
        "clarify_generate",
        "clarify_export",
        "reconcile",
    }
)

#: Job types subject to one-active-per-season exclusion (D7).
PIPELINE_CLASS: frozenset[str] = frozenset(
    {
        "acquire",
        "screen",
        "match",
        "generate",
        "stats",
        "clarify_generate",
    }
)

#: Job statuses considered active (blocking / cancellable).
ACTIVE_STATUSES: tuple[str, ...] = ("queued", "running", "cancel_requested")


@dataclass
class JobContext:
    """Everything one handler invocation needs (plan §6.1).

    Attributes:
        cfg: The season configuration snapshot.
        actor: Requesting identity for attribution.
        options: Normalized job options.
        progress: Reporter ``(phase, completed=0, failed=0, total=None)``;
            writes ``jobs.progress`` plus a throttled ``job_events``
            row.
        cancel_check: Raises :class:`ccat.jobs.errors.JobCancelRequested`
            when cancellation was requested; call between stages and
            between per-item commits (§6.2).
        repo_factory: Async context manager yielding a fresh
            :class:`SeasonStoreRepository` for ad-hoc access.
        job_id: The job row's id.
    """

    cfg: "Config"
    actor: str
    options: dict[str, Any]
    progress: "Callable[..., None]"
    cancel_check: "Callable[[], None]"
    repo_factory: "RepoFactory | None" = None
    job_id: int = 0


__all__ = [
    "ACTIVE_STATUSES",
    "JOB_TYPES",
    "PIPELINE_CLASS",
    "JobContext",
    "RepoFactory",
]
