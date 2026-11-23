"""DB-mode pipeline services for acquire, screen, and match (§5.1, D12).

These async services run the legacy stage order and semantics exactly,
with every read/write going through the DB-era
:class:`ccat.db.repositories.SeasonStoreRepository` instead of the
workbook.  Pure algorithms are reused verbatim:

- :func:`ccat.preparation.prepare_rows` for cleaning/diagnostics/IDs;
- ``ccat.audit.store`` dict helpers for reconciliation;
- ``ccat.screening`` internals for the external checks and AI work;
- :func:`ccat.match` pure pool/search/reconcile functions.

Blocking legacy work runs in worker threads (:func:`asyncio.to_thread`)
so the worker loop keeps heartbeating; provider calls are never held
inside a DB transaction (§4.4).  Cancellation is cooperative: callers
pass ``cancel``, a callable that raises
:class:`ccat.jobs.errors.JobCancelRequested` when the job should stop;
completed per-child work stays committed (§6.2).
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import polars as pl

from ccat.acquire import AcquisitionError, import_table, pull_tables
from ccat.audit import AuditError
from ccat.audit.store import (
    CHILD_ASSIGNMENT_COLUMNS,
    FAMILY_ASSIGNMENT_COLUMNS,
    SPONSOR_ASSIGNMENT_COLUMNS,
    _prepared_groups,
    _reconcile_rows,
    as_bool,
    eligible_output_rows,
    merge_owned_fields,
    normalize_row,
)
from ccat.db.artifacts import artifact_root, register_file
from ccat.db.uow import UnitOfWork
from ccat.match import (
    MatchError,
    PoolFamily,
    SponsorPlan,
    _angel_sort_key,
    _apply_child_assignments,
    _apply_summaries,
    _duplicate_pool_warnings,
    _excluded_family_report,
    _manual_family_ids,
    _participating_children,
    _prior_child_assignments,
    match_sponsors,
    reconcile_pre_assignments,
    special_needs_weight,
)
from ccat.preparation import (
    dob_notes_by_child,
    duplicate_note,
    prepare_rows,
)
from ccat.screening import (
    ScreeningError,
    _address_engine,
    _apply_flags,
    _email_batch_engine,
    _email_engine,
    _email_log_rows,
    _merge_anomoly,
    _require_opencode_v2,
    _screen_targets,
    dense_final_row,
    first_family_children,
)
from ccat.services.audit import entity_count_changes
from ccat.services.db_assignments import (
    STATE_SPLIT,
    STATE_UNASSIGNED,
    apply_angel_claims,
    apply_family_summary,
    apply_sponsor_summary,
    assignment_state,
    group_children_by_family,
)
from ccat.services.db_duplications import sync_pending_angel_letters
from ccat.services.results import (
    AcquireResult,
    MatchResult,
    ReconcileResult,
    ScreenResult,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Iterable

    from ccat.config import Config
    from ccat.db.repositories import SeasonStoreRepository

logger = logging.getLogger(__name__)

#: Pipeline entity/table names.
_ENTITIES = ("sponsor", "family", "child")


@asynccontextmanager
async def _store(
    cfg: "Config", actor: str
) -> "AsyncGenerator[SeasonStoreRepository]":
    """Yield a short-lived season repository on its own engine.

    Args:
        cfg: Pipeline configuration (season + database URL).
        actor: Attribution identity for the session's writes.

    Yields:
        A repository bound to one fresh session; the engine is
        disposed on exit.  Repository write methods commit their own
        short transactions, so no provider call ever sits inside one.
    """
    from ccat.db.engine import create_async_engine

    engine = create_async_engine(cfg.storage.database_url)
    try:
        async with UnitOfWork(engine).store(cfg, actor) as repo:
            yield repo
    finally:
        await engine.dispose()


def _normalized_stores(
    stores: "dict[str, list[dict[str, Any]]]",
) -> dict[str, list[dict[str, Any]]]:
    """Drop the underscore metadata keys for a value-only comparison.

    Args:
        stores: Entity -> final rows as read from the repository.

    Returns:
        Entity -> normalized legacy-vocabulary rows.
    """
    return {
        entity: [normalize_row(entity, row) for row in rows]
        for entity, rows in stores.items()
    }


def _unversioned(
    rows: "list[dict[str, Any]]",
) -> list[dict[str, Any]]:
    """Drop version/attribution metadata from screen-owned rows.

    Screen is a broad writer persisting its whole in-memory store view
    per commit (matching the legacy workbook write); carrying row
    versions across its own successive commits would trip the
    optimistic-concurrency guard on itself.

    Args:
        rows: Rows as built by the reconcile/AI merge steps.

    Returns:
        Shallow copies without leading-underscore metadata.
    """
    return [
        {k: v for k, v in row.items() if not k.startswith("_")} for row in rows
    ]


def _apply_angel_claims(
    reconciled: "dict[str, list[dict[str, Any]]]",
) -> None:
    """Place the sponsors' Assigned Angel ID claims on the children.

    A sponsor's claim is honoured when the data is loaded, so the audit
    stage sees the child already placed.  A claim another sponsor also
    makes waits for the Duplications section, and a claim that names no
    child leaves a clarification note on the sponsor.

    Args:
        reconciled: The reconciled final stores, updated in place.
    """
    unusable = apply_angel_claims(reconciled["sponsor"], reconciled["child"])
    for angel_id in unusable:
        logger.warning(
            "assigned angel id %s names no child; the sponsor needs a"
            " clarification",
            angel_id,
        )
    apply_family_summary(reconciled["family"], reconciled["child"])
    apply_sponsor_summary(reconciled["sponsor"], reconciled["child"])


def validate_screen_options(
    snapshot: "dict[str, list[dict[str, Any]]]",
    *,
    redo: str | None = None,
) -> None:
    """Reject an invalid redo target before enqueueing (§9.2).

    The worker re-validates against the prepared rows (authoritative);
    this sync pre-check lets the API/jobs layer fail fast on a target
    that is already absent or ignored, before any billable work.

    Args:
        snapshot: ``{"child": final children, "raw_child": raw
            children}`` row lists from a repository snapshot.
        redo: The requested ``--redo`` child id, if any.

    Raises:
        ScreeningError: When the target is unknown or its final row is
            ignored.
    """
    if redo is None:
        return
    keys = {
        str(row.get("zite_record_id") or "")
        for row in snapshot.get("child", [])
    }
    keys |= {
        str(row.get("zite_record_id") or "")
        for row in snapshot.get("raw_child", [])
    }
    if redo not in keys:
        raise ScreeningError(
            f"redo target {redo!r} not found in the child rows"
        )
    for row in snapshot.get("child", []):
        if str(row.get("zite_record_id") or "") == redo and as_bool(
            row.get("ignore")
        ):
            raise ScreeningError(
                f"redo target {redo!r} is ignored; un-ignore it first"
            )


# ---------------------------------------------------------------------------
# Acquire (§9.1).
# ---------------------------------------------------------------------------


async def acquire(
    cfg: "Config",
    actor: str,
    progress: "Callable[..., None]",
    cancel: "Callable[[], None]",
) -> AcquireResult:
    """Pull Zite tables and replace the raw snapshot; never touch finals.

    Args:
        cfg: Pipeline configuration.
        actor: Requesting user/job identity.
        progress: Progress reporter ``(phase, completed, failed, total)``.
        cancel: Cooperative cancel check; raises
            :class:`JobCancelRequested` when cancellation was requested.

    Returns:
        Row counts per raw table.

    Raises:
        AcquisitionError: When the pull/import fails, or when the §9.1
            guard observes any final-entity change.
        JobCancelRequested: When cancellation was requested.
    """
    async with _store(cfg, actor) as repo:
        before = {entity: await repo.final_rows(entity) for entity in _ENTITIES}
    progress("pull", total=3)
    cancel()
    frames = await asyncio.to_thread(pull_tables, cfg)
    raw: dict[str, list[dict[str, Any]]] = {}
    for completed, table in enumerate(_ENTITIES, start=1):
        raw[table] = await asyncio.to_thread(
            import_table, frames[table], table, cfg
        )
        progress("pull", completed=completed, total=3)
    cancel()
    async with _store(cfg, actor) as repo:
        counts = await repo.replace_raw_snapshot("acquire", raw, actor)
        after = {entity: await repo.final_rows(entity) for entity in _ENTITIES}
    if _normalized_stores(after) != _normalized_stores(before):
        # §9.1 guard: structurally unreachable — acquire has no final
        # write rights — and cheap enough to assert outright.
        raise AcquisitionError(
            "final entities changed during acquisition (§9.1 ground-truth"
            " violation); raw snapshot rolled forward but finals must be"
            " inspected"
        )
    return AcquireResult(
        raw_rows={table: counts.get(table, 0) for table in _ENTITIES}
    )


# ---------------------------------------------------------------------------
# External checks with DB replay (§6.4, §9.2).
# ---------------------------------------------------------------------------


async def _check_emails_db(
    repo: "SeasonStoreRepository",
    cfg: "Config",
    rows_by_table: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """ZeroBounce check over new emails only; results append to the DB log.

    Mirrors :func:`ccat.screening.check_emails` but replays against the
    season's ``email_checks`` table instead of the workbook log, and
    reuses screening's internal row builders so log rows stay
    byte-compatible.  API failures stay non-fatal.

    Args:
        repo: Season repository (replay read + log append).
        cfg: Pipeline configuration.
        rows_by_table: Prepared rows keyed by sponsor/family/child.

    Returns:
        The new ``email_validation_log`` rows (possibly empty).
    """
    targets: list[tuple[str, str, str, str]] = []
    fields: dict[str, tuple[str, ...]] = {
        "sponsor": ("email",),
        "family": ("primary_email", "secondary_email"),
    }
    for table_name, table_fields in fields.items():
        sheet = f"{table_name}_submissions"
        for row in rows_by_table.get(table_name, []):
            record_id = str(row.get("zite_record_id") or "")
            for field in table_fields:
                email = row.get(field)
                if email is None or not str(email).strip():
                    continue
                targets.append((sheet, record_id, field, str(email).strip()))
    existing = await repo.email_log_rows()
    checked_emails = {
        str(row.get("email")).strip().lower()
        for row in existing
        if row.get("email")
    }
    pending = [t for t in targets if t[3].lower() not in checked_emails]
    if not pending:
        logger.info("email check: nothing new to check")
        return []
    unique_emails = list(dict.fromkeys(t[3] for t in pending))
    logger.info(
        "email check: %d new email(s) of %d target(s) via ZeroBounce",
        len(unique_emails),
        len(pending),
    )
    validate_email, out_of_credits = _email_engine()
    bulk_validate_emails = _email_batch_engine()
    checked_at = datetime.now(UTC).isoformat(timespec="seconds")
    if bulk_validate_emails is not None:
        try:
            bulk_results = await asyncio.to_thread(
                bulk_validate_emails, unique_emails
            )
        except out_of_credits as error:  # type: ignore[misc]
            logger.error("%s; stopping the email check for this run", error)
            return []
        except Exception as error:  # noqa: BLE001 - fall back per-email
            logger.error(
                "ZeroBounce batch check failed (%s); falling back to"
                " threaded per-email checks",
                error,
            )
        else:
            if bulk_results is not None:
                return _email_log_rows(pending, bulk_results, checked_at)
    else:
        logger.info("email check: batch engine unavailable; threaded checks")

    def _threaded() -> dict[str, dict[str, Any] | None]:
        """Threaded per-email fallback (I/O-bound, threads scale)."""
        results: dict[str, dict[str, Any] | None] = {}
        workers = max(1, min(cfg.email_check.workers, len(unique_emails)))
        from concurrent.futures import ThreadPoolExecutor, as_completed

        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = {
                pool.submit(validate_email, email): email
                for email in unique_emails
            }
            for future in as_completed(futures):
                email = futures[future]
                try:
                    results[email] = future.result()
                except out_of_credits as error:  # type: ignore[misc]
                    logger.error(
                        "%s; stopping the email check for this run", error
                    )
                    break
                except Exception as error:  # noqa: BLE001
                    logger.error("ZeroBounce error for %s: %s", email, error)
                    results[email] = None
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return results

    results = await asyncio.to_thread(_threaded)
    return _email_log_rows(pending, results, checked_at)


async def _check_addresses_db(
    repo: "SeasonStoreRepository",
    cfg: "Config",
    family_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Nominatim check over new family addresses; appends to the DB log.

    Mirrors :func:`ccat.screening.check_addresses`: exact-query replay
    (engine never re-called for a stored query), configured delay
    pacing between requests, engine-level ``status="error"`` results
    persisted per decision D13, and exceptions non-fatal (the affected
    address stays unchecked).

    Args:
        repo: Season repository (replay read + log append).
        cfg: Pipeline configuration.
        family_rows: Prepared family rows.

    Returns:
        The new ``address_validation_log`` rows (possibly empty).
    """
    check_address, build_query = _address_engine()
    targets: list[tuple[str, str, dict[str, Any]]] = []
    for row in family_rows:
        record_id = str(row.get("zite_record_id") or "")
        parts = {
            "street": row.get("street_address"),
            "apt_floor": row.get("apt_floor"),
            "city": row.get("city"),
            "state": row.get("state"),
            "zip_code": row.get("zip_code"),
        }
        if not any(str(value or "").strip() for value in parts.values()):
            continue
        targets.append(("family_submissions", record_id, parts))
    existing = await repo.address_log_rows()
    checked_queries = {
        str(row.get("query")).strip() for row in existing if row.get("query")
    }
    pending: list[tuple[str, str, dict[str, Any], str]] = []
    seen_queries: set[str] = set()
    for sheet, record_id, parts in targets:
        query = build_query(
            parts["street"],
            parts["apt_floor"],
            parts["city"],
            parts["state"],
            parts["zip_code"],
        )
        if query in checked_queries or query in seen_queries:
            continue
        seen_queries.add(query)
        pending.append((sheet, record_id, parts, query))
    if not pending:
        logger.info("address check: nothing new to check")
        return []
    logger.info("address check: %d new address(es) via Nominatim", len(pending))
    log_rows: list[dict[str, Any]] = []
    for index, (sheet, record_id, parts, query) in enumerate(pending):
        try:
            result = await asyncio.to_thread(
                check_address,
                parts["street"],
                apt_floor=parts["apt_floor"],
                city=parts["city"],
                state=parts["state"],
                zip_code=parts["zip_code"],
                base_url=cfg.address_check.base_url,
                user_agent=cfg.address_check.user_agent,
                timeout=cfg.address_check.timeout,
            )
        except Exception as error:  # noqa: BLE001 - keep checking the rest
            logger.error("Nominatim error for %r: %s", query, error)
            continue
        log_rows.append(
            {
                "source_sheet": sheet,
                "zite_record_id": record_id,
                "query": str(result.get("query", query)),
                "status": str(result.get("status", "error")),
                "display_name": result.get("display_name"),
                "lat": result.get("lat"),
                "lon": result.get("lon"),
                "response_json": result.get("response_json"),
                "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        )
        if index < len(pending) - 1:
            await asyncio.sleep(cfg.address_check.delay)
    return log_rows


# ---------------------------------------------------------------------------
# Screen (§9.2).
# ---------------------------------------------------------------------------


async def screen(
    cfg: "Config",
    actor: str,
    options: dict[str, Any],
    progress: "Callable[..., None]",
    cancel: "Callable[[], None]",
) -> ScreenResult:
    """Run the screening stages in the exact legacy order over the DB.

    Stages: prepare raw against finals → redo validation → reconcile
    finals (one commit per entity) → replace diagnostics → email check
    → address check → AI candidate selection with marker reset →
    per-child AI commits.  Completed children stay committed through a
    cancellation (§6.2); ``redo``/``overwrite`` marker resets persist
    before any AI work so failures remain retryable.

    Args:
        cfg: Pipeline configuration.
        actor: Requesting user/job identity.
        options: ``redo`` / ``test`` / ``overwrite`` /
            ``skip_email_check`` / ``skip_address_check``.
        progress: Progress reporter.
        cancel: Cooperative cancel check.

    Returns:
        The typed screening result.

    Raises:
        ScreeningError: On invalid redo targets or AI setup failures.
        JobCancelRequested: When cancellation was requested.
    """
    redo = options.get("redo")
    redo = str(redo) if redo is not None else None
    test = bool(options.get("test"))
    overwrite = bool(options.get("overwrite"))
    skip_email = bool(options.get("skip_email_check"))
    skip_address = bool(options.get("skip_address_check"))

    async with _store(cfg, actor) as repo:
        raw = {table: await repo.raw_rows(table) for table in _ENTITIES}
        before = {entity: await repo.final_rows(entity) for entity in _ENTITIES}
    if not any(raw.values()):
        raise ScreeningError(
            "no raw snapshot for this season; run `ccat acquire` (or"
            " `ccat db-import`) before screening"
        )
    progress("prepare", total=len(raw["child"]))
    preparation = await asyncio.to_thread(prepare_rows, raw, cfg, before)
    children = preparation.rows["child"]
    if redo is not None and not any(
        child.get("zite_record_id") == redo for child in children
    ):
        raise ScreeningError(
            f"redo target {redo!r} not found in the prepared child rows"
        )
    cancel()

    progress("reconcile")
    conflict_notes = dob_notes_by_child(preparation.dob_log)
    async with _store(cfg, actor) as repo:
        reconciled: dict[str, list[dict[str, Any]]] = {}
        for entity in _ENTITIES:
            reconciled[entity] = _reconcile_rows(
                entity,
                before[entity],
                _prepared_groups(preparation, entity),
                preparation,
                cfg,
                True,
            )
        _apply_angel_claims(reconciled)
        for entity in _ENTITIES:
            await repo.commit_finals(
                "screen", entity, _unversioned(reconciled[entity]), actor
            )
        await repo.replace_screen_diagnostics(
            preparation.duplicate_log, preparation.dob_log, actor
        )
        progress(
            "diagnostics",
            completed=len(preparation.duplicate_log) + len(preparation.dob_log),
        )
        cancel()

        email_checked = 0
        if skip_email:
            logger.info("email check: skipped (--skip-email-check)")
        elif not cfg.email_check.enabled:
            logger.info("email check: disabled (config.email_check.enabled)")
        else:
            cancel()
            email_rows = await _check_emails_db(repo, cfg, preparation.rows)
            email_checked = await repo.append_email_log(email_rows, actor)
        cancel()
        address_checked = 0
        if skip_address:
            logger.info("address check: skipped (--skip-address-check)")
        elif not cfg.address_check.enabled:
            logger.info(
                "address check: disabled (config.address_check.enabled)"
            )
        else:
            cancel()
            address_rows = await _check_addresses_db(
                repo, cfg, preparation.rows["family"]
            )
            address_checked = await repo.append_address_log(address_rows, actor)
        cancel()

        final_rows = reconciled["child"]
        by_id = {
            str(row.get("zite_record_id") or ""): row for row in final_rows
        }
        if redo is not None:
            if as_bool(by_id[redo].get("ignore")):
                raise ScreeningError(
                    f"redo target {redo!r} is ignored; un-ignore it first"
                )
            targets = [
                child
                for child in children
                if child.get("zite_record_id") == redo
            ]
        elif overwrite:
            targets = [
                child
                for child in children
                if not as_bool(
                    by_id[str(child.get("zite_record_id") or "")].get("ignore")
                )
            ]
        else:
            targets = [
                child
                for child in children
                if not as_bool(
                    by_id[str(child.get("zite_record_id") or "")].get(
                        "screened"
                    )
                )
                and not as_bool(
                    by_id[str(child.get("zite_record_id") or "")].get("ignore")
                )
            ]
        if test and redo is None:
            allowed = {
                child.get("zite_record_id")
                for child in first_family_children(children)
            }
            targets = [
                child
                for child in targets
                if child.get("zite_record_id") in allowed
            ]
        target_ids = [
            str(child.get("zite_record_id") or "") for child in targets
        ]
        logger.info("children to AI-screen: %d", len(targets))
        progress("ai", total=len(targets))

        if (redo is not None or overwrite) and targets:
            for target in targets:
                existing = by_id[str(target.get("zite_record_id") or "")]
                existing["screened"] = False
            # Marker reset persists BEFORE AI work so interrupted
            # rework stays retryable (§9.2).
            await repo.commit_finals(
                "screen", "child", _unversioned(final_rows), actor
            )
        cancel()

        completed: list[dict[str, Any]] = []
        if targets:
            _require_opencode_v2()
            if cfg.ai_screening.model == "TODO":
                raise ScreeningError(
                    "config.ai_screening.model is still TODO; fill in the"
                    " model id first (§12 item 1)"
                )
            survivors = preparation.survivors["child"]
            validation = preparation.validation_notes["child"]
            loop = asyncio.get_running_loop()
            done_count = {"n": 0}

            def persist(corrections: dict[str, Any]) -> None:
                """Commit one completed child, then honor cancellation."""
                record_id = str(corrections["zite_record_id"])
                child = next(
                    current
                    for current in children
                    if current.get("zite_record_id") == record_id
                )
                dense = dense_final_row(
                    child, corrections, cfg, by_id.get(record_id)
                )
                _apply_flags(
                    dense,
                    duplicate_note(record_id, survivors, "child"),
                    validation.get(record_id, []),
                )
                dense["potential_anomoly"] = _merge_anomoly(
                    dense.get("potential_anomoly"),
                    conflict_notes.get(record_id),
                )
                index = next(
                    position
                    for position, row in enumerate(final_rows)
                    if str(row.get("zite_record_id") or "") == record_id
                )
                final_rows[index] = dense
                by_id[record_id] = dense
                # Blocking bridge: the AI completion thread waits for
                # the loop to commit; the loop stays free for
                # heartbeats (§4.4).
                asyncio.run_coroutine_threadsafe(
                    repo.commit_finals(
                        "screen", "child", _unversioned(final_rows), actor
                    ),
                    loop,
                ).result()
                done_count["n"] += 1
                progress(
                    "ai",
                    completed=done_count["n"],
                    total=len(targets),
                )
                # Completed work stays committed; the next boundary
                # stops the run (§6.2).
                cancel()

            completed = await asyncio.to_thread(
                _screen_targets, targets, cfg, persist
            )
        progress("ai", completed=len(completed), total=len(targets))
        # A pending duplicate group is one angel: every member shares the
        # group's letter until the duplication audit resolves it.  This
        # runs last, because the AI commits above carry the prepared
        # (letterless) duplicate rows.
        await sync_pending_angel_letters(repo, actor)
        after = {entity: await repo.final_rows(entity) for entity in _ENTITIES}
    after_children = {
        str(row.get("zite_record_id") or ""): row for row in after["child"]
    }
    succeeded = [
        record_id
        for record_id in target_ids
        if as_bool(after_children.get(record_id, {}).get("screened"))
    ]
    return ScreenResult(
        counts=entity_count_changes(before, after),
        email_checked=email_checked,
        address_checked=address_checked,
        ai_targets=target_ids,
        ai_succeeded=succeeded,
        ai_failed=[
            record_id for record_id in target_ids if record_id not in succeeded
        ],
        diagnostics_rows=len(preparation.duplicate_log)
        + len(preparation.dob_log),
    )


# ---------------------------------------------------------------------------
# Reconcile (audit startup backstop as an explicit service, §9.3).
# ---------------------------------------------------------------------------


async def reconcile(
    cfg: "Config",
    actor: str,
    progress: "Callable[..., None]",
    cancel: "Callable[[], None]",
) -> ReconcileResult:
    """Seed only genuinely missing final entities (audit backstop).

    Args:
        cfg: Pipeline configuration.
        actor: Requesting user/job identity.
        progress: Progress reporter.
        cancel: Cooperative cancel check.

    Returns:
        Per-entity appended/merged counts.

    Raises:
        PreparationError: When raw rows are structurally invalid.
    """
    async with _store(cfg, actor) as repo:
        raw = {table: await repo.raw_rows(table) for table in _ENTITIES}
        before = {entity: await repo.final_rows(entity) for entity in _ENTITIES}
    if not any(raw.values()) and not any(before.values()):
        raise AuditError(
            "no prepared sponsor/family/child rows; run `ccat acquire`"
            " and `ccat screen` first"
        )
    progress("prepare")
    preparation = await asyncio.to_thread(prepare_rows, raw, cfg, before, False)
    cancel()
    async with _store(cfg, actor) as repo:
        after: dict[str, list[dict[str, Any]]] = {}
        for entity in _ENTITIES:
            rows = _reconcile_rows(
                entity,
                before[entity],
                _prepared_groups(preparation, entity),
                preparation,
                cfg,
                False,
            )
            await repo.commit_finals("audit", entity, rows, actor)
            after[entity] = await repo.final_rows(entity)
    return ReconcileResult(counts=entity_count_changes(before, after))


# ---------------------------------------------------------------------------
# Match (§9.4, D8).
# ---------------------------------------------------------------------------


def _version_map(rows: "Iterable[dict[str, Any]]", key: str) -> dict[str, int]:
    """Map one entity's rows to their optimistic row versions.

    Args:
        rows: Final rows carrying ``_row_version``.
        key: The entity's key column.

    Returns:
        Normalized key -> row version.
    """
    return {
        str(row.get(key)): int(row["_row_version"])
        for row in rows
        if row.get("_row_version") is not None
    }


def _assert_snapshot_current(
    before: dict[str, list[dict[str, Any]]],
    current: dict[str, list[dict[str, Any]]],
) -> None:
    """Fail the match when final rows changed since the snapshot (D8).

    The child store is compared too: the matcher writes child assignments,
    so a manual write during the compute window must fail the run rather
    than be silently overwritten by the complete-store write.

    Args:
        before: The rows the search ran against.
        current: The rows as they stand before the commit.

    Raises:
        MatchError: When any family/sponsor/child key or row version differs
            (no silent mixing of assignments across snapshots).
    """
    keys = {
        "family": "family_id",
        "sponsor": "sponsor_id",
        "child": "zite_record_id",
    }
    for entity, key in keys.items():
        before_map = _version_map(before[entity], key)
        current_map = _version_map(current[entity], key)
        if before_map != current_map:
            changed = sorted(
                one
                for one in set(before_map) | set(current_map)
                if before_map.get(one) != current_map.get(one)
            )
            raise MatchError(
                f"final_{entity} rows changed while matching was running"
                f" (keys {changed}); rerun the match (D8: no silent"
                " mixing of assignments from incompatible snapshots)"
            )


def _match_compute(
    cfg: "Config",
    stored: dict[str, list[dict[str, Any]]],
    *,
    test: bool,
    skip_match: bool,
    holds: dict[int, str] | None = None,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, str]] | None,
]:
    """Run the pure match algorithm over the snapshotted final rows.

    The search and scoring are unchanged; the assignments land on the child
    rows, and the family and sponsor columns are derived from them with the
    shared helpers. Manual child assignments and held families leave the
    automatic pool (D3, D14).

    Args:
        cfg: Pipeline configuration.
        stored: Complete final stores keyed by entity.
        test: Match only the first sponsor in phase order.
        skip_match: Reconcile assignments without the search/report.
        holds: family_id -> "author: note" for the held families.

    Returns:
        The updated family pool, sponsor pool and complete child store, plus
        the report rows (None for ``skip_match``).

    Raises:
        MatchError: On empty pools or duplicate angel assignments.
    """
    stored_family, stored_sponsor, final_child = (
        stored["family"],
        stored["sponsor"],
        stored["child"],
    )
    child_store = [dict(row) for row in final_child]
    family_pool = sorted(
        (dict(row) for row in eligible_output_rows(stored_family, "family")),
        key=lambda row: row["family_id"],
    )
    sponsor_pool = sorted(
        (dict(row) for row in eligible_output_rows(stored_sponsor, "sponsor")),
        key=lambda row: row["sponsor_id"],
    )
    if not family_pool or not sponsor_pool:
        raise MatchError(
            "final_family/final_sponsor stores are empty or contain no"
            " non-ignored rows; run screen to seed persistent finals and"
            " audit to review them"
        )

    if skip_match:
        if test:
            logger.warning("--test has no effect with --skip-match")
        children = _participating_children(
            eligible_output_rows(child_store, "child")
        )
        _apply_child_assignments(
            child_store,
            children,
            prior_assignments=_prior_child_assignments(sponsor_pool, children),
            clear_auto=False,
        )
        _apply_summaries(family_pool, sponsor_pool, child_store)
        return family_pool, sponsor_pool, child_store, None

    linked_children = eligible_output_rows(child_store, "child")
    children = _participating_children(linked_children)
    logger.info("participating audited children: %d", len(children))

    pre_assigned, report = reconcile_pre_assignments(sponsor_pool, children)
    pre_assigned_angels = {
        angel_id
        for plan in pre_assigned.values()
        for angel_id in plan.assigned_angel_ids
    }
    children_by_family: dict[int, list[dict[str, Any]]] = {}
    for child in children:
        children_by_family.setdefault(child["family_id"], []).append(child)

    manual_families = _manual_family_ids(child_store)
    held = holds or {}
    family_by_id = {row["family_id"]: row for row in family_pool}
    pool_families = [
        PoolFamily(
            family_id=family_id,
            family_row=family_by_id[family_id],
            children=family_children,
        )
        for family_id, family_children in sorted(children_by_family.items())
        if family_id in family_by_id
        and family_id not in manual_families
        and family_id not in held
        and not any(
            child["angel_id"] in pre_assigned_angels
            for child in family_children
        )
    ]
    pool_sponsors = [
        SponsorPlan(
            sponsor_row=row,
            sponsor_id=row["sponsor_id"],
            num_children=int(row["num_children"]),
            is_special_needs=row.get("age_preference") == "Special Needs",
            is_pre_assigned=False,
        )
        for row in sponsor_pool
        if row["sponsor_id"] not in pre_assigned
    ]
    sn_weight = special_needs_weight(
        cfg, max((plan.num_children for plan in pool_sponsors), default=25)
    )
    assignments, sponsor_report = match_sponsors(
        pool_sponsors, pool_families, cfg, sn_weight, test_mode=test
    )
    report.extend(sponsor_report)
    if test:
        report.append(
            {
                "kind": "test",
                "ref": "-",
                "reason": (
                    "partial test run: only the first sponsor in phase"
                    " order was matched"
                ),
            }
        )
    report.extend(_excluded_family_report(family_pool, manual_families, held))

    pre_family_assignments: dict[int, int] = {}
    for plan in pre_assigned.values():
        covered = {
            child["family_id"]
            for child in children
            if child["angel_id"] in plan.assigned_angel_ids
        }
        for family_id in covered:
            family_children = children_by_family.get(family_id, [])
            fully = all(
                child["angel_id"] in plan.assigned_angel_ids
                for child in family_children
            )
            if fully and family_id not in pre_family_assignments:
                pre_family_assignments[family_id] = plan.sponsor_id

    matched_family_ids = set(assignments) | set(pre_family_assignments)
    for family in pool_families:
        if family.family_id not in matched_family_ids:
            if any(
                child.get("gender") == "Non-binary" for child in family.children
            ):
                reason = "non-binary children with no Any Gender sponsor"
            else:
                reason = "no exact count combination"
            report.append(
                {
                    "kind": "family",
                    "ref": str(family.family_id),
                    "reason": reason,
                }
            )
    for family_row in family_pool:
        family_id = family_row["family_id"]
        if (
            family_id not in children_by_family
            and family_id not in matched_family_ids
        ):
            report.append(
                {
                    "kind": "family",
                    "ref": str(family_id),
                    "reason": "no audited children (not in pool)",
                }
            )

    prior_assignments = {
        angel_id: plan.sponsor_id
        for plan in pre_assigned.values()
        for angel_id in plan.assigned_angel_ids
    }
    _apply_child_assignments(
        child_store,
        children,
        family_assignments=assignments,
        prior_assignments=prior_assignments,
        clear_auto=True,
    )
    _apply_summaries(family_pool, sponsor_pool, child_store)

    report.extend(_duplicate_pool_warnings(family_pool, sponsor_pool))
    return family_pool, sponsor_pool, child_store, report


async def match(
    cfg: "Config",
    actor: str,
    options: dict[str, Any],
    progress: "Callable[..., None]",
    cancel: "Callable[[], None]",
) -> MatchResult:
    """Run matchmaking over the DB finals and commit one atomic run.

    The deterministic search runs against a version snapshot with no
    write transaction open; the snapshot is re-verified before the
    single commit (D8), child rows included.  ``skip_match`` reconciles
    assignments without a report.  The child, family and sponsor stores
    commit in one transaction; the match CSV becomes a tracked artifact
    written after it, whose failure leaves ``csv_export_ok=False`` (§9.4).

    Args:
        cfg: Pipeline configuration.
        actor: Requesting user/job identity.
        options: ``test`` / ``overwrite`` / ``skip_match``.
        progress: Progress reporter.
        cancel: Cooperative cancel check.

    Returns:
        The typed match result, counted at the child grain.

    Raises:
        MatchError: On empty pools, duplicate assignments, or a
            snapshot revision conflict.
        JobCancelRequested: When cancellation was requested.
    """
    test = bool(options.get("test"))
    overwrite = bool(options.get("overwrite"))
    skip_match = bool(options.get("skip_match"))

    async with _store(cfg, actor) as repo:
        stored = {entity: await repo.final_rows(entity) for entity in _ENTITIES}
        hold_rows = await repo.family_holds(
            int(row["family_id"])
            for row in stored["family"]
            if row.get("family_id") is not None
        )
    holds = {
        family_id: f"{row['created_by']}: {row['note']}"
        for family_id, row in hold_rows.items()
    }
    progress("pools", total=len(stored["child"]))
    family_pool, sponsor_pool, child_pool, report = await asyncio.to_thread(
        _match_compute,
        cfg,
        stored,
        test=test,
        skip_match=skip_match,
        holds=holds,
    )
    cancel()
    progress("commit", total=len(report or []))

    async with _store(cfg, actor) as repo:
        current = {
            entity: await repo.final_rows(entity) for entity in _ENTITIES
        }
    _assert_snapshot_current(stored, current)

    async with _store(cfg, actor) as repo:
        merged_family = merge_owned_fields(
            stored["family"], family_pool, "family", FAMILY_ASSIGNMENT_COLUMNS
        )
        merged_sponsor = merge_owned_fields(
            stored["sponsor"],
            sponsor_pool,
            "sponsor",
            SPONSOR_ASSIGNMENT_COLUMNS,
        )
        merged_child = merge_owned_fields(
            stored["child"], child_pool, "child", CHILD_ASSIGNMENT_COLUMNS
        )
        commit = await repo.commit_match(
            merged_family,
            merged_sponsor,
            report,
            actor,
            child_rows=merged_child,
            overwrite=overwrite,
            test_mode=test,
        )
        csv_export_ok = True
        csv_path = ""
        if report is not None:
            try:
                root = artifact_root(cfg)
                target = root / "match" / f"match_report_{commit.run_id}.csv"
                target.parent.mkdir(parents=True, exist_ok=True)
                pl.DataFrame(
                    report,
                    schema={
                        "kind": pl.String,
                        "ref": pl.String,
                        "reason": pl.String,
                    },
                ).write_csv(target)
                await register_file(
                    repo,
                    "csv_export",
                    target,
                    actor,
                    meta={"match_run_id": commit.run_id},
                    root=root,
                )
                csv_path = str(target)
            except Exception as error:  # noqa: BLE001 - explicit partial
                logger.error("match CSV artifact failed: %s", error)
                csv_export_ok = False
        post = {entity: await repo.final_rows(entity) for entity in _ENTITIES}

    families = eligible_output_rows(post["family"], "family")
    sponsors = eligible_output_rows(post["sponsor"], "sponsor")
    grouped = group_children_by_family(post["child"])
    matched_families = 0
    unmatched_family_ids: list[int] = []
    split_family_ids: list[int] = []
    for row in families:
        family_id = int(row["family_id"])
        state = assignment_state(grouped.get(row["family_id"], []))
        if state == STATE_SPLIT:
            split_family_ids.append(family_id)
        elif state == STATE_UNASSIGNED:
            unmatched_family_ids.append(family_id)
        else:
            matched_families += 1
    angel_ids = {
        angel.strip()
        for row in sponsors
        if row.get("assigned_angel_ids")
        for angel in str(row["assigned_angel_ids"]).split(",")
        if angel.strip()
    }
    return MatchResult(
        matched_families=matched_families,
        matched_sponsors=sum(
            1 for row in sponsors if row.get("assigned_angel_ids")
        ),
        assigned_angel_ids=sorted(angel_ids, key=_angel_sort_key),
        unmatched_family_ids=sorted(unmatched_family_ids),
        split_family_ids=sorted(split_family_ids),
        unmatched_sponsor_ids=[
            int(row["sponsor_id"])
            for row in sponsors
            if not row.get("assigned_angel_ids")
        ],
        report_rows=len(report or []),
        archived=report is not None,
        csv_path=csv_path or str(cfg.match_report_csv),
        run_id=commit.run_id,
        csv_export_ok=csv_export_ok,
    )


__all__ = [
    "acquire",
    "match",
    "reconcile",
    "screen",
    "validate_screen_options",
]
