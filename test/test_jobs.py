"""Tests for the durable job layer (plan §6.1, decision D7).

File-backed SQLite in ``tmp_path`` with a real async engine, migrated
via Alembic.  Every test runs one ``asyncio.run`` body that owns the
engine's event loop and disposes it before returning.  Handlers are
fakes injected through ``Worker(handlers=...)``; the real DB-mode
handlers are exercised in ``test_db_pipeline.py``.
"""

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from conftest import load_test_cfg
from ccat.config import Config, StorageSpec, WorkerSpec
from ccat.db.engine import create_async_engine, run_migrations
from ccat.db.models import Job, JobEvent
from ccat.db.repositories import _config_digest
from ccat.jobs import (
    JobConflictError,
    JobError,
    JobNotFound,
    Worker,
    enqueue_job,
    get_job,
    list_jobs,
    request_cancel,
)
from ccat.zite import ZiteError


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path, **worker: Any) -> Config:
    """Test config: temp SQLite DB, fast worker tuning, temp artifacts."""
    return load_test_cfg(
        excel_file=str(tmp_path / "wb.xlsx"),
        log_file=str(tmp_path / "ccat.log"),
        match_report_csv=str(tmp_path / "report.csv"),
        storage=StorageSpec(
            mode="db",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'ccat.sqlite3'}",
            artifact_root=str(tmp_path / "artifacts"),
        ),
        worker=WorkerSpec(
            poll_interval_seconds=0.01,
            heartbeat_seconds=0.02,
            lease_seconds=5.0,
            max_attempts=2,
            **{"progress_throttle_seconds": 0.0, **worker},
        ),
    )


def _prepare(tmp_path: Path, **worker: Any) -> Config:
    """Migrate the temp database and return its config."""
    cfg = _cfg(tmp_path, **worker)
    run_migrations(cfg.storage.database_url)
    return cfg


def _factory(cfg: Config) -> async_sessionmaker:
    """Async session factory over the config's database."""
    return async_sessionmaker(
        create_async_engine(cfg.storage.database_url), expire_on_commit=False
    )


async def _drain_events(factory: async_sessionmaker) -> list[dict[str, Any]]:
    """Read every job event as plain dicts."""
    from ccat.db.engine import sync_url_from
    from sqlalchemy import create_engine as sa_create_engine

    engine = sa_create_engine(sync_url_from(_url_of(factory)))
    try:
        from sqlalchemy.orm import Session

        with Session(engine) as session:
            rows = session.query(JobEvent).order_by(JobEvent.id).all()
            return [
                {"job_id": row.job_id, "kind": row.kind, "data": row.data}
                for row in rows
            ]
    finally:
        engine.dispose()


def _url_of(factory: async_sessionmaker) -> str:
    """Recover the database URL from a session factory's engine."""
    return str(factory.kw["bind"].url)


# ---------------------------------------------------------------------------
# Enqueue: validation, idempotency, conflict matrix.
# ---------------------------------------------------------------------------


def test_enqueue_creates_queued_job(tmp_path: Path) -> None:
    """A normal enqueue lands queued with options and config hash."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        factory = _factory(cfg)
        try:
            job = await enqueue_job(
                factory, cfg, "screen", {"test": True}, "topher", "key-1"
            )
            assert job["status"] == "queued"
            assert job["job_type"] == "screen"
            assert job["requester"] == "topher"
            assert job["options"] == {"test": True}
            assert job["attempt"] == 0
            assert job["config_hash"] == _config_digest(cfg)
            assert job["idempotency_key"] == "key-1"
        finally:
            await _dispose(factory)

    asyncio.run(main())


async def _dispose(factory: async_sessionmaker) -> None:
    """Dispose the factory's underlying engine."""
    await factory.kw["bind"].dispose()


def test_enqueue_rejects_unknown_type(tmp_path: Path) -> None:
    """Unknown job types fail fast with JobError."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        factory = _factory(cfg)
        try:
            with pytest.raises(JobError, match="unknown job type"):
                await enqueue_job(factory, cfg, "bogus", None, "topher")
        finally:
            await _dispose(factory)

    asyncio.run(main())


def test_idempotency_same_key_same_request_reuses_job(tmp_path: Path) -> None:
    """Same key + same hash returns the existing job (§6.1)."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        factory = _factory(cfg)
        try:
            first = await enqueue_job(
                factory, cfg, "match", {"test": True}, "a", "idem-1"
            )
            second = await enqueue_job(
                factory, cfg, "match", {"test": True}, "b", "idem-1"
            )
            assert second["id"] == first["id"]
            assert second["requester"] == "a"
        finally:
            await _dispose(factory)

    asyncio.run(main())


def test_idempotency_key_conflict_on_different_request(tmp_path: Path) -> None:
    """Same key + different request hash is a 409-class conflict."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        factory = _factory(cfg)
        try:
            await enqueue_job(factory, cfg, "match", {"test": True}, "a", "k")
            with pytest.raises(JobConflictError, match="different request"):
                await enqueue_job(factory, cfg, "match", {}, "a", "k")
        finally:
            await _dispose(factory)

    asyncio.run(main())


def test_pipeline_class_conflict_names_active_job(tmp_path: Path) -> None:
    """One active pipeline-class job per season (D7); the 409 names it."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        factory = _factory(cfg)
        try:
            active = await enqueue_job(factory, cfg, "acquire", {}, "a")
            with pytest.raises(JobConflictError) as excinfo:
                await enqueue_job(factory, cfg, "screen", {}, "a")
            assert f"#{active['id']}" in str(excinfo.value)
            assert "acquire" in str(excinfo.value)
        finally:
            await _dispose(factory)

    asyncio.run(main())


def test_non_pipeline_jobs_coexist(tmp_path: Path) -> None:
    """clarify_export/reconcile are not pipeline-class (D7)."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        factory = _factory(cfg)
        try:
            await enqueue_job(factory, cfg, "reconcile", {}, "a")
            await enqueue_job(factory, cfg, "clarify_export", {}, "a")
            jobs = await list_jobs(factory)
            assert len(jobs) == 2
        finally:
            await _dispose(factory)

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Cancel.
# ---------------------------------------------------------------------------


def test_request_cancel_queued_cancels_immediately(tmp_path: Path) -> None:
    """A queued job is cancelled outright (§6.2)."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        factory = _factory(cfg)
        try:
            job = await enqueue_job(factory, cfg, "match", {}, "a")
            cancelled = await request_cancel(factory, job["id"])
            assert cancelled["status"] == "cancelled"
            assert cancelled["finished_at"] is not None
        finally:
            await _dispose(factory)

    asyncio.run(main())


def test_request_cancel_missing_job_raises(tmp_path: Path) -> None:
    """Unknown job ids raise JobNotFound."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        factory = _factory(cfg)
        try:
            with pytest.raises(JobNotFound):
                await request_cancel(factory, 999)
        finally:
            await _dispose(factory)

    asyncio.run(main())


def test_request_cancel_finished_job_conflicts(tmp_path: Path) -> None:
    """Cancelling an already-cancelled job is a conflict."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        factory = _factory(cfg)
        try:
            job = await enqueue_job(factory, cfg, "match", {}, "a")
            await request_cancel(factory, job["id"])
            with pytest.raises(JobConflictError, match="already finished"):
                await request_cancel(factory, job["id"])
        finally:
            await _dispose(factory)

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Worker: lifecycle, progress, cancel, retry, recovery, shutdown.
# ---------------------------------------------------------------------------


def test_worker_runs_job_to_success(tmp_path: Path) -> None:
    """Claim → handler → succeeded; events recorded; attempt 1."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        factory = _factory(cfg)

        async def handler(ctx: Any) -> dict[str, Any]:
            return {"worked": True}

        worker = Worker(
            cfg, session_factory=factory, handlers={"match": handler}
        )
        task = asyncio.create_task(worker.run())
        job = await enqueue_job(factory, cfg, "match", {"test": True}, "a")
        for _ in range(500):
            state = await get_job(factory, job["id"])
            if state["status"] == "succeeded":
                break
            await asyncio.sleep(0.01)
        worker.stop.set()
        await task
        assert state["status"] == "succeeded"
        assert state["result"] == {"worked": True}
        assert state["attempt"] == 1
        events = await _drain_events(factory)
        kinds = [
            event["kind"] for event in events if event["job_id"] == job["id"]
        ]
        assert kinds[0] == "queued"
        assert "started" in kinds
        assert kinds[-1] == "succeeded"
        await _dispose(factory)

    asyncio.run(main())


def test_progress_events_written_and_throttled(tmp_path: Path) -> None:
    """progress() persists jobs.progress plus throttled event rows."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        factory = _factory(cfg)
        calls: list[tuple[Any, ...]] = []

        async def handler(ctx: Any) -> dict[str, Any]:
            ctx.progress("phase1", 0, 0, 10)
            ctx.progress("phase2", 5, 0, 10)
            ctx.progress("phase3", 10, 0, 10)
            calls.append(("done",))
            return {}

        worker = Worker(
            cfg, session_factory=factory, handlers={"stats": handler}
        )
        task = asyncio.create_task(worker.run())
        job = await enqueue_job(factory, cfg, "stats", {}, "a")
        for _ in range(500):
            state = await get_job(factory, job["id"])
            if state["status"] == "succeeded":
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        worker.stop.set()
        await task
        assert state["progress"]["phase"] == "phase3"
        events = await _drain_events(factory)
        progress_events = [
            event
            for event in events
            if event["job_id"] == job["id"] and event["kind"] == "progress"
        ]
        assert sorted(event["data"]["phase"] for event in progress_events) == [
            "phase1",
            "phase2",
            "phase3",
        ]
        await _dispose(factory)

    asyncio.run(main())


def test_progress_throttle_skips_rapid_updates(tmp_path: Path) -> None:
    """With a large throttle window only the first update persists."""

    async def main() -> None:
        cfg = _prepare(tmp_path, progress_throttle_seconds=60.0)
        factory = _factory(cfg)

        async def handler(ctx: Any) -> dict[str, Any]:
            ctx.progress("one", 0, 0, 2)
            ctx.progress("two", 1, 0, 2)
            return {}

        worker = Worker(
            cfg, session_factory=factory, handlers={"stats": handler}
        )
        task = asyncio.create_task(worker.run())
        job = await enqueue_job(factory, cfg, "stats", {}, "a")
        for _ in range(500):
            state = await get_job(factory, job["id"])
            if state["status"] == "succeeded":
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        worker.stop.set()
        await task
        assert state["progress"]["phase"] == "one"
        events = await _drain_events(factory)
        progress_events = [
            event
            for event in events
            if event["job_id"] == job["id"] and event["kind"] == "progress"
        ]
        assert len(progress_events) == 1
        await _dispose(factory)

    asyncio.run(main())


def test_cancel_queued_job_never_runs(tmp_path: Path) -> None:
    """A cancelled queued job is never claimed (§6.2)."""
    ran: list[int] = []

    async def main() -> None:
        cfg = _prepare(tmp_path)
        factory = _factory(cfg)

        async def handler(ctx: Any) -> dict[str, Any]:
            ran.append(ctx.job_id)
            return {}

        job = await enqueue_job(factory, cfg, "match", {}, "a")
        cancelled = await request_cancel(factory, job["id"])
        assert cancelled["status"] == "cancelled"
        worker = Worker(
            cfg, session_factory=factory, handlers={"match": handler}
        )
        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0.05)
        worker.stop.set()
        await task
        state = await get_job(factory, job["id"])
        assert state["status"] == "cancelled"
        assert ran == []
        await _dispose(factory)

    asyncio.run(main())


def test_cancel_running_job_cooperative(tmp_path: Path) -> None:
    """The handler's cancel token fires; the job ends cancelled (§6.2)."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        factory = _factory(cfg)

        async def handler(ctx: Any) -> dict[str, Any]:
            for _ in range(2000):
                ctx.cancel_check()
                await asyncio.sleep(0.01)
            return {"done": True}

        worker = Worker(
            cfg, session_factory=factory, handlers={"match": handler}
        )
        task = asyncio.create_task(worker.run())
        job = await enqueue_job(factory, cfg, "match", {}, "a")
        for _ in range(500):
            state = await get_job(factory, job["id"])
            if state["status"] == "running":
                break
            await asyncio.sleep(0.01)
        await request_cancel(factory, job["id"])
        for _ in range(500):
            state = await get_job(factory, job["id"])
            if state["status"] == "cancelled":
                break
            await asyncio.sleep(0.01)
        worker.stop.set()
        await task
        assert state["status"] == "cancelled"
        assert state["result"] == {"cancelled": True}
        await _dispose(factory)

    asyncio.run(main())


def test_retryable_error_requeues_then_fails(tmp_path: Path) -> None:
    """Retryable errors requeue up to max_attempts, then fail (§6.1)."""

    async def main() -> None:
        cfg = _prepare(tmp_path)  # max_attempts=2
        factory = _factory(cfg)
        runs: list[int] = []

        async def handler(ctx: Any) -> dict[str, Any]:
            runs.append(ctx.job_id)
            raise ZiteError("zite transport down")

        worker = Worker(
            cfg, session_factory=factory, handlers={"match": handler}
        )
        task = asyncio.create_task(worker.run())
        job = await enqueue_job(factory, cfg, "match", {}, "a")
        for _ in range(1000):
            state = await get_job(factory, job["id"])
            if state["status"] == "failed":
                break
            await asyncio.sleep(0.01)
        worker.stop.set()
        await task
        assert state["status"] == "failed"
        assert state["attempt"] == 2
        assert state["error"]["retryable"] is True
        assert state["error"]["code"] == "ZiteError"
        assert len(runs) == 2
        events = await _drain_events(factory)
        kinds = [
            event["kind"] for event in events if event["job_id"] == job["id"]
        ]
        assert "retry" in kinds
        await _dispose(factory)

    asyncio.run(main())


def test_non_retryable_error_fails_immediately(tmp_path: Path) -> None:
    """Validation-type errors fail on the first attempt (§6.1)."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        factory = _factory(cfg)
        runs: list[int] = []

        async def handler(ctx: Any) -> dict[str, Any]:
            runs.append(ctx.job_id)
            raise ValueError("bad options")

        worker = Worker(
            cfg, session_factory=factory, handlers={"match": handler}
        )
        task = asyncio.create_task(worker.run())
        job = await enqueue_job(factory, cfg, "match", {}, "a")
        for _ in range(500):
            state = await get_job(factory, job["id"])
            if state["status"] == "failed":
                break
            await asyncio.sleep(0.01)
        worker.stop.set()
        await task
        assert state["status"] == "failed"
        assert state["attempt"] == 1
        assert state["error"]["retryable"] is False
        assert runs == [job["id"]]
        await _dispose(factory)

    asyncio.run(main())


def test_lease_recovery_on_startup(tmp_path: Path) -> None:
    """Expired-lease running jobs fail as recovery_required (§6.1)."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        factory = _factory(cfg)
        engine = create_async_engine(cfg.storage.database_url)
        try:
            async with factory() as session:
                job = Job(
                    job_type="screen",
                    status="running",
                    requester="dead-worker",
                    attempt=1,
                    leased_until=datetime.now(UTC) - timedelta(seconds=1),
                )
                session.add(job)
                await session.commit()
                job_id = job.id
        finally:
            await engine.dispose()
        stop = asyncio.Event()
        stop.set()
        worker = Worker(cfg, session_factory=factory, stop=stop)
        await worker.run()
        state = await get_job(factory, job_id)
        assert state["status"] == "failed"
        assert state["error"]["code"] == "recovery_required"
        assert state["error"]["retryable"] is False
        await _dispose(factory)

    asyncio.run(main())


def test_shutdown_graceful_thread_based(tmp_path: Path) -> None:
    """Stop mid-job: current job finishes; thread exits cleanly.

    Uses a thread-based ``Worker.run`` with an injected stop event
    (the documented alternative to spawning a real subprocess); the
    signal path in ``run_worker`` only sets the same event.
    """

    async def main() -> None:
        cfg = _prepare(tmp_path)
        factory = _factory(cfg)

        async def handler(ctx: Any) -> dict[str, Any]:
            # Never checks the cancel token: shutdown waits for it.
            await asyncio.sleep(0.3)
            return {"slow": True}

        worker = Worker(
            cfg, session_factory=factory, handlers={"match": handler}
        )
        job = await enqueue_job(factory, cfg, "match", {}, "a")
        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=loop.run_until_complete, args=(worker.run(),)
        )
        thread.start()
        state = await get_job(factory, job["id"])
        for _ in range(500):
            state = await get_job(factory, job["id"])
            if state["status"] in ("running", "cancel_requested"):
                break
            await asyncio.sleep(0.01)
        loop.call_soon_threadsafe(worker.stop.set)
        thread.join(timeout=10)
        assert not thread.is_alive()
        state = await get_job(factory, job["id"])
        assert state["status"] == "succeeded"
        assert state["result"] == {"slow": True}
        loop.close()
        await _dispose(factory)

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Observation.
# ---------------------------------------------------------------------------


def test_list_and_get_jobs(tmp_path: Path) -> None:
    """list_jobs filters by season/status; get_job renders everything."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        factory = _factory(cfg)
        try:
            one = await enqueue_job(factory, cfg, "reconcile", {}, "a")
            await enqueue_job(factory, cfg, "clarify_export", {}, "b")
            await request_cancel(factory, one["id"])
            all_jobs = await list_jobs(factory)
            assert len(all_jobs) == 2
            cancelled = await list_jobs(factory, status="cancelled")
            assert [job["id"] for job in cancelled] == [one["id"]]
            detail = await get_job(factory, one["id"])
            assert detail["job_type"] == "reconcile"
            assert detail["cancel_requested"] is True
            season_jobs = await list_jobs(
                factory, season_id=detail["season_id"]
            )
            assert len(season_jobs) == 2
        finally:
            await _dispose(factory)

    asyncio.run(main())


def test_storage_artifact_root_default(tmp_path: Path) -> None:
    """artifact_root defaults backward-compatibly and is configurable."""
    from ccat.config import StorageSpec

    default = StorageSpec()
    assert default.artifact_root == "output/artifacts"
    assert StorageSpec(mode="db").artifact_root == "output/artifacts"
    custom = StorageSpec(artifact_root="/srv/ccat-artifacts")
    assert custom.artifact_root == "/srv/ccat-artifacts"


def test_unregistered_handler_type_fails_cleanly(tmp_path: Path) -> None:
    """A job type without a handler fails non-retryably in the worker."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        factory = _factory(cfg)
        worker = Worker(cfg, session_factory=factory, handlers={})
        task = asyncio.create_task(worker.run())
        job = await enqueue_job(factory, cfg, "generate", {}, "a")
        for _ in range(500):
            state = await get_job(factory, job["id"])
            if state["status"] == "failed":
                break
            await asyncio.sleep(0.01)
        worker.stop.set()
        await task
        assert state["status"] == "failed"
        assert "no handler registered" in state["error"]["message"]
        assert state["error"]["retryable"] is False
        await _dispose(factory)

    asyncio.run(main())
