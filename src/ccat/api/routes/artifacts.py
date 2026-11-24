"""Artifact routes: metadata list + contained download (§6.3, §5.2)."""

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse

from ccat.api.auth import AuthUser, current_user, record_event, require_csrf
from ccat.api.deps import season_repo
from ccat.api.dto import ArtifactView
from ccat.db.artifacts import resolve_download
from ccat.db.models import Artifact, Season
from ccat.db.repositories import SeasonStoreRepository
from ccat.api.errors import ApiError

if TYPE_CHECKING:
    from ccat.config import Config

router = APIRouter(tags=["artifacts"])

#: Kinds holding child/family PII; their downloads are audited (§8).
SENSITIVE_KINDS = frozenset(
    {
        "child_sheet",
        "sponsor_packet",
        "clarify_draft",
        "clarify_draft_fragment",
        "csv_export",
        "stats_report",
    }
)


@router.get(
    "/seasons/{season}/artifacts",
    response_model=list[ArtifactView],
)
async def list_artifacts(
    season: str,
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    kind: str | None = Query(None),
    _user: AuthUser = Depends(current_user),
) -> list[ArtifactView]:
    """List the season's artifacts (any authenticated role).

    Args:
        season: The season name.
        request: The current request.
        repo: The season-bound repository.
        kind: Optional kind filter.
        _user: Authenticated identity.

    Returns:
        The artifact metadata views.
    """
    rows = await repo.artifact_rows(kind)
    return [ArtifactView(**row) for row in rows]


@router.get("/artifacts/{artifact_id}/download")
async def download_artifact(
    artifact_id: int,
    request: Request,
    inline: bool = Query(False),
    _user: AuthUser = Depends(current_user),
    _csrf: None = Depends(require_csrf),
) -> FileResponse:
    """Download one artifact (any authenticated role; no-store).

    The id resolves only to a path inside the canonical artifact root
    (:func:`ccat.db.artifacts.resolve_download`); sensitive kinds are
    recorded in the security-audit events (§8).

    Args:
        artifact_id: The artifact id.
        request: The current request.
        inline: Display PDF artifacts inline instead of downloading.
        _user: Authenticated identity.
        _csrf: CSRF guard.

    Returns:
        The file response.

    Raises:
        ApiError: 404 unknown artifact/missing file; 403 path escape.
    """
    cfg: Config = request.app.state.cfg
    uow = request.app.state.uow
    async with uow.session() as session:
        row = await session.get(Artifact, artifact_id)
        if row is None:
            raise ApiError(
                404, "artifact_not_found", f"artifact {artifact_id} not found"
            )
        season = await session.get(Season, row.season_id)
        repo = SeasonStoreRepository(session, season)
        path, content_type, filename = await resolve_download(
            repo, artifact_id, cfg
        )
        kind = row.kind
        season_name = str(season.name)
    if kind in SENSITIVE_KINDS:
        await record_event(
            uow.session_factory,
            "artifact_download",
            user_id=_user.id,
            object_type="artifact",
            object_id=str(artifact_id),
            season=season_name,
            details={"kind": kind},
        )
    disposition = (
        "inline"
        if inline and content_type == "application/pdf"
        else "attachment"
    )
    return FileResponse(
        path,
        media_type=content_type,
        filename=filename,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'{disposition}; filename="{filename}"',
        },
    )


__all__ = ["router"]
