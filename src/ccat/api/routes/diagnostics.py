"""Diagnostic routes (refactor §5.2).

Duplicate/DOB snapshots and the match report are serialised as stored;
email/address rows hide the full provider ``response_json`` unless
``include_payload=true`` AND the caller holds auditor/admin (plan §5.2
provider-payload restriction).
"""

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from ccat.api.auth import AuthUser, current_user
from ccat.api.deps import season_repo
from ccat.api.errors import ApiError
from ccat.db.repositories import SeasonStoreRepository

router = APIRouter(prefix="/seasons/{season}/diagnostics", tags=["diagnostics"])


def _strip_payload(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove ``response_json`` from provider rows.

    Args:
        rows: The log rows.

    Returns:
        Rows without the payload key.
    """
    return [
        {key: value for key, value in row.items() if key != "response_json"}
        for row in rows
    ]


@router.get("/duplicates")
async def duplicates(
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    _user: AuthUser = Depends(current_user),
) -> list[dict[str, Any]]:
    """Read the current duplicate-log snapshot.

    Args:
        request: The current request.
        repo: The season-bound repository.
        _user: Authenticated identity.

    Returns:
        The duplicate-log rows.
    """
    return await repo.duplicate_log_rows()


@router.get("/dob")
async def dob(
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    _user: AuthUser = Depends(current_user),
) -> list[dict[str, Any]]:
    """Read the current sibling-DOB-conflict snapshot.

    Args:
        request: The current request.
        repo: The season-bound repository.
        _user: Authenticated identity.

    Returns:
        The dob_conflict_log rows.
    """
    return await repo.dob_log_rows()


@router.get("/emails")
async def emails(
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    include_payload: bool = Query(False),
    user: AuthUser = Depends(current_user),
) -> list[dict[str, Any]]:
    """Email-validation log; payloads role-gated (§5.2).

    Args:
        request: The current request.
        repo: The season-bound repository.
        include_payload: Include the full ZeroBounce response JSON.
        user: Authenticated identity (payload gate).

    Returns:
        The email-validation rows.
    """
    rows = await repo.email_log_rows()
    if include_payload:
        if not set(user.roles) & {"auditor", "admin"}:
            raise ApiError(
                403, "forbidden", "payload access requires auditor or admin"
            )
        return rows
    return _strip_payload(rows)


@router.get("/addresses")
async def addresses(
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    include_payload: bool = Query(False),
    user: AuthUser = Depends(current_user),
) -> list[dict[str, Any]]:
    """Address-validation log; payloads role-gated (§5.2).

    Args:
        request: The current request.
        repo: The season-bound repository.
        include_payload: Include the full Nominatim response JSON.
        user: Authenticated identity (payload gate).

    Returns:
        The address-validation rows.
    """
    rows = await repo.address_log_rows()
    if include_payload:
        if not set(user.roles) & {"auditor", "admin"}:
            raise ApiError(
                403, "forbidden", "payload access requires auditor or admin"
            )
        return rows
    return _strip_payload(rows)


@router.get("/match-report")
async def match_report(
    request: Request,
    repo: SeasonStoreRepository = Depends(season_repo),
    _user: AuthUser = Depends(current_user),
) -> list[dict[str, Any]]:
    """Read the latest match-report rows; 404 when no run exists.

    A legitimately absent report is never fabricated as empty (§10).

    Args:
        request: The current request.
        repo: The season-bound repository.
        _user: Authenticated identity.

    Returns:
        The report rows.

    Raises:
        ApiError: 404 when no match run exists.
    """
    rows = await repo.match_report_rows()
    if rows is None:
        raise ApiError(404, "not_found", "no match run exists for this season")
    return rows


__all__ = ["router"]
