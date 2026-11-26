"""Contract tests for the DB-era SeasonStore repository (Phase 2b).

File-backed SQLite in ``tmp_path`` with a real async engine, migrated
via Alembic and seeded via the bootstrap users.  Every test runs one
``asyncio.run`` body that owns the engine's event loop and disposes it
before returning, so no connection outlives its loop.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from conftest import load_test_cfg
from ccat.audit.store import ENTITY_SPECS, normalize_row
from ccat.config import Config, StorageSpec
from ccat.db.engine import (
    create_async_engine,
    create_sync_engine,
    run_migrations,
    sync_url_from,
)
from ccat.db.errors import (
    DbStoreError,
    EntityRemovalError,
    KeyValidationError,
    OwnershipError,
    StaleRowVersionError,
)
from ccat.db.models import (
    AcquisitionRun,
    FinalChild,
    FinalSponsor,
    MatchAssignmentSnapshot,
    MatchRun,
    RawChild,
    Season,
)
from ccat.db.repositories import (
    SeasonStoreRepository,
    ensure_season,
)
from ccat.db.seed import seed_bootstrap
from ccat.db.uow import UnitOfWork


# ---------------------------------------------------------------------------
# Fixtures: config, engine bootstrap, and legacy-vocabulary row builders.
# ---------------------------------------------------------------------------


def _db_cfg(tmp_path: Path) -> Config:
    """Build a test config whose storage points at a temp SQLite file."""
    return load_test_cfg(
        excel_file=str(tmp_path / "wb.xlsx"),
        log_file=str(tmp_path / "ccat.log"),
        match_report_csv=str(tmp_path / "report.csv"),
        storage=StorageSpec(
            mode="db",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'ccat.sqlite3'}",
        ),
    )


def _prepare(tmp_path: Path) -> Config:
    """Migrate the temp database and seed the bootstrap identities."""
    cfg = _db_cfg(tmp_path)
    run_migrations(cfg.storage.database_url)
    engine = create_sync_engine(sync_url_from(cfg.storage.database_url))
    try:
        seed_bootstrap(engine)
    finally:
        engine.dispose()
    return cfg


def _columns(entity: str) -> tuple[str, ...]:
    """Return the entity's legacy dict column vocabulary."""
    return tuple(ENTITY_SPECS[entity]["columns"])


def child_row(record_id: str | None, **overrides: Any) -> dict[str, Any]:
    """Build one full final-child row under legacy dict names."""
    base: dict[str, Any] = {column: None for column in _columns("child")}
    base.update(
        zite_record_id=record_id,
        family_id=1,
        angel_id="1A",
        first_name="Kiddo",
        primary_email="fam@example.com",
        dob="2018-05-10",
        age_at_xmas=8,
        need_1="winter coat",
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
        created="2026-01-05T10:00:00Z",
        primary_email=f"fam{family_id}@example.com",
        first_name="Jane",
        last_name="Doe",
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
        num_children=2,
        age_preference="Any Age",
        pref_age_min=0,
        pref_age_max=18,
        status="new",
        ignore=False,
        potential_duplicate=False,
        accepts_disclaimer=True,
    )
    base.update(overrides)
    return base


CHILDREN: list[dict[str, Any]] = [
    child_row("c1", potential_anomoly="age mismatch"),
    child_row("c2", angel_id=None, dob="2020-12-25", age_at_xmas=6),
    child_row("orphan1", family_id=None, angel_id=None),
]
FAMILIES: list[dict[str, Any]] = [family_row(1), family_row(2)]
SPONSORS: list[dict[str, Any]] = [
    sponsor_row(1),
    sponsor_row(
        2,
        age_preference="Middle School (11-13 yrs)",
        pref_age_min=11,
        pref_age_max=13,
    ),
]


async def _seed(repo: SeasonStoreRepository) -> None:
    """Commit the fixture final stores through the migrate module."""
    await repo.commit_finals("migrate", "child", CHILDREN, "migration")
    await repo.commit_finals("migrate", "family", FAMILIES, "migration")
    await repo.commit_finals("migrate", "sponsor", SPONSORS, "migration")


def _plain(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip the underscore metadata keys from read rows."""
    return [
        {key: value for key, value in row.items() if not key.startswith("_")}
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Season bootstrap.
# ---------------------------------------------------------------------------


def test_ensure_season_idempotent_and_hash_tracks_content(
    tmp_path: Path,
) -> None:
    """Re-ensuring returns the same row; a content change re-hashes."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        private = await ensure_season(cfg, "migration")
        engine = create_async_engine(cfg.storage.database_url)
        try:
            uow = UnitOfWork(engine)
            again = await ensure_season(cfg, "migration", engine=engine)
            assert again.id == private.id
            assert again.config_hash == private.config_hash

            changed_cfg = cfg.model_copy(
                update={"log_file": str(tmp_path / "other.log")}
            )
            updated = await ensure_season(
                changed_cfg, "migration", engine=engine
            )
            assert updated.id == private.id
            assert updated.config_hash != private.config_hash
            assert updated.config_json == changed_cfg.model_dump(mode="json")

            async with uow.session() as session:
                seasons = (await session.scalars(select(Season))).all()
            assert [season.id for season in seasons] == [private.id]
        finally:
            await engine.dispose()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Round-trip and the two renames.
# ---------------------------------------------------------------------------


def test_round_trip_full_final_stores(tmp_path: Path) -> None:
    """Migrate commits read back equivalent, normalized, versioned."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                assert (
                    await repo.commit_finals(
                        "migrate", "child", CHILDREN, "migration"
                    )
                    == 3
                )
                await repo.commit_finals(
                    "migrate", "family", FAMILIES, "migration"
                )
                await repo.commit_finals(
                    "migrate", "sponsor", SPONSORS, "migration"
                )
            async with uow.store(cfg, "audit") as repo:
                for entity, originals in (
                    ("child", CHILDREN),
                    ("family", FAMILIES),
                    ("sponsor", SPONSORS),
                ):
                    rows = await repo.final_rows(entity)
                    assert len(rows) == len(originals)
                    by_key = {
                        row[ENTITY_SPECS[entity]["key"]]: row for row in rows
                    }
                    for original in originals:
                        key = original[ENTITY_SPECS[entity]["key"]]
                        row = by_key[key]
                        assert _plain([row]) == [
                            normalize_row(entity, original)
                        ]
                        assert row["_row_version"] == 1
                        assert row["_last_updated_by"] == "migration"
                        assert row["_last_updated_at"] is not None
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_rename_adapters_at_the_boundary(tmp_path: Path) -> None:
    """Legacy potential_anomoly/create map to ai_screening_notes/created."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
                child = (await repo.final_rows("child"))[0]
                assert child["potential_anomoly"] == "age mismatch"
                sponsor = (await repo.final_rows("sponsor"))[0]
                assert sponsor["create"] == "2026-01-01T10:00:00Z"
                await repo.replace_raw_snapshot(
                    "acquire",
                    {
                        "sponsor": [
                            {
                                "zite_record_id": "rec_s1",
                                "create": "2026-01-01T10:00:00Z",
                                "email": "sponsor1@example.com",
                            }
                        ]
                    },
                    "acquire",
                )
                raw = (await repo.raw_rows("sponsor"))[0]
                assert raw["create"] == "2026-01-01T10:00:00Z"
            async with uow.session() as session:
                season = await session.scalar(
                    select(Season).where(Season.name == cfg.season)
                )
                assert season is not None
                child = await session.scalar(
                    select(FinalChild).where(
                        FinalChild.season_id == season.id,
                        FinalChild.zite_record_id == "c1",
                    )
                )
                assert child is not None
                assert child.ai_screening_notes == "age mismatch"
                sponsor = await session.scalar(
                    select(FinalSponsor).where(
                        FinalSponsor.season_id == season.id,
                        FinalSponsor.sponsor_id == 1,
                    )
                )
                assert sponsor is not None
                assert sponsor.created == "2026-01-01T10:00:00Z"
                raw_sponsor = (await session.scalars(select(RawChild))).all()
                assert raw_sponsor == []
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Ownership.
# ---------------------------------------------------------------------------


def test_audit_cannot_change_assignment_columns(tmp_path: Path) -> None:
    """Audit owns editable+control+computed columns only."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
            async with uow.store(cfg, "auditor") as repo:
                rows = await repo.final_rows("family")
                rows[0]["assigned_sponsor_id"] = 9
                with pytest.raises(OwnershipError) as excinfo:
                    await repo.commit_finals("audit", "family", rows, "auditor")
                assert excinfo.value.columns == ("assigned_sponsor_id",)
                assert excinfo.value.entity == "family"
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_match_writes_only_child_assignment_columns(tmp_path: Path) -> None:
    """Match writes the child's assignment columns, nothing else."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
            async with uow.store(cfg, "matcher") as repo:
                rows = [dict(row) for row in CHILDREN]
                rows[0]["assigned_sponsor_id"] = 7
                rows[0]["assignment_source"] = "auto"
                await repo.commit_finals("match", "child", rows, "matcher")
                with pytest.raises(OwnershipError):
                    bad = [dict(row) for row in CHILDREN]
                    bad[0]["first_name"] = "Changed"
                    await repo.commit_finals("match", "child", bad, "matcher")
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_manual_match_owns_only_assignment_columns(tmp_path: Path) -> None:
    """The manual matcher may place a child, and may touch nothing else."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
            async with uow.store(cfg, "coordinator") as repo:
                rows = [dict(row) for row in CHILDREN]
                rows[0]["assigned_sponsor_id"] = 7
                rows[0]["assignment_source"] = "manual"
                await repo.commit_finals(
                    "manual_match", "child", rows, "coordinator"
                )
                with pytest.raises(OwnershipError):
                    bad = [dict(row) for row in CHILDREN]
                    bad[0]["first_name"] = "Changed"
                    await repo.commit_finals(
                        "manual_match", "child", bad, "coordinator"
                    )
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_generate_owns_only_generated_info_sheet(tmp_path: Path) -> None:
    """Generate may flip the marker but not controls."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
            async with uow.store(cfg, "generator") as repo:
                rows = [dict(row) for row in CHILDREN]
                rows[0]["generated_info_sheet"] = True
                assert (
                    await repo.commit_finals(
                        "generate", "child", rows, "generator"
                    )
                    == 1
                )
                marked = {
                    row["zite_record_id"]: row
                    for row in await repo.final_rows("child")
                }
                assert marked["c1"]["generated_info_sheet"] is True
                assert marked["c1"]["_row_version"] == 2

                rows = [dict(row) for row in CHILDREN]
                rows[0]["status"] = "audited"
                with pytest.raises(OwnershipError) as excinfo:
                    await repo.commit_finals(
                        "generate", "child", rows, "generator"
                    )
                assert excinfo.value.columns == ("status",)
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_screen_and_migrate_may_change_all_columns(tmp_path: Path) -> None:
    """The broad writers can touch source, control, and module columns."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
            async with uow.store(cfg, "screen") as repo:
                rows = [dict(row) for row in CHILDREN]
                rows[0].update(
                    first_name="Kid",
                    need_1="new coat",
                    status="in_progress",
                    screened=True,
                )
                assert (
                    await repo.commit_finals("screen", "child", rows, "screen")
                    == 1
                )
                families = [dict(row) for row in FAMILIES]
                families[0]["assigned_sponsor_id"] = 2
                assert (
                    await repo.commit_finals(
                        "screen", "family", families, "screen"
                    )
                    == 1
                )
                fresh = {
                    row["zite_record_id"]: row
                    for row in await repo.final_rows("child")
                }
                assert fresh["c1"]["screened"] is True
                assert fresh["c1"]["_row_version"] == 2
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_audit_positive_path_changes_editable_and_controls(
    tmp_path: Path,
) -> None:
    """A versioned audit save updates editable fields and controls."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
            async with uow.store(cfg, "alice") as repo:
                rows = await repo.final_rows("child")
                rows[0].update(need_1="human edit", status="in_progress")
                assert (
                    await repo.commit_finals("audit", "child", rows, "alice")
                    == 1
                )
                saved = {
                    row["zite_record_id"]: row
                    for row in await repo.final_rows("child")
                }
                assert saved["c1"]["need_1"] == "human edit"
                assert saved["c1"]["_row_version"] == 2
                assert saved["c1"]["_last_updated_by"] == "alice"
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Key and no-removal validation.
# ---------------------------------------------------------------------------


def test_dropping_an_existing_key_is_rejected(tmp_path: Path) -> None:
    """Complete-store commits never remove persisted entities."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
                with pytest.raises(EntityRemovalError) as excinfo:
                    await repo.commit_finals(
                        "migrate", "child", CHILDREN[:2], "migration"
                    )
                assert excinfo.value.entity == "child"
                assert excinfo.value.keys == ["orphan1"]
                assert len(await repo.final_rows("child")) == 3
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


@pytest.mark.parametrize(
    ("entity", "rows", "match"),
    [
        ("child", [child_row("c1"), child_row("c1")], "duplicate"),
        ("child", [child_row(None)], "missing or blank"),
        ("child", [child_row("  ")], "missing or blank"),
        ("family", [dict(family_row(1), family_id=True)], "boolean"),
        ("family", [dict(family_row(1), family_id=2.5)], "non-integral"),
        ("family", [dict(family_row(1), family_id="2.5")], "non-integral"),
    ],
)
def test_invalid_keys_are_rejected(
    tmp_path: Path,
    entity: str,
    rows: list[dict[str, Any]],
    match: str,
) -> None:
    """Null/blank/duplicate/boolean/non-integral keys fail closed."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                with pytest.raises(KeyValidationError, match=match):
                    await repo.commit_finals(
                        "migrate", entity, rows, "migration"
                    )
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_unknown_columns_are_rejected(tmp_path: Path) -> None:
    """Columns outside the persistent schema never reach storage."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                rows = [dict(row) for row in CHILDREN]
                rows[0]["mystery"] = "x"
                with pytest.raises(KeyValidationError, match="mystery"):
                    await repo.commit_finals(
                        "migrate", "child", rows, "migration"
                    )
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Optimistic concurrency (plan §4.4 stale-editor case).
# ---------------------------------------------------------------------------


def test_optimistic_ab_conflict_and_rebase(tmp_path: Path) -> None:
    """A's commit wins; B's stale version fails, rebases, then wins."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
            async with uow.store(cfg, "alice") as repo:
                loaded = await repo.final_rows("child")
            alice_rows = [dict(row) for row in loaded]
            alice_rows[0]["need_1"] = "alice edit"
            async with uow.store(cfg, "alice") as repo:
                await repo.commit_finals("audit", "child", alice_rows, "alice")

            # B still holds version 1 of c1.
            bob_rows = [dict(row) for row in loaded]
            bob_rows[0]["need_1"] = "bob edit"
            async with uow.store(cfg, "bob") as repo:
                with pytest.raises(StaleRowVersionError) as excinfo:
                    await repo.commit_finals("audit", "child", bob_rows, "bob")
                assert excinfo.value.entity == "child"
                assert excinfo.value.key == "c1"
                assert excinfo.value.current_version == 2

                # B re-fetches, rebases onto version 2, and commits.
                fresh = await repo.final_rows("child")
                assert fresh[0]["_last_updated_by"] == "alice"
                rebased = [dict(row) for row in fresh]
                rebased[0]["need_1"] = "bob edit"
                await repo.commit_finals(
                    "audit",
                    "child",
                    rebased,
                    "bob",
                    expected_versions={"c1": 2},
                )
                final = {
                    row["zite_record_id"]: row
                    for row in await repo.final_rows("child")
                }
                assert final["c1"]["need_1"] == "bob edit"
                assert final["c1"]["_row_version"] == 3
                assert final["c1"]["_last_updated_by"] == "bob"
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_audit_requires_expected_version_on_changes(tmp_path: Path) -> None:
    """An audit change without an expected version is rejected."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
            async with uow.store(cfg, "auditor") as repo:
                rows = _plain(await repo.final_rows("child"))
                rows[0]["need_1"] = "unversioned edit"
                with pytest.raises(DbStoreError, match="expected"):
                    await repo.commit_finals("audit", "child", rows, "auditor")
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_worker_modules_bump_without_expected_version(tmp_path: Path) -> None:
    """Screen/generate/match updates win and bump without versions."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
            async with uow.store(cfg, "screen") as repo:
                rows = [dict(row) for row in CHILDREN]
                rows[1]["need_1"] = "screen edit"
                assert (
                    await repo.commit_finals("screen", "child", rows, "screen")
                    == 1
                )
                assert (
                    await repo.commit_finals("screen", "child", rows, "screen")
                    == 0
                )
                fresh = {
                    row["zite_record_id"]: row
                    for row in await repo.final_rows("child")
                }
                assert fresh["c2"]["_row_version"] == 2
            async with uow.store(cfg, "matcher") as repo:
                families = [dict(row) for row in FAMILIES]
                families[0]["assigned_sponsor_id"] = 1
                result = await repo.commit_match(
                    families,
                    [dict(row) for row in SPONSORS],
                    None,
                    "matcher",
                )
                assert result.families_updated == 1
                matched = {
                    row["family_id"]: row
                    for row in await repo.final_rows("family")
                }
                assert matched[1]["assigned_sponsor_id"] == 1
                assert matched[1]["_row_version"] == 2
                assert matched[1]["_last_updated_by"] == "matcher"
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Derived recompute on write.
# ---------------------------------------------------------------------------


def test_child_dob_change_recomputes_age(tmp_path: Path) -> None:
    """age_at_xmas follows dob and the season's Christmas date."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
            async with uow.store(cfg, "screen") as repo:
                rows = [dict(row) for row in CHILDREN]
                rows[0]["dob"] = "2020-12-25"  # birthday on Christmas
                await repo.commit_finals("screen", "child", rows, "screen")
                fresh = {
                    row["zite_record_id"]: row
                    for row in await repo.final_rows("child")
                }
                assert fresh["c1"]["age_at_xmas"] == 6

                rows = [dict(row) for row in CHILDREN]
                rows[0]["dob"] = "not-a-date"
                rows[0]["age_at_xmas"] = 99  # must not be trusted
                await repo.commit_finals("screen", "child", rows, "screen")
                fresh = {
                    row["zite_record_id"]: row
                    for row in await repo.final_rows("child")
                }
                assert fresh["c1"]["age_at_xmas"] is None
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_sponsor_age_preference_recomputes_bounds(tmp_path: Path) -> None:
    """Pref bounds follow age_preference; invalid maps to None/None."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
            async with uow.store(cfg, "screen") as repo:
                rows = [dict(row) for row in SPONSORS]
                rows[0]["age_preference"] = "High School (14-18+ yrs)"
                rows[0]["pref_age_min"] = 0  # stale; must be recomputed
                await repo.commit_finals("screen", "sponsor", rows, "screen")
                fresh = {
                    row["sponsor_id"]: row
                    for row in await repo.final_rows("sponsor")
                }
                assert fresh[1]["pref_age_min"] == 14
                assert fresh[1]["pref_age_max"] == 18

                rows = [dict(row) for row in SPONSORS]
                rows[1]["age_preference"] = "Bogus Preference"
                await repo.commit_finals("screen", "sponsor", rows, "screen")
                fresh = {
                    row["sponsor_id"]: row
                    for row in await repo.final_rows("sponsor")
                }
                assert fresh[2]["pref_age_min"] is None
                assert fresh[2]["pref_age_max"] is None
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_migrate_trusts_submitted_derived_values(tmp_path: Path) -> None:
    """The migration identity may carry explicit derived values."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                children = [dict(row) for row in CHILDREN]
                children[0]["age_at_xmas"] = 99
                await repo.commit_finals(
                    "migrate", "child", children, "migration"
                )
                sponsors = [dict(row) for row in SPONSORS]
                sponsors[0]["pref_age_min"] = 42
                sponsors[0]["pref_age_max"] = 43
                await repo.commit_finals(
                    "migrate", "sponsor", sponsors, "migration"
                )
                fresh_child = {
                    row["zite_record_id"]: row
                    for row in await repo.final_rows("child")
                }
                assert fresh_child["c1"]["age_at_xmas"] == 99
                fresh_sponsor = {
                    row["sponsor_id"]: row
                    for row in await repo.final_rows("sponsor")
                }
                assert fresh_sponsor[1]["pref_age_min"] == 42
                assert fresh_sponsor[1]["pref_age_max"] == 43
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Raw acquisition snapshots.
# ---------------------------------------------------------------------------


def test_raw_snapshot_activation_history_and_duplicates(
    tmp_path: Path,
) -> None:
    """Snapshots activate atomically; history stays; duplicates insert."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "acquire") as repo:
                first = {
                    "child": [
                        {
                            "zite_record_id": "c1",
                            "first_name": "Kiddo",
                            "dob": "2018-05-10",
                            "source": "web",
                        },
                        {
                            # Duplicate source IDs insert verbatim;
                            # preparation detects them later.
                            "zite_record_id": "c1",
                            "first_name": "Kiddo",
                            "dob": "2018-05-10",
                            "source": "web",
                        },
                        {
                            "zite_record_id": None,
                            "first_name": "Blank",
                            "source": "web",
                        },
                    ]
                }
                assert await repo.replace_raw_snapshot(
                    "acquire", first, "acquire"
                ) == {"child": 3}
                active = await repo.raw_rows("child")
                assert len(active) == 3
                assert active[0]["zite_record_id"] == "c1"
                assert "create" not in active[0]

                assert await repo.replace_raw_snapshot(
                    "acquire",
                    {
                        "child": [
                            {
                                "zite_record_id": "c9",
                                "first_name": "New",
                                "source": "web",
                            }
                        ]
                    },
                    "acquire",
                ) == {"child": 1}
                active = await repo.raw_rows("child")
                assert [row["zite_record_id"] for row in active] == ["c9"]
            async with uow.session() as session:
                season = await session.scalar(
                    select(Season).where(Season.name == cfg.season)
                )
                assert season is not None
                runs = (
                    await session.scalars(
                        select(AcquisitionRun)
                        .where(
                            AcquisitionRun.season_id == season.id,
                            AcquisitionRun.table_name == "child",
                        )
                        .order_by(AcquisitionRun.id)
                    )
                ).all()
                assert [run.active for run in runs] == [False, True]
                counts = {
                    run.id: len(
                        (
                            await session.scalars(
                                select(RawChild).where(
                                    RawChild.run_id == run.id
                                )
                            )
                        ).all()
                    )
                    for run in runs
                }
                assert counts == {runs[0].id: 3, runs[1].id: 1}
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_raw_snapshot_rejects_non_acquire_module(tmp_path: Path) -> None:
    """Only acquire may replace raw snapshots."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "screen") as repo:
                with pytest.raises(OwnershipError):
                    await repo.replace_raw_snapshot(
                        "screen", {"child": []}, "screen"
                    )
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Screening diagnostics.
# ---------------------------------------------------------------------------


def test_screen_diagnostics_latest_run_only(tmp_path: Path) -> None:
    """Each run owns its snapshot; reads serve the latest run."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "screen") as repo:
                duplicates = [
                    {
                        "sheet": "family_submissions",
                        "group_key": "fam@example.com|Kiddo",
                        "zite_record_id": "c2",
                        "created": "2026-02-01T09:00:00Z",
                        "survivor": False,
                        "conflict_columns": "need_1",
                        "conflict_details": (
                            "need_1: survivor 'x' vs 'old coat'"
                        ),
                        "record": '{"zite_record_id": "c2"}',
                    }
                ]
                dobs = [
                    {
                        "family_id": 1,
                        "primary_email": "fam@example.com",
                        "child_a_zite_record_id": "c1",
                        "child_a_angel_id": "1A",
                        "child_a_first_name": "Kiddo",
                        "child_a_dob": "2018-05-10",
                        "child_b_zite_record_id": "c2",
                        "child_b_angel_id": None,
                        "child_b_first_name": "Kid",
                        "child_b_dob": "2018-06-01",
                        "gap_months": 0,
                        "gap_days": 22,
                    }
                ]
                assert (
                    await repo.replace_screen_diagnostics(
                        duplicates, dobs, "screen"
                    )
                    == 2
                )
                current = await repo.duplicate_log_rows()
                assert len(current) == 1
                assert current[0]["sheet"] == "family_submissions"
                assert current[0]["survivor"] is False
                assert current[0]["record"] == '{"zite_record_id": "c2"}'
                assert len(await repo.dob_log_rows()) == 1

                assert (
                    await repo.replace_screen_diagnostics([], dobs, "screen")
                    == 1
                )
                assert await repo.duplicate_log_rows() == []
                latest_dob = await repo.dob_log_rows()
                assert len(latest_dob) == 1
                assert latest_dob[0]["child_a_zite_record_id"] == "c1"
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# External validation logs.
# ---------------------------------------------------------------------------


def _email_log_row(record_id: str, field: str, email: str) -> dict[str, Any]:
    """One legacy email_validation_log row."""
    return {
        "source_sheet": "final_family",
        "zite_record_id": record_id,
        "field": field,
        "email": email,
        "status": "valid",
        "sub_status": "",
        "did_you_mean": "",
        "response_json": '{"status": "valid"}',
        "checked_at": "2026-11-01T10:00:00+00:00",
    }


def test_email_log_replays_by_normalized_email(tmp_path: Path) -> None:
    """One check per normalized email; targets expand per record/field."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "screen") as repo:
                assert (
                    await repo.append_email_log(
                        [
                            _email_log_row(
                                "rec_f1", "primary_email", "Fam@Example.com"
                            )
                        ],
                        "screen",
                    )
                    == 1
                )
                # Same normalized email from another record/field: skip.
                assert (
                    await repo.append_email_log(
                        [
                            _email_log_row(
                                "rec_f2", "secondary_email", "fam@example.com"
                            ),
                            _email_log_row(
                                "rec_s1", "email", "sponsor@example.com"
                            ),
                        ],
                        "screen",
                    )
                    == 1
                )
                rows = await repo.email_log_rows()
                assert len(rows) == 2
                assert rows[0]["email"] == "Fam@Example.com"
                assert rows[0]["source_sheet"] == "final_family"
                assert rows[1]["zite_record_id"] == "rec_s1"
                assert rows[1]["status"] == "valid"
                assert await repo.sheet_rows("email_validation_log") == rows
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_address_log_replays_by_exact_query(tmp_path: Path) -> None:
    """One row per distinct query; repeats and in-batch dups skip."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "screen") as repo:

                def address_row(query: str) -> dict[str, Any]:
                    return {
                        "source_sheet": "family_submissions",
                        "zite_record_id": "rec_f1",
                        "query": query,
                        "status": "ok",
                        "display_name": "Houghton, MI",
                        "lat": "47.12",
                        "lon": "-88.57",
                        "response_json": '{"place_id": 1}',
                        "checked_at": "2026-11-01T10:00:00+00:00",
                    }

                assert (
                    await repo.append_address_log(
                        [
                            address_row("q1"),
                            address_row("q1"),
                            address_row("q2"),
                        ],
                        "screen",
                    )
                    == 2
                )
                rows = await repo.address_log_rows()
                assert [row["query"] for row in rows] == ["q1", "q2"]
                assert rows[0]["display_name"] == "Houghton, MI"
                assert (
                    await repo.append_address_log([address_row("q1")], "screen")
                    == 0
                )
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Match commits.
# ---------------------------------------------------------------------------


async def _patched_match_rows(
    repo: SeasonStoreRepository,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read both stores with family 1 / sponsor 1 assigned to each other."""
    families = await repo.final_rows("family")
    sponsors = await repo.final_rows("sponsor")
    for row in families:
        row["assigned_sponsor_id"] = 1
        row["assigned_angel_ids"] = "1A"
        row["num_angels_in_family"] = 1
    for row in sponsors:
        row["assigned_angel_ids"] = "1A"
        row["num_angels_assigned"] = 1
    return families, sponsors


REPORT: list[dict[str, Any]] = [
    {"kind": "matched", "ref": "1", "reason": "age preference satisfied"}
]


def test_commit_match_archives_reports_and_applies(tmp_path: Path) -> None:
    """One transaction: archive snapshot, report, assignment, versions."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
            run_id: int | None = None
            async with uow.store(cfg, "matcher") as repo:
                families, sponsors = await _patched_match_rows(repo)
                result = await repo.commit_match(
                    families, sponsors, REPORT, "matcher"
                )
                run_id = result.run_id
                assert result.report_rows == 1
                assert result.families_updated == 2
                assert result.sponsors_updated == 2
                assert await repo.match_report_rows() == REPORT
                fresh_family = {
                    row["family_id"]: row
                    for row in await repo.final_rows("family")
                }
                assert fresh_family[1]["assigned_sponsor_id"] == 1
                assert fresh_family[1]["assigned_angel_ids"] == "1A"
                assert fresh_family[1]["_row_version"] == 2
                fresh_sponsor = {
                    row["sponsor_id"]: row
                    for row in await repo.final_rows("sponsor")
                }
                assert fresh_sponsor[2]["num_angels_assigned"] == 1
                assert fresh_sponsor[2]["_last_updated_by"] == "matcher"
            async with uow.session() as session:
                snapshots = (
                    await session.scalars(
                        select(MatchAssignmentSnapshot).where(
                            MatchAssignmentSnapshot.run_id == run_id
                        )
                    )
                ).all()
                assert len(snapshots) == 7  # 2 families, 2 sponsors, 3 children
                archived = next(
                    snap
                    for snap in snapshots
                    if snap.entity == "family" and snap.key == "1"
                )
                assert archived.assignment_values == {
                    "assigned_sponsor_id": None,
                    "assigned_angel_ids": None,
                    "num_angels_in_family": None,
                }
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_commit_match_rejects_non_assignment_changes(
    tmp_path: Path,
) -> None:
    """Match may not smuggle source/control edits through."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
            async with uow.store(cfg, "matcher") as repo:
                families, sponsors = await _patched_match_rows(repo)
                families[0]["first_name"] = "Smuggled"
                with pytest.raises(OwnershipError) as excinfo:
                    await repo.commit_match(
                        families, sponsors, REPORT, "matcher"
                    )
                assert excinfo.value.entity == "family"
                assert excinfo.value.columns == ("first_name",)
                # Nothing applied: the whole transaction failed.
                unchanged = {
                    row["family_id"]: row
                    for row in await repo.final_rows("family")
                }
                assert unchanged[1]["first_name"] == "Jane"
                assert unchanged[1]["assigned_sponsor_id"] is None
                assert unchanged[1]["_row_version"] == 1
                assert await repo.match_report_rows() is None
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_commit_match_skip_match_mode(tmp_path: Path) -> None:
    """skip-match applies assignments without a run or report."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
            async with uow.store(cfg, "matcher") as repo:
                families, sponsors = await _patched_match_rows(repo)
                await repo.commit_match(families, sponsors, REPORT, "matcher")
                before = await repo.match_report_rows()

                families = await repo.final_rows("family")
                sponsors = await repo.final_rows("sponsor")
                for row in families:
                    row["assigned_sponsor_id"] = 2
                result = await repo.commit_match(
                    families, sponsors, None, "matcher", overwrite=True
                )
                assert result.run_id is None
                assert result.report_rows == 0
                assert result.families_updated == 2
                assert await repo.match_report_rows() == before
                fresh = {
                    row["family_id"]: row
                    for row in await repo.final_rows("family")
                }
                assert fresh[1]["assigned_sponsor_id"] == 2
                assert fresh[1]["_row_version"] == 3
            async with uow.session() as session:
                season = await session.scalar(
                    select(Season).where(Season.name == cfg.season)
                )
                assert season is not None
                runs = (
                    await session.scalars(
                        select(MatchRun).where(MatchRun.season_id == season.id)
                    )
                ).all()
                assert len(runs) == 1
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Artifacts and clarification state.
# ---------------------------------------------------------------------------


def test_artifact_register_and_read(tmp_path: Path) -> None:
    """Artifacts register once and read back season-scoped."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "generator") as repo:
                first = await repo.register_artifact(
                    "child_sheet",
                    "child_sheets/1A.pdf",
                    "1A.pdf",
                    "application/pdf",
                    100,
                    "abc123",
                    "generator",
                    meta={"page_count": 2},
                )
                second = await repo.register_artifact(
                    "stats_report",
                    "stats/report.html",
                    "report.html",
                    "text/html",
                    50,
                    "def456",
                    "generator",
                )
                assert second != first
                rows = await repo.artifact_rows()
                assert [row["id"] for row in rows] == [first, second]
                assert rows[0]["meta"] == {"page_count": 2}
                assert rows[0]["created_by"] == "generator"
                only_sheets = await repo.artifact_rows("child_sheet")
                assert [row["id"] for row in only_sheets] == [first]
                single = await repo.artifact(first)
                assert single is not None
                assert single["sha256"] == "abc123"
                assert single["size_bytes"] == 100
                assert await repo.artifact(999) is None
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_clarification_contact_status_tri_state(tmp_path: Path) -> None:
    """Only supplied fields change; False is a real value."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "alice") as repo:
                state = await repo.set_contact_status(
                    1, contacted=True, contacted_at="2026-11-01", actor="alice"
                )
                assert state["contacted"] is True
                assert state["contacted_at"] == "2026-11-01"
                assert state["replied"] is False
                assert state["source"] == "db"
                assert state["updated_by"] == "alice"

                state = await repo.set_contact_status(
                    1, replied=True, actor="bob"
                )
                assert state["contacted"] is True
                assert state["contacted_at"] == "2026-11-01"
                assert state["replied"] is True
                assert state["updated_by"] == "bob"

                state = await repo.set_contact_status(
                    1, contacted=False, actor="bob"
                )
                assert state["contacted"] is False
                families = await repo.clarification_families()
                assert len(families) == 1
                assert families[0]["family_id"] == 1
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_clarification_drafts_version_and_fetch(tmp_path: Path) -> None:
    """Drafts version max+1; latest and exact version reads work."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "alice") as repo:
                paragraphs = [
                    {
                        "zite_record_id": "c1",
                        "angel_id": "1A",
                        "order": 0,
                        "text": "Kiddo would love a coat.",
                    }
                ]
                first = await repo.save_draft(
                    1,
                    "Subject",
                    "Hi",
                    "intro",
                    "bye",
                    paragraphs,
                    actor="alice",
                )
                assert first["version"] == 1
                assert first["paragraphs"] == paragraphs
                second = await repo.save_draft(
                    1,
                    "Subject v2",
                    "Hi",
                    "intro",
                    "bye",
                    paragraphs,
                    actor="bob",
                    generated_by="ai",
                )
                assert second["version"] == 2
                assert second["generated_by"] == "ai"

                latest = await repo.latest_draft(1)
                assert latest is not None
                assert latest["version"] == 2
                assert latest["subject"] == "Subject v2"
                oldest = await repo.draft_at(1, 1)
                assert oldest is not None
                assert oldest["subject"] == "Subject"
                assert await repo.latest_draft(99) is None
                assert await repo.draft_at(1, 3) is None
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Generic reads and transaction atomicity.
# ---------------------------------------------------------------------------


def test_sheet_rows_dispatch_and_unknown_sheet(tmp_path: Path) -> None:
    """Known sheets map to DB equivalents; unknown names fail."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "screen") as repo:
                assert await repo.sheet_rows("match_report") == []
                assert await repo.sheet_rows("duplicate_log") == []
                with pytest.raises(DbStoreError, match="mystery"):
                    await repo.sheet_rows("mystery_sheet")
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_sheet_names_reflect_presence(tmp_path: Path) -> None:
    """raw_* and match_report appear only when their runs exist."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "screen") as repo:
                base = await repo.sheet_names()
                assert "final_child" in base
                assert "duplicate_log" in base
                assert "raw_child" not in base
                assert "match_report" not in base
                await repo.replace_raw_snapshot(
                    "acquire", {"child": []}, "acquire"
                )
                assert "raw_child" in (await repo.sheet_names())
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_commit_finals_atomic_on_last_row_violation(tmp_path: Path) -> None:
    """A batch whose last row violates validation commits nothing."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
            async with uow.store(cfg, "alice") as repo:
                rows = await repo.final_rows("child")
                rows[0]["need_1"] = "alice edit"  # legal audit change
                rows[-1]["screened"] = True  # module-owned: rejected
                with pytest.raises(OwnershipError):
                    await repo.commit_finals("audit", "child", rows, "alice")
            async with uow.store(cfg, "check") as repo:
                fresh = {
                    row["zite_record_id"]: row
                    for row in await repo.final_rows("child")
                }
                assert fresh["c1"]["need_1"] == "winter coat"
                assert fresh["orphan1"]["screened"] is False
                assert {row["_row_version"] for row in fresh.values()} == {1}
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Exact-row raw dedupe + live child counts.
# ---------------------------------------------------------------------------


def test_family_child_counts_excludes_ignored(tmp_path: Path) -> None:
    """The grouped child count tracks live children, not ignored rows."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migrate") as repo:
                await repo.commit_finals(
                    "migrate",
                    "family",
                    [
                        {
                            "family_id": 1,
                            "primary_email": "fam@example.com",
                            "status": "new",
                        }
                    ],
                    "migration",
                )
                await repo.commit_finals(
                    "migrate",
                    "child",
                    [
                        {
                            "zite_record_id": "c1",
                            "family_id": 1,
                            "primary_email": "fam@example.com",
                            "status": "new",
                        },
                        {
                            "zite_record_id": "c2",
                            "family_id": 1,
                            "primary_email": "fam@example.com",
                            "status": "ignored",
                            "ignore": True,
                        },
                        {
                            "zite_record_id": "c3",
                            "family_id": 1,
                            "primary_email": "fam@example.com",
                            "status": "new",
                            "ignore": False,
                        },
                    ],
                    "migration",
                )
                counts = await repo.family_child_counts()
                assert counts == {1: 2}
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_dedupe_raw_rows_removes_exact_duplicates(tmp_path: Path) -> None:
    """Identical raw rows collapse to one; distinct rows and row_count hold."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "acquire") as repo:
                await repo.replace_raw_snapshot(
                    "acquire",
                    {
                        "child": [
                            {
                                "zite_record_id": "c1",
                                "first_name": "Kiddo",
                                "dob": "2018-05-10",
                                "source": "web",
                            },
                            # exact duplicate of the first row
                            {
                                "zite_record_id": "c1",
                                "first_name": "Kiddo",
                                "dob": "2018-05-10",
                                "source": "web",
                            },
                            # differs in one column: survives
                            {
                                "zite_record_id": "c2",
                                "first_name": "Kiddo",
                                "dob": "2018-05-10",
                                "source": "web",
                            },
                        ]
                    },
                    "acquire",
                )
                summary = await repo.dedupe_raw_rows("migrate", "child")
                assert summary == {"child": {"deleted": 1, "kept": 2}}
                rows = await repo.raw_rows("child")
                assert [row["zite_record_id"] for row in rows] == ["c1", "c2"]
                async with uow.session() as session:
                    season = await session.scalar(
                        select(Season).where(Season.name == cfg.season)
                    )
                    assert season is not None
                    run = await session.scalar(
                        select(AcquisitionRun).where(
                            AcquisitionRun.season_id == season.id,
                            AcquisitionRun.table_name == "child",
                            AcquisitionRun.active.is_(True),
                        )
                    )
                    assert run is not None
                    assert run.row_count == 2
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_dedupe_raw_rows_scoped_per_run(tmp_path: Path) -> None:
    """Identical rows in different runs both survive (dedupe is per run)."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            snapshot = {
                "child": [
                    {
                        "zite_record_id": "c1",
                        "first_name": "Kiddo",
                        "source": "web",
                    }
                ]
            }
            async with uow.store(cfg, "acquire") as repo:
                await repo.replace_raw_snapshot("acquire", snapshot, "acquire")
                await repo.replace_raw_snapshot("acquire", snapshot, "acquire")
                summary = await repo.dedupe_raw_rows("migrate", "child")
                assert summary == {"child": {"deleted": 0, "kept": 1}}
                assert len(await repo.raw_rows("child")) == 1
        finally:
            await uow.engine.dispose()

    asyncio.run(main())


def test_dedupe_raw_rejects_non_snapshot_module(tmp_path: Path) -> None:
    """Only raw-snapshot modules may dedupe or prune raw tables."""

    async def main() -> None:
        cfg = _prepare(tmp_path)
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "screen") as repo:
                with pytest.raises(OwnershipError):
                    await repo.dedupe_raw_rows("screen", "child")
                with pytest.raises(OwnershipError):
                    await repo.prune_inactive_runs("screen")
        finally:
            await uow.engine.dispose()

    asyncio.run(main())
