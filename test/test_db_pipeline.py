"""Tests for the DB-mode pipeline services (plan §9.1–§9.4, D12).

File-backed SQLite in ``tmp_path``; every external provider (Zite,
ZeroBounce, Nominatim, OpenCode) is faked at the seams the domain
modules already expose.  Stores are seeded through the ``migrate``
module commits (the Phase 2c importer's ownership), so the services
see realistic repository dicts.
"""

import asyncio
import json
import re
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import polars as pl
import pytest

from conftest import load_test_cfg
from ccat.audit.store import ENTITY_SPECS
from ccat.config import AIScreeningSpec, Config, StorageSpec, WorkerSpec
from ccat.db.engine import create_async_engine, run_migrations
from ccat.db.errors import KeyValidationError
from ccat.db.uow import UnitOfWork
from ccat.jobs import JobCancelRequested
from ccat.services import db_pipeline
from ccat.services.db_audit import save_entity_db
from ccat.services.audit import SavePatch

# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path, **updates: Any) -> Config:
    """Test config: temp DB/artifacts, offline-friendly, one AI worker."""
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
            "address_check": base.address_check.model_copy(
                update={"enabled": True, "delay": 0.0}
            ),
        }
        | updates
    )


def _prepare(tmp_path: Path, **updates: Any) -> Config:
    """Migrate the temp database and return its config."""
    cfg = _cfg(tmp_path, **updates)
    run_migrations(cfg.storage.database_url)
    return cfg


def _db_path(cfg: Config) -> str:
    """Return the raw sqlite path behind the config's async URL."""
    return cfg.storage.database_url.split("sqlite+aiosqlite:///", 1)[1]


def _sql(cfg: Config, query: str, params: tuple[Any, ...] = ()) -> list[tuple]:
    """One sync sqlite read (for in-fake assertions and test polling).

    Fail-fast on locks (retry at the call site): a blocking wait here
    would stall the event loop driving the pipeline commits.
    """
    connection = sqlite3.connect(_db_path(cfg), timeout=0.05)
    try:
        return list(connection.execute(query, params))
    finally:
        connection.close()


def _sql_retry(
    cfg: Config, query: str, params: tuple[Any, ...] = ()
) -> list[tuple]:
    """Retry one _sql read past transient lock errors."""
    import time

    last: Exception | None = None
    for _ in range(200):
        try:
            return _sql(cfg, query, params)
        except sqlite3.OperationalError as error:
            last = error
            time.sleep(0.01)
    raise AssertionError(f"sqlite read never succeeded: {last}")


def _columns(entity: str) -> tuple[str, ...]:
    """Return the entity's legacy dict column vocabulary."""
    return tuple(ENTITY_SPECS[entity]["columns"])


def child_row(record_id: str, **overrides: Any) -> dict[str, Any]:
    """One full final-child row under legacy dict names."""
    base: dict[str, Any] = {column: None for column in _columns("child")}
    base.update(
        zite_record_id=record_id,
        family_id=1,
        angel_id=None,
        first_name="Kiddo",
        primary_email="fam@example.com",
        dob="2018-05-10",
        age_at_xmas=8,
        age_reported=7,
        gender="Male",
        need_1="winter coat taht fits",
        desire_1="legos",
        shoe_size="13",
        shoe_size_type="Youth",
        pant_waist_size='24"',
        pant_inseam='20"',
        pant_size_letter="XS",
        pant_size_type="Youth",
        jean_size="12 slim",
        shirt_size_letter="XS",
        shirt_size_type="Youth",
        favorite_colors="blue",
        interests="trains",
        source="web",
        status="new",
        ignore=False,
        screened=False,
        potential_duplicate=False,
        generated_info_sheet=False,
    )
    base.update(overrides)
    # Keep the seeded dob consistent with the requested age: the commit
    # path recomputes age_at_xmas from dob, and match may only change the
    # two assignment columns.
    if "dob" not in overrides:
        base["dob"] = f"{2018 + (8 - int(base['age_at_xmas']))}-05-10"
    return base


def family_row(family_id: int, **overrides: Any) -> dict[str, Any]:
    """One full final-family row under legacy dict names."""
    base: dict[str, Any] = {column: None for column in _columns("family")}
    base.update(
        family_id=family_id,
        zite_record_id=f"rec_f{family_id}",
        created="2026-01-05T10:00:00Z",
        primary_email=f"fam{family_id}@example.com",
        first_name="Jane",
        last_name="Doe",
        street_address="1 Main St",
        city="Hancock",
        zip_code="49930",
        state="Michigan",
        gift_pickup_location="TSA (Hancock)",
        source="web",
        status="new",
        ignore=False,
        potential_duplicate=False,
        accepts_disclaimer=True,
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
        pref_age_min=0,
        pref_age_max=18,
        gender_preference="Any Gender",
        dropoff_location="Hancock",
        source="web",
        status="new",
        ignore=False,
        potential_duplicate=False,
        accepts_disclaimer=True,
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
        "shoe_size": "13",
        "shoe_size_type": "Youth",
        "pant_waist_size": '24"',
        "pant_inseam": '20"',
        "pant_size_letter": "XS",
        "pant_size_type": "Youth",
        "jean_size": "12 slim",
        "shirt_size_letter": "XS",
        "shirt_size_type": "Youth",
        "favorite_colors": "blue",
        "interests": "trains",
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
        "phone": "(906) 555-9876",
        "street_address": "1 Main St",
        "city": "Hancock",
        "zip_code": "49930",
        "state": "Michigan",
        "gift_pickup_location": "TSA (Hancock)",
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
        "phone": "(906) 555-1234",
        "num_children": 2,
        "age_preference": "Any Age",
        "gender_preference": "Any Gender",
        "dropoff_location": "Hancock",
        "source": "web",
    }
    base.update(overrides)
    return base


async def _seed_stores(
    repo: Any,
    *,
    raw: dict[str, list[dict[str, Any]]] | None = None,
    children: list[dict[str, Any]] | None = None,
    families: list[dict[str, Any]] | None = None,
    sponsors: list[dict[str, Any]] | None = None,
) -> None:
    """Seed raw snapshot and/or final stores through migrate ownership."""
    if raw is not None:
        await repo.replace_raw_snapshot("migrate", raw, "migration")
    for entity, rows in (
        ("child", children),
        ("family", families),
        ("sponsor", sponsors),
    ):
        if rows:
            await repo.commit_finals("migrate", entity, rows, "migration")


def _by_key(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    """Index one row list by its key column."""
    return {str(row[key]): row for row in rows}


# ---------------------------------------------------------------------------
# AI fake (opencode client), keyed by zite_record_id in the prompt.
# ---------------------------------------------------------------------------


class KeyedFakeOpencode:
    """Fake opencode client answering each child a canned response."""

    responses: dict[str, str] = {}
    calls: list[str] = []
    hook: Any = None  # optional callable(record_id) at generate time

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Record the fake session resource."""
        self.session = SimpleNamespace(
            create=lambda model: SimpleNamespace(id="ses_test"),
            generate=self._generate,
        )

    def _generate(self, session_id: str, prompt: str) -> SimpleNamespace:
        """Answer with the canned response for the prompted child."""
        match = re.search(r'"zite_record_id": "([^"]+)"', prompt)
        assert match is not None
        record_id = match.group(1)
        KeyedFakeOpencode.calls.append(record_id)
        if KeyedFakeOpencode.hook is not None:
            KeyedFakeOpencode.hook(record_id)
        return SimpleNamespace(text=KeyedFakeOpencode.responses[record_id])

    def __enter__(self) -> "KeyedFakeOpencode":
        """Return self as the context manager target."""
        return self

    def __exit__(self, *args: Any) -> None:
        """Swallow exceptions like the real client does."""
        return None


def _use_fake_ai(
    monkeypatch: pytest.MonkeyPatch, responses: dict[str, str]
) -> None:
    """Install the offline AI fake (and the fake opencode v2)."""
    import ccat.screening as screening
    import opencode

    class _Version:
        stdout = "2.0.0"
        stderr = ""

    monkeypatch.setattr(
        screening.subprocess, "run", lambda *args, **kwargs: _Version()
    )
    KeyedFakeOpencode.responses = dict(responses)
    KeyedFakeOpencode.calls = []
    KeyedFakeOpencode.hook = None
    monkeypatch.setattr(opencode, "Opencode", KeyedFakeOpencode)


def _null_response(record_id: str) -> str:
    """Return the literal-null model response (no corrections)."""
    return "null"


# ---------------------------------------------------------------------------
# Acquire (§9.1).
# ---------------------------------------------------------------------------


def _zite_frame(
    cfg: Config, table: str, records: list[dict[str, Any]]
) -> pl.DataFrame:
    """Build a Zite frame whose source columns satisfy import_table."""
    columns: dict[str, list[Any]] = {}
    for key, spec in cfg.tables[table].columns.items():
        if key == "zite_record_id":
            continue  # mapped from the Zite system "id" column
        source = spec.rename or key
        columns[source] = [record.get(key) for record in records]
    columns["id"] = [record.get("zite_record_id") for record in records]
    return pl.DataFrame(columns)


def test_acquire_db_replaces_raw_and_touches_no_finals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """acquire: raw snapshot active, finals untouched (§9.1 guard)."""
    import ccat.zite

    cfg = _prepare(tmp_path)

    async def main() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(
                    repo,
                    children=[child_row("c1")],
                    families=[family_row(1)],
                    sponsors=[sponsor_row(1)],
                )
                before = {
                    entity: await repo.final_rows(entity)
                    for entity in ("child", "family", "sponsor")
                }
            records = {
                "sponsor": [raw_sponsor("rec_s1")],
                "family": [raw_family("fam@example.com")],
                "child": [raw_child("c1")],
            }
            table_of = {
                cfg.zite.sponsor: "sponsor",
                cfg.zite.family: "family",
                cfg.zite.child: "child",
            }

            def fake_pull(reference: str) -> pl.DataFrame:
                return _zite_frame(
                    cfg, table_of[reference], records[table_of[reference]]
                )

            monkeypatch.setattr(ccat.zite, "get_table_data", fake_pull)

            result = await db_pipeline.acquire(
                cfg, "topher", lambda *a, **k: None, lambda: None
            )
            assert result.raw_rows == {"sponsor": 1, "family": 1, "child": 1}
            async with uow.store(cfg, "topher") as repo:
                raw_child_rows = await repo.raw_rows("child")
                assert raw_child_rows[0]["zite_record_id"] == "c1"
                assert raw_child_rows[0]["dob"] == "2018-05-10"
                raw_sponsor_rows = await repo.raw_rows("sponsor")
                assert raw_sponsor_rows[0]["create"] == (
                    "2026-01-01T10:00:00+00:00"
                )
                after = {
                    entity: await repo.final_rows(entity)
                    for entity in ("child", "family", "sponsor")
                }
            assert db_pipeline._normalized_stores(after) == (
                db_pipeline._normalized_stores(before)
            )
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_acquire_db_rerun_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second acquire writes a fresh snapshot; finals stay untouched."""
    import ccat.zite

    cfg = _prepare(tmp_path)
    records = {
        "sponsor": [raw_sponsor("rec_s1")],
        "family": [raw_family("fam@example.com")],
        "child": [raw_child("c1")],
    }
    table_of = {
        cfg.zite.sponsor: "sponsor",
        cfg.zite.family: "family",
        cfg.zite.child: "child",
    }

    def fake_pull(reference: str) -> pl.DataFrame:
        return _zite_frame(
            cfg, table_of[reference], records[table_of[reference]]
        )

    monkeypatch.setattr(ccat.zite, "get_table_data", fake_pull)

    async def main() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            first = await db_pipeline.acquire(
                cfg, "topher", lambda *a, **k: None, lambda: None
            )
            async with uow.store(cfg, "topher") as repo:
                finals_after_first = {
                    entity: await repo.final_rows(entity)
                    for entity in ("child", "family", "sponsor")
                }
            second = await db_pipeline.acquire(
                cfg, "topher", lambda *a, **k: None, lambda: None
            )
            assert second.raw_rows == first.raw_rows
            async with uow.store(cfg, "topher") as repo:
                finals_after_second = {
                    entity: await repo.final_rows(entity)
                    for entity in ("child", "family", "sponsor")
                }
                assert finals_after_second == finals_after_first
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_acquire_db_missing_source_column_keeps_old_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed pull leaves the previous raw snapshot active (§9.1)."""
    import ccat.zite
    from ccat.acquire import AcquisitionError

    cfg = _prepare(tmp_path)

    async def main() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(repo, raw={"child": [raw_child("old-1")]})
            empty = pl.DataFrame({"unrelated": [1]})
            monkeypatch.setattr(ccat.zite, "get_table_data", lambda ref: empty)
            with pytest.raises(AcquisitionError, match="missing required"):
                await db_pipeline.acquire(
                    cfg, "topher", lambda *a, **k: None, lambda: None
                )
            async with uow.store(cfg, "topher") as repo:
                raw_child_rows = await repo.raw_rows("child")
                assert [row["zite_record_id"] for row in raw_child_rows] == [
                    "old-1"
                ]
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Screen (§9.2).
# ---------------------------------------------------------------------------


_DEFAULT_RAW = {
    "sponsor": [raw_sponsor("rec_s1")],
    "family": [raw_family("fam@example.com")],
    "child": [
        raw_child("c1"),
        raw_child(
            "c2",
            first_name="Two",
            primary_email="fam@example.com",
            dob="2020-12-25",
        ),
    ],
}


def test_screen_db_places_the_assigned_angel_id_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sponsor's claim lands on the child when the data is loaded."""
    cfg = _prepare(tmp_path)
    _use_fake_ai(monkeypatch, {"c1": "null"})
    raw = {
        "sponsor": [
            raw_sponsor(
                "rec_s1",
                email="walmart@example.com",
                prior_assigned_angel_ids="1A",
            ),
            raw_sponsor(
                "rec_s2",
                email="stranger@example.com",
                prior_assigned_angel_ids="99Z",
            ),
        ],
        "family": [raw_family("fam@example.com")],
        "child": [raw_child("c1")],
    }

    async def main() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(repo, raw=raw)
            options = {
                "skip_email_check": True,
                "skip_address_check": True,
            }
            await db_pipeline.screen(
                cfg, "topher", options, lambda *a, **k: None, lambda: None
            )
            async with uow.store(cfg, "topher") as repo:
                children = _by_key(
                    await repo.final_rows("child"), "zite_record_id"
                )
                sponsors = _by_key(
                    await repo.final_rows("sponsor"), "zite_record_id"
                )
            # The claim names the child's angel id, so the child is placed.
            assert children["c1"]["angel_id"] == "1A"
            assert (
                children["c1"]["assigned_sponsor_id"]
                == (sponsors["rec_s1"]["sponsor_id"])
            )
            assert children["c1"]["assignment_source"] == "auto"
            assert sponsors["rec_s1"]["assigned_angel_ids"] == "1A"
            # The other claim names no child, so it needs the audit.
            assert sponsors["rec_s2"]["clarification_notes"] == (
                "Assigned angel ID 99Z names no child"
            )
            assert sponsors["rec_s2"]["status"] == "needs_clarification"
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_screen_db_first_run_appends_finals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First screen seeds finals from raw and screens both children."""
    cfg = _prepare(tmp_path)
    _use_fake_ai(
        monkeypatch,
        {"c1": "null", "c2": json.dumps(_correction_response("c2"))},
    )

    async def main() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(repo, raw=_DEFAULT_RAW)
            options = {
                "skip_email_check": True,
                "skip_address_check": True,
            }
            result = await db_pipeline.screen(
                cfg, "topher", options, lambda *a, **k: None, lambda: None
            )
            assert result.counts.appended == {
                "sponsor": 1,
                "family": 1,
                "child": 2,
            }
            assert sorted(result.ai_targets) == ["c1", "c2"]
            assert sorted(result.ai_succeeded) == ["c1", "c2"]
            async with uow.store(cfg, "topher") as repo:
                children = _by_key(
                    await repo.final_rows("child"), "zite_record_id"
                )
                assert children["c1"]["screened"] is True
                assert children["c1"]["need_1"] == "winter coat taht fits"
                assert children["c2"]["need_1"] == "winter coat that fits"
                assert children["c2"]["potential_anomoly"] == (
                    "age_reported 7 differs from computed 8"
                )
                # Diagnostics snapshot recorded by the run.
                duplicate_rows = await repo.duplicate_log_rows()
                assert duplicate_rows == []  # different first names
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def _correction_response(record_id: str) -> dict[str, Any]:
    """One well-formed model response correcting need_1."""
    from ccat.screening import RESPONSE_COLUMNS

    return {column: None for column in RESPONSE_COLUMNS} | {
        "zite_record_id": record_id,
        "need_1": "winter coat that fits",
        "potential_anomoly": "age_reported 7 differs from computed 8",
    }


def test_screen_db_rerun_zero_ai_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An immediate rerun makes no AI calls and leaves finals intact."""
    cfg = _prepare(tmp_path)
    _use_fake_ai(monkeypatch, {"c1": "null", "c2": "null"})
    options = {"skip_email_check": True, "skip_address_check": True}

    async def main() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(repo, raw=_DEFAULT_RAW)
            await db_pipeline.screen(
                cfg, "topher", options, lambda *a, **k: None, lambda: None
            )
            calls_after_first = list(KeyedFakeOpencode.calls)
            async with uow.store(cfg, "topher") as repo:
                before = {
                    entity: await repo.final_rows(entity)
                    for entity in ("child", "family", "sponsor")
                }
            result = await db_pipeline.screen(
                cfg, "topher", options, lambda *a, **k: None, lambda: None
            )
            assert KeyedFakeOpencode.calls == calls_after_first
            assert result.ai_targets == []
            async with uow.store(cfg, "topher") as repo:
                after = {
                    entity: await repo.final_rows(entity)
                    for entity in ("child", "family", "sponsor")
                }
            assert db_pipeline._normalized_stores(after) == (
                db_pipeline._normalized_stores(before)
            )
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_screen_db_per_child_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each child's screened commit lands before the next AI call."""
    cfg = _prepare(tmp_path)
    seen: dict[str, bool] = {}

    def hook(record_id: str) -> None:
        if record_id == "c2":
            rows = _sql(
                cfg,
                "SELECT zite_record_id, screened FROM final_child",
            )
            seen.update(dict(rows))

    _use_fake_ai(monkeypatch, {"c1": "null", "c2": "null"})
    KeyedFakeOpencode.hook = hook
    options = {"skip_email_check": True, "skip_address_check": True}

    async def main() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(repo, raw=_DEFAULT_RAW)
            await db_pipeline.screen(
                cfg, "topher", options, lambda *a, **k: None, lambda: None
            )
            # When c2's AI call started, c1 was already committed
            # screened; c2 itself was not (per-child commits, §9.2).
            assert seen == {"c1": 1, "c2": 0}
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_screen_db_email_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replayed emails never reach ZeroBounce; new ones do (§6.4)."""
    cfg = _prepare(tmp_path)
    _use_fake_ai(monkeypatch, {"c1": "null", "c2": "null"})
    bulk_calls: list[list[str]] = []

    def fake_bulk(emails: list[str]) -> dict[str, dict[str, str]]:
        bulk_calls.append(list(emails))
        return {
            email: {
                "status": "valid",
                "sub_status": "",
                "did_you_mean": "",
                "raw_response": "{}",
            }
            for email in emails
        }

    monkeypatch.setattr(
        "ccat.engines.zerobounce_email_check.bulk_validate_emails", fake_bulk
    )

    async def main() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(repo, raw=_DEFAULT_RAW)
                await repo.append_email_log(
                    [
                        {
                            "source_sheet": "family_submissions",
                            "zite_record_id": "rec_f_fam@example.com",
                            "field": "primary_email",
                            "email": "fam@example.com",
                            "status": "valid",
                            "sub_status": "",
                            "did_you_mean": "",
                            "response_json": "{}",
                            "checked_at": "2026-01-01T00:00:00+00:00",
                        }
                    ],
                    "seed",
                )
            options: dict[str, Any] = {"skip_address_check": True}
            result = await db_pipeline.screen(
                cfg, "topher", options, lambda *a, **k: None, lambda: None
            )
            assert bulk_calls == [["sponsor@example.com"]]
            assert result.email_checked == 1
            options2: dict[str, Any] = {
                "skip_address_check": True,
            }
            result2 = await db_pipeline.screen(
                cfg, "topher", options2, lambda *a, **k: None, lambda: None
            )
            assert bulk_calls == [["sponsor@example.com"]]
            assert result2.email_checked == 0
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_screen_db_address_replay_and_error_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Addresses replay by exact query; engine error rows persist (D13)."""
    cfg = _prepare(tmp_path)
    _use_fake_ai(monkeypatch, {"c1": "null", "c2": "null"})
    address_calls: list[str] = []

    def fake_check_address(
        street: str | None,
        apt_floor: str | None = None,
        city: str | None = None,
        state: str | None = None,
        zip_code: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        from ccat.engines.nominatim_address_check import build_query

        query = build_query(street, apt_floor, city, state, zip_code)
        address_calls.append(str(street))
        if street == "1 Error St":
            return {
                "query": query,
                "status": "error",
                "display_name": None,
                "lat": None,
                "lon": None,
                "response_json": json.dumps({"error": "HTTP 500"}),
            }
        return {
            "query": query,
            "status": "found",
            "display_name": "1 Main St, Hancock",
            "lat": "47.12",
            "lon": "-88.6",
            "response_json": "[]",
        }

    monkeypatch.setattr(
        "ccat.engines.nominatim_address_check.check_address",
        fake_check_address,
    )

    async def main() -> None:
        error_raw = {
            **_DEFAULT_RAW,
            "family": [
                raw_family("fam@example.com"),
                raw_family(
                    "err@example.com",
                    zite_record_id="rec_f_err",
                    street_address="1 Error St",
                ),
            ],
        }
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(repo, raw=error_raw)
            options: dict[str, Any] = {"skip_email_check": True}
            first = await db_pipeline.screen(
                cfg, "topher", options, lambda *a, **k: None, lambda: None
            )
            assert first.address_checked == 2
            async with uow.store(cfg, "topher") as repo:
                rows = await repo.address_log_rows()
            statuses = sorted(row["status"] for row in rows)
            assert statuses == ["error", "found"]
            error_row = next(row for row in rows if row["status"] == "error")
            assert json.loads(error_row["response_json"]) == {
                "error": "HTTP 500"
            }
            # Rerun: replay by exact query, zero provider calls.
            await db_pipeline.screen(
                cfg, "topher", options, lambda *a, **k: None, lambda: None
            )
            assert len(address_calls) == 2
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_screen_db_diagnostics_snapshot_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """duplicate_log/dob snapshots are current-run, not history (§4.1)."""
    cfg = _prepare(tmp_path)
    _use_fake_ai(monkeypatch, {"c1": "null", "c2": "null"})
    options = {"skip_email_check": True, "skip_address_check": True}

    async def main() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(repo, raw=_DEFAULT_RAW)
                # Poison: a stale run with junk diagnostics.
                await repo.replace_screen_diagnostics(
                    [{"sheet": "junk", "group_key": "x"}], [], "junk"
                )
            await db_pipeline.screen(
                cfg, "topher", options, lambda *a, **k: None, lambda: None
            )
            async with uow.store(cfg, "topher") as repo:
                duplicate_rows = await repo.duplicate_log_rows()
                dob_rows = await repo.dob_log_rows()
            # c1/c2 share the email but not (email, first_name): no dupes.
            assert duplicate_rows == []
            assert dob_rows == []
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_screen_db_duplicate_child_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Duplicate child submissions land in duplicate_log with survivor."""
    cfg = _prepare(tmp_path)
    _use_fake_ai(monkeypatch, {"c1": "null", "c1b": "null"})
    options = {"skip_email_check": True, "skip_address_check": True}

    async def main() -> None:
        dup_raw = {
            "sponsor": [raw_sponsor("rec_s1")],
            "family": [raw_family("fam@example.com")],
            "child": [
                raw_child("c1", created="2026-02-01T09:00:00+00:00"),
                raw_child(
                    "c1b",
                    created="2026-02-02T09:00:00+00:00",
                ),
            ],
        }
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(repo, raw=dup_raw)
            await db_pipeline.screen(
                cfg, "topher", options, lambda *a, **k: None, lambda: None
            )
            async with uow.store(cfg, "topher") as repo:
                duplicate_rows = await repo.duplicate_log_rows()
                children = _by_key(
                    await repo.final_rows("child"), "zite_record_id"
                )
            assert {row["zite_record_id"] for row in duplicate_rows} == {
                "c1",
                "c1b",
            }
            survivors = {
                row["zite_record_id"]: row["survivor"] for row in duplicate_rows
            }
            # Child dedup is latest-wins: c1b survives.
            assert survivors == {"c1": False, "c1b": True}
            assert children["c1"]["potential_duplicate"] is True
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_screen_db_redo_unknown_and_ignored_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown and ignored redo targets raise ScreeningError (§9.2)."""
    cfg = _prepare(tmp_path)
    _use_fake_ai(monkeypatch, {"c1": "null"})
    options = {"skip_email_check": True, "skip_address_check": True}

    async def main() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(repo, raw=_DEFAULT_RAW)
            with pytest.raises(Exception, match="redo target 'nope'"):
                await db_pipeline.screen(
                    cfg,
                    "topher",
                    {**options, "redo": "nope"},
                    lambda *a, **k: None,
                    lambda: None,
                )
            # Seed c1 screened and ignored; redo must refuse.
            async with uow.store(cfg, "migration") as repo:
                children = [child_row("c1", screened=True, ignore=True)]
                await repo.commit_finals(
                    "migrate", "child", children, "migration"
                )
            with pytest.raises(Exception, match="ignored"):
                await db_pipeline.screen(
                    cfg,
                    "topher",
                    {**options, "redo": "c1"},
                    lambda *a, **k: None,
                    lambda: None,
                )
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_screen_db_redo_resets_marker_before_ai(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redo persists screened=False before the AI call (§9.2)."""
    cfg = _prepare(tmp_path)
    marker_at_send: dict[str, int] = {}

    def hook(record_id: str) -> None:
        rows = _sql(
            cfg,
            "SELECT screened FROM final_child WHERE zite_record_id = ?",
            (record_id,),
        )
        marker_at_send[record_id] = rows[0][0]

    _use_fake_ai(monkeypatch, {"c1": "null"})
    KeyedFakeOpencode.hook = hook
    options = {
        "skip_email_check": True,
        "skip_address_check": True,
        "redo": "c1",
    }

    async def main() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(
                    repo,
                    raw={"child": [raw_child("c1")]},
                    children=[child_row("c1", screened=True)],
                )
            result = await db_pipeline.screen(
                cfg, "topher", options, lambda *a, **k: None, lambda: None
            )
            assert result.ai_targets == ["c1"]
            assert marker_at_send == {"c1": 0}
            async with uow.store(cfg, "topher") as repo:
                children = _by_key(
                    await repo.final_rows("child"), "zite_record_id"
                )
            assert children["c1"]["screened"] is True
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_screen_db_overwrite_rescreens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Overwrite re-screens every non-ignored child."""
    cfg = _prepare(tmp_path)
    _use_fake_ai(monkeypatch, {"c1": "null", "c2": "null"})
    options = {
        "skip_email_check": True,
        "skip_address_check": True,
        "overwrite": True,
    }

    async def main() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(
                    repo,
                    raw=_DEFAULT_RAW,
                    children=[
                        child_row("c1", screened=True),
                        child_row("c2", screened=True),
                    ],
                )
            result = await db_pipeline.screen(
                cfg, "topher", options, lambda *a, **k: None, lambda: None
            )
            assert sorted(result.ai_targets) == ["c1", "c2"]
            assert sorted(result.ai_succeeded) == ["c1", "c2"]
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_screen_db_test_mode_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test mode restricts AI targets to the first stable family."""
    cfg = _prepare(tmp_path)
    _use_fake_ai(monkeypatch, {"c1": "null", "c9": "null"})
    options = {
        "skip_email_check": True,
        "skip_address_check": True,
        "test": True,
    }

    async def main() -> None:
        two_family_raw = {
            "sponsor": [raw_sponsor("rec_s1")],
            "family": [
                raw_family(
                    "fam1@example.com", created="2026-01-05T10:00:00+00:00"
                ),
                raw_family(
                    "fam2@example.com", created="2026-01-06T10:00:00+00:00"
                ),
            ],
            "child": [
                raw_child("c1", primary_email="fam1@example.com"),
                raw_child(
                    "c9",
                    primary_email="fam2@example.com",
                    first_name="Other",
                ),
            ],
        }
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(repo, raw=two_family_raw)
            result = await db_pipeline.screen(
                cfg, "topher", options, lambda *a, **k: None, lambda: None
            )
            assert result.ai_targets == ["c1"]
            assert KeyedFakeOpencode.calls == ["c1"]
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_screen_db_cancel_preserves_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancel between children: completed children stay committed (§6.2)."""
    cfg = _prepare(tmp_path)
    # Sequential AI workers make the cancel boundary deterministic.
    cfg = cfg.model_copy(
        update={
            "ai_screening": cfg.ai_screening.model_copy(
                update={"max_workers": 1}
            )
        }
    )
    unblock = threading.Event()
    cancel_flag = threading.Event()

    def hook(record_id: str) -> None:
        # Block c2's AI call until the test has observed c1 committed
        # and armed the cancel flag; with sequential workers the
        # inter-child check then raises before c3 ever starts.
        if record_id == "c2":
            unblock.wait(timeout=10)
            KeyedFakeOpencode.hook = None

    _use_fake_ai(monkeypatch, {"c1": "null", "c2": "null", "c3": "null"})
    KeyedFakeOpencode.hook = hook
    options = {"skip_email_check": True, "skip_address_check": True}

    async def main() -> None:
        three_raw = {
            **_DEFAULT_RAW,
            "child": [
                raw_child("c1"),
                raw_child(
                    "c2",
                    first_name="Two",
                    primary_email="fam@example.com",
                    created="2026-02-01T10:00:00+00:00",
                ),
                raw_child(
                    "c3",
                    first_name="Three",
                    primary_email="fam@example.com",
                    created="2026-02-01T11:00:00+00:00",
                ),
            ],
        }

        def cancel() -> None:
            if cancel_flag.is_set():
                raise JobCancelRequested("requested")

        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(repo, raw=three_raw)
            screen_task = asyncio.create_task(
                db_pipeline.screen(
                    cfg, "topher", options, lambda *a, **k: None, cancel
                )
            )
            # Wait until c1 is committed, then arm cancellation and
            # release c2; with sequential workers persist(c2) raises
            # at the next boundary before c3 ever starts (§6.2).
            for _ in range(3000):
                rows = _sql_retry(
                    cfg,
                    "SELECT screened FROM final_child WHERE zite_record_id = ?",
                    ("c1",),
                )
                if rows and rows[0][0]:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("c1 was never committed screened")
            cancel_flag.set()
            unblock.set()
            with pytest.raises(JobCancelRequested):
                await screen_task
            await asyncio.sleep(0.05)
            rows = _sql(
                cfg,
                "SELECT zite_record_id, screened FROM final_child ORDER BY zite_record_id",
            )
            screened = dict(rows)
            assert screened["c1"] == 1
            assert screened["c2"] == 1
            assert screened["c3"] == 0  # never attempted
        finally:
            unblock.set()
            await uow.engine.dispose()

    asyncio.run(main())


def test_screen_db_preserves_human_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AI corrections skip human-edited fields (§7.3 via DB save)."""
    cfg = _prepare(tmp_path)
    _use_fake_ai(
        monkeypatch,
        {"c1": json.dumps(_correction_response("c1"))},
    )
    options = {"skip_email_check": True, "skip_address_check": True}

    async def main() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(
                    repo,
                    raw={"child": [raw_child("c1")]},
                    children=[child_row("c1")],
                )
            async with uow.store(cfg, "auditor") as repo:
                await save_entity_db(
                    repo,
                    cfg,
                    "child",
                    "c1",
                    SavePatch(need_1="human-fixed coat"),
                    "auditor",
                )
            async with uow.store(cfg, "auditor") as repo:
                children = _by_key(
                    await repo.final_rows("child"), "zite_record_id"
                )
            assert children["c1"]["_last_updated_by"] == "auditor"
            await db_pipeline.screen(
                cfg, "topher", options, lambda *a, **k: None, lambda: None
            )
            async with uow.store(cfg, "topher") as repo:
                children = _by_key(
                    await repo.final_rows("child"), "zite_record_id"
                )
            assert children["c1"]["need_1"] == "human-fixed coat"
            assert children["c1"]["screened"] is True
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_validate_screen_options_precheck(tmp_path: Path) -> None:
    """The sync pre-check rejects unknown/ignored redo targets (§9.2)."""
    from ccat.screening import ScreeningError

    snapshot = {
        "child": [child_row("c1", ignore=True)],
        "raw_child": [raw_child("c1")],
    }
    with pytest.raises(ScreeningError, match="ignored"):
        db_pipeline.validate_screen_options(snapshot, redo="c1")
    with pytest.raises(ScreeningError, match="not found"):
        db_pipeline.validate_screen_options(snapshot, redo="nope")
    ok = {"child": [child_row("c1")], "raw_child": [raw_child("c1")]}
    db_pipeline.validate_screen_options(ok, redo="c1")
    db_pipeline.validate_screen_options(ok)


# ---------------------------------------------------------------------------
# Match (§9.4, D8).
# ---------------------------------------------------------------------------


_MATCH_FINALS: dict[str, Any] = dict(
    children=[
        child_row("c1", angel_id="1A", status="audited", age_at_xmas=8),
        child_row(
            "c2",
            first_name="Two",
            angel_id="1B",
            status="audited",
            age_at_xmas=6,
        ),
    ],
    families=[family_row(1)],
    sponsors=[sponsor_row(1)],
)


def _match_cfg(tmp_path: Path) -> Config:
    """Prepare a DB config for match tests."""
    return _prepare(tmp_path)


def test_match_db_assigns_and_records_artifact(tmp_path: Path) -> None:
    """Full match: assignments committed, run archived, CSV registered."""
    cfg = _match_cfg(tmp_path)

    async def main() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(repo, **_MATCH_FINALS)
            result = await db_pipeline.match(
                cfg, "topher", {}, lambda *a, **k: None, lambda: None
            )
            assert result.matched_families == 1
            assert result.matched_sponsors == 1
            assert result.assigned_angel_ids == ["1A", "1B"]
            assert result.archived is True
            assert result.run_id is not None
            assert result.csv_export_ok is True
            async with uow.store(cfg, "topher") as repo:
                families = _by_key(await repo.final_rows("family"), "family_id")
                sponsors = _by_key(
                    await repo.final_rows("sponsor"), "sponsor_id"
                )
                artifacts = await repo.artifact_rows("csv_export")
                report_rows = await repo.match_report_rows()
            assert families["1"]["assigned_sponsor_id"] == 1
            assert families["1"]["assigned_angel_ids"] == "1A,1B"
            assert sponsors["1"]["assigned_angel_ids"] == "1A,1B"
            assert sponsors["1"]["num_angels_assigned"] == 2
            assert len(artifacts) == 1
            assert artifacts[0]["meta"]["match_run_id"] == result.run_id
            # Perfect pool: nothing to report; the run itself is recorded.
            assert report_rows == []
            # The child row is the assignment grain (§9.4).
            children = _by_key(await repo.final_rows("child"), "zite_record_id")
            assert children["c1"]["screened"] is False
            assert children["c1"]["assigned_sponsor_id"] == 1
            assert children["c1"]["assignment_source"] == "auto"
            assert children["c2"]["assigned_sponsor_id"] == 1
            assert children["c2"]["assignment_source"] == "auto"
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_match_db_counts_a_shared_angel_letter_once(tmp_path: Path) -> None:
    """A pending duplicate shares its group's letter: one family angel.

    Both submissions are assigned children, so the sponsor summary counts
    two children; the family size counts the one letter (D2).
    """
    cfg = _match_cfg(tmp_path)

    async def main() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(
                    repo,
                    children=[
                        child_row(
                            "c1", angel_id="1A", status="audited", age_at_xmas=8
                        ),
                        child_row(
                            "c2",
                            first_name="Two",
                            angel_id="1A",
                            status="audited",
                            age_at_xmas=6,
                        ),
                    ],
                    families=[family_row(1)],
                    sponsors=[sponsor_row(1, num_children=1)],
                )
            await db_pipeline.match(
                cfg, "topher", {}, lambda *a, **k: None, lambda: None
            )
            async with uow.store(cfg, "topher") as repo:
                families = _by_key(await repo.final_rows("family"), "family_id")
                sponsors = _by_key(
                    await repo.final_rows("sponsor"), "sponsor_id"
                )
                children = _by_key(
                    await repo.final_rows("child"), "zite_record_id"
                )
            assert families["1"]["num_angels_in_family"] == 1
            assert families["1"]["assigned_angel_ids"] == "1A,1A"
            assert sponsors["1"]["assigned_angel_ids"] == "1A,1A"
            assert sponsors["1"]["num_angels_assigned"] == 2
            for record_id in ("c1", "c2"):
                assert children[record_id]["assigned_sponsor_id"] == 1
                assert children[record_id]["assignment_source"] == "auto"
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_match_db_clears_stale_automatic_assignments(tmp_path: Path) -> None:
    """A run recomputes its own assignments; the unmatched family clears."""
    cfg = _match_cfg(tmp_path)

    async def main() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(
                    repo,
                    children=[
                        child_row(
                            "c1",
                            angel_id="1A",
                            status="audited",
                            age_at_xmas=8,
                            assigned_sponsor_id=9,
                            assignment_source="auto",
                        ),
                        child_row(
                            "c2",
                            first_name="Two",
                            angel_id="2A",
                            family_id=2,
                            status="audited",
                            age_at_xmas=8,
                            assigned_sponsor_id=9,
                            assignment_source="auto",
                        ),
                    ],
                    families=[family_row(1), family_row(2)],
                    sponsors=[sponsor_row(1, num_children=1)],
                )
            await db_pipeline.match(
                cfg, "topher", {}, lambda *a, **k: None, lambda: None
            )
            async with uow.store(cfg, "topher") as repo:
                children = _by_key(
                    await repo.final_rows("child"), "zite_record_id"
                )
            assert children["c1"]["assigned_sponsor_id"] == 1
            assert children["c1"]["assignment_source"] == "auto"
            assert children["c2"]["assigned_sponsor_id"] is None
            assert children["c2"]["assignment_source"] is None
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_match_db_manual_assignment_leaves_pool(tmp_path: Path) -> None:
    """A manual child protects its whole family and survives the run (D3)."""
    cfg = _match_cfg(tmp_path)

    async def main() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(
                    repo,
                    children=[
                        child_row(
                            "c1",
                            angel_id="1A",
                            status="audited",
                            age_at_xmas=8,
                            assigned_sponsor_id=2,
                            assignment_source="manual",
                        ),
                        child_row(
                            "c2",
                            first_name="Two",
                            angel_id="1B",
                            status="audited",
                            age_at_xmas=8,
                        ),
                        child_row(
                            "c3",
                            first_name="Three",
                            angel_id="2A",
                            family_id=2,
                            status="audited",
                            age_at_xmas=8,
                        ),
                    ],
                    families=[family_row(1), family_row(2)],
                    sponsors=[
                        sponsor_row(1, num_children=1),
                        sponsor_row(2, num_children=1),
                    ],
                )
            await db_pipeline.match(
                cfg, "topher", {}, lambda *a, **k: None, lambda: None
            )
            async with uow.store(cfg, "topher") as repo:
                children = _by_key(
                    await repo.final_rows("child"), "zite_record_id"
                )
                families = _by_key(await repo.final_rows("family"), "family_id")
                report_rows = await repo.match_report_rows()
            assert children["c1"]["assigned_sponsor_id"] == 2
            assert children["c1"]["assignment_source"] == "manual"
            assert children["c2"]["assigned_sponsor_id"] is None
            assert children["c3"]["assigned_sponsor_id"] == 1
            assert children["c3"]["assignment_source"] == "auto"
            assert families["1"]["assigned_sponsor_id"] == 2
            assert families["1"]["assigned_angel_ids"] == "1A"
            reasons = {
                row["ref"]: row["reason"]
                for row in report_rows or []
                if row["kind"] == "family"
            }
            assert reasons["1"] == "manual assignment"
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_match_db_held_family_leaves_pool(tmp_path: Path) -> None:
    """A held family leaves the pool and is reported with its reason (D14)."""
    cfg = _match_cfg(tmp_path)

    async def main() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(repo, **_MATCH_FINALS)
                await repo.record_family_hold(1, "needs review", "alice")
            result = await db_pipeline.match(
                cfg, "topher", {}, lambda *a, **k: None, lambda: None
            )
            async with uow.store(cfg, "topher") as repo:
                children = _by_key(
                    await repo.final_rows("child"), "zite_record_id"
                )
                report_rows = await repo.match_report_rows()
            assert result.matched_families == 0
            assert result.unmatched_family_ids == [1]
            assert children["c1"]["assigned_sponsor_id"] is None
            assert children["c2"]["assigned_sponsor_id"] is None
            reasons = {
                row["ref"]: row["reason"]
                for row in report_rows or []
                if row["kind"] == "family"
            }
            assert reasons["1"] == "held by alice: needs review"
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_match_db_reports_split_families_at_child_grain(
    tmp_path: Path,
) -> None:
    """The run result separates split families from matched and unmatched."""
    cfg = _match_cfg(tmp_path)

    async def main() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(
                    repo,
                    children=[
                        child_row(
                            "c1",
                            angel_id="1A",
                            status="audited",
                            age_at_xmas=8,
                            assigned_sponsor_id=1,
                            assignment_source="manual",
                        ),
                        child_row(
                            "c2",
                            first_name="Two",
                            angel_id="1B",
                            status="audited",
                            age_at_xmas=8,
                            assigned_sponsor_id=2,
                            assignment_source="manual",
                        ),
                    ],
                    families=[family_row(1)],
                    sponsors=[
                        sponsor_row(1, num_children=1),
                        sponsor_row(2, num_children=1),
                    ],
                )
            result = await db_pipeline.match(
                cfg,
                "topher",
                {"skip_match": True},
                lambda *a, **k: None,
                lambda: None,
            )
            async with uow.store(cfg, "topher") as repo:
                families = _by_key(await repo.final_rows("family"), "family_id")
                sponsors = _by_key(
                    await repo.final_rows("sponsor"), "sponsor_id"
                )
            assert result.matched_families == 0
            assert result.unmatched_family_ids == []
            assert result.split_family_ids == [1]
            assert families["1"]["assigned_sponsor_id"] is None
            assert families["1"]["assigned_angel_ids"] == "1A,1B"
            assert sponsors["1"]["assigned_angel_ids"] == "1A"
            assert sponsors["2"]["assigned_angel_ids"] == "1B"
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_match_db_commit_atomicity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A report-row failure rolls the whole run back (§9.4)."""

    def poison_warning(
        family_pool: list[dict[str, Any]],
        sponsor_pool: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        return [{"kind": "duplicate", "ref": "1", "reason": "x", "bogus": "1"}]

    monkeypatch.setattr(db_pipeline, "_duplicate_pool_warnings", poison_warning)
    cfg = _match_cfg(tmp_path)

    async def main() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(repo, **_MATCH_FINALS)
            with pytest.raises(KeyValidationError, match="match_report"):
                await db_pipeline.match(
                    cfg, "topher", {}, lambda *a, **k: None, lambda: None
                )
            # Nothing committed: no assignments, no run, no archive.
            async with uow.store(cfg, "topher") as repo:
                families = _by_key(await repo.final_rows("family"), "family_id")
                report_rows = await repo.match_report_rows()
            assert families["1"]["assigned_sponsor_id"] is None
            assert report_rows is None
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_match_db_skip_match_reconciles_only(tmp_path: Path) -> None:
    """skip_match: prior assignments propagate; no run/report (§9.4)."""
    cfg = _match_cfg(tmp_path)

    async def main() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(
                    repo,
                    **_MATCH_FINALS,
                )
                await repo.commit_finals(
                    "migrate",
                    "sponsor",
                    [sponsor_row(1, prior_assigned_angel_ids="1A,1B")],
                    "migration",
                )
            result = await db_pipeline.match(
                cfg,
                "topher",
                {"skip_match": True},
                lambda *a, **k: None,
                lambda: None,
            )
            assert result.report_rows == 0
            assert result.archived is False
            assert result.run_id is None
            async with uow.store(cfg, "topher") as repo:
                sponsors = _by_key(
                    await repo.final_rows("sponsor"), "sponsor_id"
                )
                families = _by_key(await repo.final_rows("family"), "family_id")
                report_rows = await repo.match_report_rows()
            assert sponsors["1"]["assigned_angel_ids"] == "1A,1B"
            assert sponsors["1"]["num_angels_assigned"] == 2
            assert families["1"]["assigned_sponsor_id"] == 1
            assert report_rows is None  # report untouched
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_match_db_test_mode_first_sponsor_only(tmp_path: Path) -> None:
    """Test mode matches the first sponsor in phase order + test row."""
    cfg = _match_cfg(tmp_path)

    async def main() -> None:
        two_sponsors: dict[str, Any] = dict(
            children=[
                child_row("c1", angel_id="1A", status="audited"),
                child_row(
                    "c2",
                    first_name="Two",
                    angel_id="1B",
                    status="audited",
                    age_at_xmas=6,
                ),
            ],
            families=[family_row(1)],
            sponsors=[sponsor_row(1), sponsor_row(2)],
        )
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(repo, **two_sponsors)
            result = await db_pipeline.match(
                cfg,
                "topher",
                {"test": True},
                lambda *a, **k: None,
                lambda: None,
            )
            assert result.run_id is not None
            async with uow.store(cfg, "topher") as repo:
                report_rows = await repo.match_report_rows()
                sponsors = _by_key(
                    await repo.final_rows("sponsor"), "sponsor_id"
                )
            kinds = {row["kind"] for row in report_rows or []}
            assert "test" in kinds
            assert sponsors["1"]["num_angels_assigned"] == 2
            assert sponsors["2"]["num_angels_assigned"] == 0
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_match_db_revision_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A family row_version bump after the snapshot → MatchError (D8)."""
    from ccat.match import MatchError

    cfg = _prepare(tmp_path)
    original_match_sponsors = db_pipeline.match_sponsors
    db_path = _db_path(cfg)

    def bump_and_match(*args: Any, **kwargs: Any) -> Any:
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                "UPDATE final_family SET row_version = row_version + 1"
                " WHERE family_id = 1"
            )
            connection.commit()
        finally:
            connection.close()
        return original_match_sponsors(*args, **kwargs)

    monkeypatch.setattr(db_pipeline, "match_sponsors", bump_and_match)

    async def main() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(repo, **_MATCH_FINALS)
            with pytest.raises(MatchError, match="changed while matching"):
                await db_pipeline.match(
                    cfg, "topher", {}, lambda *a, **k: None, lambda: None
                )
            async with uow.store(cfg, "topher") as repo:
                families = _by_key(await repo.final_rows("family"), "family_id")
                report_rows = await repo.match_report_rows()
            assert families["1"]["assigned_sponsor_id"] is None
            assert report_rows is None
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_match_db_child_revision_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child row_version bump after the snapshot → MatchError (D8)."""
    from ccat.match import MatchError

    cfg = _prepare(tmp_path)
    original_match_sponsors = db_pipeline.match_sponsors
    db_path = _db_path(cfg)

    def bump_and_match(*args: Any, **kwargs: Any) -> Any:
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                "UPDATE final_child SET row_version = row_version + 1"
                " WHERE zite_record_id = 'c1'"
            )
            connection.commit()
        finally:
            connection.close()
        return original_match_sponsors(*args, **kwargs)

    monkeypatch.setattr(db_pipeline, "match_sponsors", bump_and_match)

    async def main() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(repo, **_MATCH_FINALS)
            with pytest.raises(MatchError, match="final_child rows changed"):
                await db_pipeline.match(
                    cfg, "topher", {}, lambda *a, **k: None, lambda: None
                )
            async with uow.store(cfg, "topher") as repo:
                children = _by_key(
                    await repo.final_rows("child"), "zite_record_id"
                )
            assert children["c1"]["assigned_sponsor_id"] is None
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_match_db_csv_failure_is_explicit_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CSV artifact failure after commit → csv_export_ok=False (§9.4)."""

    async def broken_register(*args: Any, **kwargs: Any) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(db_pipeline, "register_file", broken_register)
    cfg = _match_cfg(tmp_path)

    async def main() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(repo, **_MATCH_FINALS)
            result = await db_pipeline.match(
                cfg, "topher", {}, lambda *a, **k: None, lambda: None
            )
            assert result.csv_export_ok is False
            # Assignments stay committed despite the artifact failure.
            async with uow.store(cfg, "topher") as repo:
                families = _by_key(await repo.final_rows("family"), "family_id")
            assert families["1"]["assigned_sponsor_id"] == 1
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# save_entity_db (§9.3 over the DB).
# ---------------------------------------------------------------------------


def test_save_entity_db_field_patch_and_stamp(tmp_path: Path) -> None:
    """A field patch persists, stamps audited_at, bumps row_version."""
    cfg = _prepare(tmp_path)

    async def main() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(
                    repo,
                    raw={"child": [raw_child("c1")]},
                    children=[child_row("c1")],
                )
            async with uow.store(cfg, "auditor") as repo:
                result = await save_entity_db(
                    repo,
                    cfg,
                    "child",
                    "c1",
                    SavePatch(first_name="Renamed"),
                    "auditor",
                )
            assert result.status == "in_progress"
            assert result.applied == {"first_name": "Renamed"}
            assert result.audited_at is not None
            async with uow.store(cfg, "auditor") as repo:
                children = _by_key(
                    await repo.final_rows("child"), "zite_record_id"
                )
            assert children["c1"]["first_name"] == "Renamed"
            assert children["c1"]["_row_version"] == 2
            assert children["c1"]["_last_updated_by"] == "auditor"
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_save_entity_db_tri_state(tmp_path: Path) -> None:
    """Omitted keeps the stored value; explicit null clears (§9.3)."""
    cfg = _prepare(tmp_path)

    async def main() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(
                    repo,
                    raw={"child": [raw_child("c1")]},
                    children=[
                        child_row(
                            "c1",
                            first_name="Kept",
                            other_notes="note text",
                        )
                    ],
                )
            async with uow.store(cfg, "auditor") as repo:
                # Omitted other_notes keeps it; explicit null clears.
                await save_entity_db(
                    repo, cfg, "child", "c1", SavePatch(first_name="Again"), "a"
                )
                await save_entity_db(
                    repo, cfg, "child", "c1", SavePatch(other_notes=None), "a"
                )
            async with uow.store(cfg, "auditor") as repo:
                children = _by_key(
                    await repo.final_rows("child"), "zite_record_id"
                )
            assert children["c1"]["first_name"] == "Again"
            assert children["c1"]["other_notes"] is None
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_save_entity_db_ignore_and_unignore(tmp_path: Path) -> None:
    """Ignore reduces to the skeleton; unignore reseeds (§5.5)."""
    from ccat.services.db_audit import ignore_entity_db, unignore_entity_db

    cfg = _prepare(tmp_path)

    async def main() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(
                    repo,
                    raw={"child": [raw_child("c1")]},
                    children=[child_row("c1", need_1="precious edit")],
                )
            async with uow.store(cfg, "auditor") as repo:
                await ignore_entity_db(
                    repo, cfg, "child", "c1", "auditor", confirm=True
                )
            async with uow.store(cfg, "auditor") as repo:
                children = _by_key(
                    await repo.final_rows("child"), "zite_record_id"
                )
            ignored = children["c1"]
            assert ignored["status"] == "ignored"
            assert ignored["need_1"] is None  # skeleton: data nulled
            assert ignored["first_name"] == "Kiddo"  # retained column
            assert ignored["angel_id"] is None
            # Unignore reseeds from the raw baseline; previous edits
            # stay lost (§5.5).
            async with uow.store(cfg, "auditor") as repo:
                result = await unignore_entity_db(
                    repo, cfg, "child", "c1", "auditor", confirm=True
                )
            assert result.status in {"new", "in_progress"}
            async with uow.store(cfg, "auditor") as repo:
                children = _by_key(
                    await repo.final_rows("child"), "zite_record_id"
                )
            assert children["c1"]["need_1"] == "winter coat taht fits"
            assert children["c1"]["ignore"] is False
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_save_entity_db_preserves_module_owned(tmp_path: Path) -> None:
    """screened/marker columns survive an audit save (§4.4)."""
    cfg = _prepare(tmp_path)

    async def main() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(
                    repo,
                    raw={"child": [raw_child("c1")]},
                    children=[child_row("c1", screened=True)],
                )
            async with uow.store(cfg, "auditor") as repo:
                await save_entity_db(
                    repo,
                    cfg,
                    "child",
                    "c1",
                    SavePatch(other_notes=None),
                    "auditor",
                )
            async with uow.store(cfg, "auditor") as repo:
                children = _by_key(
                    await repo.final_rows("child"), "zite_record_id"
                )
            assert children["c1"]["screened"] is True
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Reconcile service (§9.3).
# ---------------------------------------------------------------------------


def test_reconcile_db_seeds_missing_only(tmp_path: Path) -> None:
    """The explicit backstop seeds missing entities; rerun is a no-op."""
    cfg = _prepare(tmp_path)

    async def main() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_stores(
                    repo,
                    raw={"child": [raw_child("c1")], **{}},
                )
            result = await db_pipeline.reconcile(
                cfg, "topher", lambda *a, **k: None, lambda: None
            )
            assert result.counts.appended["child"] == 1
            async with uow.store(cfg, "topher") as repo:
                children = await repo.final_rows("child")
            assert len(children) == 1
            # Rerun: nothing appended, nothing merged.
            result2 = await db_pipeline.reconcile(
                cfg, "topher", lambda *a, **k: None, lambda: None
            )
            assert result2.counts.appended == {
                "sponsor": 0,
                "family": 0,
                "child": 0,
            }
            assert result2.counts.merged["child"] == 0
        finally:
            await uow.engine.dispose()

    asyncio.run(main())
