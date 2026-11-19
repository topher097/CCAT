"""Typed errors for the durable job layer (plan §6.1, decision D7)."""


class JobError(Exception):
    """Base class for job-layer failures."""


class JobConflictError(JobError):
    """A conflicting job/state exists (HTTP 409 semantics).

    Raised for pipeline-class exclusivity violations (one active
    pipeline job per season, D7) and idempotency-key reuse with a
    different request hash (§6.1).
    """


class JobNotFound(JobError):  # noqa: N818 - spec-mandated name
    """No job exists under the requested id (HTTP 404 semantics)."""


class JobCancelRequested(Exception):  # noqa: N818 - spec-mandated name
    """Cooperative cancellation signal raised inside a handler (§6.2).

    Deliberately NOT a :class:`JobError` subclass: it is the worker's
    control-flow signal for a requested cancel, not a job failure.
    """


__all__ = [
    "JobCancelRequested",
    "JobConflictError",
    "JobError",
    "JobNotFound",
]
