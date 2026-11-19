"""Durable job enqueue/observation service (plan §6.1, decision D7).

All functions are async and take an async session factory (from
:func:`ccat.db.engine.async_session_factory`).  Idempotency: callers
supply an ``Idempotency-Key``; the canonical request hash covers job
type, season, normalized options, and the config content hash — the
same key + hash returns the existing job, the same key with a
different hash is a 409-class :class:`JobConflictError` (§6.1).

Pipeline-class exclusivity (D7): at most one active
(queued/running/cancel_requested) pipeline-class job per season;
violations raise :class:`JobConflictError` naming the active job.
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ccat.db.models import Job, JobEvent
from ccat.db.repositories import _config_digest, ensure_season_row
from ccat.jobs.errors import (
    JobConflictError,
    JobError,
    JobNotFound,
)
from ccat.jobs.registry import ACTIVE_STATUSES, JOB_TYPES, PIPELINE_CLASS

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from ccat.config import Config


def _canonical(
    job_type: str, season: str, options: dict[str, Any], config_hash: str
) -> str:
    """Serialize the canonical request used for the hash (§6.1).

    Args:
        job_type: The job type.
        season: The season name.
        options: Normalized (plain dict) options.
        config_hash: The config content digest.

    Returns:
        The canonical JSON text.
    """
    return json.dumps(
        {
            "job_type": job_type,
            "season": season,
            "options": options,
            "config_hash": config_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def request_hash(cfg: "Config", job_type: str, options: dict[str, Any]) -> str:
    """Hash one job request canonically (§6.1).

    Args:
        cfg: Pipeline configuration.
        job_type: The job type.
        options: Normalized job options.

    Returns:
        The sha256 hex digest.
    """
    return hashlib.sha256(
        _canonical(job_type, cfg.season, options, _config_digest(cfg)).encode(
            "utf-8"
        )
    ).hexdigest()


def _job_dict(job: Job) -> dict[str, Any]:
    """Render one job row as a plain dict.

    Args:
        job: The ORM job row.

    Returns:
        The job dict with datetimes rendered as ISO text.
    """

    def iso(value: Any) -> str | None:
        return value.isoformat() if value is not None else None

    return {
        "id": job.id,
        "job_type": job.job_type,
        "season_id": job.season_id,
        "status": job.status,
        "requester": job.requester,
        "options": job.options,
        "idempotency_key": job.idempotency_key,
        "request_hash": job.request_hash,
        "config_hash": job.config_hash,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "progress": job.progress,
        "cancel_requested": job.cancel_requested,
        "error": job.error,
        "result": job.result,
        "created_at": iso(job.created_at),
        "started_at": iso(job.started_at),
        "finished_at": iso(job.finished_at),
        "heartbeat_at": iso(job.heartbeat_at),
        "leased_until": iso(job.leased_until),
    }


async def enqueue_job(
    session_factory: "async_sessionmaker",
    cfg: "Config",
    job_type: str,
    options: dict[str, Any] | None = None,
    requester: str = "system",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Enqueue one durable job (§6.1, D7).

    Args:
        session_factory: Async session factory over the job database.
        cfg: Season configuration (season identity + config hash).
        job_type: One of :data:`ccat.jobs.registry.JOB_TYPES`.
        options: Normalized job options (JSON-serializable).
        requester: Requesting identity.
        idempotency_key: Optional idempotency key.

    Returns:
        The job dict (existing one on an idempotent replay).

    Raises:
        JobError: For an unknown job type.
        JobConflictError: On key reuse with a different request, a
            same-key race, or an active pipeline-class job for the
            season (D7).
    """
    if job_type not in JOB_TYPES:
        raise JobError(f"unknown job type {job_type!r}")
    normalized = dict(options or {})
    digest = request_hash(cfg, job_type, normalized)
    async with session_factory() as session:
        try:
            season = await ensure_season_row(session, cfg)
            if idempotency_key is not None:
                existing = await session.scalar(
                    select(Job).where(Job.idempotency_key == idempotency_key)
                )
                if existing is not None:
                    if existing.request_hash != digest:
                        raise JobConflictError(
                            f"idempotency key {idempotency_key!r} was"
                            " already used for a different request"
                        )
                    snapshot = _job_dict(existing)
                    await session.rollback()
                    return snapshot
            if job_type in PIPELINE_CLASS:
                active = await session.scalar(
                    select(Job)
                    .where(
                        Job.season_id == season.id,
                        Job.job_type.in_(PIPELINE_CLASS),
                        Job.status.in_(ACTIVE_STATUSES),
                    )
                    .order_by(Job.id)
                )
                if active is not None:
                    raise JobConflictError(
                        f"season {season.name!r} already has an active"
                        f" {active.job_type} job #{active.id}"
                        f" (status {active.status}); one pipeline-class"
                        " job per season may run at a time"
                    )
            now = datetime.now(UTC)
            job = Job(
                job_type=job_type,
                season_id=season.id,
                status="queued",
                requester=requester,
                options=normalized,
                idempotency_key=idempotency_key,
                request_hash=digest,
                config_hash=_config_digest(cfg),
            )
            session.add(job)
            await session.flush()
            session.add(
                JobEvent(
                    job_id=job.id,
                    ts=now,
                    kind="queued",
                    data={"requester": requester},
                )
            )
            await session.commit()
            return _job_dict(job)
        except IntegrityError as error:
            await session.rollback()
            raise JobConflictError(
                f"idempotency key {idempotency_key!r} race; the key is"
                " already registered"
            ) from error
        except Exception:
            await session.rollback()
            raise


async def get_job(
    session_factory: "async_sessionmaker", job_id: int
) -> dict[str, Any]:
    """Read one job dict.

    Args:
        session_factory: Async session factory.
        job_id: The job id.

    Returns:
        The job dict.

    Raises:
        JobNotFound: When the job does not exist.
    """
    async with session_factory() as session:
        job = await session.get(Job, job_id)
        if job is None:
            raise JobNotFound(f"job {job_id} not found")
        return _job_dict(job)


async def list_jobs(
    session_factory: "async_sessionmaker",
    season_id: int | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List jobs newest-first with optional season/status filters.

    Args:
        session_factory: Async session factory.
        season_id: Optional season id filter.
        status: Optional status filter.
        limit: Maximum number of jobs returned.

    Returns:
        Job dicts ordered by id descending.
    """
    statement = select(Job).order_by(Job.id.desc()).limit(limit)
    if season_id is not None:
        statement = statement.where(Job.season_id == season_id)
    if status is not None:
        statement = statement.where(Job.status == status)
    async with session_factory() as session:
        jobs = (await session.scalars(statement)).all()
        return [_job_dict(job) for job in jobs]


async def request_cancel(
    session_factory: "async_sessionmaker", job_id: int
) -> dict[str, Any]:
    """Request cancellation of one job (§6.2).

    Queued jobs are cancelled immediately; running jobs get the
    ``cancel_requested`` status/flag that the cooperative cancel token
    observes.  Finished jobs refuse the request.

    Args:
        session_factory: Async session factory.
        job_id: The job id.

    Returns:
        The updated job dict.

    Raises:
        JobNotFound: When the job does not exist.
        JobConflictError: When the job already finished.
    """
    async with session_factory() as session:
        try:
            job = await session.get(Job, job_id)
            if job is None:
                raise JobNotFound(f"job {job_id} not found")
            if job.status not in ACTIVE_STATUSES:
                raise JobConflictError(
                    f"job {job_id} already finished ({job.status})"
                )
            now = datetime.now(UTC)
            job.cancel_requested = True
            if job.status == "queued":
                job.status = "cancelled"
                job.finished_at = now
                kind = "cancelled"
            else:
                job.status = "cancel_requested"
                kind = "cancel_requested"
            session.add(JobEvent(job_id=job.id, ts=now, kind=kind, data=None))
            await session.commit()
            return _job_dict(job)
        except Exception:
            await session.rollback()
            raise


async def append_job_event(
    session_factory: "async_sessionmaker",
    job_id: int,
    kind: str,
    data: dict[str, Any] | None = None,
) -> None:
    """Append one job event row (throttling is the caller's duty).

    Args:
        session_factory: Async session factory.
        job_id: The owning job's id.
        kind: Event kind (progress / note / ...).
        data: Optional JSON payload.
    """
    async with session_factory() as session:
        try:
            session.add(
                JobEvent(
                    job_id=job_id,
                    ts=datetime.now(UTC),
                    kind=kind,
                    data=data,
                )
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise


__all__ = [
    "append_job_event",
    "enqueue_job",
    "get_job",
    "list_jobs",
    "request_cancel",
    "request_hash",
]
