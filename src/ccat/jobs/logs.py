"""Capture ``ccat`` log records for the durable job event stream.

The worker attaches :class:`JobLogHandler` to the ``ccat`` logger for
the duration of one job; every record is rendered into the frontend
contract (``kind="log"``) and buffered in memory by :class:`LogBuffer`.
A pump task drains the buffer into ``job_events`` in batches, so
:meth:`JobLogHandler.emit` never does I/O and never blocks the logging
caller.
"""

import logging
import threading
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

#: Event kind stored for one captured log line.
LOG_EVENT_KIND = "log"


def _event(record: logging.LogRecord) -> dict[str, Any]:
    """Render one log record as the ``kind="log"`` event payload.

    Args:
        record: The record being emitted.

    Returns:
        The event data dict (level, logger, message, ts).
    """
    message = record.getMessage()
    if record.exc_info:
        message += "\n" + logging.Formatter().formatException(record.exc_info)
    return {
        "level": record.levelname,
        "logger": record.name,
        "message": message,
        "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
    }


class LogBuffer:
    """Thread-safe buffer between ``emit`` and the worker's pump task."""

    def __init__(self) -> None:
        """Initialize an empty buffer."""
        self._lock = threading.Lock()
        self._lines: deque[dict[str, Any]] = deque()

    def append(self, line: dict[str, Any]) -> None:
        """Append one event payload (called from ``emit``).

        Args:
            line: The event data dict.
        """
        with self._lock:
            self._lines.append(line)

    def drain(self) -> list[dict[str, Any]]:
        """Take every buffered payload, oldest first.

        Returns:
            The drained payloads; empty when nothing is pending.
        """
        with self._lock:
            lines = list(self._lines)
            self._lines.clear()
        return lines


class JobLogHandler(logging.Handler):
    """Forward ``ccat`` records to a sink without doing I/O in ``emit``."""

    def __init__(self, sink: Callable[[dict[str, Any]], None]) -> None:
        """Initialize the handler.

        Args:
            sink: Callback receiving one event payload per record.
        """
        super().__init__()
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        """Render and forward one record; never raise into the caller.

        Args:
            record: The record to capture.
        """
        try:
            self._sink(_event(record))
        except Exception:  # noqa: BLE001 - logging must never break a job
            pass


__all__ = ["LOG_EVENT_KIND", "JobLogHandler", "LogBuffer"]
