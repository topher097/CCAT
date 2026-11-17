"""CCAT gift-giving pipeline and workbook-migration CLI.

The persistent workbook has one editable truth per entity: ``final_sponsor``,
``final_family``, and ``final_child``. ``raw_*`` sheets are replaceable Zite
source snapshots; operational logs are separate process history. No active
submissions, adjudication, or final-projection layers exist.

Commands (all accept ``--config`` where applicable)::

    ccat acquire
    ccat screen [--redo ID] [--test] [--overwrite]
                [--skip-email-check] [--skip-address-check]
    ccat audit [--test] [--restart]
    ccat match [--test] [--overwrite] [--skip-match]
    ccat generate [--test] [--overwrite] [--sponsor ID] [--max-workers N]
    ccat stats [--raw]
    ccat clarify [--generate] [--overwrite]
    ccat clarify-export [--output PATH]
    ccat audit-migrate --from PATH [--force]
    ccat consolidate-migrate --from PATH --output PATH
                             [--force --backup PATH] [--report PATH]

``acquire`` replaces only raw sheets. ``screen`` cleans all raw records in
memory, appends new persistent entities, refreshes global diagnostics and
validation logs, and AI-screens only uncompleted children. ``--redo`` and
``--overwrite`` are deliberate re-evaluation controls, not blanket data
replacement: human edits and module-owned values retain their protections.

``audit`` is the sole interactive entity editor. Full final rows retain
ignored, orphaned, duplicate, and raw-missing entities; output eligibility is
a separate in-memory filter. Match owns assignment fields, while generate
owns ``generated_info_sheet`` after actual PDF completion. Timestamped match
archives preserve previous assignments. Sponsor packets are built by the
generate command after required angel PDFs exist.

``stats --raw`` retains its CLI spelling for compatibility but means ephemeral
locally cleaned/ID-assigned preparation, not persisted submissions and not a
second review of raw source text. Clarification HTML/JSON drafts, contact
state, and CSV exports remain external to workbook consolidation.

``audit-migrate`` bootstraps the old v1 format directly into persistent finals.
``consolidate-migrate`` instead converts full v2 adj stores into a separate
new workbook without changing its input. It validates schemas, row identities,
all fields/controls, logs, reports, and age formulas/caches, then writes a JSON
migration report. Existing output is refused unless ``--force`` is paired with
an explicit readable ``--backup``. Production config cutover is always manual;
rollback restores the original config and immutable input workbook.
"""

import importlib
import logging
import os
from pathlib import Path

import fire

from ccat.config import ConfigError, load_config
from ccat.io import SheetOwnershipError, SheetSchemaError, setup_logging

logger = logging.getLogger(__name__)

#: Default config path for every ``ccat`` command.  The
#: ``DEFAULT_CONFIG`` environment variable overrides it (exported by
#: ``.envrc``); the fallback is the original 2026 season config, so the
#: repo default only changes when the user cuts over (audit arch §14
#: Q1).
DEFAULT_CONFIG = os.environ.get("DEFAULT_CONFIG", "config/ccat_2026.json")


def _typed_errors() -> tuple[type[BaseException], ...]:
    """Collect the typed exceptions modules may raise (§11)."""
    from ccat.acquire import AcquisitionError
    from ccat.audit import AuditError
    from ccat.child_info_generate import InfoSheetError
    from ccat.clarify import ClarifyError
    from ccat.consolidate import ConsolidationError
    from ccat.match import MatchError
    from ccat.preparation import PreparationError
    from ccat.screening import ScreeningError
    from ccat.sponsor_packets import SponsorPacketError
    from ccat.stats import StatsError

    return (
        ConfigError,
        AcquisitionError,
        ScreeningError,
        AuditError,
        ClarifyError,
        MatchError,
        ConsolidationError,
        PreparationError,
        SheetSchemaError,
        InfoSheetError,
        SponsorPacketError,
        StatsError,
        SheetOwnershipError,
    )


def _dispatch(
    name: str, runner: str, config: str = DEFAULT_CONFIG, **kwargs: object
) -> None:
    """Load config, set up logging, and run one pipeline module.

    Typed failures print an actionable message instead of a traceback (§11).

    Args:
        name: Module display name.
        runner: ``module:function`` target executed via fire.
        config: Path to the JSON configuration file.
        kwargs: Module-specific keyword arguments.

    Raises:
        SystemExit: When a module raises one of the typed pipeline errors.
    """
    try:
        cfg = load_config(config)
        setup_logging(cfg.log_file)
        module_name, _, function_name = runner.partition(":")
        module = importlib.import_module(f"ccat.{module_name}")
        function = getattr(module, function_name if function_name else "run")
        logger.info("starting module %s (config=%s)", name, config)
        function(cfg, **kwargs)
        logger.info("finished module %s", name)
    except _typed_errors() as error:
        message = f"ccat {name} failed: {error}"
        logger.error(message)
        raise SystemExit(message) from error


def _acquire_cmd(config: str = DEFAULT_CONFIG) -> None:
    """Run module 1: data acquisition and validation (§5)."""
    _dispatch("acquire", "acquire:run", config)


def _screen_cmd(
    config: str = DEFAULT_CONFIG,
    redo: str | None = None,
    test: bool = False,
    overwrite: bool = False,
    skip_email_check: bool = False,
    skip_address_check: bool = False,
) -> None:
    """Run module 2: pre-audit screening (§6).

    Args:
        config: Path to the JSON configuration file.
        redo: Optional current final-child zite_record_id to re-screen.
        test: Stop after the first full family (first family's children
            only are AI-screened).
        overwrite: Re-screen every current child under the same human-edit
            and module-owned-field protection as redo.
        skip_email_check: Skip the ZeroBounce email deliverability
            validation.
        skip_address_check: Skip the Nominatim family-address validation.
    """
    _dispatch(
        "screen",
        "screening:run",
        config,
        redo=redo,
        test=test,
        overwrite=overwrite,
        skip_email_check=skip_email_check,
        skip_address_check=skip_address_check,
    )


def _audit_cmd(
    config: str = DEFAULT_CONFIG,
    test: bool = False,
    restart: bool = False,
) -> None:
    """Run module 3: local human audit web tool (§7 audit architecture).

    Args:
        config: Path to the JSON configuration file.
        test: Limit every list to the first family (lowest family_id),
            its children, and the first sponsor; the dashboard reflects
            the reduced scope.
        restart: Stop any currently running audit tool process before
            starting.
    """
    _dispatch(
        "audit",
        "audit.server:run",
        config,
        test=test,
        restart=restart,
    )


def _audit_migrate_cmd(config: str = DEFAULT_CONFIG, **kwargs: object) -> None:
    """Bootstrap the v2 workbook from the old one (§11 audit arch).

    Args:
        config: Path to the **new** JSON configuration file (typically
            ``config/ccat_2026_v2.json``).
        kwargs: ``from`` (passed as ``--from``; the old workbook,
            opened read-only) and the optional ``force`` flag that
            re-creates persistent final sheets in a non-empty target.

    Raises:
        SystemExit: On unknown flags.
    """
    from_path = kwargs.pop("from", None)
    if from_path is None:
        from_path = kwargs.pop("from_path", None)
    force = bool(kwargs.pop("force", False))
    if kwargs:
        raise SystemExit(f"unknown flags: {sorted(kwargs)}")
    if not from_path:
        raise SystemExit(
            "audit-migrate requires --from PATH (the old workbook)"
        )
    _dispatch(
        "audit-migrate",
        "audit.migrate:run",
        config,
        from_path=str(from_path),
        force=force,
    )


def _consolidate_migrate_cmd(
    config: str = DEFAULT_CONFIG, **kwargs: object
) -> None:
    """Convert full legacy adj stores to a separate persistent-final workbook.

    Args:
        config: Season configuration, including Christmas formula context.
        kwargs: Required ``from`` and ``output`` paths; optional ``force``,
            required-existing ``backup`` when forcing, and ``report`` path.

    Raises:
        SystemExit: If required paths are absent or flags are unknown.
    """
    from_path = kwargs.pop("from", kwargs.pop("from_path", None))
    output_path = kwargs.pop("output", kwargs.pop("output_path", None))
    force = bool(kwargs.pop("force", False))
    backup_path = kwargs.pop("backup", kwargs.pop("backup_path", None))
    report_path = kwargs.pop("report", kwargs.pop("report_path", None))
    if kwargs:
        raise SystemExit(f"unknown flags: {sorted(kwargs)}")
    if not from_path or not output_path:
        raise SystemExit(
            "consolidate-migrate requires --from PATH and --output PATH"
        )
    _dispatch(
        "consolidate-migrate",
        "consolidate:run",
        config,
        from_path=str(from_path),
        output_path=str(output_path),
        force=force,
        backup_path=str(backup_path) if backup_path else None,
        report_path=str(report_path) if report_path else None,
    )


def _match_cmd(
    config: str = DEFAULT_CONFIG,
    test: bool = False,
    overwrite: bool = False,
    skip_match: bool = False,
) -> None:
    """Run module 4: matchmaking (§8).

    Args:
        config: Path to the JSON configuration file.
        test: Stop after the first full sponsor is matched.
        overwrite: Replace previous final_family/final_sponsor/match_report
            in place instead of archiving them. The audited-child
            participation gate still applies.
        skip_match: Reconcile assignment-owned columns in the persistent
            family/sponsor stores without running matching. Existing
            assignments carry over; prior_assigned_angel_ids propagates to
            assigned_angel_ids and points the angel's family at that sponsor.
            Duplicate angel ids across sponsors raise before writes.
            match_report is untouched.
            Without skip_match the §8.5 reconciliation stays
            authoritative; prior angels only fill in families the
            matching left unassigned.
    """
    _dispatch(
        "match",
        "match:run",
        config,
        test=test,
        overwrite=overwrite,
        skip_match=skip_match,
    )


def _generate_cmd(
    config: str = DEFAULT_CONFIG,
    test: bool = False,
    overwrite: bool = False,
    sponsor: int | None = None,
    max_workers: int | None = None,
) -> None:
    """Run module 5 + 5b: info sheets, then sponsor packets (§13, §13b).

    Args:
        config: Path to the JSON configuration file.
        test: Stop after the first info sheet PDF is generated (no
            sponsor packets).
        overwrite: Regenerate every child's info sheet, ignoring the
            generated_info_sheet column, and rebuild the packets.
        sponsor: Restrict the run to one sponsor id: the sheet phase
            renders only that sponsor's pending children and only that
            sponsor's packet is built.
        max_workers: Override ``info_sheet.max_workers``: how many
            children render and convert concurrently (Ctrl+C cancels
            cleanly; finished sheets are recorded).
    """
    _dispatch(
        "generate",
        "child_info_generate:run",
        config,
        test=test,
        overwrite=overwrite,
        sponsor=sponsor,
        max_workers=max_workers,
    )


def _stats_cmd(config: str = DEFAULT_CONFIG, raw: bool = False) -> None:
    """Run module 6: statistics report generation.

    Args:
        config: Path to the JSON configuration file.
        raw: Build from ephemeral locally cleaned/ID-assigned preparation
            instead of the eligible persistent-final population. No prepared
            submission sheets are persisted.
    """
    _dispatch("stats", "stats:run", config, raw=raw)


def _clarify_cmd(
    config: str = DEFAULT_CONFIG,
    generate: bool = False,
    overwrite: bool = False,
) -> None:
    """Run module 7: clarification email web app or headless generation.

    Args:
        config: Path to the JSON configuration file.
        generate: Headless mode (no server): generate an email draft
            for every family that does not have one yet, then exit.
        overwrite: With ``generate``, regenerate even families that
            already have a saved draft; without ``generate`` this
            flag is ignored and the server starts normally.
    """
    _dispatch(
        "clarify",
        "clarify.server:run",
        config,
        generate=generate,
        overwrite=overwrite,
    )


def _clarify_export_cmd(
    config: str = DEFAULT_CONFIG, output: str | None = None
) -> None:
    """Run module 7 (headless): refresh the clarifications CSV.

    Args:
        config: Path to the JSON configuration file.
        output: Output CSV path; defaults to
            ``clarify.clarifications_csv``.
    """
    _dispatch(
        "clarify-export",
        "clarify.extract:write_csv",
        config,
        output=Path(output) if output else None,
    )


def main() -> None:
    """Entry point for the ``ccat`` CLI."""
    fire.Fire(
        {
            "acquire": _acquire_cmd,
            "screen": _screen_cmd,
            "audit": _audit_cmd,
            "audit-migrate": _audit_migrate_cmd,
            "consolidate-migrate": _consolidate_migrate_cmd,
            "match": _match_cmd,
            "generate": _generate_cmd,
            "stats": _stats_cmd,
            "clarify": _clarify_cmd,
            "clarify-export": _clarify_export_cmd,
        }
    )


__all__ = [
    "ConfigError",
    "SheetOwnershipError",
    "main",
]
