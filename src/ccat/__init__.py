"""CCAT gift-giving matching system.

Main dispatch for the ``ccat`` CLI (pipeline_architecture.md §2): five
modules runnable independently, each taking an optional ``--config`` path
(default ``config/ccat_2026.json``).

    ccat acquire  [--config PATH]
    ccat screen   [--config PATH] [--redo ZITE_RECORD_ID] [--test]
                  [--overwrite] [--skip-email-check] [--skip-address-check]
    ccat audit    [--config PATH] [--test] [--restart]
    ccat match    [--config PATH] [--test] [--overwrite] [--skip-match]
    ccat generate [--config PATH] [--test] [--overwrite] [--sponsor ID]
                  [--max-workers N]
    ccat stats    [--config PATH] [--raw]
    ccat clarify  [--config PATH] [--generate] [--overwrite]
    ccat clarify-export [--config PATH] [--output PATH]
    ccat audit-migrate [--config PATH] --from PATH [--force]

``ccat acquire`` only fetches: it pulls the sponsor/family/child tables
from Zite and writes them verbatim to the ``raw_*`` sheets -- no
cleaning, no validation, no deduplication.  ``ccat screen`` prepares
everything downstream needs from those verbatim records: it cleans the
trivial formatting locally (capitalization, phone/email/date formats --
never the AI), flags format/enum problems instead of dropping rows,
assigns the stable IDs, writes the cleaned ``*_submissions`` rows,
**flags potential duplicates** (families by primary email, children by
email + first name, sponsors by email -- never merged; the user resolves
them in the audit tool), logs sibling DOB conflicts, validates every new
sponsor/family email with ZeroBounce and every family address with
Nominatim (both full API responses logged), and then AI-screens the
children only (the prompt carries no personal data).

The ``--test`` flag (screen/audit/match/generate) stops the run after the
first full family (screening: only the first family's children are
screened; audit: the lists are limited to the first family, its
children, and the first sponsor; match: only the first sponsor in phase
order is matched; generate: only the first info sheet PDF is generated).

The audit tool (module 3) is a multi-page NiceGUI app: a dashboard with
sponsor/family stats, sponsor and family lists with status filters, and
detail pages where every adjudicable field is edited against the raw
submissions side by side.  ``audited`` is a revocable checkbox and the
status (new / in_progress / needs_clarification / ignored / audited) is
recomputed on every save; ignored rows are reduced to their identity
skeleton and disappear from the final sheets.  ``--restart`` stops a
stale audit tool process before starting.

``ccat audit-migrate`` bootstraps the v2 workbook
(``output/ccat_2026_v2.xlsx`` via ``config/ccat_2026_v2.json``) from the
old one given by ``--from``: the raw layer is copied verbatim (the old
workbook is never written), the dense adj sheets are built from the old
sparse adj_child plus the old final rows, and all three finals are
projected so downstream modules work immediately.  It refuses to run
when the target already contains adj/final sheets unless ``--force``
is given.

``ccat match --skip-match`` skips the automatic assignment algorithm
and only reconciles ``final_family`` with the current ``raw_family``
(previously assigned sponsors carry over; new families get rows with
no sponsor) so downstream modules see every current family.

``ccat generate`` renders the child information sheet PDFs, then —
once every sponsored angel's sheet exists — builds one sponsor packet
directory per assigned sponsor under ``sponsor_packets.output_dir``
(default ``output/sponsor_packets``): an HTML email draft
(personalized greeting plus a table of angel IDs, genders, ages, and
drop-off locations) and the combined child information PDF, plus a
copy of the sponsor instruction file.  Open the HTML in a browser and
copy it into your email client for manual sending.

``ccat stats`` renders a dated statistics report (daily admissions per
pickup site, per-location table, gender/age distributions) as HTML and
PDF into ``report.output_dir`` (default ``output/reports``).  With
``--raw`` it reads ``raw_child``/``raw_family`` instead of the final
sheets, so the report works before audit/match has run; sponsor counts
are zero in that mode (``raw_family`` has no ``assigned_sponsor_id``).

``ccat clarify`` serves a local NiceGUI web app with one card per
family whose Angels carry a ``require_clarification`` note
(module 7).  A per-family button generates a polite clarification
email to the family's primary email — prose written by the configured
LLM via the opencode SDK, wrapped in a paste-safe HTML template —
shown in an editable copy dialog and saved under ``clarify.output_dir``
(``family_<id>.html`` plus its editable ``family_<id>.json``
companion).  Each card also tracks contact status ("Mark contacted"
stamps the time, a "Reply received" switch records the answer),
persisted in a JSON sidecar and merged into the CSV's ``contacted``,
``contacted_at``, and ``replied`` columns.  The CSV copy
(``clarify.clarifications_csv``) is refreshed on start and after
every save/status change; its ``clarification_email_file`` column
references each family's saved draft.  ``ccat clarify --generate``
skips the server entirely and headlessly generates every missing
family's draft; adding ``--overwrite`` regenerates existing drafts
too.  ``--overwrite`` without ``--generate`` does nothing (the
server starts normally).  ``ccat clarify-export`` only refreshes the
CSV (no server, no generation), the successor of the old
``scripts/find_clarification.py``.
"""

import importlib
import logging
import os
from pathlib import Path

import fire

from ccat.config import ConfigError, load_config
from ccat.io import SheetOwnershipError, setup_logging

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
    from ccat.match import MatchError
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
        redo: Optional zite_record_id to re-screen, replacing its adj row.
        test: Stop after the first full family (first family's children
            only are AI-screened).
        overwrite: Re-screen every child, replacing all existing
            adj_child rows.
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
            re-creates adj/final sheets in a non-empty target.

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
        overwrite: Ignore the audited column and replace previous
            final_family/final_sponsor/match_report in place instead of
            archiving them.
        skip_match: Reconcile final_family and final_sponsor with the
            current raw rows without running the matching algorithm: every
            family and sponsor gets a row, existing assignments carry
            over, new ones get none.  prior_assigned_angel_ids propagates
            to assigned_angel_ids and points the angel's family at that
            sponsor in final_family; duplicate angel ids across sponsors
            raise an error.  match_report is untouched.
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
        raw: Build the report from the raw sheets instead of the final
            sheets.
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
