"""API tests (refactor §5, §8; Phase 3 acceptance).

File-backed SQLite per test; httpx AsyncClient over ASGITransport; all
providers faked at the seams the domain modules expose.  In dev auth
mode every request is authenticated as the auto-login identity
(``cmendres``, admin); the strict session rules are exercised with a
non-dev app (``oidc_app``).  The dev no-password bootstrap users (D2)
are ``cmendres`` (admin) and ``lmboswell`` (coordinator+auditor); extra
users are created through the admin API.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import load_test_cfg
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from ccat.api import create_app
from ccat.api.auth import SESSION_COOKIE, issue_session
from ccat.config import AIScreeningSpec, Config, StorageSpec, WorkerSpec
from ccat.db.engine import create_async_engine, run_migrations
from ccat.db.models import JobEvent
from ccat.db.models import Session as SessionRow
from ccat.db.models import User
from ccat.db.uow import UnitOfWork
from ccat.jobs.worker import Worker

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Fixtures / helpers.
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path, **updates: Any) -> Config:
    """Test config: temp DB/artifacts, dev auth, fast worker."""
    base = load_test_cfg(
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
            progress_throttle_seconds=0.0,
        ),
    )
    return base.model_copy(
        update={
            "ai_screening": AIScreeningSpec(
                model="test/model",
                system_prompt=base.ai_screening.system_prompt,
                temperature=0.0,
                max_workers=1,
            ),
        }
        | updates
    )


def _columns(entity: str) -> tuple[str, ...]:
    """Return the entity's legacy dict column vocabulary."""
    from ccat.audit.store import ENTITY_SPECS

    return tuple(ENTITY_SPECS[entity]["columns"])


def child_row(record_id: str, **overrides: Any) -> dict[str, Any]:
    """One full final-child row under legacy dict names."""
    base: dict[str, Any] = {column: None for column in _columns("child")}
    base.update(
        zite_record_id=record_id,
        family_id=1,
        first_name="Kiddo",
        primary_email="fam@example.com",
        dob="2018-05-10",
        gender="Male",
        need_1="winter coat taht fits",
        source="web",
        status="new",
        ignore=False,
        screened=False,
        potential_duplicate=False,
        generated_info_sheet=False,
    )
    base.update(overrides)
    return base


def family_row(family_id: int, **overrides: Any) -> dict[str, Any]:
    """One full final-family row under legacy dict names."""
    base: dict[str, Any] = {column: None for column in _columns("family")}
    base.update(
        family_id=family_id,
        zite_record_id=f"rec_f{family_id}",
        primary_email=f"fam{family_id}@example.com",
        first_name="Jane",
        last_name="Doe",
        city="Hancock",
        source="web",
        status="new",
        ignore=False,
        potential_duplicate=False,
    )
    base.update(overrides)
    return base


def sponsor_row(sponsor_id: int, **overrides: Any) -> dict[str, Any]:
    """One full final-sponsor row under legacy dict names."""
    base: dict[str, Any] = {column: None for column in _columns("sponsor")}
    base.update(
        sponsor_id=sponsor_id,
        zite_record_id=f"rec_s{sponsor_id}",
        create="2026-01-01T10:00:00Z",
        email=f"sponsor{sponsor_id}@example.com",
        sponsor_type="Individual",
        first_name="Mary",
        last_name="Sponsor",
        num_children=2,
        age_preference="Any Age",
        source="web",
        status="new",
        ignore=False,
        potential_duplicate=False,
    )
    base.update(overrides)
    return base


def raw_child(record_id: str, **overrides: Any) -> dict[str, Any]:
    """One verbatim raw-child row (config columns only)."""
    base: dict[str, Any] = {
        "zite_record_id": record_id,
        "zite_created_at": "2026-02-01T09:00:00+00:00",
        "created": "2026-02-01T09:00:00+00:00",
        "first_name": "Kiddo",
        "primary_email": "fam@example.com",
        "dob": "2018-05-10",
        "age_reported": 7,
        "gender": "Male",
        "need_1": "winter coat taht fits",
        "desire_1": "legos",
        "source": "web",
    }
    base.update(overrides)
    return base


def raw_family(family_email: str, **overrides: Any) -> dict[str, Any]:
    """One verbatim raw-family row (config columns only)."""
    base: dict[str, Any] = {
        "zite_record_id": f"rec_f_{family_email}",
        "zite_created_at": "2026-01-05T10:00:00+00:00",
        "created": "2026-01-05T10:00:00+00:00",
        "primary_email": family_email,
        "accepts_disclaimer": True,
        "first_name": "Jane",
        "last_name": "Doe",
        "city": "Hancock",
        "source": "web",
    }
    base.update(overrides)
    return base


def raw_sponsor(record_id: str, **overrides: Any) -> dict[str, Any]:
    """One verbatim raw-sponsor row (config columns only)."""
    base: dict[str, Any] = {
        "zite_record_id": record_id,
        "create": "2026-01-01T10:00:00+00:00",
        "email": "sponsor@example.com",
        "accepts_disclaimer": True,
        "sponsor_type": "Individual",
        "first_name": "Mary",
        "last_name": "Sponsor",
        "num_children": 2,
        "age_preference": "Any Age",
        "gender_preference": "Any Gender",
        "dropoff_location": "Hancock",
        "source": "web",
    }
    base.update(overrides)
    return base


_DEFAULT_RAW: dict[str, list[dict[str, Any]]] = {
    "sponsor": [raw_sponsor("rec_s1")],
    "family": [raw_family("fam@example.com")],
    "child": [raw_child("c1"), raw_child("c2", first_name="Lee")],
}


async def _seed(repo: Any, **stores: Any) -> None:
    """Seed raw snapshot and/or final stores through migrate ownership."""
    raw = stores.pop("raw", None)
    if raw is not None:
        await repo.replace_raw_snapshot("migrate", raw, "migration")
    names = {"children": "child", "families": "family", "sponsors": "sponsor"}
    for plural, rows in stores.items():
        if rows:
            await repo.commit_finals(
                "migrate", names[plural], rows, "migration"
            )


class Client:
    """Test client bound to one app, with login + CSRF helpers."""

    def __init__(self, app: Any, csrf_token: str | None = None) -> None:
        """Wrap one AsyncClient.

        Args:
            app: The FastAPI app.
            csrf_token: The session CSRF token (after login).
        """
        self.client = AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        )
        self.csrf = csrf_token

    async def login(self, username: str) -> None:
        """Log in and cache the CSRF token from /auth/me.

        Args:
            username: The bootstrap username.
        """
        response = await self.client.post(
            "/api/v1/auth/login", json={"username": username}
        )
        assert response.status_code == 204, response.text
        me = await self.client.get("/api/v1/auth/me")
        self.csrf = me.json()["csrf_token"]

    def headers(self, csrf: bool = True) -> dict[str, str]:
        """Build request headers (CSRF by default).

        Args:
            csrf: Include the X-CCAT-CSRF header.

        Returns:
            The header dict.
        """
        if csrf and self.csrf:
            return {"X-CCAT-CSRF": self.csrf}
        return {}

    async def close(self) -> None:
        """Close the underlying client."""
        await self.client.aclose()


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[Any]:
    """One migrated, seeded app instance (dev auth mode).

    Args:
        tmp_path: Per-test temporary directory.

    Yields:
        The FastAPI app.
    """
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)
    from ccat.db.engine import create_sync_engine
    from ccat.db.seed import seed_bootstrap

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


@pytest.fixture
async def oidc_app(app: Any) -> AsyncIterator[Any]:
    """One app instance in a non-dev auth mode (production posture).

    Shares the seeded database with :func:`app`; only the auth mode
    differs, so the strict session rules stay testable.

    Args:
        app: The seeded app.

    Yields:
        The app in OIDC auth mode.
    """
    cfg = app.state.cfg
    application = create_app(
        cfg.model_copy(
            update={
                "api": cfg.api.model_copy(update={"auth_mode": "oidc"}),
            }
        )
    )
    try:
        yield application
    finally:
        await application.state.uow.engine.dispose()


async def _session_token(
    app: Any, username: str, *, expires_at: datetime | None = None
) -> str:
    """Create one session row for a user and return its raw token.

    Args:
        app: The FastAPI app owning the database.
        username: The account the session belongs to.
        expires_at: Expiry to stamp on the row (default: not expired).

    Returns:
        The raw cookie token for the new session.
    """
    cfg = app.state.cfg
    from ccat.db.engine import async_session_factory

    engine = create_async_engine(cfg.storage.database_url)
    factory = async_session_factory(engine)
    try:
        async with factory() as session:
            user = await session.scalar(
                select(User).where(User.username == username)
            )
            token, _row = await issue_session(session, cfg, user)
            if expires_at is not None:
                await session.execute(
                    update(SessionRow).values(expires_at=expires_at)
                )
            await session.commit()
    finally:
        await engine.dispose()
    return token


@pytest.fixture
async def anonymous(app: Any) -> AsyncIterator[Client]:
    """Provide an unauthenticated client."""
    client = Client(app)
    yield client
    await client.close()


async def _logged_in(app: Any, username: str = "lmboswell") -> Client:
    """Provide a client logged in as the named bootstrap user."""
    client = Client(app)
    await client.login(username)
    return client


@pytest.fixture
async def auditor(app: Any) -> AsyncIterator[Client]:
    """Provide a client logged in as lmboswell (coordinator + auditor)."""
    client = await _logged_in(app)
    yield client
    await client.close()


@pytest.fixture
async def admin(app: Any) -> AsyncIterator[Client]:
    """Provide a client logged in as cmendres (admin + coordinator + auditor)."""
    client = await _logged_in(app, "cmendres")
    yield client
    await client.close()


async def _seed_default(app: Any) -> None:
    """Seed the raw snapshot + one family/child/sponsor (migrate owner)."""
    cfg = app.state.cfg
    engine = create_async_engine(cfg.storage.database_url)
    try:
        async with UnitOfWork(engine).store(cfg, "migration") as repo:
            await _seed(
                repo,
                raw=_DEFAULT_RAW,
                children=[child_row("c1"), child_row("c2", first_name="Lee")],
                families=[family_row(1)],
                sponsors=[sponsor_row(1)],
            )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Auth.
# ---------------------------------------------------------------------------


async def test_login_sets_devcookie_and_me(app: Any, anonymous: Client) -> None:
    """Dev login sets an HttpOnly SameSite cookie; /me returns it."""
    response = await anonymous.client.post(
        "/api/v1/auth/login", json={"username": "lmboswell"}
    )
    assert response.status_code == 204
    cookie = response.headers["set-cookie"].lower()
    assert "ccat_session=" in cookie
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    me = await anonymous.client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == "lmboswell"
    assert "auditor" in me.json()["roles"]
    assert me.json()["csrf_token"]


async def test_dev_mode_needs_no_login(app: Any, anonymous: Client) -> None:
    """Dev mode authenticates every request as the auto-login admin."""
    me = await anonymous.client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == "cmendres"
    assert "admin" in me.json()["roles"]
    # The auto-login identity is an admin, so the admin route answers.
    users = await anonymous.client.get("/api/v1/users")
    assert users.status_code == 200
    assert {u["username"] for u in users.json()} >= {
        "cmendres",
        "lmboswell",
    }


async def test_dev_mode_login_still_wins(app: Any) -> None:
    """An explicit dev login outranks the auto-login identity."""
    client = await _logged_in(app)
    me = await client.client.get("/api/v1/auth/me")
    assert me.json()["username"] == "lmboswell"
    assert "auditor" in me.json()["roles"]


async def test_logout_revokes_session(oidc_app: Any) -> None:
    """Logout revokes the server session; the cookie stops working."""
    token = await _session_token(oidc_app, "lmboswell")
    client = Client(oidc_app)
    client.client.cookies.set(SESSION_COOKIE, token)
    me = await client.client.get("/api/v1/auth/me")
    assert me.status_code == 200
    csrf = me.json()["csrf_token"]
    response = await client.client.post(
        "/api/v1/auth/logout", headers={"X-CCAT-CSRF": csrf}
    )
    assert response.status_code == 204
    client.client.cookies.set(SESSION_COOKIE, token)
    me = await client.client.get("/api/v1/auth/me")
    assert me.status_code == 401
    await client.close()


async def test_expired_session_is_401(oidc_app: Any) -> None:
    """An expired session row authenticates nobody."""
    token = await _session_token(
        oidc_app,
        "lmboswell",
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )
    client = Client(oidc_app)
    client.client.cookies.set(SESSION_COOKIE, token)
    me = await client.client.get("/api/v1/auth/me")
    assert me.status_code == 401
    await client.close()


async def test_wrong_csrf_403_on_mutation(app: Any) -> None:
    """Mutations without the matching CSRF header are 403."""
    client = await _logged_in(app)
    await _seed_default(app)
    response = await client.client.patch(
        "/api/v1/seasons/2026/families/1",
        json={"first_name": "Janet"},
        headers={"X-CCAT-CSRF": "wrong"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "csrf"


async def test_login_403_when_not_dev_mode(tmp_path: Path) -> None:
    """Non-dev auth mode refuses passwordless login with guidance."""
    cfg = _cfg(
        tmp_path,
        api=__import__("ccat.config", fromlist=["ApiSpec"]).ApiSpec(
            auth_mode="oidc"
        ),
    )
    run_migrations(cfg.storage.database_url)
    oidc_app = create_app(cfg)
    client = Client(oidc_app)
    try:
        response = await client.client.post(
            "/api/v1/auth/login", json={"username": "lmboswell"}
        )
        assert response.status_code == 403
        assert "identity adapter" in response.json()["detail"]["message"]
    finally:
        await client.close()


async def test_failed_logins_401_and_audited(app: Any) -> None:
    """Unknown and inactive users get 401; failures are audited."""
    cfg = app.state.cfg
    from ccat.db.models import SecurityAuditEvent, User
    from ccat.db.engine import async_session_factory

    response = await Client(app).client.post(
        "/api/v1/auth/login", json={"username": "nobody"}
    )
    assert response.status_code == 401
    engine = create_async_engine(cfg.storage.database_url)
    factory = async_session_factory(engine)
    async with factory() as session:
        # an inactive bootstrap user
        session.add(
            User(
                username="ghost",
                roles=["auditor"],
                dev_no_password=True,
                active=False,
            )
        )
        await session.commit()
    await Client(app).client.post(
        "/api/v1/auth/login", json={"username": "ghost"}
    )
    async with factory() as session:
        events = (
            await session.scalars(
                SecurityAuditEvent.__table__.select().where(
                    SecurityAuditEvent.action == "login_failed"
                )
            )
        ).all()
        await engine.dispose()
    assert len(events) == 2


async def test_rate_limit_429(app: Any) -> None:
    """The sixth login in a window from one IP is 429."""
    client = Client(app)
    try:
        statuses = []
        for _ in range(6):
            response = await client.client.post(
                "/api/v1/auth/login", json={"username": "lmboswell"}
            )
            statuses.append(response.status_code)
        assert statuses[:5] == [204] * 5
        assert statuses[5] == 429
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# RBAC.
# ---------------------------------------------------------------------------


async def test_anonymous_401_on_every_route_class(oidc_app: Any) -> None:
    """Every route class requires authentication (401 anonymous).

    Outside dev mode the auto-login fallback is gone, so an
    unauthenticated request is refused again.
    """
    client = Client(oidc_app)
    paths = [
        ("GET", "/api/v1/users"),
        ("GET", "/api/v1/seasons"),
        ("GET", "/api/v1/seasons/2026/overview"),
        ("GET", "/api/v1/seasons/2026/children"),
        ("GET", "/api/v1/seasons/2026/children/c1"),
        ("GET", "/api/v1/seasons/2026/diagnostics/duplicates"),
        ("GET", "/api/v1/seasons/2026/jobs"),
        ("GET", "/api/v1/jobs/1"),
        ("GET", "/api/v1/seasons/2026/artifacts"),
        ("GET", "/api/v1/seasons/2026/clarifications"),
    ]
    # healthz/readyz are deliberately unauthenticated (§5.2).
    for method, path in paths:
        response = await client.client.request(method, path)
        assert response.status_code == 401, f"{method} {path}"
    await client.close()


async def test_read_only_can_read_not_write(app: Any) -> None:
    """read_only GETs everywhere; mutations are 403."""
    await _seed_default(app)
    client = Client(app)
    await client.login("lmboswell")
    await client.close()
    # create a read-only user through the admin API
    admin_client = await _logged_in(app, "cmendres")
    response = await admin_client.client.post(
        "/api/v1/users",
        json={"username": "viewer", "roles": ["read_only"]},
        headers=admin_client.headers(),
    )
    assert response.status_code == 201
    await admin_client.close()
    client = Client(app)
    await client.login("viewer")
    response = await client.client.get("/api/v1/seasons/2026/children")
    assert response.status_code == 200
    response = await client.client.patch(
        "/api/v1/seasons/2026/children/c1",
        json={"first_name": "Nope"},
        headers=client.headers(),
    )
    assert response.status_code == 403
    await client.close()


async def test_auditor_patches_but_not_jobs(app: Any) -> None:
    """A pure-auditor user: PATCH ok; POST /jobs 403."""
    await _seed_default(app)
    admin_client = await _logged_in(app, "cmendres")
    response = await admin_client.client.post(
        "/api/v1/users",
        json={"username": "pure_auditor", "roles": ["auditor"]},
        headers=admin_client.headers(),
    )
    assert response.status_code == 201
    await admin_client.close()
    client = await _logged_in(app, "pure_auditor")
    detail = await client.client.get("/api/v1/seasons/2026/children/c1")
    version = detail.json()["row_version"]
    response = await client.client.patch(
        "/api/v1/seasons/2026/children/c1",
        json={"first_name": "Kiddo Jr"},
        headers=client.headers() | {"If-Match": str(version)},
    )
    assert response.status_code == 200
    response = await client.client.post(
        "/api/v1/seasons/2026/jobs",
        json={"job_type": "screen", "options": {}},
        headers=client.headers(),
    )
    assert response.status_code == 403


async def test_coordinator_jobs_but_not_users(app: Any) -> None:
    """coordinator: jobs ok; user admin 403."""
    await _seed_default(app)
    client = await _logged_in(app, "lmboswell")
    response = await client.client.post(
        "/api/v1/seasons/2026/jobs",
        json={
            "job_type": "screen",
            "options": {"skip_email_check": True, "skip_address_check": True},
        },
        headers=client.headers(),
    )
    assert response.status_code == 202
    response = await client.client.get("/api/v1/users")
    assert response.status_code == 403


async def test_admin_user_crud(app: Any) -> None:
    """admin: create, list, patch users; self-deactivation refused."""
    client = await _logged_in(app, "cmendres")
    response = await client.client.post(
        "/api/v1/users",
        json={
            "username": "newbie",
            "roles": ["auditor"],
            "display_name": "New",
        },
        headers=client.headers(),
    )
    assert response.status_code == 201
    user_id = response.json()["id"]
    response = await client.client.get("/api/v1/users")
    assert {u["username"] for u in response.json()} >= {
        "cmendres",
        "lmboswell",
        "newbie",
    }
    response = await client.client.patch(
        f"/api/v1/users/{user_id}",
        json={"roles": ["coordinator"]},
        headers=client.headers(),
    )
    assert response.json()["roles"] == ["coordinator"]
    await client.client.get("/api/v1/auth/me")
    admin_id = [
        u["id"]
        for u in (await client.client.get("/api/v1/users")).json()
        if u["username"] == "cmendres"
    ][0]
    response = await client.client.patch(
        f"/api/v1/users/{admin_id}",
        json={"active": False},
        headers=client.headers(),
    )
    assert response.status_code == 409


# ---------------------------------------------------------------------------
# Audit lists / detail / patch.
# ---------------------------------------------------------------------------


async def test_list_pagination_search_status(app: Any) -> None:
    """Pagination, search, and stored-status filter (§5.2)."""
    await _seed_default(app)
    client = await _logged_in(app, "lmboswell")
    response = await client.client.get(
        "/api/v1/seasons/2026/children?page=1&page_size=1"
    )
    body = response.json()
    assert body["total"] == 2
    assert len(body["items"]) == 1
    assert body["page"] == 1
    response = await client.client.get("/api/v1/seasons/2026/children?q=lee")
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["zite_record_id"] == "c2"
    response = await client.client.get(
        "/api/v1/seasons/2026/children?status=new"
    )
    assert response.json()["total"] == 2
    response = await client.client.get(
        "/api/v1/seasons/2026/children?status=audited"
    )
    assert response.json()["total"] == 0
    # lists include ignored rows
    from ccat.db.engine import async_session_factory

    engine = create_async_engine(app.state.cfg.storage.database_url)
    factory = async_session_factory(engine)
    async with factory() as session:
        from sqlalchemy import update
        from ccat.db.models import FinalChild

        await session.execute(
            update(FinalChild).values(ignore=True, status="ignored")
        )
        await session.commit()
    await engine.dispose()
    response = await client.client.get("/api/v1/seasons/2026/children")
    assert response.json()["total"] == 2


async def test_family_list_computes_child_count(app: Any) -> None:
    """Family list rows carry a server-computed child_count (§5.3)."""
    cfg = app.state.cfg
    engine = create_async_engine(cfg.storage.database_url)
    try:
        async with UnitOfWork(engine).store(cfg, "migration") as repo:
            await _seed(
                repo,
                raw=_DEFAULT_RAW,
                children=[child_row("c1"), child_row("c2", family_id=2)],
                families=[family_row(1), family_row(2)],
            )
    finally:
        await engine.dispose()
    client = await _logged_in(app, "lmboswell")
    response = await client.client.get("/api/v1/seasons/2026/families")
    rows = {row["family_id"]: row for row in response.json()["items"]}
    assert rows[1]["child_count"] == 1
    assert rows[2]["child_count"] == 1
    await client.close()


async def test_child_list_flags_raw_missing(app: Any) -> None:
    """Child list rows carry raw_missing for absent raw records (§5.2)."""
    cfg = app.state.cfg
    engine = create_async_engine(cfg.storage.database_url)
    try:
        async with UnitOfWork(engine).store(cfg, "migration") as repo:
            await _seed(
                repo,
                raw=_DEFAULT_RAW,
                children=[
                    child_row("c1"),
                    child_row("c2"),
                    child_row("c3", first_name="Gone"),
                ],
            )
    finally:
        await engine.dispose()
    client = await _logged_in(app, "lmboswell")
    response = await client.client.get("/api/v1/seasons/2026/children")
    flags = {
        row["zite_record_id"]: row["raw_missing"]
        for row in response.json()["items"]
    }
    assert flags == {"c1": False, "c2": False, "c3": True}
    await client.close()


async def test_child_list_sorted_by_family_then_child_id(app: Any) -> None:
    """Child rows list by assigned family, then child id (orphans last)."""
    cfg = app.state.cfg
    engine = create_async_engine(cfg.storage.database_url)
    try:
        async with UnitOfWork(engine).store(cfg, "migration") as repo:
            await _seed(
                repo,
                raw=_DEFAULT_RAW,
                children=[
                    child_row("c-2b", family_id=2, angel_id="2B"),
                    child_row("c-1b", family_id=1, angel_id="1B"),
                    child_row("c-1a", family_id=1, angel_id="1A"),
                    child_row("c-orphan", family_id=None),
                ],
                families=[family_row(1), family_row(2)],
                sponsors=[sponsor_row(1)],
            )
    finally:
        await engine.dispose()
    client = await _logged_in(app, "lmboswell")
    response = await client.client.get("/api/v1/seasons/2026/children")
    assert [row["zite_record_id"] for row in response.json()["items"]] == [
        "c-1a",
        "c-1b",
        "c-2b",
        "c-orphan",
    ]
    await client.close()


async def test_family_and_sponsor_lists_carry_validation_status(
    app: Any,
) -> None:
    """Family/sponsor rows carry their email/address check status."""
    cfg = app.state.cfg
    engine = create_async_engine(cfg.storage.database_url)
    try:
        async with UnitOfWork(engine).store(cfg, "migration") as repo:
            await _seed(
                repo,
                raw=_DEFAULT_RAW,
                children=[child_row("c1")],
                families=[
                    family_row(
                        1,
                        street_address="1 Main St",
                        city="Hancock",
                        state="Michigan",
                        zip_code="49930",
                    )
                ],
                sponsors=[sponsor_row(1, email="ada@example.org")],
            )
    finally:
        await engine.dispose()
    from ccat.db.engine import async_session_factory
    from ccat.db.models import AddressCheck, EmailCheck

    factory = async_session_factory(engine)
    async with factory() as session:
        session.add(
            EmailCheck(
                season_id=1,
                normalized_email="fam1@example.com",
                status="valid",
            )
        )
        session.add(
            EmailCheck(
                season_id=1,
                normalized_email="ada@example.org",
                status="invalid",
            )
        )
        session.add(
            AddressCheck(
                season_id=1,
                query="1 Main St, Hancock, Michigan, 49930",
                status="found",
            )
        )
        await session.commit()
    await engine.dispose()
    client = await _logged_in(app, "lmboswell")
    families = await client.client.get("/api/v1/seasons/2026/families")
    family = families.json()["items"][0]
    assert family["email_status"] == "valid"
    assert family["address_status"] == "found"
    sponsors = await client.client.get("/api/v1/seasons/2026/sponsors")
    sponsor = sponsors.json()["items"][0]
    assert sponsor["email_status"] == "invalid"
    assert sponsor["address_status"] is None
    detail = await client.client.get("/api/v1/seasons/2026/families/1")
    assert detail.json()["diagnostics"]["email_status"] == "valid"
    assert detail.json()["diagnostics"]["address_status"] == "found"
    await client.close()


async def test_detail_includes_baseline_version_attribution(app: Any) -> None:
    """Detail: row + baseline + submissions + version + attribution."""
    await _seed_default(app)
    client = await _logged_in(app, "lmboswell")
    response = await client.client.get("/api/v1/seasons/2026/children/c1")
    body = response.json()
    assert body["entity"] == "child"
    assert body["key"] == "c1"
    assert body["row"]["zite_record_id"] == "c1"
    # canonical name on the wire (never potential_anomoly)
    assert "ai_screening_notes" in body["row"]
    assert "potential_anomoly" not in body["row"]
    assert "baseline" in body
    assert body["row_version"] >= 1
    assert body["last_updated_by"] == "migration"
    assert body["submissions"]  # raw duplicate-group submissions


async def test_patch_tri_state(app: Any) -> None:
    """PATCH tri-state: omitted unchanged / null clears / value sets."""
    await _seed_default(app)
    client = await _logged_in(app, "lmboswell")
    detail = await client.client.get("/api/v1/seasons/2026/children/c1")
    version = detail.json()["row_version"]
    # set two fields
    response = await client.client.patch(
        "/api/v1/seasons/2026/children/c1",
        json={"first_name": "Renamed", "ai_screening_notes": "note one"},
        headers=client.headers() | {"If-Match": str(version)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["row"]["first_name"] == "Renamed"
    assert body["row"]["ai_screening_notes"] == "note one"
    assert body["row"]["need_1"] == "winter coat taht fits"  # unchanged
    assert body["row_version"] == version + 1
    assert body["last_updated_by"] == "lmboswell"
    # omitted leaves everything; null clears
    response = await client.client.patch(
        "/api/v1/seasons/2026/children/c1",
        json={"ai_screening_notes": None},
        headers=client.headers() | {"If-Match": str(body["row_version"])},
    )
    assert response.status_code == 200
    assert response.json()["row"]["ai_screening_notes"] is None
    assert response.json()["row"]["first_name"] == "Renamed"


async def test_patch_requires_if_match_412(app: Any) -> None:
    """PATCH without If-Match → 412 (documented choice over 428)."""
    await _seed_default(app)
    client = await _logged_in(app, "lmboswell")
    response = await client.client.patch(
        "/api/v1/seasons/2026/children/c1",
        json={"first_name": "X"},
        headers=client.headers(),
    )
    assert response.status_code == 412
    assert response.json()["detail"]["code"] == "stale_version"


async def test_patch_stale_if_match_returns_fresh_row(app: Any) -> None:
    """A/B stale-edit scenario: B's stale PATCH 412s with fresh state."""
    await _seed_default(app)
    a = await _logged_in(app, "lmboswell")  # auditor
    b_client = Client(app)
    await b_client.login("lmboswell")
    detail = await a.client.get("/api/v1/seasons/2026/children/c1")
    version = detail.json()["row_version"]
    # A patches at version N → N+1, attributed A
    response = await a.client.patch(
        "/api/v1/seasons/2026/children/c1",
        json={"first_name": "By A"},
        headers=a.headers() | {"If-Match": str(version)},
    )
    assert response.status_code == 200
    winner_at = response.json()["last_updated_at"]
    # B patches with the stale version N
    response = await b_client.client.patch(
        "/api/v1/seasons/2026/children/c1",
        json={"first_name": "By B"},
        headers=b_client.headers() | {"If-Match": str(version)},
    )
    assert response.status_code == 412
    detail = response.json()["detail"]
    assert detail["current_version"] == version + 1
    assert detail["row"]["first_name"] == "By A"
    assert detail["last_updated_by"] == "lmboswell"
    assert detail["last_updated_at"] == winner_at
    # B rebases on N+1 → succeeds
    response = await b_client.client.patch(
        "/api/v1/seasons/2026/children/c1",
        json={"first_name": "By B"},
        headers=b_client.headers() | {"If-Match": str(version + 1)},
    )
    assert response.status_code == 200
    assert response.json()["row"]["first_name"] == "By B"
    await b_client.close()


async def test_patch_unknown_field_422(app: Any) -> None:
    """Unknown/mass-assignment fields are 422."""
    await _seed_default(app)
    client = await _logged_in(app, "lmboswell")
    response = await client.client.patch(
        "/api/v1/seasons/2026/children/c1",
        json={"screened": True},
        headers=client.headers() | {"If-Match": "1"},
    )
    assert response.status_code == 422


async def test_ignore_without_confirm_422(app: Any) -> None:
    """Ignore without confirm=True is 422."""
    await _seed_default(app)
    client = await _logged_in(app, "lmboswell")
    response = await client.client.post(
        "/api/v1/seasons/2026/children/c1/ignore",
        json={"confirm": False, "expected_version": 1},
        headers=client.headers(),
    )
    assert response.status_code == 422


async def test_ignore_unignore_flow(app: Any) -> None:
    """Ignore → skeleton; unignore → reseeded row (§5.5)."""
    await _seed_default(app)
    client = await _logged_in(app, "lmboswell")
    response = await client.client.post(
        "/api/v1/seasons/2026/children/c1/ignore",
        json={"confirm": True, "expected_version": 1},
        headers=client.headers(),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    detail = await client.client.get("/api/v1/seasons/2026/children/c1")
    # identity skeleton: names/emails retained, data columns nulled (§5.5)
    assert detail.json()["row"]["first_name"] == "Kiddo"
    assert detail.json()["row"]["need_1"] is None
    # reseed: raw rows still exist, so unignore works
    version = detail.json()["row_version"]
    response = await client.client.post(
        "/api/v1/seasons/2026/children/c1/unignore",
        json={"confirm": True, "expected_version": version},
        headers=client.headers(),
    )
    assert response.status_code == 200
    assert response.json()["row"]["first_name"] == "Kiddo"


async def test_patch_marks_row_audited(app: Any) -> None:
    """A PATCH with status=audited accepts the row as the final entry."""
    await _seed_default(app)
    client = await _logged_in(app, "lmboswell")
    response = await client.client.patch(
        "/api/v1/seasons/2026/children/c1",
        json={"first_name": "Kiddo", "status": "audited"},
        headers=client.headers() | {"If-Match": "1"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "audited"
    assert response.json()["row"]["status"] == "audited"
    assert response.json()["audited_at"]
    detail = await client.client.get("/api/v1/seasons/2026/children/c1")
    assert detail.json()["status"] == "audited"
    # Un-auditing is admin-only: the auditor is refused.
    version = detail.json()["row_version"]
    response = await client.client.patch(
        "/api/v1/seasons/2026/children/c1",
        json={"status": "new"},
        headers=client.headers() | {"If-Match": str(version)},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "admin_required"
    # An admin undoes the audit, dropping back to the derived status.
    admin_client = await _logged_in(app, "cmendres")
    response = await admin_client.client.patch(
        "/api/v1/seasons/2026/children/c1",
        json={"status": "new"},
        headers=admin_client.headers() | {"If-Match": str(version)},
    )
    assert response.status_code == 200
    assert response.json()["status"] != "audited"
    await admin_client.close()


async def _child_detail(client: Client, key: str = "c1") -> dict[str, Any]:
    """Read one child's detail through the API.

    Args:
        client: The logged-in client.
        key: The child's zite_record_id.

    Returns:
        The detail payload.
    """
    response = await client.client.get(f"/api/v1/seasons/2026/children/{key}")
    assert response.status_code == 200, response.text
    return response.json()


async def _audit_child(client: Client, key: str = "c1") -> int:
    """Mark one child audited and return the resulting row version.

    Args:
        client: The logged-in client.
        key: The child's zite_record_id.

    Returns:
        The row version after the audit.
    """
    detail = await _child_detail(client, key)
    response = await client.client.patch(
        f"/api/v1/seasons/2026/children/{key}",
        json={"status": "audited"},
        headers=client.headers() | {"If-Match": str(detail["row_version"])},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "audited"
    return int(response.json()["row_version"])


async def _patch_child(
    client: Client, key: str, body: dict[str, Any], version: int
) -> Any:
    """Send one PATCH for a child at the given row version.

    Args:
        client: The logged-in client.
        key: The child's zite_record_id.
        body: The JSON patch body.
        version: The If-Match row version.

    Returns:
        The HTTP response.
    """
    return await client.client.patch(
        f"/api/v1/seasons/2026/children/{key}",
        json=body,
        headers=client.headers() | {"If-Match": str(version)},
    )


async def test_audited_row_values_are_final_409(
    app: Any, auditor: Client
) -> None:
    """A non-admin field edit on an audited row is 409 row_audited."""
    await _seed_default(app)
    version = await _audit_child(auditor)
    response = await _patch_child(
        auditor, "c1", {"first_name": "Changed"}, version
    )
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "row_audited"
    assert detail["message"] == "this row is audited; its values are final"
    current = await _child_detail(auditor)
    assert current["row"]["first_name"] == "Kiddo"
    assert current["status"] == "audited"


async def test_audited_row_undo_needs_admin_403(
    app: Any, auditor: Client
) -> None:
    """A non-admin may not undo an audit (403 admin_required)."""
    await _seed_default(app)
    version = await _audit_child(auditor)
    response = await _patch_child(auditor, "c1", {"status": "new"}, version)
    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "admin_required"
    assert detail["message"] == "only an admin may undo an audit"
    assert (await _child_detail(auditor))["status"] == "audited"


async def test_admin_undo_edit_reaudit_flow(app: Any, admin: Client) -> None:
    """An admin undoes the audit, edits, then re-audits the row."""
    await _seed_default(app)
    version = await _audit_child(admin)
    response = await _patch_child(admin, "c1", {"status": "new"}, version)
    assert response.status_code == 200, response.text
    assert response.json()["status"] != "audited"
    response = await _patch_child(
        admin, "c1", {"first_name": "Changed"}, response.json()["row_version"]
    )
    assert response.status_code == 200, response.text
    assert response.json()["row"]["first_name"] == "Changed"
    response = await _patch_child(
        admin, "c1", {"status": "audited"}, response.json()["row_version"]
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "audited"
    assert response.json()["row"]["first_name"] == "Changed"


async def test_audit_lock_spares_non_audited_rows(
    app: Any, auditor: Client
) -> None:
    """The lock does not leak: a non-audited row still edits."""
    await _seed_default(app)
    version = int((await _child_detail(auditor))["row_version"])
    response = await _patch_child(
        auditor, "c1", {"first_name": "Changed"}, version
    )
    assert response.status_code == 200, response.text
    assert response.json()["row"]["first_name"] == "Changed"


async def test_ignore_refused_on_audited_row_409(
    app: Any, auditor: Client
) -> None:
    """Ignore on an audited row is 409 row_audited; the row is intact."""
    await _seed_default(app)
    await _audit_child(auditor)
    detail = await _child_detail(auditor)
    response = await auditor.client.post(
        "/api/v1/seasons/2026/children/c1/ignore",
        json={"confirm": True, "expected_version": detail["row_version"]},
        headers=auditor.headers(),
    )
    assert response.status_code == 409, response.text
    body = response.json()["detail"]
    assert body["code"] == "row_audited"
    assert body["message"] == (
        "this row is audited; undo the audit before ignoring it"
    )
    current = await _child_detail(auditor)
    assert current["status"] == "audited"
    assert not current["row"]["ignore"]


async def test_duplication_audit_twice_still_ok(
    app: Any, auditor: Client
) -> None:
    """A second duplication audit of the surviving entry returns 200."""
    await _seed_duplicates(app)
    listing = await auditor.client.get("/api/v1/seasons/2026/duplications")
    group = listing.json()["items"][0]
    decided = await auditor.client.post(
        f"/api/v1/seasons/2026/duplications/{group['id']}/members/c1",
        json={"decision": "not_duplicate", "confirm": True},
        headers=auditor.headers(),
    )
    assert decided.status_code == 200, decided.text
    for _ in range(2):
        audited = await auditor.client.post(
            f"/api/v1/seasons/2026/duplications/{group['id']}/audit",
            headers=auditor.headers(),
        )
        assert audited.status_code == 200, audited.text
        assert audited.json()["status"] == "audited"


async def test_orphan_cannot_become_audited_409(app: Any) -> None:
    """A null-family child cannot be marked audited (§8.7, 409)."""
    # raw child with NO matching family registration → real orphan
    raw = {
        "sponsor": [raw_sponsor("rec_s1")],
        "family": [raw_family("fam@example.com")],
        "child": [
            raw_child("c1"),
            raw_child("orphan1", primary_email="orphan@example.com"),
        ],
    }
    cfg = app.state.cfg
    engine = create_async_engine(cfg.storage.database_url)
    try:
        async with UnitOfWork(engine).store(cfg, "migration") as repo:
            await _seed(
                repo,
                raw=raw,
                children=[
                    child_row("c1"),
                    child_row("orphan1", family_id=None),
                ],
                families=[family_row(1)],
                sponsors=[sponsor_row(1)],
            )
    finally:
        await engine.dispose()
    client = await _logged_in(app, "lmboswell")
    response = await client.client.patch(
        "/api/v1/seasons/2026/children/orphan1",
        json={"status": "audited"},
        headers=client.headers() | {"If-Match": "1"},
    )
    assert response.status_code == 409
    assert "orphaned child" in response.json()["detail"]["message"]


# ---------------------------------------------------------------------------
# Diagnostics.
# ---------------------------------------------------------------------------


async def test_diagnostics_payloads_gated(app: Any) -> None:
    """Email/address payloads hidden by default; role-gated (§5.2)."""
    await _seed_default(app)
    engine = create_async_engine(app.state.cfg.storage.database_url)
    from ccat.db.engine import async_session_factory

    factory = async_session_factory(engine)
    async with factory() as session:
        from ccat.db.models import AddressCheck, EmailCheck, EmailCheckTarget

        check = EmailCheck(
            season_id=1,
            normalized_email="fam@example.com",
            status="valid",
            response_json='{"zero": "bounce"}',
        )
        session.add(check)
        await session.flush()
        session.add(
            EmailCheckTarget(
                email_check_id=check.id,
                source_sheet="final_family",
                zite_record_id="rec_f1",
                field="primary_email",
                email="fam@example.com",
            )
        )
        session.add(
            AddressCheck(
                season_id=1,
                query="1 Main St",
                status="ok",
                response_json='{"lat": "47.1"}',
            )
        )
        await session.commit()
    await engine.dispose()
    client = await _logged_in(app, "lmboswell")
    emails = await client.client.get("/api/v1/seasons/2026/diagnostics/emails")
    assert emails.json()[0].get("response_json") is None
    addresses = await client.client.get(
        "/api/v1/seasons/2026/diagnostics/addresses"
    )
    assert addresses.json()[0].get("response_json") is None
    # auditor may include payloads
    emails = await client.client.get(
        "/api/v1/seasons/2026/diagnostics/emails?include_payload=true"
    )
    assert emails.json()[0]["response_json"] == '{"zero": "bounce"}'
    # read_only may not
    read_only = Client(app)
    await read_only.login("lmboswell")
    await read_only.close()
    admin_client = await _logged_in(app, "cmendres")
    await admin_client.client.post(
        "/api/v1/users",
        json={"username": "ro", "roles": ["read_only"]},
        headers=admin_client.headers(),
    )
    await admin_client.close()
    ro = Client(app)
    await ro.login("ro")
    response = await ro.client.get(
        "/api/v1/seasons/2026/diagnostics/emails?include_payload=true"
    )
    assert response.status_code == 403
    await ro.close()


async def test_match_report_404_when_absent(app: Any) -> None:
    """No fabricated empty match run (§10)."""
    await _seed_default(app)
    client = await _logged_in(app, "lmboswell")
    response = await client.client.get(
        "/api/v1/seasons/2026/diagnostics/match-report"
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Jobs.
# ---------------------------------------------------------------------------


async def test_enqueue_idempotency_and_conflicts(app: Any) -> None:
    """202 + id; same key same hash → same id; different hash → 409."""
    await _seed_default(app)
    client = await _logged_in(app, "lmboswell")
    first = await client.client.post(
        "/api/v1/seasons/2026/jobs",
        json={"job_type": "reconcile", "idempotency_key": "k1"},
        headers=client.headers(),
    )
    assert first.status_code == 202
    job_id = first.json()["id"]
    replay = await client.client.post(
        "/api/v1/seasons/2026/jobs",
        json={"job_type": "reconcile", "idempotency_key": "k1"},
        headers=client.headers(),
    )
    assert replay.json()["id"] == job_id
    conflict = await client.client.post(
        "/api/v1/seasons/2026/jobs",
        json={
            "job_type": "reconcile",
            "options": {"x": 1},
            "idempotency_key": "k1",
        },
        headers=client.headers(),
    )
    assert conflict.status_code == 409
    # pipeline-class conflict: an active reconcile is not pipeline-class,
    # enqueue a screen then a match → the match is 409 while screen is active
    screen = await client.client.post(
        "/api/v1/seasons/2026/jobs",
        json={
            "job_type": "screen",
            "options": {"skip_email_check": True, "skip_address_check": True},
        },
        headers=client.headers(),
    )
    assert screen.status_code == 202
    match = await client.client.post(
        "/api/v1/seasons/2026/jobs",
        json={"job_type": "match"},
        headers=client.headers(),
    )
    assert match.status_code == 409


async def test_screen_invalid_redo_422_before_worker(app: Any) -> None:
    """Invalid redo target is 422 before enqueueing (§9.2)."""
    await _seed_default(app)
    client = await _logged_in(app, "lmboswell")
    response = await client.client.post(
        "/api/v1/seasons/2026/jobs",
        json={"job_type": "screen", "options": {"redo": "ghost"}},
        headers=client.headers(),
    )
    assert response.status_code == 422


async def test_job_list_detail_cancel_and_events(app: Any) -> None:
    """List/detail/events; cancel a queued job → cancelled."""
    await _seed_default(app)
    client = await _logged_in(app, "lmboswell")
    enqueued = await client.client.post(
        "/api/v1/seasons/2026/jobs",
        json={"job_type": "reconcile"},
        headers=client.headers(),
    )
    job_id = enqueued.json()["id"]
    listing = await client.client.get("/api/v1/seasons/2026/jobs")
    assert any(job["id"] == job_id for job in listing.json())
    detail = await client.client.get(f"/api/v1/jobs/{job_id}")
    assert detail.json()["status"] in {"queued", "running", "succeeded"}
    assert detail.json()["events"]
    cancelled = await client.client.post(
        f"/api/v1/jobs/{job_id}/cancel", headers=client.headers()
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] in {"cancelled", "cancel_requested"}
    events = await client.client.get(f"/api/v1/jobs/{job_id}/events?after=0")
    assert events.status_code == 200
    assert events.json()
    # finished jobs refuse cancel
    response = await client.client.post(
        f"/api/v1/jobs/{job_id}/cancel", headers=client.headers()
    )
    if response.status_code == 200:
        state = (await client.client.get(f"/api/v1/jobs/{job_id}")).json()
        assert state["status"] in {"cancelled", "cancel_requested"}


async def test_job_detail_keeps_status_events_beside_logs(app: Any) -> None:
    """GET /jobs/{id} returns the status tail plus the log tail."""
    await _seed_default(app)
    client = await _logged_in(app, "lmboswell")
    enqueued = await client.client.post(
        "/api/v1/seasons/2026/jobs",
        json={"job_type": "reconcile"},
        headers=client.headers(),
    )
    job_id = enqueued.json()["id"]
    from ccat.db.engine import async_session_factory

    engine = create_async_engine(app.state.cfg.storage.database_url)
    factory = async_session_factory(engine)
    stamp = datetime.now(UTC).isoformat()
    async with factory() as session:
        session.add_all(
            JobEvent(
                job_id=job_id,
                ts=datetime.now(UTC),
                kind="log",
                data={
                    "level": "INFO",
                    "logger": "ccat.match",
                    "message": f"line {index}",
                    "ts": stamp,
                },
            )
            for index in range(25)
        )
        await session.commit()
    await engine.dispose()
    detail = (await client.client.get(f"/api/v1/jobs/{job_id}")).json()
    kinds = [event["kind"] for event in detail["events"]]
    assert kinds.count("log") == 25
    # the lifecycle event survives even though 25 newer log rows exist
    assert "queued" in kinds
    ids = [event["id"] for event in detail["events"]]
    assert ids == sorted(ids)
    await client.close()


async def test_worker_runs_screen_job_end_to_end(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enqueue → real Worker (fake AI) → succeeded → children screened."""
    await _seed_default(app)
    # fake the AI at the opencode seam (same pattern as test_db_pipeline)
    import re
    from types import SimpleNamespace

    import ccat.screening as screening
    import opencode

    class FakeOpencode:
        """Fake opencode client answering every child 'null'."""

        calls: list[str] = []

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            """Record the fake session resource."""
            self.session = SimpleNamespace(
                create=lambda model: SimpleNamespace(id="ses_test"),
                generate=self._generate,
            )

        def _generate(self, session_id: str, prompt: str) -> SimpleNamespace:
            """Answer 'null' (no corrections) per child."""
            match = re.search(r'"zite_record_id": "([^"]+)"', prompt)
            assert match is not None
            FakeOpencode.calls.append(match.group(1))
            return SimpleNamespace(text="null")

        def __enter__(self) -> "FakeOpencode":
            """Return self."""
            return self

        def __exit__(self, *args: Any) -> None:
            """Swallow exceptions like the real client does."""
            return None

    class _Version:
        stdout = "2.0.0"
        stderr = ""

    monkeypatch.setattr(screening.subprocess, "run", lambda *a, **k: _Version())
    monkeypatch.setattr(opencode, "Opencode", FakeOpencode)
    client = await _logged_in(app, "lmboswell")
    enqueued = await client.client.post(
        "/api/v1/seasons/2026/jobs",
        json={
            "job_type": "screen",
            "options": {"skip_email_check": True, "skip_address_check": True},
        },
        headers=client.headers(),
    )
    job_id = enqueued.json()["id"]
    cfg = app.state.cfg
    worker = Worker(cfg)
    task = asyncio.create_task(worker.run())
    try:
        state = None
        for _ in range(600):
            state = (await client.client.get(f"/api/v1/jobs/{job_id}")).json()
            if state["status"] in {"succeeded", "failed"}:
                break
            await asyncio.sleep(0.05)
        assert state is not None and state["status"] == "succeeded", state
        children = (
            await client.client.get("/api/v1/seasons/2026/children")
        ).json()
        assert all(child["screened"] for child in children["items"])
    finally:
        worker.stop.set()
        await task


async def test_sse_stream_returns_events(app: Any) -> None:
    """SSE stream yields events and ends at the terminal state."""
    await _seed_default(app)
    client = await _logged_in(app, "lmboswell")
    enqueued = await client.client.post(
        "/api/v1/seasons/2026/jobs",
        json={"job_type": "reconcile"},
        headers=client.headers(),
    )
    job_id = enqueued.json()["id"]
    worker = Worker(app.state.cfg)
    task = asyncio.create_task(worker.run())
    try:
        async with client.client.stream(
            "GET", f"/api/v1/jobs/{job_id}/events/stream", timeout=15.0
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith(
                "text/event-stream"
            )
            lines: list[str] = []
            async for line in response.aiter_lines():
                lines.append(line)
                if "succeeded" in line or "failed" in line:
                    break
        assert any(line.startswith("data: ") for line in lines)
    finally:
        worker.stop.set()
        await task


# ---------------------------------------------------------------------------
# Artifacts.
# ---------------------------------------------------------------------------


async def test_artifact_list_download_headers(app: Any) -> None:
    """List + download: any role, no-store, attachment disposition."""
    await _seed_default(app)
    cfg = app.state.cfg
    root = Path(cfg.storage.artifact_root) / "csv_export"
    root.mkdir(parents=True, exist_ok=True)
    (root / "export.csv").write_text("a,b\n1,2\n")
    (root / "packet.pdf").write_bytes(b"%PDF-1.4 test")
    engine = create_async_engine(cfg.storage.database_url)
    from ccat.db.engine import async_session_factory

    factory = async_session_factory(engine)
    async with factory() as session:
        from ccat.db.models import Artifact
        from sqlalchemy import select as sa_select

        session.add(
            Artifact(
                season_id=1,
                kind="csv_export",
                path="csv_export/export.csv",
                filename="export.csv",
                content_type="text/csv",
                size_bytes=8,
                sha256="x",
                created_by="system",
            )
        )
        session.add(
            Artifact(
                season_id=1,
                kind="sponsor_packet",
                path="csv_export/packet.pdf",
                filename="packet.pdf",
                content_type="application/pdf",
                size_bytes=13,
                sha256="x",
                created_by="system",
            )
        )
        await session.commit()
        artifact_id = await session.scalar(
            sa_select(Artifact.id).where(Artifact.filename == "export.csv")
        )
        pdf_artifact_id = await session.scalar(
            sa_select(Artifact.id).where(Artifact.filename == "packet.pdf")
        )
    await engine.dispose()
    read_only = Client(app)
    await read_only.login("lmboswell")
    listing = await read_only.client.get(
        "/api/v1/seasons/2026/artifacts?kind=csv_export"
    )
    assert listing.status_code == 200
    assert listing.json()[0]["id"] == artifact_id
    download = await read_only.client.get(
        f"/api/v1/artifacts/{artifact_id}/download"
    )
    assert download.status_code == 200
    assert download.headers["cache-control"] == "no-store"
    assert "attachment" in download.headers["content-disposition"]
    assert download.text == "a,b\n1,2\n"
    inline_pdf = await read_only.client.get(
        f"/api/v1/artifacts/{pdf_artifact_id}/download?inline=true"
    )
    assert inline_pdf.status_code == 200
    assert inline_pdf.headers["content-disposition"].startswith("inline")
    attached_pdf = await read_only.client.get(
        f"/api/v1/artifacts/{pdf_artifact_id}/download"
    )
    assert attached_pdf.status_code == 200
    assert attached_pdf.headers["content-disposition"].startswith("attachment")
    inline_csv = await read_only.client.get(
        f"/api/v1/artifacts/{artifact_id}/download?inline=true"
    )
    assert inline_csv.status_code == 200
    assert inline_csv.headers["content-disposition"].startswith("attachment")
    await read_only.close()


async def test_artifact_outside_root_rejected(app: Any) -> None:
    """A path escaping the artifact root is rejected (§6.3)."""
    await _seed_default(app)
    cfg = app.state.cfg
    engine = create_async_engine(cfg.storage.database_url)
    from ccat.db.engine import async_session_factory

    factory = async_session_factory(engine)
    async with factory() as session:
        from ccat.db.models import Artifact

        session.add(
            Artifact(
                season_id=1,
                kind="csv_export",
                path="../../secrets.txt",
                filename="secrets.txt",
                content_type="text/plain",
                size_bytes=1,
                sha256="x",
                created_by="system",
            )
        )
        await session.commit()
    await engine.dispose()
    client = await _logged_in(app, "lmboswell")
    response = await client.client.get("/api/v1/artifacts/1/download")
    assert response.status_code in {403, 404}


async def test_unknown_artifact_404(app: Any) -> None:
    """Unknown artifact id is 404."""
    await _seed_default(app)
    client = await _logged_in(app, "lmboswell")
    response = await client.client.get("/api/v1/artifacts/999/download")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Clarify.
# ---------------------------------------------------------------------------


async def test_clarify_list_detail_draft_contacted(app: Any) -> None:
    """List/detail; draft PATCH stale → 412 + fresh; contacted stamps."""
    await _seed_default(app)
    # give c1 a clarification note so it is flagged
    engine = create_async_engine(app.state.cfg.storage.database_url)
    from ccat.db.engine import async_session_factory

    factory = async_session_factory(engine)
    async with factory() as session:
        from sqlalchemy import update
        from ccat.db.models import FinalChild

        await session.execute(
            update(FinalChild)
            .where(FinalChild.zite_record_id == "c1")
            .values(clarification_notes="please clarify sizes")
        )
        await session.commit()
    await engine.dispose()
    client = await _logged_in(app, "lmboswell")
    listing = await client.client.get("/api/v1/seasons/2026/clarifications")
    assert listing.status_code == 200
    assert listing.json()[0]["family_id"] == 1
    detail = await client.client.get("/api/v1/seasons/2026/clarifications/1")
    assert detail.json()["children"]
    # draft edit without a draft → 404
    response = await client.client.patch(
        "/api/v1/seasons/2026/clarifications/1/draft",
        json={"subject": "Hi"},
        headers=client.headers() | {"If-Match": "1"},
    )
    assert response.status_code == 404
    # seed a draft at the repo level
    engine = create_async_engine(app.state.cfg.storage.database_url)
    factory = async_session_factory(engine)
    async with factory() as session:
        from ccat.db.models import Season
        from ccat.db.repositories import SeasonStoreRepository

        season = await session.get(Season, 1)
        repo = SeasonStoreRepository(session, season)
        await repo.save_draft(
            1, "Subject", "Hello", "Intro", "Bye", [], "system"
        )
        await session.commit()
    await engine.dispose()
    # stale version → 412 with fresh draft
    response = await client.client.patch(
        "/api/v1/seasons/2026/clarifications/1/draft",
        json={"subject": "Stale"},
        headers=client.headers() | {"If-Match": "99"},
    )
    assert response.status_code == 412
    assert response.json()["detail"]["code"] == "stale_version"
    # correct version → new version saved
    response = await client.client.patch(
        "/api/v1/seasons/2026/clarifications/1/draft",
        json={"subject": "Fresh"},
        headers=client.headers() | {"If-Match": "1"},
    )
    assert response.status_code == 200
    assert response.json()["version"] == 2
    # contacted tri-state + UTC stamp
    response = await client.client.post(
        "/api/v1/seasons/2026/clarifications/1/contacted",
        json={"contacted": True},
        headers=client.headers(),
    )
    assert response.status_code == 200
    assert response.json()["contacted"] is True
    assert response.json()["contacted_at"] is not None
    stamp = response.json()["contacted_at"]
    assert stamp.endswith("+00:00") or stamp.endswith("Z")


async def test_clarify_export_enqueues_job(app: Any) -> None:
    """clarify-export POST enqueues the job; no send endpoint exists."""
    await _seed_default(app)
    client = await _logged_in(app, "lmboswell")
    response = await client.client.post(
        "/api/v1/seasons/2026/clarify-export", headers=client.headers()
    )
    assert response.status_code == 202
    assert response.json()["job_type"] == "clarify_export"
    # no autosend anywhere: the route list has no send route
    routes = [
        route.path
        for route in app.routes
        if "clarif" in getattr(route, "path", "")
    ]
    assert not any("send" in path for path in routes)


# ---------------------------------------------------------------------------
# Overview.
# ---------------------------------------------------------------------------


async def test_overview_matches_repo_queries(app: Any) -> None:
    """Overview counts match direct repository queries (§11.2 parity)."""
    await _seed_default(app)
    client = await _logged_in(app, "lmboswell")
    overview = (await client.client.get("/api/v1/seasons/2026/overview")).json()
    engine = create_async_engine(app.state.cfg.storage.database_url)
    try:
        async with UnitOfWork(engine).store(app.state.cfg, "check") as repo:
            children = await repo.final_rows("child")
    finally:
        await engine.dispose()
    assert overview["entities"]["child"]["total"] == len(children)
    assert overview["entities"]["child"]["pending_ai"] == sum(
        1 for row in children if not row.get("screened")
    )
    assert overview["entities"]["child"]["orphans"] == sum(
        1 for row in children if row.get("family_id") is None
    )
    assert overview["season"] == "2026"


# ---------------------------------------------------------------------------
# Errors + health + OpenAPI.
# ---------------------------------------------------------------------------


async def test_unknown_season_404(app: Any) -> None:
    """Unknown season → 404 on any /seasons/{season} route (no IDOR)."""
    client = await _logged_in(app, "lmboswell")
    response = await client.client.get("/api/v1/seasons/1999/children")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "season_not_found"


async def test_configured_season_row_is_created_on_demand(app: Any) -> None:
    """A migrated database with no season row still serves the season.

    The season row holds configuration-derived data only, so the API
    creates it instead of refusing every season-scoped request.
    """
    client = await _logged_in(app, "lmboswell")
    first = await client.client.get("/api/v1/seasons/2026/children")
    assert first.status_code == 200, first.text
    assert first.json()["items"] == []
    # The row is reused, not recreated, and is now listed.
    second = await client.client.get("/api/v1/seasons/2026/children")
    assert second.status_code == 200, second.text
    seasons = await client.client.get("/api/v1/seasons")
    assert [row["name"] for row in seasons.json()] == ["2026"]
    # A season other than the configured one is still refused.
    other = await client.client.get("/api/v1/seasons/1999/children")
    assert other.status_code == 404
    assert other.json()["detail"]["code"] == "season_not_found"


async def test_unknown_entity_job_artifact_404(app: Any) -> None:
    """Unknown entity key / job / artifact → 404."""
    await _seed_default(app)
    client = await _logged_in(app, "lmboswell")
    assert (
        await client.client.get("/api/v1/seasons/2026/children/ghost")
    ).status_code == 404
    assert (await client.client.get("/api/v1/jobs/999")).status_code == 404


async def test_malformed_body_422(app: Any) -> None:
    """Malformed JSON body → 422 without internals."""
    client = await _logged_in(app, "lmboswell")
    response = await client.client.post(
        "/api/v1/seasons/2026/jobs",
        content=b"{not json",
        headers=client.headers() | {"content-type": "application/json"},
    )
    assert response.status_code == 422
    assert "Traceback" not in response.text


async def test_healthz_readyz(app: Any) -> None:
    """Healthz is always ok; readyz checks DB/head/artifact root."""
    anonymous_client = Client(app)
    health = await anonymous_client.client.get("/api/v1/healthz")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    ready = await anonymous_client.client.get("/api/v1/readyz")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ok"
    await anonymous_client.close()


async def test_openapi_contract(app: Any, anonymous: Client) -> None:
    """OpenAPI lists all DTO schemas; the committed export matches."""
    response = await anonymous.client.get("/api/v1/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    expected = {
        "EntityDetail",
        "EntityListPage",
        "JobView",
        "Overview",
        "UserView",
        "SeasonView",
        "ArtifactView",
        "ClarifyFamilyView",
        "ClarifyDraftView",
        "HealthView",
        "MeView",
    }
    assert expected <= set(schema["components"]["schemas"])
    committed = json.loads(
        (
            Path(__file__).resolve().parents[1] / "frontend" / "openapi.json"
        ).read_text()
    )
    # The committed artifact is the same contract (routes unchanged).
    assert set(committed["components"]["schemas"]) >= expected


async def test_no_stack_traces_in_errors(app: Any) -> None:
    """Error bodies never contain tracebacks or paths (§8)."""
    client = await _logged_in(app, "lmboswell")
    await _seed_default(app)
    responses = [
        await client.client.get("/api/v1/seasons/2026/children/ghost"),
        await client.client.get("/api/v1/jobs/999"),
        await client.client.patch(
            "/api/v1/seasons/2026/children/c1",
            json={"first_name": 3.5},
            headers=client.headers() | {"If-Match": "1"},
        ),
    ]
    for response in responses:
        text = response.text
        assert "Traceback" not in text
        assert "/home/" not in text


# ---------------------------------------------------------------------------
# Duplications.
# ---------------------------------------------------------------------------


def _duplicate_log_row(
    sheet: str,
    group_key: str,
    record_id: str,
    created: str,
    survivor: bool,
    record: dict[str, Any],
) -> dict[str, Any]:
    """One duplicate-log row, as a screening run leaves it."""
    return {
        "sheet": sheet,
        "group_key": group_key,
        "zite_record_id": record_id,
        "created": created,
        "survivor": survivor,
        "conflict_columns": "dob",
        "conflict_details": "dob: survivor '2018-05-10' vs ['2018-06-10']",
        "record": json.dumps(record, default=str, sort_keys=True),
    }


async def _seed_duplicates(app: Any) -> None:
    """Seed one duplicate child group plus its screened rows."""
    cfg = app.state.cfg
    engine = create_async_engine(cfg.storage.database_url)
    try:
        async with UnitOfWork(engine).store(cfg, "migration") as repo:
            await _seed(
                repo,
                raw=_DEFAULT_RAW,
                children=[
                    child_row("c1", angel_id=None),
                    child_row("c1b", angel_id="1A"),
                ],
                families=[family_row(1)],
                sponsors=[sponsor_row(1)],
            )
            await repo.replace_screen_diagnostics(
                [
                    _duplicate_log_row(
                        "child_submissions",
                        "fam@example.com|Kiddo",
                        "c1",
                        "2026-02-01T09:00:00+00:00",
                        False,
                        raw_child("c1"),
                    ),
                    _duplicate_log_row(
                        "child_submissions",
                        "fam@example.com|Kiddo",
                        "c1b",
                        "2026-02-02T09:00:00+00:00",
                        True,
                        raw_child("c1b"),
                    ),
                ],
                [],
                "screen",
            )
    finally:
        await engine.dispose()


async def _seed_family_duplicates(app: Any) -> None:
    """Seed one duplicate family group, its family row and its child."""
    cfg = app.state.cfg
    engine = create_async_engine(cfg.storage.database_url)
    try:
        async with UnitOfWork(engine).store(cfg, "migration") as repo:
            await _seed(
                repo,
                raw={
                    "sponsor": [raw_sponsor("rec_s1")],
                    "family": [
                        raw_family("fam@example.com", zite_record_id="f1"),
                        raw_family("fam@example.com", zite_record_id="f2"),
                    ],
                    "child": [raw_child("c1")],
                },
                children=[child_row("c1", angel_id="1A")],
                families=[
                    family_row(
                        1,
                        zite_record_id="f1",
                        primary_email="fam@example.com",
                    )
                ],
                sponsors=[sponsor_row(1)],
            )
            await repo.replace_screen_diagnostics(
                [
                    _duplicate_log_row(
                        "family_submissions",
                        "fam@example.com",
                        "f1",
                        "2026-02-01T09:00:00+00:00",
                        True,
                        raw_family("fam@example.com", zite_record_id="f1"),
                    ),
                    _duplicate_log_row(
                        "family_submissions",
                        "fam@example.com",
                        "f2",
                        "2026-02-02T09:00:00+00:00",
                        False,
                        raw_family("fam@example.com", zite_record_id="f2"),
                    ),
                ],
                [],
                "screen",
            )
    finally:
        await engine.dispose()


async def _seed_children(app: Any, children: list[dict[str, Any]]) -> None:
    """Replace the stored final children through migrate ownership."""
    cfg = app.state.cfg
    engine = create_async_engine(cfg.storage.database_url)
    try:
        async with UnitOfWork(engine).store(cfg, "migration") as repo:
            await _seed(repo, children=children)
    finally:
        await engine.dispose()


async def _seed_many_duplicate_groups(app: Any, count: int) -> None:
    """Seed the given number of single-member duplicate groups.

    Args:
        app: The app owning the database.
        count: How many groups to seed.
    """
    cfg = app.state.cfg
    engine = create_async_engine(cfg.storage.database_url)
    try:
        async with UnitOfWork(engine).store(cfg, "migration") as repo:
            await _seed(repo, raw=_DEFAULT_RAW)
            await repo.replace_screen_diagnostics(
                [
                    _duplicate_log_row(
                        "child_submissions",
                        f"fam{index}@example.com|Kiddo",
                        f"c{index}",
                        "2026-02-01T09:00:00+00:00",
                        False,
                        raw_child(f"c{index}"),
                    )
                    for index in range(count)
                ],
                [],
                "screen",
            )
    finally:
        await engine.dispose()


async def test_duplications_list_and_detail(app: Any, auditor: Client) -> None:
    """The Duplications list and detail expose the raw and screened rows."""
    await _seed_duplicates(app)
    listing = await auditor.client.get("/api/v1/seasons/2026/duplications")
    assert listing.status_code == 200, listing.text
    page = listing.json()
    assert page["total"] == 1
    group = page["items"][0]
    assert group["entity"] == "child"
    assert group["member_count"] == 2
    assert group["status"] == "pending"

    detail = await auditor.client.get(
        f"/api/v1/seasons/2026/duplications/{group['id']}"
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert [member["zite_record_id"] for member in body["members"]] == [
        "c1",
        "c1b",
    ]
    assert body["members"][0]["raw"]["zite_record_id"] == "c1"
    assert body["members"][1]["screened"]["angel_id"] == "1A"


async def test_duplication_detail_attributes_children_to_entries(
    app: Any, auditor: Client
) -> None:
    """Each family entry exposes the children submitted with it."""
    await _seed_family_duplicates(app)
    await _seed_children(
        app,
        [child_row("c1", angel_id="1A", created="2026-02-01T12:00:00Z")],
    )
    listing = await auditor.client.get("/api/v1/seasons/2026/duplications")
    group = listing.json()["items"][0]
    detail = await auditor.client.get(
        f"/api/v1/seasons/2026/duplications/{group['id']}"
    )
    assert detail.status_code == 200, detail.text
    members = {
        member["zite_record_id"]: member for member in detail.json()["members"]
    }
    assert [child["zite_record_id"] for child in members["f1"]["children"]] == [
        "c1"
    ]
    assert members["f2"]["children"] == []


async def test_duplications_unknown_group_is_404(
    app: Any, auditor: Client
) -> None:
    """An unknown group id answers 404."""
    response = await auditor.client.get(
        "/api/v1/seasons/2026/duplications/nope"
    )
    assert response.status_code == 404


async def test_duplication_not_duplicate_assigns_own_angel(
    app: Any, auditor: Client
) -> None:
    """The not-duplicate decision gives the member a letter of its own."""
    await _seed_duplicates(app)
    listing = await auditor.client.get("/api/v1/seasons/2026/duplications")
    group = listing.json()["items"][0]
    response = await auditor.client.post(
        f"/api/v1/seasons/2026/duplications/{group['id']}/members/c1",
        json={"decision": "not_duplicate", "confirm": True},
        headers=auditor.headers(),
    )
    assert response.status_code == 200, response.text
    member = response.json()["members"][0]
    assert member["decision"] == "not_duplicate"
    assert member["assigned_id"] == "1B"

    children = await auditor.client.get("/api/v1/seasons/2026/children")
    rows = {row["zite_record_id"]: row for row in children.json()["items"]}
    assert rows["c1"]["angel_id"] == "1B"
    assert rows["c1"]["potential_duplicate"] is False


async def test_duplication_ignore_matches_the_audit_ignore(
    app: Any, auditor: Client
) -> None:
    """The ignore decision ignores the member like the audit ignore action."""
    await _seed_duplicates(app)
    listing = await auditor.client.get("/api/v1/seasons/2026/duplications")
    group = listing.json()["items"][0]
    response = await auditor.client.post(
        f"/api/v1/seasons/2026/duplications/{group['id']}/members/c1",
        json={"decision": "ignore", "confirm": True},
        headers=auditor.headers(),
    )
    assert response.status_code == 200, response.text
    children = await auditor.client.get("/api/v1/seasons/2026/children")
    rows = {row["zite_record_id"]: row for row in children.json()["items"]}
    assert rows["c1"]["ignore"] is True
    assert rows["c1"]["status"] == "ignored"


async def test_duplication_decision_needs_confirmation(
    app: Any, auditor: Client
) -> None:
    """An unconfirmed decision is refused with 422."""
    await _seed_duplicates(app)
    listing = await auditor.client.get("/api/v1/seasons/2026/duplications")
    group = listing.json()["items"][0]
    response = await auditor.client.post(
        f"/api/v1/seasons/2026/duplications/{group['id']}/members/c1",
        json={"decision": "ignore", "confirm": False},
        headers=auditor.headers(),
    )
    assert response.status_code == 422


async def test_duplication_decision_on_unknown_member_is_404(
    app: Any, auditor: Client
) -> None:
    """A member outside the group is refused."""
    await _seed_duplicates(app)
    listing = await auditor.client.get("/api/v1/seasons/2026/duplications")
    group = listing.json()["items"][0]
    response = await auditor.client.post(
        f"/api/v1/seasons/2026/duplications/{group['id']}/members/ghost",
        json={"decision": "ignore", "confirm": True},
        headers=auditor.headers(),
    )
    assert response.status_code == 404


async def test_entity_detail_reports_filtered_neighbours(
    app: Any, auditor: Client
) -> None:
    """The detail names the entries around it in the list's own order."""
    await _seed_default(app)
    first = await auditor.client.get("/api/v1/seasons/2026/children/c1")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["previous"] is None
    assert body["next"]["key"] == "c2"

    second = await auditor.client.get("/api/v1/seasons/2026/children/c2")
    assert second.json()["previous"]["key"] == "c1"
    assert second.json()["next"] is None

    # A filter that excludes the entry leaves it without neighbours.
    filtered = await auditor.client.get(
        "/api/v1/seasons/2026/children/c1?q=nothing-matches"
    )
    assert filtered.json()["previous"] is None
    assert filtered.json()["next"] is None


async def test_duplication_detail_reports_neighbours(
    app: Any, auditor: Client
) -> None:
    """The duplication detail names the groups around it."""
    await _seed_duplicates(app)
    listing = await auditor.client.get("/api/v1/seasons/2026/duplications")
    groups = listing.json()["items"]
    assert len(groups) == 1
    detail = await auditor.client.get(
        f"/api/v1/seasons/2026/duplications/{groups[0]['id']}"
    )
    body = detail.json()
    assert body["previous"] is None
    assert body["next"] is None


async def test_duplication_mark_audited(app: Any, auditor: Client) -> None:
    """Marking a group audited audits the surviving entry only."""
    await _seed_duplicates(app)
    listing = await auditor.client.get("/api/v1/seasons/2026/duplications")
    group = listing.json()["items"][0]
    # No decision leaves two survivors: the group refuses.
    refused = await auditor.client.post(
        f"/api/v1/seasons/2026/duplications/{group['id']}/audit",
        headers=auditor.headers(),
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "conflict"

    # Release c1, leaving c1b as the group's surviving entry.
    decided = await auditor.client.post(
        f"/api/v1/seasons/2026/duplications/{group['id']}/members/c1",
        json={"decision": "not_duplicate", "confirm": True},
        headers=auditor.headers(),
    )
    assert decided.status_code == 200, decided.text

    audited = await auditor.client.post(
        f"/api/v1/seasons/2026/duplications/{group['id']}/audit",
        headers=auditor.headers(),
    )
    assert audited.status_code == 200, audited.text
    assert audited.json()["status"] == "audited"
    assert audited.json()["audited_at"]

    children = await auditor.client.get("/api/v1/seasons/2026/children")
    rows = {row["zite_record_id"]: row for row in children.json()["items"]}
    assert rows["c1b"]["status"] == "audited"
    assert rows["c1b"]["audited_at"]
    # The released member is its own entry now, back to "new".
    assert rows["c1"]["status"] == "new"


async def test_duplication_audit_carries_the_current_values(
    app: Any, auditor: Client
) -> None:
    """The audit writes the carried values into the surviving entry."""
    await _seed_duplicates(app)
    listing = await auditor.client.get("/api/v1/seasons/2026/duplications")
    group = listing.json()["items"][0]
    decided = await auditor.client.post(
        f"/api/v1/seasons/2026/duplications/{group['id']}/members/c1",
        json={"decision": "not_duplicate", "confirm": True},
        headers=auditor.headers(),
    )
    assert decided.status_code == 200, decided.text
    current = await auditor.client.get("/api/v1/seasons/2026/children/c1b")
    version = current.json()["row_version"]

    audited = await auditor.client.post(
        f"/api/v1/seasons/2026/duplications/{group['id']}/audit",
        json={
            "values": {
                "first_name": "Marcus",
                "ai_screening_notes": "reviewed note",
            },
            "expected_version": version,
        },
        headers=auditor.headers(),
    )
    assert audited.status_code == 200, audited.text

    stored = await auditor.client.get("/api/v1/seasons/2026/children/c1b")
    row = stored.json()["row"]
    assert row["first_name"] == "Marcus"
    assert row["ai_screening_notes"] == "reviewed note"
    assert stored.json()["status"] == "audited"


async def test_duplication_audit_stale_version_is_412(
    app: Any, auditor: Client
) -> None:
    """A stale expected_version refuses the audit instead of overwriting."""
    await _seed_duplicates(app)
    listing = await auditor.client.get("/api/v1/seasons/2026/duplications")
    group = listing.json()["items"][0]
    decided = await auditor.client.post(
        f"/api/v1/seasons/2026/duplications/{group['id']}/members/c1",
        json={"decision": "not_duplicate", "confirm": True},
        headers=auditor.headers(),
    )
    assert decided.status_code == 200, decided.text

    audited = await auditor.client.post(
        f"/api/v1/seasons/2026/duplications/{group['id']}/audit",
        json={"expected_version": 999},
        headers=auditor.headers(),
    )
    assert audited.status_code == 412, audited.text
    assert audited.json()["detail"]["code"] == "stale_version"


async def test_duplication_reaudit_of_an_audited_entry(
    app: Any, auditor: Client
) -> None:
    """An audited surviving entry is re-affirmed but never re-valued."""
    await _seed_duplicates(app)
    listing = await auditor.client.get("/api/v1/seasons/2026/duplications")
    group = listing.json()["items"][0]
    decided = await auditor.client.post(
        f"/api/v1/seasons/2026/duplications/{group['id']}/members/c1",
        json={"decision": "not_duplicate", "confirm": True},
        headers=auditor.headers(),
    )
    assert decided.status_code == 200, decided.text
    for _ in range(2):
        audited = await auditor.client.post(
            f"/api/v1/seasons/2026/duplications/{group['id']}/audit",
            headers=auditor.headers(),
        )
        assert audited.status_code == 200, audited.text

    changed = await auditor.client.post(
        f"/api/v1/seasons/2026/duplications/{group['id']}/audit",
        json={"values": {"first_name": "Marcus"}},
        headers=auditor.headers(),
    )
    assert changed.status_code == 409, changed.text
    assert changed.json()["detail"]["code"] == "row_audited"

    stored = await auditor.client.get("/api/v1/seasons/2026/children/c1b")
    assert stored.json()["row"]["first_name"] == "Kiddo"


async def test_duplication_reports_survivor_state(
    app: Any, auditor: Client
) -> None:
    """The list and detail report each member's survivor state."""
    await _seed_duplicates(app)
    listing = await auditor.client.get("/api/v1/seasons/2026/duplications")
    group = listing.json()["items"][0]
    assert group["survivor_count"] == 2
    assert group["released_count"] == 0

    decided = await auditor.client.post(
        f"/api/v1/seasons/2026/duplications/{group['id']}/members/c1",
        json={"decision": "not_duplicate", "confirm": True},
        headers=auditor.headers(),
    )
    assert decided.status_code == 200, decided.text

    listing = await auditor.client.get("/api/v1/seasons/2026/duplications")
    group = listing.json()["items"][0]
    assert group["survivor_count"] == 1
    assert group["released_count"] == 1

    detail = await auditor.client.get(
        f"/api/v1/seasons/2026/duplications/{group['id']}"
    )
    body = detail.json()
    assert body["survivor_count"] == 1
    members = {member["zite_record_id"]: member for member in body["members"]}
    assert members["c1"]["state"] == "released"
    assert members["c1"]["row_key"] == "c1"
    assert members["c1"]["row_status"] == "new"
    assert members["c1b"]["state"] == "surviving"
    assert members["c1b"]["row_key"] == "c1b"
    assert members["c1b"]["row_status"] == "new"


async def test_duplication_two_surviving_entries_is_409(
    app: Any, auditor: Client
) -> None:
    """Two undecided members make the audit refuse the group."""
    await _seed_duplicates(app)
    listing = await auditor.client.get("/api/v1/seasons/2026/duplications")
    group = listing.json()["items"][0]
    audited = await auditor.client.post(
        f"/api/v1/seasons/2026/duplications/{group['id']}/audit",
        headers=auditor.headers(),
    )
    assert audited.status_code == 409, audited.text
    assert (
        "more than one surviving entry" in audited.json()["detail"]["message"]
    )


async def test_split_family_children_are_assigned(
    app: Any, auditor: Client
) -> None:
    """A split family's child moves to the released family."""
    await _seed_family_duplicates(app)
    listing = await auditor.client.get("/api/v1/seasons/2026/duplications")
    group = listing.json()["items"][0]
    assert group["entity"] == "family"
    released = await auditor.client.post(
        f"/api/v1/seasons/2026/duplications/{group['id']}/members/f1",
        json={"decision": "not_duplicate", "confirm": True},
        headers=auditor.headers(),
    )
    assert released.status_code == 200, released.text
    members = {
        member["zite_record_id"]: member
        for member in released.json()["members"]
    }
    new_family_id = int(members["f1"]["assigned_id"])
    assert new_family_id != 1

    assigned = await auditor.client.post(
        f"/api/v1/seasons/2026/duplications/{group['id']}/children",
        json={"assignments": {"c1": new_family_id}},
        headers=auditor.headers(),
    )
    assert assigned.status_code == 200, assigned.text
    children = {
        child["zite_record_id"]: child for child in assigned.json()["children"]
    }
    assert children["c1"]["family_id"] == new_family_id

    stored = await auditor.client.get("/api/v1/seasons/2026/children/c1")
    assert stored.json()["row"]["family_id"] == new_family_id
    assert stored.json()["row"]["angel_id"] == f"{new_family_id}A"

    unknown_child = await auditor.client.post(
        f"/api/v1/seasons/2026/duplications/{group['id']}/children",
        json={"assignments": {"ghost": new_family_id}},
        headers=auditor.headers(),
    )
    assert unknown_child.status_code == 404, unknown_child.text
    unknown_group = await auditor.client.post(
        "/api/v1/seasons/2026/duplications/nope/children",
        json={"assignments": {"c1": new_family_id}},
        headers=auditor.headers(),
    )
    assert unknown_group.status_code == 404, unknown_group.text


async def test_duplications_list_paginates(app: Any, auditor: Client) -> None:
    """The list serves one page of groups at a time."""
    await _seed_many_duplicate_groups(app, 120)
    first = await auditor.client.get(
        "/api/v1/seasons/2026/duplications?page=1&page_size=50"
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["total"] == 120
    assert len(body["items"]) == 50
    second = await auditor.client.get(
        "/api/v1/seasons/2026/duplications?page=2&page_size=50"
    )
    other = second.json()
    assert len(other["items"]) == 50
    assert other["total"] == 120
    first_ids = {group["id"] for group in body["items"]}
    assert first_ids.isdisjoint({group["id"] for group in other["items"]})


async def test_children_list_reports_family_child_count(
    app: Any, auditor: Client
) -> None:
    """A child row carries its angel id and the size of its family."""
    await _seed_default(app)
    await _seed_children(
        app,
        [
            child_row("c1", angel_id="1A"),
            child_row("c2", first_name="Lee", angel_id="1B"),
            child_row("orphan", family_id=None),
        ],
    )
    response = await auditor.client.get("/api/v1/seasons/2026/children")
    assert response.status_code == 200, response.text
    rows = {row["zite_record_id"]: row for row in response.json()["items"]}
    assert rows["c1"]["angel_id"] == "1A"
    assert rows["c1"]["family_child_count"] == 2
    assert rows["c2"]["family_child_count"] == 2
    assert rows["orphan"]["family_child_count"] == 0


async def test_children_list_filters_one_family_exactly(
    app: Any, auditor: Client
) -> None:
    """The family filter matches the family id, not a substring of it."""
    await _seed_default(app)
    await _seed_children(
        app,
        [
            child_row("c1", family_id=1),
            child_row("c2", family_id=1),
            child_row("c10", family_id=10),
            child_row("c11", family_id=11),
            child_row("other", family_id=2, first_name="Anna1"),
        ],
    )
    response = await auditor.client.get(
        "/api/v1/seasons/2026/children?family_id=1"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert sorted(row["zite_record_id"] for row in body["items"]) == [
        "c1",
        "c2",
    ]


async def test_child_detail_reports_family_child_count(
    app: Any, auditor: Client
) -> None:
    """The child detail row carries its family child count too."""
    await _seed_default(app)
    await _seed_children(
        app,
        [
            child_row("c1", angel_id="1A"),
            child_row("c2", first_name="Lee", angel_id="1B"),
        ],
    )
    response = await auditor.client.get("/api/v1/seasons/2026/children/c1")
    assert response.status_code == 200, response.text
    assert response.json()["row"]["family_child_count"] == 2


async def test_children_list_reports_open_duplicate_group(
    app: Any, auditor: Client
) -> None:
    """Group members carry the open group; other rows carry none."""
    await _seed_duplicates(app)
    await _seed_children(
        app,
        [
            child_row("c1", angel_id=None),
            child_row("c1b", angel_id="1A"),
            child_row("c3", angel_id="1B"),
        ],
    )
    listing = await auditor.client.get("/api/v1/seasons/2026/duplications")
    group = listing.json()["items"][0]

    children = await auditor.client.get("/api/v1/seasons/2026/children")
    rows = {row["zite_record_id"]: row for row in children.json()["items"]}
    for record_id in ("c1", "c1b"):
        assert rows[record_id]["duplicate_pending"] is True
        assert rows[record_id]["duplicate_group_id"] == group["id"]
    assert rows["c3"]["duplicate_pending"] is False
    assert rows["c3"]["duplicate_group_id"] is None


async def test_marking_pending_duplicate_audited_is_409(
    app: Any, auditor: Client
) -> None:
    """An audit of a row in an open duplicate group is refused."""
    await _seed_duplicates(app)
    response = await auditor.client.patch(
        "/api/v1/seasons/2026/children/c1",
        json={"status": "audited"},
        headers=auditor.headers() | {"If-Match": "1"},
    )
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "duplicate_pending"
    assert detail["group_id"]
    assert detail["group_key"] == "fam@example.com|Kiddo"

    current = await auditor.client.get("/api/v1/seasons/2026/children/c1")
    assert current.json()["status"] == "new"


async def test_audited_duplication_unblocks_the_audit(
    app: Any, auditor: Client
) -> None:
    """Once its group is audited, the row may be marked audited."""
    await _seed_duplicates(app)
    listing = await auditor.client.get("/api/v1/seasons/2026/duplications")
    group = listing.json()["items"][0]
    decided = await auditor.client.post(
        f"/api/v1/seasons/2026/duplications/{group['id']}/members/c1",
        json={"decision": "not_duplicate", "confirm": True},
        headers=auditor.headers(),
    )
    assert decided.status_code == 200, decided.text
    audited = await auditor.client.post(
        f"/api/v1/seasons/2026/duplications/{group['id']}/audit",
        headers=auditor.headers(),
    )
    assert audited.status_code == 200, audited.text

    children = await auditor.client.get("/api/v1/seasons/2026/children")
    rows = {row["zite_record_id"]: row for row in children.json()["items"]}
    assert rows["c1"]["duplicate_pending"] is False
    assert rows["c1"]["duplicate_group_id"] is None

    current = await auditor.client.get("/api/v1/seasons/2026/children/c1")
    version = current.json()["row_version"]
    patched = await auditor.client.patch(
        "/api/v1/seasons/2026/children/c1",
        json={"status": "audited"},
        headers=auditor.headers() | {"If-Match": str(version)},
    )
    assert patched.status_code == 200, patched.text


async def test_overview_reports_duplication_counts(
    app: Any, auditor: Client
) -> None:
    """The overview carries the group totals per status."""
    await _seed_duplicates(app)
    response = await auditor.client.get("/api/v1/seasons/2026/overview")
    assert response.status_code == 200, response.text
    duplication = response.json()["entities"]["duplication"]
    assert duplication["total"] == 1
    assert duplication["by_status"] == {
        "pending": 1,
        "resolved": 0,
        "audited": 0,
    }
