"""Season routes: list, current, and the server-computed overview (§7.1.2)."""

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select

from ccat.api.auth import AuthUser, current_user
from ccat.api.deps import season_repo
from ccat.api.dto import Overview, SeasonView
from ccat.db.models import AddressCheck, EmailCheck, Job, Season
from ccat.db.repositories import SeasonStoreRepository
from ccat.services import db_duplications

if TYPE_CHECKING:
    from ccat.db.uow import UnitOfWork

router = APIRouter(prefix="/seasons", tags=["seasons"])


@router.get("", response_model=list[SeasonView])
async def list_seasons(
    request: Request,
    _user: AuthUser = Depends(current_user),
) -> list[SeasonView]:
    """List every stored season (any authenticated user).

    Args:
        request: The current request.
        _user: Authenticated identity.

    Returns:
        All season rows.
    """
    uow: UnitOfWork = request.app.state.uow
    async with uow.session() as session:
        rows = (await session.scalars(select(Season).order_by(Season.id))).all()
        return [
            SeasonView(id=row.id, name=row.name, xmas_date=row.xmas_date)
            for row in rows
        ]


@router.get("/current", response_model=SeasonView)
async def current_season(request: Request) -> SeasonView:
    """Return the configured (active) season (D3/D4).

    Args:
        request: The current request.

    Returns:
        The configured season's identity; the id is included only when
        the season row exists in the database.
    """
    cfg = request.app.state.cfg
    uow: UnitOfWork = request.app.state.uow
    async with uow.session() as session:
        row = await session.scalar(
            select(Season).where(Season.name == cfg.season)
        )
    return SeasonView(
        id=row.id if row else None,
        name=cfg.season,
        xmas_date=cfg.xmas_date,
    )


def _entity_counts(entity: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute one entity's overview counts (§7.1.2).

    Args:
        entity: "child", "family", or "sponsor".
        rows: The complete final rows.

    Returns:
        total, by_status (all five), ignored, orphans, raw_missing,
        pending_ai, generated, and assigned counts.  ``raw_missing``
        is filled by the caller (needs the raw snapshot).
    """
    from ccat.audit.store import STATUS_ORDER, as_bool

    by_status = {status: 0 for status in STATUS_ORDER}
    ignored = orphans = pending_ai = generated = assigned = 0
    for row in rows:
        status = str(row.get("status") or "new")
        by_status[status] = by_status.get(status, 0) + 1
        if as_bool(row.get("ignore")):
            ignored += 1
        if row.get("family_id") is None:
            orphans += 1
        if entity == "child":
            if not as_bool(row.get("screened")) and not as_bool(
                row.get("ignore")
            ):
                pending_ai += 1
            if as_bool(row.get("generated_info_sheet")):
                generated += 1
        if entity == "family" and row.get("assigned_sponsor_id") is not None:
            assigned += 1
        if entity == "sponsor" and row.get("assigned_angel_ids"):
            assigned += 1
    return {
        "total": len(rows),
        "by_status": by_status,
        "ignored": ignored,
        "orphans": orphans,
        "raw_missing": 0,
        "pending_ai": pending_ai,
        "generated": generated,
        "assigned": assigned,
    }


@router.get("/{season}/overview", response_model=Overview)
async def overview(
    request: Request,
    user: AuthUser = Depends(current_user),
    repo: SeasonStoreRepository = Depends(season_repo),
) -> Overview:
    """Return the rich server-computed season overview (§7.1.2).

    Every count is computed server-side; the client never aggregates
    hidden rows.  Includes per-entity status counts, diagnostics
    counts, artifact counts by kind, the last job per type, and the
    data revision (max ``last_updated_at``).

    Args:
        request: The current request.
        user: The authenticated identity.
        repo: The season-bound repository.

    Returns:
        The overview payload.
    """
    cfg = request.app.state.cfg
    uow: UnitOfWork = request.app.state.uow
    entities: dict[str, dict[str, Any]] = {}
    revision: str | None = None
    raw_by_table: dict[str, list[dict[str, Any]]] = {}
    for entity in ("child", "family", "sponsor"):
        rows = await repo.final_rows(entity)
        raw_by_table[entity] = await repo.raw_rows(entity)
        counts = _entity_counts(entity, rows)
        raw_ids = {
            str(row.get("zite_record_id") or "") for row in raw_by_table[entity]
        }
        counts["raw_missing"] = sum(
            1
            for row in rows
            if str(row.get("zite_record_id") or "") not in raw_ids
        )
        entities[entity] = counts
        for row in rows:
            stamp = row.get("_last_updated_at")
            if stamp and (revision is None or str(stamp) > revision):
                revision = str(stamp)
    duplicate_rows = await repo.duplicate_log_rows()
    duplication = await db_duplications.status_counts(repo)
    entities["duplication"] = {
        "total": sum(duplication.values()),
        "by_status": duplication,
    }
    dob_rows = await repo.dob_log_rows()
    async with uow.session() as session:
        season_id_row = await session.scalar(
            select(Season.id).where(Season.name == cfg.season)
        )
        email_count = await session.scalar(
            select(func.count()).where(EmailCheck.season_id == season_id_row)
        )
        address_count = await session.scalar(
            select(func.count()).where(AddressCheck.season_id == season_id_row)
        )
    artifacts: dict[str, int] = {}
    for artifact in await repo.artifact_rows():
        kind = str(artifact.get("kind") or "other")
        artifacts[kind] = artifacts.get(kind, 0) + 1
    last_jobs: dict[str, dict[str, Any]] = {}
    async with uow.session() as session:
        jobs = (
            await session.scalars(
                select(Job)
                .where(Job.season_id == season_id_row)
                .order_by(Job.id.desc())
            )
        ).all()
        for job in jobs:
            if job.job_type not in last_jobs:
                last_jobs[job.job_type] = {
                    "id": job.id,
                    "status": job.status,
                    "created_at": (
                        job.created_at.isoformat() if job.created_at else None
                    ),
                    "finished_at": (
                        job.finished_at.isoformat() if job.finished_at else None
                    ),
                }
    return Overview(
        season=cfg.season,
        entities=entities,
        diagnostics={
            "duplicate_groups": len(
                {
                    row.get("group_key")
                    for row in duplicate_rows
                    if row.get("group_key")
                }
            ),
            "dob_conflicts": len(dob_rows),
            "email_checks": int(email_count or 0),
            "address_checks": int(address_count or 0),
        },
        artifacts=artifacts,
        last_jobs=last_jobs,
        data_revision=revision,
    )


__all__ = ["router"]
