"""Audit entity routes: lists, detail, PATCH, ignore/unignore (§5.2, §9.3).

Lists intentionally INCLUDE ignored/orphan/raw-missing rows (plan §5.2)
and use the stored status.  PATCH requires ``If-Match: <row_version>``
(412 when missing — the documented choice over 428) and applies the
tri-state :class:`EntityPatch` through the shared DB audit save
(:mod:`ccat.services.db_audit`); a stale version returns 412 with the
FRESH server state and current version so the editor can rebase
(A/B scenario, plan §4.4).  Row values cross the DTO boundary under
their canonical names (``ai_screening_notes``, never
``potential_anomoly``).
"""

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse

from ccat.api.auth import AuthUser, require_csrf
from ccat.api.auth import require_roles
from ccat.api.deps import season_repo
from ccat.api.dto import (
    EntityDetail,
    EntityListPage,
    EntityPatch,
    IgnoreRequest,
    canonicalize,
    decanonicalize_patch,
)
from ccat.api.errors import ApiError
from ccat.db.repositories import SeasonStoreRepository
from ccat.db.errors import StaleRowVersionError
from ccat.services import db_audit
from ccat.services.audit import SavePatch

if TYPE_CHECKING:
    from ccat.config import Config

router = APIRouter(prefix="/seasons/{season}", tags=["audit"])

#: URL segment -> entity name.
_SEGMENTS: dict[str, str] = {
    "sponsors": "sponsor",
    "families": "family",
    "children": "child",
}

#: Entity name -> the column holding its assigned id.
_KEY_COLUMN: dict[str, str] = {
    "child": "zite_record_id",
    "family": "family_id",
    "sponsor": "sponsor_id",
}


def _entity(segment: str) -> str:
    """Map a URL segment to the entity name.

    Args:
        segment: "sponsors"/"families"/"children".

    Returns:
        "sponsor"/"family"/"child".
    """
    try:
        return _SEGMENTS[segment]
    except KeyError:
        raise ApiError(
            404, "not_found", f"unknown entity {segment!r}"
        ) from None


def _match(row: dict[str, Any], q: str) -> bool:
    """Case-insensitive substring match on name/email/id (§5.2).

    Args:
        row: The final row.
        q: The search query.

    Returns:
        True when any searchable field contains the query.
    """
    needle = q.strip().lower()
    if not needle:
        return True
    haystack = [
        row.get("first_name"),
        row.get("last_name"),
        row.get("primary_email"),
        row.get("email"),
        row.get("zite_record_id"),
        row.get("family_id"),
        row.get("sponsor_id"),
    ]
    return any(
        value is not None and needle in str(value).lower() for value in haystack
    )


def _public_row(entity: str, row: dict[str, Any]) -> dict[str, Any]:
    """Render one final row for the wire (canonical names, no cache).

    Args:
        entity: The entity name.
        row: The stored row with ``_``-prefixed metadata.

    Returns:
        The wire dict.
    """
    public = {
        key: value for key, value in row.items() if not key.startswith("_")
    }
    return canonicalize(entity, public)


async def _detail_payload(
    repo: SeasonStoreRepository, cfg: "Config", entity: str, key: str
) -> EntityDetail:
    """Build one entity's full detail (§5.2).

    Runs the shared preparation for the baseline and duplicate
    submissions (the store's own semantics, plan §9.3) and merges the
    child's DOB-conflict note into ``diagnostics``; families and
    sponsors carry their email/address validation status there.

    Args:
        repo: The season-bound repository.
        cfg: Pipeline configuration.
        entity: The entity name.
        key: The entity key as text.

    Returns:
        The detail view.

    Raises:
        ApiError: 404 when the entity row is absent.
    """
    from ccat.audit.store import dob_conflict_notes
    from ccat.services.db_audit import _prepared_and_baseline

    spec_key = _KEY_COLUMN[entity]
    rows = await repo.final_rows(entity)
    existing = next(
        (row for row in rows if str(row.get(spec_key)) == str(key)), None
    )
    if existing is None:
        raise ApiError(404, "not_found", f"unknown {entity} {key!r}")
    prepared, baseline = await _prepared_and_baseline(
        repo, cfg, entity, key, existing
    )
    diagnostics: dict[str, Any] = {}
    if entity == "child":
        log_rows = await repo.dob_log_rows()
        note = dob_conflict_notes(log_rows, str(existing.get(spec_key)))
        if note:
            diagnostics["conflict_note"] = note
    elif entity in {"family", "sponsor"}:
        statuses = await db_audit.validation_statuses(repo, entity, [existing])
        entry = statuses.get(str(existing.get(spec_key)))
        if entry is not None:
            diagnostics["email_status"] = entry["email"]
            diagnostics["address_status"] = entry["address"]
    submissions: list[dict[str, Any]] = []
    if prepared is not None:
        from ccat.audit.store import group_submissions

        groups = group_submissions(await _group_rows(cfg, repo, entity), entity)
        email = prepared.get("primary_email") or prepared.get("email")
        group_key = (
            (email, prepared.get("first_name"))
            if entity == "child"
            else (email,)
        )
        submissions = list(groups.get(group_key, []))
    return EntityDetail(
        entity=entity,
        key=str(existing.get(spec_key)),
        row=_public_row(entity, existing),
        baseline=canonicalize(entity, baseline),
        submissions=submissions,
        status=str(existing.get("status") or "new"),
        row_version=int(existing.get("_row_version") or 1),
        last_updated_by=existing.get("_last_updated_by"),
        last_updated_at=existing.get("_last_updated_at"),
        audited_at=existing.get("audited_at"),
        diagnostics=diagnostics,
    )


async def _group_rows(
    cfg: "Config", repo: SeasonStoreRepository, entity: str
) -> list[dict[str, Any]]:
    """Return the entity's prepared rows for duplicate grouping.

    Args:
        cfg: Pipeline configuration.
        repo: The season-bound repository.
        entity: The entity name.

    Returns:
        The prepared rows (raw-based preparation output).
    """
    from ccat.preparation import prepare_rows

    raw = {t: await repo.raw_rows(t) for t in ("sponsor", "family", "child")}
    finals = {
        t: await repo.final_rows(t) for t in ("sponsor", "family", "child")
    }
    preparation = prepare_rows(raw, cfg, finals, report=False)
    return preparation.rows[entity]


@router.get("/sponsors", response_model=EntityListPage)
async def list_sponsors(
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    q: str = "",
    status: str | None = None,
) -> EntityListPage:
    """List sponsors — ignored/orphan/raw-missing rows included (§5.2).

    Args:
        request: The current request.
        repo: The season-bound repository.
        page: 1-based page number.
        page_size: Page size (max 200).
        q: Search on name/email/id.
        status: Stored-status filter.

    Returns:
        One page of rows.
    """
    return await _list("sponsor", repo, page, page_size, q, status)


@router.get("/families", response_model=EntityListPage)
async def list_families(
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    q: str = "",
    status: str | None = None,
) -> EntityListPage:
    """List families — ignored/orphan/raw-missing rows included (§5.2).

    Args:
        request: The current request.
        repo: The season-bound repository.
        page: 1-based page number.
        page_size: Page size (max 200).
        q: Search on name/email/id.
        status: Stored-status filter.

    Returns:
        One page of rows.
    """
    return await _list("family", repo, page, page_size, q, status)


@router.get("/children", response_model=EntityListPage)
async def list_children(
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    q: str = "",
    status: str | None = None,
) -> EntityListPage:
    """List children — ignored/orphan/raw-missing rows included (§5.2).

    Args:
        request: The current request.
        repo: The season-bound repository.
        page: 1-based page number.
        page_size: Page size (max 200).
        q: Search on name/email/id.
        status: Stored-status filter.

    Returns:
        One page of rows.
    """
    return await _list("child", repo, page, page_size, q, status)


async def _list(
    entity: str,
    repo: SeasonStoreRepository,
    page: int,
    page_size: int,
    q: str,
    status: str | None,
) -> EntityListPage:
    """Shared list implementation (sort → filter → paginate → canonicalize).

    Args:
        entity: The entity name.
        repo: The season-bound repository.
        page: Page number.
        page_size: Page size.
        q: Search query.
        status: Stored-status filter.

    Returns:
        The page DTO.
    """
    rows = await repo.final_rows(entity)
    rows = sorted(rows, key=lambda row: db_audit.audit_sort_key(entity, row))
    filtered = [
        row
        for row in rows
        if (status is None or row.get("status") == status) and _match(row, q)
    ]
    start = (page - 1) * page_size
    items = [
        _public_row(entity, row) for row in filtered[start : start + page_size]
    ]
    if entity == "family":
        counts = await repo.family_child_counts()
        for item in items:
            item["child_count"] = counts.get(int(item["family_id"]), 0)
    elif entity == "child":
        raw_ids = {
            str(row.get("zite_record_id") or "")
            for row in await repo.raw_rows("child")
        }
        for item in items:
            item["raw_missing"] = (
                str(item.get("zite_record_id") or "") not in raw_ids
            )
    if entity in {"family", "sponsor"}:
        statuses = await db_audit.validation_statuses(repo, entity, filtered)
        for item in items:
            entry = statuses.get(str(item.get(_KEY_COLUMN[entity])))
            if entry is not None:
                item["email_status"] = entry["email"]
                item["address_status"] = entry["address"]
    return EntityListPage(
        items=items,
        total=len(filtered),
        page=page,
        page_size=page_size,
    )


async def _entity_detail(
    entity: str,
    key: str,
    request: Request,
    repo: SeasonStoreRepository,
) -> EntityDetail:
    """Shared detail implementation.

    Args:
        entity: The entity name.
        key: The entity key as text.
        request: The current request.
        repo: The season-bound repository.

    Returns:
        The detail DTO.
    """
    cfg: Config = request.app.state.cfg
    return await _detail_payload(repo, cfg, entity, key)


def _detail_routes(segment: str, entity: str) -> None:
    """Register detail/PATCH/ignore/unignore for one entity segment.

    Args:
        segment: The URL segment ("sponsors"/"families"/"children").
        entity: The entity name.
    """

    @router.get(
        f"/{segment}/{{key}}",
        response_model=EntityDetail,
    )
    async def get_entity(
        key: str,
        request: Request,
        repo: SeasonStoreRepository = Depends(season_repo),
    ) -> EntityDetail:
        """One entity's full detail (baseline + submissions + version).

        Args:
            key: The entity key.
            request: The current request.
            repo: The season-bound repository.

        Returns:
            The detail DTO.
        """
        return await _entity_detail(entity, key, request, repo)

    @router.patch(
        f"/{segment}/{{key}}",
        response_model=EntityDetail,
        dependencies=[Depends(require_csrf)],
    )
    async def patch_entity(
        key: str,
        # The patch model is built at runtime, so the checker cannot
        # treat it as a class.
        body: EntityPatch,  # ty: ignore[invalid-type-form]
        request: Request,
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
        repo: SeasonStoreRepository = Depends(season_repo),
        user: AuthUser = Depends(require_roles("auditor")),
    ) -> Any:
        """Apply one tri-state save (auditor; If-Match required).

        Args:
            key: The entity key.
            body: The patch (omitted = unchanged, null = clear).
            request: The current request.
            if_match: The required row version.
            repo: The season-bound repository.
            user: The authenticated identity.

        Returns:
            The updated detail.

        Raises:
            ApiError: 412 missing/stale If-Match (stale carries the
                fresh row + current version); 409 domain refusals.
        """
        cfg: Config = request.app.state.cfg
        if not if_match:
            raise ApiError(
                412,
                "stale_version",
                "If-Match row_version header is required for saves",
            )
        try:
            expected = int(if_match)
        except ValueError:
            raise ApiError(
                422, "invalid_input", "If-Match must be an int"
            ) from None
        provided = {
            field: getattr(body, field) for field in body.model_fields_set
        }
        patch = SavePatch(**decanonicalize_patch(provided))
        try:
            await db_audit.save_entity_db(
                repo,
                cfg,
                entity,
                key,
                patch,
                user.username,
                expected_version=expected,
            )
        except StaleRowVersionError as error:
            return await _stale_response(repo, entity, key, error)
        return await _entity_detail(entity, key, request, repo)

    @router.post(
        f"/{segment}/{{key}}/ignore",
        response_model=EntityDetail,
        dependencies=[Depends(require_csrf)],
    )
    async def ignore_entity(
        key: str,
        body: IgnoreRequest,
        request: Request,
        repo: SeasonStoreRepository = Depends(season_repo),
        user: AuthUser = Depends(require_roles("auditor")),
    ) -> Any:
        """Ignore one entity (destructive skeleton reset, §5.5).

        Args:
            key: The entity key.
            body: Confirmation + expected version.
            request: The current request.
            repo: The season-bound repository.
            user: The authenticated identity.

        Returns:
            The updated detail.
        """
        return await _control(entity, key, body, request, repo, user, True)

    @router.post(
        f"/{segment}/{{key}}/unignore",
        response_model=EntityDetail,
        dependencies=[Depends(require_csrf)],
    )
    async def unignore_entity(
        key: str,
        body: IgnoreRequest,
        request: Request,
        repo: SeasonStoreRepository = Depends(season_repo),
        user: AuthUser = Depends(require_roles("auditor")),
    ) -> Any:
        """Un-ignore one entity (baseline reseed, §5.5).

        Args:
            key: The entity key.
            body: Confirmation + expected version.
            request: The current request.
            repo: The season-bound repository.
            user: The authenticated identity.

        Returns:
            The updated detail.
        """
        return await _control(entity, key, body, request, repo, user, False)


for _segment, _entity_name in _SEGMENTS.items():
    _detail_routes(_segment, _entity_name)


async def _control(
    entity: str,
    key: str,
    body: IgnoreRequest,
    request: Request,
    repo: SeasonStoreRepository,
    user: AuthUser,
    ignore: bool,
) -> Any:
    """Shared ignore/unignore implementation (auditor; confirmed).

    Args:
        entity: The entity name.
        key: The entity key.
        body: Confirmation + expected version.
        request: The current request.
        repo: The season-bound repository.
        user: The authenticated identity.
        ignore: True to ignore, False to unignore.

    Returns:
        The updated detail.

    Raises:
        ApiError: 422 without confirmation; 412 stale version.
    """
    cfg: Config = request.app.state.cfg
    if not body.confirm:
        raise ApiError(
            422,
            "invalid_input",
            f"ignoring {entity} {key!r} requires confirm=True",
        )
    kwargs = {
        "confirm": body.confirm,
        "expected_version": body.expected_version,
    }
    try:
        if ignore:
            await db_audit.ignore_entity_db(
                repo, cfg, entity, key, user.username, **kwargs
            )
        else:
            await db_audit.unignore_entity_db(
                repo, cfg, entity, key, user.username, **kwargs
            )
    except StaleRowVersionError as error:
        return await _stale_response(repo, entity, key, error)
    return await _entity_detail(entity, key, request, repo)


async def _stale_response(
    repo: SeasonStoreRepository,
    entity: str,
    key: str,
    error: StaleRowVersionError,
) -> JSONResponse:
    """Build the 412 body with the FRESH server state (§4.4).

    Re-reads the row after the rolled-back commit so the stale editor
    sees the winner's values, version, and attribution.

    Args:
        repo: The season-bound repository (usable after rollback).
        entity: The entity name.
        key: The entity key.
        error: The raised stale-version error.

    Returns:
        The 412 JSON response.
    """
    spec_key = _KEY_COLUMN[entity]
    rows = await repo.final_rows(entity)
    existing = next(
        (row for row in rows if str(row.get(spec_key)) == str(key)), None
    )
    fresh = _public_row(entity, existing) if existing else None
    return JSONResponse(
        status_code=412,
        content={
            "detail": {
                "code": "stale_version",
                "message": str(error),
                "entity": entity,
                "key": str(key),
                "current_version": error.current_version,
                "row": fresh,
                "last_updated_by": (
                    existing.get("_last_updated_by") if existing else None
                ),
                "last_updated_at": (
                    existing.get("_last_updated_at") if existing else None
                ),
            }
        },
    )


__all__ = ["router"]
