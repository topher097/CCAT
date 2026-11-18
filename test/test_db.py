"""Tests for the CCAT persistence layer (refactor Phase 2a).

Pins the schema contract (table set and the three persistent-final
column inventories, including the two deliberate renames), the
Python-only age derivation (plan §4.2), the Alembic migration
round-trip, SQLite connection PRAGMAs, and bootstrap seeding.
"""

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from ccat.db.engine import (
    ALEMBIC_DIR,
    create_async_engine,
    create_sync_engine,
    run_migrations,
    sync_url_from,
)
from ccat.db.models import Base, User
from ccat.db.seed import seed_bootstrap
from ccat.domain import assign_angel_ids, compute_age_at_xmas

#: Every table the initial schema must create (plan §4.1 groups).
EXPECTED_TABLES = frozenset(
    {
        "seasons",
        "final_child",
        "final_family",
        "final_sponsor",
        "acquisition_runs",
        "raw_sponsor",
        "raw_family",
        "raw_child",
        "screening_runs",
        "duplicate_log",
        "dob_conflict_log",
        "email_checks",
        "email_check_targets",
        "address_checks",
        "match_runs",
        "match_report_rows",
        "match_assignment_snapshots",
        "artifacts",
        "jobs",
        "job_events",
        "users",
        "sessions",
        "security_audit_events",
        "clarification_families",
        "clarification_drafts",
    }
)

#: final_child column contract: the audit-store persistent schema
#: (§4.3) plus the row-version/attribution columns, with the legacy
#: ``potential_anomoly`` renamed to ``ai_screening_notes``.
FINAL_CHILD_COLUMNS = frozenset(
    {
        "season_id",
        "zite_record_id",
        "zite_created_at",
        "created",
        "family_id",
        "angel_id",
        "first_name",
        "primary_email",
        "dob",
        "age_reported",
        "gender",
        "special_needs",
        "need_1",
        "need_2",
        "desire_1",
        "desire_2",
        "shoe_size",
        "shoe_size_type",
        "pant_waist_size",
        "pant_inseam",
        "pant_size_letter",
        "pant_size_type",
        "jean_size",
        "shirt_size_letter",
        "shirt_size_type",
        "favorite_colors",
        "interests",
        "other_notes",
        "ai_screening_notes",
        "source",
        "age_at_xmas",
        "generated_info_sheet",
        "screened",
        "potential_duplicate",
        "status",
        "ignore",
        "clarification_notes",
        "audited_at",
        "row_version",
        "created_by",
        "last_updated_by",
        "created_at",
        "last_updated_at",
    }
)

#: final_family column contract (§4.3) — no unique on primary_email.
FINAL_FAMILY_COLUMNS = frozenset(
    {
        "season_id",
        "family_id",
        "zite_record_id",
        "zite_created_at",
        "created",
        "primary_email",
        "first_name",
        "last_name",
        "phone",
        "street_address",
        "apt_floor",
        "city",
        "zip_code",
        "state",
        "secondary_first_name",
        "secondary_last_name",
        "secondary_email",
        "secondary_phone",
        "gift_pickup_location",
        "source",
        "potential_duplicate",
        "accepts_disclaimer",
        "assigned_sponsor_id",
        "assigned_angel_ids",
        "num_angels_in_family",
        "status",
        "clarification_notes",
        "audited_at",
        "ignore",
        "row_version",
        "created_by",
        "last_updated_by",
        "created_at",
        "last_updated_at",
    }
)

#: final_sponsor column contract (§4.3) with the legacy ``create``
#: renamed to ``created``.
FINAL_SPONSOR_COLUMNS = frozenset(
    {
        "season_id",
        "sponsor_id",
        "zite_record_id",
        "created",
        "email",
        "sponsor_type",
        "organization_name",
        "prior_assigned_angel_ids",
        "first_name",
        "last_name",
        "phone",
        "num_children",
        "age_preference",
        "gender_preference",
        "dropoff_location",
        "source",
        "potential_duplicate",
        "accepts_disclaimer",
        "pref_age_min",
        "pref_age_max",
        "assigned_angel_ids",
        "num_angels_assigned",
        "status",
        "clarification_notes",
        "audited_at",
        "ignore",
        "row_version",
        "created_by",
        "last_updated_by",
        "created_at",
        "last_updated_at",
    }
)


def test_all_tables_present() -> None:
    """Every planned table exists, and nothing extra."""
    assert set(Base.metadata.tables) == EXPECTED_TABLES


@pytest.mark.parametrize(
    ("table", "expected_columns"),
    [
        ("final_child", FINAL_CHILD_COLUMNS),
        ("final_family", FINAL_FAMILY_COLUMNS),
        ("final_sponsor", FINAL_SPONSOR_COLUMNS),
    ],
)
def test_final_column_inventories(
    table: str, expected_columns: frozenset[str]
) -> None:
    """The final stores carry exactly the contract columns."""
    columns = {c.name for c in Base.metadata.tables[table].columns}
    assert columns == expected_columns


def test_deliberate_renames_applied() -> None:
    """Legacy spellings exist only in the adapters, not the DB."""
    child = {c.name for c in Base.metadata.tables["final_child"].columns}
    sponsor = {c.name for c in Base.metadata.tables["final_sponsor"].columns}
    assert "ai_screening_notes" in child
    assert "potential_anomoly" not in child
    assert "created" in sponsor
    assert "create" not in sponsor


def test_composite_season_keys() -> None:
    """Final stores are keyed by (season_id, stable entity id)."""
    for table, entity_key in (
        ("final_child", "zite_record_id"),
        ("final_family", "family_id"),
        ("final_sponsor", "sponsor_id"),
    ):
        pk = [c.name for c in Base.metadata.tables[table].primary_key.columns]
        assert pk == ["season_id", entity_key]


@pytest.mark.parametrize(
    ("dob", "expected"),
    [
        ("2020-12-25", 6),  # birthday on Christmas counts
        ("2020-12-26", 5),  # day after: still the younger age on Xmas
        ("2024-02-29", 2),  # leap-day DOB ages normally
        ("2020-02-29", 6),  # leap-day DOB across several non-leap years
    ],
)
def test_compute_age_at_xmas_boundaries(dob: str, expected: int) -> None:
    """One Python function is the age authority — no Excel DATEDIF."""
    assert compute_age_at_xmas(dob, date(2026, 12, 25)) == expected


def test_unparsable_dob_is_none_not_guessed() -> None:
    """Unparsable DOB degrades to None, never a guessed age.

    The primitive raises; the assignment pipeline maps it to None
    (§4.2).
    """

    class _Screening:
        sibling_dob_max_gap_days = 274

    class _Cfg:
        @property
        def xmas(self) -> date:
            return date(2026, 12, 25)

        @property
        def screening(self) -> _Screening:
            return _Screening()

    with pytest.raises(ValueError):
        compute_age_at_xmas("not-a-date", date(2026, 12, 25))
    rows = assign_angel_ids(
        [
            {
                "zite_record_id": "bad-1",
                "dob": "not-a-date",
                "primary_email": None,
                "first_name": None,
            }
        ],
        _Cfg(),  # type: ignore[arg-type]
    )
    assert rows[0]["age_at_xmas"] is None


def _alembic_config(url: str) -> AlembicConfig:
    """Build the programmatic Alembic config used by run_migrations."""
    config = AlembicConfig()
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    config.set_main_option("sqlalchemy.url", sync_url_from(url))
    return config


def _table_names(url: str) -> set[str]:
    """Inspect a sync sqlite URL for its table names."""
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_migration_round_trip(tmp_path: Path) -> None:
    """Migration round-trip on a fresh file.

    Upgrade creates all tables; downgrade drops them; upgrade again
    works on the same file.
    """
    url = f"sqlite:///{tmp_path / 'ccat.sqlite3'}"
    run_migrations(url)
    tables = _table_names(url)
    assert EXPECTED_TABLES <= tables
    assert "alembic_version" in tables

    command.downgrade(_alembic_config(url), "base")
    tables = _table_names(url)
    assert EXPECTED_TABLES.isdisjoint(tables)
    assert tables == {"alembic_version"}

    run_migrations(url)
    assert EXPECTED_TABLES <= _table_names(url)


def test_run_migrations_accepts_async_url(tmp_path: Path) -> None:
    """The async config spelling derives the sync migration URL."""
    async_url = f"sqlite+aiosqlite:///{tmp_path / 'ccat.sqlite3'}"
    run_migrations(async_url)
    assert EXPECTED_TABLES <= _table_names(sync_url_from(async_url))


def test_async_engine_pragmas(tmp_path: Path) -> None:
    """Async runtime connections get the SQLite PRAGMAs (§4.4).

    WAL journaling, foreign-key enforcement, and the busy timeout.
    """

    async def check() -> tuple[Any, Any, Any]:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'ccat.sqlite3'}"
        )
        try:
            async with engine.connect() as connection:
                journal_mode = await connection.scalar(
                    text("PRAGMA journal_mode")
                )
                foreign_keys = await connection.scalar(
                    text("PRAGMA foreign_keys")
                )
                busy_timeout = await connection.scalar(
                    text("PRAGMA busy_timeout")
                )
            return journal_mode, foreign_keys, busy_timeout
        finally:
            await engine.dispose()

    journal_mode, foreign_keys, busy_timeout = asyncio.run(check())
    assert str(journal_mode).lower() == "wal"
    assert int(foreign_keys) == 1
    assert int(busy_timeout) == 15000


def test_seed_bootstrap_idempotent(tmp_path: Path) -> None:
    """Seeding twice leaves the same four bootstrap users (§5.2, §8)."""
    url = f"sqlite:///{tmp_path / 'ccat.sqlite3'}"
    run_migrations(url)
    engine = create_sync_engine(url)
    try:
        seed_bootstrap(engine)
        seed_bootstrap(engine)
        with Session(engine) as session:
            users = {
                user.username: user for user in session.scalars(select(User))
            }
    finally:
        engine.dispose()
    assert set(users) == {"cmendres", "lmboswell", "system", "migration"}
    assert users["cmendres"].roles == ["admin", "coordinator", "auditor"]
    assert users["cmendres"].dev_no_password is True
    assert users["lmboswell"].roles == ["coordinator", "auditor"]
    assert users["lmboswell"].dev_no_password is True
    assert users["system"].dev_no_password is False
    assert users["system"].roles == []
    assert users["migration"].active is True
    assert users["migration"].dev_no_password is False
