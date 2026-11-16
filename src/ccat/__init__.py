"""CCAT gift-giving matching system.

Main dispatch for the ``ccat`` CLI (pipeline_architecture.md §2): five
modules runnable independently, each taking an optional ``--config`` path
(default ``config/ccat_2026.json``).

    ccat acquire  [--config PATH]
    ccat screen   [--config PATH] [--redo ZITE_RECORD_ID] [--test]
                  [--overwrite]
    ccat audit    [--config PATH] [--test] [--overwrite] [--restart]
                  [--hide-completed]
    ccat match    [--config PATH] [--test] [--overwrite] [--skip-match]
    ccat generate [--config PATH] [--test] [--overwrite]
    ccat stats    [--config PATH] [--raw]
    ccat clarify  [--config PATH] [--generate] [--overwrite]
    ccat clarify-export [--config PATH] [--output PATH]

The ``--test`` flag (screen/audit/match/generate) stops the run after the
first full family (screening: only the first family's children are
screened; audit: the queue is limited to the first family and the server
stops once it is fully audited; match: only the first sponsor in phase
order is matched; generate: only the first info sheet PDF is generated).

The ``--overwrite`` flag (screen/audit/match/generate) ignores previously
generated output: screening replaces every ``adj_child`` row; the audit
tool re-queues audited children and allows re-saving frozen rows;
matchmaking ignores the ``audited`` column and replaces its outputs in
place instead of archiving; info sheet generation re-renders every PDF
regardless of the ``generated_info_sheet`` column.

The audit tool's ``--restart`` flag stops any currently running audit
tool process (e.g. a stale server still holding the port) before
starting.  With ``--hide-completed`` the queue shows only children
that have no ``final_child`` row at all (children saved earlier —
audited or needing clarification — are hidden).

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
from pathlib import Path

import fire

from ccat.config import ConfigError, load_config
from ccat.io import SheetOwnershipError, setup_logging

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = "config/ccat_2026.json"


def _typed_errors() -> tuple[type[BaseException], ...]:
    """Collect the typed exceptions modules may raise (§11)."""
    from ccat.acquire import AcquisitionError
    from ccat.audit.server import AuditError
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
) -> None:
    """Run module 2: AI screening (§6).

    Args:
        config: Path to the JSON configuration file.
        redo: Optional zite_record_id to re-screen, replacing its adj row.
        test: Stop after the first full family (first family's children
            only).
        overwrite: Re-screen every child, replacing all existing
            adj_child rows.
    """
    _dispatch(
        "screen",
        "screening:run",
        config,
        redo=redo,
        test=test,
        overwrite=overwrite,
    )


def _audit_cmd(
    config: str = DEFAULT_CONFIG,
    test: bool = False,
    overwrite: bool = False,
    restart: bool = False,
    hide_completed: bool = False,
) -> None:
    """Run module 3: local human audit web tool (§7).

    Args:
        config: Path to the JSON configuration file.
        test: Stop after the first full family is audited.
        overwrite: Ignore the audited column: audited children re-enter
            the queue and frozen rows may be re-saved.
        restart: Stop any currently running audit tool process before
            starting.
        hide_completed: Queue only children with no final_child row at
            all; children saved earlier are hidden.
    """
    _dispatch(
        "audit",
        "audit.server:run",
        config,
        test=test,
        overwrite=overwrite,
        restart=restart,
        hide_completed=hide_completed,
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
