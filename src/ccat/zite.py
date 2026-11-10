"""Download Zite databases as polars DataFrames.

Uses the Zite Database REST API:
https://developers.zite.com/api/overview

Authentication comes from the `ZITE_API_KEY` environment variable, sent as a
Bearer token against the base URL `https://tables.zite.com/api/v1`.
"""

import os
import time
from typing import Any

import polars as pl
import requests

BASE_URL = "https://tables.zite.com/api/v1"
API_KEY_ENV_VAR = "ZITE_API_KEY"

_PAGE_LIMIT = 2000  # API max per page
_MAX_RETRIES = 5


class ZiteError(Exception):
    """Raised when the Zite API returns an error response."""


def _get_api_key() -> str:
    """Read the API key from the environment.

    Returns:
        The value of the `ZITE_API_KEY` variable.

    Raises:
        ZiteError: If `ZITE_API_KEY` is not set.
    """
    api_key = os.environ.get(API_KEY_ENV_VAR)
    if not api_key:
        raise ZiteError(
            f"Environment variable {API_KEY_ENV_VAR!r} is not set. "
            "Generate an API key at https://app.zite.com/home/settings/developer"
        )
    return api_key


def _request(
    method: str,
    path: str,
    api_key: str,
    json_body: dict[str, Any] | None = None,
) -> Any:
    """Perform one API request with bearer auth and 429 retry.

    Args:
        method: HTTP method ("GET" or "POST").
        path: API path beginning with "/", e.g. "/bases".
        api_key: The Zite API key for the Authorization header.
        json_body: Optional JSON request body.

    Returns:
        The decoded JSON response, or the raw response object when the body
        is not JSON.

    Raises:
        ZiteError: If the server returns an error response or a non-JSON
            error body.
    """
    url = BASE_URL + path
    headers = {"Authorization": f"Bearer {api_key}"}
    for _attempt in range(_MAX_RETRIES):
        response = requests.request(
            method, url, headers=headers, json=json_body, timeout=120
        )
        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", 1))
            time.sleep(max(retry_after, 1))
            continue
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as error:
            try:
                body = response.json()
            except ValueError as parse_error:
                raise ZiteError(
                    f"Zite API returned HTTP {response.status_code} for {path}"
                ) from parse_error
            detail = body.get("error", body) if isinstance(body, dict) else body
            raise ZiteError(f"Zite API error for {path}: {detail}") from error
        try:
            return response.json()
        except ValueError:
            return response
    raise ZiteError(
        f"Zite API rate limit for {path} did not clear after {_MAX_RETRIES} retries"
    )


def _list_database(api_key: str) -> list[dict[str, Any]]:
    """List all databases accessible to the API key.

    Args:
        api_key: The Zite API key for the Authorization header.

    Returns:
        The list of database summaries from `GET /bases`.
    """
    return _request("GET", "/bases", api_key)


def _get_database_definition(api_key: str, database_id: str) -> dict[str, Any]:
    """Fetch one database with its complete table definitions.

    Args:
        api_key: The Zite API key for the Authorization header.
        database_id: The unique database identifier.

    Returns:
        The database object, including tables, fields, and views.

    Raises:
        ZiteError: If the database does not exist or is not accessible.
    """
    return _request("GET", f"/bases/{database_id}", api_key)


def _fetch_table_records(
    api_key: str, database_id: str, table_id: str
) -> list[dict[str, Any]]:
    """Download every record of a table, following pagination.

    Uses `POST /bases/{id}/tables/{id}/records/list` with 2000-record pages
    ordered deterministically, and honors `Retry-After` on 429 responses.

    Args:
        api_key: The Zite API key for the Authorization header.
        database_id: The unique database identifier.
        table_id: The unique table identifier.

    Returns:
        The full list of record objects for the table.

    Raises:
        ZiteError: If any page request fails.
    """
    records: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = _request(
            "POST",
            f"/bases/{database_id}/tables/{table_id}/records/list",
            api_key,
            {
                "limit": _PAGE_LIMIT,
                "offset": offset,
                "sort": [{"field": "createdAt", "direction": "asc"}],
            },
        )
        records.extend(page.get("records", []))
        if not page.get("hasMore", False):
            return records
        offset += _PAGE_LIMIT


def _records_to_frame(records: list[dict[str, Any]]) -> pl.DataFrame:
    """Convert record objects to a polars DataFrame.

    Columns come from each record's `fields` mapping (field names as keys),
    prefixed with the system columns `id`, `createdAt`, and `updatedAt`.
    Field values share names across tables, so no additional renaming is
    needed here.

    Args:
        records: Record objects from the records list endpoint.

    Returns:
        One row per record; empty DataFrame when no records exist.
    """
    rows: list[dict[str, Any]] = []
    for record in records:
        row: dict[str, Any] = {
            "id": record.get("id"),
            "createdAt": record.get("createdAt"),
            "updatedAt": record.get("updatedAt"),
        }
        row.update(record.get("fields") or {})
        rows.append(row)
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows)


def get_table_data(database: str) -> pl.DataFrame:
    """Download one Zite table and return its records as a polars DataFrame.

    Args:
        database: A dashboard URL of the form
            `app.zite.com/.../database/{tableId}/{viewId}` with the database
            ID embedded in the workspace slug, written for one specific
            table; or a bare table ID / exact table name combined with a
            database reference as `DATABASE_ID::TABLE_ID` (or table NAME).
            Table names are matched exactly within the database.

    Returns:
        One row record DataFrame as per `_records_to_frame`.

    Raises:
        ZiteError: If `ZITE_API_KEY` is unset, the database or table cannot
            be resolved, or any API request fails.
    """
    api_key = _get_api_key()
    database_id, table_ref = (
        database.split("::", 1) if "::" in database else (database, "")
    )
    if "://" in database_id:
        if "/workspace/" not in database_id or "/database/" not in database_id:
            raise ZiteError(
                "Expected a table URL of the form "
                "https://app.zite.com/workspace/{slug}/database/{tableId}/{viewId}"
            )
        # Workspace URL: /workspace/{slug-with-dbId}/database/{tableId}/{viewId}.
        slug = database_id.split("/workspace/", 1)[1].split("/", 1)[0]
        table_id = (
            database_id.split("/database/", 1)[1].split("/", 1)[0].split("?")[0]
        )
        database_id = slug.rsplit("-", 1)[-1]
    else:
        table_id = table_ref
    definition = _get_database_definition(api_key, database_id)
    table = next(
        (
            t
            for t in definition.get("tables", [])
            if t["id"] == table_id or t["name"] == table_ref
        ),
        None,
    )
    if table is None:
        raise ZiteError(f"No table {table_id!r} in database {database_id!r}")
    records = _fetch_table_records(api_key, database_id, table["id"])
    return _records_to_frame(records)


def cli() -> None:
    """Run the Zite download function as a CLI via python-fire.

    Usage:
        zite get_table_data TABLE_URL
    """
    import fire

    fire.Fire({"get_table_data": get_table_data})


if __name__ == "__main__":
    cli()
