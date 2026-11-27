"""Tests for the controlled schema-migration CLI command."""

from __future__ import annotations

import sqlite3
from typing import Any, TYPE_CHECKING

import pytest

from ccat.config import load_config
from ccat.db.cli import run as db_upgrade

if TYPE_CHECKING:
    from ccat.config import Config
    from pathlib import Path


def _cfg(tmp_path: Path, **updates: Any) -> Config:
    """Point the configured database at a temporary file."""
    base = load_config("config/ccat_2026.json")
    storage = base.storage.model_copy(
        update={
            "database_url": (
                "sqlite+aiosqlite:///" + str(tmp_path / "ccat.sqlite3")
            )
        }
    )
    return base.model_copy(update={"storage": storage, **updates})


def test_db_upgrade_creates_schema_and_is_idempotent(tmp_path: Path) -> None:
    """The command migrates to head and can be re-run safely."""
    cfg = _cfg(tmp_path)
    db_upgrade(cfg)
    db_upgrade(cfg)

    path = tmp_path / "ccat.sqlite3"
    tables = {
        row[0]
        for row in sqlite3.connect(path).execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {"final_child", "final_family", "final_sponsor", "jobs"} <= tables


def test_db_upgrade_creates_missing_parent_directory(tmp_path: Path) -> None:
    """A missing SQLite directory is created before migrating."""
    cfg = _cfg(tmp_path)
    cfg = cfg.model_copy(
        update={
            "storage": cfg.storage.model_copy(
                update={
                    "database_url": (
                        "sqlite+aiosqlite:///"
                        + str(tmp_path / "nested" / "dir" / "ccat.sqlite3")
                    )
                }
            )
        }
    )
    db_upgrade(cfg)
    assert (tmp_path / "nested" / "dir" / "ccat.sqlite3").exists()


def test_db_upgrade_names_a_revision_the_build_does_not_have(
    tmp_path: Path,
) -> None:
    """A database at an unknown revision is diagnosed, not left to Alembic.

    The deployment runs one branch's migrations against a shared volume,
    so the stored revision can belong to a build that is not deployed.
    """
    from ccat.db.errors import MigrationStateError
    from ccat.db.engine import run_migrations

    cfg = _cfg(tmp_path)
    run_migrations(cfg.storage.database_url)
    connection = sqlite3.connect(tmp_path / "ccat.sqlite3")
    connection.execute(
        "UPDATE alembic_version SET version_num = '20990101_9999'"
    )
    connection.commit()
    connection.close()

    with pytest.raises(MigrationStateError) as failure:
        run_migrations(cfg.storage.database_url)
    assert "out of step" in str(failure.value)
    assert "20990101_9999" in str(failure.value)


def test_db_upgrade_retires_cmendres400_account(tmp_path: Path) -> None:
    """The retired dev account and the audit rows it owns are removed."""
    from alembic import command
    from alembic.config import Config as AlembicConfig

    from ccat.db.engine import ALEMBIC_DIR

    url = f"sqlite:///{tmp_path / 'ccat.sqlite3'}"
    config = AlembicConfig()
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    config.set_main_option("sqlalchemy.url", url)
    # Before the retirement revision the account still exists.
    command.upgrade(config, "20261119_0002")
    connection = sqlite3.connect(tmp_path / "ccat.sqlite3")
    connection.execute(
        "INSERT INTO users (username, display_name, active, roles,"
        " dev_no_password, otp_enrolled)"
        " VALUES ('cmendres400', NULL, 1, '[\"admin\"]', 1, 0)"
    )
    connection.execute(
        "INSERT INTO security_audit_events (ts, user_id, action, object_type,"
        " object_id) VALUES (CURRENT_TIMESTAMP,"
        " (SELECT id FROM users WHERE username = 'cmendres400'),"
        " 'logout', 'user', 'cmendres400')"
    )
    connection.commit()
    connection.close()

    command.upgrade(config, "head")
    connection = sqlite3.connect(tmp_path / "ccat.sqlite3")
    usernames = {
        row[0] for row in connection.execute("SELECT username FROM users")
    }
    events = connection.execute(
        "SELECT count(*) FROM security_audit_events"
    ).fetchone()[0]
    connection.close()
    assert "cmendres400" not in usernames
    assert events == 0


def test_db_upgrade_settles_walmart_claim_takeovers(tmp_path: Path) -> None:
    """Contested claims move the child to the specific claimant (0011)."""
    from alembic import command
    from alembic.config import Config as AlembicConfig

    from ccat.db.engine import ALEMBIC_DIR

    url = f"sqlite:///{tmp_path / 'ccat.sqlite3'}"
    config = AlembicConfig()
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "20261123_0010")
    connection = sqlite3.connect(tmp_path / "ccat.sqlite3")
    # The live shape: a Walmart placeholder holds the child; an
    # individual claims the same angel; a manual placement nearby must
    # survive untouched.
    connection.executescript(
        """
        INSERT INTO final_family
            (season_id, family_id, zite_record_id, ignore, status,
             row_version, created_by, last_updated_by)
        VALUES (1, 36, 'rec_f36', 0, 'audited', 1, 'migration', 'migration'),
               (1, 100, 'rec_f100', 0, 'audited', 1, 'migration', 'migration');

        INSERT INTO final_sponsor
            (season_id, sponsor_id, zite_record_id, sponsor_type,
             prior_assigned_angel_ids, num_children,
             num_angels_assigned, ignore, status,
             row_version, created_by, last_updated_by)
        VALUES (1, 35, 'rec_s35', 'Walmart', '36A', 1, 1, 0, 'new',
                1, 'migration', 'migration'),
               (1, 39, 'rec_s39', 'Walmart', '100A', 1, 1, 0, 'new',
                1, 'migration', 'migration'),
               (1, 47, 'rec_s47', 'Individual', '100A', 1, 0, 0, 'new',
                1, 'migration', 'migration'),
               (1, 55, 'rec_s55', 'Individual', '36A', 1, 0, 0, 'new',
                1, 'migration', 'migration');

        INSERT INTO final_child
            (season_id, zite_record_id, family_id, angel_id,
             assigned_sponsor_id, assignment_source, ignore, status,
             row_version, created_by, last_updated_by)
        VALUES (1, 'rec_c36a', 36, '36A', 35, 'auto', 0, 'audited',
                1, 'migration', 'migration'),
               (1, 'rec_c100a', 100, '100A', 39, 'auto', 0, 'audited',
                1, 'migration', 'migration'),
               (1, 'rec_cmanual', 100, '100B', 39, 'manual', 0, 'audited',
                1, 'migration', 'migration');
        """
    )
    connection.commit()
    connection.close()

    command.upgrade(config, "head")

    connection = sqlite3.connect(tmp_path / "ccat.sqlite3")
    children = {
        row[0]: (row[1], row[2])
        for row in connection.execute(
            "SELECT angel_id, assigned_sponsor_id, assignment_source"
            " FROM final_child"
        )
    }
    sponsors = {
        row[0]: (row[1], row[2])
        for row in connection.execute(
            "SELECT sponsor_id, assigned_angel_ids, num_angels_assigned"
            " FROM final_sponsor"
        )
    }
    families = {
        row[0]: (row[1], row[2])
        for row in connection.execute(
            "SELECT family_id, assigned_sponsor_id, assigned_angel_ids"
            " FROM final_family"
        )
    }
    connection.close()

    # The specific claimants take the angels over from the placeholders.
    assert children["100A"] == (47, "auto")
    assert children["36A"] == (55, "auto")
    # A manual placement is never moved.
    assert children["100B"] == (39, "manual")
    # The derived summaries follow the children.
    assert sponsors[47] == ("100A", 1)
    assert sponsors[55] == ("36A", 1)
    assert sponsors[39] == ("100B", 1)
    assert sponsors[35] == (None, 0)
    # Family 100 spans both the taken-over child and the manual one, so
    # it is a split family: no single sponsor, both angels named.
    assert families[100] == (None, "100A,100B")
    assert families[36] == (55, "36A")


def test_cli_exposes_db_upgrade_command() -> None:
    """The Fire dispatch advertises the new command."""
    import ccat

    assert callable(ccat.main)
    from ccat import _db_upgrade_cmd

    assert callable(_db_upgrade_cmd)


@pytest.mark.parametrize("command", ["db-import", "workbook-export", "worker"])
def test_cli_exposes_new_commands(command: str) -> None:
    """Phase 2/3 additions stay part of the CLI surface."""
    import ccat

    assert hasattr(ccat, "_dispatch")
    doc = ccat.__doc__ or ""
    assert command in doc or command.replace("-", "_") in doc
