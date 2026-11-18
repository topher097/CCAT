"""Season storage port (refactor §4, §5.1).

Two implementations are planned for this protocol:

- **Workbook legacy path (today):** ``ccat.io`` sheet I/O plus the
  read/validate/write helpers in ``ccat.audit.store`` and the
  per-module writers in ``ccat.screening``/``ccat.match``/``ccat.
  child_info_generate``. The Phase 1b services call those legacy
  modules directly; no workbook implementation of this protocol
  exists on purpose — wrapping the workbook would duplicate the
  active path.
- **DB repositories (Phase 2):** SQLAlchemy/Alembic repositories with
  season-scoped keys, ``row_version`` optimistic concurrency, actor
  attribution, and ``ai_screening_notes`` naming. They implement this
  protocol so both storage modes run the same services (decision D12).

Phase 1b ships this module as documentation and type surface only.
Method semantics follow the plan's ownership rules: ``commit_finals``
validates the module's write rights (§5.1 table), diagnostics are
current snapshots (replace) while external-validation logs append,
and match commits assignments, report, and archive atomically (D8).
"""

from typing import Any, Protocol


class SeasonStore(Protocol):
    """Read/write access to one season's entities, logs, and reports.

    Keys are season-scoped: child ``(season_id, zite_record_id)``,
    family ``(season_id, family_id)``, sponsor ``(season_id,
    sponsor_id)``. Rows are complete normalized dictionaries with the
    full persistent-final schema; readers never filter out ignored,
    orphaned, duplicate, or raw-missing entities (§1.2 rule 2).
    """

    def sheet_names(self) -> list[str]:
        """Return every stored sheet/collection name.

        Used for sheet-presence checks (e.g. match-report existence
        and legacy archive detection).
        """
        ...

    def raw_rows(self, entity: str) -> list[dict[str, Any]]:
        """Return the current raw snapshot rows for one entity.

        Args:
            entity: "sponsor", "family", or "child".

        Returns:
            Verbatim raw rows; the active snapshot, not history.
        """
        ...

    def final_rows(self, entity: str) -> list[dict[str, Any]]:
        """Return every persistent final row for one entity.

        Args:
            entity: "sponsor", "family", or "child".

        Returns:
            Complete rows including ignored/orphan/raw-missing ones.
        """
        ...

    def commit_finals(
        self,
        module: str,
        entity: str,
        rows: list[dict[str, Any]],
        *,
        actor: str | None = None,
    ) -> None:
        """Atomically write one complete final store.

        Args:
            module: Writing module identity ("audit", "screen",
                "match", "generate"); the implementation enforces
                each module's field-ownership rights.
            entity: "sponsor", "family", or "child".
            rows: The complete row set — never a partial upsert that
                could drop stored entities.
            actor: Requesting user/job identity for attribution.

        Raises:
            PermissionError: When ``module`` may not write ``entity``.
        """
        ...

    def sheet_rows(self, name: str) -> list[dict[str, Any]]:
        """Return rows of one operational sheet by name.

        Covers diagnostics snapshots, validation logs, and the match
        report — the sheet-name-keyed collections outside the three
        final stores.

        Args:
            name: Sheet name, e.g. "duplicate_log",
                "dob_conflict_log", "email_validation_log",
                "match_report".

        Returns:
            The sheet's rows, or an empty list when absent.
        """
        ...

    def replace_diagnostics(
        self, module: str, name: str, rows: list[dict[str, Any]]
    ) -> None:
        """Replace one current-diagnostic snapshot wholesale.

        Duplicate and DOB-conflict logs are current snapshots, not
        append-only history (§4.1): each screening run replaces them.

        Args:
            module: Writing module identity ("screen").
            name: "duplicate_log" or "dob_conflict_log".
            rows: The complete new snapshot.
        """
        ...

    def append_validation_log(
        self, module: str, name: str, rows: list[dict[str, Any]]
    ) -> None:
        """Append external-validation results (email/address logs).

        Provider results are retained with their replay keys and full
        responses; failures stay retryable under the existing
        validation-log semantics (§6.4).

        Args:
            module: Writing module identity ("screen").
            name: "email_validation_log" or
                "address_validation_log".
            rows: The new log rows to append.
        """
        ...

    def commit_match(
        self,
        module: str,
        family_rows: list[dict[str, Any]],
        sponsor_rows: list[dict[str, Any]],
        report_rows: list[dict[str, Any]] | None,
        *,
        archive: bool,
        actor: str | None = None,
    ) -> None:
        """Commit one match run's assignments, report, and archive.

        Only family/sponsor assignment-owned fields change; every
        other column of the merged rows comes from storage (§9.4).
        ``report_rows=None`` is ``--skip-match``: assignments
        reconcile but the current match report stays untouched.

        Args:
            module: Writing module identity ("match").
            family_rows: Complete family rows with patched
                assignment fields.
            sponsor_rows: Complete sponsor rows with patched
                assignment fields.
            report_rows: The new match-report rows, or None.
            archive: Archive the previous complete outputs as a match
                run snapshot instead of replacing in place.
            actor: Requesting user/job identity for attribution.
        """
        ...


__all__ = ["SeasonStore"]
