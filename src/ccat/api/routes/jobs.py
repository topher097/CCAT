"""Pipeline job routes: enqueue, observe, cancel, events + SSE (§6.1).

Enqueue pre-validates screen options against the stored snapshot
(:func:`ccat.services.db_pipeline.validate_screen_options`) so a bad
``redo`` target fails 422 before any billable work (§9.2).  The
idempotency-key semantics come from :func:`ccat.jobs.service.enqueue_job`
(same key + same hash → the existing job; same key + different hash →
409).  Cancellation: any coordinator/admin may request it (documented:
jobs are season-scoped, not owner-scoped, in the first deployment).
"""

import asyncio
import json
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from ccat.api.auth import AuthUser, current_user, require_csrf
from ccat.api.auth import require_roles
from ccat.api.deps import season_repo, session_factory_dep
from ccat.api.dto import JobCreate, JobView
from ccat.db.repositories import SeasonStoreRepository
from ccat.jobs import service as jobs_service
from ccat.services.db_pipeline import validate_screen_options

if TYPE_CHECKING:
    from ccat.config import Config

router = APIRouter(tags=["jobs"])

#: SSE poll cadence (seconds).
_SSE_POLL_SECONDS = 0.25

#: SSE safety bound (~30s of silence before the stream ends).
_SSE_MAX_POLLS = 120

#: Log-event tail for the job detail view (log rows are far more frequent).
_LOG_TAIL = 500


def _view(
    job: dict[str, Any], events: list[dict[str, Any]] | None = None
) -> JobView:
    """Render one job dict as the view.

    Args:
        job: The job dict from the jobs service.
        events: Optional events tail.

    Returns:
        The job view.
    """
    data = dict(job)
    data["events"] = events or []
    return JobView(**data)


async def _events_tail(
    session_factory: Any, job_id: int, limit: int = 20
) -> list[dict[str, Any]]:
    """Read the status event tail plus the most recent log lines.

    Log rows arrive at a far higher rate than lifecycle events, so they
    get their own (larger) tail; otherwise a busy job would push its
    status events out of the detail view.

    Args:
        session_factory: Async session factory.
        job_id: The job id.
        limit: Status-event tail length.

    Returns:
        Event dicts, oldest first, status and log events merged.
    """
    from ccat.db.models import JobEvent as JobEventRow

    async with session_factory() as session:
        status_rows = (
            await session.scalars(
                select(JobEventRow)
                .where(
                    JobEventRow.job_id == job_id,
                    JobEventRow.kind != "log",
                )
                .order_by(JobEventRow.id.desc())
                .limit(limit)
            )
        ).all()
        log_rows = (
            await session.scalars(
                select(JobEventRow)
                .where(
                    JobEventRow.job_id == job_id,
                    JobEventRow.kind == "log",
                )
                .order_by(JobEventRow.id.desc())
                .limit(_LOG_TAIL)
            )
        ).all()
        rows = sorted((*status_rows, *log_rows), key=lambda row: row.id)
        return [
            {
                "id": row.id,
                "job_id": row.job_id,
                "ts": row.ts.isoformat() if row.ts else None,
                "kind": row.kind,
                "data": row.data,
            }
            for row in rows
        ]


@router.post(
    "/seasons/{season}/jobs",
    response_model=JobView,
    status_code=202,
    dependencies=[Depends(require_csrf)],
)
async def enqueue(
    season: str,
    body: JobCreate,
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    session_factory: Any = Depends(session_factory_dep),
    user: AuthUser = Depends(require_roles("coordinator", "admin")),
) -> JobView:
    """Enqueue one durable job (202 + job view).

    Args:
        season: The season name.
        body: Job type, options, optional idempotency key.
        request: The current request.
        repo: The season-bound repository (screen pre-check snapshot).
        session_factory: The app session factory.
        user: The authenticated coordinator.

    Returns:
        The queued job view.

    Raises:
        ApiError: 422 invalid options (screen pre-check); 409 active
            pipeline job or idempotency-key reuse.
    """
    cfg: Config = request.app.state.cfg
    if body.job_type == "screen":
        options = body.options
        redo = options.get("redo")
        if redo is not None:
            snapshot = {
                "child": await repo.final_rows("child"),
                "raw_child": await repo.raw_rows("child"),
            }
            validate_screen_options(snapshot, redo=redo)
    try:
        job = await jobs_service.enqueue_job(
            session_factory,
            cfg,
            body.job_type,
            body.options,
            requester=user.username,
            idempotency_key=body.idempotency_key,
        )
    except Exception:
        raise
    return _view(job)


@router.get("/seasons/{season}/jobs", response_model=list[JobView])
async def list_jobs(
    season: str,
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    session_factory: Any = Depends(session_factory_dep),
    _user: AuthUser = Depends(current_user),
) -> list[JobView]:
    """List the season's jobs, newest first.

    Args:
        season: The season name.
        request: The current request.
        repo: The season-bound repository (season id source).
        session_factory: The app session factory.
        _user: Authenticated identity.

    Returns:
        The job views.
    """
    jobs = await jobs_service.list_jobs(
        session_factory, season_id=repo.season_id
    )
    return [_view(job) for job in jobs]


@router.get("/jobs/{job_id}", response_model=JobView)
async def get_job(
    job_id: int,
    request: Request,
    session_factory: Any = Depends(session_factory_dep),
    _user: AuthUser = Depends(current_user),
) -> JobView:
    """Read one job with its event tail.

    Args:
        job_id: The job id.
        request: The current request.
        session_factory: The app session factory.
        _user: Authenticated identity.

    Returns:
        The job view.

    Raises:
        ApiError: 404 when the job does not exist.
    """
    job = await jobs_service.get_job(session_factory, job_id)
    return _view(job, await _events_tail(session_factory, job_id))


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=JobView,
    dependencies=[Depends(require_csrf)],
)
async def cancel_job(
    job_id: int,
    request: Request,
    session_factory: Any = Depends(session_factory_dep),
    _user: AuthUser = Depends(require_roles("coordinator", "admin")),
) -> JobView:
    """Request cancellation (coordinator/admin; any coordinator may).

    Documented: jobs are season-scoped in the first deployment, so
    ownership is the coordinator role, not a per-row owner column.

    Args:
        job_id: The job id.
        request: The current request.
        session_factory: The app session factory.
        _user: Authenticated coordinator.

    Returns:
        The updated job view.
    """
    job = await jobs_service.request_cancel(session_factory, job_id)
    return _view(job)


@router.get("/jobs/{job_id}/events", response_model=list[dict[str, Any]])
async def job_events(
    job_id: int,
    request: Request,
    after: int = Query(0, ge=0),
    session_factory: Any = Depends(session_factory_dep),
    _user: AuthUser = Depends(current_user),
) -> list[dict[str, Any]]:
    """Poll events after an id (JSON polling fallback).

    Args:
        job_id: The job id.
        request: The current request.
        after: The last event id seen.
        session_factory: The app session factory.
        _user: Authenticated identity.

    Returns:
        Events with ``id > after``, oldest first.
    """
    from ccat.db.models import JobEvent as JobEventRow

    async with session_factory() as session:
        rows = (
            await session.scalars(
                select(JobEventRow)
                .where(JobEventRow.job_id == job_id, JobEventRow.id > after)
                .order_by(JobEventRow.id)
            )
        ).all()
        return [
            {
                "id": row.id,
                "job_id": row.job_id,
                "ts": row.ts.isoformat() if row.ts else None,
                "kind": row.kind,
                "data": row.data,
            }
            for row in rows
        ]


@router.get("/jobs/{job_id}/events/stream")
async def job_events_stream(
    job_id: int,
    request: Request,
    session_factory: Any = Depends(session_factory_dep),
    _user: AuthUser = Depends(current_user),
) -> StreamingResponse:
    """One-way SSE progress stream (plan §5.2; no sse-starlette).

    Emits one ``data:`` JSON line per event; ends after the job
    reaches a terminal state.  Same auth as the JSON routes.

    Args:
        job_id: The job id.
        request: The current request.
        session_factory: The app session factory.
        _user: Authenticated identity.

    Returns:
        The streaming ``text/event-stream`` response.
    """

    async def stream() -> Any:
        """Yield events until the job terminates (bounded polls)."""
        cursor = 0
        for _ in range(_SSE_MAX_POLLS):
            from ccat.db.models import JobEvent as JobEventRow
            from ccat.db.models import Job as JobRow

            async with session_factory() as session:
                rows = (
                    await session.scalars(
                        select(JobEventRow)
                        .where(
                            JobEventRow.job_id == job_id,
                            JobEventRow.id > cursor,
                        )
                        .order_by(JobEventRow.id)
                    )
                ).all()
                job = await session.get(JobRow, job_id)
            terminal = job is not None and job.status in {
                "succeeded",
                "failed",
                "cancelled",
            }
            for row in rows:
                cursor = row.id
                payload = json.dumps(
                    {
                        "id": row.id,
                        "kind": row.kind,
                        "data": row.data,
                    },
                    default=str,
                )
                yield f"data: {payload}\n\n"
            if terminal and not rows:
                break
            await asyncio.sleep(_SSE_POLL_SECONDS)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store"},
    )


__all__ = ["router"]
