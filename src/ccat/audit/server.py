"""Module 3 — local human audit web server (§7).

A Python-stdlib ``http.server`` serving one static HTML page plus a small
JSON API.  No frameworks, no extra dependencies, single human user.  The
tool reviews raw vs. adjudicated child data side by side and commits
audited rows to ``final_child``; it never touches ``final_family`` or
``final_sponsor`` (§7.3).
"""

import json
import logging
import re
from datetime import datetime, UTC
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TYPE_CHECKING
from urllib.parse import urlsplit

import polars as pl

from ccat.config import Config
from ccat.io import read_sheet, write_sheets
from ccat.screening import ADJ_CHILD_COLUMNS, first_family_children

if TYPE_CHECKING:
    from collections.abc import Callable
    from polars._typing import PolarsDataType

logger = logging.getLogger(__name__)

#: adj_child fields displayed but never editable: ``age_at_xmas`` is
#: computed by acquire (§5.5) and never overridden by the audit tool.
ADJ_READ_ONLY = ("age_at_xmas",)

#: Editable adjudicable fields (everything in adj_child except the key
#: and read-only fields).
ADJ_FIELDS = tuple(
    column
    for column in ADJ_CHILD_COLUMNS
    if column not in ("zite_record_id",) + ADJ_READ_ONLY
)

#: Queue/API display order: editable fields with read-only fields placed
#: beside the raw field they annotate (age_at_xmas after dob).
_dob_index = ADJ_FIELDS.index("dob")
ADJ_QUEUE_FIELDS = (
    ADJ_FIELDS[: _dob_index + 1] + ADJ_READ_ONLY + ADJ_FIELDS[_dob_index + 1 :]
)

#: Control fields accepted in a PUT body alongside the adj fields (§7.2).
CONTROL_FIELDS = ("special_needs", "audited", "require_clarification")

#: Extra final_child columns beyond the raw_child columns (§7.3).
FINAL_CHILD_EXTRA = (
    "potential_anomoly",
    "special_needs",
    "audited",
    "require_clarification",
    "audited_at",
)

#: Pre-filled clarification note for orphaned children (§7.2).
ORPHAN_NOTE = "No matching family registration for this child's primary email"


class AuditError(Exception):
    """Raised when the audit tool cannot complete a request."""


class AuditClientError(Exception):
    """Raised for client errors carrying an HTTP status code."""

    def __init__(self, status: int, message: str) -> None:
        """Store the HTTP status and message.

        Args:
            status: HTTP status code to return (400/404/409).
            message: Human-readable explanation.
        """
        super().__init__(message)
        self.status = status


def _as_bool(value: Any) -> bool | None:
    """Coerce a sheet/API value to bool, tolerating string booleans."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    return None


class AuditState:
    """Reads and writes the audit queue and final_child rows (§7)."""

    def __init__(self, cfg: Config, test_mode: bool = False) -> None:
        """Bind the state to one pipeline configuration.

        Args:
            cfg: Pipeline configuration.
            test_mode: Limit the queue to the first family's children and
                fire ``on_complete`` once that family is fully audited.
        """
        self.cfg = cfg
        self.test_mode = test_mode
        self.on_complete: Callable[[], None] | None = None

    def _load_sheets(
        self,
    ) -> tuple[pl.DataFrame, pl.DataFrame | None, pl.DataFrame | None]:
        """Load raw_child, adj_child, and final_child sheets.

        Returns:
            A tuple of (raw_child rows, adj_child rows keyed lookup is done
            by callers, final_child frame).

        Raises:
            AuditError: If raw_child is missing (run `ccat acquire` first).
        """
        raw_child = read_sheet(self.cfg.excel_file, "raw_child")
        if raw_child is None or raw_child.is_empty():
            raise AuditError(
                "raw_child sheet is missing or empty; run `ccat acquire` first"
            )
        adj_child = read_sheet(self.cfg.excel_file, "adj_child")
        final_child = read_sheet(self.cfg.excel_file, "final_child")
        return raw_child, adj_child, final_child

    def queue(self) -> list[dict[str, Any]]:
        """Children needing review (§7.1).

        Every child in raw_child whose zite_record_id has no final_child
        row, or has one with ``audited == false``.  In test mode the queue
        is limited to the first family's children.

        Returns:
            Queue items: ``{zite_record_id, angel_id, raw, adj}`` where
            ``adj`` is the adj_child row (all-null fields when screened
            with no changes) or the null object when not screened yet.
        """
        raw_child, adj_child, final_child = self._load_sheets()
        adj_by_id: dict[Any, dict[str, Any]] = {}
        if adj_child is not None:
            for row in adj_child.to_dicts():
                adj_by_id[row.get("zite_record_id")] = row
        final_by_id: dict[Any, dict[str, Any]] = {}
        if final_child is not None:
            for row in final_child.to_dicts():
                final_by_id[row.get("zite_record_id")] = row
        items: list[dict[str, Any]] = []
        for raw_row in raw_child.to_dicts():
            record_id = raw_row.get("zite_record_id")
            final_row = final_by_id.get(record_id)
            if (
                final_row is not None
                and _as_bool(final_row.get("audited")) is True
            ):
                continue
            adj_row = adj_by_id.get(record_id)
            if adj_row is not None:
                adj = {field: adj_row.get(field) for field in ADJ_QUEUE_FIELDS}
                adj["zite_record_id"] = record_id
            else:
                adj = None
            items.append(
                {
                    "zite_record_id": record_id,
                    "angel_id": raw_row.get("angel_id"),
                    "raw": raw_row,
                    "adj": adj,
                }
            )
        if self.test_mode:
            allowed = {
                child["zite_record_id"]
                for child in first_family_children(raw_child.to_dicts())
            }
            items = [
                item for item in items if item["zite_record_id"] in allowed
            ]
        return items

    def save(self, zite_record_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Write one child's final_child row (§7.1, §7.3).

        Args:
            zite_record_id: The child's join key.
            body: The edited adj JSON plus control fields.

        Returns:
            The written final_child row.

        Raises:
            AuditClientError: With status 400 for invalid bodies, 404 for
                unknown children, and 409 for frozen (already audited)
                rows or audit attempts on orphaned children.
        """
        unknown = [
            key for key in body if key not in ADJ_FIELDS + CONTROL_FIELDS
        ]
        if unknown:
            raise AuditClientError(400, f"unknown keys: {sorted(unknown)}")
        raw_child, _adj_child, final_child = self._load_sheets()
        raw_rows = raw_child.to_dicts()
        raw_row = next(
            (
                row
                for row in raw_rows
                if row.get("zite_record_id") == zite_record_id
            ),
            None,
        )
        if raw_row is None:
            raise AuditClientError(404, f"unknown child {zite_record_id!r}")
        audited = _as_bool(body.get("audited"))
        if audited is None:
            raise AuditClientError(400, "audited must be a boolean")
        special_needs = _as_bool(body.get("special_needs"))
        if special_needs is None:
            raise AuditClientError(400, "special_needs must be a boolean")
        require_clarification = body.get("require_clarification")
        if require_clarification is not None and not isinstance(
            require_clarification, str
        ):
            raise AuditClientError(
                400, "require_clarification must be a string"
            )
        if (
            require_clarification is not None
            and require_clarification.strip() == ""
        ):
            require_clarification = None
        if raw_row.get("angel_id") is None and audited:
            raise AuditClientError(
                400,
                "orphaned child cannot be marked audited before a matching"
                " family registration exists (§7.2)",
            )
        final_rows: list[dict[str, Any]] = []
        if final_child is not None:
            final_rows = final_child.to_dicts()
        existing = next(
            (
                row
                for row in final_rows
                if row.get("zite_record_id") == zite_record_id
            ),
            None,
        )
        if existing is not None and _as_bool(existing.get("audited")) is True:
            raise AuditClientError(
                409,
                f"child {zite_record_id!r} is already audited and frozen (§7.3)",
            )
        final_row = build_final_child_row(
            raw_row, body, special_needs, audited, require_clarification
        )
        if existing is None:
            final_rows.append(final_row)
        else:
            final_rows[final_rows.index(existing)] = final_row
        frame = _final_child_frame(final_rows, raw_child)
        write_sheets(self.cfg.excel_file, "audit", {"final_child": frame})
        logger.info(
            "saved final_child row for %s (audited=%s)", zite_record_id, audited
        )
        if self.test_mode and not self.queue():
            logger.info(
                "test mode: first family fully audited; stopping audit tool"
            )
            if self.on_complete is not None:
                self.on_complete()
        return final_row


def build_final_child_row(
    raw_row: dict[str, Any],
    adj_body: dict[str, Any],
    special_needs: bool,
    audited: bool,
    require_clarification: str | None,
) -> dict[str, Any]:
    """Join raw_child with the edited adj JSON into one final_child row (§7.3).

    Starts from the full raw_child row and replaces any adjudicable field
    whose adj value is non-null, then always sets the control fields.

    Args:
        raw_row: The full raw_child row.
        adj_body: The edited adj JSON from the audit UI.
        special_needs: The special-needs checkbox value.
        audited: Whether the row is being marked audited.
        require_clarification: Optional free-text clarification note.

    Returns:
        The complete final_child row.
    """
    row = dict(raw_row)
    for field in ADJ_FIELDS:
        value = adj_body.get(field)
        if value is not None:
            row[field] = value
    row["zite_record_id"] = raw_row["zite_record_id"]
    row["angel_id"] = raw_row.get("angel_id")
    row["potential_anomoly"] = adj_body.get("potential_anomoly")
    row["special_needs"] = special_needs
    row["audited"] = audited
    row["require_clarification"] = require_clarification
    row["audited_at"] = datetime.now(UTC).isoformat()
    return row


def _final_child_frame(
    rows: list[dict[str, Any]], raw_child: pl.DataFrame
) -> pl.DataFrame:
    """Build the final_child DataFrame preserving column order and dtypes."""
    schema: dict[str, PolarsDataType] = dict(raw_child.schema)
    schema.update(
        {
            "potential_anomoly": pl.String,
            "special_needs": pl.Boolean,
            "audited": pl.Boolean,
            "require_clarification": pl.String,
            "audited_at": pl.String,
        }
    )
    frame = pl.DataFrame(
        [{key: row.get(key) for key in schema} for row in rows], schema=schema
    )
    return frame.select(list(schema))


def make_handler(state: AuditState) -> type[BaseHTTPRequestHandler]:
    """Build the request handler bound to one audit state."""

    class AuditHandler(BaseHTTPRequestHandler):
        """HTTP handler for the audit UI and JSON API."""

        server_version = "CCATAudit/1.0"

        def _send_json(self, status: int, payload: object) -> None:
            """Write a JSON response."""
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            """Serve the UI page and the queue API."""
            path = urlsplit(self.path).path
            if path == "/":
                index_path = Path(__file__).parent / "static" / "index.html"
                if not index_path.is_file():
                    self._send_json(500, {"error": "index.html missing"})
                    return
                data = index_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/api/queue":
                try:
                    items = state.queue()
                except AuditError as error:
                    self._send_json(500, {"error": str(error)})
                    return
                self._send_json(200, items)
                return
            self._send_json(404, {"error": "not found"})

        def do_PUT(self) -> None:  # noqa: N802 - http.server API
            """Save one child's edited adj JSON to final_child."""
            match = re.fullmatch(
                r"/api/children/([^/]+)", urlsplit(self.path).path
            )
            if match is None:
                self._send_json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            try:
                body = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"error": "body is not valid JSON"})
                return
            if not isinstance(body, dict):
                self._send_json(400, {"error": "body must be a JSON object"})
                return
            try:
                row = state.save(match.group(1), body)
            except AuditClientError as error:
                self._send_json(error.status, {"error": str(error)})
                return
            except AuditError as error:
                self._send_json(500, {"error": str(error)})
                return
            self._send_json(200, {"ok": True, "row": row})

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            """Route http.server access logs to the pipeline logger."""
            logger.info("%s - %s", self.address_string(), format % args)

    return AuditHandler


def run(cfg: Config, test: bool = False) -> None:
    """Run module 3: serve the audit tool until interrupted (§7).

    Args:
        cfg: Pipeline configuration.
        test: Stop after the first full family: the queue only contains
            the first family's children and the server shuts itself down
            once that family is fully audited.  Exits immediately when the
            first family is already audited.
    """
    state = AuditState(cfg, test_mode=test)
    if test and not state.queue():
        logger.info(
            "test mode: first family already audited; nothing to review"
        )
        return
    server = ThreadingHTTPServer(
        (cfg.audit.host, cfg.audit.port), make_handler(state)
    )
    state.on_complete = server.shutdown
    logger.info(
        "audit tool listening on http://%s:%d (Ctrl-C to stop%s)",
        cfg.audit.host,
        cfg.audit.port,
        "; test mode: stops after the first family is audited" if test else "",
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("audit tool stopped")
    finally:
        server.server_close()
