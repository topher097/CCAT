"""Tests for the DB-mode stats service (plan §9.6, D15).

File-backed SQLite in ``tmp_path``; stores seeded through ``migrate``
commits; the report itself renders via the reused legacy helpers (real
HTML/PDF rendering — no external service).
"""

import asyncio
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from conftest import load_test_cfg
from test_db_pipeline import child_row, family_row, sponsor_row
from ccat import stats as stats_module
from ccat.config import Config, StorageSpec
from ccat.db.engine import create_async_engine, run_migrations
from ccat.db.uow import UnitOfWork
from ccat.services import db_stats
from ccat.stats import StatsError

# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path, **updates: Any) -> Config:
    """Test config: temp DB/artifacts."""
    return load_test_cfg(
        excel_file=str(tmp_path / "wb.xlsx"),
        log_file=str(tmp_path / "ccat.log"),
        match_report_csv=str(tmp_path / "report.csv"),
        storage=StorageSpec(
            mode="db",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'ccat.sqlite3'}",
            artifact_root=str(tmp_path / "artifacts"),
        ),
    )


def _prepare(tmp_path: Path, **updates: Any) -> Config:
    """Migrate the temp database and return its config."""
    cfg = _cfg(tmp_path, **updates)
    run_migrations(cfg.storage.database_url)
    return cfg


def _stats_child(record_id: str, created: str, **overrides: Any) -> dict:
    """One final child with a usable created timestamp."""
    return child_row(
        record_id,
        created=created,
        gender=overrides.pop("gender", "Male"),
        **overrides,
    )


def _raw_child(
    record_id: str, created: str, **overrides: Any
) -> dict[str, Any]:
    """One verbatim raw-child row."""
    return {
        "zite_record_id": record_id,
        "zite_created_at": created,
        "created": created,
        "first_name": "Kiddo",
        "primary_email": f"{record_id}@example.com",
        "dob": "2018-05-10",
        "gender": "Male",
        "source": "web",
        **overrides,
    }


def _raw_family(
    record_id: str, created: str, **overrides: Any
) -> dict[str, Any]:
    """One verbatim raw-family row."""
    return {
        "zite_record_id": record_id,
        "zite_created_at": created,
        "created": created,
        "primary_email": f"{record_id}@example.com",
        "first_name": "Jane",
        "last_name": "Doe",
        "gift_pickup_location": "TSA (Hancock)",
        "source": "web",
        **overrides,
    }


def _raw_sponsor(
    record_id: str, created: str, **overrides: Any
) -> dict[str, Any]:
    """One verbatim raw-sponsor row."""
    return {
        "zite_record_id": record_id,
        "create": created,
        "email": f"{record_id}@example.com",
        "first_name": "Mary",
        "last_name": "Sponsor",
        "num_children": 2,
        "age_preference": "Any Age",
        "gender_preference": "Any Gender",
        "dropoff_location": "Hancock",
        "source": "web",
        **overrides,
    }


async def _seed(cfg: Config, *, raw: Any = None, **stores: Any) -> None:
    """Seed raw snapshot and/or final stores through migrate ownership."""
    uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
    try:
        async with uow.store(cfg, "migration") as repo:
            if raw is not None:
                await repo.replace_raw_snapshot("migrate", raw, "migration")
            for key, entity in (
                ("children", "child"),
                ("families", "family"),
                ("sponsors", "sponsor"),
            ):
                rows = stores.get(key)
                if rows:
                    await repo.commit_finals(
                        "migrate", entity, rows, "migration"
                    )
    finally:
        await uow.engine.dispose()


def _run(cfg: Config, options: dict[str, Any] | None = None) -> Any:
    """Run the stats service synchronously."""
    return asyncio.run(
        db_stats.stats(
            cfg, "topher", options or {}, lambda *a, **k: None, lambda: None
        )
    )


# ---------------------------------------------------------------------------
# Modes.
# ---------------------------------------------------------------------------


def test_default_mode_uses_eligible_finals(tmp_path: Path) -> None:
    """Default mode: orphans/ignored excluded, assignments counted."""
    cfg = _prepare(tmp_path)
    children = [
        _stats_child("c1", "2026-02-01T09:00:00Z"),
        _stats_child("c2", "2026-02-02T09:00:00Z", gender="Female"),
        child_row(
            "c3",
            created="2026-02-03T09:00:00Z",
            family_id=None,
            status="audited",
        ),  # orphan
        child_row(
            "c4",
            created="2026-02-04T09:00:00Z",
            status="audited",
            ignore=True,
        ),  # ignored
    ]
    families = [family_row(1, assigned_sponsor_id=7)]
    sponsors = [sponsor_row(7, num_children=3, assigned_angel_ids="1A,1B,1C")]
    asyncio.run(
        _seed(cfg, children=children, families=families, sponsors=sponsors)
    )
    result = _run(cfg, {})
    assert result.html_written and result.pdf_written
    plain = " ".join(Path(result.html_path).read_text().split())
    # Two eligible children, one assigned sponsor.
    assert "2 children, 1 families, 1 sponsors with assigned angels." in plain


def test_raw_mode_ephemeral_no_orphan_filter_zero_assignments(
    tmp_path: Path,
) -> None:
    """--raw: every prepared child counts, assignments stay zero."""
    cfg = _prepare(tmp_path)
    raw = {
        "child": [
            _raw_child("r1", "2026-02-01T09:00:00+00:00"),
            _raw_child(
                "orphan-1",
                "2026-02-02T09:00:00+00:00",
                primary_email="nowhere@example.com",
            ),
        ],
        "family": [_raw_family("f1", "2026-01-05T09:00:00+00:00")],
        "sponsor": [_raw_sponsor("s1", "2026-01-01T09:00:00+00:00")],
    }
    asyncio.run(_seed(cfg, raw=raw))
    result = _run(cfg, {"raw": True})
    plain = " ".join(Path(result.html_path).read_text().split())
    # No output ignore/orphan filtering: both prepared children count.
    assert "2 children" in plain
    # Prepared families carry no assigned sponsor.
    assert "0 sponsors with assigned angels." in plain


def test_raw_mode_reconciles_ids_against_finals(tmp_path: Path) -> None:
    """Raw mode is reconciled, not identity-independent (§9.6)."""
    cfg = _prepare(tmp_path)
    families = [
        family_row(
            1,
            assigned_sponsor_id=None,
            zite_record_id="raw-f1",
        )
    ]
    children = [_stats_child("c1", "2026-02-01T09:00:00Z")]
    asyncio.run(
        _seed(
            cfg,
            children=children,
            families=families,
            raw={
                "child": [
                    _raw_child(
                        "c1",
                        "2026-02-01T09:00:00+00:00",
                        primary_email="fam1@example.com",
                    )
                ],
                "family": [
                    _raw_family(
                        "raw-f1",
                        "2026-01-05T09:00:00+00:00",
                        primary_email="fam1@example.com",
                    )
                ],
                "sponsor": [],
            },
        )
    )
    enriched, _family, _sponsor, _locations = db_stats._load_enriched(
        cfg,
        {e: asyncio.run(_rows(cfg, e)) for e in ("child", "family", "sponsor")},
        {
            "child": [_raw_child("c1", "2026-02-01T09:00:00+00:00")],
            "family": [_raw_family("raw-f1", "2026-01-05T09:00:00+00:00")],
            "sponsor": [],
        },
        raw=True,
    )
    # The raw child reconciled onto the stored stable identity c1 (same
    # zite_record_id), not a fresh provisional id.
    assert enriched["zite_record_id"].to_list() == ["c1"]


async def _rows(cfg: Config, entity: str) -> list[dict[str, Any]]:
    """Read one final store through a throwaway repository."""
    uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
    try:
        async with uow.store(cfg, "system") as repo:
            return await repo.final_rows(entity)
    finally:
        await uow.engine.dispose()


def test_empty_child_population_raises(tmp_path: Path) -> None:
    """An empty eligible population fails with the legacy typed error."""
    cfg = _prepare(tmp_path)
    with pytest.raises(StatsError, match="final_child sheet"):
        _run(cfg, {})


# ---------------------------------------------------------------------------
# Calculations.
# ---------------------------------------------------------------------------


def test_daily_admissions_zero_filled(tmp_path: Path) -> None:
    """The horizon zero-fills every calendar day per location (§9.6)."""
    cfg = _prepare(tmp_path)
    children = [
        _stats_child("c1", "2026-02-01T09:00:00Z"),  # family 1 -> Hancock
        _stats_child(
            "c2",
            "2026-02-03T09:00:00Z",
            family_id=2,
            gender="Female",
        ),  # family 2 -> Calumet
    ]
    families = [
        family_row(1),
        family_row(2, gift_pickup_location="CCAM (Calumet)"),
    ]
    asyncio.run(_seed(cfg, children=children, families=families))
    finals = {
        e: asyncio.run(_rows(cfg, e)) for e in ("child", "family", "sponsor")
    }
    enriched, family, sponsor, _locations = db_stats._load_enriched(
        cfg, finals, raw=False
    )
    days, per_location = db_stats._daily_admissions(
        enriched, date(2026, 2, 4), "final_child"
    )
    assert days == [
        date(2026, 2, 1),
        date(2026, 2, 2),
        date(2026, 2, 3),
        date(2026, 2, 4),
    ]
    hancock = per_location["Hancock"]
    calumet = per_location["Calumet"]
    assert hancock == [1, 0, 0, 0]
    assert calumet == [0, 0, 1, 0]


def test_requested_angels_vs_assigned_sponsors(tmp_path: Path) -> None:
    """Requested num_children and assigned sponsor counts stay distinct."""
    cfg = _prepare(tmp_path)
    children = [
        _stats_child("c1", "2026-02-01T09:00:00Z"),
        _stats_child("c2", "2026-02-02T09:00:00Z", gender="Female"),
    ]
    families = [family_row(1, assigned_sponsor_id=7)]
    sponsors = [sponsor_row(7, num_children=5, assigned_angel_ids="1A,1B")]
    asyncio.run(
        _seed(cfg, children=children, families=families, sponsors=sponsors)
    )
    finals = {
        e: asyncio.run(_rows(cfg, e)) for e in ("child", "family", "sponsor")
    }
    enriched, family, sponsor, locations = db_stats._load_enriched(
        cfg, finals, raw=False
    )
    table = stats_module._location_table(
        enriched, family, sponsor, locations, ["male", "female"]
    )
    hancock = next(row for row in table if row["label"] == "Hancock")
    # "Sponsors" sums the requested num_children (5), "sponsored" counts
    # the assigned angels (2).
    assert hancock["sponsors"] == 5
    assert hancock["sponsored"] == 2


# ---------------------------------------------------------------------------
# Timezone (D15).
# ---------------------------------------------------------------------------


def test_report_today_local_matches_date_today(tmp_path: Path) -> None:
    """``local`` keeps today's date.today() behavior."""
    cfg = _cfg(tmp_path)
    assert db_stats._report_today(cfg) == date.today()


def test_report_today_iana_zone(tmp_path: Path) -> None:
    """An IANA key dates the report in that zone."""
    cfg = _cfg(
        tmp_path,
        report=load_test_cfg().report.model_copy(
            update={"timezone": "America/Detroit"}
        ),
    )
    assert (
        db_stats._report_today(cfg)
        == datetime.now(ZoneInfo("America/Detroit")).date()
    )


def test_filename_and_generated_date_use_zone(tmp_path: Path) -> None:
    """The artifact stem embeds the zoned report day (D15)."""
    cfg = _prepare(
        tmp_path,
        report=load_test_cfg().report.model_copy(
            update={"timezone": "America/Detroit"}
        ),
    )
    children = [_stats_child("c1", "2026-02-01T09:00:00Z")]
    families = [family_row(1)]
    asyncio.run(_seed(cfg, children=children, families=families))
    result = _run(cfg, {})
    expected_stem = f"report_{db_stats._report_today(cfg).isoformat()}"
    assert result.html_path.endswith(f"{expected_stem}.html")


# ---------------------------------------------------------------------------
# Artifacts and partial state.
# ---------------------------------------------------------------------------


def test_artifacts_registered_complete(tmp_path: Path) -> None:
    """A complete run registers HTML and PDF artifacts (§9.6)."""
    cfg = _prepare(tmp_path)
    children = [_stats_child("c1", "2026-02-01T09:00:00Z")]
    families = [family_row(1)]
    asyncio.run(_seed(cfg, children=children, families=families))
    _run(cfg, {})
    artifacts = asyncio.run(_artifacts(cfg))
    kinds = [artifact["kind"] for artifact in artifacts]
    assert kinds == ["stats_report", "stats_report"]
    metas = [artifact["meta"] for artifact in artifacts]
    assert metas == [
        {"complete": True, "mode": "final"},
        {"complete": True},
    ]
    assert (tmp_path / "artifacts" / "reports").is_dir()


async def _artifacts(cfg: Config) -> list[dict[str, Any]]:
    """List artifact rows through a throwaway repository."""
    uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
    try:
        async with uow.store(cfg, "system") as repo:
            return await repo.artifact_rows()
    finally:
        await uow.engine.dispose()


def test_pdf_failure_keeps_html_partial_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PDF failure keeps the HTML as an explicit partial artifact."""
    cfg = _prepare(tmp_path)
    children = [_stats_child("c1", "2026-02-01T09:00:00Z")]
    families = [family_row(1)]
    asyncio.run(_seed(cfg, children=children, families=families))

    def broken_pdf(html: str, pdf_path: Any) -> None:
        raise StatsError("boom")

    monkeypatch.setattr(stats_module, "_render_pdf", broken_pdf)
    result = _run(cfg, {})
    assert result.html_written is True
    assert result.pdf_written is False
    assert result.pdf_path is None
    assert Path(result.html_path).is_file()
    artifacts = asyncio.run(_artifacts(cfg))
    assert len(artifacts) == 1
    assert artifacts[0]["meta"] == {
        "complete": False,
        "mode": "final",
    }
