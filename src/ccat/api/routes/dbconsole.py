"""Admin SQL console routes (admin only).

The console runs exactly one SQL statement per request against the live
SQLite database — the same file the API and the worker use, so a write
is visible everywhere at once.

A statement that changes the database needs ``write=true``.  Without it
the server refuses anything that is not a read before running it, and
runs the statement inside a savepoint that is rolled back afterwards,
so a read can never change the database file.

Every statement is recorded in the security audit log with its author.
Schema changes belong in an Alembic revision: the console can apply
them, but they stay out of the migration history.

Known limit: SQLite has no statement timeout, so a pathological query
holds the API worker until it finishes.
"""

import time
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.exc import SQLAlchemyError

from ccat.api.auth import AuthUser, record_event, require_csrf, require_roles
from ccat.api.dto import DbQueryResult, DbQueryRequest, DbTableView
from ccat.api.errors import ApiError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from ccat.db.uow import UnitOfWork

router = APIRouter(
    prefix="/admin/db",
    tags=["database"],
    dependencies=[Depends(require_roles("admin"))],
)

#: Maximum rows one statement returns to the client.
ROW_LIMIT = 500

#: Longest SQL text kept in the audit log.
AUDIT_SQL_LIMIT = 2000

#: Statement kinds that may run without write mode.
READ_KEYWORDS = frozenset({"SELECT", "WITH", "EXPLAIN", "PRAGMA"})

#: Savepoint that keeps a read statement from changing the database.
READ_SAVEPOINT = "console_read"


def _quote(identifier: str) -> str:
    """Quote one SQLite identifier.

    Args:
        identifier: The table or column name.

    Returns:
        The name as a quoted SQLite identifier.
    """
    return '"' + identifier.replace('"', '""') + '"'


def _statement_keyword(sql: str) -> str:
    """Return the first keyword of one statement.

    Leading whitespace and comments are skipped, so a commented-out
    statement is classified by what it actually does.

    Args:
        sql: The statement text.

    Returns:
        The first keyword, uppercased ("" when there is none).
    """
    rest = sql.lstrip()
    while rest.startswith("--") or rest.startswith("/*"):
        if rest.startswith("--"):
            _, _, rest = rest.partition("\n")
        else:
            _, _, rest = rest.partition("*/")
        rest = rest.lstrip()
    return rest.split(maxsplit=1)[0].upper() if rest else ""


def _jsonable(value: Any) -> Any:
    """Convert one SQLite value into a JSON-safe value.

    Args:
        value: The value the driver returned.

    Returns:
        The value as JSON can carry it.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value).hex()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


def _driver_message(error: SQLAlchemyError) -> str:
    """Render one SQL error as a safe message.

    Args:
        error: The SQLAlchemy error.

    Returns:
        The driver's own message.
    """
    original = getattr(error, "orig", None)
    return str(original) if original is not None else str(error)


async def _scalar(conn: "AsyncConnection", sql: str) -> Any:
    """Run one statement and return its first value.

    Args:
        conn: The connection inside the request transaction.
        sql: The statement to run.

    Returns:
        The first column of the first row, or None.
    """
    result = await conn.exec_driver_sql(sql)
    return result.scalar()


async def _total_changes(conn: "AsyncConnection") -> int:
    """Return the connection's cumulative row-change count.

    Args:
        conn: The connection inside the request transaction.

    Returns:
        The number of rows the connection has changed so far.
    """
    return int(await _scalar(conn, "SELECT total_changes()") or 0)


async def _schema_version(conn: "AsyncConnection") -> int:
    """Return SQLite's schema cookie.

    Args:
        conn: The connection inside the request transaction.

    Returns:
        The cookie, which changes on every schema change.
    """
    return int(await _scalar(conn, "PRAGMA schema_version") or 0)


async def _rollback_to(conn: "AsyncConnection", name: str) -> None:
    """Undo everything done since one savepoint.

    Args:
        conn: The connection inside the request transaction.
        name: The savepoint name.
    """
    try:
        await conn.exec_driver_sql(f"ROLLBACK TO {name}")
        await conn.exec_driver_sql(f"RELEASE {name}")
    except SQLAlchemyError:
        # The statement's own error is the one worth reporting.
        pass


@router.get("/tables", response_model=list[DbTableView])
async def list_tables(
    request: Request,
    _admin: AuthUser = Depends(require_roles("admin")),
) -> list[DbTableView]:
    """List every table with its columns and row count (admin only).

    Args:
        request: The current request.
        _admin: Admin guard.

    Returns:
        One entry per table, ordered by name.
    """
    uow: UnitOfWork = request.app.state.uow
    async with uow.session() as session:
        conn = await session.connection()
        result = await conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        tables: list[DbTableView] = []
        for row in result.mappings().all():
            name = str(row["name"])
            quoted = _quote(name)
            count = await _scalar(conn, f"SELECT COUNT(*) FROM {quoted}")
            info = await conn.exec_driver_sql(f"PRAGMA table_info({quoted})")
            columns = [str(column["name"]) for column in info.mappings().all()]
            tables.append(
                DbTableView(
                    name=name,
                    columns=columns,
                    row_count=int(count or 0),
                )
            )
        return tables


@router.post("/query", response_model=DbQueryResult)
async def run_query(
    body: DbQueryRequest,
    request: Request,
    _admin: AuthUser = Depends(require_roles("admin")),
    _csrf: None = Depends(require_csrf),
) -> DbQueryResult:
    """Run one SQL statement against the live database (admin only).

    Args:
        body: The statement and the write flag.
        request: The current request.
        _admin: Admin guard.
        _csrf: CSRF guard.

    Returns:
        The statement's rows (read) or its affected-row count (write).

    Raises:
        ApiError: 422 empty statement; 400 invalid SQL; 403 a
            modifying statement without ``write=true``.
    """
    sql = body.sql.strip()
    if not sql:
        raise ApiError(422, "invalid_input", "the statement is empty")
    if not body.write and _statement_keyword(sql) not in READ_KEYWORDS:
        raise ApiError(
            403,
            "read_only",
            "only read statements run without writes enabled",
        )

    uow: UnitOfWork = request.app.state.uow
    started = time.perf_counter()
    async with uow.session() as session:
        conn = await session.connection()
        changes_before = await _total_changes(conn)
        schema_before = await _schema_version(conn)
        if not body.write:
            # The savepoint is rolled back whatever the statement does,
            # so a read can never change the database file.
            await conn.exec_driver_sql(f"SAVEPOINT {READ_SAVEPOINT}")
        try:
            result = await conn.exec_driver_sql(sql)
            columns = list(result.keys()) if result.returns_rows else []
            rows: list[dict[str, Any]] = []
            truncated = False
            if result.returns_rows:
                fetched = result.fetchmany(ROW_LIMIT + 1)
                truncated = len(fetched) > ROW_LIMIT
                for row in fetched[:ROW_LIMIT]:
                    rows.append(
                        {
                            column: _jsonable(value)
                            for column, value in zip(columns, row, strict=True)
                        }
                    )
            changes_after = await _total_changes(conn)
            schema_after = await _schema_version(conn)
        except SQLAlchemyError as error:
            raise ApiError(400, "sql_error", _driver_message(error)) from error
        finally:
            if not body.write:
                await _rollback_to(conn, READ_SAVEPOINT)
        affected = max(0, changes_after - changes_before)
        if not body.write and (affected or schema_after != schema_before):
            raise ApiError(
                403,
                "read_only",
                "the statement changed the database; run it with writes"
                " enabled",
            )

    duration_ms = (time.perf_counter() - started) * 1000
    await record_event(
        uow.session_factory,
        "db_exec",
        user_id=_admin.id,
        object_type="sql",
        object_id=_statement_keyword(sql)[:20],
        details={
            "write": body.write,
            "rows": len(rows),
            "affected": affected,
            "sql": sql[:AUDIT_SQL_LIMIT],
        },
    )
    return DbQueryResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        affected_rows=affected,
        duration_ms=round(duration_ms, 3),
    )


__all__ = ["router"]
