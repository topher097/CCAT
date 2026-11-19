"""DB-backed tri-state audit save (§9.3; services.audit over the DB).

Applies the :class:`ccat.services.audit.SavePatch` tri-state
translation to the DB-era final stores: omitted fields re-send the
stored value (a legacy no-op), explicit nulls clear, and values
update.  The row-level semantics follow ``ccat.audit.store._save_row``
— baseline reseed on ignore/unignore, ``audited_at`` stamped on every
save, module-owned columns restored, derived age/bounds recomputed,
status derived server-side — implemented over
:meth:`SeasonStoreRepository.final_rows` /
:meth:`SeasonStoreRepository.commit_finals` with optimistic expected
versions so a stale editor cannot silently overwrite (§4.4).
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ccat.audit import AuditError
from ccat.audit.store import (
    ENTITY_SPECS,
    STATUS_AUDITED,
    STATUS_IGNORED,
    STATUS_NEW,
    _as_int,
    _as_text,
    _child_baseline,
    _computed_age,
    _linkage_columns,
    _module_owned_columns,
    _seed_row_from_baseline,
    _upsert_row,
    as_bool,
    dob_conflict_notes,
    group_submissions,
    merge_anomoly,
    reduce_ignored,
    status_of,
)
from ccat.config import Config
from ccat.domain import AGE_PREF_BOUNDS
from ccat.preparation import prepare_rows
from ccat.services.audit import SavePatch
from ccat.services.results import SaveResult

if TYPE_CHECKING:
    from ccat.db.repositories import SeasonStoreRepository
    from ccat.preparation import Preparation

_ENTITIES = ("sponsor", "family", "child")


def _child_groups_from(
    preparation: "Preparation",
) -> dict[tuple[str | None, str | None], list[dict[str, Any]]]:
    """Duplicate-child groups from in-memory preparation (store twin).

    Args:
        preparation: The current preparation output.

    Returns:
        Only the two-key (email, first_name) duplicate groups.
    """
    return {
        values: rows
        for values, rows in group_submissions(
            preparation.rows["child"], "child"
        ).items()
        if len(values) == 2
    }


def _baseline_for(
    entity: str,
    preparation: "Preparation",
    prepared: dict[str, Any],
) -> dict[str, Any]:
    """Build the entity's baseline (store ``_entity_baseline`` twin).

    Args:
        entity: Entity name.
        preparation: The current preparation output.
        prepared: The entity's current prepared row.

    Returns:
        Linkage/editable baseline values.
    """
    if entity != "child":
        return {
            column: prepared.get(column) for column in _linkage_columns(entity)
        }
    group = _child_groups_from(preparation).get(
        (
            _as_text(prepared.get("primary_email")),
            _as_text(prepared.get("first_name")),
        )
    )
    return _child_baseline(prepared, group or [])


def _stored_key(row: dict[str, Any], entity: str) -> str:
    """Render one row's key in the repository's normalized form.

    Args:
        row: A final/prepared row.
        entity: Entity name.

    Returns:
        The string key (family/sponsor keys are normalized integers).
    """
    key_column = ENTITY_SPECS[entity]["key"]
    if entity in {"family", "sponsor"}:
        number = _as_int(row.get(key_column))
        return str(number)
    return _as_text(row.get(key_column)) or ""


async def _prepared_and_baseline(
    repo: "SeasonStoreRepository",
    cfg: Config,
    entity: str,
    key: str | int,
    existing: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Find the prepared row and baseline for one entity key.

    Args:
        repo: Season repository supplying raw + final stores.
        cfg: Pipeline configuration.
        entity: Entity name.
        key: The entity's key.
        existing: The stored final row, when one exists.

    Returns:
        (prepared row or None, baseline values).  A raw-missing entity
        falls back to its stored values as the baseline (§1.2 rule 7).
    """
    raw = {table: await repo.raw_rows(table) for table in _ENTITIES}
    finals = {table: await repo.final_rows(table) for table in _ENTITIES}
    preparation = prepare_rows(raw, cfg, finals, report=False)
    key_column = ENTITY_SPECS[entity]["key"]
    prepared = next(
        (
            row
            for row in preparation.rows[entity]
            if str(row.get(key_column)) == str(key)
        ),
        None,
    )
    if prepared is not None:
        return prepared, _baseline_for(entity, preparation, prepared)
    if existing is not None:
        # Raw-missing: the stored values are their own baseline.
        return (
            None,
            {
                column: existing.get(column)
                for column in (
                    *_linkage_columns(entity),
                    *ENTITY_SPECS[entity]["editable"],
                )
            },
        )
    return None, {}


async def save_entity_db(
    repo: "SeasonStoreRepository",
    cfg: Config,
    entity: str,
    key: str | int,
    patch: SavePatch,
    actor: str,
    *,
    expected_version: int | None = None,
) -> SaveResult:
    """Apply one tri-state partial save against the DB final store (§9.3).

    Args:
        repo: Season repository (reads + the single audit commit).
        cfg: Pipeline configuration.
        entity: "child", "family", or "sponsor".
        key: zite_record_id / family_id / sponsor_id.
        patch: The partial edit; fields outside the entity's editable
            set raise :class:`AuditError`.
        actor: Requesting user identity.
        expected_version: Caller's ``If-Match`` row version; when
            given, the commit checks it explicitly (§4.4 optimistic
            concurrency) instead of trusting the just-read version.

    Returns:
        The save result with the persisted row's status and timestamp.

    Raises:
        AuditError: On unknown entities/fields/keys, or marking an
            orphaned child audited (§8.7).
        StaleRowVersionError: When the stored row changed since the
            caller's read (the commit carries the expected version).
    """
    spec = ENTITY_SPECS.get(entity)
    if spec is None:
        raise AuditError(f"unknown entity {entity!r}")
    provided: dict[str, Any] = {
        field: getattr(patch, field) for field in patch.model_fields_set
    }
    unknown = sorted(
        field
        for field in provided
        if field not in {"clarification_notes", "status"}
        and field not in set(spec["editable"])
    )
    if unknown:
        raise AuditError(f"unknown {entity} fields: {unknown}")

    key_column = spec["key"]
    rows = await repo.final_rows(entity)
    existing = next(
        (row for row in rows if str(row.get(key_column)) == str(key)), None
    )
    prepared_row, baseline = await _prepared_and_baseline(
        repo, cfg, entity, key, existing
    )
    raw_for_seed = prepared_row if prepared_row is not None else existing
    if raw_for_seed is None:
        raise AuditError(f"unknown {entity} {key!r}")
    if (
        entity == "child"
        and prepared_row is not None
        and prepared_row.get("family_id") is None
        and provided.get("status") == STATUS_AUDITED
    ):
        raise AuditError(
            "orphaned child cannot be marked audited before a matching"
            " family registration exists (§8.7)"
        )

    payload: dict[str, Any] = dict(provided)
    if existing is not None:
        for field in (*spec["editable"], "clarification_notes"):
            if field not in payload:
                payload[field] = existing.get(field)
    payload["ignore"] = (
        as_bool(existing.get("ignore")) if existing is not None else False
    )
    row = await _apply_save(
        repo, cfg, entity, key, payload, existing, raw_for_seed, baseline
    )
    await _commit_save(
        repo, entity, rows, row, existing, actor, expected_version
    )
    return SaveResult(
        entity=entity,
        key=key,
        status=str(row.get("status") or STATUS_NEW),
        applied=provided,
        audited_at=row.get("audited_at"),
    )


async def _apply_save(
    repo: "SeasonStoreRepository",
    cfg: Config,
    entity: str,
    key: str | int,
    payload: dict[str, Any],
    existing: dict[str, Any] | None,
    raw_for_seed: dict[str, Any],
    baseline: dict[str, Any],
) -> dict[str, Any]:
    """Apply the legacy ``_save_row`` dict logic to one payload (§9.2)."""
    spec = ENTITY_SPECS[entity]
    key_column = spec["key"]
    ignore_now = as_bool(payload.get("ignore"))
    ignore_before = as_bool(existing.get("ignore")) if existing else False
    if ignore_now or (ignore_before and not ignore_now):
        # Ignore, or un-ignore: data columns restart from the
        # baseline (§5.5); ignore then nulls the skeleton.
        row = _seed_row_from_baseline(entity, raw_for_seed, baseline, cfg)
    else:
        row = (
            dict(existing)
            if existing is not None
            else _seed_row_from_baseline(entity, raw_for_seed, baseline, cfg)
        )
        for column in spec["editable"]:
            if column in payload:
                row[column] = payload[column]
    # Linkage + module-owned columns are screen/acquire/match/generate
    # owned on the DB (audit may not change them): a stored row keeps
    # its values through the save — including across the ignore
    # skeleton and the unignore reseed.
    if existing is not None:
        for column in (
            *_linkage_columns(entity),
            *_module_owned_columns(entity),
        ):
            row[column] = existing.get(column)
        row[key_column] = existing.get(key_column)
    else:
        row[key_column] = raw_for_seed.get(key_column)
    requested_status = _as_text(payload.get("status"))
    audited_now = requested_status == STATUS_AUDITED or (
        requested_status is None
        and existing is not None
        and _as_text(existing.get("status")) == STATUS_AUDITED
    )
    row["ignore"] = ignore_now
    row["clarification_notes"] = _as_text(payload.get("clarification_notes"))
    row["audited_at"] = datetime.now(UTC).isoformat()
    if entity == "child":
        row["age_at_xmas"] = _computed_age(row.get("dob"), cfg)
    elif entity == "sponsor":
        bounds = AGE_PREF_BOUNDS.get(str(row.get("age_preference") or ""))
        row["pref_age_min"] = bounds[0] if bounds else None
        row["pref_age_max"] = bounds[1] if bounds else None
    if existing is not None:
        for column in _module_owned_columns(entity):
            row[column] = existing.get(column)
    if ignore_now:
        row = reduce_ignored(row, entity)
        if existing is not None:
            # The skeleton's nulled linkage/module columns stay at
            # their stored values: audit may not change them (§5.1).
            for column in (
                *_linkage_columns(entity),
                *_module_owned_columns(entity),
            ):
                row[column] = existing.get(column)
        row["status"] = STATUS_IGNORED
    else:
        if entity == "child":
            log_rows = await repo.dob_log_rows()
            row["potential_anomoly"] = merge_anomoly(
                row.get("potential_anomoly"),
                dob_conflict_notes(log_rows, str(row.get(key_column))),
            )
        if audited_now:
            row["status"] = STATUS_AUDITED
        else:
            row["status"] = requested_status or STATUS_NEW
            row["status"] = status_of(row, baseline, entity)
    return row


async def _commit_save(
    repo: "SeasonStoreRepository",
    entity: str,
    rows: list[dict[str, Any]],
    row: dict[str, Any],
    existing: dict[str, Any] | None,
    actor: str,
    expected_version: int | None = None,
) -> None:
    """Upsert the saved row and commit the complete store (§4.4)."""
    key_column = ENTITY_SPECS[entity]["key"]
    _upsert_row(rows, row, key_column)
    expected = None
    if expected_version is not None:
        expected = {_stored_key(row, entity): expected_version}
    elif existing is not None and existing.get("_row_version") is not None:
        expected = {_stored_key(row, entity): existing["_row_version"]}
    await repo.commit_finals(
        "audit", entity, rows, actor, expected_versions=expected
    )


async def ignore_entity_db(
    repo: "SeasonStoreRepository",
    cfg: Config,
    entity: str,
    key: str | int,
    actor: str,
    *,
    confirm: bool,
    expected_version: int | None = None,
) -> SaveResult:
    """Ignore one entity (destructive skeleton reset, §5.5).

    Args:
        repo: Season repository.
        cfg: Pipeline configuration.
        entity: "child", "family", or "sponsor".
        key: The entity's key.
        actor: Requesting user identity.
        confirm: Must be True; anything else refuses the action.
        expected_version: Caller's ``If-Match`` row version (§4.4).

    Returns:
        The save result (status ``ignored``).

    Raises:
        AuditError: Without confirmation or on unknown keys.
    """
    if not confirm:
        raise AuditError(f"ignoring {entity} {key!r} requires confirm=True")
    return await _control_save(
        repo,
        cfg,
        entity,
        key,
        actor,
        ignore=True,
        expected_version=expected_version,
    )


async def unignore_entity_db(
    repo: "SeasonStoreRepository",
    cfg: Config,
    entity: str,
    key: str | int,
    actor: str,
    *,
    confirm: bool,
    expected_version: int | None = None,
) -> SaveResult:
    """Un-ignore one entity (reseed from the raw baseline, §5.5).

    Args:
        repo: Season repository.
        cfg: Pipeline configuration.
        entity: "child", "family", or "sponsor".
        key: The entity's key.
        actor: Requesting user identity.
        confirm: Must be True; anything else refuses the action.
        expected_version: Caller's ``If-Match`` row version (§4.4).

    Returns:
        The save result with the recomputed status.

    Raises:
        AuditError: Without confirmation, when the entity is not
            currently ignored, or on unknown keys.
    """
    if not confirm:
        raise AuditError(f"un-ignoring {entity} {key!r} requires confirm=True")
    key_column = ENTITY_SPECS[entity]["key"]
    rows = await repo.final_rows(entity)
    existing = next(
        (row for row in rows if str(row.get(key_column)) == str(key)), None
    )
    if existing is None or not as_bool(existing.get("ignore")):
        raise AuditError(f"{entity} {key!r} is not ignored")
    return await _control_save(
        repo,
        cfg,
        entity,
        key,
        actor,
        ignore=False,
        expected_version=expected_version,
    )


async def _control_save(
    repo: "SeasonStoreRepository",
    cfg: Config,
    entity: str,
    key: str | int,
    actor: str,
    *,
    ignore: bool,
    expected_version: int | None = None,
) -> SaveResult:
    """Save one ignore/unignore action with the stored values."""
    spec = ENTITY_SPECS[entity]
    key_column = spec["key"]
    rows = await repo.final_rows(entity)
    existing = next(
        (row for row in rows if str(row.get(key_column)) == str(key)), None
    )
    raw_for_seed, baseline = await _prepared_and_baseline(
        repo, cfg, entity, key, existing
    )
    if raw_for_seed is None and existing is None:
        raise AuditError(f"unknown {entity} {key!r}")
    payload: dict[str, Any] = {
        field: (existing or {}).get(field)
        for field in (*spec["editable"], "clarification_notes")
    }
    payload["status"] = (existing or {}).get("status") or STATUS_NEW
    payload["ignore"] = ignore
    seed = raw_for_seed if raw_for_seed is not None else existing
    assert seed is not None
    row = await _apply_save(
        repo, cfg, entity, key, payload, existing, seed, baseline
    )
    await _commit_save(
        repo, entity, rows, row, existing, actor, expected_version
    )
    return SaveResult(
        entity=entity,
        key=key,
        status=str(row.get("status") or STATUS_NEW),
        applied={"ignore": ignore},
        audited_at=row.get("audited_at"),
    )


__all__ = [
    "ignore_entity_db",
    "save_entity_db",
    "unignore_entity_db",
]
