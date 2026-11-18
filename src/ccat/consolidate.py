"""Convert full legacy adj stores into a persistent-final workbook.

The source workbook is read-only throughout conversion.  Dense ``adj_*``
sheets are the sole entity sources: projected finals are never consulted,
sparse stores are rejected, and no screening, matching, generation, or seeding
runs here.  A separately staged workbook is validated before it replaces the
requested output path.
"""

from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
import polars as pl

from ccat.audit.store import (
    ENTITY_SPECS,
    as_bool,
    build_final_frame,
    normalize_row,
    read_final,
)
from ccat.config import Config
from ccat.domain import compute_age_at_xmas
from ccat.io import list_sheets, read_sheet, write_sheets

logger = logging.getLogger(__name__)

_ENTITIES = ("sponsor", "family", "child")
_RAW_SHEETS = ("raw_sponsor", "raw_family", "raw_child")
_LOG_SHEETS = (
    "duplicate_log",
    "dob_conflict_log",
    "email_validation_log",
    "address_validation_log",
)
_COPIED_AUXILIARY_SHEETS = (*_LOG_SHEETS, "match_report")


class ConsolidationError(Exception):
    """Raised when a consolidation cannot be completed and verified."""


def _file_sha256(path: Path) -> str:
    """Hash one file without modifying it.

    Args:
        path: Existing readable file.

    Returns:
        Lowercase SHA-256 hexadecimal digest.
    """
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _same_path(left: Path, right: Path) -> bool:
    """Compare paths after resolution, including not-yet-created outputs."""
    return left.resolve() == right.resolve()


def _require_distinct_paths(
    source: Path, output: Path, backup: Path | None, report: Path
) -> None:
    """Reject aliases that could overwrite input, output, or backup data."""
    if _same_path(source, output):
        raise ConsolidationError(
            "--from and --output must resolve to different paths"
        )
    protected = (("source", source), ("output", output))
    if backup is not None:
        protected += (("backup", backup),)
    for label, path in protected:
        if _same_path(report, path):
            raise ConsolidationError(
                f"report path must differ from the {label} path"
            )


def _verify_backup(
    source: Path, output: Path, backup_path: str | Path | None
) -> tuple[Path, str]:
    """Verify a byte-identical, restorable workbook backup for force.

    Args:
        source: Immutable input workbook.
        output: Existing output workbook being replaced.
        backup_path: User-supplied backup location.

    Returns:
        The validated backup path and its SHA-256 digest.

    Raises:
        ConsolidationError: If the backup is absent, non-workbook, unreadable,
            not byte-identical to the output, or aliases either path.
    """
    if not backup_path:
        raise ConsolidationError(
            "--force requires --backup PATH: a byte-identical restorable"
            " copy of the output about to be replaced"
        )
    backup = Path(backup_path)
    if _same_path(backup, source) or _same_path(backup, output):
        raise ConsolidationError(
            "backup path must differ from input and output"
        )
    if not backup.is_file():
        raise ConsolidationError(f"backup file not found: {backup}")
    try:
        workbook = load_workbook(backup, read_only=True)
        workbook.close()
    except Exception as error:
        raise ConsolidationError(
            f"backup is not a readable workbook: {backup}: {error}"
        ) from error
    backup_hash = _file_sha256(backup)
    output_hash = _file_sha256(output)
    if backup_hash != output_hash:
        raise ConsolidationError(
            f"backup {backup} is not byte-identical to output {output};"
            " force replacement would not be restorable"
        )
    return backup, backup_hash


def _read_required_sheet(source: Path, sheet: str) -> pl.DataFrame:
    """Read a required source sheet or raise migration-specific guidance."""
    frame = read_sheet(source, sheet)
    if frame is None:
        raise ConsolidationError(
            f"source workbook {source} requires full {sheet}; sparse or "
            "projected-only v1 workbooks must first use `ccat audit-migrate` "
            "with a separate target"
        )
    return frame


def _legacy_bool(value: Any) -> bool:
    """Interpret Boolean/string/numeric legacy control cells safely."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, int | float):
        return value == 1
    return str(value).strip().lower() in {"true", "yes", "1"}


def _legacy_dense_row(entity: str, row: dict[str, Any]) -> dict[str, Any]:
    """Convert one old dense row to status-only/classification storage."""
    from ccat.audit.store import STATUS_AUDITED

    converted = dict(row)
    if "require_clarification" in converted:
        converted["clarification_notes"] = converted.pop(
            "require_clarification"
        )
    if _legacy_bool(converted.pop("audited", False)):
        converted["status"] = STATUS_AUDITED
    return {
        column: value
        for column, value in converted.items()
        if column not in {"audited", "require_clarification"}
    }


def _source_frames(
    source: Path,
) -> tuple[dict[str, pl.DataFrame], dict[str, pl.DataFrame]]:
    """Load raw/log layers and validate dense adj schemas.

    Returns:
        Pair of (all output-ready auxiliary/final frames, final frames only).
    """
    sheets: dict[str, pl.DataFrame] = {}
    for raw in _RAW_SHEETS:
        sheets[raw] = _read_required_sheet(source, raw)
    for sheet in _COPIED_AUXILIARY_SHEETS:
        frame = read_sheet(source, sheet)
        if frame is not None:
            sheets[sheet] = frame

    finals: dict[str, pl.DataFrame] = {}
    for entity in _ENTITIES:
        adj_sheet = f"adj_{entity}"
        frame = _read_required_sheet(source, adj_sheet)
        expected = list(ENTITY_SPECS[entity]["columns"])
        # Old dense inputs carry the removed Boolean and old note name. Convert
        # them to status-only/classification-notes storage below.
        legacy_controls = {
            column
            for column in ("audited", "require_clarification")
            if column in frame.columns
        }
        missing = [column for column in expected if column not in frame.columns]
        extra = [
            column
            for column in frame.columns
            if column not in expected and column not in legacy_controls
        ]
        if (
            missing
            or extra
            or legacy_controls
            not in ({"audited", "require_clarification"}, set())
        ):
            raise ConsolidationError(
                f"{adj_sheet} is not a full dense store; missing={missing}, "
                f"extra={extra}, legacy_controls={sorted(legacy_controls)}."
                " Do not infer values from projected finals or merge sparse"
                " legacy rows here; use `ccat audit-migrate` for an old v1"
                " workbook"
            )
        try:
            final = build_final_frame(
                entity,
                [_legacy_dense_row(entity, row) for row in frame.to_dicts()],
            )
        except Exception as error:
            raise ConsolidationError(
                f"invalid full source {adj_sheet}: {error}"
            ) from error
        finals[entity] = final
        sheets[ENTITY_SPECS[entity]["sheet"]] = final
    return sheets, finals


def _key_token(entity: str, value: Any) -> str:
    """Normalize one persistent key for forced-overwrite comparisons."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ConsolidationError(
            f"{entity} contains a null or blank entity key"
        )
    if entity == "child":
        return str(value).strip()
    try:
        return str(int(value))
    except (TypeError, ValueError):
        try:
            number = float(value)
            if not number.is_integer():
                raise ValueError(value)
            return str(int(number))
        except (TypeError, ValueError) as error:
            raise ConsolidationError(
                f"{entity} contains a non-integer persistent key: {value!r}"
            ) from error


def _guard_forced_row_removal(
    output: Path, finals: dict[str, pl.DataFrame]
) -> None:
    """Apply the writer's no-row-removal invariant before fresh staging.

    Staging from an empty workbook prevents obsolete adj/submission/archive
    sheets surviving a forced conversion.  This preflight deliberately keeps
    the same protection as ``io.write_sheets`` on an in-place forced write: a
    source with fewer entity keys cannot silently replace a richer output.

    Args:
        output: Existing output about to be replaced under ``force``.
        finals: Fully normalized source-derived final frames.

    Raises:
        ConsolidationError: If an existing final key would disappear.
    """
    for entity, final in finals.items():
        spec = ENTITY_SPECS[entity]
        sheet = spec["sheet"]
        key = spec["key"]
        existing = read_sheet(output, sheet)
        if existing is None:
            continue
        if key not in existing.columns:
            raise ConsolidationError(
                f"existing {sheet} lacks {key}; force cannot bypass the "
                "row-removal safety guard"
            )
        previous = {
            _key_token(entity, value) for value in existing.get_column(key)
        }
        incoming = {
            _key_token(entity, value) for value in final.get_column(key)
        }
        removed = previous - incoming
        if removed:
            raise ConsolidationError(
                f"force would remove persisted {sheet} key(s): "
                f"{sorted(removed)}; the row-removal guard remains in force. "
                "Use a new output path or restore the complete source"
            )


def _canonical_scalar(value: Any) -> dict[str, Any] | None:
    """Represent an Excel value robustly across numeric/date dtype shifts."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return {"date": value.isoformat()}
    if isinstance(value, date):
        return {"date": value.isoformat()}
    if isinstance(value, bool):
        return {"bool": value}
    if isinstance(value, int | float | Decimal):
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            return {"number": str(value)}
        if not number.is_finite():
            return {"number": str(number)}
        if number == 0:
            return {"number": "0"}
        return {"number": format(number.normalize(), "f")}
    return {"text": str(value)}


def _canonical_frame(frame: pl.DataFrame) -> dict[str, Any]:
    """Capture columns, row order, and values without depending on dtypes."""
    return {
        "columns": list(frame.columns),
        "rows": [
            [_canonical_scalar(value) for value in row]
            for row in frame.iter_rows()
        ],
    }


def _frame_sha256(frame: pl.DataFrame) -> str:
    """Hash a copied sheet's normalized content for the migration report."""
    payload = json.dumps(
        _canonical_frame(frame),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _distribution(rows: list[dict[str, Any]], column: str) -> dict[str, int]:
    """Count normalized control values with JSON-safe bucket names."""
    counts: Counter[str] = Counter()
    for row in rows:
        value = row.get(column)
        if value is None:
            token = "null"
        elif isinstance(value, bool):
            token = "true" if value else "false"
        else:
            token = str(value)
        counts[token] += 1
    return dict(sorted(counts.items()))


def _entity_metrics(
    entity: str, rows: list[dict[str, Any]], raw: pl.DataFrame
) -> dict[str, Any]:
    """Summarize preserved controls, linkage, assignments, and work state."""
    spec = ENTITY_SPECS[entity]
    key = spec["key"]
    # Source presence is always a Zite record identity, never a derived
    # family/sponsor ID; acquired raw sheets do not contain derived IDs.
    raw_record_ids = {
        str(value).strip()
        for value in (
            raw.get_column("zite_record_id").to_list()
            if "zite_record_id" in raw.columns
            else []
        )
        if value is not None and str(value).strip()
    }
    metrics: dict[str, Any] = {
        "rows": len(rows),
        "orphans": (
            sum(row.get("family_id") is None for row in rows)
            if entity == "child"
            else 0
        ),
        "ignored": sum(as_bool(row.get("ignore")) for row in rows),
        "raw_missing": sum(
            str(row.get("zite_record_id") or "").strip() not in raw_record_ids
            for row in rows
        ),
        "keys": sorted(
            str(row.get(key))
            for row in rows
            if row.get(key) is not None and str(row.get(key)).strip()
        ),
        "controls": {
            column: _distribution(rows, column)
            for column in (
                "status",
                "ignore",
                "clarification_notes",
                "audited_at",
            )
        },
        "assignments": {},
        "generated": {},
    }
    if entity == "child":
        metrics["controls"]["screened"] = _distribution(rows, "screened")
        metrics["controls"]["potential_duplicate"] = _distribution(
            rows, "potential_duplicate"
        )
        metrics["assignments"] = {
            "with_angel_id": sum(
                row.get("angel_id") is not None for row in rows
            ),
            "with_family_id": sum(
                row.get("family_id") is not None for row in rows
            ),
        }
        generated = sum(
            as_bool(row.get("generated_info_sheet")) for row in rows
        )
        metrics["generated"] = {
            "info_sheet_true": generated,
            "info_sheet_false": len(rows) - generated,
        }
    elif entity == "family":
        metrics["controls"]["potential_duplicate"] = _distribution(
            rows, "potential_duplicate"
        )
        metrics["assignments"] = {
            "with_sponsor": sum(
                row.get("assigned_sponsor_id") is not None for row in rows
            ),
            "with_angel_ids": sum(
                row.get("assigned_angel_ids") is not None for row in rows
            ),
            "num_angels_in_family_total": sum(
                row.get("num_angels_in_family") or 0 for row in rows
            ),
        }
    else:
        metrics["controls"]["potential_duplicate"] = _distribution(
            rows, "potential_duplicate"
        )
        metrics["assignments"] = {
            "with_angel_ids": sum(
                row.get("assigned_angel_ids") is not None for row in rows
            ),
            "num_angels_assigned_total": sum(
                row.get("num_angels_assigned") or 0 for row in rows
            ),
        }
    return metrics


def _normalized_email(value: Any) -> str:
    """Normalize one email for replay comparison."""
    return "" if value is None else str(value).strip().lower()


def _pending_email_count(
    raw_frames: dict[str, pl.DataFrame], logs: dict[str, pl.DataFrame | None]
) -> int:
    """Count unique email addresses not already successfully/check logged."""
    existing = logs.get("email_validation_log")
    checked = {
        _normalized_email(row.get("email"))
        for row in (existing.to_dicts() if existing is not None else [])
        if _normalized_email(row.get("email"))
    }
    targets: list[str] = []
    for row in raw_frames.get("sponsor", pl.DataFrame()).to_dicts():
        targets.append(_normalized_email(row.get("email")))
    for row in raw_frames.get("family", pl.DataFrame()).to_dicts():
        targets.extend(
            [
                _normalized_email(row.get("primary_email")),
                _normalized_email(row.get("secondary_email")),
            ]
        )
    return len({email for email in targets if email and email not in checked})


def _pending_address_count(
    raw_frames: dict[str, pl.DataFrame], logs: dict[str, pl.DataFrame | None]
) -> int:
    """Count unique family address queries not already logged."""
    from ccat.screening import _address_engine

    existing = logs.get("address_validation_log")
    checked = {
        str(row.get("query")).strip()
        for row in (existing.to_dicts() if existing is not None else [])
        if row.get("query") is not None
    }
    _check, build_query = _address_engine()
    queries: set[str] = set()
    for row in raw_frames.get("family", pl.DataFrame()).to_dicts():
        parts = {
            "street": row.get("street_address"),
            "apt_floor": row.get("apt_floor"),
            "city": row.get("city"),
            "state": row.get("state"),
            "zip_code": row.get("zip_code"),
        }
        if not any(str(value or "").strip() for value in parts.values()):
            continue
        query = build_query(
            parts["street"],
            apt_floor=parts["apt_floor"],
            city=parts["city"],
            state=parts["state"],
            zip_code=parts["zip_code"],
        ).strip()
        if query and query not in checked:
            queries.add(query)
    return len(queries)


def _generated_file_metrics(
    cfg: Config,
    children: list[dict[str, Any]],
    families: list[dict[str, Any]],
) -> dict[str, Any]:
    """Check each generated marker against its actual child info PDF."""
    from ccat.match import canonical_location

    locations = {
        row.get("family_id"): row.get("gift_pickup_location")
        for row in families
    }
    output_root = Path(cfg.info_sheet.output_dir)
    present = 0
    missing: list[str] = []
    for row in children:
        if not as_bool(row.get("generated_info_sheet")):
            continue
        angel_id = row.get("angel_id")
        family_id = row.get("family_id")
        location = canonical_location(
            locations.get(family_id), cfg.location_groups
        )
        pdf = (
            output_root
            / (location or "unknown")
            / f"AT{cfg.season}_{angel_id}.pdf"
        )
        if pdf.is_file():
            present += 1
        else:
            missing.append(str(angel_id))
    return {
        "marked": sum(
            as_bool(row.get("generated_info_sheet")) for row in children
        ),
        "files_present": present,
        "files_missing": len(missing),
        "missing_angel_ids": missing,
    }


def _validate_child_formulas(
    workbook_path: Path, rows: list[dict[str, Any]], cfg: Config
) -> None:
    """Verify live age formulas and cached values using read-only opens."""
    formula_book = load_workbook(workbook_path, read_only=True, data_only=False)
    value_book = load_workbook(workbook_path, read_only=True, data_only=True)
    try:
        if not formula_book.read_only or not value_book.read_only:
            raise ConsolidationError("output workbook did not open read-only")
        formula_sheet = formula_book[ENTITY_SPECS["child"]["sheet"]]
        value_sheet = value_book[ENTITY_SPECS["child"]["sheet"]]
        headers = [cell.value for cell in next(formula_sheet.iter_rows())]
        dob_column = headers.index("dob") + 1
        age_column = headers.index("age_at_xmas") + 1
        dob_letter = get_column_letter(dob_column)
        for row_number, row in enumerate(rows, start=2):
            formula = formula_sheet.cell(row_number, age_column).value
            cached = value_sheet.cell(row_number, age_column).value
            dob = row.get("dob")
            expected_age: int | None = None
            if dob is not None:
                try:
                    expected_age = compute_age_at_xmas(str(dob), cfg.xmas)
                except ValueError:
                    expected_age = None
            if expected_age is not None:
                expected_formula = (
                    f"=DATEDIF({dob_letter}{row_number}, "
                    f'DATE({cfg.xmas.year},{cfg.xmas.month},{cfg.xmas.day}), "y")'
                )
                if formula != expected_formula:
                    raise ConsolidationError(
                        f"final_child row {row_number} has invalid age formula: "
                        f"{formula!r}"
                    )
            elif isinstance(formula, str) and formula.startswith("="):
                raise ConsolidationError(
                    f"final_child row {row_number} unexpectedly has an age "
                    f"formula for dob {dob!r}"
                )
            expected_cached = (
                expected_age
                if expected_age is not None
                else row.get("age_at_xmas")
            )
            if cached != expected_cached:
                raise ConsolidationError(
                    f"final_child row {row_number} cached age {cached!r} does "
                    f"not match effective dob/xmas value {expected_cached!r}"
                )
    finally:
        formula_book.close()
        value_book.close()


def _validate_output(
    cfg: Config,
    candidate: Path,
    source_sheets: dict[str, pl.DataFrame],
    finals: dict[str, pl.DataFrame],
) -> tuple[list[str], dict[str, list[dict[str, Any]]]]:
    """Validate the complete staged workbook before exposing it as output."""
    validation: list[str] = []
    actual_names = set(list_sheets(candidate))
    expected_names = set(source_sheets)
    if actual_names != expected_names:
        raise ConsolidationError(
            "output sheet set differs from the persistent-final contract; "
            f"missing={sorted(expected_names - actual_names)}, "
            f"extra={sorted(actual_names - expected_names)}"
        )
    validation.append(
        "output opens read-only and contains no adj/submissions/archives"
    )

    output_rows: dict[str, list[dict[str, Any]]] = {}
    for entity, expected_frame in finals.items():
        rows = read_final(cfg, entity, workbook=candidate)
        expected = [
            normalize_row(entity, row) for row in expected_frame.to_dicts()
        ]
        if rows != expected:
            raise ConsolidationError(
                f"final_{entity} rows, order, keys, fields, or controls differ "
                f"from normalized adj_{entity} source rows"
            )
        stored = read_sheet(candidate, ENTITY_SPECS[entity]["sheet"])
        if stored is None:  # pragma: no cover - read_final already required it
            raise ConsolidationError(
                f"final_{entity} disappeared during validation"
            )
        key = ENTITY_SPECS[entity]["key"]
        for value in stored.get_column(key).to_list():
            if entity == "child":
                valid_type = isinstance(value, str) and bool(value.strip())
            else:
                valid_type = isinstance(value, int) and not isinstance(
                    value, bool
                )
            if not valid_type:
                raise ConsolidationError(
                    f"final_{entity} key {value!r} changed its required "
                    "non-null storage type"
                )
        output_rows[entity] = rows
        validation.append(
            f"final_{entity}: full schema, unique typed keys, row/order/value parity"
        )

    for sheet, expected_frame in source_sheets.items():
        if sheet in {spec["sheet"] for spec in ENTITY_SPECS.values()}:
            continue
        actual = read_sheet(candidate, sheet)
        if actual is None or _canonical_frame(actual) != _canonical_frame(
            expected_frame
        ):
            raise ConsolidationError(
                f"copied sheet {sheet} did not survive with identical columns, "
                "rows, order, and values"
            )
        validation.append(f"{sheet}: source content preserved")
    _validate_child_formulas(candidate, output_rows["child"], cfg)
    validation.append(
        "final_child age formulas and cached values match dob/xmas"
    )
    return validation, output_rows


def _stage_json_report(path: Path, report: dict[str, Any]) -> Path:
    """Stage a successful JSON report before exposing output.

    Args:
        path: Final report destination.
        report: Complete report data.

    Returns:
        The staged report path.

    Raises:
        ConsolidationError: If the destination is invalid or unwritable.
    """
    if path.exists() and path.is_dir():
        raise ConsolidationError(
            f"migration report path is a directory: {path}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        temporary.write_text(
            json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise ConsolidationError(
            f"could not stage migration report {path}: {error}"
        ) from error
    return temporary


def _commit_output_and_report(
    output: Path,
    candidate: Path,
    report: Path,
    report_staging: Path,
) -> None:
    """Install workbook and report, restoring output on any late failure."""
    output_existed = output.exists()
    report_existed = report.exists()
    rollback: Path | None = None
    report_rollback: Path | None = None
    if output_existed:
        rollback = output.parent / f".{output.name}.{uuid4().hex}.rollback"
        try:
            shutil.copy2(output, rollback)
        except OSError as error:
            report_staging.unlink(missing_ok=True)
            raise ConsolidationError(
                f"could not create a restorable rollback copy of {output}:"
                f" {error}"
            ) from error
    if report_existed:
        report_rollback = (
            report.parent / f".{report.name}.{uuid4().hex}.rollback"
        )
        try:
            shutil.copy2(report, report_rollback)
        except OSError as error:
            if rollback is not None:
                rollback.unlink(missing_ok=True)
            report_staging.unlink(missing_ok=True)
            raise ConsolidationError(
                f"could not create a restorable rollback copy of {report}:"
                f" {error}"
            ) from error
    try:
        os.replace(candidate, output)
        os.replace(report_staging, report)
        if not output.is_file() or not report.is_file():
            raise ConsolidationError(
                "workbook/report commit verification failed"
            )
    except Exception:
        # A workbook installed without its handoff report is not a successful
        # migration. Restore the exact prior artifacts whenever possible.
        if rollback is not None and rollback.is_file():
            shutil.copy2(rollback, output)
        elif not output_existed and output.exists():
            output.unlink()
        if report_rollback is not None and report_rollback.is_file():
            shutil.copy2(report_rollback, report)
        elif not report_existed and report.exists():
            report.unlink()
        raise
    finally:
        for temporary in (rollback, report_rollback, report_staging):
            if temporary is not None and temporary.exists():
                try:
                    temporary.unlink()
                except OSError as error:
                    logger.warning(
                        "could not remove temporary commit file %s: %s",
                        temporary,
                        error,
                    )


def run(
    cfg: Config,
    from_path: str,
    output_path: str,
    force: bool = False,
    backup_path: str | None = None,
    report_path: str | None = None,
) -> Path:
    """Consolidate full adj sheets into a verified persistent-final workbook.

    The input hash is checked before and after conversion.  Existing outputs
    require ``force`` plus an explicit readable backup.  Forced replacement
    retains the no-row-removal guard, while fresh staging ensures removed adj,
    submissions, and archive layers cannot leak into the new workbook.

    Args:
        cfg: Season configuration; ``xmas`` supplies child formula context.
        from_path: Full legacy workbook containing all three dense adj stores.
        output_path: Separate destination workbook.
        force: Permit replacement of an existing destination.
        backup_path: Required with ``force``; existing nonempty readable file.
        report_path: Optional JSON report destination. Defaults to
            ``<output>.migration.json``.

    Returns:
        The requested output path after validation and report creation.

    Raises:
        ConsolidationError: If source, overwrite, backup, write, or validation
            requirements fail. No success report is written on failure.
    """
    source = Path(from_path)
    output = Path(output_path)
    report = (
        Path(report_path)
        if report_path is not None
        else Path(f"{output}.migration.json")
    )
    candidate: Path | None = None
    try:
        if not source.is_file():
            raise ConsolidationError(f"source workbook not found: {source}")
        _require_distinct_paths(source, output, None, report)
        output_existed = output.exists()
        if output_existed and not force:
            raise ConsolidationError(
                f"output already exists: {output}; refusing repeated conversion "
                "without --force"
            )
        if output_existed and not output.is_file():
            raise ConsolidationError(f"output is not a regular file: {output}")
        backup: tuple[Path, str] | None = (
            _verify_backup(source, output, backup_path) if force else None
        )
        if backup is not None:
            _require_distinct_paths(source, output, backup[0], report)
        input_hash_before = _file_sha256(source)
        source_sheets, finals = _source_frames(source)
        if output_existed:
            _guard_forced_row_removal(output, finals)

        output.parent.mkdir(parents=True, exist_ok=True)
        candidate = output.parent / f".{output.name}.{uuid4().hex}.tmp.xlsx"
        write_sheets(candidate, "migrate", source_sheets, xmas=cfg.xmas)
        validation, output_rows = _validate_output(
            cfg, candidate, source_sheets, finals
        )
        input_hash_after = _file_sha256(source)
        if input_hash_after != input_hash_before:
            raise ConsolidationError(
                "input workbook changed during consolidation"
            )
        validation.append("input SHA-256 and source content remained immutable")
        if output.exists() and not force:
            raise ConsolidationError(
                f"output appeared during conversion and will not be replaced: {output}"
            )
        output_hash = _file_sha256(candidate)
        entities = {
            entity: _entity_metrics(
                entity,
                output_rows[entity],
                source_sheets[f"raw_{entity}"],
            )
            for entity in _ENTITIES
        }
        generated_metrics = _generated_file_metrics(
            cfg,
            output_rows["child"],
            output_rows["family"],
        )
        validation_logs = {
            sheet: source_sheets.get(sheet) for sheet in _LOG_SHEETS
        }
        raw_child_ids = {
            str(value).strip()
            for value in source_sheets["raw_child"]["zite_record_id"].to_list()
            if value is not None and str(value).strip()
        }
        pending_ai = sum(
            str(row.get("zite_record_id") or "").strip() in raw_child_ids
            and not as_bool(row.get("ignore"))
            and not as_bool(row.get("screened"))
            for row in output_rows["child"]
        )
        pending_work = {
            "ai_children": pending_ai,
            "email_addresses": _pending_email_count(
                source_sheets, validation_logs
            ),
            "family_addresses": _pending_address_count(
                source_sheets, validation_logs
            ),
        }
        entities["child"]["generated_files"] = generated_metrics
        copied: dict[str, Any] = {}
        for sheet in (*_RAW_SHEETS, *_COPIED_AUXILIARY_SHEETS):
            frame = source_sheets.get(sheet)
            copied[sheet] = {
                "present": frame is not None,
                "rows": frame.height if frame is not None else 0,
                "sha256": _frame_sha256(frame) if frame is not None else None,
            }
        report_data: dict[str, Any] = {
            "input": {
                "path": str(source),
                "sha256_before": input_hash_before,
                "sha256_after": input_hash_after,
            },
            "output": {"path": str(output), "sha256": output_hash},
            "backup": (
                None
                if backup is None
                else {
                    "path": str(backup[0]),
                    "sha256": backup[1],
                    "byte_identical_to_previous_output": True,
                }
            ),
            "entities": entities,
            "pending_work": pending_work,
            "sheets": copied,
            "logs": {sheet: copied[sheet] for sheet in _LOG_SHEETS},
            "validation": validation,
        }
        report_staging = _stage_json_report(report, report_data)
        _commit_output_and_report(output, candidate, report, report_staging)
        logger.info(
            "consolidation complete: %d sponsors, %d families, %d children; "
            "report=%s; source never written",
            len(output_rows["sponsor"]),
            len(output_rows["family"]),
            len(output_rows["child"]),
            report,
        )
        return output
    except ConsolidationError:
        raise
    except Exception as error:
        raise ConsolidationError(f"consolidation failed: {error}") from error
    finally:
        if candidate is not None and candidate.exists():
            try:
                candidate.unlink()
            except OSError as error:
                logger.warning(
                    "could not remove failed staging file: %s", error
                )
