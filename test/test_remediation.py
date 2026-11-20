"""Regression tests for review remediation blockers RB-01 through RB-20."""

from pathlib import Path
from typing import Any

import polars as pl
import pytest

from ccat.audit.store import _refresh_metadata, _raw_columns, read_final
from ccat.config import Config
from ccat.io import read_sheet, write_sheets
from ccat.preparation import clean_rows, prepare_rows


def child_row(**overrides: Any) -> dict[str, Any]:
    """Minimal valid child source row with config defaults for other fields."""
    return {
        "zite_record_id": overrides.pop("zite_record_id", "c1"),
        "created": overrides.pop("created", "2026-01-01T00:00:00+00:00"),
        "zite_created_at": "2026-01-01T00:00:00+00:00",
        "primary_email": overrides.pop("primary_email", "fam@example.com"),
        "first_name": overrides.pop("first_name", "Kiddo"),
        "dob": overrides.pop("dob", "2018-05-10"),
        "age_reported": 7,
        "gender": "Male",
        "need_1": "winter coat",
        "source": "web",
        "family_id": overrides.pop("family_id", 1),
        "angel_id": overrides.pop("angel_id", None),
        **overrides,
    }


def family_row(**overrides: Any) -> dict[str, Any]:
    """Minimal family source row."""
    return {
        "zite_record_id": overrides.pop("zite_record_id", "f1"),
        "created": overrides.pop("created", "2026-01-01T00:00:00+00:00"),
        "zite_created_at": "2026-01-01T00:00:00+00:00",
        "primary_email": overrides.pop("primary_email", "fam@example.com"),
        "first_name": "Jane",
        "last_name": "Doe",
        "gift_pickup_location": "TSA (Hancock)",
        "source": "web",
        **overrides,
    }


def sponsor_row(**overrides: Any) -> dict[str, Any]:
    """Minimal sponsor source row."""
    return {
        "zite_record_id": overrides.pop("zite_record_id", "s1"),
        "create": "2026-01-01T00:00:00+00:00",
        "email": "sponsor@example.com",
        "first_name": "Mary",
        "last_name": "Smith",
        "num_children": "1",
        "age_preference": "Any Age",
        "gender_preference": "Any Gender",
        "source": "web",
        **overrides,
    }


def final_child(**overrides: Any) -> dict[str, Any]:
    """Return stored child with stable linkage and generated state."""
    source = child_row(**overrides)
    source.update(
        {
            "age_at_xmas": 8,
            "screened": True,
            "generated_info_sheet": True,
            "status": "ignored" if overrides.get("ignore") else "audited",
            "audited": not overrides.get("ignore", False),
            "ignore": overrides.pop("ignore", False),
        }
    )
    return source


def test_ignored_angel_owner_blocks_late_duplicate(
    cfg: Config,
) -> None:
    """A generated/assigned angel remains reserved after ignore."""
    ignored = final_child(
        zite_record_id="c-old",
        angel_id="1A",
        ignore=True,
        status="ignored",
    )
    raw = [
        child_row(zite_record_id="c-new", created="2026-02-01T00:00:00+00:00")
    ]
    preparation = prepare_rows(
        {"sponsor": [], "family": [family_row()], "child": raw},
        cfg,
        existing={"sponsor": [], "family": [], "child": [ignored]},
    )
    assert preparation.rows["child"][0]["zite_record_id"] == "c-new"
    assert preparation.rows["child"][0]["angel_id"] is None
    assert ignored["angel_id"] == "1A"


def test_edited_owner_key_blocks_raw_duplicate(
    cfg: Config,
) -> None:
    """Ownership joins current raw members by record ID, not edited keys."""
    owner = final_child(
        zite_record_id="c-old",
        first_name="Edited",
        angel_id="1A",
    )
    raw = [
        child_row(
            zite_record_id="c-old",
            first_name="Raw",
            created="2026-01-01T00:00:00+00:00",
        ),
        child_row(
            zite_record_id="c-new",
            first_name="Raw",
            created="2026-02-01T00:00:00+00:00",
        ),
    ]
    preparation = prepare_rows(
        {"sponsor": [], "family": [family_row()], "child": raw},
        cfg,
        existing={"sponsor": [], "family": [], "child": [owner]},
    )
    by_id = {row["zite_record_id"]: row for row in preparation.rows["child"]}
    assert by_id["c-old"]["angel_id"] == "1A"
    assert by_id["c-new"]["angel_id"] is None


def test_dense_result_preserves_ignored_skeleton(
    cfg: Config,
) -> None:
    """Defensive AI handling changes only the screening marker."""
    from ccat.screening import dense_final_row

    ignored = final_child(
        zite_record_id="c-ignored",
        first_name="Historical",
        need_1=None,
        created=None,
        source=None,
        ignore=True,
        status="ignored",
    )
    before = dict(ignored)
    raw = child_row(
        zite_record_id="c-ignored",
        created="2026-02-01T00:00:00+00:00",
        need_1="new raw coat",
    )
    rebuilt = dense_final_row(
        raw, {"zite_record_id": "c-ignored", "need_1": "AI coat"}, cfg, ignored
    )
    for column in set(before) - {"screened"}:
        assert rebuilt[column] == before[column], column
    assert rebuilt["screened"] is True


def test_raw_missing_child_baseline_is_isolated(cfg: Config) -> None:
    """A raw-missing child never borrows a current duplicate's values."""
    from ccat.audit.store import _entity_baseline

    stored = final_child(
        zite_record_id="historical",
        need_1="historical coat",
        first_name="Same",
        primary_email="same@example.com",
    )
    baseline = _entity_baseline(cfg, "child", stored, is_current_raw=False)
    expected = {column: stored.get(column) for column in _raw_columns("child")}
    assert baseline == expected


def test_family_metadata_refresh_copies_full_survivor_bundle() -> None:
    """Family source identity and timestamps come from one submission."""
    existing = {
        "family_id": 1,
        "zite_record_id": "f-old",
        "created": "2026-02-01T00:00:00+00:00",
        "zite_created_at": "2026-02-01T00:00:00+00:00",
        "source": "old",
        "status": "new",
    }
    survivor = {
        "family_id": 1,
        "zite_record_id": "f-new",
        "created": "2026-01-01T00:00:00+00:00",
        "zite_created_at": "2026-01-01T00:00:00+00:00",
        "source": "new",
    }
    refreshed = _refresh_metadata(existing, survivor, "family")
    assert refreshed["zite_record_id"] == "f-new"
    assert refreshed["created"] == survivor["created"]
    assert refreshed["zite_created_at"] == survivor["zite_created_at"]
    assert refreshed["source"] == "new"
    assert refreshed["family_id"] == 1


def test_minimum_raw_fixture_has_required_columns(cfg: Config) -> None:
    """Fixture rows remain compatible with shared preparation schemas."""
    rows = {
        "sponsor": [sponsor_row()],
        "family": [family_row()],
        "child": [child_row()],
    }
    for entity_name, entity_rows in rows.items():
        prepared, _notes = clean_rows(entity_rows, entity_name, cfg)
        assert prepared


def test_numeric_entity_ids_reject_lossy_values() -> None:
    """Stable integer IDs never truncate decimals or coerce booleans."""
    from ccat.audit.store import build_final_frame

    for value in (1.5, True):
        with pytest.raises(Exception, match="stable IDs"):
            build_final_frame("family", [{"family_id": value}])


def test_write_rejects_existing_projected_final(cfg: Config) -> None:
    """Same-key projected finals cannot bypass full-schema validation."""
    from openpyxl import Workbook

    from ccat.audit.store import build_final_frame
    from ccat.io import write_sheets

    path = Path(cfg.excel_file)
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "final_family"
    sheet.append(["family_id"])
    sheet.append([1])
    workbook.save(path)

    valid = build_final_frame("family", [{"family_id": 1}])
    with pytest.raises(Exception, match="persistent full schema"):
        write_sheets(path, "audit", {"final_family": valid})


def test_formula_cache_capture_fails_closed(cfg: Config) -> None:
    """A formula without a cached age is a structural failure."""
    import re
    import zipfile

    from ccat.audit.store import build_final_frame
    from ccat.io import _final_child_formula_caches, write_sheets
    from test_screening import child, final_child_row

    write_sheets(
        cfg.excel_file,
        "audit",
        {
            "final_child": build_final_frame(
                "child",
                [
                    final_child_row(
                        child(dob="2018-01-01"),
                        zite_record_id="c1",
                    )
                ],
            )
        },
        xmas=cfg.xmas,
    )
    source = Path(cfg.excel_file)
    assert _final_child_formula_caches(source)
    patched = source.parent / "missing-cache.xlsx"
    with zipfile.ZipFile(source) as archive:
        entries = [
            (item, archive.read(item.filename)) for item in archive.infolist()
        ]
    with zipfile.ZipFile(patched, "w", zipfile.ZIP_DEFLATED) as archive:
        for item, data in entries:
            if item.filename.startswith("xl/worksheets/sheet"):
                text = data.decode("utf-8")
                text, count = re.subn(
                    r"(?<=</f>)<v>[^<]+</v>", "", text, count=1
                )
                assert count
                data = text.encode("utf-8")
            archive.writestr(item, data)
    with pytest.raises(Exception, match="no usable cached age"):
        _final_child_formula_caches(patched)


def test_formula_cache_capture_handles_whitespace(cfg: Config) -> None:
    """Valid whitespace OOXML is parsed, not mistaken for no formulas."""
    import re
    import zipfile

    from ccat.audit.store import build_final_frame
    from ccat.io import _final_child_formula_caches, write_sheets
    from test_screening import child, final_child_row

    write_sheets(
        cfg.excel_file,
        "audit",
        {
            "final_child": build_final_frame(
                "child",
                [final_child_row(child(dob="2018-01-01"), zite_record_id="c1")],
            )
        },
        xmas=cfg.xmas,
    )
    source = Path(cfg.excel_file)
    expected = _final_child_formula_caches(source)
    assert expected
    patched = source.parent / "whitespace.xlsx"
    with zipfile.ZipFile(source) as archive:
        entries = [
            (item, archive.read(item.filename)) for item in archive.infolist()
        ]
    with zipfile.ZipFile(patched, "w", zipfile.ZIP_DEFLATED) as archive:
        for item, data in entries:
            if item.filename.startswith("xl/worksheets/sheet"):
                text = data.decode("utf-8")
                text, count = re.subn(
                    r'(<c r="[A-Z]+\d+"[^>]*>)(?=<f)', r"\1\n  ", text
                )
                assert count
                data = text.encode("utf-8")
            archive.writestr(item, data)
    assert _final_child_formula_caches(patched) == expected


def test_email_results_survive_address_failure(
    screen_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Address failure cannot discard successful billable email work."""
    from ccat import screening
    from test_screening import _seed_workbook

    _seed_workbook(screen_cfg)
    monkeypatch.setattr(
        screening,
        "check_emails",
        lambda cfg, rows: [
            {
                "source_sheet": "sponsor",
                "zite_record_id": "rec_s1",
                "field": "email",
                "email": "sponsor@example.com",
                "status": "valid",
                "sub_status": "",
                "did_you_mean": "",
                "response_json": '{"status":"valid"}',
                "checked_at": "2026-11-17T00:00:00+00:00",
            }
        ],
    )

    def address_failure(cfg: Config, families: list[dict[str, Any]]) -> None:
        raise RuntimeError("address engine unavailable")

    monkeypatch.setattr(screening, "check_addresses", address_failure)
    with pytest.raises(RuntimeError, match="address engine"):
        screening.run(
            screen_cfg, skip_email_check=False, skip_address_check=False
        )
    log = read_sheet(screen_cfg.excel_file, "email_validation_log")
    assert log is not None and log.height == 1
    assert log["status"][0] == "valid"


def test_audit_starts_with_empty_raw_and_populated_finals(
    cfg: Config,
) -> None:
    """Schema-valid empty raw cannot hide preserved raw-missing entities."""
    from ccat.acquire import PL_DTYPES
    from ccat.audit.store import ensure_final_sheets
    from test_audit_store import (
        default_raw_families,
        default_raw_sponsors,
        seed_raw,
    )

    seed_raw(cfg)
    ensure_final_sheets(cfg)
    raw_schemas = {
        f"raw_{entity}": pl.DataFrame(
            schema={
                column: PL_DTYPES[spec.type]
                for column, spec in cfg.tables[entity].columns.items()
            }
        )
        for entity in ("sponsor", "family", "child")
    }
    write_sheets(cfg.excel_file, "acquire", raw_schemas)
    ensure_final_sheets(cfg)
    assert read_final(cfg, "child")
    # Reference fixture constructors remain exercised for schema compatibility.
    assert default_raw_families() and default_raw_sponsors()


def test_consolidation_metrics_use_real_raw_identity() -> None:
    """Raw presence compares Zite IDs for families and sponsors too."""
    import polars as pl

    from ccat.consolidate import _entity_metrics

    rows = [
        {"family_id": 1, "zite_record_id": "present", "ignore": False},
        {"family_id": 2, "zite_record_id": "missing", "ignore": False},
    ]
    raw = pl.DataFrame({"zite_record_id": ["present"]})
    metrics = _entity_metrics("family", rows, raw)
    assert metrics["raw_missing"] == 1
    assert metrics["keys"] == ["1", "2"]


def test_converted_copy_rehearsal_and_rollback(
    cfg: Config, tmp_path: Path
) -> None:
    """A produced workbook drives audit/readers and backup restoration works."""
    import shutil

    from ccat.audit.store import ensure_final_sheets, load_audit_data
    from ccat.child_info_generate import _load_enriched_rows
    from ccat.clarify.extract import build_table
    from ccat.consolidate import run as consolidate
    from ccat.match import run as match
    from ccat.stats import run as stats
    from test_consolidate import _golden_frames, _write_workbook

    source = tmp_path / "legacy.xlsx"
    _write_workbook(source, _golden_frames())
    output = tmp_path / "converted.xlsx"
    consolidation_cfg = cfg.model_copy(
        update={
            "excel_file": str(output),
            "match_report_csv": str(tmp_path / "match.csv"),
            "report": cfg.report.model_copy(
                update={"output_dir": str(tmp_path / "reports")}
            ),
        }
    )
    consolidation_cfg = consolidation_cfg.model_copy(
        update={
            "clarify": consolidation_cfg.clarify.model_copy(
                update={
                    "output_dir": str(tmp_path / "clarify"),
                    "clarifications_csv": str(tmp_path / "clarify.csv"),
                }
            )
        }
    )
    consolidate(consolidation_cfg, str(source), str(output))
    assert output.is_file()

    # Replace the intentionally minimal legacy fixture raw schema with the
    # full acquired schema, as a real v2 source would already contain.
    from test_audit_store import seed_raw

    seed_raw(consolidation_cfg)
    ensure_final_sheets(consolidation_cfg)
    audit_data = load_audit_data(consolidation_cfg)
    assert audit_data.children and audit_data.families
    match(consolidation_cfg)
    enriched, _context = _load_enriched_rows(consolidation_cfg)
    assert enriched.height > 0
    stats(consolidation_cfg)
    clarification = build_table(output)
    assert isinstance(clarification, pl.DataFrame)

    backup = tmp_path / "converted.backup.xlsx"
    shutil.copy2(output, backup)
    before = backup.read_bytes()
    match(
        consolidation_cfg,
        skip_match=True,
        overwrite=True,
    )
    assert output.read_bytes() != before
    shutil.copy2(backup, output)
    assert output.read_bytes() == before
    ensure_final_sheets(consolidation_cfg)
    assert read_final(consolidation_cfg, "child")


def test_workbook_report_commit_restores_output(
    cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A report failure after output replacement restores the prior bytes."""
    from ccat.consolidate import _commit_output_and_report

    output = tmp_path / "output.xlsx"
    output.write_bytes(b"old-workbook")
    candidate = tmp_path / "candidate.xlsx"
    candidate.write_bytes(b"new-workbook")
    report = tmp_path / "report.json"
    staging = tmp_path / "report.staged"
    staging.write_text("{}")
    calls: list[tuple[Path, Path]] = []
    real_replace = __import__("os").replace

    def replace(source: str | Path, target: str | Path) -> None:
        calls.append((Path(source), Path(target)))
        real_replace(source, target)
        if len(calls) == 2:
            raise OSError("report commit failed")

    monkeypatch.setattr("os.replace", replace)
    with pytest.raises(OSError, match="report commit"):
        _commit_output_and_report(output, candidate, report, staging)
    assert output.read_bytes() == b"old-workbook"
    assert not report.exists()
    assert not list(tmp_path.glob(".*.rollback"))
    assert not staging.exists()
