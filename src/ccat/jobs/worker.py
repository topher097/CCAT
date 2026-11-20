"""Durable job worker: claim, heartbeat, cancel, recover (§6.1, D7).

One worker process polls the ``jobs`` table.  Claiming is a single
atomic compare-and-set ``UPDATE jobs SET status='running' ... WHERE
id=:id AND status='queued'`` (rowcount-checked): SQLite has no ``SKIP
LOCKED``, and one claimer makes the compare-and-set sufficient;
PostgreSQL later adds real skipping via the same query shape with a
locking clause.  Pipeline-class exclusivity is enforced at enqueue
(D7), so the claim needs only the status check.

A supervision task heartbeats ``heartbeat_at``/``leased_until`` and
watches ``cancel_requested`` for the cooperative cancel token.  At
startup, running jobs whose lease expired are failed as
``recovery_required`` — never blind-repeated (§6.1); operators rerun
them idempotently.  Retryable failures requeue up to
``max_attempts`` with ``attempt * poll_interval`` backoff; SIGTERM /
SIGINT stop claiming, trigger the running job's cancel token, and
wait bounded (one lease) before exiting 0.
"""

import asyncio
import logging
import signal
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update

from ccat.config import ConfigError, load_config
from ccat.db.engine import (
    async_session_factory,
    create_async_engine,
)
from ccat.db.models import Job, JobEvent
from ccat.db.uow import UnitOfWork
from ccat.jobs.errors import (
    JobCancelRequested,
    JobError,
)
from ccat.jobs.handlers import HANDLERS, error_json
from ccat.jobs.logs import JobLogHandler, LogBuffer
from ccat.jobs.registry import JobContext

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncEngine

    from ccat.config import Config
    from ccat.db.repositories import SeasonStoreRepository
logger = logging.getLogger(__name__)

#: Log-buffer pump cadence (seconds).
_LOG_PUMP_SECONDS = 0.25


@dataclass
class CancelState:
    """Cooperative cancel token shared with the running handler."""

    requested: bool = False


class Worker:
    """One durable-job worker loop over one job database (§6.1)."""

    def __init__(
        self,
        cfg: "Config",
        session_factory: "async_sessionmaker | None" = None,
        handlers: "dict[str, Any] | None" = None,
        stop: asyncio.Event | None = None,
        config_path: str | Path | None = None,
    ) -> None:
        """Initialize the worker.

        Args:
            cfg: Season configuration (worker tuning + database URL).
            session_factory: Async session factory; one is created
                from ``cfg.storage.database_url`` when omitted.
            handlers: job_type -> async handler; defaults to
                :data:`ccat.jobs.handlers.HANDLERS`.
            stop: Graceful-stop event; a fresh one is created when
                omitted (set it from a signal handler or another
                thread).
            config_path: The configuration file to watch; an edited
                file is picked up between jobs.
        """
        self.cfg = cfg
        self.spec = cfg.worker
        self.handlers = handlers if handlers is not None else HANDLERS
        self.stop = stop or asyncio.Event()
        self._owns_engine = session_factory is None
        self._engine: AsyncEngine | None
        self._session_factory: async_sessionmaker
        if session_factory is None:
            self._engine = create_async_engine(cfg.storage.database_url)
            self._session_factory = async_session_factory(self._engine)
        else:
            self._engine = None
            self._session_factory = session_factory
        self._config_path = Path(config_path) if config_path else None
        self._config_mtime = self._watched_mtime()
        self._cancel = CancelState()
        self._current: asyncio.Task[Any] | None = None
        self._pending: set[asyncio.Task[Any]] = set()
        # Progress writes are dispatched concurrently; serializing them
        # keeps the persisted snapshot from regressing to an older phase.
        self._progress_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Loop.
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run until :attr:`stop` is set; graceful, exit-safe (§6.1)."""
        try:
            await self._recover_leases()
            while not self.stop.is_set():
                await self._reload_config()
                job = await self._claim()
                if job is None:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(
                            self.stop.wait(),
                            timeout=self.spec.poll_interval_seconds,
                        )
                    continue
                if job.cancel_requested:
                    # Defensive: queued + cancel race.
                    await self._finalize(
                        job.id, "cancelled", result={"cancelled": True}
                    )
                    continue
                self._current = asyncio.create_task(self._execute(job))
                await self._current
                self._current = None
        finally:
            current, self._current = self._current, None
            if current is not None and not current.done():
                self._cancel.requested = True
                try:
                    await asyncio.wait_for(
                        asyncio.shield(current),
                        timeout=self.spec.lease_seconds,
                    )
                except TimeoutError:
                    current.cancel()
                    await asyncio.gather(current, return_exceptions=True)
                except Exception:  # noqa: BLE001 - task end is enough
                    pass
            if self._owns_engine and self._engine is not None:
                await self._engine.dispose()

    def _watched_mtime(self) -> float | None:
        """Return the watched configuration file's mtime, if readable.

        Returns:
            The modification time, or None when there is no watched
            file or it cannot be stat'ed.
        """
        if self._config_path is None:
            return None
        try:
            return self._config_path.stat().st_mtime
        except OSError:
            return None

    async def _reload_config(self) -> None:
        """Pick up an edited configuration file between jobs.

        Reloading between jobs (never mid-job) keeps a running pipeline
        on one configuration.  A file that fails validation is ignored
        and retried on the next change, so a bad edit never stops the
        worker.
        """
        if self._config_path is None:
            return
        mtime = self._watched_mtime()
        if mtime is None or mtime == self._config_mtime:
            return
        self._config_mtime = mtime
        try:
            cfg = load_config(self._config_path)
        except ConfigError:
            logger.warning(
                "configuration reload failed; keeping the running config",
                exc_info=True,
            )
            return
        if (
            self._owns_engine
            and cfg.storage.database_url != self.cfg.storage.database_url
        ):
            await self._swap_database(cfg.storage.database_url)
        self.cfg = cfg
        self.spec = cfg.worker
        logger.info("reloaded configuration from %s", self._config_path.name)

    async def _swap_database(self, url: str) -> None:
        """Rebuild the engine after the configured database URL changed.

        Args:
            url: The new async SQLAlchemy URL.
        """
        old_engine = self._engine
        self._engine = create_async_engine(url)
        self._session_factory = async_session_factory(self._engine)
        if old_engine is not None:
            await old_engine.dispose()

    async def _claim(self) -> Job | None:
        """Atomically claim one queued job.

        The compare-and-set UPDATE is the whole claim: SQLite has no
        ``SKIP LOCKED``; with one worker the rowcount-checked UPDATE is
        atomic enough, and the status guard makes double-claiming
        impossible even with two pollers.

        Returns:
            The claimed job (re-read, status running), or None.
        """
        async with self._session_factory() as session:
            try:
                candidate = await session.scalar(
                    select(Job.id)
                    .where(Job.status == "queued")
                    .order_by(Job.id)
                )
                if candidate is None:
                    return None
                now = datetime.now(UTC)
                result = await session.execute(
                    update(Job)
                    .where(Job.id == candidate, Job.status == "queued")
                    .values(
                        status="running",
                        attempt=Job.attempt + 1,
                        started_at=now,
                        heartbeat_at=now,
                        leased_until=now
                        + timedelta(seconds=self.spec.lease_seconds),
                    )
                )
                if result.rowcount != 1:
                    await session.rollback()
                    return None
                job = await session.get(Job, candidate)
                session.add(
                    JobEvent(
                        job_id=candidate,
                        ts=now,
                        kind="started",
                        data={"attempt": job.attempt},
                    )
                )
                await session.commit()
                return job
            except Exception:
                await session.rollback()
                raise

    async def _recover_leases(self) -> int:
        """Fail expired-lease jobs as ``recovery_required`` (§6.1).

        A killed worker must never blind-repeat paid or externally
        visible work; the operator inspects partial state and reruns
        the job idempotently.

        Returns:
            The number of recovered jobs.
        """
        now = datetime.now(UTC)
        error = {
            "code": "recovery_required",
            "message": (
                "worker restart found this job's lease expired; inspect"
                " partial state and rerun the job idempotently (§6.1)"
            ),
            "retryable": False,
        }
        async with self._session_factory() as session:
            try:
                stale = (
                    await session.scalars(
                        select(Job).where(
                            Job.status.in_(("running", "cancel_requested")),
                            Job.leased_until.is_not(None),
                            Job.leased_until < now,
                        )
                    )
                ).all()
                for job in stale:
                    job.status = "failed"
                    job.error = error
                    job.finished_at = now
                    job.leased_until = None
                    session.add(
                        JobEvent(
                            job_id=job.id,
                            ts=now,
                            kind="failed",
                            data=error,
                        )
                    )
                await session.commit()
                if stale:
                    logger.warning(
                        "lease recovery: failed %d expired job(s)",
                        len(stale),
                    )
                return len(stale)
            except Exception:
                await session.rollback()
                raise

    # ------------------------------------------------------------------
    # Execution.
    # ------------------------------------------------------------------

    async def _execute(self, job: Job) -> None:
        """Run one claimed job to a terminal state (§6.1).

        Args:
            job: The claimed job row (status running).
        """
        token = CancelState()
        self._cancel = token
        loop = asyncio.get_running_loop()
        progress_state: dict[str, Any] = {"last": 0.0, "latest": None}
        log_buffer = LogBuffer()
        ccat_logger = logging.getLogger("ccat")
        log_handler = JobLogHandler(log_buffer.append)
        previous_level = ccat_logger.level
        # Programmatic runs (tests) never call setup_logging, so INFO
        # records would otherwise be filtered out before reaching emit.
        lowered_level = ccat_logger.getEffectiveLevel() > logging.INFO
        if lowered_level:
            ccat_logger.setLevel(logging.INFO)
        ccat_logger.addHandler(log_handler)

        def progress(
            phase: str,
            completed: int = 0,
            failed: int = 0,
            total: int | None = None,
        ) -> None:
            """Throttled progress write usable from any thread."""
            now = loop.time()
            progress_state["latest"] = {
                "phase": phase,
                "completed": completed,
                "failed": failed,
                "total": total,
            }
            if (
                now - progress_state["last"]
                < self.spec.progress_throttle_seconds
            ):
                return
            progress_state["last"] = now
            progress_state["latest"] = None
            self._dispatch_progress(
                loop, job.id, phase, completed, failed, total
            )

        async def flush_progress() -> None:
            """Persist the newest progress snapshot, ignoring the throttle.

            The throttle drops intermediate updates; without this the
            job would keep the last persisted snapshot, which can be
            many steps behind the work that actually finished (three
            tables pulled, one reported).  Only a dropped snapshot is
            flushed, so an unthrottled run writes nothing extra.
            """
            snapshot = progress_state["latest"]
            if snapshot is None:
                return
            progress_state["latest"] = None
            try:
                await self._write_progress(
                    job.id,
                    snapshot["phase"],
                    snapshot["completed"],
                    snapshot["failed"],
                    snapshot["total"],
                )
            except Exception:  # noqa: BLE001 - progress is best effort
                logger.warning("final progress write failed", exc_info=True)

        def cancel_check() -> None:
            """Cooperative cancel check (raises when requested)."""
            if token.requested or self.stop.is_set():
                raise JobCancelRequested(
                    "cancellation requested; completed work stays"
                    " committed (§6.2)"
                )

        async def flush_logs() -> None:
            """Persist the buffered log lines as one event batch.

            Best effort: a failed write is warned about and dropped so a
            broken log sink never fails the job itself.
            """
            lines = log_buffer.drain()
            if not lines:
                return
            try:
                await self._write_logs(job.id, lines)
            except Exception:  # noqa: BLE001 - logs must not kill the job
                logger.warning(
                    "log flush for job %s failed", job.id, exc_info=True
                )

        async def pump_logs() -> None:
            """Drain the log buffer on the pump cadence."""
            while True:
                await asyncio.sleep(_LOG_PUMP_SECONDS)
                await flush_logs()

        async def supervise() -> None:
            """Heartbeat the lease and watch the cancel flag."""
            while True:
                await asyncio.sleep(self.spec.heartbeat_seconds)
                try:
                    async with self._session_factory() as session:
                        row = await session.get(Job, job.id)
                        if row is None:
                            continue
                        now = datetime.now(UTC)
                        row.heartbeat_at = now
                        row.leased_until = now + timedelta(
                            seconds=self.spec.lease_seconds
                        )
                        if row.cancel_requested:
                            token.requested = True
                        session.add(
                            JobEvent(job_id=job.id, ts=now, kind="heartbeat")
                        )
                        await session.commit()
                except Exception:  # noqa: BLE001 - keep heartbeating
                    logger.warning(
                        "heartbeat for job %s failed", job.id, exc_info=True
                    )

        ctx = JobContext(
            job_id=job.id,
            cfg=self.cfg,
            actor=job.requester,
            options=dict(job.options or {}),
            progress=progress,
            cancel_check=cancel_check,
            repo_factory=self._repo_factory(job.requester),
        )
        supervision = asyncio.create_task(supervise())
        log_pump = asyncio.create_task(pump_logs())
        try:
            handler = self.handlers.get(job.job_type)
            if handler is None:
                raise JobError(
                    f"no handler registered for job type {job.job_type!r}"
                )
            try:
                result = await handler(ctx)
                payload = (
                    result.model_dump()
                    if hasattr(result, "model_dump")
                    else result
                )
            finally:
                # Let the dispatched writes land, then persist the newest
                # snapshot so the terminal state follows the real progress.
                if self._pending:
                    await asyncio.gather(*self._pending, return_exceptions=True)
                await flush_progress()
                await flush_logs()
            await self._finalize(job.id, "succeeded", result=payload)
        except JobCancelRequested:
            await flush_logs()
            await self._finalize(
                job.id, "cancelled", result={"cancelled": True}
            )
        except Exception as error:  # noqa: BLE001 - job boundary
            logger.exception("job %s (%s) failed", job.id, job.job_type)
            await flush_logs()
            err = error_json(error)
            attempts = job.max_attempts or self.spec.max_attempts
            if (
                err["retryable"]
                and job.attempt < attempts
                and not token.requested
            ):
                backoff = job.attempt * self.spec.poll_interval_seconds
                if backoff > 0:
                    await asyncio.sleep(backoff)
                await self._requeue(job.id, err)
            else:
                await self._finalize(job.id, "failed", error=err)
        finally:
            log_pump.cancel()
            await asyncio.gather(log_pump, return_exceptions=True)
            ccat_logger.removeHandler(log_handler)
            if lowered_level:
                ccat_logger.setLevel(previous_level)
            supervision.cancel()
            await asyncio.gather(supervision, return_exceptions=True)

    async def _write_logs(
        self, job_id: int, lines: list[dict[str, Any]]
    ) -> None:
        """Persist one drained log batch as ``kind="log"`` events.

        Args:
            job_id: The running job's id.
            lines: Event payloads drained from the log buffer.

        Raises:
            Exception: Any database failure, after rolling back.
        """
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            try:
                session.add_all(
                    JobEvent(job_id=job_id, ts=now, kind="log", data=line)
                    for line in lines
                )
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    def _dispatch_progress(
        self,
        loop: asyncio.AbstractEventLoop,
        job_id: int,
        phase: str,
        completed: int,
        failed: int,
        total: int | None,
    ) -> None:
        """Write progress from the loop thread or a worker thread.

        Args:
            loop: The worker's event loop.
            job_id: The running job's id.
            phase: Phase name.
            completed: Completed item count.
            failed: Failed item count.
            total: Optional total item count.
        """
        coroutine = self._write_progress(
            job_id, phase, completed, failed, total
        )
        try:
            current: asyncio.AbstractEventLoop | None = (
                asyncio.get_running_loop()
            )
        except RuntimeError:
            current = None
        if current is loop:
            task = loop.create_task(coroutine)
            # Strong-ref bookkeeping: the loop keeps only weak task
            # references, so an unsaved progress write could be GC'd.
            self._pending.add(task)
            task.add_done_callback(
                lambda done: (
                    self._pending.discard(done),
                    _log_progress_error(done),
                )
            )
        else:
            asyncio.run_coroutine_threadsafe(coroutine, loop).result()

    async def _write_progress(
        self,
        job_id: int,
        phase: str,
        completed: int,
        failed: int,
        total: int | None,
    ) -> None:
        """Persist one progress snapshot plus its throttled event."""
        data = {
            "phase": phase,
            "completed": completed,
            "failed": failed,
            "total": total,
        }
        async with self._progress_lock:
            async with self._session_factory() as session:
                try:
                    job = await session.get(Job, job_id)
                    if job is None:
                        return
                    job.progress = data
                    session.add(
                        JobEvent(
                            job_id=job_id,
                            ts=datetime.now(UTC),
                            kind="progress",
                            data=data,
                        )
                    )
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise

    @asynccontextmanager
    async def _repo_factory(
        self, actor: str
    ) -> "AsyncGenerator[SeasonStoreRepository]":
        """Yield one ad-hoc season repository (ctx.repo_factory)."""
        engine = create_async_engine(self.cfg.storage.database_url)
        try:
            async with UnitOfWork(engine).store(self.cfg, actor) as repo:
                yield repo
        finally:
            await engine.dispose()

    # ------------------------------------------------------------------
    # Terminal writes.
    # ------------------------------------------------------------------

    async def _requeue(self, job_id: int, error: dict[str, Any]) -> None:
        """Requeue one retryable failure with its attempt kept."""
        async with self._session_factory() as session:
            try:
                job = await session.get(Job, job_id)
                if job is None:
                    return
                job.status = "queued"
                job.error = error
                job.leased_until = None
                session.add(
                    JobEvent(
                        job_id=job_id,
                        ts=datetime.now(UTC),
                        kind="retry",
                        data=error,
                    )
                )
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _finalize(
        self,
        job_id: int,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """Write one terminal job state plus its event."""
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            try:
                job = await session.get(Job, job_id)
                if job is None:
                    return
                job.status = status
                job.result = result
                job.error = error
                job.finished_at = now
                job.leased_until = None
                session.add(
                    JobEvent(
                        job_id=job_id,
                        ts=now,
                        kind=status,
                        data={"error": error, "result": result},
                    )
                )
                await session.commit()
            except Exception:
                await session.rollback()
                raise


def _log_progress_error(task: "asyncio.Task[Any]") -> None:
    """Log a fire-and-forget progress write failure."""
    if task.cancelled():
        return
    exception = task.exception()
    if exception is not None:
        logger.warning("progress write failed: %s", exception)


def run_worker(cfg: "Config", config_path: str | Path | None = None) -> None:
    """Run the worker loop until SIGTERM/SIGINT (§6.1; ``ccat worker``).

    Args:
        cfg: Season configuration (worker tuning + database URL).
        config_path: The configuration file to watch; an edited file is
            picked up between jobs.
    """

    async def _main() -> None:
        worker = Worker(cfg, config_path=config_path)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, worker.stop.set)
        await worker.run()

    asyncio.run(_main())


__all__ = ["Worker", "run_worker"]
