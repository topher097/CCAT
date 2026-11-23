"""Typed result DTOs for the Phase 1b service layer (refactor §5.1).

Every service function returns one of these pydantic models instead of
``None``/log-only output, so the CLI (unchanged legacy behavior) and the
future API/jobs layers (Phase 3) share one typed contract. All models
reject unknown fields: callers must not invent result columns.

Counts computed post-run from the workbook are documented as
approximations where the legacy entry point does not track them; see
:mod:`ccat.services.pipeline` for the exact approximations.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StrictResult(BaseModel):
    """Base result model: unknown fields are forbidden."""

    model_config = ConfigDict(extra="forbid")


class EntityCounts(StrictResult):
    """Reconciliation row-count changes per entity.

    Attributes:
        appended: Entity name -> count of new final rows.
        merged: Entity name -> count of changed pre-existing rows.
    """

    appended: dict[str, int] = Field(default_factory=dict)
    merged: dict[str, int] = Field(default_factory=dict)


class AcquireResult(StrictResult):
    """Result of one acquisition run.

    Attributes:
        raw_rows: Raw table name ("sponsor"/"family"/"child") -> row
            count written verbatim to ``raw_<table>``.
        workbook: Path of the workbook that was written (empty in DB
            mode, where acquire writes no workbook).
    """

    raw_rows: dict[str, int] = Field(default_factory=dict)
    workbook: str = ""


class ScreenResult(StrictResult):
    """Result of one screening run.

    Attributes:
        counts: Appended/merged final-row changes per entity.
        email_checked: New email-validation log rows this run.
        address_checked: New address-validation log rows this run.
        ai_targets: Child ids selected for AI screening (approximate;
            see :func:`ccat.services.pipeline.screen`).
        ai_succeeded: Target child ids with ``screened`` true after
            the run.
        ai_failed: Target child ids left unscreened.
        diagnostics_rows: duplicate_log + dob_conflict_log row count
            after the run (current snapshots, not per-run deltas).
    """

    counts: EntityCounts = Field(default_factory=EntityCounts)
    email_checked: int = 0
    address_checked: int = 0
    ai_targets: list[str] = Field(default_factory=list)
    ai_succeeded: list[str] = Field(default_factory=list)
    ai_failed: list[str] = Field(default_factory=list)
    diagnostics_rows: int = 0


class MatchResult(StrictResult):
    """Result of one match run.

    Attributes:
        matched_families: Non-ignored families with an assigned
            sponsor after the run.
        matched_sponsors: Non-ignored sponsors with assigned angels
            after the run.
        assigned_angel_ids: Every angel id across sponsor assignments,
            in natural angel order (``1A < 1B < 1Z < 1AA``).
        unmatched_family_ids: Non-ignored families left unassigned.
        split_family_ids: Non-ignored families whose assigned children
            span two or more sponsors.
        unmatched_sponsor_ids: Non-ignored sponsors without angels.
        report_rows: match_report sheet row count (0 when absent).
        archived: True when this run created timestamped archive
            sheets.
        csv_path: Configured match-report CSV path (DB mode: the
            tracked artifact's path).
        run_id: The committed match run's id (DB mode only).
        csv_export_ok: False when the DB-mode CSV artifact failed after
            the assignment commit (explicit partial state, plan §9.4).
    """

    matched_families: int = 0
    matched_sponsors: int = 0
    assigned_angel_ids: list[str] = Field(default_factory=list)
    unmatched_family_ids: list[int] = Field(default_factory=list)
    split_family_ids: list[int] = Field(default_factory=list)
    unmatched_sponsor_ids: list[int] = Field(default_factory=list)
    report_rows: int = 0
    archived: bool = False
    csv_path: str = ""
    run_id: int | None = None
    csv_export_ok: bool = True


class GenerateResult(StrictResult):
    """Result of one generation run (sheets, then packets).

    Attributes:
        sheets_generated: Stems of sheets whose
            ``generated_info_sheet`` flag turned true this run.
        sheets_failed: Stems that failed (empty on the success path;
            the legacy run raises :class:`ccat.child_info_generate.
            InfoSheetError` after failures instead of returning).
        sheets_skipped: Stems of sponsored children without a newly
            generated sheet (already generated or left pending).
        packets_built: Sponsors whose packet became complete this run.
        packets_skipped: Sponsors whose packet was already complete
            before and after the run (approximate; see :func:`ccat.
            services.pipeline.generate`).
        packets_blocked: Sponsors a packets-only run skipped because a
            child still has no info sheet, as
            ``{"sponsor_id": int, "angel_ids": [...]}``.
        info_sheet_dir: Info-sheet output directory.
        packets_dir: Sponsor-packet output directory.
    """

    sheets_generated: list[str] = Field(default_factory=list)
    sheets_failed: list[str] = Field(default_factory=list)
    sheets_skipped: list[str] = Field(default_factory=list)
    packets_built: int = 0
    packets_skipped: int = 0
    packets_blocked: list[dict[str, Any]] = Field(default_factory=list)
    info_sheet_dir: str = ""
    packets_dir: str = ""


class StatsResult(StrictResult):
    """Result of one statistics-report run.

    Attributes:
        html_path: Written HTML report path (None when absent).
        pdf_path: Written PDF report path (None when absent).
        html_written: True when the HTML file exists after the run.
        pdf_written: True when the PDF file exists after the run.
    """

    html_path: str | None = None
    pdf_path: str | None = None
    html_written: bool = False
    pdf_written: bool = False


class ClarifyGenerateResult(StrictResult):
    """Result of headless clarification-draft generation.

    Attributes:
        generated_family_ids: Families whose draft was written.
        failed_family_ids: Families whose generation failed.
    """

    generated_family_ids: list[int] = Field(default_factory=list)
    failed_family_ids: list[int] = Field(default_factory=list)


class ClarifyExportResult(StrictResult):
    """Result of one clarifications-CSV export.

    Attributes:
        csv_path: The CSV path written.
        rows: Row count of the written CSV (including the header-free
            data rows only).
    """

    csv_path: str = ""
    rows: int = 0


class ReconcileResult(StrictResult):
    """Result of one audit-startup reconciliation.

    Attributes:
        counts: Appended/merged final-row changes per entity.
    """

    counts: EntityCounts = Field(default_factory=EntityCounts)


class SaveResult(StrictResult):
    """Result of one audit save action.

    Attributes:
        entity: "child", "family", or "sponsor".
        key: The entity key the save applied to.
        status: The persisted status after the save.
        applied: The fields this action supplied (field -> value);
            ignore/unignore actions report ``{"ignore": bool}``.
        audited_at: The audit timestamp stamped by the save.
    """

    entity: str
    key: str | int
    status: str
    applied: dict[str, Any] = Field(default_factory=dict)
    audited_at: str | None = None


__all__ = [
    "AcquireResult",
    "ClarifyExportResult",
    "ClarifyGenerateResult",
    "EntityCounts",
    "GenerateResult",
    "MatchResult",
    "ReconcileResult",
    "SaveResult",
    "ScreenResult",
    "StatsResult",
]
