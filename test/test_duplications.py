"""Tests for the duplication-audit service (the Duplications section).

File-backed SQLite in ``tmp_path``.  The duplicate groups are seeded
the way a screening run leaves them (one ``duplicate_log`` row per
group member), so the tests exercise the service against realistic
repository rows.
"""

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import load_test_cfg

from ccat.audit import RowAuditedError
from ccat.audit.store import ENTITY_SPECS
from ccat.config import Config, StorageSpec
from ccat.db.engine import create_async_engine, run_migrations
from ccat.db.errors import StaleRowVersionError
from ccat.db.uow import UnitOfWork
from ccat.services import db_duplications, db_pipeline

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
        gender="Male",
        source="web",
        status="new",
        ignore=False,
        screened=True,
        potential_duplicate=True,
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
        potential_duplicate=True,
        num_angels_in_family=1,
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
        num_children=1,
        age_preference="Any Age",
        source="web",
        status="new",
        ignore=False,
        potential_duplicate=True,
    )
    base.update(overrides)
    return base


def _raw_child(
    record_id: str, created: str, **overrides: Any
) -> dict[str, Any]:
    """One verbatim raw-child row."""
    base = {
        "zite_record_id": record_id,
        "zite_created_at": created,
        "created": created,
        "first_name": "Kiddo",
        "primary_email": "fam@example.com",
        "dob": "2018-05-10",
        "gender": "Male",
        "source": "web",
    }
    base.update(overrides)
    return base


def _raw_family(record_id: str, created: str) -> dict[str, Any]:
    """One verbatim raw-family row."""
    return {
        "zite_record_id": record_id,
        "zite_created_at": created,
        "created": created,
        "primary_email": "fam@example.com",
        "first_name": "Jane",
        "last_name": "Doe",
        "city": "Hancock",
        "source": "web",
    }


def _raw_sponsor(record_id: str) -> dict[str, Any]:
    """One verbatim raw-sponsor row."""
    return {
        "zite_record_id": record_id,
        "create": "2026-01-01T10:00:00Z",
        "email": "sponsor1@example.com",
        "sponsor_type": "Individual",
        "first_name": "Mary",
        "last_name": "Sponsor",
        "num_children": 1,
        "age_preference": "Any Age",
        "source": "web",
    }


def _log_row(
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


async def _seed(repo: Any) -> None:
    """Seed the raw snapshot, the final stores, and the duplicate log."""
    await repo.replace_raw_snapshot(
        "migrate",
        {
            "sponsor": [_raw_sponsor("rec_s1")],
            "family": [
                _raw_family("rec_f1", "2026-01-05T10:00:00Z"),
                _raw_family("rec_f2", "2026-01-06T10:00:00Z"),
            ],
            "child": [
                _raw_child(
                    "c1",
                    "2026-02-01T09:00:00+00:00",
                    interests="dinosaurs",
                ),
                _raw_child("c1b", "2026-02-02T09:00:00+00:00"),
            ],
        },
        "migration",
    )
    await repo.commit_finals(
        "migrate",
        "child",
        [
            child_row("c1", angel_id=None),
            child_row("c1b", angel_id="1A"),
        ],
        "migration",
    )
    await repo.commit_finals("migrate", "family", [family_row(1)], "migration")
    await repo.commit_finals(
        "migrate", "sponsor", [sponsor_row(1)], "migration"
    )
    await repo.replace_screen_diagnostics(
        [
            _log_row(
                "child_submissions",
                "fam@example.com|Kiddo",
                "c1",
                "2026-02-01T09:00:00+00:00",
                False,
                _raw_child("c1", "2026-02-01T09:00:00+00:00"),
            ),
            _log_row(
                "child_submissions",
                "fam@example.com|Kiddo",
                "c1b",
                "2026-02-02T09:00:00+00:00",
                True,
                _raw_child("c1b", "2026-02-02T09:00:00+00:00"),
            ),
            _log_row(
                "family_submissions",
                "fam@example.com",
                "rec_f1",
                "2026-01-05T10:00:00Z",
                True,
                _raw_family("rec_f1", "2026-01-05T10:00:00Z"),
            ),
            _log_row(
                "family_submissions",
                "fam@example.com",
                "rec_f2",
                "2026-01-06T10:00:00Z",
                False,
                _raw_family("rec_f2", "2026-01-06T10:00:00Z"),
            ),
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


async def _group(repo: Any, entity: str) -> dict[str, Any]:
    """Return one entity's duplicate group summary."""
    groups = await db_duplications.list_groups(repo)
    return next(item for item in groups if item["entity"] == entity)


async def _rows(repo: Any, entity: str) -> dict[Any, dict[str, Any]]:
    """Return one entity's final rows, keyed by its key column."""
    key = ENTITY_SPECS[entity]["key"]
    return {row[key]: row for row in await repo.final_rows(entity)}


class _FakeOpencode:
    """Fake opencode client answering every child the literal null."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Record the fake session resource."""
        self.session = SimpleNamespace(
            create=lambda model: SimpleNamespace(id="ses_test"),
            generate=self._generate,
        )

    def _generate(self, session_id: str, prompt: str) -> SimpleNamespace:
        """Answer with a literal null (no corrections)."""
        assert re.search(r'"zite_record_id": "([^"]+)"', prompt) is not None
        return SimpleNamespace(text="null")

    def __enter__(self) -> "_FakeOpencode":
        """Return self as the context manager target."""
        return self

    def __exit__(self, *args: Any) -> None:
        """Swallow exceptions like the real client does."""
        return None


def _use_fake_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the offline AI fake (and the fake opencode v2)."""
    import ccat.screening as screening
    import opencode

    class _Version:
        stdout = "2.0.0"
        stderr = ""

    monkeypatch.setattr(
        screening.subprocess, "run", lambda *args, **kwargs: _Version()
    )
    monkeypatch.setattr(opencode, "Opencode", _FakeOpencode)


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


def test_list_groups_reports_detected_duplicates(tmp_path: Path) -> None:
    """Every detected group is listed with its member and decision count."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
                groups = await db_duplications.list_groups(repo)
            assert [group["entity"] for group in groups] == ["child", "family"]
            child_group = groups[0]
            assert child_group["member_count"] == 2
            assert child_group["decided_count"] == 0
            assert child_group["survivor_count"] == 2
            assert child_group["released_count"] == 0
            assert child_group["status"] == "pending"
            assert child_group["group_key"] == "fam@example.com|Kiddo"
            assert child_group["survivor_record_id"] == "c1b"
        finally:
            await uow.engine.dispose()

    _run(main())


def test_detail_shows_raw_and_screened_results(tmp_path: Path) -> None:
    """One group's members carry their raw record and screened row."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
                groups = await db_duplications.list_groups(repo)
                detail = await db_duplications.group_detail(
                    repo, groups[0]["id"]
                )
            assert detail is not None
            assert detail["status"] == "pending"
            assert [
                member["zite_record_id"] for member in detail["members"]
            ] == [
                "c1",
                "c1b",
            ]
            first = detail["members"][0]
            assert first["raw"]["zite_record_id"] == "c1"
            assert first["screened"]["angel_id"] is None
            assert first["screened"]["family_id"] == 1
            assert first["decision"] is None
            assert first["state"] == "surviving"
            assert first["row_key"] == "c1"
            assert first["row_status"] == "new"
            assert detail["members"][1]["screened"]["angel_id"] == "1A"
            assert detail["survivor_count"] == 2
            assert detail["children"] == []
        finally:
            await uow.engine.dispose()

    _run(main())


def test_pending_duplicate_children_share_one_angel_letter(
    tmp_path: Path,
) -> None:
    """A pending duplicate group is one angel: one letter, one count."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
                changed = await db_duplications.sync_pending_angel_letters(
                    repo, "screen"
                )
                children = {
                    row["zite_record_id"]: row
                    for row in await repo.final_rows("child")
                }
            assert changed == 1
            assert children["c1"]["angel_id"] == "1A"
            assert children["c1b"]["angel_id"] == "1A"
        finally:
            await uow.engine.dispose()

    _run(main())


def test_not_duplicate_child_gets_its_own_angel_letter(
    tmp_path: Path,
) -> None:
    """Resolving a member as unique gives it a letter of its own."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
                await db_duplications.sync_pending_angel_letters(repo, "screen")
                groups = await db_duplications.list_groups(repo)
                group = next(
                    item for item in groups if item["entity"] == "child"
                )
                detail = await db_duplications.decide(
                    repo,
                    cfg,
                    group["id"],
                    "c1",
                    db_duplications.DECISION_NOT_DUPLICATE,
                    "auditor",
                    confirm=True,
                )
                children = {
                    row["zite_record_id"]: row
                    for row in await repo.final_rows("child")
                }
                trail = await repo.duplication_audit_rows()
            assert detail["status"] == "resolved"
            assert children["c1"]["angel_id"] == "1B"
            assert children["c1"]["potential_duplicate"] is False
            assert children["c1b"]["angel_id"] == "1A"
            assert trail == [
                {
                    "id": trail[0]["id"],
                    "sheet": "child_submissions",
                    "group_key": "fam@example.com|Kiddo",
                    "zite_record_id": "c1",
                    "decision": "not_duplicate",
                    "assigned_id": "1B",
                    "actor": "auditor",
                    "created_at": trail[0]["created_at"],
                }
            ]
        finally:
            await uow.engine.dispose()

    _run(main())


def test_ignore_decision_ignores_the_member(tmp_path: Path) -> None:
    """The ignore decision behaves like the audit tool's ignore action."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
                groups = await db_duplications.list_groups(repo)
                group = next(
                    item for item in groups if item["entity"] == "child"
                )
                await db_duplications.decide(
                    repo,
                    cfg,
                    group["id"],
                    "c1",
                    db_duplications.DECISION_IGNORE,
                    "auditor",
                    confirm=True,
                )
                children = {
                    row["zite_record_id"]: row
                    for row in await repo.final_rows("child")
                }
                trail = await repo.duplication_audit_rows()
            assert children["c1"]["ignore"] is True
            assert children["c1"]["status"] == "ignored"
            assert trail[0]["decision"] == "ignore"
        finally:
            await uow.engine.dispose()

    _run(main())


def test_not_duplicate_family_gets_its_own_family_id(tmp_path: Path) -> None:
    """A released family submission becomes its own family row."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
                groups = await db_duplications.list_groups(repo)
                group = next(
                    item for item in groups if item["entity"] == "family"
                )
                detail = await db_duplications.decide(
                    repo,
                    cfg,
                    group["id"],
                    "rec_f1",
                    db_duplications.DECISION_NOT_DUPLICATE,
                    "auditor",
                    confirm=True,
                )
                families = {
                    row["family_id"]: row
                    for row in await repo.final_rows("family")
                }
                trail = await repo.duplication_audit_rows()
            assert set(families) == {1, 2}
            assert families[2]["zite_record_id"] == "rec_f1"
            assert families[2]["potential_duplicate"] is False
            assert trail[0]["assigned_id"] == "2"
            assert detail["members"][0]["decision"] == "not_duplicate"
        finally:
            await uow.engine.dispose()

    _run(main())


def test_not_duplicate_sponsor_keeps_its_own_sponsor_id(
    tmp_path: Path,
) -> None:
    """A sponsor submission already owns a unique id; only the flag goes."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
                await repo.replace_screen_diagnostics(
                    [
                        _log_row(
                            "sponsor_submissions",
                            "sponsor1@example.com",
                            "rec_s1",
                            "2026-01-01T10:00:00Z",
                            True,
                            _raw_sponsor("rec_s1"),
                        )
                    ],
                    [],
                    "screen",
                )
                groups = await db_duplications.list_groups(repo)
                group = next(
                    item for item in groups if item["entity"] == "sponsor"
                )
                await db_duplications.decide(
                    repo,
                    cfg,
                    group["id"],
                    "rec_s1",
                    db_duplications.DECISION_NOT_DUPLICATE,
                    "auditor",
                    confirm=True,
                )
                sponsors = {
                    row["sponsor_id"]: row
                    for row in await repo.final_rows("sponsor")
                }
                trail = await repo.duplication_audit_rows()
            assert sponsors[1]["potential_duplicate"] is False
            assert trail[0]["assigned_id"] == "1"
        finally:
            await uow.engine.dispose()

    _run(main())


def test_repeating_a_decision_changes_nothing(tmp_path: Path) -> None:
    """A retry after a lost response neither re-letters nor re-trails."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
                await db_duplications.sync_pending_angel_letters(repo, "screen")
                groups = await db_duplications.list_groups(repo)
                group = next(
                    item for item in groups if item["entity"] == "child"
                )
                for _ in range(2):
                    await db_duplications.decide(
                        repo,
                        cfg,
                        group["id"],
                        "c1",
                        db_duplications.DECISION_NOT_DUPLICATE,
                        "auditor",
                        confirm=True,
                    )
                children = {
                    row["zite_record_id"]: row
                    for row in await repo.final_rows("child")
                }
                trail = await repo.duplication_audit_rows()
            assert children["c1"]["angel_id"] == "1B"
            assert children["c1b"]["angel_id"] == "1A"
            assert len(trail) == 1
        finally:
            await uow.engine.dispose()

    _run(main())


def test_decision_requires_confirmation(tmp_path: Path) -> None:
    """An unconfirmed decision is refused."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
                groups = await db_duplications.list_groups(repo)
                with pytest.raises(Exception, match="confirm=True"):
                    await db_duplications.decide(
                        repo,
                        cfg,
                        groups[0]["id"],
                        "c1",
                        db_duplications.DECISION_IGNORE,
                        "auditor",
                        confirm=False,
                    )
        finally:
            await uow.engine.dispose()

    _run(main())


def test_screen_run_leaves_one_letter_for_a_pending_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real screening run leaves the pending duplicate on one letter."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)
    _use_fake_ai(monkeypatch)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await repo.replace_raw_snapshot(
                    "migrate",
                    {
                        "sponsor": [_raw_sponsor("rec_s1")],
                        "family": [
                            _raw_family("rec_f1", "2026-01-05T10:00:00Z")
                        ],
                        "child": [
                            _raw_child("c1", "2026-02-01T09:00:00+00:00"),
                            _raw_child("c1b", "2026-02-02T09:00:00+00:00"),
                        ],
                    },
                    "migration",
                )
            await db_pipeline.screen(
                cfg,
                "topher",
                {"skip_email_check": True, "skip_address_check": True},
                lambda *args, **kwargs: None,
                lambda: None,
            )
            async with uow.store(cfg, "topher") as repo:
                children = {
                    row["zite_record_id"]: row
                    for row in await repo.final_rows("child")
                }
                duplicates = await repo.duplicate_log_rows()
            assert {row["zite_record_id"] for row in duplicates} == {
                "c1",
                "c1b",
            }
            assert children["c1"]["angel_id"] is not None
            assert children["c1"]["angel_id"] == children["c1b"]["angel_id"]
        finally:
            await uow.engine.dispose()

    _run(main())


def test_mark_audited_refuses_a_group_with_no_surviving_entry(
    tmp_path: Path,
) -> None:
    """A group whose every member is decided has no surviving entry."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
                group = await _group(repo, "child")
                for record_id in ("c1", "c1b"):
                    await db_duplications.decide(
                        repo,
                        cfg,
                        group["id"],
                        record_id,
                        db_duplications.DECISION_NOT_DUPLICATE,
                        "auditor",
                        confirm=True,
                    )
                with pytest.raises(Exception, match="no surviving entry"):
                    await db_duplications.mark_audited(
                        repo, cfg, group["id"], "auditor"
                    )
                children = await _rows(repo, "child")
                states = await repo.duplication_state_rows()
            assert children["c1"]["status"] == "new"
            assert children["c1b"]["status"] == "new"
            assert states == []
        finally:
            await uow.engine.dispose()

    _run(main())


def test_mark_audited_updates_the_group_state_in_the_list(
    tmp_path: Path,
) -> None:
    """The group list reports the resolved and audited states."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
                group = await _group(repo, "child")
                await db_duplications.decide(
                    repo,
                    cfg,
                    group["id"],
                    "c1",
                    db_duplications.DECISION_IGNORE,
                    "auditor",
                    confirm=True,
                )
                child_group = await _group(repo, "child")
                assert child_group["status"] == "resolved"
                await db_duplications.mark_audited(
                    repo, cfg, group["id"], "auditor"
                )
                child_group = await _group(repo, "child")
            assert child_group["status"] == "audited"
        finally:
            await uow.engine.dispose()

    _run(main())


def test_mark_audited_finalizes_the_undecided_surviving_entry(
    tmp_path: Path,
) -> None:
    """One discarded member plus one untouched member audits the latter."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
                group = await _group(repo, "child")
                await db_duplications.decide(
                    repo,
                    cfg,
                    group["id"],
                    "c1",
                    db_duplications.DECISION_IGNORE,
                    "auditor",
                    confirm=True,
                )
                detail = await db_duplications.mark_audited(
                    repo, cfg, group["id"], "auditor"
                )
                children = await _rows(repo, "child")
                states = await repo.duplication_state_rows()
            assert detail["status"] == "audited"
            assert detail["members"][0]["state"] == "discarded"
            assert detail["members"][1]["state"] == "surviving"
            assert children["c1b"]["status"] == "audited"
            assert children["c1b"]["audited_at"] is not None
            assert children["c1"]["ignore"] is True
            assert children["c1"]["status"] == "ignored"
            assert states[0]["audited_by"] == "auditor"
        finally:
            await uow.engine.dispose()

    _run(main())


def test_mark_audited_requires_exactly_one_surviving_entry(
    tmp_path: Path,
) -> None:
    """Two undecided members and no undecided member are both refused."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
                group = await _group(repo, "child")
                with pytest.raises(
                    Exception, match="more than one surviving entry"
                ):
                    await db_duplications.mark_audited(
                        repo, cfg, group["id"], "auditor"
                    )
                for record_id in ("c1", "c1b"):
                    await db_duplications.decide(
                        repo,
                        cfg,
                        group["id"],
                        record_id,
                        db_duplications.DECISION_IGNORE,
                        "auditor",
                        confirm=True,
                    )
                with pytest.raises(Exception, match="no surviving entry"):
                    await db_duplications.mark_audited(
                        repo, cfg, group["id"], "auditor"
                    )
        finally:
            await uow.engine.dispose()

    _run(main())


def test_group_status_resolves_only_with_one_undecided_member(
    tmp_path: Path,
) -> None:
    """A group is pending until exactly one submission is undecided."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
                group = await _group(repo, "child")
                assert group["status"] == "pending"
                assert group["survivor_count"] == 2
                await db_duplications.decide(
                    repo,
                    cfg,
                    group["id"],
                    "c1",
                    db_duplications.DECISION_IGNORE,
                    "auditor",
                    confirm=True,
                )
                group = await _group(repo, "child")
            assert group["status"] == "resolved"
            assert group["survivor_count"] == 1
        finally:
            await uow.engine.dispose()

    _run(main())


def test_discarding_the_flagged_survivor_promotes_the_other_member(
    tmp_path: Path,
) -> None:
    """The flagged survivor is only a hint; the undecided member survives."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
                group = await _group(repo, "child")
                assert group["survivor_record_id"] == "c1b"
                await db_duplications.decide(
                    repo,
                    cfg,
                    group["id"],
                    "c1b",
                    db_duplications.DECISION_IGNORE,
                    "auditor",
                    confirm=True,
                )
                detail = await db_duplications.mark_audited(
                    repo, cfg, group["id"], "auditor"
                )
                children = await _rows(repo, "child")
            assert detail["members"][0]["state"] == "surviving"
            assert detail["members"][1]["state"] == "discarded"
            assert children["c1"]["status"] == "audited"
            assert children["c1b"]["status"] == "ignored"
        finally:
            await uow.engine.dispose()

    _run(main())


def test_mark_audited_carries_the_reviewed_values(tmp_path: Path) -> None:
    """The surviving entry's values and audited status land in one save."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
                group = await _group(repo, "child")
                await db_duplications.decide(
                    repo,
                    cfg,
                    group["id"],
                    "c1",
                    db_duplications.DECISION_IGNORE,
                    "auditor",
                    confirm=True,
                )
                version = (await _rows(repo, "child"))["c1b"]["_row_version"]
                await db_duplications.mark_audited(
                    repo,
                    cfg,
                    group["id"],
                    "auditor",
                    values={"first_name": "Marcus", "dob": "2018-06-01"},
                )
                children = await _rows(repo, "child")
            assert children["c1b"]["first_name"] == "Marcus"
            assert children["c1b"]["dob"] == "2018-06-01"
            assert children["c1b"]["status"] == "audited"
            assert children["c1b"]["_row_version"] == version + 1
        finally:
            await uow.engine.dispose()

    _run(main())


def test_audited_surviving_entry_reaches_the_family_rollup(
    tmp_path: Path,
) -> None:
    """The surviving entry's letter appears in the family's angel list."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
                group = await _group(repo, "child")
                await db_duplications.decide(
                    repo,
                    cfg,
                    group["id"],
                    "c1",
                    db_duplications.DECISION_IGNORE,
                    "auditor",
                    confirm=True,
                )
                await db_duplications.mark_audited(
                    repo, cfg, group["id"], "auditor"
                )
                children = await _rows(repo, "child")
                families = await _rows(repo, "family")
            assert children["c1b"]["angel_id"] == "1A"
            assert families[1]["assigned_angel_ids"] == "1A"
        finally:
            await uow.engine.dispose()

    _run(main())


def test_mark_audited_refuses_a_stale_expected_version(
    tmp_path: Path,
) -> None:
    """A concurrent edit of the surviving row is never overwritten."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
                group = await _group(repo, "child")
                await db_duplications.decide(
                    repo,
                    cfg,
                    group["id"],
                    "c1",
                    db_duplications.DECISION_IGNORE,
                    "auditor",
                    confirm=True,
                )
                with pytest.raises(StaleRowVersionError):
                    await db_duplications.mark_audited(
                        repo,
                        cfg,
                        group["id"],
                        "auditor",
                        values={"first_name": "Marcus"},
                        expected_version=999,
                    )
                children = await _rows(repo, "child")
            assert children["c1b"]["first_name"] == "Kiddo"
            assert children["c1b"]["status"] == "new"
        finally:
            await uow.engine.dispose()

    _run(main())


def test_audited_surviving_entry_is_reaffirmed_but_values_are_final(
    tmp_path: Path,
) -> None:
    """No values re-affirms an audited entry; a value change is refused."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
                group = await _group(repo, "child")
                await db_duplications.decide(
                    repo,
                    cfg,
                    group["id"],
                    "c1",
                    db_duplications.DECISION_IGNORE,
                    "auditor",
                    confirm=True,
                )
                await db_duplications.mark_audited(
                    repo,
                    cfg,
                    group["id"],
                    "auditor",
                    values={"first_name": "Marcus"},
                )
                detail = await db_duplications.mark_audited(
                    repo, cfg, group["id"], "auditor"
                )
                with pytest.raises(RowAuditedError):
                    await db_duplications.mark_audited(
                        repo,
                        cfg,
                        group["id"],
                        "auditor",
                        values={"first_name": "Other"},
                    )
                children = await _rows(repo, "child")
            assert detail["status"] == "audited"
            assert children["c1b"]["first_name"] == "Marcus"
        finally:
            await uow.engine.dispose()

    _run(main())


def test_released_member_row_returns_to_new(tmp_path: Path) -> None:
    """A released member becomes its own unaudited row, even if audited."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
                children = await repo.final_rows("child")
                next(row for row in children if row["zite_record_id"] == "c1")[
                    "status"
                ] = "audited"
                await repo.commit_finals("audit", "child", children, "auditor")
                group = await _group(repo, "child")
                await db_duplications.decide(
                    repo,
                    cfg,
                    group["id"],
                    "c1",
                    db_duplications.DECISION_NOT_DUPLICATE,
                    "auditor",
                    confirm=True,
                )
                released = await _rows(repo, "child")
                assert released["c1"]["status"] == "audited"
                await db_duplications.mark_audited(
                    repo, cfg, group["id"], "auditor"
                )
                children = await _rows(repo, "child")
            assert children["c1"]["status"] == "new"
            assert children["c1"]["angel_id"] == "1B"
            assert children["c1b"]["status"] == "audited"
        finally:
            await uow.engine.dispose()

    _run(main())


def test_discarded_member_row_is_left_ignored_and_untouched(
    tmp_path: Path,
) -> None:
    """The group audit never resurrects a destroyed row."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
                group = await _group(repo, "child")
                await db_duplications.decide(
                    repo,
                    cfg,
                    group["id"],
                    "c1",
                    db_duplications.DECISION_IGNORE,
                    "auditor",
                    confirm=True,
                )
                ignored = await _rows(repo, "child")
                assert ignored["c1"]["interests"] is None
                await db_duplications.mark_audited(
                    repo, cfg, group["id"], "auditor"
                )
                children = await _rows(repo, "child")
            assert children["c1"]["status"] == "ignored"
            assert children["c1"]["ignore"] is True
            assert children["c1"]["interests"] is None
            assert children["c1"]["dob"] is None
        finally:
            await uow.engine.dispose()

    _run(main())


def test_mark_audited_is_idempotent(tmp_path: Path) -> None:
    """Auditing the same group twice reconciles without a second state row."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
                group = await _group(repo, "child")
                await db_duplications.decide(
                    repo,
                    cfg,
                    group["id"],
                    "c1",
                    db_duplications.DECISION_IGNORE,
                    "auditor",
                    confirm=True,
                )
                for _ in range(2):
                    detail = await db_duplications.mark_audited(
                        repo, cfg, group["id"], "auditor"
                    )
                states = await repo.duplication_state_rows()
            assert detail["status"] == "audited"
            assert len(states) == 1
        finally:
            await uow.engine.dispose()

    _run(main())


def test_two_member_family_group_audits_its_shared_row_once(
    tmp_path: Path,
) -> None:
    """A shared family row is saved once, even when a member is released."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
                group = await _group(repo, "family")
                assert group["member_count"] == 2
                await db_duplications.decide(
                    repo,
                    cfg,
                    group["id"],
                    "rec_f1",
                    db_duplications.DECISION_NOT_DUPLICATE,
                    "auditor",
                    confirm=True,
                )
                families = await _rows(repo, "family")
                assert set(families) == {1, 2}
                version = families[1]["_row_version"]
                detail = await db_duplications.mark_audited(
                    repo,
                    cfg,
                    group["id"],
                    "auditor",
                    values={"first_name": "Marcus"},
                )
                families = await _rows(repo, "family")
            assert detail["status"] == "audited"
            assert families[1]["first_name"] == "Marcus"
            assert families[1]["status"] == "audited"
            assert families[1]["_row_version"] == version + 1
            assert families[1]["zite_record_id"] == "rec_f1"
            assert families[2]["status"] == "new"
        finally:
            await uow.engine.dispose()

    _run(main())


def test_split_family_children_move_to_the_chosen_family(
    tmp_path: Path,
) -> None:
    """Each child ends in one family with a letter of that family."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
                group = await _group(repo, "family")
                await db_duplications.decide(
                    repo,
                    cfg,
                    group["id"],
                    "rec_f1",
                    db_duplications.DECISION_NOT_DUPLICATE,
                    "auditor",
                    confirm=True,
                )
                children = await repo.final_rows("child")
                for row in children:
                    row["status"] = "audited"
                await repo.commit_finals("audit", "child", children, "auditor")
                detail = await db_duplications.assign_group_children(
                    repo, group["id"], "auditor", {"c1": 2, "c1b": 1}
                )
                children = await _rows(repo, "child")
                families = await _rows(repo, "family")
            assert children["c1"]["family_id"] == 2
            assert children["c1"]["angel_id"].startswith("2")
            assert children["c1b"]["family_id"] == 1
            assert children["c1b"]["angel_id"] == "1A"
            assert families[1]["assigned_angel_ids"] == "1A"
            assert (
                families[2]["assigned_angel_ids"] == children["c1"]["angel_id"]
            )
            assert families[1]["num_angels_in_family"] == 1
            assert families[2]["num_angels_in_family"] == 1
            assert {child["family_id"] for child in detail["children"]} == {
                1,
                2,
            }
        finally:
            await uow.engine.dispose()

    _run(main())


def test_list_groups_reports_survivor_and_released_counts(
    tmp_path: Path,
) -> None:
    """The list distinguishes undecided candidates from released members."""
    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)

    async def main() -> None:
        uow = _store(cfg)
        try:
            async with uow.store(cfg, "migration") as repo:
                await _seed(repo)
                group = await _group(repo, "child")
                await db_duplications.decide(
                    repo,
                    cfg,
                    group["id"],
                    "c1",
                    db_duplications.DECISION_NOT_DUPLICATE,
                    "auditor",
                    confirm=True,
                )
                child_group = await _group(repo, "child")
                family_group = await _group(repo, "family")
            assert child_group["survivor_count"] == 1
            assert child_group["released_count"] == 1
            assert child_group["decided_count"] == 1
            assert family_group["survivor_count"] == 2
            assert family_group["released_count"] == 0
        finally:
            await uow.engine.dispose()

    _run(main())
