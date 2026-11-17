"""CLI dispatch contract for workbook migration commands."""

from typing import Any

import pytest

import ccat


def test_consolidate_dispatch_passes_paths_and_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Required paths and optional conversion flags dispatch intact."""
    captured: dict[str, Any] = {}

    def dispatch(name: str, runner: str, config: str, **kwargs: Any) -> None:
        captured.update(name=name, runner=runner, config=config, **kwargs)

    monkeypatch.setattr(ccat, "_dispatch", dispatch)
    ccat._consolidate_migrate_cmd(
        config="season.json",
        **{
            "from": "old.xlsx",
            "output": "new.xlsx",
            "force": True,
            "backup": "backup.xlsx",
            "report": "report.json",
        },
    )
    assert captured == {
        "name": "consolidate-migrate",
        "runner": "consolidate:run",
        "config": "season.json",
        "from_path": "old.xlsx",
        "output_path": "new.xlsx",
        "force": True,
        "backup_path": "backup.xlsx",
        "report_path": "report.json",
    }


def test_consolidate_dispatch_requires_paths() -> None:
    """Missing input or output is actionable, not a runner TypeError."""
    with pytest.raises(SystemExit, match="--from PATH and --output PATH"):
        ccat._consolidate_migrate_cmd(from_path="old.xlsx")
    with pytest.raises(SystemExit, match="--from PATH and --output PATH"):
        ccat._consolidate_migrate_cmd(output_path="new.xlsx")


def test_consolidate_dispatch_rejects_unknown_flags() -> None:
    """Unexpected migration flags fail before config/loading."""
    kwargs: dict[str, object] = {
        "from": "old.xlsx",
        "output": "new.xlsx",
        "dry_run": True,
    }
    with pytest.raises(SystemExit, match="unknown flags"):
        ccat._consolidate_migrate_cmd(
            **kwargs  # ty: ignore[invalid-argument-type]
        )
