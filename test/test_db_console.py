"""Admin database console API tests (``/api/v1/admin/db``).

File-backed SQLite per test; ``httpx`` AsyncClient over ASGITransport via
the :class:`Client` helper in :mod:`test_api`.  The console is the only
door to the raw tables, so the tests cover the admin gate, the
read-only guard, the write path, and the audit trail.
"""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from conftest import load_test_cfg
from test_api import Client

from ccat.api import create_app
from ccat.config import StorageSpec
from ccat.db.engine import create_sync_engine, run_migrations
from ccat.db.seed import seed_bootstrap

pytestmark = pytest.mark.asyncio

#: The tables every migrated database has.
TABLES = ("users", "sessions", "final_child", "final_family")


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[Any]:
    """One migrated, seeded app instance (dev auth mode).

    Args:
        tmp_path: Per-test temporary directory.

    Yields:
        The FastAPI app.
    """
    cfg = load_test_cfg(
        excel_file=str(tmp_path / "wb.xlsx"),
        log_file=str(tmp_path / "ccat.log"),
        match_report_csv=str(tmp_path / "report.csv"),
        storage=StorageSpec(
            mode="db",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'ccat.sqlite3'}",
            artifact_root=str(tmp_path / "artifacts"),
        ),
    )
    run_migrations(cfg.storage.database_url)
    seed_bootstrap(
        create_sync_engine(
            cfg.storage.database_url.replace("sqlite+aiosqlite", "sqlite")
        )
    )
    application = create_app(cfg)
    try:
        yield application
    finally:
        # End the aiosqlite worker thread with the test's event loop.
        await application.state.uow.engine.dispose()


async def _query(client: Client, sql: str, write: bool = False) -> Any:
    """Run one statement through the console.

    Args:
        client: The logged-in client.
        sql: The statement.
        write: Whether writes are enabled.

    Returns:
        The response.
    """
    return await client.client.post(
        "/api/v1/admin/db/query",
        json={"sql": sql, "write": write},
        headers=client.headers(),
    )


async def _display_name(client: Client, username: str) -> Any:
    """Read one user's display name through the console.

    Args:
        client: The logged-in client.
        username: The username to read.

    Returns:
        The display name, or None when it is unset.
    """
    response = await _query(
        client, f"SELECT display_name FROM users WHERE username = '{username}'"
    )
    return response.json()["rows"][0]["display_name"]


async def test_lists_tables_with_columns_for_admin(app: Any) -> None:
    """The table list names every table with its columns and row count.

    Args:
        app: The app fixture.
    """
    client = Client(app)
    try:
        await client.login("cmendres")
        response = await client.client.get("/api/v1/admin/db/tables")
    finally:
        await client.close()
    assert response.status_code == 200, response.text
    tables = {table["name"]: table for table in response.json()}
    for name in TABLES:
        assert name in tables
    assert "username" in tables["users"]["columns"]
    assert tables["users"]["row_count"] >= 2


async def test_refuses_a_non_admin(app: Any) -> None:
    """A coordinator without the admin role gets no console.

    Args:
        app: The app fixture.
    """
    client = Client(app)
    try:
        await client.login("lmboswell")
        listing = await client.client.get("/api/v1/admin/db/tables")
        query = await _query(client, "SELECT 1")
    finally:
        await client.close()
    assert listing.status_code == 403, listing.text
    assert query.status_code == 403, query.text


async def test_read_statement_returns_rows(app: Any) -> None:
    """A read statement returns its columns and rows.

    Args:
        app: The app fixture.
    """
    client = Client(app)
    try:
        await client.login("cmendres")
        response = await _query(
            client, "SELECT username FROM users ORDER BY username"
        )
    finally:
        await client.close()
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["columns"] == ["username"]
    assert "cmendres" in [row["username"] for row in body["rows"]]
    assert body["row_count"] == len(body["rows"])


async def test_write_without_the_flag_is_refused_and_rolled_back(
    app: Any,
) -> None:
    """A modifying statement without ``write`` changes nothing.

    Args:
        app: The app fixture.
    """
    client = Client(app)
    try:
        await client.login("cmendres")
        response = await _query(
            client,
            "UPDATE users SET display_name = 'nope'"
            " WHERE username = 'lmboswell'",
        )
        assert response.status_code == 403, response.text
        assert response.json()["detail"]["code"] == "read_only"
        assert await _display_name(client, "lmboswell") is None
    finally:
        await client.close()


async def test_a_hidden_write_is_rolled_back(app: Any) -> None:
    """A write behind a read-shaped statement is rolled back too.

    Args:
        app: The app fixture.
    """
    client = Client(app)
    try:
        await client.login("cmendres")
        response = await _query(
            client,
            "WITH victim AS (SELECT id FROM users"
            " WHERE username = 'lmboswell')"
            " UPDATE users SET display_name = 'sneaky'"
            " WHERE id IN (SELECT id FROM victim)",
        )
        assert response.status_code == 403, response.text
        assert response.json()["detail"]["code"] == "read_only"
        assert await _display_name(client, "lmboswell") is None
    finally:
        await client.close()


async def test_write_with_the_flag_applies(app: Any) -> None:
    """A modifying statement with ``write`` reaches the database.

    Args:
        app: The app fixture.
    """
    client = Client(app)
    try:
        await client.login("cmendres")
        response = await _query(
            client,
            "UPDATE users SET display_name = 'Renamed'"
            " WHERE username = 'lmboswell'",
            write=True,
        )
        assert response.status_code == 200, response.text
        assert response.json()["affected_rows"] == 1
        assert await _display_name(client, "lmboswell") == "Renamed"
    finally:
        await client.close()


async def test_refuses_several_statements(app: Any) -> None:
    """One request runs one statement only.

    Args:
        app: The app fixture.
    """
    client = Client(app)
    try:
        await client.login("cmendres")
        response = await _query(client, "SELECT 1; SELECT 2", write=True)
    finally:
        await client.close()
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "sql_error"


async def test_refuses_broken_sql(app: Any) -> None:
    """Invalid SQL is a 400, not a 500.

    Args:
        app: The app fixture.
    """
    client = Client(app)
    try:
        await client.login("cmendres")
        response = await _query(client, "SELECT nope FROM nothing")
    finally:
        await client.close()
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "sql_error"


async def test_requires_the_csrf_header(app: Any) -> None:
    """A session-carrying write needs the CSRF header.

    Args:
        app: The app fixture.
    """
    client = Client(app)
    try:
        await client.login("cmendres")
        response = await client.client.post(
            "/api/v1/admin/db/query",
            json={"sql": "SELECT 1", "write": False},
        )
    finally:
        await client.close()
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "csrf"


async def test_records_the_statement_in_the_audit_log(app: Any) -> None:
    """Every executed statement is attributable.

    Args:
        app: The app fixture.
    """
    client = Client(app)
    try:
        await client.login("cmendres")
        response = await _query(client, "SELECT count(*) FROM users")
        assert response.status_code == 200, response.text
        events = await client.client.get("/api/v1/admin/db/tables")
    finally:
        await client.close()
    assert events.status_code == 200
    rows = await _audit_events(app)
    assert any(
        row["action"] == "db_exec" and "count(*)" in (row["details"] or "")
        for row in rows
    )


async def _audit_events(app: Any) -> list[dict[str, Any]]:
    """Read the security audit log.

    Args:
        app: The app fixture.

    Returns:
        The audit rows, newest last.
    """
    from sqlalchemy import select

    from ccat.db.models import SecurityAuditEvent
    from ccat.db.uow import UnitOfWork

    async with UnitOfWork(app.state.uow.engine).session() as session:
        rows = (
            await session.scalars(
                select(SecurityAuditEvent).order_by(SecurityAuditEvent.id)
            )
        ).all()
        return [
            {
                "action": row.action,
                "details": json.dumps(row.details or {}),
            }
            for row in rows
        ]
