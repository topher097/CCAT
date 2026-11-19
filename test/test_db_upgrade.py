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
