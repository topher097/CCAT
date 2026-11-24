"""Dispatch: the sponsor packet work list, the drafts, and the sends.

The dispatch page is the last mile of the program: generate the child
information sheets and the sponsor packets, draft each sponsor's email
from the program's template, and send it with the packet PDFs attached.

Three rules shape this module.

**The packet build is the truth.** Every packet build writes a
``manifest.json`` naming the children it was built from and the files it
contains with their hashes.  The draft body is rendered from that
manifest, never from the live assignment, so the email and the attached
PDF always describe the same children.  A send re-checks the manifest
against the current assignment and the current file bytes and refuses
when they no longer agree.

**A send is an attempt, not a boolean.** A network failure does not prove
the message was not accepted, so every send writes an attempt row with
one of three outcomes: ``sent`` (accepted), ``failed`` (the provider
refused it, so a retry is safe) and ``unknown`` (the provider's answer
does not say, so it is never retried automatically).

**Sending is a trust boundary.** Only an admin may send, every attempt is
audited, and no body, address or attachment content is written to a log
line.

The service returns plain data; the routes own the HTTP shapes.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment
from sqlalchemy.exc import IntegrityError

from ccat.audit.store import as_bool, eligible_output_rows
from ccat.db.artifacts import artifact_root
from ccat.db.errors import StaleRowVersionError
from ccat.services.db_assignments import child_sponsor
from ccat.services.ports import Attachment, MailError, SendOutcome, SendRequest

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ccat.config import Config
    from ccat.db.repositories import SeasonStoreRepository
    from ccat.services.ports import MailTransport


#: Codes a :class:`DispatchError` may carry.  The routes map them to
#: HTTP statuses; the service itself carries no status numbers.
NOT_FOUND = "not_found"
CONFLICT = "conflict"
MAIL_NOT_CONFIGURED = "mail_not_configured"
MAIL_FAILED = "mail_failed"
INVALID_INPUT = "invalid_input"
STALE_VERSION = "stale_version"

#: How long a send attempt may stay "sending" before the recovery sweep
#: calls its outcome unknown.
STALE_ATTEMPT_SECONDS = 900

#: Transport outcome -> attempt state.  The transport says what the
#: provider did; the attempt records what happened to the message.
_STATE_BY_OUTCOME: dict[str, str] = {
    "sent": "sent",
    "rejected": "failed",
    "ambiguous": "unknown",
}


class DispatchError(Exception):
    """A refusal the dispatch routes turn into an HTTP error.

    Attributes:
        code: Stable machine-readable code.
        context: Extra JSON-safe context fields.
    """

    def __init__(self, code: str, message: str, **context: Any) -> None:
        """Initialize the error.

        Args:
            code: Stable machine-readable code.
            message: Safe human-readable message.
            context: Extra JSON-safe context fields.
        """
        self.code = code
        self.context = context
        super().__init__(message)


def _text(value: Any) -> str | None:
    """Return a trimmed string, or None.

    Args:
        value: The raw value.

    Returns:
        The trimmed text, or None when it is absent or blank.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value: Any) -> int | None:
    """Return an integer, or None.

    Args:
        value: The raw value.

    Returns:
        The integer, or None when it is absent or not a number.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _packet_dir(cfg: Config, sponsor_id: int) -> Path:
    """Return one sponsor's packet directory.

    Args:
        cfg: Pipeline configuration.
        sponsor_id: The sponsor's key.

    Returns:
        The packet directory path.
    """
    return artifact_root(cfg) / "sponsor_packets" / str(sponsor_id)


def read_manifest(cfg: Config, sponsor_id: int) -> dict[str, Any] | None:
    """Read one sponsor's packet manifest.

    Args:
        cfg: Pipeline configuration.
        sponsor_id: The sponsor's key.

    Returns:
        The manifest payload, or None when the packet has none.
    """
    path = _packet_dir(cfg, sponsor_id) / "manifest.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _render_body(
    cfg: Config, manifest: dict[str, Any], note: str | None
) -> str:
    """Render the email body from a packet manifest.

    Args:
        cfg: Pipeline configuration.
        manifest: The packet manifest.
        note: The coordinator's plain-text addition.

    Returns:
        The rendered HTML body.
    """
    template_file = Path(cfg.sponsor_packets.email_template_html_file)
    environment = Environment(autoescape=True)
    template = environment.from_string(
        template_file.read_text(encoding="utf-8")
    )
    items = {str(item.get("role")): item for item in manifest.get("items", [])}
    angels = list(manifest.get("angels", []))
    sponsor = dict(manifest.get("sponsor") or {})
    return template.render(
        season=manifest.get("season") or cfg.season,
        sponsor=sponsor,
        angels=angels,
        angel_count=len(angels),
        subject=manifest.get("subject") or "",
        instructions_filename=str(
            (items.get("instructions_pdf") or {}).get("filename") or ""
        ),
        child_information_filename=str(
            (items.get("packet_pdf") or {}).get("filename") or ""
        ),
        bcc_emails=cfg.sponsor_packets.bcc_emails,
        manual=False,
        note=(note or "").strip(),
    )


def _manifest_attachments(
    manifest: dict[str, Any], roles: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Build the attachment list from a manifest.

    Args:
        manifest: The packet manifest.
        roles: The sponsor's artifact rows, keyed by role, so the popup
            can offer a download for each file.

    Returns:
        One attachment dict per file the packet carries.
    """
    attachments: list[dict[str, Any]] = []
    for item in manifest.get("items", []):
        role = str(item.get("role") or "")
        if role == "email_html":
            continue
        attachments.append(
            {
                "artifact_id": _int((roles.get(role) or {}).get("id")),
                "role": role,
                "filename": str(item.get("filename") or ""),
                "content_type": (
                    "text/html" if role == "email_html" else "application/pdf"
                ),
                "size_bytes": int(item.get("size_bytes") or 0),
                "sha256": str(item.get("sha256") or ""),
            }
        )
    return attachments


def _assigned_children(
    child_rows: list[dict[str, Any]],
) -> dict[int, list[dict[str, Any]]]:
    """Group the eligible children by the sponsor they are placed with.

    Args:
        child_rows: The eligible child rows.

    Returns:
        Mapping of sponsor id -> that sponsor's children.
    """
    grouped: dict[int, list[dict[str, Any]]] = {}
    for child in child_rows:
        owner = child_sponsor(child)
        if owner is None or not child.get("angel_id"):
            continue
        grouped.setdefault(int(owner), []).append(child)
    return grouped


def _packet_artifacts(
    artifacts: list[dict[str, Any]],
) -> dict[int, dict[str, dict[str, Any]]]:
    """Index the season's packet artifacts by sponsor and role.

    New packets carry ``meta["sponsor_id"]`` and ``meta["role"]``.
    Packets built before that carry none, so the root-relative path is
    the fallback.

    Args:
        artifacts: The season's artifact rows.

    Returns:
        Mapping of sponsor id -> role -> artifact row.
    """
    indexed: dict[int, dict[str, dict[str, Any]]] = {}
    for row in artifacts:
        kind = _text(row.get("kind"))
        if kind not in ("sponsor_packet", "sponsor_instructions"):
            continue
        meta = row.get("meta")
        meta = meta if isinstance(meta, dict) else {}
        sponsor_id = _int(meta.get("sponsor_id"))
        role = _text(meta.get("role"))
        if sponsor_id is None or role is None:
            path = _text(row.get("path")) or ""
            parts = path.split("/")
            if len(parts) < 3 or parts[0] != "sponsor_packets":
                continue
            sponsor_id = _int(parts[1])
            filename = parts[-1]
            if filename.lower().endswith(".html"):
                role = "email_html"
            elif "Child Information" in filename:
                role = "packet_pdf"
            else:
                role = "instructions_pdf"
        if sponsor_id is None:
            continue
        roles = indexed.setdefault(sponsor_id, {})
        existing = roles.get(role)
        if existing is None or int(row.get("id") or 0) >= int(
            existing.get("id") or 0
        ):
            roles[role] = row
    return indexed


async def _stores(
    repo: SeasonStoreRepository,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Read the eligible child, family and sponsor stores.

    Args:
        repo: The season-bound repository.

    Returns:
        The three eligible stores.
    """
    child_rows = eligible_output_rows(await repo.final_rows("child"), "child")
    family_rows = eligible_output_rows(
        await repo.final_rows("family"), "family"
    )
    sponsor_rows = eligible_output_rows(
        await repo.final_rows("sponsor"), "sponsor"
    )
    return child_rows, family_rows, sponsor_rows


def _sponsor_name(row: dict[str, Any]) -> str:
    """Return a sponsor's display name.

    Args:
        row: The sponsor row.

    Returns:
        The organization name, or the person's name.
    """
    organization = _text(row.get("organization_name"))
    if organization:
        return organization
    return " ".join(
        part
        for part in (_text(row.get("first_name")), _text(row.get("last_name")))
        if part
    )


def _blockers(
    *,
    email: str | None,
    assigned_count: int,
    num_children: int | None,
    sheets_ready: int,
    manifest: dict[str, Any] | None,
    draft: dict[str, Any] | None,
    attempt_state: str | None,
    mail_configured: bool,
) -> list[str]:
    """Derive why a sponsor cannot be sent right now.

    Args:
        email: The sponsor's address.
        assigned_count: How many children are placed with the sponsor.
        num_children: How many children the sponsor asked for.
        sheets_ready: How many of those children have a sheet.
        manifest: The sponsor's packet manifest, when it has one.
        draft: The sponsor's current draft, when it has one.
        attempt_state: The latest attempt's state.
        mail_configured: Whether the mail account is usable.

    Returns:
        The blocker codes, empty when the sponsor is ready.
    """
    blockers: list[str] = []
    if not email:
        blockers.append("no_email")
    if sheets_ready < assigned_count:
        blockers.append("missing_sheets")
    if num_children is not None and assigned_count < num_children:
        blockers.append("slots_not_filled")
    if manifest is None:
        blockers.append("manifest_missing")
    if attempt_state == "sent":
        blockers.append("already_sent")
    if draft is not None and manifest is not None:
        if _text(draft.get("build_id")) != _text(manifest.get("build_id")):
            blockers.append("packet_changed")
    if not mail_configured:
        blockers.append("mail_not_configured")
    return blockers


def _current_children(children: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Describe the children a sponsor is placed with.

    Args:
        children: The sponsor's eligible children.

    Returns:
        One entry per child with its id and sheet state.
    """
    described: list[dict[str, Any]] = []
    for child in children:
        described.append(
            {
                "child_id": child.get("zite_record_id"),
                "angel_id": child.get("angel_id"),
                "generated": as_bool(child.get("generated_info_sheet")),
            }
        )
    described.sort(key=lambda item: str(item.get("angel_id") or ""))
    return described


def _manifest_matches(
    manifest: dict[str, Any], children: list[dict[str, Any]]
) -> bool:
    """Whether a manifest still describes the current children.

    Args:
        manifest: The packet manifest.
        children: The sponsor's current eligible children.

    Returns:
        True when the manifest's children match the current ones.
    """
    manifest_children = sorted(
        (str(entry.get("child_id")), str(entry.get("angel_id")))
        for entry in manifest.get("children", [])
    )
    current = sorted(
        (str(child.get("child_id")), str(child.get("angel_id")))
        for child in children
    )
    return manifest_children == current


def _email_status(
    draft: dict[str, Any] | None, attempt_state: str | None
) -> str:
    """Derive the email state of one sponsor.

    Args:
        draft: The sponsor's current draft, when it has one.
        attempt_state: The latest attempt's state.

    Returns:
        The email status.
    """
    if attempt_state in ("sending", "sent", "failed", "unknown"):
        return attempt_state
    if draft is None:
        return "not_drafted"
    return "draft_edited" if _text(draft.get("note")) else "draft_ready"


def _totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum the work list into the page header's counters.

    Args:
        rows: The work list rows.

    Returns:
        The totals.
    """
    return {
        "sponsors": len(rows),
        "assigned_children": sum(row["assigned_count"] for row in rows),
        "sheets_ready": sum(row["sheets_ready_count"] for row in rows),
        "sheets_missing": sum(
            row["sheets_total"] - row["sheets_ready_count"] for row in rows
        ),
        "packets_ready": sum(
            1 for row in rows if row["packet_status"] == "ready"
        ),
        "packets_missing": sum(
            1 for row in rows if row["packet_status"] != "ready"
        ),
        "dispatched": sum(1 for row in rows if row["packets_dispatched"]),
    }


async def list_rows(repo: SeasonStoreRepository, cfg: Config) -> dict[str, Any]:
    """Build the sponsor work list.

    Args:
        repo: The season-bound repository.
        cfg: Pipeline configuration.

    Returns:
        The work list, with one row per sponsor that has children.
    """
    child_rows, _families, sponsor_rows = await _stores(repo)
    grouped = _assigned_children(child_rows)
    artifacts = await repo.artifact_rows()
    packets = _packet_artifacts(artifacts)
    mail_configured = bool(cfg.dispatch.enabled and cfg.dispatch.sender_email)

    recipients: dict[str, list[int]] = {}
    for row in sponsor_rows:
        address = (_text(row.get("email")) or "").lower()
        if address:
            recipients.setdefault(address, []).append(
                int(row.get("sponsor_id") or 0)
            )

    rows: list[dict[str, Any]] = []
    for row in sponsor_rows:
        sponsor_id = int(row.get("sponsor_id") or 0)
        children = grouped.get(sponsor_id, [])
        if not children:
            continue
        sheet_children = _current_children(children)
        sheets_ready = sum(1 for child in sheet_children if child["generated"])
        manifest = read_manifest(cfg, sponsor_id)
        draft = await repo.sponsor_draft(sponsor_id)
        attempts = await repo.sponsor_send_attempts(sponsor_id, limit=1)
        attempt_state = attempts[0]["state"] if attempts else None
        roles = packets.get(sponsor_id, {})
        pdf_row = roles.get("packet_pdf") or {}
        instructions_row = roles.get("instructions_pdf") or {}
        manifest_ok = manifest is not None and _manifest_matches(
            manifest, sheet_children
        )
        blockers = _blockers(
            email=_text(row.get("email")),
            assigned_count=len(children),
            num_children=_int(row.get("num_children")),
            sheets_ready=sheets_ready,
            manifest=manifest if manifest_ok else None,
            draft=draft,
            attempt_state=attempt_state,
            mail_configured=mail_configured,
        )
        if manifest is not None and not manifest_ok:
            blockers.append("packet_changed")
        address = (_text(row.get("email")) or "").lower()
        rows.append(
            {
                "sponsor_id": sponsor_id,
                "display_name": _sponsor_name(row),
                "first_name": _text(row.get("first_name")),
                "last_name": _text(row.get("last_name")),
                "organization_name": _text(row.get("organization_name")),
                "sponsor_type": _text(row.get("sponsor_type")),
                "email": _text(row.get("email")),
                "physical_packet": as_bool(row.get("physical_packet")),
                "num_children": _int(row.get("num_children")),
                "assigned_count": len(children),
                "assigned_angel_ids": [
                    str(child.get("angel_id")) for child in sheet_children
                ],
                "sheets_ready_count": sheets_ready,
                "sheets_total": len(children),
                "missing_sheet_angel_ids": [
                    str(child.get("angel_id"))
                    for child in sheet_children
                    if not child["generated"]
                ],
                "packet_status": "ready" if manifest_ok else "missing",
                "packet_pdf_artifact_id": _int(pdf_row.get("id")),
                "packet_created_at": _text(pdf_row.get("created_at")),
                "instruction_pdf_artifact_id": _int(instructions_row.get("id")),
                "attachments": (
                    _manifest_attachments(manifest, roles)
                    if manifest_ok
                    else []
                ),
                "email_status": _email_status(draft, attempt_state),
                "email_sent_at": (
                    attempts[0].get("finished_at")
                    if attempt_state == "sent"
                    else None
                ),
                "email_error": attempts[0].get("error") if attempts else None,
                "draft_version": _int(draft.get("version")) if draft else None,
                "draft_subject": _text(draft.get("subject")) if draft else None,
                "packets_dispatched": bool(
                    _text(row.get("packets_dispatched_at"))
                ),
                "packets_dispatched_at": _text(
                    row.get("packets_dispatched_at")
                ),
                "sponsor_row_version": _int(row.get("_row_version")) or 0,
                "duplicate_recipient_sponsor_ids": [
                    other
                    for other in recipients.get(address, [])
                    if other != sponsor_id
                ],
                "blockers": blockers,
            }
        )
    rows.sort(key=lambda item: item["sponsor_id"])
    return {
        "season": cfg.season,
        "mail_configured": mail_configured,
        "can_send": False,
        "totals": _totals(rows),
        "rows": rows,
    }


async def list_children(
    repo: SeasonStoreRepository, cfg: Config
) -> dict[str, Any]:
    """List the assigned children and their sheet state.

    Args:
        repo: The season-bound repository.
        cfg: Pipeline configuration.

    Returns:
        The child rows.
    """
    child_rows, _families, _sponsors = await _stores(repo)
    grouped = _assigned_children(child_rows)
    artifacts = await repo.artifact_rows()
    sheets_by_angel: dict[str, int] = {}
    for row in artifacts:
        if _text(row.get("kind")) != "child_sheet":
            continue
        meta = row.get("meta")
        meta = meta if isinstance(meta, dict) else {}
        angel_id = _text(meta.get("angel_id"))
        if angel_id:
            sheets_by_angel[angel_id] = _int(row.get("id")) or 0
    rows: list[dict[str, Any]] = []
    for sponsor_id, children in sorted(grouped.items()):
        for child in sorted(
            children, key=lambda item: str(item.get("angel_id"))
        ):
            angel_id = _text(child.get("angel_id"))
            rows.append(
                {
                    "zite_record_id": str(child.get("zite_record_id") or ""),
                    "angel_id": angel_id,
                    "family_id": _int(child.get("family_id")),
                    "sponsor_id": sponsor_id,
                    "generated_info_sheet": as_bool(
                        child.get("generated_info_sheet")
                    ),
                    "sheet_artifact_id": sheets_by_angel.get(angel_id or ""),
                }
            )
    return {"rows": rows}


async def build_sheets_zip(
    repo: SeasonStoreRepository, cfg: Config, sponsor_id: int | None
) -> tuple[bytes, str]:
    """Build a ZIP containing the selected child information sheets.

    Args:
        repo: The season-bound repository.
        cfg: Pipeline configuration.
        sponsor_id: Optional sponsor to scope the sheets to.

    Returns:
        The ZIP bytes and download filename.

    Raises:
        DispatchError: When no matching sheet artifacts exist.
    """
    root = artifact_root(cfg).resolve()
    sheets = []
    for row in await repo.artifact_rows():
        if row.get("kind") != "child_sheet":
            continue
        meta = row.get("meta")
        meta = meta if isinstance(meta, dict) else {}
        if sponsor_id is not None and meta.get("sponsor_id") != sponsor_id:
            continue
        sheets.append(row)

    if not sheets:
        raise DispatchError(NOT_FOUND, "no child information sheets found")

    members: list[tuple[str, bytes]] = []
    used_names: set[str] = set()
    for row in sorted(
        sheets,
        key=lambda item: (
            str(item.get("filename") or ""),
            str((item.get("meta") or {}).get("sponsor_id") or ""),
            str(item.get("path") or ""),
            int(item.get("id") or 0),
        ),
    ):
        target = (root / str(row.get("path") or "")).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            logger.warning(
                "skipping unavailable child sheet artifact %s", row.get("id")
            )
            continue
        filename = str(row.get("filename") or target.name)
        member_name = filename
        if member_name in used_names:
            meta = row.get("meta")
            meta = meta if isinstance(meta, dict) else {}
            member_name = f"{meta.get('sponsor_id')}_{filename}"
        used_names.add(member_name)
        members.append((member_name, target.read_bytes()))

    if not members:
        raise DispatchError(NOT_FOUND, "no child information sheets found")

    output = io.BytesIO()
    with zipfile.ZipFile(
        output, mode="w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        for name, payload in sorted(members, key=lambda item: item[0]):
            archive.writestr(name, payload)

    filename = f"child_info_sheets_{cfg.season}"
    if sponsor_id is not None:
        filename += f"_sponsor{sponsor_id}"
    return output.getvalue(), f"{filename}.zip"


async def draft_sponsors(
    repo: SeasonStoreRepository,
    cfg: Config,
    actor: str,
    sponsor_ids: list[int] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Render and store the email draft of one or more sponsors.

    Drafting never sends and never stamps a sponsor.

    Args:
        repo: The season-bound repository.
        cfg: Pipeline configuration.
        actor: Attribution identity.
        sponsor_ids: The sponsors to draft; every ready sponsor when
            None.
        overwrite: Re-render a draft that already exists.

    Returns:
        The refreshed work list.
    """
    view = await list_rows(repo, cfg)
    wanted = set(sponsor_ids) if sponsor_ids else None
    for row in view["rows"]:
        if wanted is not None and row["sponsor_id"] not in wanted:
            continue
        if row["packet_status"] != "ready":
            continue
        if row["email_status"] in ("sent", "sending"):
            continue
        manifest = read_manifest(cfg, row["sponsor_id"])
        if manifest is None:
            continue
        if not overwrite and row["draft_version"] is not None:
            continue
        await repo.upsert_sponsor_draft(
            row["sponsor_id"],
            subject=str(manifest.get("subject") or ""),
            body_html=_render_body(cfg, manifest, None),
            note=None,
            to_email=row["email"] or "",
            bcc_emails=list(cfg.sponsor_packets.bcc_emails),
            attachments=[dict(item) for item in row["attachments"]],
            build_id=str(manifest.get("build_id") or ""),
            actor=actor,
        )
    return await list_rows(repo, cfg)


async def get_draft(
    repo: SeasonStoreRepository, cfg: Config, sponsor_id: int
) -> dict[str, Any]:
    """Read one sponsor's draft.

    Args:
        repo: The season-bound repository.
        cfg: Pipeline configuration.
        sponsor_id: The sponsor's key.

    Returns:
        The draft.

    Raises:
        DispatchError: 404 when the sponsor has no draft.
    """
    draft = await repo.sponsor_draft(sponsor_id)
    if draft is None:
        raise DispatchError(NOT_FOUND, f"sponsor {sponsor_id} has no draft yet")
    manifest = read_manifest(cfg, sponsor_id)
    attempts = await repo.sponsor_send_attempts(sponsor_id, limit=1)
    attempt_state = attempts[0]["state"] if attempts else None
    blockers = _blockers(
        email=draft.get("to_email"),
        assigned_count=0,
        num_children=None,
        sheets_ready=0,
        manifest=manifest,
        draft=draft,
        attempt_state=attempt_state,
        mail_configured=bool(
            cfg.dispatch.enabled and cfg.dispatch.sender_email
        ),
    )
    return {
        "sponsor_id": sponsor_id,
        "version": int(draft.get("version") or 0),
        "subject": str(draft.get("subject") or ""),
        "body_html": str(draft.get("body_html") or ""),
        "note": str(draft.get("note") or ""),
        "to_email": str(draft.get("to_email") or ""),
        "from_email": cfg.dispatch.sender_email,
        "bcc_emails": list(draft.get("bcc_emails") or []),
        "attachments": [
            dict(item) for item in (draft.get("attachments") or [])
        ],
        "build_id": _text(draft.get("build_id")),
        "email_status": _email_status(draft, attempt_state),
        "email_error": attempts[0].get("error") if attempts else None,
        "blockers": blockers,
    }


async def update_draft(
    repo: SeasonStoreRepository,
    cfg: Config,
    actor: str,
    sponsor_id: int,
    subject: str | None,
    note: str | None,
    expected_version: int,
) -> dict[str, Any]:
    """Change one sponsor's draft subject or note.

    Args:
        repo: The season-bound repository.
        cfg: Pipeline configuration.
        actor: Attribution identity.
        sponsor_id: The sponsor's key.
        subject: The new subject, when it changed.
        note: The new note, when it changed.
        expected_version: The draft version the caller read.

    Returns:
        The updated draft.

    Raises:
        DispatchError: 404 when the sponsor has no draft; 412 when the
            draft moved on.
    """
    draft = await repo.sponsor_draft(sponsor_id)
    if draft is None:
        raise DispatchError(NOT_FOUND, f"sponsor {sponsor_id} has no draft yet")
    manifest = read_manifest(cfg, sponsor_id) or {}
    final_note = note if note is not None else draft.get("note")
    body = _render_body(cfg, manifest, final_note)
    await repo.upsert_sponsor_draft(
        sponsor_id,
        subject=(
            subject if subject is not None else str(draft.get("subject") or "")
        ),
        body_html=body,
        note=final_note,
        to_email=str(draft.get("to_email") or ""),
        bcc_emails=list(draft.get("bcc_emails") or []),
        attachments=[dict(item) for item in (draft.get("attachments") or [])],
        build_id=str(draft.get("build_id") or ""),
        actor=actor,
        expected_version=expected_version,
    )
    return await get_draft(repo, cfg, sponsor_id)


def _audit_event(
    actor: str,
    user_id: int | None,
    season: str,
    sponsor_id: int,
    message_id: str,
    subject: str,
    to_email: str,
    bcc_emails: list[str],
    attachments: list[dict[str, Any]],
    state: str,
) -> dict[str, Any]:
    """Build the audit row for one send attempt.

    Args:
        actor: The acting identity.
        user_id: The acting user's id.
        season: The season name.
        sponsor_id: The sponsor's key.
        message_id: The RFC 5322 Message-ID.
        subject: The subject sent.
        to_email: The recipient.
        bcc_emails: The archive addresses.
        attachments: The files attached, with their hashes.
        state: The attempt's outcome.

    Returns:
        The insert kwargs.
    """
    return {
        "ts": datetime.now(UTC),
        "user_id": user_id,
        "action": "sponsor_packet_send",
        "object_type": "sponsor",
        "object_id": str(sponsor_id),
        "season": season,
        "details": {
            "actor": actor,
            "state": state,
            "message_id": message_id,
            "subject": subject,
            "to": to_email,
            "bcc": list(bcc_emails),
            "attachments": [
                {
                    "role": item.get("role"),
                    "filename": item.get("filename"),
                    "sha256": item.get("sha256"),
                }
                for item in attachments
            ],
        },
    }


async def _read_attachments(
    cfg: Config, manifest: dict[str, Any]
) -> list[tuple[str, Attachment]]:
    """Read the packet files the manifest names.

    The bytes are read once and hashed; the send transmits exactly these
    bytes, so a file replaced in between cannot slip into the message.

    Args:
        cfg: Pipeline configuration.
        manifest: The packet manifest.

    Returns:
        One (role, attachment) pair per file.

    Raises:
        DispatchError: 409 when a file is missing or its bytes changed.
    """
    packet_dir = _packet_dir(cfg, int(manifest.get("sponsor_id") or 0))
    attachments: list[tuple[str, Attachment]] = []
    for item in manifest.get("items", []):
        role = str(item.get("role") or "")
        if role == "email_html":
            continue
        path = packet_dir / str(item.get("filename") or "")
        if not path.is_file():
            raise DispatchError(
                CONFLICT,
                f"the packet file {path.name} is missing; build the packet"
                " again before sending",
                blocker="packet_changed",
            )
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if digest != str(item.get("sha256") or ""):
            raise DispatchError(
                CONFLICT,
                f"the packet file {path.name} changed since it was built;"
                " build the packet again before sending",
                blocker="packet_changed",
            )
        attachments.append(
            (
                role,
                Attachment(
                    filename=path.name,
                    content_type=(
                        "text/html"
                        if role == "email_html"
                        else "application/pdf"
                    ),
                    payload=payload,
                    sha256=digest,
                ),
            )
        )
    return attachments


async def send_draft(
    repo: SeasonStoreRepository,
    cfg: Config,
    actor: str,
    user_id: int | None,
    sponsor_id: int,
    subject: str | None,
    note: str | None,
    expected_version: int,
    transport: MailTransport,
) -> dict[str, Any]:
    """Send one sponsor's packet email.

    The send is one attempt: the draft is claimed and the attempt is
    opened in one transaction, the provider is called outside it, and the
    outcome is written back with the sponsor's dispatch stamp.

    Args:
        repo: The season-bound repository.
        cfg: Pipeline configuration.
        actor: Attribution identity.
        user_id: The acting user's id.
        sponsor_id: The sponsor's key.
        subject: The subject being sent, when the coordinator typed one.
        note: The note being sent, when the coordinator typed one.
        expected_version: The draft version the popup was opened with.
        transport: The mail transport.

    Returns:
        The send outcome.

    Raises:
        DispatchError: 404 without a draft; 409 when the sponsor is not
            ready or the packet no longer matches; 412 on a stale draft;
            422 when the mail account is not configured.
    """
    if not cfg.dispatch.enabled or not cfg.dispatch.sender_email:
        raise DispatchError(
            MAIL_NOT_CONFIGURED,
            "the mail account is not configured; set dispatch.enabled and"
            " dispatch.sender_email, then run `ccat mail-authorize`",
        )
    draft = await repo.sponsor_draft(sponsor_id)
    if draft is None:
        raise DispatchError(NOT_FOUND, f"sponsor {sponsor_id} has no draft yet")
    to_email = str(draft.get("to_email") or "").strip()
    if not to_email:
        raise DispatchError(
            CONFLICT,
            f"sponsor {sponsor_id} has no email address on file",
            blocker="no_email",
        )
    manifest = read_manifest(cfg, sponsor_id)
    if manifest is None:
        raise DispatchError(
            CONFLICT,
            f"sponsor {sponsor_id} has no packet manifest; build the packet"
            " again before sending",
            blocker="manifest_missing",
        )
    child_rows, _families, sponsor_rows = await _stores(repo)
    children = _assigned_children(child_rows).get(sponsor_id, [])
    if not _manifest_matches(manifest, _current_children(children)):
        raise DispatchError(
            CONFLICT,
            f"sponsor {sponsor_id}'s assignment changed since the packet was"
            " built; rebuild the packet and draft it again",
            blocker="packet_changed",
        )
    active_jobs = await repo.active_pipeline_jobs()
    if active_jobs:
        raise DispatchError(
            CONFLICT,
            "a pipeline job is running; wait for it to finish before sending",
            blocker="pipeline_job_running",
            job_id=active_jobs[0]["job_id"],
            job_type=active_jobs[0]["job_type"],
        )
    attachments = await _read_attachments(cfg, manifest)
    total = sum(len(attachment.payload) for _role, attachment in attachments)
    if total > cfg.dispatch.max_attachment_bytes:
        raise DispatchError(
            INVALID_INPUT,
            f"the packet is {total} bytes, over the configured limit of"
            f" {cfg.dispatch.max_attachment_bytes}",
            blocker="attachments_too_large",
        )
    final_note = note if note is not None else draft.get("note")
    body = _render_body(cfg, manifest, final_note)
    final_subject = (
        subject
        if subject is not None
        else str(draft.get("subject") or manifest.get("subject") or "")
    )
    if not final_subject.strip():
        raise DispatchError(
            INVALID_INPUT, "the email needs a subject", blocker="no_subject"
        )
    message_id = f"<{uuid.uuid4().hex}@ccat.local>"
    attempt_id = uuid.uuid4().hex
    snapshot = [
        {
            "role": role,
            "filename": attachment.filename,
            "content_type": attachment.content_type,
            "size_bytes": len(attachment.payload),
            "sha256": attachment.sha256,
        }
        for role, attachment in attachments
    ]
    try:
        attempt = await repo.commit_dispatch_attempt(
            sponsor_id,
            subject=final_subject,
            body_html=body,
            note=final_note,
            expected_version=expected_version,
            attempt_id=attempt_id,
            message_id=message_id,
            to_email=str(draft.get("to_email") or ""),
            bcc_emails=list(draft.get("bcc_emails") or []),
            attachments=snapshot,
            actor=actor,
            user_id=user_id,
        )
    except StaleRowVersionError as error:
        raise DispatchError(
            STALE_VERSION,
            "the draft changed in another window; reload it and try again",
            current_version=error.current_version,
        ) from error
    except IntegrityError as error:
        raise DispatchError(
            CONFLICT,
            "a send for this sponsor is already in flight",
            blocker="send_in_flight",
        ) from error

    request = SendRequest(
        sender=cfg.dispatch.sender_email,
        sender_name=cfg.dispatch.sender_name,
        reply_to=cfg.dispatch.reply_to,
        to=str(draft.get("to_email") or ""),
        bcc=tuple(str(item) for item in (draft.get("bcc_emails") or [])),
        subject=final_subject,
        html_body=body,
        message_id=message_id,
        attachments=tuple(attachment for _role, attachment in attachments),
    )
    try:
        outcome = await asyncio.to_thread(transport.send, request)
    except MailError as error:
        # The account could not even attempt the delivery: nothing was
        # sent, so the outcome is a definite refusal, not an unknown one.
        outcome = SendOutcome("rejected", "", str(error))
    except Exception as error:
        # A transport that raises did not say whether it delivered.
        logger.exception("the mail transport failed for sponsor %s", sponsor_id)
        outcome = SendOutcome(
            "ambiguous",
            "",
            f"the mail transport failed ({type(error).__name__})",
        )
    state = _STATE_BY_OUTCOME.get(outcome.state, "unknown")
    sponsor_store: list[dict[str, Any]] | None = None
    if state == "sent":
        sponsor_store = []
        for row in sponsor_rows:
            copy = dict(row)
            if int(copy.get("sponsor_id") or 0) == sponsor_id:
                copy["packets_dispatched_at"] = datetime.now(UTC).isoformat()
            sponsor_store.append(copy)
    await repo.commit_dispatch_result(
        attempt_id,
        state=state,
        provider_message_id=outcome.provider_message_id or None,
        error=outcome.error or None,
        sponsor_rows=sponsor_store,
        actor=actor,
        audit_events=[
            _audit_event(
                actor,
                user_id,
                cfg.season,
                sponsor_id,
                message_id,
                final_subject,
                str(draft.get("to_email") or ""),
                list(draft.get("bcc_emails") or []),
                snapshot,
                state,
            )
        ],
    )
    return {
        "sponsor_id": sponsor_id,
        "state": state,
        "attempt_id": attempt_id,
        "message_id": message_id,
        "draft_version": int(attempt.get("draft_version") or 0),
        "sent_at": datetime.now(UTC).isoformat() if state == "sent" else None,
        "error": outcome.error or None,
    }


async def recover_stale_attempts(
    repo: SeasonStoreRepository, actor: str = "system"
) -> list[str]:
    """Close attempts that never reported an outcome.

    Args:
        repo: The season-bound repository.
        actor: Attribution identity.

    Returns:
        The ids of the attempts that were closed.
    """
    cutoff = datetime.now(UTC).timestamp() - STALE_ATTEMPT_SECONDS
    closed = await repo.fail_stale_attempts(
        datetime.fromtimestamp(cutoff, UTC), actor
    )
    for attempt_id in closed:
        await repo.commit_dispatch_result(
            attempt_id,
            state="unknown",
            error="the send did not report an outcome",
            actor=actor,
        )
    return closed


__all__ = [
    "CONFLICT",
    "INVALID_INPUT",
    "MAIL_FAILED",
    "MAIL_NOT_CONFIGURED",
    "NOT_FOUND",
    "STALE_VERSION",
    "DispatchError",
    "build_sheets_zip",
    "draft_sponsors",
    "get_draft",
    "list_children",
    "list_rows",
    "read_manifest",
    "recover_stale_attempts",
    "send_draft",
    "update_draft",
]
