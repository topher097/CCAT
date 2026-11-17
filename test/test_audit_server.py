"""Tests for the audit server: process discovery and run() (§9.3)."""

import os
import signal
from pathlib import Path
from typing import Any

import pytest

from ccat.audit import server
from ccat.audit.server import (
    _ancestor_pids,
    find_audit_processes,
    run,
    stop_audit_processes,
)
from ccat.audit.store import read_adj
from ccat.config import Config
from ccat.io import read_sheet


def _write_proc(root: Path, pid: int, arguments: list[str]) -> None:
    """Fake one /proc entry with an NUL-separated cmdline."""
    entry = root / str(pid)
    entry.mkdir()
    cmdline = entry / "cmdline"
    cmdline.write_bytes(b"\0".join(arg.encode() for arg in arguments) + b"\0")


class TestRestart:
    """--restart finds and stops a stale audit server (§7)."""

    def test_find_audit_processes_matches_ccat_audit(
        self, tmp_path: Path
    ) -> None:
        """Only ccat-audit processes (never self or ancestors) match."""
        _write_proc(tmp_path, 100, ["python", "/venv/bin/ccat", "audit"])
        _write_proc(tmp_path, 101, ["python", "-m", "ccat", "audit", "--test"])
        _write_proc(  # unrelated: dbus carries "audit" as substring
            tmp_path,
            102,
            ["dbus-broker", "--audit"],
        )
        _write_proc(  # ccat with a different module
            tmp_path, 103, ["python", "/venv/bin/ccat", "screen"]
        )
        found = find_audit_processes(str(tmp_path))
        assert sorted(found) == [100, 101]

    def test_find_audit_processes_never_matches_own_ancestry(self) -> None:
        """Wrapper commands (timeout, uv run, shells) are excluded."""
        assert all(
            pid not in find_audit_processes() for pid in _ancestor_pids()
        )

    def test_stop_audit_processes_terminates(self, tmp_path: Path) -> None:
        """SIGTERM goes to each pid; gone processes are not re-killed."""
        killed: list[tuple[int, int]] = []

        def fake_kill(pid: int, sig: int) -> None:
            killed.append((pid, sig))
            if sig == 0 and pid in {7, 9} and len(killed) > 2:
                raise ProcessLookupError  # alive once, then exits

        monkey = pytest.MonkeyPatch()
        monkey.setattr(os, "kill", fake_kill)
        monkey.setattr("ccat.audit.server.time.sleep", lambda _s: None)
        monkey.setattr("ccat.audit.server.time.monotonic", lambda: 0.0)
        try:
            stop_audit_processes(
                {7: "ccat audit", 9: "ccat audit --test"}, timeout_seconds=0.1
            )
        finally:
            monkey.undo()
        assert (7, signal.SIGTERM) in killed
        assert (9, signal.SIGTERM) in killed
        # escalation only when the process survives the deadline
        assert not any(sig == signal.SIGKILL for _pid, sig in killed)

    def test_stop_audit_processes_escalates_to_sigkill(self) -> None:
        """A process ignoring SIGTERM is killed after the deadline."""
        killed: list[tuple[int, int]] = []

        def fake_kill(pid: int, sig: int) -> None:
            killed.append((pid, sig))
            if sig == 0 and len(killed) > 6:
                raise ProcessLookupError  # survives SIGTERM, then dies

        monkey = pytest.MonkeyPatch()
        monkey.setattr(os, "kill", fake_kill)
        ticks = iter(range(0, 100, 10))

        def fake_monotonic() -> float:
            return float(next(ticks))

        monkey.setattr("ccat.audit.server.time.monotonic", fake_monotonic)
        monkey.setattr("ccat.audit.server.time.sleep", lambda _s: None)
        try:
            stop_audit_processes({5: "ccat audit"}, timeout_seconds=5.0)
        finally:
            monkey.undo()
        assert (5, signal.SIGKILL) in killed


class TestRun:
    """run() seeds, installs state, and serves via NiceGUI (§9.3)."""

    @pytest.fixture
    def seeded_cfg(self, cfg: Config) -> Config:
        """Return a config whose workbook has the raw sheets."""
        from test_audit_store import seed_raw

        seed_raw(cfg)
        return cfg

    def test_run_seeds_and_serves(
        self, seeded_cfg: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """run() seeds adj sheets, sets state, and calls ui.run."""
        started: dict[str, Any] = {}

        def fake_ui_run(**kwargs: Any) -> None:
            started.update(kwargs)

        monkeypatch.setattr(server.ui, "run", fake_ui_run)
        run(seeded_cfg)
        assert started["host"] == seeded_cfg.audit.host
        assert started["port"] == seeded_cfg.audit.port
        assert started["reload"] is False
        assert "Human Audit" in started["title"]
        children = read_adj(seeded_cfg, "child")
        assert len(children) == 3
        final_child = read_sheet(seeded_cfg.excel_file, "final_child")
        assert final_child is not None and final_child.height == 2
        # The orphaned child stays in adj_child but never in final_child.
        assert "orphan1" not in final_child["zite_record_id"].to_list()

    def test_run_restart_stops_stale_server_first(
        self,
        seeded_cfg: Config,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """restart=True stops found processes before serving."""
        stopped: dict[int, str] = {}
        monkeypatch.setattr(
            "ccat.audit.server.find_audit_processes",
            lambda: {4242: "ccat audit"},
        )
        monkeypatch.setattr(
            "ccat.audit.server.stop_audit_processes",
            lambda processes, **_kw: stopped.update(processes),
        )
        monkeypatch.setattr(server.ui, "run", lambda **_kw: None)
        run(seeded_cfg, restart=True)
        assert stopped == {4242: "ccat audit"}

    def test_run_requires_raw_sheets(self, cfg: Config) -> None:
        """Missing raw sheets fail with an actionable error."""
        from ccat.audit import AuditError

        with pytest.raises(AuditError, match="acquire"):
            run(cfg)
