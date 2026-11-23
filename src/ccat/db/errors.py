"""Typed errors for the DB-era season store (plan §5.1, §4.4).

Every repository failure a caller can act on carries its own class so
Phase 3 services/API can map them to precise HTTP responses (409/412
stale versions, 422 key problems, 403 ownership) instead of parsing
message text.  All share :class:`DbStoreError` as the common root.
"""

from typing import Any


class DbStoreError(Exception):
    """Base class for every season-store persistence failure."""


class MigrationStateError(DbStoreError):
    """The database is at a revision this build does not contain.

    Raised when the stored schema and the build's migration history are
    out of step: the database was migrated by another build, and Alembic
    cannot even start because it cannot resolve the stored revision.
    """


class StaleRowVersionError(DbStoreError):
    """A submitted row version no longer matches the stored row.

    Raised when ``commit_finals`` receives an expected ``row_version``
    that differs from storage (plan §4.4: the stale-editor case — B
    must re-fetch, review A's changes, and re-submit).
    """

    def __init__(self, entity: str, key: Any, current_version: int) -> None:
        """Initialize the error.

        Args:
            entity: Entity name ("child", "family", "sponsor").
            key: The row's entity key as submitted.
            current_version: The row's actual stored version.
        """
        self.entity = entity
        self.key = key
        self.current_version = current_version
        super().__init__(
            f"{entity} {key!r} is at row_version {current_version}, not"
            " the expected version; re-fetch the row and rebase"
        )


class EntityRemovalError(DbStoreError):
    """A complete-store commit would drop persisted entity keys.

    Final stores are append-and-merge (§1.2 rule 2): ignored, orphaned,
    duplicate, and raw-missing entities are never removed.
    """

    def __init__(self, entity: str, keys: list[str]) -> None:
        """Initialize the error.

        Args:
            entity: Entity name.
            keys: The stored keys missing from the submitted rows.
        """
        self.entity = entity
        self.keys = keys
        super().__init__(
            f"commit would remove persisted {entity} key(s): {keys};"
            " final stores are append-and-merge, not projections"
        )


class OwnershipError(DbStoreError):
    """A module tried to write outside its owned columns or entities.

    Mirrors ``ccat.io.WRITABLE_SHEETS`` plus the column-level rules
    from plan §5.1, enforced in executable code at the repository.
    """

    def __init__(
        self,
        module: str,
        entity: str,
        key: Any = None,
        columns: tuple[str, ...] = (),
    ) -> None:
        """Initialize the error.

        Args:
            module: The writing module identity.
            entity: Entity name.
            key: The offending row's key, when a row triggered this.
            columns: The non-owned columns that changed, when known.
        """
        self.module = module
        self.entity = entity
        self.key = key
        self.columns = columns
        detail = f" on {entity} key {key!r}" if key is not None else ""
        super().__init__(
            f"module {module!r} may not change {entity} columns"
            f" {sorted(columns)}{detail}; see the ownership matrix"
            " (io.WRITABLE_SHEETS, plan §5.1)"
        )


class KeyValidationError(DbStoreError):
    """A submitted row set has invalid entity keys or unknown columns.

    Mirrors the workbook guards: null/blank/duplicate keys, boolean or
    non-integral family/sponsor stable IDs, and columns outside the
    persistent-final schema are rejected before any write.
    """

    def __init__(self, entity: str, message: str) -> None:
        """Initialize the error.

        Args:
            entity: Entity or table name the bad rows belong to.
            message: What is wrong with the key or column.
        """
        self.entity = entity
        super().__init__(f"invalid {entity} rows: {message}")


__all__ = [
    "DbStoreError",
    "EntityRemovalError",
    "KeyValidationError",
    "OwnershipError",
    "StaleRowVersionError",
]
