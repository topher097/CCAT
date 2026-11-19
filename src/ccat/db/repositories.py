"""DB-era SeasonStore repository over the Phase 2a models (plan §4).

One :class:`SeasonStoreRepository` instance serves one season inside
one async session.  Dict rows keep the **legacy workbook vocabulary**
everywhere (``ccat.audit.store`` column names); the two deliberate
renames are translated only at this boundary through one mapping dict
per entity (:data:`LEGACY_COLUMN_RENAMES`):

- child ``potential_anomoly`` ↔ column ``ai_screening_notes``;
- sponsor ``create`` ↔ column ``created``.

Rules enforced here in executable code, not declarations:

- **Ownership** (:data:`WRITABLE_ENTITIES` / :data:`ALLOWED_CHANGES`,
  mirroring ``ccat.io.WRITABLE_SHEETS`` plus plan §5.1): a commit diffs
  each stored row against the submitted row and rejects changes to
  columns the module does not own.  ``screen`` and ``migrate`` may
  change every column of the entities they may write; ``audit`` only
  editable+control+computed columns; ``match`` only assignment columns
  on family/sponsor; ``generate`` only ``generated_info_sheet``.
- **Optimistic concurrency** (plan §4.4): submitted rows may carry
  ``_row_version`` (as returned by :meth:`final_rows`) and callers may
  pass ``expected_versions``; a mismatch raises
  :class:`StaleRowVersionError`.  Audit commits must carry an expected
  version for every row they change.  Every changed row bumps
  ``row_version``, stamps ``last_updated_by``/``last_updated_at``;
  inserts stamp ``created_by``/``created_at``.
- **No row disappearance**: complete-store commits must cover every
  stored key (:class:`EntityRemovalError`); keys are validated against
  the workbook guards (:class:`KeyValidationError`).
- **Derived recompute**: child ``age_at_xmas`` follows ``dob`` plus the
  season's Christmas date and sponsor ``pref_age_min``/``pref_age_max``
  follow ``age_preference`` via :data:`ccat.domain.AGE_PREF_BOUNDS`;
  submitted derived values are trusted only from module ``migrate``.

Each write method commits exactly one transaction (plan §4.4: keep
SQLite write transactions short).
"""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from sqlalchemy import Boolean as SaBoolean
from sqlalchemy import Integer as SaInteger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ccat.audit.store import (
    CHILD_COMPUTED_COLUMNS,
    CHILD_EDITABLE_COLUMNS,
    CHILD_MODULE_COLUMNS,
    CONTROL_COLUMNS,
    ENTITY_SPECS,
    FAMILY_ASSIGNMENT_COLUMNS,
    FAMILY_EDITABLE_COLUMNS,
    SPONSOR_ASSIGNMENT_COLUMNS,
    SPONSOR_COMPUTED_COLUMNS,
    SPONSOR_EDITABLE_COLUMNS,
    _as_int,
    _as_text,
    as_bool,
    normalize_row,
)
from ccat.db.errors import (
    DbStoreError,
    EntityRemovalError,
    KeyValidationError,
    OwnershipError,
    StaleRowVersionError,
)
from ccat.db.models import (
    AcquisitionRun,
    AddressCheck,
    Artifact,
    ClarificationDraft,
    ClarificationFamily,
    DobConflictLog,
    DuplicateLog,
    EmailCheck,
    EmailCheckTarget,
    FinalChild,
    FinalFamily,
    FinalSponsor,
    MatchAssignmentSnapshot,
    MatchReportRow,
    MatchRun,
    RawChild,
    RawFamily,
    RawSponsor,
    ScreeningRun,
    Season,
)
from ccat.domain import AGE_PREF_BOUNDS, compute_age_at_xmas

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable, Mapping
    from datetime import date

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

    from ccat.config import Config


#: Legacy workbook column -> ORM column renames, one dict per entity.
#: Every other column name is identical on both sides.
LEGACY_COLUMN_RENAMES: dict[str, dict[str, str]] = {
    "child": {"potential_anomoly": "ai_screening_notes"},
    "family": {},
    "sponsor": {"create": "created"},
}

#: Same renames for the verbatim raw tables (sponsor only).
RAW_LEGACY_RENAMES: dict[str, dict[str, str]] = {
    "child": {},
    "family": {},
    "sponsor": {"created": "create"},
}

#: Entity-level write rights, mirroring ``ccat.io.WRITABLE_SHEETS``.
#: A module absent from an entity's set may not write that entity at
#: all; ``acquire`` may never write finals.
WRITABLE_ENTITIES: dict[str, frozenset[str]] = {
    "acquire": frozenset(),
    "screen": frozenset({"child", "family", "sponsor"}),
    "audit": frozenset({"child", "family", "sponsor"}),
    "match": frozenset({"family", "sponsor"}),
    "generate": frozenset({"child"}),
    "migrate": frozenset({"child", "family", "sponsor"}),
}

#: Allowed-change columns per module/entity, in legacy names.
#: ``None`` means every column of the entity may change.
ALLOWED_CHANGES: dict[str, dict[str, frozenset[str] | None]] = {
    "screen": {"child": None, "family": None, "sponsor": None},
    "migrate": {"child": None, "family": None, "sponsor": None},
    "audit": {
        "child": frozenset(
            (*CHILD_EDITABLE_COLUMNS, *CONTROL_COLUMNS, *CHILD_COMPUTED_COLUMNS)
        ),
        "family": frozenset((*FAMILY_EDITABLE_COLUMNS, *CONTROL_COLUMNS)),
        "sponsor": frozenset(
            (
                *SPONSOR_EDITABLE_COLUMNS,
                *CONTROL_COLUMNS,
                *SPONSOR_COMPUTED_COLUMNS,
            )
        ),
    },
    "match": {
        "family": frozenset(FAMILY_ASSIGNMENT_COLUMNS),
        "sponsor": frozenset(SPONSOR_ASSIGNMENT_COLUMNS),
    },
    "generate": {"child": frozenset(CHILD_MODULE_COLUMNS)},
    "acquire": {},
}

#: Modules allowed to append NEW final rows.  Screening seeds missing
#: entities and audit backfills; match/generate only patch stored rows.
MAY_CREATE_ROWS: frozenset[str] = frozenset({"screen", "audit", "migrate"})

#: Modules allowed to replace the active raw snapshot.  ``acquire`` is
#: the ordinary path; ``migrate`` imports a verbatim workbook snapshot
#: during ``ccat db-import`` (plan §10).  Anything else is refused.
RAW_SNAPSHOT_MODULES: frozenset[str] = frozenset({"acquire", "migrate"})

#: Diagnostic sheet names accepted by :meth:`sheet_rows`.
_SHEET_TABLES: dict[str, str] = {
    "duplicate_log": "duplicate_log",
    "dob_conflict_log": "dob_conflict_log",
    "email_validation_log": "email_validation_log",
    "address_validation_log": "address_validation_log",
    "match_report": "match_report",
}


@dataclass(frozen=True)
class _FinalSpec:
    """Column inventory of one persistent-final entity."""

    entity: str
    model: Any
    key: str
    orm_key: str
    legacy_columns: tuple[str, ...]
    orm_columns: tuple[str, ...]
    to_orm: Mapping[str, str]
    to_legacy: Mapping[str, str]
    bool_columns: frozenset[str]
    int_columns: frozenset[str]


@dataclass(frozen=True)
class _RowSpec:
    """Column inventory of one run-owned row table (raw/diagnostic)."""

    entity: str
    model: Any
    orm_columns: tuple[str, ...]
    to_legacy: Mapping[str, str]
    bool_columns: frozenset[str]
    int_columns: frozenset[str]

    @property
    def legacy_columns(self) -> frozenset[str]:
        """The table's columns under their legacy dict names."""
        return frozenset(self.to_legacy.get(c, c) for c in self.orm_columns)


def _column_kinds(
    model: Any, orm_columns: Iterable[str]
) -> tuple[frozenset[str], frozenset[str]]:
    """Classify columns as boolean or integer from the table metadata.

    Args:
        model: The declarative model class.
        orm_columns: Column names to classify.

    Returns:
        A tuple of (boolean column names, integer column names).
    """
    table = model.__table__
    bools: set[str] = set()
    ints: set[str] = set()
    for name in orm_columns:
        kind = table.columns[name].type.__class__
        if issubclass(kind, SaBoolean):
            bools.add(name)
        elif issubclass(kind, SaInteger):
            ints.add(name)
    return frozenset(bools), frozenset(ints)


def _final_spec(entity: str) -> _FinalSpec:
    """Build one final-entity spec from the store registry and models.

    Args:
        entity: "child", "family", or "sponsor".

    Returns:
        The spec; raises :class:`DbStoreError` for unknown entities.
    """
    if entity not in ENTITY_SPECS:
        raise DbStoreError(f"unknown persistent entity {entity!r}")
    model = {
        "child": FinalChild,
        "family": FinalFamily,
        "sponsor": FinalSponsor,
    }[entity]
    to_orm = LEGACY_COLUMN_RENAMES[entity]
    to_legacy = {orm: legacy for legacy, orm in to_orm.items()}
    legacy_columns = tuple(ENTITY_SPECS[entity]["columns"])
    orm_columns = tuple(to_orm.get(c, c) for c in legacy_columns)
    bools, ints = _column_kinds(model, orm_columns)
    key = str(ENTITY_SPECS[entity]["key"])
    return _FinalSpec(
        entity=entity,
        model=model,
        key=key,
        orm_key=to_orm.get(key, key),
        legacy_columns=legacy_columns,
        orm_columns=orm_columns,
        to_orm=to_orm,
        to_legacy=to_legacy,
        bool_columns=bools,
        int_columns=ints,
    )


#: Final-entity specs, keyed by entity name.
_FINAL_SPECS: dict[str, _FinalSpec] = {
    entity: _final_spec(entity) for entity in ("child", "family", "sponsor")
}

_RAW_MODELS: dict[str, Any] = {
    "sponsor": RawSponsor,
    "family": RawFamily,
    "child": RawChild,
}


def _row_spec(entity: str, model: Any, renames: Mapping[str, str]) -> _RowSpec:
    """Build one run-owned table spec (raw or diagnostic).

    Args:
        entity: Table/entity name used in error messages.
        model: The declarative model class.
        renames: ORM -> legacy column renames.

    Returns:
        The spec covering every column except ``id``/``run_id``.
    """
    orm_columns = tuple(
        column.name
        for column in model.__table__.columns
        if column.name not in {"id", "run_id"}
    )
    bools, ints = _column_kinds(model, orm_columns)
    return _RowSpec(
        entity=entity,
        model=model,
        orm_columns=orm_columns,
        to_legacy=renames,
        bool_columns=bools,
        int_columns=ints,
    )


#: Raw-table specs, keyed by entity name.
_RAW_SPECS: dict[str, _RowSpec] = {
    entity: _row_spec(entity, model, RAW_LEGACY_RENAMES[entity])
    for entity, model in _RAW_MODELS.items()
}

#: Screening-diagnostic specs, keyed by sheet name.
_DIAGNOSTIC_SPECS: dict[str, _RowSpec] = {
    "duplicate_log": _row_spec("duplicate_log", DuplicateLog, {}),
    "dob_conflict_log": _row_spec("dob_conflict_log", DobConflictLog, {}),
}


def _coerce(spec: _FinalSpec, orm_column: str, value: Any) -> Any:
    """Coerce one value to its column type (store read/write semantics).

    Args:
        spec: The final-entity spec.
        orm_column: ORM column name.
        value: Raw submitted or stored value.

    Returns:
        The coerced value: ``as_bool`` for booleans, ``_as_int`` for
        integers, ``_as_text`` (blank -> None) for text.
    """
    if orm_column in spec.bool_columns:
        return as_bool(value)
    if orm_column in spec.int_columns:
        return _as_int(value)
    return _as_text(value)


def _iso(value: datetime | None) -> str | None:
    """Render one stored timestamp as ISO text.

    Args:
        value: The stored datetime, or None.

    Returns:
        The ISO-8601 string, or None.
    """
    return value.isoformat() if value is not None else None


def _config_digest(cfg: Config) -> str:
    """Hash the configuration content.

    Args:
        cfg: Pipeline configuration.

    Returns:
        The sha256 hex digest of the canonical JSON snapshot.  ``Config``
        does not retain its source path, so the digest is derived from
        the same snapshot stored alongside it — it changes exactly when
        the config content changes, which is what the update path needs.
    """
    snapshot = cfg.model_dump(mode="json")
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def ensure_season_row(session: AsyncSession, cfg: Config) -> Season:
    """Idempotently create or refresh the season row inside a session.

    Args:
        session: The session owning the transaction.
        cfg: Pipeline configuration (season name, Christmas date,
            config snapshot).

    Returns:
        The persisted :class:`~ccat.db.models.Season` row.
    """
    snapshot = cfg.model_dump(mode="json")
    digest = _config_digest(cfg)
    season = await session.scalar(
        select(Season).where(Season.name == cfg.season)
    )
    if season is None:
        season = Season(
            name=cfg.season,
            xmas_date=cfg.xmas_date,
            config_hash=digest,
            config_json=snapshot,
        )
        session.add(season)
    else:
        season.xmas_date = cfg.xmas_date
        season.config_hash = digest
        season.config_json = snapshot
    await session.flush()
    return season


async def ensure_season(
    cfg: Config, actor: str, *, engine: AsyncEngine | None = None
) -> Season:
    """Idempotently create or refresh the season row with its own session.

    Args:
        cfg: Pipeline configuration; the database URL comes from
            ``cfg.storage.database_url`` when no engine is supplied.
        actor: Attribution identity for the create/update.
        engine: Optional shared async engine; when omitted a private
            engine is created from the config and disposed after.

    Returns:
        The persisted season row (``.id`` is the season id).  ``actor``
        is accepted for call-site symmetry; seasons carry no
        attribution columns today.
    """
    own_engine = engine is None
    target = engine if engine is not None else _engine_from(cfg)
    try:
        factory = async_sessionmaker(target, expire_on_commit=False)
        async with factory() as session:
            season = await ensure_season_row(session, cfg)
            await session.commit()
            return season
    finally:
        if own_engine:
            await target.dispose()


def _engine_from(cfg: Config) -> AsyncEngine:
    """Create the async engine for the config's database URL.

    Args:
        cfg: Pipeline configuration.

    Returns:
        The engine with the connection PRAGMAs applied.
    """
    from ccat.db.engine import create_async_engine

    return create_async_engine(cfg.storage.database_url)


class MatchCommitResult(BaseModel):
    """Outcome summary of one :meth:`commit_match` transaction."""

    run_id: int | None
    families_updated: int = 0
    sponsors_updated: int = 0
    report_rows: int = 0


class SeasonStoreRepository:
    """Season-scoped SeasonStore implementation over async SQLAlchemy.

    Binds one async session to one season row.  Dict rows use the
    legacy workbook vocabulary (see the module docstring for the two
    renames); reads return normalized values with the same semantics as
    ``ccat.audit.store.read_final`` plus the ``_row_version`` /
    ``_last_updated_by`` / ``_last_updated_at`` metadata keys.
    """

    def __init__(self, session: AsyncSession, season: Season) -> None:
        """Initialize the repository.

        Args:
            session: The async session owning every transaction.
            season: The ensured season row for this store.

        Raises:
            DbStoreError: If the season's Christmas date is unparsable.
        """
        self._session = session
        # Capture plain values eagerly: a later rollback in the shared
        # session expires ORM objects, but the repository keeps working.
        self._season_id = int(season.id)
        self._config_hash = str(season.config_hash)
        season_name = str(season.name)
        xmas_date = str(season.xmas_date)
        try:
            self._xmas: date = datetime.strptime(xmas_date, "%m/%d/%Y").date()
        except ValueError as error:
            raise DbStoreError(
                f"season {season_name!r} has an unparsable xmas_date"
                f" {xmas_date!r}"
            ) from error

    @property
    def season_id(self) -> int:
        """The bound season's id (read-only accessor for API routes)."""
        return self._season_id

    # ------------------------------------------------------------------
    # Final stores.
    # ------------------------------------------------------------------

    async def final_rows(self, entity: str) -> list[dict[str, Any]]:
        """Read the complete final store under legacy dict names.

        Args:
            entity: "child", "family", or "sponsor".

        Returns:
            Every row — including ignored, orphaned, duplicate, and
            raw-missing entities — normalized like
            ``ccat.audit.store.read_final`` output, ordered by key, and
            carrying ``_row_version``, ``_last_updated_by``, and
            ``_last_updated_at``.

        Raises:
            DbStoreError: For an unknown entity.
        """
        spec = self._spec(entity)
        result = await self._session.scalars(
            select(spec.model)
            .where(spec.model.season_id == self._season_id)
            .order_by(spec.orm_key)
        )
        rows: list[dict[str, Any]] = []
        for obj in result:
            legacy = {
                spec.to_legacy.get(column, column): getattr(obj, column)
                for column in spec.orm_columns
            }
            row = normalize_row(entity, legacy)
            row["_row_version"] = obj.row_version
            row["_last_updated_by"] = obj.last_updated_by
            row["_last_updated_at"] = _iso(obj.last_updated_at)
            rows.append(row)
        return rows

    async def commit_finals(
        self,
        module: str,
        entity: str,
        rows: list[dict[str, Any]],
        actor: str | None = None,
        *,
        expected_versions: Mapping[Any, int] | None = None,
    ) -> int:
        """Atomically replace one complete final store.

        The submitted rows are the full store: every stored key must be
        present (:class:`EntityRemovalError` otherwise), keys must be
        valid (:class:`KeyValidationError`), and any column that
        changes must be owned by ``module``
        (:class:`OwnershipError`).  Rows may carry ``_row_version`` as
        their expected version; ``expected_versions`` overrides per
        key.  A mismatch raises :class:`StaleRowVersionError`.  Audit
        commits must carry an expected version for every row they
        change.  Derived columns are recomputed except from ``migrate``.

        Args:
            module: Writing module identity ("acquire"/"screen"/
                "audit"/"match"/"generate"/"migrate").
            entity: "child", "family", or "sponsor".
            rows: The complete row set under legacy dict names.
            actor: Requesting user/job identity.
            expected_versions: Optional mapping of key to expected
                ``row_version``.

        Returns:
            The number of rows inserted or updated.

        Raises:
            DbStoreError: For unknown modules/entities or audit rows
                changing without an expected version.
        """
        async with self._transaction():
            changed = await self._write_finals(
                module, entity, rows, actor or "system", expected_versions
            )
        return changed

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[None]:
        """Bound one write call to exactly one short transaction.

        Yields:
            Nothing; the surrounding session commits on normal exit and
            rolls back on any exception, so a failed write never leaves
            staged rows behind in the caller's session.
        """
        try:
            yield
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise

    async def _write_finals(
        self,
        module: str,
        entity: str,
        rows: list[dict[str, Any]],
        actor: str,
        expected_versions: Mapping[Any, int] | None,
    ) -> int:
        """Validate and stage one complete-store write (no commit).

        Args:
            module: Writing module identity.
            entity: Entity name.
            rows: Complete row set under legacy dict names.
            actor: Attribution identity.
            expected_versions: Optional key -> expected row_version.

        Returns:
            The number of rows staged for insert/update.

        Raises:
            DbStoreError: On any validation failure; nothing is staged.
        """
        spec = self._spec(entity)
        if module not in WRITABLE_ENTITIES:
            raise DbStoreError(f"unknown module {module!r}")
        if entity not in WRITABLE_ENTITIES[module]:
            raise OwnershipError(module, entity)
        expected = (
            {str(key): version for key, version in expected_versions.items()}
            if expected_versions
            else {}
        )
        submitted = self._submitted_rows(spec, rows)
        stored = await self._load_final(spec)
        missing = sorted(set(stored) - set(submitted))
        if missing:
            raise EntityRemovalError(entity, missing)
        allowed = ALLOWED_CHANGES[module][entity]
        now = datetime.now(UTC)
        updates: list[tuple[Any, dict[str, Any]]] = []
        inserts: list[dict[str, Any]] = []
        for norm_key, (raw_key, values, row_version) in submitted.items():
            version = expected.get(norm_key, row_version)
            existing = stored.get(norm_key)
            if existing is None:
                if module not in MAY_CREATE_ROWS:
                    raise OwnershipError(module, entity, raw_key)
                inserts.append(values)
                continue
            obj, stored_values = existing
            if module != "migrate":
                self._recompute_derived(entity, values)
            if version is not None and obj.row_version != version:
                raise StaleRowVersionError(entity, raw_key, obj.row_version)
            changed = [
                column
                for column in spec.orm_columns
                if values[column] != stored_values[column]
            ]
            if not changed:
                continue
            if module == "audit" and version is None:
                raise DbStoreError(
                    f"audit saves must carry an expected _row_version for"
                    f" {entity} {raw_key!r}"
                )
            illegal = tuple(
                sorted(
                    spec.to_legacy.get(column, column)
                    for column in changed
                    if allowed is not None
                    and spec.to_legacy.get(column, column) not in allowed
                )
            )
            if illegal:
                raise OwnershipError(module, entity, raw_key, illegal)
            updates.append((obj, values))
        for obj, values in updates:
            for column, value in values.items():
                setattr(obj, column, value)
            obj.row_version += 1
            obj.last_updated_by = actor
            obj.last_updated_at = now
        for values in inserts:
            self._session.add(
                spec.model(
                    season_id=self._season_id,
                    **values,
                    row_version=1,
                    created_by=actor,
                    last_updated_by=actor,
                    created_at=now,
                    last_updated_at=now,
                )
            )
        return len(updates) + len(inserts)

    def _spec(self, entity: str) -> _FinalSpec:
        """Return the final-entity spec.

        Args:
            entity: Entity name.

        Returns:
            The spec.

        Raises:
            DbStoreError: For an unknown entity.
        """
        try:
            return _FINAL_SPECS[entity]
        except KeyError:
            raise DbStoreError(
                f"unknown persistent entity {entity!r}"
            ) from None

    def _submitted_rows(
        self, spec: _FinalSpec, rows: list[dict[str, Any]]
    ) -> dict[str, tuple[Any, dict[str, Any], int | None]]:
        """Normalize submitted rows and validate keys/columns.

        Args:
            spec: The final-entity spec.
            rows: Complete row set under legacy dict names.

        Returns:
            Mapping of normalized key to (raw key, coerced values keyed
            by ORM column, expected row version from ``_row_version``).

        Raises:
            KeyValidationError: On unknown columns or null, duplicate,
                boolean, or non-integral keys.
        """
        legacy_set = set(spec.legacy_columns)
        submitted: dict[str, tuple[Any, dict[str, Any], int | None]] = {}
        for row in rows:
            extra = sorted(
                column
                for column in row
                if not column.startswith("_") and column not in legacy_set
            )
            if extra:
                raise KeyValidationError(
                    spec.entity, f"unknown columns {extra}"
                )
            raw_key = row.get(spec.key)
            norm_key = self._validate_key(spec, raw_key)
            if norm_key in submitted:
                raise KeyValidationError(
                    spec.entity, f"duplicate {spec.key} key {raw_key!r}"
                )
            values = {
                column: _coerce(
                    spec, column, row.get(spec.to_legacy.get(column, column))
                )
                for column in spec.orm_columns
            }
            version = row.get("_row_version")
            submitted[norm_key] = (
                raw_key,
                values,
                version
                if isinstance(version, int) and not isinstance(version, bool)
                else None,
            )
        return submitted

    def _validate_key(self, spec: _FinalSpec, raw_key: Any) -> str:
        """Validate and normalize one entity key.

        Args:
            spec: The final-entity spec.
            raw_key: The submitted key value.

        Returns:
            The normalized key string.

        Raises:
            KeyValidationError: On boolean, non-integral, null, or
                blank keys (mirroring ``ccat.audit.store`` /
                ``ccat.io`` guards).
        """
        if spec.entity in {"family", "sponsor"}:
            if isinstance(raw_key, bool):
                raise KeyValidationError(
                    spec.entity,
                    f"boolean {spec.key} {raw_key!r}; stable IDs must"
                    " remain integers",
                )
            if isinstance(raw_key, float) and not raw_key.is_integer():
                raise KeyValidationError(
                    spec.entity,
                    f"non-integral {spec.key} {raw_key!r}; stable IDs"
                    " must not be truncated",
                )
            if isinstance(raw_key, str):
                text = raw_key.strip()
                try:
                    numeric: float | None = float(text)
                except ValueError:
                    numeric = None
                if numeric is not None and not numeric.is_integer():
                    raise KeyValidationError(
                        spec.entity,
                        f"non-integral {spec.key} {raw_key!r}; stable"
                        " IDs must not be truncated",
                    )
            number = _as_int(raw_key)
            if number is None:
                raise KeyValidationError(
                    spec.entity, f"missing or invalid {spec.key} {raw_key!r}"
                )
            return str(number)
        text = _as_text(raw_key)
        if text is None:
            raise KeyValidationError(
                spec.entity, f"missing or blank {spec.key} key {raw_key!r}"
            )
        return text

    async def _load_final(
        self, spec: _FinalSpec
    ) -> dict[str, tuple[Any, dict[str, Any]]]:
        """Load the stored final rows with coerced values.

        Args:
            spec: The final-entity spec.

        Returns:
            Mapping of normalized key to (ORM object, coerced values).
        """
        result = await self._session.scalars(
            select(spec.model).where(spec.model.season_id == self._season_id)
        )
        stored: dict[str, tuple[Any, dict[str, Any]]] = {}
        for obj in result:
            values = {
                column: _coerce(spec, column, getattr(obj, column))
                for column in spec.orm_columns
            }
            stored[str(getattr(obj, spec.orm_key))] = (obj, values)
        return stored

    def _recompute_derived(self, entity: str, values: dict[str, Any]) -> None:
        """Recompute derived columns inside one submitted value set.

        Args:
            entity: Entity name.
            values: Coerced values keyed by ORM column; updated in
                place.
        """
        if entity == "child":
            values["age_at_xmas"] = self._age(values.get("dob"))
        elif entity == "sponsor":
            bounds = AGE_PREF_BOUNDS.get(
                str(values.get("age_preference") or "")
            )
            values["pref_age_min"] = bounds[0] if bounds else None
            values["pref_age_max"] = bounds[1] if bounds else None

    def _age(self, dob: str | None) -> int | None:
        """Compute the age at Christmas from one adjudicated dob.

        Args:
            dob: ISO date-of-birth string, or None.

        Returns:
            The completed-years age, or None when absent/unparsable —
            never a guessed age.
        """
        if dob is None:
            return None
        try:
            return compute_age_at_xmas(dob, self._xmas)
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Raw acquisition snapshots.
    # ------------------------------------------------------------------

    async def raw_rows(self, table: str) -> list[dict[str, Any]]:
        """Read the current active raw snapshot rows verbatim.

        Args:
            table: "sponsor", "family", or "child" (a ``raw_`` prefix
                is tolerated).

        Returns:
            The active run's rows under legacy dict names, in insertion
            order; empty when no active run exists.

        Raises:
            DbStoreError: For an unknown table.
        """
        spec = self._raw_spec(table)
        run = await self._session.scalar(
            select(AcquisitionRun)
            .where(
                AcquisitionRun.season_id == self._season_id,
                AcquisitionRun.table_name == spec.entity,
                AcquisitionRun.active.is_(True),
            )
            .order_by(AcquisitionRun.id.desc())
        )
        if run is None:
            return []
        result = await self._session.scalars(
            select(spec.model)
            .where(spec.model.run_id == run.id)
            .order_by(spec.model.id)
        )
        return [self._row_dict(spec, obj) for obj in result]

    async def replace_raw_snapshot(
        self,
        module: str = "acquire",
        raw: Mapping[str, list[dict[str, Any]]] | None = None,
        actor: str | None = None,
    ) -> dict[str, int]:
        """Promote one complete raw acquisition snapshot atomically.

        Per table: a new active ``acquisition_run`` is created, its rows
        inserted verbatim (duplicate source IDs included — preparation
        detects them later), and every previous active run for that
        table deactivated — all in one transaction.  Raw replacement
        never touches final rows (§1.2 rule 1).

        Args:
            module: Writing module identity ("acquire" or "migrate").
            raw: Mapping of table name ("sponsor"/"family"/"child") to
                its complete verbatim rows.
            actor: Requesting user/job identity.

        Returns:
            Inserted row counts per table.

        Raises:
            OwnershipError: When any module outside
                :data:`RAW_SNAPSHOT_MODULES` is supplied.
            DbStoreError: For unknown tables.
            KeyValidationError: For unknown columns.
        """
        if module not in RAW_SNAPSHOT_MODULES:
            raise OwnershipError(module, "raw_snapshot")
        async with self._transaction():
            now = datetime.now(UTC)
            counts: dict[str, int] = {}
            for table, rows in (raw or {}).items():
                spec = self._raw_spec(table)
                for run in await self._session.scalars(
                    select(AcquisitionRun).where(
                        AcquisitionRun.season_id == self._season_id,
                        AcquisitionRun.table_name == spec.entity,
                        AcquisitionRun.active.is_(True),
                    )
                ):
                    run.active = False
                run = AcquisitionRun(
                    season_id=self._season_id,
                    table_name=spec.entity,
                    actor=actor or "system",
                    active=True,
                    row_count=len(rows),
                    started_at=now,
                    finished_at=now,
                )
                self._session.add(run)
                await self._session.flush()
                for obj in self._run_row_objects(spec, rows, run.id):
                    self._session.add(obj)
                counts[table] = len(rows)
        return counts

    def _raw_spec(self, table: str) -> _RowSpec:
        """Return the raw-table spec for one table name.

        Args:
            table: "sponsor"/"family"/"child", with optional
                ``raw_`` prefix.

        Returns:
            The spec.

        Raises:
            DbStoreError: For an unknown table.
        """
        entity = table.removeprefix("raw_")
        try:
            return _RAW_SPECS[entity]
        except KeyError:
            raise DbStoreError(f"unknown raw table {table!r}") from None

    # ------------------------------------------------------------------
    # Screening diagnostics.
    # ------------------------------------------------------------------

    async def replace_screen_diagnostics(
        self,
        duplicate_rows: list[dict[str, Any]],
        dob_rows: list[dict[str, Any]],
        actor: str | None = None,
    ) -> int:
        """Create one screening run owning the new diagnostic snapshots.

        Reads (:meth:`duplicate_log_rows` / :meth:`dob_log_rows`) always
        serve the latest run for the season; earlier runs keep their
        rows as history.

        Args:
            duplicate_rows: Complete duplicate_log snapshot rows.
            dob_rows: Complete dob_conflict_log snapshot rows.
            actor: Requesting user/job identity.

        Returns:
            The number of diagnostic rows created.

        Raises:
            KeyValidationError: For unknown columns.
        """
        async with self._transaction():
            run = ScreeningRun(
                season_id=self._season_id,
                actor=actor or "system",
                created_at=datetime.now(UTC),
                config_hash=self._config_hash,
            )
            self._session.add(run)
            await self._session.flush()
            for spec, rows in (
                (_DIAGNOSTIC_SPECS["duplicate_log"], duplicate_rows),
                (_DIAGNOSTIC_SPECS["dob_conflict_log"], dob_rows),
            ):
                for obj in self._run_row_objects(spec, rows, run.id):
                    self._session.add(obj)
        return len(duplicate_rows) + len(dob_rows)

    async def _latest_screening_run(self) -> ScreeningRun | None:
        """Return the latest screening run for the season.

        Returns:
            The run with the highest id, or None when none exists.
        """
        return await self._session.scalar(
            select(ScreeningRun)
            .where(ScreeningRun.season_id == self._season_id)
            .order_by(ScreeningRun.id.desc())
        )

    async def duplicate_log_rows(self) -> list[dict[str, Any]]:
        """Read the latest screening run's duplicate-log rows.

        Returns:
            The current duplicate diagnostics under legacy dict names.
        """
        run = await self._latest_screening_run()
        if run is None:
            return []
        spec = _DIAGNOSTIC_SPECS["duplicate_log"]
        result = await self._session.scalars(
            select(spec.model)
            .where(spec.model.run_id == run.id)
            .order_by(spec.model.id)
        )
        return [self._row_dict(spec, obj) for obj in result]

    async def dob_log_rows(self) -> list[dict[str, Any]]:
        """Read the latest screening run's DOB-conflict rows.

        Returns:
            The current DOB diagnostics under legacy dict names.
        """
        run = await self._latest_screening_run()
        if run is None:
            return []
        spec = _DIAGNOSTIC_SPECS["dob_conflict_log"]
        result = await self._session.scalars(
            select(spec.model)
            .where(spec.model.run_id == run.id)
            .order_by(spec.model.id)
        )
        return [self._row_dict(spec, obj) for obj in result]

    def _run_row_objects(
        self, spec: _RowSpec, rows: list[dict[str, Any]], run_id: int
    ) -> list[Any]:
        """Build run-owned ORM rows from legacy-named dicts.

        Args:
            spec: The row-table spec.
            rows: Legacy-named row dicts.
            run_id: The owning run's id.

        Returns:
            The ORM objects (not yet added to the session).

        Raises:
            KeyValidationError: For unknown columns.
        """
        legacy_set = spec.legacy_columns
        objects: list[Any] = []
        for row in rows:
            extra = sorted(
                column
                for column in row
                if not column.startswith("_") and column not in legacy_set
            )
            if extra:
                raise KeyValidationError(
                    spec.entity, f"unknown columns {extra}"
                )
            values: dict[str, Any] = {}
            for column in spec.orm_columns:
                legacy = spec.to_legacy.get(column, column)
                raw = row.get(legacy)
                if column in spec.bool_columns:
                    values[column] = as_bool(raw)
                elif column in spec.int_columns:
                    values[column] = _as_int(raw)
                elif isinstance(raw, dict | list):
                    values[column] = json.dumps(raw)
                else:
                    values[column] = _as_text(raw)
            objects.append(spec.model(run_id=run_id, **values))
        return objects

    def _row_dict(self, spec: _RowSpec, obj: Any) -> dict[str, Any]:
        """Render one run-owned ORM row as a legacy-named dict.

        Args:
            spec: The row-table spec.
            obj: The ORM row.

        Returns:
            The verbatim row under legacy dict names.
        """
        return {
            spec.to_legacy.get(column, column): getattr(obj, column)
            for column in spec.orm_columns
        }

    # ------------------------------------------------------------------
    # External validation logs.
    # ------------------------------------------------------------------

    async def email_log_rows(self) -> list[dict[str, Any]]:
        """Read every email-validation row for the season.

        Returns:
            One dict per (record, field) target joined with its check,
            under the legacy ``email_validation_log`` names.
        """
        result = await self._session.execute(
            select(EmailCheckTarget, EmailCheck)
            .join(EmailCheck, EmailCheckTarget.email_check_id == EmailCheck.id)
            .where(EmailCheck.season_id == self._season_id)
            .order_by(EmailCheckTarget.id)
        )
        return [
            {
                "source_sheet": target.source_sheet,
                "zite_record_id": target.zite_record_id,
                "field": target.field,
                "email": target.email,
                "status": check.status,
                "sub_status": check.sub_status,
                "did_you_mean": check.did_you_mean,
                "response_json": check.response_json,
                "checked_at": check.checked_at,
            }
            for target, check in result
        ]

    async def append_email_log(
        self, rows: list[dict[str, Any]], actor: str | None = None
    ) -> int:
        """Append email-check results with normalized-email replay.

        Any row whose normalized email (``strip().lower()``) already
        exists in the season's checks is skipped — replay is by email,
        not by record/field.  Each new row inserts one
        ``email_check`` plus its ``email_check_target``.

        Args:
            rows: Legacy ``email_validation_log`` row dicts.
            actor: Requesting user/job identity; checks carry only
                their legacy ``checked_at`` timestamps.

        Returns:
            The number of newly inserted checks.
        """
        async with self._transaction():
            existing = set(
                await self._session.scalars(
                    select(EmailCheck.normalized_email).where(
                        EmailCheck.season_id == self._season_id
                    )
                )
            )
            inserted = 0
            for row in rows:
                email = _as_text(row.get("email"))
                normalized = email.strip().lower() if email else None
                if normalized is None or normalized in existing:
                    continue
                check = EmailCheck(
                    season_id=self._season_id,
                    normalized_email=normalized,
                    status=_as_text(row.get("status")),
                    sub_status=_as_text(row.get("sub_status")),
                    did_you_mean=_as_text(row.get("did_you_mean")),
                    response_json=_as_text(row.get("response_json")),
                    checked_at=_as_text(row.get("checked_at")),
                )
                self._session.add(check)
                await self._session.flush()
                self._session.add(
                    EmailCheckTarget(
                        email_check_id=check.id,
                        source_sheet=_as_text(row.get("source_sheet")),
                        zite_record_id=_as_text(row.get("zite_record_id")),
                        field=_as_text(row.get("field")),
                        email=email,
                    )
                )
                existing.add(normalized)
                inserted += 1
        return inserted

    async def address_log_rows(self) -> list[dict[str, Any]]:
        """Read every address-validation row for the season.

        Returns:
            The stored checks under the legacy
            ``address_validation_log`` names (one representative row
            per distinct query; source record identity is not stored).
        """
        result = await self._session.scalars(
            select(AddressCheck)
            .where(AddressCheck.season_id == self._season_id)
            .order_by(AddressCheck.id)
        )
        return [
            {
                "query": check.query,
                "status": check.status,
                "display_name": check.display_name,
                "lat": check.lat,
                "lon": check.lon,
                "response_json": check.response_json,
                "checked_at": check.checked_at,
            }
            for check in result
        ]

    async def append_address_log(
        self, rows: list[dict[str, Any]], actor: str | None = None
    ) -> int:
        """Append address-check results with exact-query replay.

        Rows whose exact query already exists (in storage or earlier in
        the same batch) are skipped; one row is inserted per distinct
        query.

        Args:
            rows: Legacy ``address_validation_log`` row dicts.
            actor: Requesting user/job identity; checks carry only
                their legacy ``checked_at`` timestamps.

        Returns:
            The number of newly inserted checks.
        """
        async with self._transaction():
            existing = set(
                await self._session.scalars(
                    select(AddressCheck.query).where(
                        AddressCheck.season_id == self._season_id
                    )
                )
            )
            inserted = 0
            for row in rows:
                query = _as_text(row.get("query"))
                if query is None or query in existing:
                    continue
                self._session.add(
                    AddressCheck(
                        season_id=self._season_id,
                        query=query,
                        status=_as_text(row.get("status")),
                        display_name=_as_text(row.get("display_name")),
                        lat=_as_text(row.get("lat")),
                        lon=_as_text(row.get("lon")),
                        response_json=_as_text(row.get("response_json")),
                        checked_at=_as_text(row.get("checked_at")),
                    )
                )
                existing.add(query)
                inserted += 1
        return inserted

    # ------------------------------------------------------------------
    # Match.
    # ------------------------------------------------------------------

    async def commit_match(
        self,
        family_rows: list[dict[str, Any]],
        sponsor_rows: list[dict[str, Any]],
        report_rows: list[dict[str, Any]] | None,
        actor: str | None = None,
        *,
        overwrite: bool = False,
        test_mode: bool = False,
    ) -> MatchCommitResult:
        """Commit one match run's archive, report, and assignments.

        One transaction: the prior family/sponsor assignment columns
        are archived into ``match_assignment_snapshots`` (always —
        ``overwrite`` only governs whether a prior *report* is
        superseded by making this run the latest), the report rows are
        inserted, and the assignment columns are applied through the
        module-"match" ownership path with version bumps.  Only
        assignment columns may change; every other column must match
        current storage.  ``report_rows=None`` is ``--skip-match``: no
        run, report, or archive is created and only the reconcile
        assignments are applied.

        Args:
            family_rows: Complete family rows with patched assignment
                fields.
            sponsor_rows: Complete sponsor rows with patched
                assignment fields.
            report_rows: The new match-report rows, or None.
            actor: Requesting user/job identity.
            overwrite: Recorded on the run; latest run stays current.
            test_mode: Recorded on the run.

        Returns:
            The commit summary.

        Raises:
            OwnershipError: When a non-assignment column changes or a
                row would be created.
            EntityRemovalError: When submitted rows drop stored keys.
            StaleRowVersionError: When a submitted row carries a stale
                ``_row_version``.
        """
        async with self._transaction():
            now = datetime.now(UTC)
            run_id: int | None = None
            if report_rows is not None:
                run = MatchRun(
                    season_id=self._season_id,
                    created_at=now,
                    actor=actor or "system",
                    overwrite=overwrite,
                    test_mode=test_mode,
                    config_hash=self._config_hash,
                )
                self._session.add(run)
                await self._session.flush()
                run_id = run.id
                for entity, model in (
                    ("family", FinalFamily),
                    ("sponsor", FinalSponsor),
                ):
                    spec = self._spec(entity)
                    columns = (
                        FAMILY_ASSIGNMENT_COLUMNS
                        if entity == "family"
                        else SPONSOR_ASSIGNMENT_COLUMNS
                    )
                    for obj in await self._session.scalars(
                        select(model).where(model.season_id == self._season_id)
                    ):
                        self._session.add(
                            MatchAssignmentSnapshot(
                                run_id=run_id,
                                entity=entity,
                                key=str(getattr(obj, spec.orm_key)),
                                assignment_values={
                                    column: getattr(obj, column)
                                    for column in columns
                                },
                            )
                        )
                for row in report_rows:
                    extra = sorted(
                        column
                        for column in row
                        if column not in {"kind", "ref", "reason"}
                    )
                    if extra:
                        raise KeyValidationError(
                            "match_report", f"unknown columns {extra}"
                        )
                    self._session.add(
                        MatchReportRow(
                            run_id=run_id,
                            kind=_as_text(row.get("kind")),
                            ref=_as_text(row.get("ref")),
                            reason=_as_text(row.get("reason")),
                        )
                    )
            families_updated = await self._write_finals(
                "match", "family", family_rows, actor or "system", None
            )
            sponsors_updated = await self._write_finals(
                "match", "sponsor", sponsor_rows, actor or "system", None
            )
        return MatchCommitResult(
            run_id=run_id,
            families_updated=families_updated,
            sponsors_updated=sponsors_updated,
            report_rows=len(report_rows or []),
        )

    async def match_report_rows(self) -> list[dict[str, Any]] | None:
        """Read the latest match run's report rows.

        Returns:
            The current report's rows, or None when no run exists — a
            legitimately absent report stays absent.
        """
        run = await self._session.scalar(
            select(MatchRun)
            .where(MatchRun.season_id == self._season_id)
            .order_by(MatchRun.id.desc())
        )
        if run is None:
            return None
        result = await self._session.scalars(
            select(MatchReportRow)
            .where(MatchReportRow.run_id == run.id)
            .order_by(MatchReportRow.id)
        )
        return [
            {"kind": row.kind, "ref": row.ref, "reason": row.reason}
            for row in result
        ]

    # ------------------------------------------------------------------
    # Artifacts.
    # ------------------------------------------------------------------

    async def register_artifact(
        self,
        kind: str,
        path: str,
        filename: str,
        content_type: str,
        size_bytes: int,
        sha256: str,
        actor: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> int:
        """Register one generated file artifact.

        Args:
            kind: Artifact kind (child_sheet / sponsor_packet / ...).
            path: Server-relative path under the artifact root.
            filename: Download filename.
            content_type: MIME type.
            size_bytes: File size.
            sha256: Content digest.
            actor: Creating user/job identity.
            meta: Optional JSON metadata.

        Returns:
            The new artifact id.
        """
        async with self._transaction():
            artifact = Artifact(
                season_id=self._season_id,
                kind=kind,
                path=path,
                filename=filename,
                content_type=content_type,
                size_bytes=size_bytes,
                sha256=sha256,
                meta=dict(meta) if meta else None,
                created_by=actor or "system",
                created_at=datetime.now(UTC),
            )
            self._session.add(artifact)
            await self._session.flush()
        return int(artifact.id)

    async def artifact_rows(
        self, kind: str | None = None
    ) -> list[dict[str, Any]]:
        """List the season's artifacts.

        Args:
            kind: Optional kind filter.

        Returns:
            Artifact metadata dicts ordered by id.
        """
        statement = select(Artifact).where(
            Artifact.season_id == self._season_id
        )
        if kind is not None:
            statement = statement.where(Artifact.kind == kind)
        result = await self._session.scalars(statement.order_by(Artifact.id))
        return [self._artifact_dict(row) for row in result]

    async def artifact(self, artifact_id: int) -> dict[str, Any] | None:
        """Read one artifact's metadata.

        Args:
            artifact_id: The artifact id.

        Returns:
            The artifact dict, or None when absent in this season.
        """
        row = await self._session.scalar(
            select(Artifact).where(
                Artifact.season_id == self._season_id,
                Artifact.id == artifact_id,
            )
        )
        return self._artifact_dict(row) if row is not None else None

    def _artifact_dict(self, row: Artifact) -> dict[str, Any]:
        """Render one artifact row as a dict.

        Args:
            row: The artifact ORM row.

        Returns:
            The artifact metadata dict.
        """
        return {
            "id": row.id,
            "kind": row.kind,
            "path": row.path,
            "filename": row.filename,
            "content_type": row.content_type,
            "size_bytes": row.size_bytes,
            "sha256": row.sha256,
            "meta": row.meta,
            "created_by": row.created_by,
            "created_at": _iso(row.created_at),
        }

    # ------------------------------------------------------------------
    # Clarification state (decision D5).
    # ------------------------------------------------------------------

    async def clarification_families(self) -> list[dict[str, Any]]:
        """List the season's clarification contact state.

        Returns:
            One dict per family, ordered by family_id.
        """
        result = await self._session.scalars(
            select(ClarificationFamily)
            .where(ClarificationFamily.season_id == self._season_id)
            .order_by(ClarificationFamily.family_id)
        )
        return [self._clarification_dict(row) for row in result]

    async def set_contact_status(
        self,
        family_id: int,
        contacted: bool | None = None,
        contacted_at: str | None = None,
        replied: bool | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Upsert one family's contact state, changing only supplies.

        Only fields whose argument is not None change (tri-state: a
        real ``False`` updates the flag, ``None`` leaves it alone).

        Args:
            family_id: The family's key.
            contacted: New contacted flag, or None to keep.
            contacted_at: New contacted-at text, or None to keep.
            replied: New replied flag, or None to keep.
            actor: Requesting user identity.

        Returns:
            The persisted state dict.
        """
        async with self._transaction():
            row = await self._session.scalar(
                select(ClarificationFamily).where(
                    ClarificationFamily.season_id == self._season_id,
                    ClarificationFamily.family_id == family_id,
                )
            )
            if row is None:
                row = ClarificationFamily(
                    season_id=self._season_id,
                    family_id=family_id,
                    contacted=False,
                    replied=False,
                    source="db",
                )
                self._session.add(row)
            if contacted is not None:
                row.contacted = contacted
            if contacted_at is not None:
                row.contacted_at = contacted_at
            if replied is not None:
                row.replied = replied
            row.updated_by = actor or "system"
            row.updated_at = datetime.now(UTC)
        return self._clarification_dict(row)

    def _clarification_dict(self, row: ClarificationFamily) -> dict[str, Any]:
        """Render one clarification-family row as a dict.

        Args:
            row: The ORM row.

        Returns:
            The state dict.
        """
        return {
            "family_id": row.family_id,
            "contacted": row.contacted,
            "contacted_at": row.contacted_at,
            "replied": row.replied,
            "source": row.source,
            "updated_by": row.updated_by,
            "updated_at": _iso(row.updated_at),
        }

    async def save_draft(
        self,
        family_id: int,
        subject: str | None,
        greeting: str | None,
        intro: str | None,
        closing: str | None,
        paragraphs: list[dict[str, Any]] | None,
        actor: str | None = None,
        generated_by: str | None = None,
        legacy_positional: bool = False,
    ) -> dict[str, Any]:
        """Save one new draft version for a family.

        Args:
            family_id: The family's key.
            subject: Draft subject.
            greeting: Draft greeting.
            intro: Draft intro.
            closing: Draft closing.
            paragraphs: Keyed paragraph list (``zite_record_id``,
                ``angel_id``, ``order``, ``text``).
            actor: Editing user identity.
            generated_by: Generating identity for AI drafts.
            legacy_positional: Marks imported drafts that predate the
                keyed-paragraph format.

        Returns:
            The persisted draft dict with its new version.
        """
        async with self._transaction():
            version = (
                await self._session.scalar(
                    select(func.max(ClarificationDraft.version)).where(
                        ClarificationDraft.season_id == self._season_id,
                        ClarificationDraft.family_id == family_id,
                    )
                )
                or 0
            ) + 1
            draft = ClarificationDraft(
                season_id=self._season_id,
                family_id=family_id,
                version=version,
                subject=subject,
                greeting=greeting,
                intro=intro,
                closing=closing,
                paragraphs=paragraphs,
                legacy_positional=legacy_positional,
                generated_at=datetime.now(UTC),
                generated_by=generated_by,
                edited_by=actor or "system",
                edited_at=datetime.now(UTC),
            )
            self._session.add(draft)
            await self._session.flush()
        return self._draft_dict(draft)

    async def latest_draft(self, family_id: int) -> dict[str, Any] | None:
        """Read a family's latest draft.

        Args:
            family_id: The family's key.

        Returns:
            The highest-version draft dict, or None when none exists.
        """
        draft = await self._session.scalar(
            select(ClarificationDraft)
            .where(
                ClarificationDraft.season_id == self._season_id,
                ClarificationDraft.family_id == family_id,
            )
            .order_by(ClarificationDraft.version.desc())
        )
        return self._draft_dict(draft) if draft is not None else None

    async def draft_at(
        self, family_id: int, version: int
    ) -> dict[str, Any] | None:
        """Read one specific draft version.

        Args:
            family_id: The family's key.
            version: The draft version.

        Returns:
            The draft dict, or None when that version does not exist.
        """
        draft = await self._session.scalar(
            select(ClarificationDraft).where(
                ClarificationDraft.season_id == self._season_id,
                ClarificationDraft.family_id == family_id,
                ClarificationDraft.version == version,
            )
        )
        return self._draft_dict(draft) if draft is not None else None

    def _draft_dict(self, draft: ClarificationDraft) -> dict[str, Any]:
        """Render one draft row as a dict.

        Args:
            draft: The ORM row.

        Returns:
            The draft dict.
        """
        return {
            "id": draft.id,
            "family_id": draft.family_id,
            "version": draft.version,
            "subject": draft.subject,
            "greeting": draft.greeting,
            "intro": draft.intro,
            "closing": draft.closing,
            "paragraphs": draft.paragraphs,
            "legacy_positional": draft.legacy_positional,
            "generated_at": _iso(draft.generated_at),
            "generated_by": draft.generated_by,
            "edited_by": draft.edited_by,
            "edited_at": _iso(draft.edited_at),
        }

    # ------------------------------------------------------------------
    # Generic sheet reads.
    # ------------------------------------------------------------------

    async def sheet_rows(self, sheet: str) -> list[dict[str, Any]]:
        """Read one operational sheet by its legacy name.

        Args:
            sheet: "duplicate_log", "dob_conflict_log",
                "email_validation_log", "address_validation_log", or
                "match_report".

        Returns:
            The sheet's rows; an absent match report reads as empty.

        Raises:
            DbStoreError: For an unknown sheet name.
        """
        if sheet == "duplicate_log":
            return await self.duplicate_log_rows()
        if sheet == "dob_conflict_log":
            return await self.dob_log_rows()
        if sheet == "email_validation_log":
            return await self.email_log_rows()
        if sheet == "address_validation_log":
            return await self.address_log_rows()
        if sheet == "match_report":
            report = await self.match_report_rows()
            return report if report is not None else []
        raise DbStoreError(f"unknown sheet {sheet!r}")

    async def sheet_names(self) -> list[str]:
        """List every stored sheet/collection name.

        Returns:
            The final and diagnostic/log names, plus ``raw_*`` names
            for tables with an active run and ``match_report`` when a
            match run exists.
        """
        names = ["final_child", "final_family", "final_sponsor"]
        for entity in ("child", "family", "sponsor"):
            active = await self._session.scalar(
                select(AcquisitionRun.id).where(
                    AcquisitionRun.season_id == self._season_id,
                    AcquisitionRun.table_name == entity,
                    AcquisitionRun.active.is_(True),
                )
            )
            if active is not None:
                names.append(f"raw_{entity}")
        names.extend(
            [
                "duplicate_log",
                "dob_conflict_log",
                "email_validation_log",
                "address_validation_log",
            ]
        )
        if (
            await self._session.scalar(
                select(MatchRun.id).where(MatchRun.season_id == self._season_id)
            )
        ) is not None:
            names.append("match_report")
        return names


__all__ = [
    "ALLOWED_CHANGES",
    "LEGACY_COLUMN_RENAMES",
    "MAY_CREATE_ROWS",
    "RAW_SNAPSHOT_MODULES",
    "MatchCommitResult",
    "RAW_LEGACY_RENAMES",
    "SeasonStoreRepository",
    "WRITABLE_ENTITIES",
    "ensure_season",
    "ensure_season_row",
]
