"""Thin pipeline service wrappers over the legacy entry points (§5.1).

Each wrapper runs the unchanged legacy ``run`` function and then
produces a typed result DTO by reading the workbook/artifacts after
the run. Typed errors from the legacy modules propagate unchanged;
the CLI keeps calling the legacy entry points directly and is not
modified. These wrappers exist so the future API/job layer (Phase 3)
calls typed services instead of module functions.

Counting is deliberately post-hoc: exact per-run counters would mean
instrumenting the legacy internals, which Phase 1b forbids. The
docstrings below name every approximation.
"""

from pathlib import Path
from typing import Any

import polars as pl

from ccat import acquire as acquire_module
from ccat import child_info_generate as generate_module
from ccat import match as match_module
from ccat import screening as screening_module
from ccat import stats as stats_module
from ccat.audit.store import as_bool, eligible_output_rows, read_final
from ccat.clarify.email_gen import EmailSender, group_families
from ccat.clarify.extract import build_table, write_csv
from ccat.config import Config
from ccat.io import list_sheets, read_sheet
from ccat.services.audit import entity_count_changes
from ccat.services.results import (
    AcquireResult,
    ClarifyExportResult,
    ClarifyGenerateResult,
    GenerateResult,
    MatchResult,
    ScreenResult,
    StatsResult,
)

_ENTITIES = ("sponsor", "family", "child")
_LOG_SHEETS = ("email_validation_log", "address_validation_log")


def acquire(cfg: Config) -> AcquireResult:
    """Run acquisition and count the verbatim raw rows written.

    Args:
        cfg: Pipeline configuration.

    Returns:
        Row counts per raw table plus the workbook path.

    Raises:
        AcquisitionError: As raised by the legacy run.
    """
    acquire_module.run(cfg)
    raw_rows = {
        table: _sheet_height(cfg.excel_file, f"raw_{table}")
        for table in _ENTITIES
    }
    return AcquireResult(raw_rows=raw_rows, workbook=str(cfg.excel_file))


def screen(
    cfg: Config,
    redo: str | None = None,
    test: bool = False,
    overwrite: bool = False,
    skip_email_check: bool = False,
    skip_address_check: bool = False,
) -> ScreenResult:
    """Run screening and derive its result counts post-hoc.

    Approximations (exact tracking would instrument the legacy run):

    - AI targets are derived from the final stores, not the prepared
      rows the legacy run selects: normal runs use non-ignored
      children that were unscreened before the run or appended by it;
      ``overwrite`` uses every non-ignored child. A target the AI
      never attempted (for any reason) counts as failed.
    - ``test`` scope is approximated by the lowest non-null
      ``family_id`` among final children, mirroring the legacy
      first-family helper.
    - Email/address counts are new validation-log rows; diagnostics
      are the post-run snapshot sizes (duplicate + DOB logs), not
      per-run deltas.

    Args:
        cfg: Pipeline configuration.
        redo: Optional child ``zite_record_id`` to re-screen.
        test: Restrict AI candidates to the first stable family.
        overwrite: Deliberately re-screen every current child.
        skip_email_check: Skip ZeroBounce work for this run.
        skip_address_check: Skip Nominatim work for this run.

    Returns:
        The typed screening result.

    Raises:
        ScreeningError: As raised by the legacy run.
    """
    before = {entity: read_final(cfg, entity) for entity in _ENTITIES}
    before_logs = {
        name: _sheet_height(cfg.excel_file, name) for name in _LOG_SHEETS
    }
    screening_module.run(
        cfg,
        redo=redo,
        test=test,
        overwrite=overwrite,
        skip_email_check=skip_email_check,
        skip_address_check=skip_address_check,
    )
    after = {entity: read_final(cfg, entity) for entity in _ENTITIES}
    after_children = {
        str(row.get("zite_record_id")): row for row in after["child"]
    }
    before_children = {
        str(row.get("zite_record_id")): row for row in before["child"]
    }
    if redo is not None:
        targets = [redo]
    else:
        candidates = [
            record_id
            for record_id, row in after_children.items()
            if not as_bool(row.get("ignore"))
            and (
                overwrite
                or record_id not in before_children
                or not as_bool(before_children[record_id].get("screened"))
            )
        ]
        if test:
            first_family = _first_family_id(after_children)
            candidates = [
                record_id
                for record_id in candidates
                if after_children[record_id].get("family_id") == first_family
                or first_family is None
            ]
            if first_family is None:
                candidates = candidates[:1]
        targets = candidates
    succeeded = [
        record_id
        for record_id in targets
        if as_bool(after_children.get(record_id, {}).get("screened"))
    ]
    failed = [record_id for record_id in targets if record_id not in succeeded]
    return ScreenResult(
        counts=entity_count_changes(before, after),
        email_checked=_sheet_height(cfg.excel_file, "email_validation_log")
        - before_logs["email_validation_log"],
        address_checked=_sheet_height(cfg.excel_file, "address_validation_log")
        - before_logs["address_validation_log"],
        ai_targets=targets,
        ai_succeeded=succeeded,
        ai_failed=failed,
        diagnostics_rows=_sheet_height(cfg.excel_file, "duplicate_log")
        + _sheet_height(cfg.excel_file, "dob_conflict_log"),
    )


def match(
    cfg: Config,
    test: bool = False,
    overwrite: bool = False,
    skip_match: bool = False,
) -> MatchResult:
    """Run matching and derive its result from the final stores.

    Matched/unmatched counts describe the post-run assignment state of
    non-ignored rows (prior assignments included), not only families
    matched by this run's search. ``archived`` is True when the run
    created timestamped archive sheets.

    Args:
        cfg: Pipeline configuration.
        test: Stop after the first full sponsor.
        overwrite: Replace previous outputs instead of archiving.
        skip_match: Reconcile assignments without a new report.

    Returns:
        The typed match result.

    Raises:
        MatchError: As raised by the legacy run.
    """
    before_sheets = set(list_sheets(cfg.excel_file))
    match_module.run(cfg, test=test, overwrite=overwrite, skip_match=skip_match)
    families = eligible_output_rows(read_final(cfg, "family"), "family")
    sponsors = eligible_output_rows(read_final(cfg, "sponsor"), "sponsor")
    angel_ids = {
        angel.strip()
        for row in sponsors
        if row.get("assigned_angel_ids")
        for angel in str(row["assigned_angel_ids"]).split(",")
        if angel.strip()
    }
    archives = {
        name
        for name in set(list_sheets(cfg.excel_file)) - before_sheets
        if name.startswith(("final_family_", "final_sponsor_", "match_report_"))
    }
    return MatchResult(
        matched_families=sum(
            1 for row in families if row.get("assigned_sponsor_id") is not None
        ),
        matched_sponsors=sum(
            1 for row in sponsors if row.get("assigned_angel_ids")
        ),
        assigned_angel_ids=sorted(angel_ids, key=match_module._angel_sort_key),
        unmatched_family_ids=[
            int(row["family_id"])
            for row in families
            if row.get("assigned_sponsor_id") is None
        ],
        unmatched_sponsor_ids=[
            int(row["sponsor_id"])
            for row in sponsors
            if not row.get("assigned_angel_ids")
        ],
        report_rows=_sheet_height(cfg.excel_file, "match_report"),
        archived=bool(archives),
        csv_path=str(cfg.match_report_csv),
    )


def generate(
    cfg: Config,
    test: bool = False,
    overwrite: bool = False,
    sponsor: int | None = None,
    max_workers: int | None = None,
) -> GenerateResult:
    """Run generation (sheets, then packets) and derive its counts.

    Approximations: ``sheets_generated`` counts
    ``generated_info_sheet`` flags that turned true during the run;
    ``sheets_skipped`` counts sponsored children (the sponsor's, when
    scoped) without a newly generated sheet — both already-generated
    rows and rows left pending. ``packets_built`` counts sponsors
    whose packet became complete during the run and
    ``packets_skipped`` those complete before and after (under
    ``overwrite`` a rebuilt in-place packet still counts as skipped);
    test mode skips the packet phase, so both stay 0. Per-row
    generation failures raise :class:`ccat.child_info_generate.
    InfoSheetError` from the legacy run instead of filling
    ``sheets_failed``.

    Args:
        cfg: Pipeline configuration.
        test: Stop after the first successfully generated sheet.
        overwrite: Regenerate every sponsored child's sheet.
        sponsor: Restrict the run to one sponsor id.
        max_workers: Override ``info_sheet.max_workers``.

    Returns:
        The typed generation result.

    Raises:
        InfoSheetError: After any per-row sheet failure.
        SponsorPacketError: From the packet phase.
        KeyboardInterrupt: On Ctrl+C, after committing completed flags.
    """
    before_children = {
        str(row.get("zite_record_id")): row for row in read_final(cfg, "child")
    }
    family_sponsor = {
        row["family_id"]: row.get("assigned_sponsor_id")
        for row in read_final(cfg, "family")
        if row.get("family_id") is not None
    }
    packet_sponsors = _packet_sponsors(cfg, sponsor)
    before_complete = {
        sponsor_id: _packet_complete(cfg, sponsor_id)
        for sponsor_id in packet_sponsors
    }
    generate_module.run(
        cfg,
        test=test,
        overwrite=overwrite,
        sponsor=sponsor,
        max_workers=max_workers,
    )
    after_children = {
        str(row.get("zite_record_id")): row for row in read_final(cfg, "child")
    }
    generated: list[str] = []
    skipped: list[str] = []
    for record_id, row in after_children.items():
        angel_id = row.get("angel_id")
        if angel_id is None:
            continue
        assigned = family_sponsor.get(row.get("family_id"))
        if sponsor is not None and assigned != sponsor:
            continue
        if assigned is None:
            continue
        was_generated = as_bool(
            before_children.get(record_id, {}).get("generated_info_sheet")
        )
        if not was_generated and as_bool(row.get("generated_info_sheet")):
            generated.append(generate_module._sheet_stem(cfg, str(angel_id)))
        else:
            skipped.append(generate_module._sheet_stem(cfg, str(angel_id)))
    after_complete = {
        sponsor_id: _packet_complete(cfg, sponsor_id)
        for sponsor_id in packet_sponsors
    }
    return GenerateResult(
        sheets_generated=generated,
        sheets_failed=[],
        sheets_skipped=skipped,
        packets_built=sum(
            1
            for sponsor_id in packet_sponsors
            if after_complete[sponsor_id] and not before_complete[sponsor_id]
        ),
        packets_skipped=sum(
            1
            for sponsor_id in packet_sponsors
            if after_complete[sponsor_id] and before_complete[sponsor_id]
        ),
        info_sheet_dir=str(cfg.info_sheet.output_dir),
        packets_dir=str(cfg.sponsor_packets.output_dir),
    )


def stats(cfg: Config, raw: bool = False) -> StatsResult:
    """Run the statistics report and locate its written artifacts.

    The report stem embeds the run day, so the newest matching HTML
    and PDF files in ``report.output_dir`` are returned; a run that
    crosses midnight between writing and this lookup still resolves
    the just-written pair.

    Args:
        cfg: Pipeline configuration.
        raw: Build from ephemeral prepared rows (CLI ``--raw``).

    Returns:
        The report paths and existence flags.

    Raises:
        StatsError: As raised by the legacy run.
    """
    stats_module.run(cfg, raw=raw)
    out_dir = Path(cfg.report.output_dir)
    html_path = _newest_report_file(out_dir, cfg.report.filename_prefix, "html")
    pdf_path = _newest_report_file(out_dir, cfg.report.filename_prefix, "pdf")
    return StatsResult(
        html_path=str(html_path) if html_path else None,
        pdf_path=str(pdf_path) if pdf_path else None,
        html_written=html_path is not None,
        pdf_written=pdf_path is not None,
    )


def clarify_generate(
    cfg: Config, overwrite: bool = False
) -> ClarifyGenerateResult:
    """Headlessly generate missing clarification drafts (§9.6).

    Mirrors the ``ccat clarify --generate`` branch exactly (build the
    table, refresh the CSV, generate, refresh the CSV again) without
    starting the NiceGUI app.

    Args:
        cfg: Pipeline configuration.
        overwrite: Regenerate families that already have a draft.

    Returns:
        Generated and failed family ids.

    Raises:
        ClarifyError: As raised by the extraction or generation path.
    """
    # Imported here so the service module stays free of NiceGUI.
    from ccat.clarify.server import generate_missing

    table = build_table(Path(cfg.excel_file))
    write_csv(cfg)
    families = group_families(table)
    sender = EmailSender(cfg)
    try:
        done, failed = generate_missing(
            cfg, families, sender, overwrite=overwrite
        )
    finally:
        sender.close()
    write_csv(cfg)
    return ClarifyGenerateResult(
        generated_family_ids=done, failed_family_ids=failed
    )


def clarify_export(
    cfg: Config, output: str | None = None
) -> ClarifyExportResult:
    """Write the clarifications CSV and count its rows.

    Args:
        cfg: Pipeline configuration.
        output: Output CSV path; defaults to
            ``clarify.clarifications_csv``.

    Returns:
        The CSV path and data-row count.

    Raises:
        ClarifyError: As raised by the extraction path.
    """
    csv_path = write_csv(
        cfg, output=Path(output) if output is not None else None
    )
    rows = pl.read_csv(csv_path).height
    return ClarifyExportResult(csv_path=str(csv_path), rows=rows)


def _sheet_height(path: str | Path, sheet: str) -> int:
    """Return one sheet's row count, 0 when absent."""
    frame = read_sheet(path, sheet)
    return frame.height if frame is not None else 0


def _first_family_id(children: dict[str, dict[str, Any]]) -> int | None:
    """Lowest non-null family_id among the children (test scope)."""
    present = [
        int(row["family_id"])
        for row in children.values()
        if row.get("family_id") is not None
    ]
    return min(present) if present else None


def _packet_sponsors(cfg: Config, sponsor: int | None) -> list[int]:
    """Sponsor ids that have angels assigned (optionally scoped)."""
    rows = eligible_output_rows(read_final(cfg, "sponsor"), "sponsor")
    return [
        int(row["sponsor_id"])
        for row in rows
        if row.get("assigned_angel_ids")
        and (sponsor is None or int(row["sponsor_id"]) == sponsor)
    ]


def _packet_complete(cfg: Config, sponsor_id: int) -> bool:
    """Whether a sponsor packet's HTML and combined PDF both exist."""
    spec = cfg.sponsor_packets
    packet_dir = Path(spec.output_dir) / str(sponsor_id)
    html = packet_dir / f"{spec.filename_prefix}_{sponsor_id}.html"
    pdf = (
        packet_dir
        / f"Sponsor {sponsor_id} - Child Information {cfg.season}.pdf"
    )
    return html.is_file() and pdf.is_file()


def _newest_report_file(out_dir: Path, prefix: str, suffix: str) -> Path | None:
    """Newest ``<prefix>*.<suffix>`` report artifact in the directory."""
    if not out_dir.is_dir():
        return None
    files = list(out_dir.glob(f"{prefix}*.{suffix}"))
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


__all__ = [
    "acquire",
    "clarify_export",
    "clarify_generate",
    "generate",
    "match",
    "screen",
    "stats",
]
