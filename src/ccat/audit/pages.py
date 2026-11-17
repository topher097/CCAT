"""Module 3 — NiceGUI pages and shared components (§8, §9.3).

Follows ``ccat.clarify.server`` exactly: a module-global
:class:`AuditState` set by :func:`ccat.audit.server.run`; ``@ui.page``
handlers; all workbook reads/writes through ``background.io_bound``
(clarify pattern); feedback via ``ui.notify``; errors never kill the
page.

Pages: dashboard ``/``, sponsors list ``/sponsors``, sponsor detail
``/sponsors/{sponsor_id}``, families list ``/families`` (with the
orphaned-children section), family detail ``/families/{family_id}``,
and child detail
``/families/{family_id}/children/{zite_record_id}``.
"""

import logging
from typing import Any
from collections.abc import Callable

from nicegui import run as background
from nicegui import ui

from ccat.audit import AuditError
from ccat.audit.store import (
    CHILD_EDITABLE_COLUMNS,
    FAMILY_EDITABLE_COLUMNS,
    ORPHAN_NOTE,
    SPONSOR_EDITABLE_COLUMNS,
    STATUS_AUDITED,
    STATUS_COLORS,
    STATUS_NEW,
    AuditData,
    _as_text,
    as_bool,
    load_audit_data,
    merge_anomoly,
    save_child,
    save_family,
    save_sponsor,
    status_of,
)
from ccat.config import Config

logger = logging.getLogger(__name__)

#: Status filter options shared by the list pages (§8.2/§8.4).
STATUS_FILTERS = [
    "all",
    "new",
    "in_progress",
    "needs_clarification",
    "ignored",
    "audited",
]


class AuditState:
    """Everything the page handlers need, shared via one module global.

    Attributes:
        cfg: Pipeline configuration.
        test_mode: ``--test`` scope — lists limited to the first family
            (lowest ``family_id``), its children, and the first sponsor.
    """

    def __init__(self, cfg: Config, test_mode: bool = False) -> None:
        """Capture the app state.

        Args:
            cfg: Pipeline configuration.
            test_mode: Limit every list to the first family/first
                sponsor scope (§10.2).
        """
        self.cfg = cfg
        self.test_mode = test_mode


#: The single app instance's state (set by ``server.run``).
_STATE: AuditState | None = None


def set_state(state: AuditState) -> None:
    """Install the app state used by every page handler."""
    global _STATE
    _STATE = state


def _state() -> AuditState:
    """Return the installed state."""
    if _STATE is None:
        raise AuditError("server not initialized; start via `ccat audit`")
    return _STATE


# ---------------------------------------------------------------------------
# Small helpers.
# ---------------------------------------------------------------------------


def status_badge(status: str) -> None:
    """Render one colored status badge (§5.3 colors)."""
    ui.badge(status.replace("_", " "), color=STATUS_COLORS.get(status, "grey"))


def _norm(value: Any) -> str | None:
    """Normalize a cell value for display, storage, and comparison."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _display(value: Any) -> str:
    """One cell's display text."""
    return _norm(value) or "—"


def _field_label(field: str) -> str:
    """Human label for one column key."""
    return field.replace("_", " ").title()


def _enum_options(cfg: Config, table: str, column: str) -> list[str] | None:
    """Resolve a column's enum vocabulary, when it has one."""
    spec = cfg.tables[table].columns.get(column)
    if spec is None or spec.type != "enum":
        return None
    try:
        return cfg.enum_values(table, column)
    except Exception:  # noqa: BLE001 - missing vocabulary degrades to text
        return None


_EDITABLE_COLUMNS = {
    "child": CHILD_EDITABLE_COLUMNS,
    "family": FAMILY_EDITABLE_COLUMNS,
    "sponsor": SPONSOR_EDITABLE_COLUMNS,
}

_LONG_TEXT_FIELDS = frozenset(
    {
        "need_1",
        "need_2",
        "desire_1",
        "desire_2",
        "interests",
        "other_notes",
        "potential_anomoly",
        "street_address",
    }
)


def _field_specs(entity: str, cfg: Config) -> list[dict[str, Any]]:
    """Field specs for one entity: label, editor kind, options.

    The child's ``special_needs`` column is excluded — it is edited by
    the dedicated selector below the table (§8.6), where its table cell
    is a live read-only display.

    Args:
        entity: "child", "family", or "sponsor".
        cfg: Pipeline configuration (enum vocabularies).

    Returns:
        One spec dict per adjudicable data column.
    """
    specs: list[dict[str, Any]] = []
    for field in _EDITABLE_COLUMNS[entity]:
        if entity == "child" and field == "special_needs":
            continue  # edited by special_needs_selector (§8.6)
        options = _enum_options(cfg, entity, field)
        specs.append(
            {
                "field": field,
                "label": _field_label(field),
                "editor": "select"
                if options
                else "textarea"
                if field in _LONG_TEXT_FIELDS
                else "text",
                "options": options,
            }
        )
    return specs


def _conflict_flags(
    field: str, raw_columns: list[dict[str, Any]]
) -> list[bool]:
    """Per-raw-column conflict markers for one field (§6.1).

    Args:
        field: The column key.
        raw_columns: The submission rows shown as raw_1..raw_n.

    Returns:
        One flag per raw column: True when the submissions disagree on
        this field (two or more distinct non-null values).
    """
    values = {_norm(row.get(field)) for row in raw_columns}
    values.discard(None)
    contested = len(values) > 1
    return [
        contested and _norm(row.get(field)) is not None for row in raw_columns
    ]


# ---------------------------------------------------------------------------
# Shared components (§8.8).
# ---------------------------------------------------------------------------


def _editor_cell(
    spec: dict[str, Any],
    value: Any,
    values: dict[str, Any],
    on_change: Callable[[], None],
) -> None:
    """Render one adjudicated-value editor writing back into ``values``."""
    field = spec["field"]
    if spec["editor"] == "select":
        element: Any = ui.select(
            options=spec["options"], value=_norm(value), clearable=True
        )
    elif spec["editor"] == "textarea":
        element = ui.textarea(value=_norm(value) or "").props("autogrow")
    else:
        element = ui.input(value=_norm(value) or "")
    element.props("dense").classes("w-full")

    def apply(event: Any) -> None:
        values[field] = _norm(event.value) if event.value else None
        on_change()

    element.on_value_change(apply)


def audit_table(
    entity: str,
    cfg: Config,
    raw_columns: list[dict[str, Any]],
    values: dict[str, Any],
    baseline: dict[str, Any],
    read_only_rows: list[dict[str, Any]],
    on_change: Callable[[], None],
) -> Callable[[str], None]:
    """Render the Field | raw_1..raw_n | Adjudicated table (§8.8).

    Every adjudicated cell's diff highlight is **live**: the yellow
    "changed vs baseline" class toggles the moment an editor value
    changes — before any save (§8.3).

    Args:
        entity: "child", "family", or "sponsor".
        cfg: Pipeline configuration.
        raw_columns: The submission rows (or the single raw baseline row
            for sponsors), one per raw column, in display order.
        values: The mutable editor-state dict (adjudicated values).
        baseline: The entity's baseline for diff highlighting (§5.2).
        read_only_rows: Protected columns rendered as static read-only
            rows: each a {field, label} dict.
        on_change: Called after any editor value change (live status
            recompute, §5.3).

    Returns:
        ``set_highlight(field)`` — re-evaluates one field's diff
        highlight (and its live read-only display, where present), so
        out-of-table editors such as the special-needs selector keep
        their table cells current.
    """
    columns = max(1, len(raw_columns))
    grid_classes = "w-full gap-2 py-1 border-b items-center"
    highlights: dict[str, Any] = {}
    live_labels: dict[str, Any] = {}

    def set_highlight(field: str) -> None:
        """Re-evaluate one field's highlight (and live display)."""
        wrapper = highlights.get(field)
        if wrapper is not None:
            differs = _norm(values.get(field)) != _norm(baseline.get(field))
            if differs:
                wrapper.classes(add="bg-yellow-200")
            else:
                wrapper.classes(remove="bg-yellow-200")
        label = live_labels.get(field)
        if label is not None:
            label.set_text(_display(values.get(field)))

    def header_cell(text: str) -> None:
        ui.label(text).classes("font-bold text-sm")

    with ui.grid(columns=columns + 2).classes(
        "w-full gap-2 py-1 border-b bg-gray-100"
    ):
        header_cell("Field")
        for index in range(columns):
            header_cell(f"raw_{index + 1}" if columns > 1 else "Raw")
        header_cell("Adjudicated")

    def field_row(
        label: str,
        field: str,
        kind: str,
        spec: dict[str, Any] | None = None,
    ) -> None:
        """One table row: label, raw cells, adjudicated cell.

        Args:
            label: The field-name cell's text.
            field: The column key.
            kind: "editable" (editor + live highlight), "live"
                (read-only display + live highlight), or "static"
                (read-only display, never changes).
            spec: The editor spec for "editable" rows.
        """
        with ui.grid(columns=columns + 2).classes(grid_classes):
            ui.label(label).classes("font-medium text-sm")
            conflict = _conflict_flags(field, raw_columns)
            for index in range(columns):
                value = raw_columns[index].get(field)
                with ui.row().classes("items-center gap-1"):
                    ui.label(_display(value)).classes("text-sm")
                    if columns > 1 and conflict[index]:
                        ui.icon("circle", color="amber").classes(
                            "text-[8px]"
                        ).tooltip("Submissions disagree on this field")
            if kind == "static":
                ui.label(_display(values.get(field))).classes(
                    "text-sm text-gray-500"
                )
                return
            # "editable" and "live" rows highlight against the baseline
            # immediately, before any save (§8.3).
            differs_now = _norm(values.get(field)) != _norm(baseline.get(field))
            with ui.element("div").classes(
                "w-full rounded" + (" bg-yellow-200" if differs_now else "")
            ) as wrapper:
                highlights[field] = wrapper
                if kind == "editable" and spec is not None:

                    def changed(f: str = field) -> None:
                        set_highlight(f)
                        on_change()

                    _editor_cell(spec, values.get(field), values, changed)
                else:
                    live_labels[field] = ui.label(
                        _display(values.get(field))
                    ).classes("text-sm text-gray-500")

    for spec in _field_specs(entity, cfg):
        field_row(spec["label"], spec["field"], "editable", spec)
    if entity == "child":
        # special_needs is edited by the dedicated selector below the
        # table (§8.6); its cell is a live read-only display that the
        # selector's set_highlight keeps current.
        field_row("Special Needs", "special_needs", "live")
    for info in read_only_rows:
        field_row(info["label"], info["field"], "static")
    return set_highlight


def notes_editor(
    controls: dict[str, Any], on_change: Callable[[], None]
) -> None:
    """Render the clarification_notes textarea (§8.8)."""
    box = (
        ui.textarea(
            "Clarification note (clarification_notes)",
            value=_norm(controls.get("note")) or "",
        )
        .props("autogrow")
        .classes("w-full")
    )

    def apply(event: Any) -> None:
        controls["note"] = event.value
        on_change()

    box.on_value_change(apply)


def special_needs_selector(
    values: dict[str, Any], on_change: Callable[[], None]
) -> None:
    """Render the special-needs checkbox list + custom input (§8.6, D4).

    Stored as the semicolon-joined list, e.g. ``"ADHD; other: speech
    delay"``; an empty selection means none.
    """
    current = _norm(values.get("special_needs")) or ""
    parts = [part.strip() for part in current.split(";") if part.strip()]
    other_text = ""
    fixed = []
    for part in parts:
        if part.lower().startswith("other"):
            remainder = part[5:].strip(": ")
            other_text = remainder
        else:
            fixed.append(part)
    state: dict[str, Any] = {"fixed": set(fixed), "other": other_text}

    def serialize() -> None:
        pieces = sorted(state["fixed"])
        if "other" in pieces:
            pieces = [piece for piece in pieces if piece != "other"]
            custom = str(state["other"]).strip()
            pieces.append(f"other: {custom}" if custom else "other")
        values["special_needs"] = "; ".join(pieces) if pieces else None
        on_change()

    with ui.card().classes("w-full"):
        ui.label("Special needs").classes("font-medium")
        for option in ("ADHD", "Autism", "Down Syndrome", "other"):
            checkbox = ui.checkbox(option, value=option in state["fixed"])

            def toggle(event: Any, option: str = option) -> None:
                if event.value:
                    state["fixed"].add(option)
                else:
                    state["fixed"].discard(option)
                serialize()

            checkbox.on_value_change(toggle)
        custom = ui.input("Custom value for 'other'", value=other_text)

        def set_other(event: Any) -> None:
            state["other"] = event.value
            serialize()

        custom.on_value_change(set_other)
        custom.classes("w-full")


# ---------------------------------------------------------------------------
# Data loading + --test scope.
# ---------------------------------------------------------------------------


def _scope_data(data: AuditData, test_mode: bool) -> AuditData:
    """Apply the --test scope: first family, its children, first sponsor."""
    if not test_mode:
        return data
    if data.families:
        first = data.families[0]
        data.children = [
            child
            for child in data.children
            if child.family_id == first.family_id
        ]
        data.orphans = data.orphans[:1]
        data.families = [first]
    else:
        data.children = data.children[:1]
        data.orphans = data.orphans[:1]
    data.sponsors = data.sponsors[:1]
    return data


async def _load(state: AuditState) -> AuditData:
    """Load the scoped audit data off the UI thread."""
    data = await background.io_bound(load_audit_data, state.cfg)
    return _scope_data(data, state.test_mode)


def _find(data: AuditData, entity: str, key: Any) -> Any:
    """Find one entry by key."""
    if entity == "sponsor":
        return next(
            (entry for entry in data.sponsors if entry.sponsor_id == key),
            None,
        )
    if entity == "family":
        return next(
            (entry for entry in data.families if entry.family_id == key),
            None,
        )
    return next(
        (entry for entry in data.children if entry.zite_record_id == str(key)),
        None,
    )


# ---------------------------------------------------------------------------
# Detail-page scaffolding shared by the three entities.
# ---------------------------------------------------------------------------


class _Detail:
    """Mutable editor state for one detail page render."""

    def __init__(
        self, entity: str, entry: Any, conflict_note: str | None
    ) -> None:
        """Seed the editor state from the entry's full final row.

        Args:
            entity: "child", "family", or "sponsor".
            entry: The SponsorEntry/FamilyEntry/ChildEntry.
            conflict_note: The DOB-conflict note merged into the
                displayed potential_anomoly (children only).
        """
        self.entity = entity
        self.entry = entry
        self.values: dict[str, Any] = dict(entry.adj)
        if entity == "child" and conflict_note:
            self.values["potential_anomoly"] = merge_anomoly(
                self.values.get("potential_anomoly"), conflict_note
            )
        self.controls: dict[str, Any] = {
            "audited": _as_text(entry.adj.get("status")) == STATUS_AUDITED,
            "status": _as_text(entry.adj.get("status")) or STATUS_NEW,
            "ignore": as_bool(entry.adj.get("ignore")),
            "note": entry.adj.get("clarification_notes"),
        }
        if (
            entity == "child"
            and entry.family_id is None
            and not _norm(self.controls.get("note"))
        ):
            # Auto-filled orphan note (§8.7).
            self.controls["note"] = ORPHAN_NOTE

    def status(self) -> str:
        """Compute the live status of the current editor state (§5.3)."""
        pseudo = {
            **self.values,
            "clarification_notes": self.controls.get("note"),
            "status": self.controls["status"],
            "ignore": self.controls["ignore"],
        }
        return status_of(pseudo, self.entry.baseline, self.entity)

    def payload(self) -> dict[str, Any]:
        """Build the save payload: data columns + the three controls."""
        payload = {
            field: _norm(self.values.get(field))
            for field in _EDITABLE_COLUMNS[self.entity]
        }
        payload["status"] = self.controls["status"]
        payload["ignore"] = self.controls["ignore"]
        payload["clarification_notes"] = _norm(self.controls.get("note"))
        return payload


# ---------------------------------------------------------------------------
# Detail-page rendering shared by the three entities.
# ---------------------------------------------------------------------------


def _save_entity(
    entity: str, cfg: Config, key: Any, payload: dict[str, Any]
) -> dict[str, Any]:
    """Dispatch one save to the right store function (typed for io_bound)."""
    if entity == "child":
        return save_child(cfg, str(key), payload)
    if entity == "family":
        return save_family(cfg, int(key), payload)
    return save_sponsor(cfg, int(key), payload)


def render_detail(
    state: AuditState,
    data: AuditData,
    entity: str,
    key: Any,
    title: str,
    prev_next: list[Any],
    raw_columns: list[dict[str, Any]],
    read_only_rows: list[dict[str, Any]],
    conflict_note: str | None = None,
) -> None:
    """Render one entity's detail page body (§8.3/§8.5/§8.6).

    Args:
        state: The app state.
        data: The scoped audit data.
        entity: "child", "family", or "sponsor".
        key: The entity's key.
        title: Page title text.
        prev_next: The in-scope key list for prev/next navigation.
        raw_columns: Submission rows for the raw columns.
        read_only_rows: Protected fields rendered read-only.
        conflict_note: The child's DOB-conflict note, if any.
    """
    entry = _find(data, entity, key)
    if entry is None:
        ui.label(f"No {entity} {key!r} found.").classes("text-lg")
        return
    detail = _Detail(entity, entry, conflict_note)
    was_ignored = as_bool(entry.adj.get("ignore"))

    with ui.column().classes("w-full max-w-6xl mx-auto gap-2"):
        with ui.row().classes("items-center gap-4 w-full"):
            ui.label(title).classes("text-2xl font-bold")
            badge_slot = ui.row().classes("items-center")

            def render_badge() -> None:
                badge_slot.clear()
                with badge_slot:
                    status_badge(detail.status())
                    if as_bool(entry.adj.get("potential_duplicate")):
                        ui.badge(
                            "potential duplicate", color="deep-orange"
                        ).tooltip(
                            "Flagged by screening as a possible duplicate"
                            " submission; resolve by ignoring the extras"
                            " (see duplicate_log)"
                        )

            render_badge()

            def on_change() -> None:
                render_badge()

        # Controls row (§8.3): Audited / Ignore / Save / prev-next.
        with ui.row().classes("items-center gap-4"):
            audited_box = ui.checkbox(
                "Audited", value=detail.controls["audited"]
            )

            def set_audited(event: Any) -> None:
                checked = bool(event.value)
                detail.controls["audited"] = checked
                detail.controls["status"] = (
                    STATUS_AUDITED if checked else STATUS_NEW
                )
                on_change()

            audited_box.on_value_change(set_audited)
            if entity == "child" and entry.family_id is None:
                audited_box.disable()
                audited_box.tooltip(
                    "Orphaned child: cannot be marked audited before a"
                    " matching family registration exists (§8.7)"
                )
            ignore_box = ui.checkbox("Ignore", value=detail.controls["ignore"])

            def set_ignore(event: Any) -> None:
                detail.controls["ignore"] = bool(event.value)
                on_change()

            ignore_box.on_value_change(set_ignore)
            if prev_next and len(prev_next) > 1 and key in prev_next:
                position = prev_next.index(key)
                for offset, icon in (-1, "arrow_back"), (1, "arrow_forward"):
                    target_index = position + offset
                    if not 0 <= target_index < len(prev_next):
                        ui.button(icon=icon).props("flat disable")
                        continue
                    target = prev_next[target_index]

                    def navigate(target: Any = target) -> None:
                        if entity == "sponsor":
                            ui.navigate.to(f"/sponsors/{target}")
                        elif entity == "family":
                            ui.navigate.to(f"/families/{target}")
                        else:
                            family = entry.family_id or 0
                            ui.navigate.to(
                                f"/families/{family}/children/{target}"
                            )

                    ui.button(icon=icon, on_click=navigate).props("flat")

        save_button = ui.button("Save", icon="save")

        async def perform_save() -> None:
            """Persist the edited row (§9.2)."""
            save_button.disable()
            try:
                await background.io_bound(
                    _save_entity, entity, state.cfg, key, detail.payload()
                )
                ui.notify("Saved", type="positive", position="top")
                ui.run_javascript("location.reload()")
            except AuditError as error:
                ui.notify(str(error), type="negative", position="top")
            except Exception as error:  # noqa: BLE001 - keep the page alive
                logger.exception("save failed for %s %s", entity, key)
                ui.notify(
                    f"Unexpected error: {error}",
                    type="negative",
                    position="top",
                )
            finally:
                save_button.enable()

        async def on_save() -> None:
            """Save, asking for confirmation on a first-time ignore."""
            if detail.controls["ignore"] and not was_ignored:
                ignore_dialog.open()
                return
            await perform_save()

        save_button.on_click(on_save)

        # Destructive-action confirmations (§5.5).
        ignore_dialog = ui.dialog()
        with ignore_dialog, ui.card():
            ui.label(
                "Ignoring destroys this row's adjudicated data — it is"
                " reduced to its identity skeleton.  Un-ignore later"
                " restores the raw baseline values and loses the edits."
            ).classes("max-w-xl")
            with ui.row():
                ui.button("Cancel", on_click=ignore_dialog.close).props("flat")

                async def confirm_ignore() -> None:
                    ignore_dialog.close()
                    await perform_save()

                ui.button("Ignore and save", on_click=confirm_ignore).props(
                    "negative"
                )
        if was_ignored:
            unignore_dialog = ui.dialog()
            with unignore_dialog, ui.card():
                ui.label(
                    "Un-ignoring re-seeds the data columns from the raw"
                    " baseline — previous adjudicated edits to this row"
                    " are lost."
                ).classes("max-w-xl")
                with ui.row():
                    ui.button("Cancel", on_click=unignore_dialog.close).props(
                        "flat"
                    )

                    async def confirm_unignore() -> None:
                        unignore_dialog.close()
                        detail.controls["ignore"] = False
                        await perform_save()

                    ui.button(
                        "Un-ignore and save",
                        on_click=confirm_unignore,
                    ).props("warning")
            ui.button(
                "Un-ignore (restores raw values)",
                on_click=unignore_dialog.open,
            ).props("outline warning")

        set_highlight = audit_table(
            entity,
            state.cfg,
            raw_columns,
            detail.values,
            entry.baseline,
            read_only_rows,
            on_change,
        )
        if entity == "child":
            # The selector writes values["special_needs"]; its table
            # cell highlights live with the other editors (§8.6).

            def selector_changed() -> None:
                set_highlight("special_needs")
                on_change()

            special_needs_selector(detail.values, selector_changed)
        notes_editor(detail.controls, on_change)


# ---------------------------------------------------------------------------
# Pages (§8.1–§8.7).
# ---------------------------------------------------------------------------


@ui.page("/")
async def dashboard() -> None:
    """Render the dashboard: two cards with per-status stats (§8.1)."""
    state = _state()

    @ui.refreshable
    async def cards() -> None:
        data = await _load(state)
        with ui.grid(columns=2).classes("w-full max-w-4xl mx-auto gap-4"):
            for label, stats, target, child_line in (
                ("Sponsors", data.sponsor_stats, "/sponsors", False),
                ("Families", data.family_stats, "/families", True),
            ):
                with (
                    ui.card()
                    .classes("cursor-pointer w-full")
                    .on(
                        "click",
                        lambda target=target: ui.navigate.to(target),
                    )
                ):
                    ui.label(label).classes("text-xl font-bold")
                    total = stats.total
                    audited = stats.by_status.get(STATUS_AUDITED, 0)
                    percent = int(100 * audited / total) if total else 0
                    ui.label(f"audited {audited} / {total} ({percent}%)")
                    with ui.column().classes("gap-1"):
                        for status, count in stats.by_status.items():
                            if count:
                                with ui.row().classes("items-center gap-1"):
                                    status_badge(status)
                                    ui.label(str(count)).classes("text-sm")
                    if child_line:
                        child = data.child_stats
                        child_audited = child.children_by_status.get(
                            STATUS_AUDITED, 0
                        )
                        child_percent = (
                            int(100 * child_audited / child.children_total)
                            if child.children_total
                            else 0
                        )
                        ui.label(
                            f"children audited {child_audited} /"
                            f" {child.children_total} ({child_percent}%)"
                        )
                        ui.label(
                            "children needing clarification:"
                            f" {child.children_needs_clarification}"
                        ).classes("text-sm text-orange-600")

    with ui.column().classes("w-full max-w-4xl mx-auto gap-2"):
        ui.label(f"Human Audit — {state.cfg.season} Angel Tree").classes(
            "text-2xl font-bold"
        )
        await cards()
        ui.timer(10.0, cards.refresh)


@ui.page("/sponsors")
async def sponsors_list() -> None:
    """Render the sponsors list with text + status filters (§8.2)."""
    state = _state()
    data = await _load(state)

    @ui.refreshable
    def rows(filter_text: str, status_filter: str) -> None:
        matches = [
            entry
            for entry in data.sponsors
            if _sponsor_matches(entry, filter_text, status_filter)
        ]
        for entry in matches:
            contact = (
                " ".join(
                    part
                    for part in (
                        entry.adj.get("first_name"),
                        entry.adj.get("last_name"),
                    )
                    if part
                )
                or "unknown contact"
            )
            organization = (
                f" — {entry.adj.get('organization_name')}"
                if entry.adj.get("organization_name")
                else ""
            )
            with (
                ui.card()
                .classes("w-full cursor-pointer")
                .on(
                    "click",
                    lambda sponsor_id=entry.sponsor_id: ui.navigate.to(
                        f"/sponsors/{sponsor_id}"
                    ),
                )
            ):
                with ui.row().classes("items-center justify-between w-full"):
                    ui.label(
                        f"Sponsor {entry.sponsor_id}: {contact}{organization}"
                    ).classes("text-lg font-bold")
                    status_badge(entry.status)
                ui.label(
                    f"{entry.adj.get('sponsor_type') or '—'} ·"
                    f" {entry.adj.get('email') or 'no email'}"
                ).classes("text-sm text-gray-500")
        if not matches:
            ui.label("No sponsors match the filter.").classes("text-gray-500")

    with ui.column().classes("w-full max-w-4xl mx-auto gap-2"):
        ui.label("Sponsors").classes("text-2xl font-bold")
        with ui.row().classes("w-full items-center gap-2"):
            filter_input = ui.input("Filter").classes("grow")
            status_select = ui.select(
                STATUS_FILTERS, value="all", label="Status"
            ).classes("w-48")
        rows("", "all")
        filter_input.on_value_change(
            lambda _event: rows.refresh(filter_input.value, status_select.value)
        )
        status_select.on_value_change(
            lambda _event: rows.refresh(filter_input.value, status_select.value)
        )


def _sponsor_matches(entry: Any, filter_text: str, status_filter: str) -> bool:
    """One sponsor row's filter predicate (§8.2)."""
    if status_filter != "all" and entry.status != status_filter:
        return False
    if not filter_text:
        return True
    haystack = " ".join(
        part
        for part in (
            str(entry.sponsor_id),
            entry.adj.get("sponsor_type") or "",
            entry.adj.get("first_name") or "",
            entry.adj.get("last_name") or "",
            entry.adj.get("organization_name") or "",
            entry.adj.get("email") or "",
        )
        if part
    ).lower()
    return filter_text.lower() in haystack


@ui.page("/sponsors/{sponsor_id}")
async def sponsor_detail(sponsor_id: int) -> None:
    """Sponsor detail: Field | Raw | Adjudicated (§8.3)."""
    state = _state()
    data = await _load(state)
    entry = _find(data, "sponsor", sponsor_id)
    if entry is None:
        ui.label(f"No sponsor {sponsor_id}.").classes("text-lg")
        return
    read_only = [
        {"field": field, "label": _field_label(field)}
        for field in (
            "pref_age_min",
            "pref_age_max",
            "accepts_disclaimer",
            "create",
            "source",
            "zite_record_id",
            "assigned_angel_ids",
            "num_angels_assigned",
        )
    ]
    render_detail(
        state,
        data,
        "sponsor",
        sponsor_id,
        f"Sponsor {sponsor_id}",
        [item.sponsor_id for item in data.sponsors],
        [entry.baseline],
        read_only,
    )


def _family_matches(entry: Any, filter_text: str, status_filter: str) -> bool:
    """One family row's filter predicate (§8.4)."""
    if status_filter != "all" and entry.status != status_filter:
        return False
    if not filter_text:
        return True
    haystack = " ".join(
        part
        for part in (
            str(entry.family_id),
            entry.adj.get("first_name") or "",
            entry.adj.get("last_name") or "",
            entry.adj.get("primary_email") or "",
        )
        if part
    ).lower()
    return filter_text.lower() in haystack


@ui.page("/families")
async def families_list() -> None:
    """Families list + the orphaned-children section (§8.4, §8.7)."""
    state = _state()
    data = await _load(state)

    @ui.refreshable
    def rows(filter_text: str, status_filter: str) -> None:
        matches = [
            entry
            for entry in data.families
            if _family_matches(entry, filter_text, status_filter)
        ]
        for entry in matches:
            child_count = sum(
                1
                for child in data.children
                if child.family_id == entry.family_id
            )
            contact = (
                " ".join(
                    part
                    for part in (
                        entry.adj.get("first_name"),
                        entry.adj.get("last_name"),
                    )
                    if part
                )
                or "unknown contact"
            )
            with (
                ui.card()
                .classes("w-full cursor-pointer")
                .on(
                    "click",
                    lambda family_id=entry.family_id: ui.navigate.to(
                        f"/families/{family_id}"
                    ),
                )
            ):
                with ui.row().classes("items-center justify-between w-full"):
                    ui.label(f"Family {entry.family_id}: {contact}").classes(
                        "text-lg font-bold"
                    )
                    status_badge(entry.status)
                ui.label(
                    f"{child_count} child(ren) ·"
                    f" {entry.adj.get('primary_email') or 'no email'}"
                ).classes("text-sm text-gray-500")
        if not matches:
            ui.label("No families match the filter.").classes("text-gray-500")

    with ui.column().classes("w-full max-w-4xl mx-auto gap-2"):
        ui.label("Families").classes("text-2xl font-bold")
        with ui.row().classes("w-full items-center gap-2"):
            filter_input = ui.input("Filter").classes("grow")
            status_select = ui.select(
                STATUS_FILTERS, value="all", label="Status"
            ).classes("w-48")
        rows("", "all")
        filter_input.on_value_change(
            lambda _event: rows.refresh(filter_input.value, status_select.value)
        )
        status_select.on_value_change(
            lambda _event: rows.refresh(filter_input.value, status_select.value)
        )
        if data.orphans:
            ui.separator()
            ui.label("Orphaned children").classes("text-lg font-bold")
            ui.label(
                "Children whose primary email has no family registration"
                " (§8.7)."
            ).classes("text-sm text-gray-500")
            for orphan in data.orphans:
                with (
                    ui.card()
                    .classes("w-full cursor-pointer")
                    .on(
                        "click",
                        lambda record_id=orphan.zite_record_id: ui.navigate.to(
                            f"/families/0/children/{record_id}"
                        ),
                    )
                ):
                    with ui.row().classes(
                        "items-center justify-between w-full"
                    ):
                        ui.label(
                            f"{orphan.adj.get('first_name') or orphan.zite_record_id}"
                            " — no family"
                        ).classes("font-bold")
                        with ui.row().classes("gap-1"):
                            ui.badge("orphan", color="warning")
                            status_badge(orphan.status)


@ui.page("/families/{family_id}")
async def family_detail(family_id: int) -> None:
    """Family detail: group columns + children list (§8.5)."""
    state = _state()
    data = await _load(state)
    entry = _find(data, "family", family_id)
    if entry is None:
        ui.label(f"No family {family_id}.").classes("text-lg")
        return
    read_only = [
        {"field": field, "label": _field_label(field)}
        for field in (
            "accepts_disclaimer",
            "created",
            "zite_created_at",
            "source",
            "zite_record_id",
            "assigned_sponsor_id",
            "assigned_angel_ids",
            "num_angels_in_family",
        )
    ]
    submissions = sorted(
        entry.submissions or [entry.baseline],
        key=lambda row: (
            str(row.get("created") or ""),
            str(row.get("zite_record_id") or ""),
        ),
    )
    render_detail(
        state,
        data,
        "family",
        family_id,
        f"Family {family_id}",
        [item.family_id for item in data.families],
        submissions,
        read_only,
    )
    children = sorted(
        (child for child in data.children if child.family_id == family_id),
        key=lambda child: child.angel_id or "",
    )
    with ui.card().classes("w-full"):
        ui.label("Children").classes("text-lg font-bold")
        for position, child in enumerate(children, start=1):
            with (
                ui.card()
                .classes("w-full cursor-pointer")
                .on(
                    "click",
                    lambda record_id=child.zite_record_id,
                    family_id=family_id: (
                        ui.navigate.to(
                            f"/families/{family_id}/children/{record_id}"
                        )
                    ),
                )
            ):
                with ui.row().classes("items-center justify-between w-full"):
                    ui.label(
                        f"{position}. {child.angel_id or '—'} —"
                        f" {child.adj.get('first_name') or '—'}"
                    ).classes("font-medium")
                    status_badge(child.status)
                ui.label(
                    f"dob {child.adj.get('dob') or '—'} ·"
                    f" {child.adj.get('gender') or '—'}"
                ).classes("text-sm text-gray-500")


@ui.page("/families/{family_id}/children/{zite_record_id}")
async def child_detail(family_id: int, zite_record_id: str) -> None:
    """Child detail: duplicate-group columns + special needs (§8.6)."""
    state = _state()
    data = await _load(state)
    entry = _find(data, "child", zite_record_id)
    if entry is None:
        ui.label(f"No child {zite_record_id}.").classes("text-lg")
        return
    if entry.family_id is None:
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("warning", color="warning")
                ui.label(
                    "Orphaned child — no family registration matches this"
                    " child's primary email. Re-run `ccat acquire` once the"
                    " family registers (§8.7)."
                ).classes("font-medium")
    read_only = [
        {"field": field, "label": _field_label(field)}
        for field in (
            "age_reported",
            "primary_email",
            "created",
            "zite_created_at",
            "source",
            "zite_record_id",
            "family_id",
            "angel_id",
            "generated_info_sheet",
            "screened",
        )
    ]
    submissions = sorted(
        entry.submissions or [entry.baseline],
        key=lambda row: (
            str(row.get("created") or ""),
            str(row.get("zite_record_id") or ""),
        ),
        reverse=True,
    )
    siblings = [
        child for child in data.children if child.family_id == entry.family_id
    ]
    ids = [
        child.zite_record_id
        for child in sorted(siblings, key=lambda child: child.angel_id or "")
    ]
    if entry.zite_record_id not in ids:
        ids.append(entry.zite_record_id)
    render_detail(
        state,
        data,
        "child",
        zite_record_id,
        f"{entry.angel_id or 'orphan'} —"
        f" {entry.adj.get('first_name') or zite_record_id}",
        ids,
        submissions,
        read_only,
        conflict_note=entry.conflict_note,
    )
    age = entry.adj.get("age_at_xmas")
    reported = entry.adj.get("age_reported")
    if reported is not None and age is not None and str(reported) != str(age):
        ui.label(
            f"Warning: family reported age {reported} but the adjudicated"
            f" dob gives {age} (§8.6)."
        ).classes("text-sm text-orange-600")


__all__ = [
    "AuditState",
    "audit_table",
    "notes_editor",
    "set_state",
    "special_needs_selector",
    "status_badge",
]
