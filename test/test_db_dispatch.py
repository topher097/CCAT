"""Dispatch work-list, draft, send-attempt, recovery, and API tests.

Every test owns a migrated SQLite database and packet artifacts under
``tmp_path``. Final stores are seeded through ``migrate`` ownership, and
mail is always delivered to a fake transport rather than Google.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from conftest import load_test_cfg
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from ccat.api import create_app
from ccat.api.routes import dispatch as dispatch_routes
from ccat.config import StorageSpec, WorkerSpec
from ccat.db.engine import (
    async_session_factory,
    create_async_engine,
    create_sync_engine,
    run_migrations,
)
from ccat.db.models import Season, SecurityAuditEvent, SponsorSendAttempt
from ccat.db.seed import seed_bootstrap
from ccat.db.uow import UnitOfWork
from ccat.services import db_dispatch
from ccat.services.ports import SendOutcome

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI
    from httpx import Response

    from ccat.config import Config
    from ccat.services.ports import SendRequest

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/seasons/2026/dispatch"
SUBJECT = "Your 2026 Angel packet"
BCC = ["archive@example.com", "audit@example.com"]
PACKET_BYTES = b"combined child information PDF"
INSTRUCTIONS_BYTES = b"sponsor instructions PDF"


# ---------------------------------------------------------------------------
# App, clients, and transport fakes.
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path, *, mail_enabled: bool = True) -> Config:
    """Build an isolated DB config with deterministic dispatch settings.

    Args:
        tmp_path: The test's temporary directory.
        mail_enabled: Whether the sending account is configured.

    Returns:
        The test configuration.
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
        worker=WorkerSpec(poll_interval_seconds=0.01),
    )
    return cfg.model_copy(
        update={
            "dispatch": cfg.dispatch.model_copy(
                update={
                    "enabled": mail_enabled,
                    "sender_email": "dispatch@example.com",
                    "sender_name": "Dispatch Team",
                    "reply_to": "replies@example.com",
                    "client_file": str(tmp_path / "client.json"),
                    "token_file": str(tmp_path / "gmail-token.json"),
                }
            ),
            "sponsor_packets": cfg.sponsor_packets.model_copy(
                update={"bcc_emails": list(BCC)}
            ),
        }
    )


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    """Provide one migrated app with bootstrap users and isolated storage.

    Args:
        tmp_path: The test's temporary directory.

    Yields:
        The configured FastAPI application.
    """
    cfg = _cfg(tmp_path)
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
        await application.state.uow.engine.dispose()


class Client:
    """One authenticated HTTP client and its session CSRF token."""

    def __init__(self, app: FastAPI) -> None:
        """Wrap an ASGI client.

        Args:
            app: The application under test.
        """
        self.client = AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        )
        self.csrf = ""

    async def login(self, username: str) -> None:
        """Log in a development user and read its CSRF token.

        Args:
            username: The bootstrap user's name.
        """
        response = await self.client.post(
            "/api/v1/auth/login", json={"username": username}
        )
        assert response.status_code == 204, response.text
        me = await self.client.get("/api/v1/auth/me")
        assert me.status_code == 200, me.text
        self.csrf = me.json()["csrf_token"]

    def headers(self) -> dict[str, str]:
        """Return the mutation CSRF header.

        Returns:
            The session's CSRF header.
        """
        return {"X-CCAT-CSRF": self.csrf}

    async def close(self) -> None:
        """Close the HTTP client."""
        await self.client.aclose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[Client]:
    """Provide a coordinator client.

    Args:
        app: The application under test.

    Yields:
        The logged-in coordinator (``lmboswell``).
    """
    wrapper = Client(app)
    await wrapper.login("lmboswell")
    try:
        yield wrapper
    finally:
        await wrapper.close()


@pytest.fixture
async def admin_client(app: FastAPI) -> AsyncIterator[Client]:
    """Provide an admin client.

    Args:
        app: The application under test.

    Yields:
        The logged-in admin (``cmendres``).
    """
    wrapper = Client(app)
    await wrapper.login("cmendres")
    try:
        yield wrapper
    finally:
        await wrapper.close()


class FakeTransport:
    """A recording mail transport with programmable outcomes."""

    def __init__(
        self,
        outcomes: list[SendOutcome] | None = None,
        *,
        error: Exception | None = None,
        block: bool = False,
    ) -> None:
        """Configure sequential outcomes, an exception, or a blocked send.

        Args:
            outcomes: Outcomes returned by successive calls.
            error: Exception raised after recording the request.
            block: Whether the first call waits for ``release``.
        """
        self.outcomes = outcomes or [SendOutcome("sent", "provider-123", "")]
        self.error = error
        self.block = block
        self.requests: list[SendRequest] = []
        self.started = threading.Event()
        self.release = threading.Event()

    def send(self, request: SendRequest) -> SendOutcome:
        """Record one request and produce the configured outcome.

        Args:
            request: The outgoing message.

        Returns:
            The configured provider outcome.

        Raises:
            Exception: The configured transport error, or a test timeout
                when a blocked fake is never released.
        """
        self.requests.append(request)
        if self.block:
            self.started.set()
            if not self.release.wait(5):
                raise RuntimeError("test did not release the blocked transport")
        if self.error is not None:
            raise self.error
        index = min(len(self.requests) - 1, len(self.outcomes) - 1)
        return self.outcomes[index]


def _install_transport(
    monkeypatch: pytest.MonkeyPatch, transport: FakeTransport
) -> None:
    """Make the send route use a fake transport.

    Args:
        monkeypatch: Pytest's patch fixture.
        transport: The fake returned by the route's transport factory.
    """
    monkeypatch.setattr(dispatch_routes, "_transport", lambda _cfg: transport)


# ---------------------------------------------------------------------------
# Final-store and packet fixtures.
# ---------------------------------------------------------------------------


def child(record_id: str, **overrides: Any) -> dict[str, Any]:
    """Build one minimal final-child row.

    Args:
        record_id: The child's stable record id.
        overrides: Final-column overrides.

    Returns:
        The child row.
    """
    row: dict[str, Any] = {
        "zite_record_id": record_id,
        "family_id": 1,
        "angel_id": record_id.upper(),
        "first_name": "Kiddo",
        "gender": "Male",
        "dob": "2018-01-01",
        "age_at_xmas": 8,
        "status": "audited",
        "ignore": False,
        "potential_duplicate": False,
        "generated_info_sheet": False,
        "need_1": "coat",
        "desire_1": "lego",
        "interests": "soccer",
        "other_notes": "allergy",
        "shoe_size": "5",
        "shoe_size_type": "Youth",
        "shirt_size_letter": "M",
    }
    row.update(overrides)
    if "age_at_xmas" in overrides:
        row["dob"] = f"{2026 - int(overrides['age_at_xmas'])}-01-01"
    return row


def family(family_id: int, **overrides: Any) -> dict[str, Any]:
    """Build one minimal final-family row.

    Args:
        family_id: The family's stable id.
        overrides: Final-column overrides.

    Returns:
        The family row.
    """
    row: dict[str, Any] = {
        "family_id": family_id,
        "first_name": "Jane",
        "last_name": "Doe",
        "primary_email": f"fam{family_id}@example.com",
        "city": "Hancock",
        "zip_code": "49930",
        "gift_pickup_location": "Hancock",
        "status": "audited",
        "ignore": False,
    }
    row.update(overrides)
    return row


def sponsor(sponsor_id: int, **overrides: Any) -> dict[str, Any]:
    """Build one minimal final-sponsor row.

    Args:
        sponsor_id: The sponsor's stable id.
        overrides: Final-column overrides.

    Returns:
        The sponsor row.
    """
    row: dict[str, Any] = {
        "sponsor_id": sponsor_id,
        "first_name": "Mary",
        "last_name": f"Sponsor{sponsor_id}",
        "email": f"sponsor{sponsor_id}@example.com",
        "sponsor_type": "Individual",
        "num_children": 5,
        "age_preference": "Any Age",
        "gender_preference": "Any Gender",
        "dropoff_location": "Hancock",
        "pref_age_min": 0,
        "pref_age_max": 18,
        "status": "audited",
        "ignore": False,
        "num_angels_assigned": 0,
    }
    row.update(overrides)
    return row


def _derive_counts(
    children: list[dict[str, Any]],
    families: list[dict[str, Any]],
    sponsors: list[dict[str, Any]],
) -> None:
    """Make seeded family and sponsor summaries match the children.

    Args:
        children: The child rows.
        families: The family rows, updated in place.
        sponsors: The sponsor rows, updated in place.
    """
    per_family: dict[Any, set[str]] = {}
    per_sponsor: dict[Any, int] = {}
    for row in children:
        if row.get("ignore"):
            continue
        letter = row.get("angel_id") or f"row:{row.get('zite_record_id')}"
        per_family.setdefault(row.get("family_id"), set()).add(str(letter))
        if row.get("assigned_sponsor_id") is not None:
            sponsor_id = row["assigned_sponsor_id"]
            per_sponsor[sponsor_id] = per_sponsor.get(sponsor_id, 0) + 1
    for row in families:
        row["num_angels_in_family"] = len(
            per_family.get(row.get("family_id"), set())
        )
    for row in sponsors:
        row["num_angels_assigned"] = per_sponsor.get(row.get("sponsor_id"), 0)


async def _seed(
    app: FastAPI,
    *,
    children: list[dict[str, Any]],
    families: list[dict[str, Any]],
    sponsors: list[dict[str, Any]],
) -> None:
    """Seed all final stores through migrate ownership.

    Args:
        app: The application whose database is seeded.
        children: Complete final-child store.
        families: Complete final-family store.
        sponsors: Complete final-sponsor store.
    """
    _derive_counts(children, families, sponsors)
    cfg = app.state.cfg
    engine = create_async_engine(cfg.storage.database_url)
    try:
        async with UnitOfWork(engine).store(cfg, "migration") as repo:
            for entity, rows in (
                ("child", children),
                ("family", families),
                ("sponsor", sponsors),
            ):
                await repo.commit_finals("migrate", entity, rows, "migration")
    finally:
        await engine.dispose()


def _write_packet(
    app: FastAPI,
    sponsor_id: int,
    children: list[dict[str, Any]],
    *,
    first_name: str = "Mary",
    email: str | None = None,
    subject: str = SUBJECT,
) -> dict[str, Any]:
    """Write one honest packet directory and manifest without LibreOffice.

    Args:
        app: The application owning the artifact root.
        sponsor_id: The sponsor whose packet is written.
        children: The assigned children represented by the packet.
        first_name: The sponsor greeting name.
        email: The sponsor address stored in the manifest.
        subject: The manifest's email subject.

    Returns:
        The manifest payload, including hashes of the bytes written.
    """
    if email is None:
        email = f"sponsor{sponsor_id}@example.com"
    cfg = app.state.cfg
    packet_dir = (
        Path(cfg.storage.artifact_root) / "sponsor_packets" / str(sponsor_id)
    )
    packet_dir.mkdir(parents=True, exist_ok=True)
    payloads = {
        "email_html": (
            f"sponsor_packet_{sponsor_id}.html",
            b"<html>draft</html>",
        ),
        "packet_pdf": (
            f"Sponsor {sponsor_id} - Child Information 2026.pdf",
            PACKET_BYTES,
        ),
        "instructions_pdf": (
            "Sponsor Instructions 2026.pdf",
            INSTRUCTIONS_BYTES,
        ),
    }
    items: list[dict[str, Any]] = []
    for role, (filename, payload) in payloads.items():
        path = packet_dir / filename
        path.write_bytes(payload)
        items.append(
            {
                "role": role,
                "filename": filename,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
        )
    manifest_children = [
        {
            "child_id": row["zite_record_id"],
            "angel_id": row["angel_id"],
        }
        for row in children
    ]
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "sponsor_id": sponsor_id,
                "children": manifest_children,
                "items": {item["role"]: item["sha256"] for item in items},
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    manifest: dict[str, Any] = {
        "build_id": f"build-{sponsor_id}-1",
        "sponsor_id": sponsor_id,
        "season": "2026",
        "subject": subject,
        "sponsor": {"first_name": first_name, "email": email},
        "children": manifest_children,
        "angels": [
            {
                "angel_id": row["angel_id"],
                "child_id": row["zite_record_id"],
                "gender": row.get("gender"),
                "age": row.get("age_at_xmas"),
                "dropoff": "Hancock",
            }
            for row in children
        ],
        "items": items,
        "fingerprint": fingerprint,
    }
    (packet_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest


async def _seed_ready(
    app: FastAPI, *, sponsor_overrides: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Seed one full sponsor, one generated sheet, and its packet manifest.

    Args:
        app: The application whose stores are seeded.
        sponsor_overrides: Changes to the default ready sponsor.

    Returns:
        The ready packet manifest.
    """
    child_row = child(
        "child-1",
        angel_id="1A",
        family_id=1,
        assigned_sponsor_id=1,
        generated_info_sheet=True,
    )
    overrides = {"num_children": 1}
    overrides.update(sponsor_overrides or {})
    sponsor_row = sponsor(1, **overrides)
    await _seed(
        app,
        children=[child_row],
        families=[family(1)],
        sponsors=[sponsor_row],
    )
    return _write_packet(
        app,
        1,
        [child_row],
        first_name=str(sponsor_row["first_name"]),
        email=sponsor_row["email"],
    )


# ---------------------------------------------------------------------------
# API and database helpers.
# ---------------------------------------------------------------------------


async def _create_draft(client: Client, sponsor_id: int = 1) -> dict[str, Any]:
    """Draft one sponsor through the API and return its draft DTO.

    Args:
        client: A coordinator or admin client.
        sponsor_id: The sponsor to draft.

    Returns:
        The current draft response body.
    """
    response = await client.client.post(
        f"{BASE}/drafts",
        json={"sponsor_ids": [sponsor_id]},
        headers=client.headers(),
    )
    assert response.status_code == 200, response.text
    response = await client.client.get(f"{BASE}/sponsors/{sponsor_id}/draft")
    assert response.status_code == 200, response.text
    return response.json()


async def _send(
    client: Client,
    *,
    sponsor_id: int = 1,
    version: int = 1,
    subject: str | None = None,
    note: str | None = None,
) -> Response:
    """Post one sponsor send request.

    Args:
        client: The authenticated client.
        sponsor_id: The sponsor whose draft is sent.
        version: The expected draft version.
        subject: Optional final subject edit.
        note: Optional final note edit.

    Returns:
        The HTTP response.
    """
    return await client.client.post(
        f"{BASE}/sponsors/{sponsor_id}/send",
        json={"version": version, "subject": subject, "note": note},
        headers=client.headers(),
    )


async def _read(app: FastAPI, entity: str) -> list[dict[str, Any]]:
    """Read one complete final store.

    Args:
        app: The application owning the store.
        entity: ``child``, ``family``, or ``sponsor``.

    Returns:
        The persistent final rows.
    """
    cfg = app.state.cfg
    engine = create_async_engine(cfg.storage.database_url)
    try:
        async with UnitOfWork(engine).store(cfg, "test") as repo:
            return await repo.final_rows(entity)
    finally:
        await engine.dispose()


async def _store(
    app: FastAPI, entity: str, key: str, value: Any
) -> dict[str, Any]:
    """Read one final row by key.

    Args:
        app: The application owning the store.
        entity: The entity name.
        key: The key column.
        value: The key value.

    Returns:
        The matching final row.
    """
    rows = await _read(app, entity)
    return next(row for row in rows if row.get(key) == value)


async def _attempt_rows(app: FastAPI) -> list[SponsorSendAttempt]:
    """Read all send attempts, oldest first.

    Args:
        app: The application owning the database.

    Returns:
        The send-attempt ORM rows.
    """
    cfg = app.state.cfg
    engine = create_async_engine(cfg.storage.database_url)
    factory = async_session_factory(engine)
    try:
        async with factory() as session:
            result = await session.scalars(
                select(SponsorSendAttempt).order_by(
                    SponsorSendAttempt.started_at, SponsorSendAttempt.id
                )
            )
            return list(result)
    finally:
        await engine.dispose()


async def _audit_rows(app: FastAPI) -> list[SecurityAuditEvent]:
    """Read every security audit event.

    Args:
        app: The application owning the database.

    Returns:
        Audit events in insertion order.
    """
    cfg = app.state.cfg
    engine = create_async_engine(cfg.storage.database_url)
    factory = async_session_factory(engine)
    try:
        async with factory() as session:
            result = await session.scalars(
                select(SecurityAuditEvent).order_by(SecurityAuditEvent.id)
            )
            return list(result)
    finally:
        await engine.dispose()


async def _insert_attempt(
    app: FastAPI,
    sponsor_id: int,
    *,
    state: str,
    started_at: datetime,
    finished_at: datetime | None = None,
) -> str:
    """Insert an attempt fixture directly for list and recovery scenarios.

    Args:
        app: The application owning the database.
        sponsor_id: The attempt's sponsor.
        state: Its lifecycle state.
        started_at: When the attempt began.
        finished_at: When it ended, if terminal.

    Returns:
        The inserted attempt id.
    """
    cfg = app.state.cfg
    engine = create_async_engine(cfg.storage.database_url)
    factory = async_session_factory(engine)
    attempt_id = f"{state}-{sponsor_id}-attempt"
    try:
        async with factory() as session:
            season = await session.scalar(
                select(Season).where(Season.name == cfg.season)
            )
            assert season is not None
            session.add(
                SponsorSendAttempt(
                    id=attempt_id,
                    season_id=season.id,
                    sponsor_id=sponsor_id,
                    state=state,
                    started_at=started_at,
                    finished_at=finished_at,
                )
            )
            await session.commit()
        return attempt_id
    finally:
        await engine.dispose()


async def _recover(app: FastAPI) -> list[str]:
    """Run the stale-attempt recovery sweep against the app's store.

    Args:
        app: The application owning the database.

    Returns:
        The recovered attempt ids.
    """
    cfg = app.state.cfg
    engine = create_async_engine(cfg.storage.database_url)
    try:
        async with UnitOfWork(engine).store(cfg, "test") as repo:
            return await db_dispatch.recover_stale_attempts(repo, "test")
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Work list and blockers.
# ---------------------------------------------------------------------------


async def test_work_list_counts_packets_blockers_and_omits_empty_sponsors(
    app: FastAPI, client: Client
) -> None:
    """The list summarizes assigned sponsors and every visible blocker.

    Args:
        app: The application under test.
        client: The coordinator client.
    """
    children = [
        child(
            "child-1",
            angel_id="1A",
            family_id=1,
            assigned_sponsor_id=1,
            generated_info_sheet=True,
        ),
        child(
            "child-2",
            angel_id="1B",
            family_id=2,
            assigned_sponsor_id=1,
            generated_info_sheet=False,
        ),
        child(
            "child-3",
            angel_id="2A",
            family_id=3,
            assigned_sponsor_id=2,
            generated_info_sheet=True,
        ),
    ]
    dispatched_at = "2026-11-20T12:00:00+00:00"
    await _seed(
        app,
        children=children,
        families=[family(1), family(2), family(3)],
        sponsors=[
            sponsor(1, email=None, num_children=3),
            sponsor(2, num_children=1, packets_dispatched_at=dispatched_at),
            sponsor(3, num_children=0),
        ],
    )
    _write_packet(app, 2, [children[2]])
    now = datetime.now(UTC)
    await _insert_attempt(app, 2, state="sent", started_at=now, finished_at=now)
    cfg = app.state.cfg
    app.state.cfg = cfg.model_copy(
        update={"dispatch": cfg.dispatch.model_copy(update={"enabled": False})}
    )

    response = await client.client.get(BASE)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mail_configured"] is False
    assert [row["sponsor_id"] for row in body["rows"]] == [1, 2]
    first, second = body["rows"]
    assert first["assigned_count"] == 2
    assert first["assigned_angel_ids"] == ["1A", "1B"]
    assert first["sheets_ready_count"] == 1
    assert first["sheets_total"] == 2
    assert first["missing_sheet_angel_ids"] == ["1B"]
    assert first["packet_status"] == "missing"
    assert first["blockers"] == [
        "no_email",
        "missing_sheets",
        "slots_not_filled",
        "manifest_missing",
        "mail_not_configured",
    ]
    assert second["packet_status"] == "ready"
    assert len(second["attachments"]) == 2
    assert second["email_status"] == "sent"
    assert second["email_sent_at"] is not None
    assert second["packets_dispatched"] is True
    assert second["packets_dispatched_at"] == dispatched_at
    assert second["blockers"] == ["already_sent", "mail_not_configured"]
    assert body["totals"] == {
        "sponsors": 2,
        "assigned_children": 3,
        "sheets_ready": 2,
        "sheets_missing": 1,
        "packets_ready": 1,
        "packets_missing": 1,
        "dispatched": 1,
    }


# ---------------------------------------------------------------------------
# Draft rendering and optimistic editing.
# ---------------------------------------------------------------------------


async def test_drafting_renders_manifest_without_sending_or_stamping(
    app: FastAPI,
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A draft carries the manifest body, subject, and hashed PDFs only.

    Args:
        app: The application under test.
        client: The coordinator client.
        monkeypatch: Pytest's patch fixture.
    """
    manifest = await _seed_ready(
        app, sponsor_overrides={"first_name": "Martha"}
    )
    transport = FakeTransport()
    _install_transport(monkeypatch, transport)

    response = await client.client.post(
        f"{BASE}/drafts", json={"sponsor_ids": [1]}, headers=client.headers()
    )

    assert response.status_code == 200, response.text
    row = response.json()["rows"][0]
    assert row["email_status"] == "draft_ready"
    assert row["draft_version"] == 1
    assert row["draft_subject"] == SUBJECT
    assert row["blockers"] == []
    draft = (await client.client.get(f"{BASE}/sponsors/1/draft")).json()
    assert draft["version"] == 1
    assert draft["subject"] == SUBJECT
    assert draft["to_email"] == "sponsor1@example.com"
    assert draft["bcc_emails"] == BCC
    assert "Dear Martha," in draft["body_html"]
    assert "<table" in draft["body_html"]
    assert ">1A</strong>" in draft["body_html"]
    assert draft["build_id"] == manifest["build_id"]
    expected_items = [
        {
            "role": item["role"],
            "filename": item["filename"],
            "sha256": item["sha256"],
        }
        for item in manifest["items"]
        if item["role"] != "email_html"
    ]
    assert [
        {
            "role": item["role"],
            "filename": item["filename"],
            "sha256": item["sha256"],
        }
        for item in draft["attachments"]
    ] == expected_items
    assert transport.requests == []
    assert await _attempt_rows(app) == []
    stored = await _store(app, "sponsor", "sponsor_id", 1)
    assert stored["packets_dispatched_at"] is None


async def test_draft_edit_uses_if_match_and_rejects_stale_version(
    app: FastAPI, client: Client
) -> None:
    """An edit bumps the draft version and a stale editor receives 412.

    Args:
        app: The application under test.
        client: The coordinator client.
    """
    await _seed_ready(app)
    await _create_draft(client)

    response = await client.client.patch(
        f"{BASE}/sponsors/1/draft",
        json={"subject": "Edited subject", "note": "Bring gifts Friday"},
        headers=client.headers() | {"If-Match": "1"},
    )

    assert response.status_code == 200, response.text
    edited = response.json()
    assert edited["version"] == 2
    assert edited["subject"] == "Edited subject"
    assert edited["note"] == "Bring gifts Friday"
    assert edited["email_status"] == "draft_edited"
    assert "Bring gifts Friday" in edited["body_html"]

    stale = await client.client.patch(
        f"{BASE}/sponsors/1/draft",
        json={"subject": "Stale subject"},
        headers=client.headers() | {"If-Match": "1"},
    )
    assert stale.status_code == 412, stale.text
    assert stale.json()["detail"]["code"] == "stale_version"
    current = await client.client.get(f"{BASE}/sponsors/1/draft")
    assert current.json()["version"] == 2
    assert current.json()["subject"] == "Edited subject"


# ---------------------------------------------------------------------------
# Successful send and preflight refusals.
# ---------------------------------------------------------------------------


async def test_admin_send_records_attempt_stamp_message_and_audit(
    app: FastAPI,
    client: Client,
    admin_client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful send transmits the draft and records every outcome.

    Args:
        app: The application under test.
        client: The coordinator who drafts.
        admin_client: The admin who sends.
        monkeypatch: Pytest's patch fixture.
    """
    await _seed_ready(app, sponsor_overrides={"first_name": "Martha"})
    await _create_draft(client)
    transport = FakeTransport([SendOutcome("sent", "gmail-id-1", "")])
    _install_transport(monkeypatch, transport)

    response = await _send(
        admin_client,
        subject="Final packet subject",
        note="Please deliver by Friday",
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["state"] == "sent"
    assert result["draft_version"] == 2
    assert result["sent_at"] is not None
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.to == "sponsor1@example.com"
    assert request.bcc == tuple(BCC)
    assert request.sender == "dispatch@example.com"
    assert request.subject == "Final packet subject"
    assert request.message_id == result["message_id"]
    assert "Dear Martha," in request.html_body
    assert "Please deliver by Friday" in request.html_body
    assert [item.filename for item in request.attachments] == [
        "Sponsor 1 - Child Information 2026.pdf",
        "Sponsor Instructions 2026.pdf",
    ]
    assert [item.payload for item in request.attachments] == [
        PACKET_BYTES,
        INSTRUCTIONS_BYTES,
    ]

    attempts = await _attempt_rows(app)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt.id == result["attempt_id"]
    assert attempt.state == "sent"
    assert attempt.provider_message_id == "gmail-id-1"
    assert attempt.to_email == "sponsor1@example.com"
    assert attempt.subject == "Final packet subject"
    assert attempt.finished_at is not None
    stored = await _store(app, "sponsor", "sponsor_id", 1)
    assert stored["packets_dispatched_at"] is not None

    events = [
        event
        for event in await _audit_rows(app)
        if event.action == "sponsor_packet_send"
    ]
    assert len(events) == 1
    details = events[0].details or {}
    assert details["to"] == "sponsor1@example.com"
    assert details["bcc"] == BCC
    assert details["state"] == "sent"
    assert details["attachments"] == [
        {
            "role": "packet_pdf",
            "filename": "Sponsor 1 - Child Information 2026.pdf",
            "sha256": hashlib.sha256(PACKET_BYTES).hexdigest(),
        },
        {
            "role": "instructions_pdf",
            "filename": "Sponsor Instructions 2026.pdf",
            "sha256": hashlib.sha256(INSTRUCTIONS_BYTES).hexdigest(),
        },
    ]


async def test_send_rejects_stale_draft_before_calling_transport(
    app: FastAPI,
    client: Client,
    admin_client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale send is a 412 and opens no attempt.

    Args:
        app: The application under test.
        client: The coordinator who drafts.
        admin_client: The admin who attempts to send.
        monkeypatch: Pytest's patch fixture.
    """
    await _seed_ready(app)
    await _create_draft(client)
    transport = FakeTransport()
    _install_transport(monkeypatch, transport)

    response = await _send(admin_client, version=99)

    assert response.status_code == 412, response.text
    assert response.json()["detail"]["code"] == "stale_version"
    assert transport.requests == []
    assert await _attempt_rows(app) == []
    stored = await _store(app, "sponsor", "sponsor_id", 1)
    assert stored["packets_dispatched_at"] is None


@pytest.mark.parametrize(
    "case", ["no_email", "no_manifest", "assignment_changed"]
)
async def test_send_preflight_conflicts_do_not_call_transport(
    app: FastAPI,
    client: Client,
    admin_client: Client,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """Missing recipients and stale packet inputs are send conflicts.

    Args:
        app: The application under test.
        client: The coordinator who drafts.
        admin_client: The admin who attempts to send.
        monkeypatch: Pytest's patch fixture.
        case: The preflight violation being tested.
    """
    await _seed_ready(
        app, sponsor_overrides={"email": None} if case == "no_email" else None
    )
    await _create_draft(client)
    cfg = app.state.cfg
    packet_dir = Path(cfg.storage.artifact_root) / "sponsor_packets" / "1"
    if case == "no_manifest":
        (packet_dir / "manifest.json").unlink()
    elif case == "assignment_changed":
        moved_child = child(
            "child-1",
            angel_id="1A",
            family_id=1,
            assigned_sponsor_id=2,
            generated_info_sheet=True,
        )
        await _seed(
            app,
            children=[moved_child],
            families=[family(1)],
            sponsors=[sponsor(1, num_children=1), sponsor(2, num_children=1)],
        )
    transport = FakeTransport()
    _install_transport(monkeypatch, transport)

    response = await _send(admin_client)

    assert response.status_code == 409, response.text
    expected_blocker = {
        "no_email": "no_email",
        "no_manifest": "manifest_missing",
        "assignment_changed": "packet_changed",
    }[case]
    assert response.json()["detail"]["blocker"] == expected_blocker
    assert transport.requests == []
    assert await _attempt_rows(app) == []
    stored = await _store(app, "sponsor", "sponsor_id", 1)
    assert stored["packets_dispatched_at"] is None


@pytest.mark.parametrize("case", ["missing", "changed"])
async def test_missing_or_changed_attachment_is_conflict_and_nothing_is_sent(
    app: FastAPI,
    client: Client,
    admin_client: Client,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """A packet byte mismatch prevents both provider calls and attempts.

    Args:
        app: The application under test.
        client: The coordinator who drafts.
        admin_client: The admin who attempts to send.
        monkeypatch: Pytest's patch fixture.
        case: Whether the PDF is absent or has different bytes.
    """
    await _seed_ready(app)
    await _create_draft(client)
    packet = (
        Path(app.state.cfg.storage.artifact_root)
        / "sponsor_packets"
        / "1"
        / "Sponsor 1 - Child Information 2026.pdf"
    )
    if case == "missing":
        packet.unlink()
    else:
        packet.write_bytes(b"replacement bytes")
    transport = FakeTransport()
    _install_transport(monkeypatch, transport)

    response = await _send(admin_client)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["blocker"] == "packet_changed"
    assert transport.requests == []
    assert await _attempt_rows(app) == []
    stored = await _store(app, "sponsor", "sponsor_id", 1)
    assert stored["packets_dispatched_at"] is None


async def test_send_requires_configured_mail(
    app: FastAPI,
    client: Client,
    admin_client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabled mail maps to 422 without invoking a provider.

    Args:
        app: The application under test.
        client: The coordinator who drafts.
        admin_client: The admin who attempts to send.
        monkeypatch: Pytest's patch fixture.
    """
    await _seed_ready(app)
    await _create_draft(client)
    cfg = app.state.cfg
    app.state.cfg = cfg.model_copy(
        update={"dispatch": cfg.dispatch.model_copy(update={"enabled": False})}
    )
    transport = FakeTransport()
    _install_transport(monkeypatch, transport)

    response = await _send(admin_client)

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "mail_not_configured"
    assert transport.requests == []
    assert await _attempt_rows(app) == []


# ---------------------------------------------------------------------------
# Attempt lifecycle and recovery.
# ---------------------------------------------------------------------------


async def test_timeout_leaves_attempt_unknown_and_sponsor_unstamped(
    app: FastAPI,
    client: Client,
    admin_client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raised timeout is unknown, never failed or a successful dispatch.

    Args:
        app: The application under test.
        client: The coordinator who drafts.
        admin_client: The admin who sends.
        monkeypatch: Pytest's patch fixture.
    """
    await _seed_ready(app)
    await _create_draft(client)
    transport = FakeTransport(error=TimeoutError("provider timed out"))
    _install_transport(monkeypatch, transport)

    response = await _send(admin_client)

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "unknown"
    attempts = await _attempt_rows(app)
    assert len(attempts) == 1
    assert attempts[0].state == "unknown"
    assert attempts[0].state != "failed"
    assert attempts[0].error
    stored = await _store(app, "sponsor", "sponsor_id", 1)
    assert stored["packets_dispatched_at"] is None


async def test_rejected_send_is_failed_and_retryable(
    app: FastAPI,
    client: Client,
    admin_client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal records failure and a fresh-version retry may succeed.

    Args:
        app: The application under test.
        client: The coordinator who drafts.
        admin_client: The admin who sends twice.
        monkeypatch: Pytest's patch fixture.
    """
    await _seed_ready(app)
    await _create_draft(client)
    transport = FakeTransport(
        [
            SendOutcome("rejected", "", "provider said no"),
            SendOutcome("sent", "retry-provider-id", ""),
        ]
    )
    _install_transport(monkeypatch, transport)

    rejected = await _send(admin_client)

    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["state"] == "failed"
    first_attempts = await _attempt_rows(app)
    assert len(first_attempts) == 1
    assert first_attempts[0].state == "failed"
    assert first_attempts[0].error == "provider said no"
    stored = await _store(app, "sponsor", "sponsor_id", 1)
    assert stored["packets_dispatched_at"] is None

    retry = await _send(admin_client, version=rejected.json()["draft_version"])
    assert retry.status_code == 200, retry.text
    assert retry.json()["state"] == "sent"
    attempts = await _attempt_rows(app)
    assert len(attempts) == 2
    by_id = {attempt.id: attempt for attempt in attempts}
    assert by_id[rejected.json()["attempt_id"]].state == "failed"
    assert by_id[retry.json()["attempt_id"]].state == "sent"
    assert by_id[retry.json()["attempt_id"]].provider_message_id == (
        "retry-provider-id"
    )
    assert len(transport.requests) == 2


async def test_second_send_while_one_is_in_flight_is_refused(
    app: FastAPI,
    client: Client,
    admin_client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The active-attempt reservation rejects a concurrent second send.

    Args:
        app: The application under test.
        client: The coordinator who drafts and reads versions.
        admin_client: The admin issuing concurrent sends.
        monkeypatch: Pytest's patch fixture.
    """
    await _seed_ready(app)
    await _create_draft(client)
    transport = FakeTransport(block=True)
    _install_transport(monkeypatch, transport)
    first_task = asyncio.create_task(_send(admin_client))

    try:
        started = await asyncio.to_thread(transport.started.wait, 2)
        assert started, "first send never reached the transport"
        current = await client.client.get(f"{BASE}/sponsors/1/draft")
        assert current.status_code == 200, current.text
        assert current.json()["email_status"] == "sending"
        second = await _send(admin_client, version=current.json()["version"])
    finally:
        transport.release.set()
        first = await first_task

    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["code"] == "conflict"
    assert second.json()["detail"]["blocker"] == "send_in_flight"
    assert len(transport.requests) == 1
    attempts = await _attempt_rows(app)
    assert len(attempts) == 1
    assert attempts[0].state == "sent"


async def test_recovery_turns_stale_sending_attempt_into_unknown(
    app: FastAPI,
) -> None:
    """The recovery sweep closes an abandoned send as unknown.

    Args:
        app: The application under test.
    """
    await _seed_ready(app)
    stale = datetime.now(UTC) - timedelta(
        seconds=db_dispatch.STALE_ATTEMPT_SECONDS + 60
    )
    attempt_id = await _insert_attempt(
        app, 1, state="sending", started_at=stale
    )

    recovered = await _recover(app)

    assert recovered == [attempt_id]
    attempts = await _attempt_rows(app)
    assert len(attempts) == 1
    assert attempts[0].state == "unknown"
    assert attempts[0].error == "the send did not report an outcome"
    assert attempts[0].finished_at is not None
    stored = await _store(app, "sponsor", "sponsor_id", 1)
    assert stored["packets_dispatched_at"] is None


# ---------------------------------------------------------------------------
# Route surface, authentication, roles, and CSRF.
# ---------------------------------------------------------------------------


async def test_dispatch_routes_auth_roles_and_csrf(
    app: FastAPI,
    client: Client,
    admin_client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All six operations enforce their documented auth and CSRF rules.

    Args:
        app: The application under test.
        client: The coordinator client.
        admin_client: The admin client.
        monkeypatch: Pytest's patch fixture.
    """
    await _seed_ready(app)
    transport = FakeTransport()
    _install_transport(monkeypatch, transport)
    root = "/api/v1/seasons/{season}/dispatch"
    paths = app.openapi()["paths"]
    assert set(paths[root]) == {"get"}
    assert set(paths[f"{root}/children"]) == {"get"}
    assert set(paths[f"{root}/drafts"]) == {"post"}
    assert set(paths[f"{root}/sponsors/{{sponsor_id}}/draft"]) == {
        "get",
        "patch",
    }
    assert set(paths[f"{root}/sponsors/{{sponsor_id}}/send"]) == {"post"}

    listing = await client.client.get(BASE)
    assert listing.status_code == 200
    assert listing.json()["can_send"] is False
    children = await client.client.get(f"{BASE}/children")
    assert children.status_code == 200
    assert [row["angel_id"] for row in children.json()["rows"]] == ["1A"]
    admin_listing = await admin_client.client.get(BASE)
    assert admin_listing.status_code == 200
    assert admin_listing.json()["can_send"] is True

    missing_csrf = await client.client.post(
        f"{BASE}/drafts", json={"sponsor_ids": [1]}
    )
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["detail"]["code"] == "csrf"
    await _create_draft(client)
    assert (
        await client.client.get(f"{BASE}/sponsors/1/draft")
    ).status_code == 200

    patch_without_csrf = await client.client.patch(
        f"{BASE}/sponsors/1/draft",
        json={"note": "no CSRF"},
        headers={"If-Match": "1"},
    )
    assert patch_without_csrf.status_code == 403
    send_without_csrf = await admin_client.client.post(
        f"{BASE}/sponsors/1/send", json={"version": 1}
    )
    assert send_without_csrf.status_code == 403
    coordinator_send = await _send(client)
    assert coordinator_send.status_code == 403
    assert coordinator_send.json()["detail"]["code"] == "forbidden"
    assert transport.requests == []
    # Seed has no auditor-only/read-only user. Thus a forbidden-draft case
    # cannot use dev auth; lmboswell covers the important send restriction.
