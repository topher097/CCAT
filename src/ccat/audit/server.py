"""Module 3 — the NiceGUI audit server (§9.1, §9.3).

``run()`` loads the configuration, seeds the adj sheets
(:func:`ccat.audit.store.ensure_final_sheets`), installs the app state,
and serves the pages from :mod:`ccat.audit.pages` until interrupted.
The process-discovery helpers keep ``--restart`` working: a stale
audit tool still holding the port is stopped before the new one binds.
"""

import logging
import os
import re
import signal
import time
from pathlib import Path
from typing import Any

from nicegui import ui

from ccat.audit import AuditError, pages
from ccat.audit.store import ensure_final_sheets
from ccat.config import Config

logger = logging.getLogger(__name__)


def _is_audit_command(arguments: list[bytes]) -> bool:
    """Whether a /proc cmdline looks like the audit tool being started.

    Matches ``ccat audit`` and ``python .../ccat audit`` (any entrypoint
    naming ccat plus a bare ``audit`` argument); unrelated processes
    containing the substring "audit" (dbus, kauditd, ...) never match.

    Args:
        arguments: The NUL-split ``/proc/<pid>/cmdline`` bytes.

    Returns:
        True when the command starts the audit tool.
    """
    args = [argument.decode(errors="replace") for argument in arguments]
    names = {Path(argument).name for argument in args}
    return "audit" in args and bool(
        names & {"ccat", "ccat-audit", "__main__.py"}
    )


def _ancestor_pids(pid: int = os.getpid()) -> set[int]:
    """Collect the process's own ancestry (itself included).

    Wrapper commands (``timeout``, ``uv run``, a shell) carry matching
    ``ccat audit`` arguments in their cmdline; they must never be
    stopped by the audit tool's own restart.

    Args:
        pid: Starting pid (the running process by default).

    Returns:
        The set of pids on the path from ``pid`` to init.
    """
    chain: set[int] = set()
    current: int | None = pid
    while current is not None and current not in chain:
        chain.add(current)
        try:
            status = Path(f"/proc/{current}/status").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            break
        match = re.search(r"^PPid:\s+(\d+)", status, re.MULTILINE)
        current = int(match.group(1)) if match else None
        if current == 0:
            break
    return chain


def find_audit_processes(proc_dir: str = "/proc") -> dict[int, str]:
    """Find running audit tool processes (§7).

    Args:
        proc_dir: The procfs mount to scan (overridable for tests).

    Returns:
        Mapping of pid -> command line (arguments space-joined) for
        every other live process that looks like ``ccat audit``.  The
        running process and its ancestors are never matched.
    """
    protected = _ancestor_pids()
    found: dict[int, str] = {}
    for entry in Path(proc_dir).iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in protected:
            continue
        try:
            arguments = entry.joinpath("cmdline").read_bytes()
        except OSError:  # vanished or not ours to read
            continue
        if not arguments:
            continue  # kernel thread
        if _is_audit_command(arguments.split(b"\0")):
            found[pid] = arguments.replace(b"\0", b" ").decode(errors="replace")
    return found


def stop_audit_processes(
    processes: dict[int, str], timeout_seconds: float = 5.0
) -> None:
    """Terminate the given processes, escalating to SIGKILL (§7).

    Args:
        processes: Mapping of pid -> command line from
            :func:`find_audit_processes`.
        timeout_seconds: How long to wait for SIGTERM to work before
            killing each process outright.
    """
    for pid, command in processes.items():
        logger.warning("stopping running audit tool pid %d (%s)", pid, command)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue  # already gone
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break  # exited on SIGTERM
            time.sleep(0.05)
        else:
            logger.warning("audit tool pid %d ignored SIGTERM; killing", pid)
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # exited between check and kill
        # Reap so a killed child does not linger as a zombie.
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass  # not our child, or already reaped


def run(
    cfg: Config,
    test: bool = False,
    restart: bool = False,
    **_unused: Any,
) -> None:
    """Run module 3: serve the audit tool until interrupted (§9.3).

    Args:
        cfg: Pipeline configuration.
        test: Limit every list to the first family (lowest
            ``family_id``), its children, and the first sponsor; the
            dashboard reflects the reduced scope (§10.2).
        restart: Stop any currently running audit tool process before
            starting, so a stale server does not block the port.

    Raises:
        AuditError: If the raw sheets are missing (run ``ccat acquire``
            first) or the configured port cannot be bound.
    """
    if restart:
        stale = find_audit_processes()
        if stale:
            stop_audit_processes(stale)
        else:
            logger.info("restart: no running audit tool process found")
    ensure_final_sheets(cfg)
    pages.set_state(pages.AuditState(cfg, test_mode=test))
    logger.info(
        "audit tool listening on http://%s:%d (Ctrl-C to stop%s)",
        cfg.audit.host,
        cfg.audit.port,
        "; test mode: first family + first sponsor scope" if test else "",
    )
    try:
        ui.run(
            host=cfg.audit.host,
            port=cfg.audit.port,
            title=f"Human Audit — {cfg.season}",
            reload=False,
            show=True,
        )
    except OSError as error:
        raise AuditError(
            f"could not bind http://{cfg.audit.host}:{cfg.audit.port}:"
            f" {error}; another process is using the port (use --restart"
            " to stop a running audit tool, or change audit.port in the"
            " config)"
        ) from error
