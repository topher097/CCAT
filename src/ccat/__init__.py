"""CCAT gift-giving matching system.

Main dispatch for the ``ccat`` CLI (pipeline_architecture.md §2): five
modules runnable independently, each taking an optional ``--config`` path
(default ``config/ccat_2026.json``).

    ccat acquire  [--config PATH]
    ccat screen   [--config PATH] [--redo ZITE_RECORD_ID] [--test]
                  [--overwrite]
    ccat audit    [--config PATH] [--test] [--overwrite]
    ccat match    [--config PATH] [--test] [--overwrite]
    ccat generate [--config PATH] [--test] [--overwrite]
    ccat stats    [--config PATH]

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

``ccat stats`` renders a dated statistics report (daily admissions per
pickup site, per-location table, gender/age distributions) as HTML and
PDF into ``report.output_dir`` (default ``output/reports``).
"""

import importlib
import logging

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
    from ccat.match import MatchError
    from ccat.screening import ScreeningError
    from ccat.stats import StatsError

    return (
        ConfigError,
        AcquisitionError,
        ScreeningError,
        AuditError,
        MatchError,
        InfoSheetError,
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
) -> None:
    """Run module 3: local human audit web tool (§7).

    Args:
        config: Path to the JSON configuration file.
        test: Stop after the first full family is audited.
        overwrite: Ignore the audited column: audited children re-enter
            the queue and frozen rows may be re-saved.
    """
    _dispatch(
        "audit", "audit.server:run", config, test=test, overwrite=overwrite
    )


def _match_cmd(
    config: str = DEFAULT_CONFIG,
    test: bool = False,
    overwrite: bool = False,
) -> None:
    """Run module 4: matchmaking (§8).

    Args:
        config: Path to the JSON configuration file.
        test: Stop after the first full sponsor is matched.
        overwrite: Ignore the audited column and replace previous
            final_family/final_sponsor/match_report in place instead of
            archiving them.
    """
    _dispatch("match", "match:run", config, test=test, overwrite=overwrite)


def _generate_cmd(
    config: str = DEFAULT_CONFIG,
    test: bool = False,
    overwrite: bool = False,
) -> None:
    """Run module 5: child info sheet generation (§13).

    Args:
        config: Path to the JSON configuration file.
        test: Stop after the first info sheet PDF is generated.
        overwrite: Regenerate every child's info sheet, ignoring the
            generated_info_sheet column.
    """
    _dispatch(
        "generate",
        "child_info_generate:run",
        config,
        test=test,
        overwrite=overwrite,
    )


def _stats_cmd(config: str = DEFAULT_CONFIG) -> None:
    """Run module 6: statistics report generation.

    Args:
        config: Path to the JSON configuration file.
    """
    _dispatch("stats", "stats:run", config)


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
        }
    )


__all__ = [
    "ConfigError",
    "SheetOwnershipError",
    "main",
]
