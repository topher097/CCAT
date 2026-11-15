"""Module 7 — local clarification email web app (NiceGUI).

Serves a single-user local page listing every family with flagged
Angels.  Each family card shows the Angels and their
``require_clarification`` notes, the family's contact-status
("Mark contacted" stamps the time; a "Reply received" switch records
the answer), and two draft actions:

- **Generate clarification email** — asks the configured model (via
  :class:`EmailSender`) for the polite prose, renders it through the
  HTML template, persists it to ``clarify.output_dir``
  (``family_<id>.html`` plus its editable ``family_<id>.json``
  companion), refreshes the CSV's draft-reference column, and opens
  the editor dialog.
- **Load saved draft** — opens the previously saved draft in the
  same editor dialog (only when a draft exists).

The editor dialog exposes every part of the email (subject,
greeting, intro, one paragraph per Angel, closing), a live rendered
preview, and controls to copy the result into the operator's email
client.  "Save changes" re-renders and overwrites both saved files
and refreshes the CSV; "Regenerate" asks the model again and replaces
the editor contents (then save manually if wanted).

Contact status lives in a JSON sidecar
(:mod:`ccat.clarify.status`) and is merged into the clarifications
CSV's ``contacted``/``contacted_at``/``replied`` columns on every
write, so it survives the CSV being rebuilt from the workbook.

Headless mode: ``ccat clarify --generate`` skips the server entirely
and generates every missing family's draft; adding ``--overwrite``
regenerates existing drafts too.  ``--overwrite`` without
``--generate`` does nothing and the server starts normally.

Copy mechanics: the subject copies as plain text via NiceGUI's
clipboard; the rendered body is selected with plain DOM selection and
copied as rich text (the same effect as Ctrl+A/Ctrl+C on a saved
draft), so formatting survives the paste.  The raw HTML source and an
"open draft in a browser" fallback cover email clients that need it.
"""

import atexit
import json
import logging
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from nicegui import run as background
from nicegui import ui

from ccat.clarify.email_gen import (
    EmailDraft,
    EmailSender,
    FamilyEntry,
    default_subject,
    generate_email,
    group_families,
    html_path,
    load_draft,
    render_email,
    render_email_fragment,
    write_draft,
)
from ccat.clarify.extract import ClarifyError, build_table, write_csv
from ccat.clarify.status import (
    FamilyStatus,
    load_statuses,
    store_statuses,
)
from ccat.config import Config

logger = logging.getLogger(__name__)


class ClarifyState:
    """Everything the page handlers need, shared via one module global.

    Attributes:
        cfg: Pipeline configuration.
        families: The flagged families, ordered by ``family_id``.
        statuses: Contact status per family id (JSON sidecar cache).
        sender: The lazily-connecting prompt sender.
    """

    def __init__(
        self,
        cfg: Config,
        families: list[FamilyEntry],
        statuses: dict[int, FamilyStatus] | None = None,
    ) -> None:
        """Capture the app state.

        Args:
            cfg: Pipeline configuration.
            families: The flagged families.
            statuses: Contact status per family id; an empty store
                when omitted.
        """
        self.cfg = cfg
        self.families = families
        self.statuses = statuses if statuses is not None else {}
        self.sender = EmailSender(cfg)


#: The single app instance's state (set by :func:`run`, read by the page).
_STATE: ClarifyState | None = None


class SenderLike(Protocol):
    """Anything that can send one prompt and return the raw reply.

    :class:`EmailSender` satisfies this structurally; tests pass
    lightweight fakes with the same ``send`` method.
    """

    def send(self, prompt: str) -> str:
        """Send one prompt to the model and return its raw text."""
        ...


def _select_and_copy_body(family_id: int) -> None:
    """Select one rendered email body and copy it as rich text.

    The rendered preview lives in a ``<div id="email-body-<id>">``
    that :func:`_fill_editor` created, so plain DOM selection (no
    clipboard API dependency) copies the formatted content.

    Args:
        family_id: The family whose rendered body to select.
    """
    ui.run_javascript(
        """
        const el = document.getElementById(arguments_id);
        const range = document.createRange();
        range.selectNodeContents(el);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        document.execCommand('copy');
        """.replace("arguments_id", json.dumps(f"email-body-{family_id}"))
    )
    ui.notify("Email body copied — paste it into your email client.")


def _fill_editor(
    state: ClarifyState,
    family: FamilyEntry,
    draft: EmailDraft,
    dialog: Any,
    slot: Any,
) -> None:
    """(Re)build one family's editable draft dialog content.

    Args:
        state: App state.
        family: The family the draft belongs to.
        draft: The draft to populate the editor with.
        dialog: The dialog (closed by its Close button).
        slot: The dialog's slot container (cleared before filling).
    """
    cfg = state.cfg
    out_file = html_path(cfg, family)
    with slot:
        ui.label(
            f"Editing family {family.family_id}'s clarification email"
            f" — saved file: {out_file}"
        ).classes("text-sm text-gray-500")
        subject_input = ui.input("Subject", value=draft.subject).classes(
            "w-full"
        )
        greeting_input = ui.input("Greeting", value=draft.greeting).classes(
            "w-full"
        )
        intro_box = (
            ui.textarea("Intro", value=draft.intro)
            .props("autogrow")
            .classes("w-full")
        )
        paragraph_values = list(draft.angel_paragraphs)
        if len(paragraph_values) != len(family.angels):
            ui.notify(
                "Saved draft has"
                f" {len(paragraph_values)} paragraph(s) but the family"
                " now has"
                f" {len(family.angels)} flagged Angel(s); paragraph"
                " fields were padded/trimmed to fit.",
                type="warning",
                position="top",
            )
        paragraph_values = paragraph_values[: len(family.angels)]
        paragraph_values += [""] * (len(family.angels) - len(paragraph_values))
        paragraph_boxes = [
            ui.textarea(
                f"Paragraph — {angel.first_name} ({angel.angel_id})",
                value=paragraph,
            )
            .props("autogrow")
            .classes("w-full")
            for angel, paragraph in zip(
                family.angels, paragraph_values, strict=True
            )
        ]
        closing_box = (
            ui.textarea("Closing", value=draft.closing)
            .props("autogrow")
            .classes("w-full")
        )
        source_box = (
            ui.textarea("HTML source", value="")
            .props("readonly autogrow")
            .classes("w-full font-mono")
        )
        source_box.set_visibility(False)

        def _toggle_source() -> None:
            """Flip the raw-source textarea's visibility."""
            source_box.set_visibility(not source_box.visible)

        def _collect_draft() -> EmailDraft:
            """Bundle the editor's current values into a draft."""
            return EmailDraft(
                subject=subject_input.value,
                greeting=greeting_input.value,
                intro=intro_box.value,
                angel_paragraphs=[box.value for box in paragraph_boxes],
                closing=closing_box.value,
            )

        preview_slot = ui.column().classes("w-full")

        def _refresh_preview() -> None:
            """Re-render the preview from the current editor values.

            The preview fragment carries its own solid background, so
            the email always renders on white regardless of the app
            theme; the source box shows the full document actually
            saved to disk.
            """
            try:
                email = _collect_draft().as_email()
                _subject, html = render_email(
                    family, email, cfg, subject=subject_input.value
                )
                fragment = render_email_fragment(
                    family, email, cfg, subject=subject_input.value
                )
            except ClarifyError as error:
                preview_slot.clear()
                with preview_slot:
                    ui.label(str(error)).classes("text-negative")
                return
            source_box.set_value(html)
            preview_slot.clear()
            with preview_slot:
                ui.html(fragment)

        for element in (
            subject_input,
            greeting_input,
            intro_box,
            closing_box,
            *paragraph_boxes,
        ):
            element.on_value_change(lambda _value: _refresh_preview())

        ui.separator()
        with ui.row().classes("gap-2"):
            save_button = ui.button("Save changes", icon="save")
            regenerate_button = ui.button("Regenerate with AI", icon="refresh")

            async def _on_save() -> None:
                """Persist the edited draft and refresh the CSV."""
                save_button.disable()
                try:
                    saved = await background.io_bound(
                        write_draft, cfg, family, _collect_draft()
                    )
                    await background.io_bound(write_csv, cfg)
                    _refresh_preview()
                    ui.notify(f"Saved {saved}", type="positive", position="top")
                except ClarifyError as error:
                    ui.notify(str(error), type="negative", position="top")
                except Exception as error:  # noqa: BLE001 - keep UI alive
                    logger.exception("draft save failed for family %s", family)
                    ui.notify(
                        f"Unexpected error: {error}",
                        type="negative",
                        position="top",
                    )
                finally:
                    save_button.enable()

            save_button.on_click(_on_save)

            async def _on_regenerate() -> None:
                """Ask the model again and replace the editor contents."""
                regenerate_button.disable()
                try:
                    email = await background.io_bound(
                        generate_email, family, cfg, state.sender.send
                    )
                    fresh = EmailDraft.from_email(email, default_subject(cfg))
                    await background.io_bound(write_draft, cfg, family, fresh)
                    await background.io_bound(write_csv, cfg)
                    subject_input.set_value(fresh.subject)
                    greeting_input.set_value(fresh.greeting)
                    intro_box.set_value(fresh.intro)
                    for box, paragraph in zip(
                        paragraph_boxes, fresh.angel_paragraphs, strict=True
                    ):
                        box.set_value(paragraph)
                    closing_box.set_value(fresh.closing)
                    _refresh_preview()
                    ui.notify(
                        "Regenerated and saved", type="positive", position="top"
                    )
                except ClarifyError as error:
                    ui.notify(str(error), type="negative", position="top")
                except Exception as error:  # noqa: BLE001 - keep UI alive
                    logger.exception(
                        "regeneration failed for family %s", family
                    )
                    ui.notify(
                        f"Unexpected error: {error}",
                        type="negative",
                        position="top",
                    )
                finally:
                    regenerate_button.enable()

            regenerate_button.on_click(_on_regenerate)
            ui.button("Close", on_click=dialog.close)
        with ui.row().classes("gap-2"):
            ui.button(
                "Copy subject",
                on_click=lambda: ui.clipboard.write(subject_input.value),
            ).props("flat")
            ui.button(
                "Select & copy body",
                on_click=lambda: _select_and_copy_body(family.family_id),
            ).props("flat")
            ui.button(
                "Open draft in browser",
                on_click=lambda: background.io_bound(
                    webbrowser.open, out_file.resolve().as_uri()
                ),
            ).props("flat")
            ui.button("Show HTML source", on_click=_toggle_source).props("flat")
        ui.label(
            "Edits preview live; Save changes writes the HTML file, its"
            " JSON companion, and the CSV draft reference."
        ).classes("text-xs text-gray-500")
        _refresh_preview()


def _open_editor(
    state: ClarifyState,
    family: FamilyEntry,
    draft: EmailDraft,
    dialog: Any,
    slot: Any,
) -> None:
    """Rebuild the editor dialog for one family and open it.

    Args:
        state: App state.
        family: The family whose editor to open.
        draft: The draft to load into the editor.
        dialog: The family's dialog element.
        slot: The family's dialog slot container.
    """
    slot.clear()
    _fill_editor(state, family, draft, dialog, slot)
    dialog.open()


async def _on_generate(
    state: ClarifyState,
    family: FamilyEntry,
    button: Any,
    spinner: Any,
    dialog: Any,
    slot: Any,
) -> None:
    """Generate one family's email and open the editor.

    Args:
        state: App state.
        family: The family to generate for.
        button: The generate button element (disabled during work).
        spinner: The spinner element toggled during generation.
        dialog: The editor dialog to open with fresh content.
        slot: The dialog's slot container.
    """
    button.disable()
    spinner.set_visibility(True)
    try:
        email = await background.io_bound(
            generate_email, family, state.cfg, state.sender.send
        )
        draft = EmailDraft.from_email(email, default_subject(state.cfg))
        saved = await background.io_bound(write_draft, state.cfg, family, draft)
        await background.io_bound(write_csv, state.cfg)
        _open_editor(state, family, draft, dialog, slot)
        ui.notify(f"Draft saved to {saved}", type="positive", position="top")
    except ClarifyError as error:
        ui.notify(str(error), type="negative", position="top")
    except Exception as error:  # noqa: BLE001 - never kill the UI loop
        logger.exception("email generation failed for family %s", family)
        ui.notify(f"Unexpected error: {error}", type="negative", position="top")
    finally:
        spinner.set_visibility(False)
        button.enable()


async def _on_load(
    state: ClarifyState,
    family: FamilyEntry,
    dialog: Any,
    slot: Any,
) -> None:
    """Load one family's saved draft into the editor, if any.

    Args:
        state: App state.
        family: The family whose saved draft to load.
        dialog: The editor dialog to open on success.
        slot: The dialog's slot container.
    """
    try:
        draft = await background.io_bound(load_draft, state.cfg, family)
    except ClarifyError as error:
        ui.notify(str(error), type="negative", position="top")
        return
    if draft is None:
        html_file = html_path(state.cfg, family)
        if html_file.is_file():
            ui.notify(
                f"Family {family.family_id} has a legacy HTML draft"
                f" ({html_file}) saved before editable drafts existed;"
                " regenerate to enable editing (the browser copy via"
                " the file still works).",
                type="warning",
                position="top",
            )
        else:
            ui.notify(
                f"No saved draft for family {family.family_id} yet."
                " Generate one first.",
                type="warning",
                position="top",
            )
        return
    _open_editor(state, family, draft, dialog, slot)


def _family_card(state: ClarifyState, family: FamilyEntry) -> None:
    """Render one family's card with the Angels table and actions.

    Args:
        state: App state.
        family: The family to render.
    """
    contact_name = (
        " ".join(
            part
            for part in (family.family_first_name, family.family_last_name)
            if part
        )
        or "unknown contact"
    )
    with ui.card().classes("w-full"):
        with ui.row().classes("items-center justify-between w-full"):
            ui.label(
                f"Family {family.family_id}: {contact_name}"
                f" — {len(family.angels)} Angel(s)"
            ).classes("text-lg font-bold")
            if family.family_email:
                ui.link(family.family_email, f"mailto:{family.family_email}")
            else:
                ui.badge("No email on file", color="negative")
        with ui.row().classes("items-center gap-2"):
            if html_path(state.cfg, family).is_file():
                ui.badge("Draft saved", color="positive")
            else:
                ui.badge("No draft yet", color="grey")
            ui.label(f"Phone: {family.family_phone or '—'}").classes(
                "text-sm text-gray-500"
            )

        @ui.refreshable
        def _status_area() -> None:
            """Render the contact-status badges and their controls."""

            def _status() -> FamilyStatus:
                """Return the family's status, defaulting to untouched."""
                return state.statuses.get(family.family_id, FamilyStatus())

            async def _persist(refresh: bool = True) -> None:
                """Write the status sidecar and refresh the CSV."""
                try:
                    store_statuses(state.cfg, state.statuses)
                    await background.io_bound(write_csv, state.cfg)
                    if refresh:
                        _status_area.refresh()
                except ClarifyError as error:
                    ui.notify(str(error), type="negative", position="top")

            async def _on_mark_contacted() -> None:
                """Stamp the family as contacted, right now."""
                status = _status()
                status.contacted = True
                status.contacted_at = datetime.now().isoformat(
                    timespec="seconds"
                )
                state.statuses[family.family_id] = status
                await _persist()
                ui.notify(
                    f"Marked contacted at {status.contacted_at}",
                    type="positive",
                )

            with ui.row().classes("items-center gap-2"):
                status = _status()
                if status.contacted:
                    ui.badge(
                        f"Contacted: {status.contacted_at}",
                        color="positive",
                    )
                else:
                    ui.badge("Not contacted", color="grey")
                if status.replied:
                    ui.badge("Reply received", color="positive")
                else:
                    ui.badge("No reply yet", color="grey")
                contact_button = ui.button("Mark contacted").props("flat")
                contact_button.on_click(_on_mark_contacted)

                async def _on_reply(event: Any) -> None:
                    """Record the reply-received toggle."""
                    status = _status()
                    status.replied = bool(event.value)
                    state.statuses[family.family_id] = status
                    await _persist()
                    ui.notify(
                        "Reply received"
                        if status.replied
                        else "Reply flag cleared",
                        type="positive",
                    )

                ui.switch(
                    "Reply received", value=_status().replied
                ).on_value_change(_on_reply)

        _status_area()
        with ui.expansion("Flagged Angels").classes("w-full"):
            ui.table(
                columns=[
                    {
                        "name": name,
                        "label": label,
                        "field": name,
                        "align": "left",
                    }
                    for name, label in (
                        ("angel_id", "Angel"),
                        ("first_name", "First name"),
                        ("raw_dob", "Date of birth"),
                        ("require_clarification", "Clarification needed"),
                        ("needs", "Needs"),
                        ("desires", "Wishes"),
                    )
                ],
                rows=[
                    {
                        "angel_id": angel.angel_id,
                        "first_name": angel.first_name,
                        "raw_dob": angel.raw_dob or "—",
                        "require_clarification": angel.require_clarification,
                        "needs": " | ".join(
                            part
                            for part in (angel.need_1, angel.need_2)
                            if part
                        )
                        or "—",
                        "desires": " | ".join(
                            part
                            for part in (angel.desire_1, angel.desire_2)
                            if part
                        )
                        or "—",
                    }
                    for angel in family.angels
                ],
            ).classes("w-full")
        with ui.row().classes("items-center gap-2"):
            spinner = ui.spinner("dots")
            spinner.set_visibility(False)
            dialog = ui.dialog()
            dialog.props("maximized=false").style(
                "width: 48rem; max-width: 95vw; max-height: 90vh;"
                " overflow-y: auto;"
            )
            with dialog, ui.card().classes("w-full bg-white"):
                slot = ui.column().classes("w-full")
            generate_button = ui.button(
                "Generate clarification email",
            )
            generate_button.on_click(
                lambda _args: _on_generate(
                    state, family, generate_button, spinner, dialog, slot
                )
            )
            load_button = ui.button("Load saved draft")
            load_button.on_click(
                lambda _args: _on_load(state, family, dialog, slot)
            )
            ui.label(
                "Generate or load a draft to edit, copy into your email"
                " client, and save back to disk."
            ).classes("text-sm text-gray-500")


@ui.page("/")
def _index() -> None:
    """Render the clarification overview page."""
    state = _STATE
    if state is None:
        ui.label("Server not initialized; start via `ccat clarify`.")
        return
    with ui.column().classes("w-full max-w-5xl mx-auto gap-2"):
        ui.label(
            f"Clarification emails — {state.cfg.season} Angel Tree"
        ).classes("text-2xl font-bold")
        if not state.families:
            ui.label("No Angels need clarification; nothing to send.").classes(
                "text-lg"
            )
            return
        ui.label(
            f"{len(state.families)} family(ies) with flagged Angels"
            f" — generate one polite email per family, addressed to"
            " their primary email."
        ).classes("text-gray-600")
        for family in state.families:
            _family_card(state, family)


def generate_missing(
    cfg: Config,
    families: list[FamilyEntry],
    sender: SenderLike,
    overwrite: bool = False,
) -> tuple[list[int], list[int]]:
    """Generate email drafts for families that do not have one yet.

    Runs fully headless (one opencode connection, sequential
    one-family-at-a-time generations).  A failing family is logged
    and skipped; the run continues.

    Args:
        cfg: Pipeline configuration.
        families: The flagged families.
        sender: The prompt sender to generate with.
        overwrite: Regenerate families that already have a saved
            draft, replacing their HTML and JSON files.

    Returns:
        The (done, failed) family-id lists.
    """
    if overwrite:
        targets = families
        logger.info(
            "headless generation (--overwrite): regenerating all %d"
            " family draft(s)",
            len(targets),
        )
    else:
        targets = [
            family
            for family in families
            if not html_path(cfg, family).is_file()
        ]
        logger.info(
            "headless generation: %d of %d family(ies) without a saved draft",
            len(targets),
            len(families),
        )
    done: list[int] = []
    failed: list[int] = []
    for family in targets:
        try:
            email = generate_email(family, cfg, sender.send)
            draft = EmailDraft.from_email(email, default_subject(cfg))
            path = write_draft(cfg, family, draft)
        except ClarifyError as error:
            failed.append(family.family_id)
            logger.error(
                "family %d: draft generation failed: %s",
                family.family_id,
                error,
            )
            continue
        done.append(family.family_id)
        logger.info(
            "family %d: draft saved to %s (%d done, %d failed)",
            family.family_id,
            path,
            len(done),
            len(failed),
        )
    if failed:
        logger.warning(
            "headless generation: %d family(ies) failed: %s",
            len(failed),
            sorted(failed),
        )
    return done, failed


def run(
    cfg: Config,
    generate: bool = False,
    overwrite: bool = False,
) -> None:
    """Run module 7: headless generation or the web app.

    Builds the clarification table live from the workbook and
    refreshes the configured CSV copy first, then either serves the
    NiceGUI app until interrupted (the default), or — with
    ``generate`` — exits after generating every missing family's
    draft headlessly without spawning the server.

    Args:
        cfg: Pipeline configuration.
        generate: Generate drafts for every family without one, then
            exit; no web app is started.
        overwrite: With ``generate``: regenerate existing drafts too.
            Without ``generate``: ignored (the flag does nothing and
            the server starts normally).

    Raises:
        ClarifyError: If the workbook or a required sheet is missing,
            or the configured model reference is invalid.
    """
    global _STATE
    table = build_table(Path(cfg.excel_file))
    write_csv(cfg)
    families = group_families(table)
    missing = [f.family_id for f in families if not f.family_email]
    if missing:
        logger.warning(
            "%d family(ies) have no primary email on file: %s",
            len(missing),
            missing,
        )
    if generate:
        sender = EmailSender(cfg)
        try:
            done, failed = generate_missing(
                cfg, families, sender, overwrite=overwrite
            )
        finally:
            sender.close()
        logger.info(
            "headless generation finished: %d draft(s) written, %d failure(s)",
            len(done),
            len(failed),
        )
        write_csv(cfg)
        return
    if overwrite:
        logger.info(
            "--overwrite without --generate has no effect here;"
            " starting the web app"
        )
    state = ClarifyState(cfg, families, load_statuses(cfg))
    atexit.register(state.sender.close)
    _STATE = state
    logger.info(
        "clarification tool: %d family(ies), %d flagged Angel(s);"
        " listening on http://%s:%d",
        len(families),
        table.height,
        cfg.clarify.host,
        cfg.clarify.port,
    )
    ui.run(
        host=cfg.clarify.host,
        port=cfg.clarify.port,
        title=f"Clarification emails — {cfg.season}",
        reload=False,
        show=True,
    )
