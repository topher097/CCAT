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
from difflib import SequenceMatcher
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
    _raw_columns,
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
from ccat.match import canonical_location
from ccat.preparation import prepare_rows
from ccat.services.audit import SavePatch
from ccat.services.db_assignments import (
    STATE_UNASSIGNED,
    assignment_state,
)
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
        # The store's twin seeds statuses and reseeds ignores from the
        # raw baseline's linkage AND editable columns — so the audit
        # detail shows the raw_*-derived values, not dashes.
        return {column: prepared.get(column) for column in _raw_columns(entity)}
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
            # Identity survives id reconciliation: a renumbered prepared
            # row is still the same raw submission.
            or (
                existing is not None
                and _as_text(row.get("zite_record_id"))
                == _as_text(existing.get("zite_record_id"))
            )
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


def audit_sort_key(entity: str, row: dict[str, Any]) -> tuple[Any, ...]:
    """Order one audit row by its assigned IDs (family / child / sponsor).

    Children sort by their assigned family first (siblings stay
    together) and by their child id (``angel_id``) inside that family;
    orphans (no family) come last.  Families and sponsors sort by
    their numeric id.

    Args:
        entity: "child", "family", or "sponsor".
        row: One final row under legacy column names.

    Returns:
        A comparable sort key.
    """
    if entity == "child":
        family_id = _as_int(row.get("family_id"))
        return (
            family_id is None,
            family_id or 0,
            str(row.get("angel_id") or ""),
            str(row.get("zite_record_id") or ""),
        )
    return (_as_int(row.get(ENTITY_SPECS[entity]["key"])) or 0,)


def matches_query(row: dict[str, Any], q: str) -> bool:
    """Case-insensitive all-text match with a fuzzy fallback (§5.2).

    Substring over every text value of the row; when nothing contains
    the query, each whitespace-separated token is compared with
    ``difflib.SequenceMatcher`` so typos and near-misses still match.

    Args:
        row: The final row.
        q: The search query.

    Returns:
        True when any text value contains the query or is similar to it.
    """
    needle = q.strip().lower()
    if not needle:
        return True
    values = [
        str(value).lower()
        for key, value in row.items()
        if not key.startswith("_") and value is not None
    ]
    for value in values:
        if needle in value:
            return True
    return any(
        SequenceMatcher(None, needle, token).ratio() >= 0.75
        for value in values
        for token in value.split()
    )


def ordered_rows(
    entity: str,
    rows: list[dict[str, Any]],
    q: str = "",
    status: str | None = None,
) -> list[dict[str, Any]]:
    """Return the rows in the audit list's filter and sort order.

    The list and the neighbour lookup share this order, so "next" and
    "previous" always mean the adjacent row of the table the user is
    looking at.

    Args:
        entity: "child", "family", or "sponsor".
        rows: Every final row.
        q: The list's search query.
        status: The list's stored-status filter.

    Returns:
        The filtered rows in the list's order.
    """
    ordered = sorted(rows, key=lambda row: audit_sort_key(entity, row))
    return [
        row
        for row in ordered
        if (status is None or row.get("status") == status)
        and matches_query(row, q)
    ]


def row_label(entity: str, row: dict[str, Any]) -> str:
    """Return a human label for one audit row.

    Args:
        entity: "child", "family", or "sponsor".
        row: One final row.

    Returns:
        The row's name, else its email, else its key.
    """
    name = " ".join(
        str(part)
        for part in (row.get("first_name"), row.get("last_name"))
        if part
    )
    if name:
        return name
    email = row.get("primary_email") or row.get("email")
    if email:
        return str(email)
    return str(row.get(ENTITY_SPECS[entity]["key"]) or "")


def sponsor_fully_matched(row: dict[str, Any]) -> bool:
    """Return True when the sponsor's angel quota is met (no quota: any angel)."""
    quota = row.get("num_children")
    assigned = row.get("num_angels_assigned") or 0
    return assigned >= quota if quota is not None else assigned > 0


async def search_all(
    repo: "SeasonStoreRepository",
    cfg: Config,
    *,
    q: str = "",
    entity: str = "any",
    status: str | None = None,
    ignore: bool | None = None,
    sponsored: bool | None = None,
    location: str = "any",
    family_size_min: int | None = None,
    family_size_max: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Search every final_* store at once (registry page).

    The filters mirror the per-entity lists — fuzzy all-text query,
    stored status, ignore flag, sponsorship state — plus the match
    page's drop-off location bucket and live family size.  Hits come
    back as a family block, then a child block, then a sponsor block,
    each in that store's audit order.

    Args:
        repo: The season-bound repository.
        cfg: Pipeline configuration (location buckets).
        q: All-text search query with fuzzy fallback.
        entity: "any", or one of "family", "child", "sponsor".
        status: Stored-status filter.
        ignore: Ignore-flag filter.
        sponsored: Children/families: has any sponsor; sponsors: fully
            matched.
        location: Canonical drop-off/pickup bucket; "any" disables.
        family_size_min: Minimum live-children count (families and
            children; sponsors are never size-filtered).
        family_size_max: Maximum live-children count (same scope).

    Returns:
        The hits — ``{"entity", "row", "label", "family_size"}``
        dicts — and the total count across all stores.
    """
    kinds = ["family", "child", "sponsor"] if entity == "any" else [entity]
    counts = (
        await repo.family_child_counts()
        if any(kind in ("family", "child") for kind in kinds)
        else None
    )
    family_location: dict[str, str] = {}
    if location != "any" and "child" in kinds:
        for family in await repo.final_rows("family"):
            bucket = canonical_location(
                str(family.get("gift_pickup_location") or ""),
                cfg.location_groups,
            )
            if bucket is not None:
                family_location[str(family.get("family_id") or "")] = bucket
    family_children: dict[Any, list[dict[str, Any]]] = {}
    if "family" in kinds and sponsored is not None:
        for child in await repo.final_rows("child"):
            if as_bool(child.get("ignore")):
                continue
            family_children.setdefault(child.get("family_id"), []).append(child)
    hits: list[dict[str, Any]] = []
    for kind in kinds:
        rows = ordered_rows(kind, await repo.final_rows(kind), q, status)
        if ignore is not None:
            rows = [row for row in rows if as_bool(row.get("ignore")) == ignore]
        for row in rows:
            if location != "any":
                if kind == "family":
                    bucket = canonical_location(
                        str(row.get("gift_pickup_location") or ""),
                        cfg.location_groups,
                    )
                elif kind == "sponsor":
                    bucket = canonical_location(
                        str(row.get("dropoff_location") or ""),
                        cfg.location_groups,
                    )
                else:
                    bucket = family_location.get(
                        str(row.get("family_id") or ""), ""
                    )
                if bucket != location:
                    continue
            size: int | None = None
            family_id = row.get("family_id")
            if counts is not None and family_id is not None:
                if kind in ("family", "child"):
                    size = counts.get(int(family_id), 0)
                if size is not None and (
                    (family_size_min is not None and size < family_size_min)
                    or (family_size_max is not None and size > family_size_max)
                ):
                    continue
            if sponsored is not None:
                if kind == "child":
                    matched = row.get("assigned_sponsor_id") is not None
                elif kind == "family":
                    matched = (
                        assignment_state(
                            family_children.get(row.get("family_id"), [])
                        )
                        != STATE_UNASSIGNED
                    )
                else:
                    matched = sponsor_fully_matched(row)
                if matched != sponsored:
                    continue
            hits.append(
                {
                    "entity": kind,
                    "row": row,
                    "label": row_label(kind, row),
                    "family_size": size,
                }
            )
    return hits, len(hits)


def neighbours(
    entity: str,
    rows: list[dict[str, Any]],
    key: str,
    q: str = "",
    status: str | None = None,
) -> tuple[dict[str, str | None] | None, dict[str, str | None] | None]:
    """Return the entries before and after one key in the list's order.

    Args:
        entity: "child", "family", or "sponsor".
        rows: Every final row.
        key: The entity key the neighbours are taken around.
        q: The list's search query.
        status: The list's stored-status filter.

    Returns:
        A (previous, next) pair of ``{"key": ..., "label": ...}`` dicts;
        each side is None when the key sits at the list's edge.
    """
    key_column = ENTITY_SPECS[entity]["key"]
    ordered = ordered_rows(entity, rows, q, status)
    index = next(
        (
            position
            for position, row in enumerate(ordered)
            if str(row.get(key_column)) == str(key)
        ),
        None,
    )
    if index is None:
        return None, None
    return (
        _neighbour(entity, ordered[index - 1]) if index > 0 else None,
        _neighbour(entity, ordered[index + 1])
        if index + 1 < len(ordered)
        else None,
    )


def _neighbour(entity: str, row: dict[str, Any]) -> dict[str, str | None]:
    """Render one neighbouring row for the wire.

    Args:
        entity: "child", "family", or "sponsor".
        row: The neighbouring final row.

    Returns:
        The neighbour's key, label and — children only — angel id.
    """
    return {
        "key": str(row.get(ENTITY_SPECS[entity]["key"]) or ""),
        "label": row_label(entity, row),
        "angel_id": (
            str(row.get("angel_id"))
            if entity == "child" and row.get("angel_id")
            else None
        ),
    }


async def validation_statuses(
    repo: "SeasonStoreRepository",
    entity: str,
    rows: list[dict[str, Any]],
) -> dict[str, dict[str, str | None]]:
    """Email/address validation status per entity key (families, sponsors).

    The email status is the ZeroBounce check for the row's own email;
    the address status is the Nominatim check whose query matches the
    family's address parts.  Sponsors carry no address, so theirs is
    always ``None``.

    Args:
        repo: Season repository supplying the validation logs.
        entity: "family" or "sponsor".
        rows: The final rows to annotate.

    Returns:
        ``entity key -> {"email": status, "address": status}``; the
        statuses are ``None`` when the row was never checked.
    """
    if entity not in {"family", "sponsor"}:
        return {}
    from ccat.engines.nominatim_address_check import build_query

    email_statuses = await repo.email_statuses()
    address_statuses = await repo.address_statuses()
    statuses: dict[str, dict[str, str | None]] = {}
    for row in rows:
        email = (
            row.get("primary_email") if entity == "family" else row.get("email")
        )
        normalized = str(email).strip().lower() if email else ""
        address_status: str | None = None
        if entity == "family":
            query = build_query(
                row.get("street_address"),
                row.get("apt_floor"),
                row.get("city"),
                row.get("state"),
                row.get("zip_code"),
            )
            address_status = address_statuses.get(query)
        statuses[_stored_key(row, entity)] = {
            "email": email_statuses.get(normalized) if normalized else None,
            "address": address_status,
        }
    return statuses


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
    "audit_sort_key",
    "ignore_entity_db",
    "matches_query",
    "neighbours",
    "ordered_rows",
    "row_label",
    "save_entity_db",
    "search_all",
    "sponsor_fully_matched",
    "unignore_entity_db",
    "validation_statuses",
]
