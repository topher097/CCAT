"""Module 7 — clarification email generation (hybrid AI + template).

For each family with flagged Angels, an LLM (via the opencode SDK,
same connection pattern as module 2's screening) writes the email
prose as plain-text JSON fragments; a Jinja2 HTML template then wraps
those fragments in paste-safe inline-CSS markup so the operator can
copy the result straight into an email client.  Family names and
emails are intentionally part of the prompt here (unlike screening,
where names never reach the model) -- addressing the family is the
whole point of the email.

The model must return a JSON object with exactly the keys
``greeting``, ``intro``, ``angel_paragraphs`` (one per Angel, same
order), and ``closing``; malformed or inconsistent responses are
retried once, then the family's generation fails with a
:class:`ClarifyError` (mirroring module 2's ``screen_child``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, TYPE_CHECKING

from jinja2 import Environment
from pydantic import BaseModel, ConfigDict

from ccat.clarify.extract import ClarifyError

if TYPE_CHECKING:
    import polars as pl
    from ccat.config import Config
    from collections.abc import Callable

logger = logging.getLogger(__name__)

#: Columns consumed per flagged Angel row of the clarification table.
ANGEL_COLUMNS = (
    "angel_id",
    "first_name",
    "raw_dob",
    "require_clarification",
    "need_1",
    "need_2",
    "desire_1",
    "desire_2",
    "zite_record_id",
)

#: Columns consumed per family of the clarification table.
FAMILY_COLUMNS = (
    "family_id",
    "family_first_name",
    "family_last_name",
    "family_phone",
    "family_email",
)

#: Columns sent to the model per Angel (contact details stay out of
#: the per-Angel payload; the family-level fields carry them).
PROMPT_ANGEL_FIELDS = (
    "angel_id",
    "first_name",
    "raw_dob",
    "require_clarification",
    "need_1",
    "need_2",
    "desire_1",
    "desire_2",
)

#: Jinja2 subject template for the clarification email.
SUBJECT_TEMPLATE = (
    "Copper Country Angel Tree {{ season }} — a quick question about"
    " your Angel registration"
)


class AngelEntry(BaseModel):
    """One flagged Angel of one family.

    Attributes:
        angel_id: The Angel's id code (e.g. ``5A``).
        first_name: The Angel's first name.
        raw_dob: Date of birth as typed in the ``raw_child`` form.
        require_clarification: The audit note driving the email.
        need_1: The Angel's first need.
        need_2: The Angel's second need.
        desire_1: The Angel's first wish.
        desire_2: The Angel's second wish.
        zite_record_id: The pseudonymous source-record id.
    """

    model_config = ConfigDict(extra="forbid")

    angel_id: str
    first_name: str
    raw_dob: str | None = None
    require_clarification: str
    need_1: str | None = None
    need_2: str | None = None
    desire_1: str | None = None
    desire_2: str | None = None
    zite_record_id: str | None = None


class FamilyEntry(BaseModel):
    """One flagged family and all of its Angels needing clarification.

    Attributes:
        family_id: The family's id.
        family_first_name: Primary contact's first name.
        family_last_name: Primary contact's last name.
        family_phone: Primary contact's phone number.
        family_email: Primary contact's email address; the email's
            intended recipient.
        angels: Every flagged Angel of the family, ordered by
            ``angel_id``.
    """

    model_config = ConfigDict(extra="forbid")

    family_id: int
    family_first_name: str | None = None
    family_last_name: str | None = None
    family_phone: str | None = None
    family_email: str | None = None
    angels: list[AngelEntry]


class ClarifyEmail(BaseModel):
    """The model's validated prose fragments for one family's email.

    Attributes:
        greeting: Salutation line (e.g. ``Dear Caroline,``).
        intro: One or two opening sentences.
        angel_paragraphs: One short paragraph per Angel, in the same
            order as the family's Angels.
        closing: Wrap-up sentences asking for a reply.
    """

    model_config = ConfigDict(extra="forbid")

    greeting: str
    intro: str
    angel_paragraphs: list[str]
    closing: str

    @classmethod
    def for_family(
        cls, parsed: dict[str, Any], family: FamilyEntry
    ) -> ClarifyEmail:
        """Validate a parsed response against one family.

        Args:
            parsed: The parsed JSON object.
            family: The family the response must match.

        Returns:
            The validated :class:`ClarifyEmail`.

        Raises:
            ValueError: On wrong keys, empty fragments, or a
                paragraph count that does not match the family.
        """
        email = cls(**parsed)
        if not email.greeting.strip() or not email.intro.strip():
            raise ValueError("greeting and intro must be non-empty")
        if len(email.angel_paragraphs) != len(family.angels):
            raise ValueError(
                f"expected {len(family.angels)} angel paragraph(s),"
                f" got {len(email.angel_paragraphs)}"
            )
        if not all(paragraph.strip() for paragraph in email.angel_paragraphs):
            raise ValueError("angel_paragraphs entries must be non-empty")
        if not email.closing.strip():
            raise ValueError("closing must be non-empty")
        return email


def group_families(frame: pl.DataFrame) -> list[FamilyEntry]:
    """Group the clarification table by family.

    Rows whose ``family_id`` is null are skipped with a warning; a
    flagged child without a family row may also have null family
    contact fields, which is carried through (the UI makes the
    missing email explicit).

    Args:
        frame: The clarification table (:func:`ccat.clarify.extract.build_table`
            output).

    Returns:
        One :class:`FamilyEntry` per unique family, ordered by
        ``family_id``, Angels ordered by ``angel_id``.
    """
    families: list[FamilyEntry] = []
    by_id: dict[int, FamilyEntry] = {}
    for row in frame.sort("family_id", "angel_id").to_dicts():
        family_id = row.get("family_id")
        if family_id is None:
            logger.warning(
                "angel %s has no family_id; skipping it",
                row.get("angel_id"),
            )
            continue
        entry = by_id.get(int(family_id))
        if entry is None:
            entry = FamilyEntry(
                family_id=int(family_id),
                family_first_name=_text(row.get("family_first_name")),
                family_last_name=_text(row.get("family_last_name")),
                family_phone=_text(row.get("family_phone")),
                family_email=_text(row.get("family_email")),
                angels=[],
            )
            by_id[entry.family_id] = entry
            families.append(entry)
        entry.angels.append(
            AngelEntry(
                angel_id=str(row.get("angel_id") or ""),
                first_name=str(row.get("first_name") or ""),
                raw_dob=_text(row.get("raw_dob")),
                require_clarification=str(
                    row.get("require_clarification") or ""
                ),
                need_1=_text(row.get("need_1")),
                need_2=_text(row.get("need_2")),
                desire_1=_text(row.get("desire_1")),
                desire_2=_text(row.get("desire_2")),
                zite_record_id=_text(row.get("zite_record_id")),
            )
        )
    return families


def _text(value: Any) -> str | None:
    """Normalize a cell to stripped text, or None when empty/null."""
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def build_prompt(family: FamilyEntry, cfg: Config) -> str:
    """Build the email-writing prompt for one family.

    The family's contact details are intentionally included: the whole
    point of this email is to address the family by name.  The
    ``{{season}}`` placeholder in the configured system prompt is
    replaced with the configured season.

    Args:
        family: The family to write about.
        cfg: Pipeline configuration.

    Returns:
        The full prompt text.
    """
    system_prompt = cfg.clarify.system_prompt.replace("{{season}}", cfg.season)
    record = {
        "family": {field: getattr(family, field) for field in FAMILY_COLUMNS},
        "angels": [
            {field: getattr(angel, field) for field in PROMPT_ANGEL_FIELDS}
            for angel in family.angels
        ],
    }
    return system_prompt + "\n\n" + json.dumps(record, ensure_ascii=False)


def parse_email_response(text: str, family: FamilyEntry) -> ClarifyEmail:
    """Parse and validate the model's response for one family.

    Args:
        text: Raw response text.
        family: The family the response must match.

    Returns:
        The validated :class:`ClarifyEmail`.

    Raises:
        ValueError: If the response is not a JSON object or violates
            the output contract.
    """
    stripped = text.strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(
                f"response is not a JSON object: {stripped[:100]!r}"
            ) from None
        parsed = json.loads(stripped[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError(f"response is not a JSON object: {stripped[:100]!r}")
    return ClarifyEmail.for_family(parsed, family)


def generate_email(
    family: FamilyEntry,
    cfg: Config,
    send: Callable[[str], str],
) -> ClarifyEmail:
    """Generate one family's email prose with one retry, then give up.

    Args:
        family: The family to write about.
        cfg: Pipeline configuration.
        send: Callable sending one prompt to the model session and
            returning its raw text response.

    Returns:
        The validated :class:`ClarifyEmail`.

    Raises:
        ClarifyError: When the model fails twice to produce a valid
            response for this family.
    """
    prompt = build_prompt(family, cfg)
    last_error: Exception | None = None
    for _attempt in range(2):
        response = send(prompt)
        try:
            return parse_email_response(response, family)
        except (ValueError, json.JSONDecodeError) as error:
            last_error = error
            logger.warning(
                "malformed email response for family %s: %s; retrying",
                family.family_id,
                error,
            )
    raise ClarifyError(
        f"could not generate a valid email for family"
        f" {family.family_id}: {last_error}"
    )


def render_email(
    family: FamilyEntry,
    email: ClarifyEmail,
    cfg: Config,
    subject: str | None = None,
) -> tuple[str, str]:
    """Render one family's complete email draft as HTML.

    Args:
        family: The family being addressed.
        email: The validated prose fragments.
        cfg: Pipeline configuration.
        subject: Optional edited subject line; defaults to the
            rendered :data:`SUBJECT_TEMPLATE`.

    Returns:
        The (subject, html) pair; the HTML is paste-safe (all inline
        CSS), ready to copy into an email client.

    Raises:
        ClarifyError: If the configured template file is missing.
    """
    template_file = Path(cfg.clarify.email_template_html_file)
    if not template_file.is_file():
        raise ClarifyError(f"email template file not found: {template_file}")
    environment = Environment(autoescape=True)
    rendered_subject = subject if subject is not None else default_subject(cfg)
    html = environment.from_string(
        template_file.read_text(encoding="utf-8")
    ).render(
        season=cfg.season,
        subject=rendered_subject,
        family=family,
        email=email,
    )
    return rendered_subject, html


#: Wrapper style for the embeddable preview fragment: solid white
#: background (no transparency), the template's serif face, and a
#: subtle border, mirroring the standalone template's ``<body>``
#: styling that does not survive being spliced into a page.
PREVIEW_WRAPPER_STYLE = (
    "background: #ffffff; color: #1a1a1a; padding: 1rem;"
    " border: 1px solid #cbd5e1; border-radius: 6px;"
    " font-family: Georgia, 'Times New Roman', serif; max-width: 40rem;"
)

_BODY_RE = re.compile(r"<body[^>]*>(.*)</body>", re.DOTALL)


def render_email_fragment(
    family: FamilyEntry,
    email: ClarifyEmail,
    cfg: Config,
    subject: str | None = None,
) -> str:
    """Render the email as a solid-background embeddable fragment.

    :func:`render_email` produces a full HTML document; splicing a
    full document into the web page drops the ``<body>`` styling
    (fonts, ink color) and leaves the content on the page's own
    background.  This extracts the body content and wraps it in a
    solid white, bordered div with the template's typography, keyed
    by ``id="email-body-<family_id>"`` for rich-text copy selection.

    Args:
        family: The family being addressed.
        email: The validated prose fragments.
        cfg: Pipeline configuration.
        subject: Optional edited subject line; defaults to the
            rendered :data:`SUBJECT_TEMPLATE`.

    Returns:
        The preview fragment HTML.

    Raises:
        ClarifyError: If the configured template file is missing.
    """
    _subject, html = render_email(family, email, cfg, subject)
    match = _BODY_RE.search(html)
    body = match.group(1) if match is not None else html
    return (
        f'<div id="email-body-{family.family_id}"'
        f' style="{PREVIEW_WRAPPER_STYLE}">{body}</div>'
    )


def default_subject(cfg: Config) -> str:
    """Build the default subject line from the configured season.

    Args:
        cfg: Pipeline configuration.

    Returns:
        The rendered :data:`SUBJECT_TEMPLATE` subject.
    """
    return (
        Environment(autoescape=True)
        .from_string(SUBJECT_TEMPLATE)
        .render(season=cfg.season)
    )


class EmailDraft(BaseModel):
    """One family's complete, editable email draft.

    The JSON companion (``family_<id>.json``) written beside every
    saved draft HTML stores this model, making saved drafts round-trip
    through the web app's editor: load, modify, save.

    Attributes:
        subject: The email subject line.
        greeting: Salutation line (e.g. ``Dear Caroline,``).
        intro: One or two opening sentences.
        angel_paragraphs: One short paragraph per Angel, in the same
            order as the family's Angels.
        closing: Wrap-up sentences asking for a reply.
    """

    model_config = ConfigDict(extra="forbid")

    subject: str
    greeting: str
    intro: str
    angel_paragraphs: list[str]
    closing: str

    def as_email(self) -> ClarifyEmail:
        """Return the prose fragments as a :class:`ClarifyEmail`."""
        return ClarifyEmail(
            greeting=self.greeting,
            intro=self.intro,
            angel_paragraphs=self.angel_paragraphs,
            closing=self.closing,
        )

    @classmethod
    def from_email(cls, email: ClarifyEmail, subject: str) -> EmailDraft:
        """Bundle prose fragments with their subject line.

        Args:
            email: The model's validated fragments.
            subject: The draft's subject line.

        Returns:
            The complete draft.
        """
        return cls(subject=subject, **email.model_dump())


def html_path(cfg: Config, family: FamilyEntry) -> Path:
    """Return the saved-draft HTML path for one family.

    Args:
        cfg: Pipeline configuration.
        family: The family in question.

    Returns:
        ``clarify.output_dir/family_<family_id>.html``.
    """
    return Path(cfg.clarify.output_dir) / f"family_{family.family_id}.html"


def draft_path(cfg: Config, family: FamilyEntry) -> Path:
    """Return the draft-data JSON path for one family.

    Args:
        cfg: Pipeline configuration.
        family: The family in question.

    Returns:
        ``clarify.output_dir/family_<family_id>.json``.
    """
    return Path(cfg.clarify.output_dir) / f"family_{family.family_id}.json"


def write_draft(cfg: Config, family: FamilyEntry, draft: EmailDraft) -> Path:
    """Persist one draft: rendered HTML plus its JSON companion.

    Any previous draft for the family is overwritten (both files), so
    edits and regenerations always propagate to disk.

    Args:
        cfg: Pipeline configuration.
        family: The family the draft belongs to.
        draft: The complete editable draft.

    Returns:
        The HTML path written (``clarify.output_dir/family_<id>.html``).

    Raises:
        ClarifyError: If the template file is missing.
    """
    _subject, html = render_email(
        family, draft.as_email(), cfg, subject=draft.subject
    )
    out_file = html_path(cfg, family)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(html, encoding="utf-8")
    draft_path(cfg, family).write_text(
        draft.model_dump_json(indent=2), encoding="utf-8"
    )
    logger.info("saved clarification email draft to %s", out_file)
    return out_file


def load_draft(cfg: Config, family: FamilyEntry) -> EmailDraft | None:
    """Load one family's saved, editable draft from disk.

    Legacy drafts saved before the JSON companion existed (HTML only)
    are reported as absent by this function; the caller decides how to
    surface that (regenerating creates the companion).

    Args:
        cfg: Pipeline configuration.
        family: The family whose draft to load.

    Returns:
        The saved draft, or None when no draft has been saved.

    Raises:
        ClarifyError: If the JSON companion exists but is invalid.
    """
    json_path = draft_path(cfg, family)
    if not json_path.is_file():
        return None
    try:
        return EmailDraft.model_validate_json(json_path.read_text())
    except ValueError as error:
        raise ClarifyError(
            f"saved draft data {json_path} is invalid: {error}"
        ) from error


class EmailSender:
    """Prompt sender backed by one long-lived opencode connection.

    Honors ``OPENCODE_BASE_URL`` when set (attaching to a running
    server); otherwise starts an embedded ``opencode serve`` child
    process that lives until :meth:`close` (or process exit).  The
    connection is created lazily on first use and guarded by a lock,
    so NiceGUI background-thread clicks can never race the startup.
    """

    def __init__(self, cfg: Config) -> None:
        """Prepare (but do not start) the connection.

        Args:
            cfg: Pipeline configuration.

        Raises:
            ClarifyError: If the configured model reference is not a
                ``provider/model`` pair, or opencode v2 is missing
                (same checks as module 2 before any prompt is sent).
        """
        from ccat.screening import _require_opencode_v2

        self._cfg = cfg
        self._lock = threading.Lock()
        self._client: Any = None
        self._context: Any = None
        model_ref = cfg.clarify.model
        provider_id, separator, model_id = model_ref.partition("/")
        if not separator:
            raise ClarifyError(
                f"clarify.model {model_ref!r} is not a valid opencode"
                " model reference; use the 'provider/model' form (run"
                " `opencode models` for the list)"
            )
        self._provider_id = provider_id
        self._model_id = model_id
        _require_opencode_v2()

    def _ensure_client(self) -> Any:
        """Create the opencode client once, under the lock."""
        if self._client is not None:
            return self._client
        if os.environ.get("OPENCODE_BASE_URL"):
            from opencode import Opencode

            logger.info("attaching to opencode server via OPENCODE_BASE_URL")
            self._context = Opencode()
            self._context.__enter__()
            self._client = self._context
        else:
            from opencode import OpencodeServer

            logger.info("OPENCODE_BASE_URL not set; starting embedded server")
            self._context = OpencodeServer(cwd=Path.cwd())
            self._context.__enter__()
            self._client = self._context.client()
        return self._client

    def send(self, prompt: str) -> str:
        """Send one prompt and return the raw response text.

        One fresh session per prompt keeps generations independent
        (the sender may be called from any thread).

        Args:
            prompt: The full prompt text.

        Returns:
            The model's raw text response.

        Raises:
            ClarifyError: If the server cannot be started or the
                session cannot be created.
        """
        from opencode.types import ModelRef

        with self._lock:
            try:
                client = self._ensure_client()
                session_id = client.session.create(
                    model=ModelRef(
                        id=self._model_id, provider_id=self._provider_id
                    )
                ).id
                return client.session.generate(session_id, prompt=prompt).text
            except Exception as error:  # noqa: BLE001 - surface as typed error
                raise ClarifyError(
                    f"opencode email generation failed: {error}"
                ) from error

    def close(self) -> None:
        """Stop the embedded server if one was started."""
        with self._lock:
            if self._context is not None:
                try:
                    self._context.__exit__(None, None, None)
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    logger.warning("embedded opencode server did not stop")
                self._context = None
                self._client = None
