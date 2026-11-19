"""Durable job layer for the DB pipeline (plan §6, decision D7).

Persistent ``jobs``/``job_events`` tables, one worker process with
lease/heartbeat/recovery, idempotent enqueue with pipeline-class
exclusivity per season (D7), and cooperative cancellation (§6.2).
"""

from ccat.jobs.errors import (
    JobCancelRequested,
    JobConflictError,
    JobError,
    JobNotFound,
)
from ccat.jobs.handlers import HANDLERS, error_json
from ccat.jobs.registry import (
    ACTIVE_STATUSES,
    JOB_TYPES,
    PIPELINE_CLASS,
    JobContext,
)
from ccat.jobs.service import (
    append_job_event,
    enqueue_job,
    get_job,
    list_jobs,
    request_cancel,
    request_hash,
)
from ccat.jobs.worker import Worker, run_worker

__all__ = [
    "ACTIVE_STATUSES",
    "HANDLERS",
    "JOB_TYPES",
    "JobCancelRequested",
    "JobConflictError",
    "JobContext",
    "JobError",
    "JobNotFound",
    "PIPELINE_CLASS",
    "Worker",
    "append_job_event",
    "enqueue_job",
    "error_json",
    "get_job",
    "list_jobs",
    "request_cancel",
    "request_hash",
    "run_worker",
]
