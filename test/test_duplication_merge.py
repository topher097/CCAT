"""Tests for the merged "Current (editable)" column of a duplicate group.

The Current column holds one merged row folded from every submission in
the group, and for a sponsor group the auditor picks which member's row
the merged entry is written to — which is the sponsor number it keeps.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import load_test_cfg

from ccat.audit import AuditError
from ccat.audit.store import ENTITY_SPECS
from ccat.config import Config, StorageSpec
from ccat.db.engine import create_async_engine, run_migrations
from ccat.db.uow import UnitOfWork
from ccat.services import db_duplications

# ---------------------------------------------------------------------------
# Fixtures and helpers.
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path) -> Config:
    """Test config: temp DB and artifacts."""
    base = load_test_cfg(
        excel_file=str(tmp_path / "wb.xlsx"),
        log_file=str(tmp_path / "ccat.log"),
        match_report_csv=str(tmp_path / "report.csv"),
    )
    return base.model_copy(
        update={
            "storage": StorageSpec(
                mode="db",
                database_url=f"sqlite+aiosqlite:///{tmp_path / 'ccat.sqlite3'}",
                artifact_root=str(tmp_path / "artifacts"),
            )
        }
    )


def _columns(entity: str) -> tuple[str, ...]:
    """Return the entity's legacy dict column vocabulary."""
    return tuple(ENTITY_SPECS[entity]["columns"])


def sponsor_row(sponsor_id: int, **overrides: Any) -> dict[str, Any]:
    """One full final-sponsor row under legacy dict names."""
    base: dict[str, Any] = {column: None for column in _columns("sponsor")}
    base.update(
        sponsor_id=sponsor_id,
        zite_record_id=f"rec_s{sponsor_id}",
        create=f"2026-0{sponsor_id}-01T10:00:00Z",
        email=f"sponsor{sponsor_id}@example.com",
        sponsor_type="Individual",
        first_name="Mary",
        last_name="Sponsor",
        phone=f"555-000{sponsor_id}",
        num_children=1,
        age_preference="Any Age",
        source="web",
        status="new",
        ignore=False,
        potential_duplicate=False,
    )
    base.update(overrides)
    return base


def raw_sponsor(sponsor_id: int, **overrides: Any) -> dict[str, Any]:
    """One verbatim raw-sponsor row."""
    base: dict[str, Any] = {
        "zite_record_id": f"rec_s{sponsor_id}",
        "create": f"2026-0{sponsor_id}-01T10:00:00Z",
        "email": f"sponsor{sponsor_id}@example.com",
        "sponsor_type": "Individual",
        "first_name": "Mary",
        "last_name": "Sponsor",
        "phone": f"555-000{sponsor_id}",
        "num_children": 1,
        "age_preference": "Any Age",
        "source": "web",
    }
    base.update(overrides)
    return base


def _log_row(
    group_key: str,
    record_id: str,
    created: str,
    survivor: bool,
    record: dict[str, Any],
) -> dict[str, Any]:
    """One duplicate-log row for a sponsor group."""
    return {
        "sheet": "sponsor_submissions",
        "group_key": group_key,
        "zite_record_id": record_id,
        "created": created,
        "survivor": survivor,
        "conflict_columns": "email",
        "conflict_details": "email: survivor 'a' vs ['b']",
        "record": json.dumps(record, default=str, sort_keys=True),
    }


async def _seed_group(repo: Any) -> None:
    """Seed a sponsor group of three submissions sharing one email."""
    raw = [raw_sponsor(index) for index in (1, 2, 3)]
    for row in raw:
        row["email"] = "same@example.com"
    await repo.replace_raw_snapshot("migrate", {"sponsor": raw}, "migration")
    await repo.commit_finals(
        "migrate",
        "sponsor",
        [sponsor_row(index) for index in (1, 2, 3)],
        "migration",
    )
    await repo.replace_screen_diagnostics(
        [
            _log_row(
                "same@example.com",
                f"rec_s{index}",
                f"2026-0{index}-01T10:00:00Z",
                index == 1,
                raw[index - 1],
            )
            for index in (1, 2, 3)
        ],
        [],
        "screen",
    )


def _run(coro: Any) -> Any:
    """Run one coroutine on a fresh event loop."""
    return asyncio.run(coro)


def _store(cfg: Config) -> Any:
    """Return a unit of work for the test database."""
    return UnitOfWork(create_async_engine(cfg.storage.database_url))


async def _sponsors(repo: Any) -> dict[int, dict[str, Any]]:
    """Return the final sponsor rows, keyed by sponsor id."""
    return {row["sponsor_id"]: row for row in await repo.final_rows("sponsor")}


async def _group_id(repo: Any) -> str:
    """Return the seeded group's id."""
    groups = await db_duplications.list_groups(repo)
    return next(group["id"] for group in groups if group["entity"] == "sponsor")


# ---------------------------------------------------------------------------
# The merge rule.
# ---------------------------------------------------------------------------


def _member(record: dict[str, Any]) -> dict[str, Any]:
    """One member in the shape the fold reads."""
    return {"record": json.dumps(record, default=str, sort_keys=True)}


def test_the_newest_submission_wins_a_field() -> None:
    """The newest member's value is the merged value."""
    merged = db_duplications._merge_submissions(
        [
            _member({"email": "old@example.com"}),
            _member({"email": "new@example.com"}),
        ]
    )
    assert merged["email"] == "new@example.com"


def test_a_blank_in_the_newest_falls_back_to_an_older_member() -> None:
    """A blank never wins; an older member fills the gap."""
    merged = db_duplications._merge_submissions(
        [
            _member({"email": "old@example.com", "phone": "555-0001"}),
            _member({"email": "new@example.com", "phone": None}),
        ]
    )
    assert merged["email"] == "new@example.com"
    assert merged["phone"] == "555-0001"


def test_the_fold_is_per_field_not_per_row() -> None:
    """Each field takes the newest member that filled one in."""
    merged = db_duplications._merge_submissions(
        [
            _member({"first_name": "Ann", "last_name": "Old"}),
            _member({"first_name": "Beth", "last_name": "New"}),
            _member({"first_name": None, "last_name": None}),
        ]
    )
    assert merged == {"first_name": "Beth", "last_name": "New"}


def test_a_field_only_the_oldest_holds_survives_the_fold() -> None:
    """The oldest member contributes what nobody newer has."""
    merged = db_duplications._merge_submissions(
        [
            _member({"organization_name": "Walmart"}),
            _member({"organization_name": None}),
            _member({"organization_name": ""}),
        ]
    )
    assert merged["organization_name"] == "Walmart"


# ---------------------------------------------------------------------------
# The merged row in the group detail.
# ---------------------------------------------------------------------------


def test_the_detail_carries_the_merged_row(tmp_path: Path) -> None:
    """The detail returns the merged row and the row it is written to."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_group(repo)
                detail = await db_duplications.group_detail(
                    repo, await _group_id(repo)
                )
            assert detail is not None
            # No member is decided yet, so the flagged survivor owns it.
            assert detail["current_row_key"] == "1"
            assert detail["current"]["email"] == "same@example.com"
            assert detail["current"]["phone"] == "555-0003"
            assert detail["current"]["first_name"] == "Mary"
        finally:
            await uow.engine.dispose()

    _run(main())


def test_the_merged_row_covers_only_editable_fields(
    tmp_path: Path,
) -> None:
    """Linkage, controls and the row's key are not merged."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_group(repo)
                detail = await db_duplications.group_detail(
                    repo, await _group_id(repo)
                )
            assert detail is not None
            assert "sponsor_id" not in detail["current"]
            assert "zite_record_id" not in detail["current"]
            assert "status" not in detail["current"]
            assert "source" not in detail["current"]
        finally:
            await uow.engine.dispose()

    _run(main())


# ---------------------------------------------------------------------------
# The sponsor number pick.
async def _resolve_group(repo: Any, cfg: Config) -> str:
    """Decide two of the three members, leaving rec_s1 the survivor."""
    group_id = await _group_id(repo)
    await db_duplications.decide(
        repo,
        cfg,
        group_id,
        "rec_s2",
        "not_duplicate",
        "auditor",
        confirm=True,
    )
    await db_duplications.decide(
        repo,
        cfg,
        group_id,
        "rec_s3",
        "ignore",
        "auditor",
        confirm=True,
    )
    return group_id


def test_the_pick_writes_the_merged_entry_onto_that_row(
    tmp_path: Path,
) -> None:
    """Choosing a sponsor number writes the merged row onto that row."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_group(repo)
                group_id = await _resolve_group(repo, cfg)
                await db_duplications.mark_audited(
                    repo,
                    cfg,
                    group_id,
                    "auditor",
                    values={"sponsor_id": 2, "first_name": "Merged"},
                )
                sponsors = await _sponsors(repo)
            assert sponsors[2]["first_name"] == "Merged"
            assert sponsors[2]["status"] == "audited"
            # The merge reaches the row through the auditor's payload; the
            # server writes what it is given, not the fold.
            assert sponsors[2]["email"] == "sponsor2@example.com"
        finally:
            await uow.engine.dispose()

    _run(main())


def test_the_pick_closes_the_previous_survivor_out(tmp_path: Path) -> None:
    """The entry the pick left behind awaits its own audit."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_group(repo)
                group_id = await _resolve_group(repo, cfg)
                await db_duplications.mark_audited(
                    repo,
                    cfg,
                    group_id,
                    "auditor",
                    values={"sponsor_id": 2},
                )
                sponsors = await _sponsors(repo)
            assert sponsors[2]["status"] == "audited"
            assert sponsors[1]["status"] == "new"
            assert sponsors[1]["ignore"] is False
            assert sponsors[3]["ignore"] is True
            assert sponsors[3]["status"] == "ignored"
        finally:
            await uow.engine.dispose()

    _run(main())


def test_the_default_pick_keeps_the_surviving_entry(tmp_path: Path) -> None:
    """Without a pick the merged entry lands on the surviving row."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_group(repo)
                group_id = await _resolve_group(repo, cfg)
                await db_duplications.mark_audited(
                    repo, cfg, group_id, "auditor", values={}
                )
                sponsors = await _sponsors(repo)
            assert sponsors[1]["status"] == "audited"
            assert sponsors[2]["status"] == "new"
        finally:
            await uow.engine.dispose()

    _run(main())


def test_the_pick_refuses_an_ignored_member(tmp_path: Path) -> None:
    """An ignored row is an identity skeleton; it cannot be the entry."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_group(repo)
                group_id = await _resolve_group(repo, cfg)
                with pytest.raises(AuditError, match="is ignored"):
                    await db_duplications.mark_audited(
                        repo,
                        cfg,
                        group_id,
                        "auditor",
                        values={"sponsor_id": 3},
                    )
        finally:
            await uow.engine.dispose()

    _run(main())


def test_the_pick_refuses_a_number_outside_the_group(tmp_path: Path) -> None:
    """A sponsor id the group does not hold is refused."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_group(repo)
                group_id = await _resolve_group(repo, cfg)
                with pytest.raises(AuditError, match="not one of this group"):
                    await db_duplications.mark_audited(
                        repo,
                        cfg,
                        group_id,
                        "auditor",
                        values={"sponsor_id": 99},
                    )
        finally:
            await uow.engine.dispose()

    _run(main())


def test_the_pick_never_renumbers_a_sponsor(tmp_path: Path) -> None:
    """The pick moves the data onto a row; no row's number changes."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_group(repo)
                group_id = await _resolve_group(repo, cfg)
                await db_duplications.mark_audited(
                    repo,
                    cfg,
                    group_id,
                    "auditor",
                    values={"sponsor_id": 2},
                )
                sponsors = await _sponsors(repo)
            assert sorted(sponsors) == [1, 2, 3]
            assert sponsors[1]["zite_record_id"] == "rec_s1"
            assert sponsors[2]["zite_record_id"] == "rec_s2"
            assert sponsors[3]["zite_record_id"] == "rec_s3"
        finally:
            await uow.engine.dispose()

    _run(main())


def test_the_audited_entry_is_recorded(tmp_path: Path) -> None:
    """The group remembers the row its adjudication landed on."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_group(repo)
                group_id = await _resolve_group(repo, cfg)
                await db_duplications.mark_audited(
                    repo,
                    cfg,
                    group_id,
                    "auditor",
                    values={"sponsor_id": 2},
                )
                detail = await db_duplications.group_detail(repo, group_id)
            assert detail is not None
            assert detail["current_row_key"] == "2"
            assert detail["status"] == "audited"
        finally:
            await uow.engine.dispose()

    _run(main())


def test_re_auditing_re_affirms_the_recorded_entry(tmp_path: Path) -> None:
    """A later audit does not land on a different row."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_group(repo)
                group_id = await _resolve_group(repo, cfg)
                await db_duplications.mark_audited(
                    repo,
                    cfg,
                    group_id,
                    "auditor",
                    values={"sponsor_id": 2},
                )
                detail = await db_duplications.mark_audited(
                    repo, cfg, group_id, "auditor"
                )
                sponsors = await _sponsors(repo)
            assert detail["status"] == "audited"
            assert sponsors[2]["status"] == "audited"
            assert sponsors[1]["status"] == "new"
        finally:
            await uow.engine.dispose()

    _run(main())


def test_re_auditing_cannot_move_the_entry(tmp_path: Path) -> None:
    """The audited entry's sponsor number is final."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed_group(repo)
                group_id = await _resolve_group(repo, cfg)
                await db_duplications.mark_audited(
                    repo,
                    cfg,
                    group_id,
                    "auditor",
                    values={"sponsor_id": 2},
                )
                with pytest.raises(AuditError, match="cannot be changed"):
                    await db_duplications.mark_audited(
                        repo,
                        cfg,
                        group_id,
                        "auditor",
                        values={"sponsor_id": 1},
                    )
        finally:
            await uow.engine.dispose()

    _run(main())
