"""Tri-state audit save service (refactor §5.1, §9.3).

The legacy NiceGUI editor sends a full form on every save, so the
underlying ``_save_row`` semantics are dense: an omitted ``ignore``
normalizes to false (unignore + reseed) and an omitted
``clarification_notes`` clears the note. This service adapts partial
PATCH-style edits onto that behavior without changing it:

- **Omitted** field -> the stored value is re-sent, so nothing changes
  (for an entity with no stored final row the field is left out of the
  legacy payload, so the baseline seed applies).
- **Explicit null** -> the field is cleared.
- **Value** -> the field is updated.

``status`` and ``ignore`` never come from a :class:`SavePatch`; they
are separate actions (:func:`ignore_entity`/:func:`unignore_entity`)
with an explicit confirmation.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict

from ccat.audit import AuditError
from ccat.audit.store import (
    ENTITY_SPECS,
    STATUS_NEW,
    as_bool,
    ensure_final_sheets,
    read_final,
    save_child,
    save_family,
    save_sponsor,
    status_of,
)
from ccat.config import Config
from ccat.services.results import EntityCounts, ReconcileResult, SaveResult

_ENTITIES = ("sponsor", "family", "child")


class SavePatch(BaseModel):
    """Partial edit of one entity's editable fields (§9.3 tri-state).

    Every field is optional and ``X | None``: a field absent from
    ``model_fields_set`` is omitted (keep the stored value), while an
    explicit ``None`` clears it. ``status`` and ``ignore`` are not
    patchable — they are separate confirmed actions. Fields belonging
    to another entity are accepted by the model but rejected by
    :func:`save_entity` against the store's editable list.

    Attributes:
        clarification_notes: The audit note control.
    """

    model_config = ConfigDict(extra="forbid")

    # child + family + sponsor editable fields (union; int-typed
    # workbook columns keep int annotations).
    first_name: str | None = None
    primary_email: str | None = None
    dob: str | None = None
    age_reported: int | None = None
    gender: str | None = None
    special_needs: str | None = None
    need_1: str | None = None
    need_2: str | None = None
    desire_1: str | None = None
    desire_2: str | None = None
    shoe_size: str | None = None
    shoe_size_type: str | None = None
    pant_waist_size: str | None = None
    pant_inseam: str | None = None
    pant_size_letter: str | None = None
    pant_size_type: str | None = None
    jean_size: str | None = None
    shirt_size_letter: str | None = None
    shirt_size_type: str | None = None
    favorite_colors: str | None = None
    interests: str | None = None
    other_notes: str | None = None
    potential_anomoly: str | None = None
    last_name: str | None = None
    phone: str | None = None
    street_address: str | None = None
    apt_floor: str | None = None
    city: str | None = None
    zip_code: str | None = None
    state: str | None = None
    secondary_first_name: str | None = None
    secondary_last_name: str | None = None
    secondary_email: str | None = None
    secondary_phone: str | None = None
    gift_pickup_location: str | None = None
    email: str | None = None
    sponsor_type: str | None = None
    organization_name: str | None = None
    prior_assigned_angel_ids: str | None = None
    num_children: int | None = None
    age_preference: str | None = None
    gender_preference: str | None = None
    dropoff_location: str | None = None
    clarification_notes: str | None = None


def save_entity(
    cfg: Config, entity: str, key: str | int, patch: SavePatch
) -> SaveResult:
    """Apply one tri-state partial save (§9.3).

    Provided fields use their patch value (explicit null clears);
    omitted fields keep the stored value. Status is derived by the
    legacy save (an existing audited authority is preserved), and the
    stored ``ignore`` flag is echoed so a field save never triggers
    the destructive unignore reseed. Saving a field patch on an
    already-ignored row keeps it ignored: the legacy ignore branch
    reseeds and skeletonizes regardless of the payload, so the patch
    values are not persisted until the row is unignored.

    Args:
        cfg: Pipeline configuration.
        entity: "child", "family", or "sponsor".
        key: zite_record_id / family_id / sponsor_id.
        patch: The partial edit; enum-ish values are not revalidated
            here, matching the legacy save.

    Returns:
        The save result with the persisted row's status and timestamp.

    Raises:
        AuditError: On unknown entities, fields outside the entity's
            editable set, or unknown keys.
    """
    spec = _spec(entity)
    provided: dict[str, Any] = {
        field: getattr(patch, field) for field in patch.model_fields_set
    }
    unknown = sorted(
        field
        for field in provided
        if field != "clarification_notes" and field not in set(spec["editable"])
    )
    if unknown:
        raise AuditError(f"unknown {entity} fields: {unknown}")
    stored = _stored_row(cfg, entity, key)
    payload: dict[str, Any] = dict(provided)
    if stored is not None:
        # Re-send stored values for omitted fields: the legacy
        # full-form save with the stored value is a no-op for them.
        for field in (*spec["editable"], "clarification_notes"):
            if field not in payload:
                payload[field] = stored.get(field)
    payload["ignore"] = as_bool(stored.get("ignore")) if stored else False
    row = _delegate_save(cfg, entity, key, payload)
    return _result(entity, key, row, applied=provided)


def ignore_entity(
    cfg: Config, entity: str, key: str | int, *, confirm: bool
) -> SaveResult:
    """Ignore one entity (destructive skeleton reset, §5.5).

    Builds the legacy payload from the current stored values with
    ``ignore=True`` so nothing else changes; the legacy save then
    reseeds from the baseline and reduces the row to its identity
    skeleton while preserving module-owned columns.

    Args:
        cfg: Pipeline configuration.
        entity: "child", "family", or "sponsor".
        key: The entity's key.
        confirm: Must be True; anything else refuses the action.

    Returns:
        The save result (status ``ignored``).

    Raises:
        AuditError: Without confirmation, or on unknown entities/keys.
    """
    if not confirm:
        raise AuditError(f"ignoring {entity} {key!r} requires confirm=True")
    return _control_save(cfg, entity, key, ignore=True)


def unignore_entity(
    cfg: Config, entity: str, key: str | int, *, confirm: bool
) -> SaveResult:
    """Un-ignore one entity (reseed from the raw baseline, §5.5).

    Sends the current stored (post-skeleton) values with
    ``ignore=False``; the re-seed itself comes solely from the legacy
    baseline reseed inside the save, so previous edits stay lost.

    Args:
        cfg: Pipeline configuration.
        entity: "child", "family", or "sponsor".
        key: The entity's key.
        confirm: Must be True; anything else refuses the action.

    Returns:
        The save result with the recomputed status.

    Raises:
        AuditError: Without confirmation, when the entity is not
            currently ignored, or on unknown entities/keys.
    """
    if not confirm:
        raise AuditError(f"un-ignoring {entity} {key!r} requires confirm=True")
    stored = _stored_row(cfg, entity, key)
    if stored is None or not as_bool(stored.get("ignore")):
        raise AuditError(f"{entity} {key!r} is not ignored")
    return _control_save(cfg, entity, key, ignore=False)


def reconcile(cfg: Config) -> ReconcileResult:
    """Run the audit backstop reconciliation and count its changes.

    Wraps :func:`ccat.audit.store.ensure_final_sheets` (seed only
    genuinely missing entities; never refresh existing rows) and
    derives appended/merged counts by diffing the final stores before
    and after.

    Args:
        cfg: Pipeline configuration.

    Returns:
        Per-entity appended/merged counts.

    Raises:
        AuditError: If no prepared raw rows exist or finals are
            malformed.
    """
    before = {entity: read_final(cfg, entity) for entity in _ENTITIES}
    ensure_final_sheets(cfg)
    after = {entity: read_final(cfg, entity) for entity in _ENTITIES}
    return ReconcileResult(counts=entity_count_changes(before, after))


def status_of_row(
    cfg: Config,
    entity: str,
    row: dict[str, Any],
    baseline: dict[str, Any],
) -> str:
    """Compute one row's status (thin API wrapper over the oracle).

    Args:
        cfg: Pipeline configuration (unused; kept for the future API
            route signature that resolves a season snapshot).
        entity: "child", "family", or "sponsor".
        row: The effective row (stored or live editor pseudo-row).
        baseline: The entity's baseline values.

    Returns:
        The status string; ``ignored`` outranks ``audited`` (D11).
    """
    return status_of(row, baseline, entity)


def entity_count_changes(
    before: dict[str, list[dict[str, Any]]],
    after: dict[str, list[dict[str, Any]]],
) -> EntityCounts:
    """Count appended and merged final rows per entity.

    Args:
        before: Complete final stores read before the operation.
        after: Complete final stores read after it.

    Returns:
        Per-entity counts: appended rows (new keys) and merged rows
        (same key, changed normalized values).
    """
    appended: dict[str, int] = {}
    merged: dict[str, int] = {}
    for entity, rows_after in after.items():
        key = ENTITY_SPECS[entity]["key"]
        before_by_key = {
            str(row.get(key)): row for row in before.get(entity, [])
        }
        appended[entity] = sum(
            1 for row in rows_after if str(row.get(key)) not in before_by_key
        )
        merged[entity] = sum(
            1
            for row in rows_after
            if str(row.get(key)) in before_by_key
            and row != before_by_key[str(row.get(key))]
        )
    return EntityCounts(appended=appended, merged=merged)


def _control_save(
    cfg: Config, entity: str, key: str | int, *, ignore: bool
) -> SaveResult:
    """Save one ignore/unignore action with the stored values."""
    spec = _spec(entity)
    stored = _stored_row(cfg, entity, key)
    payload: dict[str, Any] = {
        field: (stored or {}).get(field)
        for field in (*spec["editable"], "clarification_notes")
    }
    payload["status"] = (stored or {}).get("status") or STATUS_NEW
    payload["ignore"] = ignore
    row = _delegate_save(cfg, entity, key, payload)
    return _result(entity, key, row, applied={"ignore": ignore})


def _result(
    entity: str,
    key: str | int,
    row: dict[str, Any],
    *,
    applied: dict[str, Any],
) -> SaveResult:
    """Build the SaveResult from one persisted row."""
    return SaveResult(
        entity=entity,
        key=key,
        status=str(row.get("status") or STATUS_NEW),
        applied=applied,
        audited_at=row.get("audited_at"),
    )


def _delegate_save(
    cfg: Config, entity: str, key: str | int, payload: dict[str, Any]
) -> dict[str, Any]:
    """Dispatch to the entity's legacy store save function."""
    if entity == "child":
        return save_child(cfg, str(key), payload)
    if entity == "family":
        return save_family(cfg, _int_key("family", key), payload)
    return save_sponsor(cfg, _int_key("sponsor", key), payload)


def _int_key(entity: str, key: str | int) -> int:
    """Coerce a family/sponsor key to int, failing as unknown."""
    try:
        return int(key)
    except (TypeError, ValueError) as error:
        raise AuditError(f"unknown {entity} {key!r}") from error


def _spec(entity: str) -> dict[str, Any]:
    """Return the store's entity spec, rejecting unknown entities."""
    try:
        return ENTITY_SPECS[entity]
    except KeyError as error:
        raise AuditError(f"unknown entity {entity!r}") from error


def _stored_row(
    cfg: Config, entity: str, key: str | int
) -> dict[str, Any] | None:
    """Return the stored final row for one key, if any."""
    spec = ENTITY_SPECS[entity]
    return next(
        (
            row
            for row in read_final(cfg, entity)
            if str(row.get(spec["key"])) == str(key)
        ),
        None,
    )


__all__ = [
    "SavePatch",
    "entity_count_changes",
    "ignore_entity",
    "reconcile",
    "save_entity",
    "status_of_row",
    "unignore_entity",
]
