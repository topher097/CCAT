"""Manual matching: the pool, assignments, releases and holds (§12.1).

Every test builds its own migrated SQLite store under ``tmp_path`` and
seeds the final stores through the ``migrate`` ownership path, then
exercises the API exactly as the SPA does.
"""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from conftest import load_test_cfg
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from ccat.api import create_app
from ccat.config import Config, StorageSpec, WorkerSpec
from ccat.db.engine import (
    async_session_factory,
    create_async_engine,
    run_migrations,
)
from ccat.db.models import FamilyHold, Job, Season, SecurityAuditEvent
from ccat.db.uow import UnitOfWork

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/seasons/2026/manual-match"


# ---------------------------------------------------------------------------
# Fixtures and helpers.
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path) -> Config:
    """Test config: temp DB/artifacts, dev auth."""
    return load_test_cfg(
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


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[Any]:
    """One migrated app instance (dev auth mode)."""
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
        await application.state.uow.engine.dispose()


class Client:
    """One authenticated test client with its CSRF token."""

    def __init__(self, app: Any) -> None:
        """Wrap one AsyncClient.

        Args:
            app: The FastAPI app.
        """
        self.client = AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        )
        self.csrf = ""

    async def login(self, username: str = "lmboswell") -> None:
        """Log in and cache the CSRF token."""
        response = await self.client.post(
            "/api/v1/auth/login", json={"username": username}
        )
        assert response.status_code == 204, response.text
        me = await self.client.get("/api/v1/auth/me")
        self.csrf = me.json()["csrf_token"]

    def headers(self) -> dict[str, str]:
        """Return the CSRF header."""
        return {"X-CCAT-CSRF": self.csrf}

    async def close(self) -> None:
        """Close the underlying client."""
        await self.client.aclose()


@pytest.fixture
async def client(app: Any) -> AsyncIterator[Client]:
    """Provide a coordinator (lmboswell) client."""
    wrapper = Client(app)
    await wrapper.login()
    yield wrapper
    await wrapper.close()


def child(record_id: str, **overrides: Any) -> dict[str, Any]:
    """One minimal final-child row."""
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
        # Keep dob consistent: the repository recomputes the age on write.
        row["dob"] = f"{2026 - int(overrides['age_at_xmas'])}-01-01"
    return row


def family(family_id: int, **overrides: Any) -> dict[str, Any]:
    """One minimal final-family row."""
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
    """One minimal final-sponsor row."""
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
    """Make the seeded summaries agree with the children.

    The family angel count is not a manual_match-owned column, so the
    seed must equal what the shared helper derives.

    Args:
        children: The child rows.
        families: The family rows (updated in place).
        sponsors: The sponsor rows (updated in place).
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
    app: Any,
    *,
    children: list[dict[str, Any]],
    families: list[dict[str, Any]],
    sponsors: list[dict[str, Any]],
    derive: bool = True,
) -> None:
    """Seed the three final stores through the migrate path."""
    if derive:
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


async def _read(app: Any, entity: str) -> list[dict[str, Any]]:
    """Read one complete final store."""
    cfg = app.state.cfg
    engine = create_async_engine(cfg.storage.database_url)
    try:
        async with UnitOfWork(engine).store(cfg, "test") as repo:
            return await repo.final_rows(entity)
    finally:
        await engine.dispose()


async def _store(app: Any, entity: str, key: str, value: Any) -> dict[str, Any]:
    """Return one stored row by key."""
    rows = await _read(app, entity)
    return next(row for row in rows if row.get(key) == value)


async def _audit_events(app: Any) -> list[SecurityAuditEvent]:
    """Read the security audit rows."""
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


async def _manual_events(app: Any) -> list[SecurityAuditEvent]:
    """Read the manual assignment/release audit rows."""
    return [
        event
        for event in await _audit_events(app)
        if str(event.action or "").startswith("manual_")
    ]


async def _hold_rows(app: Any) -> list[FamilyHold]:
    """Read every family-hold row, oldest first."""
    cfg = app.state.cfg
    engine = create_async_engine(cfg.storage.database_url)
    factory = async_session_factory(engine)
    try:
        async with factory() as session:
            result = await session.scalars(
                select(FamilyHold).order_by(FamilyHold.id)
            )
            return list(result)
    finally:
        await engine.dispose()


async def _active_match_job(app: Any) -> int:
    """Insert one running match job and return its id."""
    cfg = app.state.cfg
    engine = create_async_engine(cfg.storage.database_url)
    factory = async_session_factory(engine)
    try:
        async with factory() as session:
            season = await session.scalar(
                select(Season).where(Season.name == cfg.season)
            )
            job = Job(
                job_type="match",
                season_id=season.id,
                status="running",
                requester="test",
            )
            session.add(job)
            await session.commit()
            return int(job.id)
    finally:
        await engine.dispose()


async def _versions(app: Any, record_ids: list[str], sponsor_id: int) -> dict:
    """Build an expected_versions map from the live stores."""
    children = {row["zite_record_id"]: row for row in await _read(app, "child")}
    sponsor_row = await _store(app, "sponsor", "sponsor_id", sponsor_id)
    versions = {
        f"child:{record_id}": children[record_id]["_row_version"]
        for record_id in record_ids
    }
    versions[f"sponsor:{sponsor_id}"] = sponsor_row["_row_version"]
    return versions


# ---------------------------------------------------------------------------
# Pool.
# ---------------------------------------------------------------------------


async def test_pool_membership_excludes_ineligible_families(
    app: Any, client: Client
) -> None:
    """Membership: ignored/empty/duplicate-only families are out."""
    await _seed(
        app,
        children=[
            child("1A", family_id=1, gender="Male"),
            child("1B", family_id=1, gender="Female", age_at_xmas=11),
            child("2A", family_id=2),
            child("3A", family_id=3, ignore=True),
            child("4A", family_id=4, potential_duplicate=True),
            child("5A", family_id=5, angel_id=None),
            child("9A", family_id=None),
        ],
        families=[
            family(1),
            family(2, ignore=True),
            family(3),
            family(4),
            family(5),
        ],
        sponsors=[sponsor(1)],
    )
    response = await client.client.get(
        f"{BASE}/pool", params={"assignment": "any"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["family_id"] for item in body["items"]] == [1, 5]
    assert body["total"] == 2
    letterless = body["items"][1]["children"][0]
    assert letterless["selectable"] is False
    assert letterless["unselectable_reason"] == "no angel id"


async def test_pool_filters_are_existential_and_paged(
    app: Any, client: Client
) -> None:
    """Each filter narrows; child filters match when one child matches."""
    await _seed(
        app,
        children=[
            child("1A", family_id=1, gender="Female", age_at_xmas=6),
            child("1B", family_id=1, gender="Male", age_at_xmas=14),
            child("2A", family_id=2, gender="Male", age_at_xmas=7),
            child(
                "3A",
                family_id=3,
                gender="Male",
                age_at_xmas=7,
                special_needs="asthma",
                status="new",
            ),
        ],
        families=[
            family(1, gift_pickup_location="Calumet"),
            family(2),
            family(3),
        ],
        sponsors=[sponsor(1)],
    )

    async def ids(**params: Any) -> list[int]:
        response = await client.client.get(f"{BASE}/pool", params=params)
        assert response.status_code == 200, response.text
        return [item["family_id"] for item in response.json()["items"]]

    assert await ids(assignment="any") == [1, 2, 3]
    assert await ids(assignment="any", child_gender="Female") == [1]
    assert await ids(assignment="any", age_min=13) == [1]
    assert await ids(assignment="any", special_needs="yes") == [3]
    assert await ids(assignment="any", location="calumet") == [1]
    assert await ids(assignment="any", location="hancock") == [2, 3]
    assert await ids(assignment="any", family_size_min=2) == [1]
    assert await ids(assignment="any", child_status="new") == [3]
    assert await ids(assignment="any", q="lee") == []
    assert await ids(assignment="any", q="Jane") == [1, 2, 3]
    first = await client.client.get(
        f"{BASE}/pool", params={"assignment": "any", "page_size": 2}
    )
    assert len(first.json()["items"]) == 2
    assert first.json()["total"] == 3
    second = await client.client.get(
        f"{BASE}/pool",
        params={"assignment": "any", "page_size": 2, "page": 2},
    )
    assert [item["family_id"] for item in second.json()["items"]] == [3]


async def test_pool_assignment_states(app: Any, client: Client) -> None:
    """The four states are distinguished; split is not assigned."""
    await _seed(
        app,
        children=[
            child("1A", family_id=1),
            child("2A", family_id=2, assigned_sponsor_id=1),
            child("2B", family_id=2),
            child("3A", family_id=3, assigned_sponsor_id=1),
            child("3B", family_id=3, assigned_sponsor_id=2),
            child("4A", family_id=4, assigned_sponsor_id=1),
            child("4B", family_id=4, assigned_sponsor_id=1),
        ],
        families=[family(1), family(2), family(3), family(4)],
        sponsors=[sponsor(1), sponsor(2)],
    )

    async def ids(state: str) -> list[int]:
        response = await client.client.get(
            f"{BASE}/pool", params={"assignment": state}
        )
        return [item["family_id"] for item in response.json()["items"]]

    assert await ids("unassigned") == [1]
    assert await ids("partial") == [2]
    assert await ids("split") == [3]
    assert await ids("assigned") == [4]
    response = await client.client.get(
        f"{BASE}/pool", params={"assignment": "split"}
    )
    assert response.json()["items"][0]["assignment_state"] == "split"


async def test_pool_card_payload_and_sponsor_fit(
    app: Any, client: Client
) -> None:
    """The card carries needs/wants/sizes; sponsor fit orders and drops."""
    await _seed(
        app,
        children=[
            child("1A", family_id=1, gender="Female"),
            child("2A", family_id=2, gender="Non-binary"),
        ],
        families=[family(1), family(2)],
        sponsors=[sponsor(1), sponsor(2, gender_preference="Female")],
    )
    response = await client.client.get(
        f"{BASE}/pool", params={"assignment": "any", "page_size": 5}
    )
    card = response.json()["items"][0]
    line = card["children"][0]
    assert line["need_1"] == "coat"
    assert line["desire_1"] == "lego"
    assert line["interests"] == "soccer"
    assert line["other_notes"] == "allergy"
    assert line["sizes"]["shoe"] == "5 Youth"
    assert line["sizes"]["shirt"] == "M"
    assert line["sizes"]["pant"] is None

    fitted = await client.client.get(
        f"{BASE}/pool",
        params={"assignment": "any", "sponsor_id": 2, "page_size": 5},
    )
    assert [item["family_id"] for item in fitted.json()["items"]] == [1]
    unknown = await client.client.get(f"{BASE}/pool", params={"sponsor_id": 99})
    assert unknown.status_code == 404


# ---------------------------------------------------------------------------
# Assign.
# ---------------------------------------------------------------------------


async def test_pool_all_audited_and_info_sheet_filters(
    app: Any, client: Client
) -> None:
    """all_audited is family-level; info_sheet is existential."""
    await _seed(
        app,
        children=[
            child("1A", family_id=1, generated_info_sheet=True),
            child("1B", family_id=1, generated_info_sheet=True),
            child("2A", family_id=2, generated_info_sheet=True),
            child("2B", family_id=2, status="in_progress"),
            child("3A", family_id=3, status="new"),
            child("4A", family_id=4, gender="Female"),
        ],
        families=[family(1), family(2), family(3), family(4)],
        sponsors=[sponsor(1)],
    )

    async def ids(**params: Any) -> list[int]:
        response = await client.client.get(
            f"{BASE}/pool", params={"assignment": "any", **params}
        )
        assert response.status_code == 200, response.text
        return [item["family_id"] for item in response.json()["items"]]

    assert await ids() == [1, 2, 3, 4]
    assert await ids(all_audited="false") == [1, 2, 3, 4]
    assert await ids(all_audited="true") == [1, 4]
    assert await ids(info_sheet="any") == [1, 2, 3, 4]
    assert await ids(info_sheet="missing") == [2, 3, 4]
    assert await ids(info_sheet="generated") == [1, 2]
    assert await ids(all_audited="true", info_sheet="missing") == [4]
    assert await ids(all_audited="true", child_gender="Female") == [4]
    response = await client.client.get(
        f"{BASE}/pool", params={"info_sheet": "bogus"}
    )
    assert response.status_code == 422


async def test_assign_whole_family_writes_summaries_and_audit(
    app: Any, client: Client
) -> None:
    """A whole family lands everywhere, in one transaction, audited."""
    await _seed(
        app,
        children=[child("1A", family_id=1), child("1B", family_id=1)],
        families=[family(1)],
        sponsors=[sponsor(1)],
    )
    versions = await _versions(app, ["1A", "1B"], 1)
    response = await client.client.post(
        f"{BASE}/assignments",
        json={
            "sponsor_id": 1,
            "family_ids": [1],
            "expected_versions": versions,
            "note": "whole family",
        },
        headers=client.headers(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert {item["zite_record_id"] for item in body["assigned"]} == {"1A", "1B"}
    assert body["families"][0]["assignment_state"] == "assigned"
    assert body["families"][0]["assigned_sponsor_id"] == 1
    assert body["sponsors"] == [
        {
            "sponsor_id": 1,
            "assigned_count": 2,
            "remaining": 3,
        }
    ]
    assert body["versions"]["child:1A"] >= 2

    stored = {row["zite_record_id"]: row for row in await _read(app, "child")}
    assert {stored[key]["assigned_sponsor_id"] for key in stored} == {1}
    assert {stored[key]["assignment_source"] for key in stored} == {"manual"}
    fam = await _store(app, "family", "family_id", 1)
    assert fam["assigned_sponsor_id"] == 1
    assert fam["assigned_angel_ids"] == "1A,1B"
    sponsor_row = await _store(app, "sponsor", "sponsor_id", 1)
    assert sponsor_row["num_angels_assigned"] == 2

    events = await _manual_events(app)
    assert [event.action for event in events] == ["manual_assignment"]
    assert events[0].object_type == "child"
    assert events[0].season == "2026"
    details = events[0].details or {}
    assert details["sponsor_id"] == 1
    assert details["family_ids"] == [1]
    assert details["note"] == "whole family"


async def test_assign_subset_splits_and_confirm_reassign(
    app: Any, client: Client
) -> None:
    """A subset leaves the family split; moving needs confirm."""
    await _seed(
        app,
        children=[
            child("1A", family_id=1, assigned_sponsor_id=1),
            child("1B", family_id=1, assigned_sponsor_id=1),
        ],
        families=[family(1)],
        sponsors=[sponsor(1), sponsor(2)],
    )
    versions = await _versions(app, ["1A"], 2)
    response = await client.client.post(
        f"{BASE}/assignments",
        json={
            "sponsor_id": 2,
            "child_ids": ["1A"],
            "expected_versions": versions,
            "confirm": False,
        },
        headers=client.headers(),
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "conflict"
    assert "1" in response.json()["detail"]["message"]

    versions = await _versions(app, ["1A"], 2)
    response = await client.client.post(
        f"{BASE}/assignments",
        json={
            "sponsor_id": 2,
            "child_ids": ["1A"],
            "expected_versions": versions,
            "confirm": True,
        },
        headers=client.headers(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reassigned"][0]["from_sponsor_id"] == 1
    assert body["families"][0]["assignment_state"] == "split"
    assert body["families"][0]["assigned_sponsor_id"] is None
    assert body["families"][0]["assigned_sponsor_ids"] == [1, 2]
    assert any("split" in warning for warning in body["warnings"])
    assert {summary["sponsor_id"] for summary in body["sponsors"]} == {1, 2}
    assert {summary["assigned_count"] for summary in body["sponsors"]} == {1}
    sponsor_one = await _store(app, "sponsor", "sponsor_id", 1)
    assert sponsor_one["num_angels_assigned"] == 1


async def test_assign_same_sponsor_is_idempotent(
    app: Any, client: Client
) -> None:
    """Assigning a child to its own sponsor is an unchanged no-op."""
    await _seed(
        app,
        children=[child("1A", family_id=1, assigned_sponsor_id=1)],
        families=[family(1)],
        sponsors=[sponsor(1)],
    )
    versions = await _versions(app, ["1A"], 1)
    response = await client.client.post(
        f"{BASE}/assignments",
        json={
            "sponsor_id": 1,
            "child_ids": ["1A"],
            "expected_versions": versions,
        },
        headers=client.headers(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["unchanged"][0]["zite_record_id"] == "1A"
    stored = await _store(app, "child", "zite_record_id", "1A")
    assert stored["_row_version"] == 1


async def test_assign_rejections(app: Any, client: Client) -> None:
    """Unknown, ignored, orphan, letterless and duplicate children refuse."""
    await _seed(
        app,
        children=[
            child("1A", family_id=1),
            child("2A", family_id=2, ignore=True),
            child("3A", family_id=None),
            child("4A", family_id=4, angel_id=None),
            child("5A", family_id=5, potential_duplicate=True),
            child("6A", family_id=6),
        ],
        families=[
            family(1),
            family(2),
            family(3),
            family(4),
            family(5),
            family(6, ignore=True),
        ],
        sponsors=[sponsor(1), sponsor(2, ignore=True)],
    )
    versions = await _versions(app, ["1A"], 1)

    async def post(body: dict[str, Any]) -> Any:
        return await client.client.post(
            f"{BASE}/assignments", json=body, headers=client.headers()
        )

    response = await post(
        {
            "sponsor_id": 99,
            "child_ids": ["1A"],
            "expected_versions": {
                "child:1A": versions["child:1A"],
                "sponsor:99": 1,
            },
        }
    )
    assert response.status_code == 404
    response = await post(
        {
            "sponsor_id": 2,
            "child_ids": ["1A"],
            "expected_versions": {
                "child:1A": versions["child:1A"],
                "sponsor:2": 1,
            },
        }
    )
    assert response.status_code == 409
    response = await post(
        {
            "sponsor_id": 1,
            "child_ids": ["nope"],
            "expected_versions": versions,
        }
    )
    assert response.status_code == 404
    for record_id in ("2A", "3A", "4A", "5A", "6A"):
        response = await post(
            {
                "sponsor_id": 1,
                "child_ids": [record_id],
                "expected_versions": {
                    "child:" + record_id: 1,
                    "sponsor:1": versions["sponsor:1"],
                },
            }
        )
        assert response.status_code == 409, record_id
    assert (await _read(app, "child"))[0]["assigned_sponsor_id"] is None


async def test_assign_expected_versions(app: Any, client: Client) -> None:
    """A missing version is 422; a stale one is 412 with the server row."""
    await _seed(
        app,
        children=[child("1A", family_id=1)],
        families=[family(1)],
        sponsors=[sponsor(1)],
    )
    missing = await client.client.post(
        f"{BASE}/assignments",
        json={"sponsor_id": 1, "child_ids": ["1A"]},
        headers=client.headers(),
    )
    assert missing.status_code == 422

    stale = await client.client.post(
        f"{BASE}/assignments",
        json={
            "sponsor_id": 1,
            "child_ids": ["1A"],
            "expected_versions": {"child:1A": 99, "sponsor:1": 1},
        },
        headers=client.headers(),
    )
    assert stale.status_code == 412, stale.text
    detail = stale.json()["detail"]
    assert detail["code"] == "stale_version"
    assert detail["entity"] == "child"
    assert detail["row"]["zite_record_id"] == "1A"


async def test_assign_is_all_or_nothing(app: Any, client: Client) -> None:
    """A mixed valid/invalid selection writes nothing."""
    await _seed(
        app,
        children=[
            child("1A", family_id=1),
            child("2A", family_id=2, ignore=True),
        ],
        families=[family(1), family(2)],
        sponsors=[sponsor(1)],
    )
    versions = await _versions(app, ["1A"], 1)
    response = await client.client.post(
        f"{BASE}/assignments",
        json={
            "sponsor_id": 1,
            "child_ids": ["1A", "2A"],
            "expected_versions": {
                **versions,
                "child:2A": 1,
            },
        },
        headers=client.headers(),
    )
    assert response.status_code == 409
    stored = await _store(app, "child", "zite_record_id", "1A")
    assert stored["assigned_sponsor_id"] is None
    sponsor_row = await _store(app, "sponsor", "sponsor_id", 1)
    assert sponsor_row["num_angels_assigned"] == 0
    assert await _manual_events(app) == []


async def test_assign_capacity_cap_and_release_frees_it(
    app: Any, client: Client
) -> None:
    """The sponsor cap is hard; release frees capacity; null is uncapped."""
    await _seed(
        app,
        children=[child("1A", family_id=1), child("2A", family_id=2)],
        families=[family(1), family(2)],
        sponsors=[sponsor(1, num_children=1)],
    )
    versions = await _versions(app, ["1A"], 1)
    first = await client.client.post(
        f"{BASE}/assignments",
        json={
            "sponsor_id": 1,
            "child_ids": ["1A"],
            "expected_versions": versions,
        },
        headers=client.headers(),
    )
    assert first.status_code == 200, first.text

    versions = await _versions(app, ["2A"], 1)
    full = await client.client.post(
        f"{BASE}/assignments",
        json={
            "sponsor_id": 1,
            "child_ids": ["2A"],
            "expected_versions": versions,
        },
        headers=client.headers(),
    )
    assert full.status_code == 409
    detail = full.json()["detail"]
    assert detail["code"] == "sponsor_full"
    assert detail["limit"] == 1
    assert detail["assigned_count"] == 1

    released = await client.client.post(
        f"{BASE}/release",
        json={
            "child_ids": ["1A"],
            "confirm": True,
            "expected_versions": {
                "child:1A": (
                    await _store(app, "child", "zite_record_id", "1A")
                )["_row_version"]
            },
        },
        headers=client.headers(),
    )
    assert released.status_code == 200, released.text

    versions = await _versions(app, ["2A"], 1)
    retry = await client.client.post(
        f"{BASE}/assignments",
        json={
            "sponsor_id": 1,
            "child_ids": ["2A"],
            "expected_versions": versions,
        },
        headers=client.headers(),
    )
    assert retry.status_code == 200, retry.text


async def test_assign_uncapped_sponsor(app: Any, client: Client) -> None:
    """A null num_children is unlimited."""
    await _seed(
        app,
        children=[child("1A", family_id=1)],
        families=[family(1)],
        sponsors=[sponsor(1, num_children=None)],
    )
    versions = await _versions(app, ["1A"], 1)
    response = await client.client.post(
        f"{BASE}/assignments",
        json={
            "sponsor_id": 1,
            "child_ids": ["1A"],
            "expected_versions": versions,
        },
        headers=client.headers(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["sponsors"][0]["remaining"] is None


async def test_assign_corrects_derived_family_angel_count(
    app: Any, client: Client
) -> None:
    """A stored num_angels_in_family that disagrees is rewritten, not refused."""
    await _seed(
        app,
        children=[child("1A", family_id=1), child("1B", family_id=1)],
        families=[family(1, num_angels_in_family=99)],
        sponsors=[sponsor(1)],
        derive=False,
    )
    versions = await _versions(app, ["1A", "1B"], 1)
    response = await client.client.post(
        f"{BASE}/assignments",
        json={
            "sponsor_id": 1,
            "family_ids": [1],
            "expected_versions": versions,
        },
        headers=client.headers(),
    )
    assert response.status_code == 200, response.text
    stored = await _store(app, "family", "family_id", 1)
    assert stored["num_angels_in_family"] == 2
    assert stored["assigned_sponsor_id"] == 1


# ---------------------------------------------------------------------------
# Release.
# ---------------------------------------------------------------------------


async def test_sponsor_picker_and_assignment_list(
    app: Any, client: Client
) -> None:
    """The picker reports capacity; the list derives from the children."""
    await _seed(
        app,
        children=[
            child("1A", family_id=1, assigned_sponsor_id=1),
            child("2A", family_id=2, assigned_sponsor_id=2),
        ],
        families=[family(1), family(2)],
        sponsors=[
            sponsor(1, num_children=3, organization_name="Acme"),
            sponsor(2, num_children=None),
            sponsor(3, num_children=0, sponsor_type="Organization"),
        ],
    )
    response = await client.client.get(f"{BASE}/sponsors")
    assert response.status_code == 200, response.text
    items = {item["sponsor_id"]: item for item in response.json()["items"]}
    assert items[1]["display_name"] == "Acme"
    assert items[1]["assigned_count"] == 1
    assert items[1]["remaining"] == 2
    assert items[2]["remaining"] is None

    full = await client.client.get(
        f"{BASE}/sponsors", params={"has_capacity": "true"}
    )
    assert {item["sponsor_id"] for item in full.json()["items"]} == {1, 2}
    typed = await client.client.get(
        f"{BASE}/sponsors", params={"affiliation": "Organization"}
    )
    assert [item["sponsor_id"] for item in typed.json()["items"]] == [3]

    listing = await client.client.get(f"{BASE}/assignments")
    assert listing.status_code == 200, listing.text
    assert listing.json()["total"] == 2
    filtered = await client.client.get(
        f"{BASE}/assignments", params={"sponsor_id": 2}
    )
    assert [item["zite_record_id"] for item in filtered.json()["items"]] == [
        "2A"
    ]


async def test_assignment_list_carries_the_angel_details(
    app: Any, client: Client
) -> None:
    """The list carries each angel's name, age, needs, wishes and drop-off."""
    await _seed(
        app,
        children=[
            child(
                "1A",
                family_id=1,
                assigned_sponsor_id=1,
                first_name="Alice",
                age_at_xmas=10,
                need_1="coat",
                need_2="boots",
                desire_1="lego",
                desire_2="puzzle",
            ),
            child("2A", family_id=99, assigned_sponsor_id=1),
        ],
        families=[family(1, gift_pickup_location="Calumet")],
        sponsors=[sponsor(1)],
    )
    response = await client.client.get(
        f"{BASE}/assignments", params={"sponsor_id": 1}
    )
    assert response.status_code == 200, response.text
    items = {item["zite_record_id"]: item for item in response.json()["items"]}
    assert items["1A"]["first_name"] == "Alice"
    assert items["1A"]["age_at_xmas"] == 10
    assert items["1A"]["need_1"] == "coat"
    assert items["1A"]["need_2"] == "boots"
    assert items["1A"]["desire_1"] == "lego"
    assert items["1A"]["desire_2"] == "puzzle"
    assert items["1A"]["dropoff_location"] == "Calumet"
    assert items["2A"]["dropoff_location"] is None


async def test_release_requires_confirm(app: Any, client: Client) -> None:
    """Releasing without confirm is a 422 and writes nothing."""
    await _seed(
        app,
        children=[child("1A", family_id=1, assigned_sponsor_id=1)],
        families=[family(1)],
        sponsors=[sponsor(1)],
    )
    response = await client.client.post(
        f"{BASE}/release",
        json={"child_ids": ["1A"]},
        headers=client.headers(),
    )
    assert response.status_code == 422
    stored = await _store(app, "child", "zite_record_id", "1A")
    assert stored["assigned_sponsor_id"] == 1


async def test_release_one_child_then_all(app: Any, client: Client) -> None:
    """Releasing part of a split resolves it; all returns unassigned."""
    await _seed(
        app,
        children=[
            child("1A", family_id=1, assigned_sponsor_id=1),
            child("1B", family_id=1, assigned_sponsor_id=2),
        ],
        families=[family(1)],
        sponsors=[sponsor(1), sponsor(2)],
    )
    child_row = await _store(app, "child", "zite_record_id", "1A")
    response = await client.client.post(
        f"{BASE}/release",
        json={
            "child_ids": ["1A"],
            "confirm": True,
            "expected_versions": {"child:1A": child_row["_row_version"]},
        },
        headers=client.headers(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["released"][0]["from_sponsor_id"] == 1
    assert body["families"][0]["assignment_state"] == "partial"
    assert body["families"][0]["assigned_sponsor_id"] == 2

    response = await client.client.post(
        f"{BASE}/release",
        json={"family_ids": [1], "confirm": True},
        headers=client.headers(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["families"][0]["assignment_state"] == "unassigned"
    stored = await _read(app, "child")
    assert all(row["assigned_sponsor_id"] is None for row in stored)
    events = await _manual_events(app)
    assert [event.action for event in events] == [
        "manual_release",
        "manual_release",
    ]


# ---------------------------------------------------------------------------
# Match-running guard.
# ---------------------------------------------------------------------------


async def test_match_running_guard(app: Any, client: Client) -> None:
    """An active match job blocks assign, release and hold."""
    await _seed(
        app,
        children=[child("1A", family_id=1, assigned_sponsor_id=1)],
        families=[family(1)],
        sponsors=[sponsor(1)],
    )
    await _active_match_job(app)
    versions = await _versions(app, ["1A"], 1)
    for path, body in (
        (
            "assignments",
            {
                "sponsor_id": 1,
                "child_ids": ["1A"],
                "expected_versions": versions,
            },
        ),
        ("release", {"child_ids": ["1A"], "confirm": True}),
        ("holds", {"family_id": 1, "note": "why"}),
    ):
        response = await client.client.post(
            f"{BASE}/{path}", json=body, headers=client.headers()
        )
        assert response.status_code == 409, path
        assert response.json()["detail"]["code"] == "match_running"
    pool = await client.client.get(f"{BASE}/pool")
    assert pool.json()["automatic_run"]["active"] is True


# ---------------------------------------------------------------------------
# Holds.
# ---------------------------------------------------------------------------


async def test_hold_validation(app: Any, client: Client) -> None:
    """Blank note 422, unknown 404, ignored 409, manual assignment 409."""
    await _seed(
        app,
        children=[child("1A", family_id=1), child("2A", family_id=2)],
        families=[family(1), family(2, ignore=True), family(3)],
        sponsors=[sponsor(1)],
    )
    blank = await client.client.post(
        f"{BASE}/holds",
        json={"family_id": 1, "note": "   "},
        headers=client.headers(),
    )
    assert blank.status_code == 422
    unknown = await client.client.post(
        f"{BASE}/holds",
        json={"family_id": 99, "note": "why"},
        headers=client.headers(),
    )
    assert unknown.status_code == 404
    ignored = await client.client.post(
        f"{BASE}/holds",
        json={"family_id": 2, "note": "why"},
        headers=client.headers(),
    )
    assert ignored.status_code == 409

    versions = await _versions(app, ["1A"], 1)
    await client.client.post(
        f"{BASE}/assignments",
        json={
            "sponsor_id": 1,
            "child_ids": ["1A"],
            "expected_versions": versions,
        },
        headers=client.headers(),
    )
    manual = await client.client.post(
        f"{BASE}/holds",
        json={"family_id": 1, "note": "why"},
        headers=client.headers(),
    )
    assert manual.status_code == 409
    assert "release the assignment" in manual.json()["detail"]["message"]


async def test_hold_lifecycle(app: Any, client: Client) -> None:
    """Hold, replace, release, and the assignment interaction."""
    await _seed(
        app,
        children=[child("1A", family_id=1), child("2A", family_id=2)],
        families=[family(1), family(2)],
        sponsors=[sponsor(1)],
    )
    first = await client.client.post(
        f"{BASE}/holds",
        json={"family_id": 1, "note": "moved away"},
        headers=client.headers(),
    )
    assert first.status_code == 200, first.text
    assert first.json()["held"] is True
    assert first.json()["note"] == "moved away"

    replaced = await client.client.post(
        f"{BASE}/holds",
        json={"family_id": 1, "note": "second reason"},
        headers=client.headers(),
    )
    assert replaced.status_code == 200
    assert replaced.json()["note"] == "second reason"

    held = await client.client.get(f"{BASE}/pool", params={"hold": "held"})
    assert [item["family_id"] for item in held.json()["items"]] == [1]
    assert held.json()["items"][0]["hold"]["note"] == "second reason"
    default = await client.client.get(f"{BASE}/pool")
    assert [item["family_id"] for item in default.json()["items"]] == [2]

    # Assigning a held family releases the hold in the same write path.
    versions = await _versions(app, ["1A"], 1)
    assigned = await client.client.post(
        f"{BASE}/assignments",
        json={
            "sponsor_id": 1,
            "child_ids": ["1A"],
            "expected_versions": versions,
        },
        headers=client.headers(),
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["families"][0]["hold_released"] is True
    holds = await client.client.get(f"{BASE}/holds")
    assert holds.json()["total"] == 0

    # Releasing the assignment does not restore the hold.
    child_row = await _store(app, "child", "zite_record_id", "1A")
    released = await client.client.post(
        f"{BASE}/release",
        json={
            "child_ids": ["1A"],
            "confirm": True,
            "expected_versions": {"child:1A": child_row["_row_version"]},
        },
        headers=client.headers(),
    )
    assert released.status_code == 200, released.text
    assert (await client.client.get(f"{BASE}/holds")).json()["total"] == 0

    # Releasing a hold that does not exist is a no-op success.
    noop = await client.client.post(
        f"{BASE}/holds/release",
        json={"family_id": 2},
        headers=client.headers(),
    )
    assert noop.status_code == 200
    assert noop.json()["held"] is False


async def test_hold_release_lands_with_the_assignment(
    app: Any, client: Client
) -> None:
    """Assigning a held family stamps the hold and stores the placement."""
    await _seed(
        app,
        children=[child("1A", family_id=1)],
        families=[family(1)],
        sponsors=[sponsor(1)],
    )
    hold = await client.client.post(
        f"{BASE}/holds",
        json={"family_id": 1, "note": "not yet a fit"},
        headers=client.headers(),
    )
    assert hold.status_code == 200, hold.text

    versions = await _versions(app, ["1A"], 1)
    assigned = await client.client.post(
        f"{BASE}/assignments",
        json={
            "sponsor_id": 1,
            "family_ids": [1],
            "expected_versions": versions,
        },
        headers=client.headers(),
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["families"][0]["hold_released"] is True

    holds = await _hold_rows(app)
    assert len(holds) == 1
    assert holds[0].released_at is not None
    assert holds[0].released_by == "lmboswell"
    assert (await _store(app, "child", "zite_record_id", "1A"))[
        "assigned_sponsor_id"
    ] == 1
    assert (await _store(app, "family", "family_id", 1))[
        "assigned_sponsor_id"
    ] == 1
    assert [event.action for event in await _manual_events(app)] == [
        "manual_assignment"
    ]


# ---------------------------------------------------------------------------
# Sponsor flags and packet dispatch.
# ---------------------------------------------------------------------------

DISPATCHED_AT = "2026-11-20T12:00:00+00:00"


async def _dispatch(
    app: Any, client: Client, sponsor_id: int, versions: dict | None = None
) -> Any:
    """POST one dispatch with live (or the given) expected versions."""
    if versions is None:
        versions = {
            f"sponsor:{sponsor_id}": (
                await _store(app, "sponsor", "sponsor_id", sponsor_id)
            )["_row_version"]
        }
    return await client.client.post(
        f"{BASE}/dispatch",
        json={"sponsor_id": sponsor_id, "expected_versions": versions},
        headers=client.headers(),
    )


async def test_sponsors_expose_flags_and_dispatch_fields(
    app: Any, client: Client
) -> None:
    """The picker carries the name parts, flags and dispatch state."""
    await _seed(
        app,
        children=[],
        families=[],
        sponsors=[
            sponsor(
                1,
                organization_name="Acme",
                elevated_budget=True,
                physical_packet=False,
            ),
            sponsor(2, packets_dispatched_at=DISPATCHED_AT),
        ],
    )
    response = await client.client.get(f"{BASE}/sponsors")
    assert response.status_code == 200, response.text
    items = {item["sponsor_id"]: item for item in response.json()["items"]}
    assert items[1]["display_name"] == "Acme"
    assert items[1]["first_name"] == "Mary"
    assert items[1]["last_name"] == "Sponsor1"
    assert items[1]["organization_name"] == "Acme"
    assert items[1]["elevated_budget"] is True
    assert items[1]["physical_packet"] is False
    assert items[1]["packets_dispatched"] is False
    assert items[1]["packets_dispatched_at"] is None
    assert items[2]["organization_name"] is None
    # The store coerces every boolean column, so "undecided" reads False.
    assert items[2]["elevated_budget"] is False
    assert items[2]["physical_packet"] is False
    assert items[2]["packets_dispatched"] is True
    assert items[2]["packets_dispatched_at"] == DISPATCHED_AT


async def test_audit_patch_sets_sponsor_flags(app: Any, client: Client) -> None:
    """An audit save writes the two flags and keeps the dispatch stamp."""
    await _seed(
        app,
        children=[],
        families=[],
        sponsors=[
            sponsor(1, status="new", packets_dispatched_at=DISPATCHED_AT)
        ],
    )
    version = (await _store(app, "sponsor", "sponsor_id", 1))["_row_version"]
    response = await client.client.patch(
        "/api/v1/seasons/2026/sponsors/1",
        json={"elevated_budget": True, "physical_packet": False},
        headers=client.headers() | {"If-Match": str(version)},
    )
    assert response.status_code == 200, response.text
    stored = await _store(app, "sponsor", "sponsor_id", 1)
    assert stored["elevated_budget"] is True
    assert stored["physical_packet"] is False
    assert stored["packets_dispatched_at"] == DISPATCHED_AT
    assert stored["_row_version"] == version + 1


async def test_dispatch_success(app: Any, client: Client) -> None:
    """A full sponsor dispatches: stamp, version bump, event, fresh DTO."""
    await _seed(
        app,
        children=[child("1A", family_id=1, assigned_sponsor_id=1)],
        families=[family(1)],
        sponsors=[sponsor(1, num_children=1)],
    )
    before = await _store(app, "sponsor", "sponsor_id", 1)
    response = await _dispatch(app, client, 1)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sponsor_id"] == 1
    assert body["packets_dispatched"] is True
    assert body["packets_dispatched_at"]
    assert body["row_version"] == before["_row_version"] + 1
    assert body["assigned_count"] == 1
    stored = await _store(app, "sponsor", "sponsor_id", 1)
    assert stored["packets_dispatched_at"] == body["packets_dispatched_at"]
    assert stored["_row_version"] == before["_row_version"] + 1
    events = await _manual_events(app)
    assert [event.action for event in events] == ["manual_dispatch"]
    assert events[0].object_type == "sponsor"
    assert events[0].object_id == "1"
    assert (events[0].details or {})["sponsor_id"] == 1


async def test_dispatch_refusals(app: Any, client: Client) -> None:
    """Unfilled, uncapped, already-dispatched, unknown and ignored refuse."""
    await _seed(
        app,
        children=[
            child("1A", family_id=1, assigned_sponsor_id=1),
            child("2A", family_id=2, assigned_sponsor_id=2),
            child("3A", family_id=3, assigned_sponsor_id=3),
        ],
        families=[family(1), family(2), family(3)],
        sponsors=[
            sponsor(1, num_children=2),
            sponsor(2, num_children=None),
            sponsor(3, num_children=1, packets_dispatched_at=DISPATCHED_AT),
            sponsor(4, num_children=1, ignore=True, status="ignored"),
        ],
    )
    not_full = await _dispatch(app, client, 1)
    assert not_full.status_code == 409
    assert not_full.json()["detail"]["code"] == "conflict"
    assert "slots must be filled" in not_full.json()["detail"]["message"]
    uncapped = await _dispatch(app, client, 2)
    assert uncapped.status_code == 409
    assert "slots must be filled" in uncapped.json()["detail"]["message"]
    again = await _dispatch(app, client, 3)
    assert again.status_code == 409
    assert "already dispatched" in again.json()["detail"]["message"]
    ignored = await _dispatch(app, client, 4)
    assert ignored.status_code == 409
    unknown = await _dispatch(app, client, 99, {"sponsor:99": 1})
    assert unknown.status_code == 404
    assert await _manual_events(app) == []


async def test_dispatch_match_running_and_stale(
    app: Any, client: Client
) -> None:
    """A running match is 409; a stale sponsor version is 412."""
    await _seed(
        app,
        children=[child("1A", family_id=1, assigned_sponsor_id=1)],
        families=[family(1)],
        sponsors=[sponsor(1, num_children=1)],
    )
    version = (await _store(app, "sponsor", "sponsor_id", 1))["_row_version"]
    stale = await _dispatch(app, client, 1, {"sponsor:1": version + 7})
    assert stale.status_code == 412
    assert stale.json()["detail"]["code"] == "stale_version"
    missing = await _dispatch(app, client, 1, {})
    assert missing.status_code == 422

    await _active_match_job(app)
    running = await _dispatch(app, client, 1, {"sponsor:1": version})
    assert running.status_code == 409
    assert running.json()["detail"]["code"] == "match_running"
    stored = await _store(app, "sponsor", "sponsor_id", 1)
    assert stored["packets_dispatched_at"] is None


async def test_dispatched_sponsor_freezes_assign_and_release(
    app: Any, client: Client
) -> None:
    """Assign to, move from, or release from a dispatched sponsor is 409."""
    await _seed(
        app,
        children=[
            child("1A", family_id=1, assigned_sponsor_id=1),
            child("2A", family_id=2),
        ],
        families=[family(1), family(2)],
        sponsors=[
            sponsor(1, num_children=1, packets_dispatched_at=DISPATCHED_AT),
            sponsor(2),
        ],
    )
    to_dispatched = await client.client.post(
        f"{BASE}/assignments",
        json={
            "sponsor_id": 1,
            "child_ids": ["2A"],
            "expected_versions": await _versions(app, ["2A"], 1),
        },
        headers=client.headers(),
    )
    assert to_dispatched.status_code == 409
    detail = to_dispatched.json()["detail"]
    assert detail["code"] == "packets_dispatched"
    assert detail["sponsor_id"] == 1

    moved_away = await client.client.post(
        f"{BASE}/assignments",
        json={
            "sponsor_id": 2,
            "child_ids": ["1A"],
            "confirm": True,
            "expected_versions": await _versions(app, ["1A"], 2),
        },
        headers=client.headers(),
    )
    assert moved_away.status_code == 409
    assert moved_away.json()["detail"]["code"] == "packets_dispatched"
    assert moved_away.json()["detail"]["sponsor_id"] == 1

    released = await client.client.post(
        f"{BASE}/release",
        json={"child_ids": ["1A"], "confirm": True},
        headers=client.headers(),
    )
    assert released.status_code == 409
    assert released.json()["detail"]["code"] == "packets_dispatched"
    stored = await _store(app, "child", "zite_record_id", "1A")
    assert stored["assigned_sponsor_id"] == 1
    assert await _manual_events(app) == []
