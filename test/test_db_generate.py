"""Tests for the DB-mode generate service (plan §9.5, D18/D20).

File-backed SQLite in ``tmp_path``; the LibreOffice conversion runs
through the module's convert seam, faked with pypdf-written tiny PDFs
(the ``test_child_info_generate`` pattern).  Stores are seeded through
``migrate`` commits.
"""

import asyncio
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any
from collections.abc import Callable

import pytest
from pypdf import PdfReader, PdfWriter

from conftest import load_test_cfg
from test_db_pipeline import child_row, family_row, sponsor_row
from ccat.child_info_generate import (
    InfoSheetCancelledError,
    InfoSheetError,
)
from ccat.config import Config, StorageSpec, WorkerSpec
from ccat.db.engine import create_async_engine, run_migrations
from ccat.db.uow import UnitOfWork
from ccat.jobs import JobCancelRequested
from ccat.services import db_generate

# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path, **updates: Any) -> Config:
    """Test config: temp DB/artifacts, stub template, real templates."""
    base = load_test_cfg(
        excel_file=str(tmp_path / "wb.xlsx"),
        log_file=str(tmp_path / "ccat.log"),
        match_report_csv=str(tmp_path / "report.csv"),
        storage=StorageSpec(
            mode="db",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'ccat.sqlite3'}",
            artifact_root=str(tmp_path / "artifacts"),
        ),
        worker=WorkerSpec(
            poll_interval_seconds=0.01,
            heartbeat_seconds=0.02,
            lease_seconds=5.0,
            max_attempts=2,
            progress_throttle_seconds=0.0,
        ),
    )
    template = tmp_path / "template.ott"
    template.touch()
    return base.model_copy(
        update={
            "info_sheet": base.info_sheet.model_copy(
                update={
                    "template_file": str(template),
                    "output_dir": str(tmp_path / "sheets"),
                }
            ),
            "sponsor_packets": base.sponsor_packets.model_copy(
                update={"output_dir": str(tmp_path / "packets")}
            ),
        }
        | updates
    )


def _prepare(tmp_path: Path, **updates: Any) -> Config:
    """Migrate the temp database and return its config."""
    cfg = _cfg(tmp_path, **updates)
    run_migrations(cfg.storage.database_url)
    return cfg


def _db_path(cfg: Config) -> str:
    """Return the raw sqlite path behind the config's async URL."""
    return cfg.storage.database_url.split("sqlite+aiosqlite:///", 1)[1]


def _sql(cfg: Config, query: str, params: tuple[Any, ...] = ()) -> list[tuple]:
    """One sync sqlite read."""
    connection = sqlite3.connect(_db_path(cfg), timeout=5)
    try:
        return list(connection.execute(query, params))
    finally:
        connection.close()


async def _seed(
    cfg: Config,
    *,
    children: list[dict[str, Any]] | None = None,
    families: list[dict[str, Any]] | None = None,
    sponsors: list[dict[str, Any]] | None = None,
) -> None:
    """Seed final stores through migrate ownership.

    A child without its own ``assigned_sponsor_id`` inherits its
    family's, so a test can state the assignment at either grain.
    """
    family_sponsors = {
        family.get("family_id"): family.get("assigned_sponsor_id")
        for family in families or []
    }
    for child in children or []:
        if not child.get("assigned_sponsor_id"):
            child["assigned_sponsor_id"] = family_sponsors.get(
                child.get("family_id")
            )
    uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
    try:
        async with uow.store(cfg, "migration") as repo:
            for entity, rows in (
                ("child", children),
                ("family", families),
                ("sponsor", sponsors),
            ):
                if rows:
                    await repo.commit_finals(
                        "migrate", entity, rows, "migration"
                    )
    finally:
        await uow.engine.dispose()


def _angel_child(record_id: str, angel_id: str, **overrides: Any) -> dict:
    """Build an audited, sponsored-pending child in family 1."""
    overrides.setdefault("status", "audited")
    return child_row(
        record_id,
        angel_id=angel_id,
        family_id=overrides.pop("family_id", 1),
        **overrides,
    )


def _family(family_id: int = 1, **overrides: Any) -> dict[str, Any]:
    """One matched family row (assigned sponsor 7 by default)."""
    overrides.setdefault("assigned_sponsor_id", 7)
    return family_row(family_id, **overrides)


def _assigned_sponsor(sponsor_id: int = 7, **overrides: Any) -> dict[str, Any]:
    """One sponsor with assigned angels."""
    return sponsor_row(sponsor_id, **overrides)


def _instructions_pdf(tmp_path: Path) -> str:
    """Write a minimal real instruction PDF; return its path."""
    path = tmp_path / "instructions.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with path.open("wb") as output:
        writer.write(output)
    return str(path)


@pytest.fixture
def fake_generation(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Fake the render + convert seams: tiny pypdf PDFs, both recorded."""

    class _Record:
        renders: list[dict[str, Any]] = []
        converts: list[str] = []

    def fake_render(
        template_file: Path, data: dict[str, Any], odt_path: Path
    ) -> None:
        _Record.renders.append(data)

    def fake_convert(
        odt_path: Path,
        out_dir: Path,
        soffice_binary: str,
        profile: Path,
        timeout: float,
        stop: threading.Event | None = None,
    ) -> Path:
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        pdf_path = out_dir / f"{odt_path.stem}.pdf"
        with pdf_path.open("wb") as output:
            writer.write(output)
        _Record.converts.append(odt_path.stem)
        return pdf_path

    monkeypatch.setattr(db_generate, "_render_one", fake_render)
    monkeypatch.setattr(db_generate, "_convert_to_pdf", fake_convert)
    return _Record


# Backwards-compat alias used by the packet tests (convert stems only).
@pytest.fixture
def fake_convert(fake_generation: Any) -> list[str]:
    """Record convert-seam stems from :func:`fake_generation`."""
    return fake_generation.converts


@pytest.fixture
def packets_cfg(tmp_path: Path) -> Callable[[Config], Config]:
    """Point the packet template/instructions at the real repo files."""

    def _with(cfg: Config) -> Config:
        packets = cfg.sponsor_packets.model_copy(
            update={"instructions_file": _instructions_pdf(tmp_path)}
        )
        return cfg.model_copy(update={"sponsor_packets": packets})

    return _with


def _run(cfg: Config, options: dict[str, Any] | None = None) -> Any:
    """Run generate() synchronously."""
    return asyncio.run(
        db_generate.generate(
            cfg, "topher", options or {}, lambda *a, **k: None, lambda: None
        )
    )


# ---------------------------------------------------------------------------
# Sheet eligibility and enrichment.
# ---------------------------------------------------------------------------


def test_eligibility_matrix(tmp_path: Path, fake_convert: list[str]) -> None:
    """Only audited + sponsored + unflagged children are pending."""
    cfg = _prepare(tmp_path)
    children = [
        _angel_child("c1", "1A", status="new"),  # unaudited
        _angel_child("c2", "1B"),  # pending
        _angel_child("c3", "1C", family_id=2),  # unsponsored family
        _angel_child("c4", "1D", generated_info_sheet=True),  # done
        child_row(
            "c5", angel_id="1E", family_id=1, status="audited", ignore=True
        ),  # ignored
    ]
    families = [
        _family(1),
        family_row(2, assigned_sponsor_id=None),
    ]
    asyncio.run(
        _seed(
            cfg,
            children=children,
            families=families,
            sponsors=[_assigned_sponsor()],
        )
    )
    result = _run(cfg, {"test": True, "overwrite": False})
    assert fake_convert == [f"AT{cfg.season}_1B"]
    assert result.sheets_generated == [f"AT{cfg.season}_1B"]
    assert result.sheets_failed == []


def test_overwrite_regenerates_flagged_but_never_unsponsored(
    tmp_path: Path, fake_convert: list[str]
) -> None:
    """--overwrite re-renders flagged rows; unsponsored stay skipped."""
    cfg = _prepare(tmp_path)
    children = [
        _angel_child("c1", "1A", generated_info_sheet=True),
        _angel_child("c2", "1B", family_id=2),
    ]
    families = [
        _family(1),
        family_row(2, assigned_sponsor_id=None),
    ]
    asyncio.run(
        _seed(
            cfg,
            children=children,
            families=families,
            sponsors=[_assigned_sponsor()],
        )
    )
    result = _run(cfg, {"overwrite": True})
    assert fake_convert == [f"AT{cfg.season}_1A"]
    assert result.sheets_skipped == []


def test_sponsor_scope_limits_sheets(
    tmp_path: Path, fake_convert: list[str]
) -> None:
    """A scoped run renders only that sponsor's pending children."""
    cfg = _prepare(tmp_path)
    children = [
        _angel_child("c1", "1A"),
        _angel_child("c2", "2A", family_id=2),
    ]
    families = [_family(1), _family(2, assigned_sponsor_id=8)]
    asyncio.run(
        _seed(
            cfg,
            children=children,
            families=families,
            sponsors=[
                _assigned_sponsor(7),
                _assigned_sponsor(8, assigned_angel_ids="2A"),
            ],
        )
    )
    _run(cfg, {"sponsor": 8})
    assert fake_convert == [f"AT{cfg.season}_2A"]


def test_family_size_counts_eligible_population_only(
    tmp_path: Path,
    fake_convert: list[str],
    fake_generation: Any,
) -> None:
    """family_size joins from the enriched eligible population (§9.5)."""
    cfg = _prepare(tmp_path)
    children = [
        # Family 1: one pending, one already-generated, one unaudited —
        # all eligible, so family_size is 3.
        _angel_child("c1", "1A"),
        _angel_child("c2", "1B", generated_info_sheet=True),
        _angel_child("c3", "1C", status="new"),
        # Excluded from the eligible population entirely:
        child_row(
            "c4",
            angel_id="1D",
            family_id=1,
            status="audited",
            ignore=True,
        ),
        child_row("c5", angel_id="1E", family_id=None, status="audited"),
    ]
    asyncio.run(
        _seed(
            cfg,
            children=children,
            families=[_family(1)],
            sponsors=[_assigned_sponsor()],
        )
    )
    _run(cfg, {"test": True})  # packet validation is out of scope here
    data = next(
        call
        for call in fake_generation.renders
        if call.get("info", {}).get("angel_id") == "1A"
    )
    assert data["info"]["family_size"] == 3
    assert data["sponsor"]["family_size"] == 3


def test_placeholder_all_or_nothing(
    tmp_path: Path,
    fake_convert: list[str],
    fake_generation: Any,
) -> None:
    """A listed-affiliation sponsor missing contact placeholders all three."""
    cfg = _prepare(tmp_path)
    children = [_angel_child("c1", "1A")]
    asyncio.run(
        _seed(
            cfg,
            children=children,
            families=[_family(1)],
            sponsors=[
                _assigned_sponsor(
                    sponsor_type="Organization",
                    email=None,
                    phone=None,
                    first_name=None,
                    last_name=None,
                )
            ],
        )
    )
    _run(cfg, {"test": True})
    data = fake_generation.renders[0]
    placeholder = "_" * cfg.info_sheet.contact_placeholder_length
    assert data["sponsor"]["full_name"] == placeholder
    assert data["sponsor"]["email"] == placeholder
    assert data["sponsor"]["phone"] == placeholder
    assert data["sponsor"]["sponsor_id"] == 7


# ---------------------------------------------------------------------------
# Per-item durability (D20).
# ---------------------------------------------------------------------------


def test_marker_commit_visible_mid_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 1's marker/artifact commit before item 2 finishes (D20)."""
    cfg = _prepare(tmp_path)
    children = [_angel_child("c1", "1A"), _angel_child("c2", "1B")]
    asyncio.run(
        _seed(
            cfg,
            children=children,
            families=[_family(1)],
            sponsors=[_assigned_sponsor()],
        )
    )
    release = threading.Event()
    observed: dict[str, Any] = {}

    def fake_convert(
        odt_path: Path,
        out_dir: Path,
        soffice_binary: str,
        profile: Path,
        timeout: float,
        stop: threading.Event | None = None,
    ) -> Path:
        if odt_path.stem.endswith("1B"):
            release.wait(5)
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        pdf_path = out_dir / f"{odt_path.stem}.pdf"
        with pdf_path.open("wb") as output:
            writer.write(output)
        return pdf_path

    monkeypatch.setattr(db_generate, "_render_one", lambda *a: None)
    monkeypatch.setattr(db_generate, "_convert_to_pdf", fake_convert)

    def progress(
        phase: str, completed: int = 0, failed: int = 0, total: Any = None
    ) -> None:
        if phase == "sheets" and completed == 1:
            # Mid-run: item 1 is fully durable already.
            markers = _sql(
                cfg,
                "SELECT zite_record_id, generated_info_sheet FROM final_child"
                " ORDER BY zite_record_id",
            )
            observed["markers"] = dict(markers)
            observed["artifacts"] = _sql(
                cfg, "SELECT kind, path FROM artifacts ORDER BY id"
            )
            observed["pdf_pages"] = len(
                PdfReader(
                    str(
                        tmp_path
                        / "artifacts"
                        / "child_sheets"
                        / "hancock"
                        / f"AT{cfg.season}_1A.pdf"
                    )
                ).pages
            )
            release.set()  # let item 2 finish

    asyncio.run(db_generate.generate(cfg, "topher", {}, progress, lambda: None))
    # Mid-run observation: c1 was already durable (marker + artifact)
    # while c2 was still converting.
    assert observed["markers"] == {"c1": 1, "c2": 0}
    assert observed["artifacts"][:1] == [
        ("child_sheet", f"child_sheets/hancock/AT{cfg.season}_1A.pdf")
    ]
    assert observed["pdf_pages"] >= 1
    # After the run c2's marker committed too.
    final = dict(
        _sql(
            cfg,
            "SELECT zite_record_id, generated_info_sheet FROM final_child"
            " ORDER BY zite_record_id",
        )
    )
    assert final == {"c1": 1, "c2": 1}


def test_failed_item_reported_and_others_continue(
    tmp_path: Path, fake_generation: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed item lands in sheets_failed; the rest still commit."""
    cfg = _prepare(tmp_path)
    children = [_angel_child("c1", "1A"), _angel_child("c2", "1B")]
    asyncio.run(
        _seed(
            cfg,
            children=children,
            families=[_family(1)],
            sponsors=[_assigned_sponsor()],
        )
    )

    def fake_convert(
        odt_path: Path,
        out_dir: Path,
        soffice_binary: str,
        profile: Path,
        timeout: float,
        stop: threading.Event | None = None,
    ) -> Path:
        if odt_path.stem.endswith("1A"):
            raise InfoSheetError("boom")
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        pdf_path = out_dir / f"{odt_path.stem}.pdf"
        with pdf_path.open("wb") as output:
            writer.write(output)
        return pdf_path

    monkeypatch.setattr(db_generate, "_convert_to_pdf", fake_convert)
    # Packets stay out of scope: this test is the sheet-phase
    # partial-success behavior.
    monkeypatch.setattr(
        db_generate, "_sponsors_with_angels", lambda *a, **k: []
    )
    result = _run(cfg, {})
    assert result.sheets_failed == [f"AT{cfg.season}_1A"]
    assert result.sheets_generated == [f"AT{cfg.season}_1B"]
    rows = _sql(
        cfg,
        "SELECT zite_record_id, generated_info_sheet FROM final_child"
        " ORDER BY zite_record_id",
    )
    assert dict(rows) == {"c1": 0, "c2": 1}


def test_cancel_after_first_item_keeps_completed(
    tmp_path: Path, fake_generation: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation keeps completed items; the job ends cancelled (§6.2)."""
    cfg = _prepare(tmp_path)
    children = [_angel_child("c1", "1A"), _angel_child("c2", "1B")]
    asyncio.run(
        _seed(
            cfg,
            children=children,
            families=[_family(1)],
            sponsors=[_assigned_sponsor()],
        )
    )

    def fake_convert(
        odt_path: Path,
        out_dir: Path,
        soffice_binary: str,
        profile: Path,
        timeout: float,
        stop: threading.Event | None = None,
    ) -> Path:
        if odt_path.stem.endswith("1B"):
            # Block until the stop event fires, then cancel like the
            # real converter's poll loop does.
            if stop is None or not stop.wait(5):
                raise InfoSheetError("test convert never stopped")
            raise InfoSheetCancelledError("cancelled")
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        pdf_path = out_dir / f"{odt_path.stem}.pdf"
        with pdf_path.open("wb") as output:
            writer.write(output)
        return pdf_path

    monkeypatch.setattr(db_generate, "_convert_to_pdf", fake_convert)
    state = {"after_first": False}

    def cancel() -> None:
        if state["after_first"]:
            raise JobCancelRequested()

    def progress(
        phase: str, completed: int = 0, failed: int = 0, total: Any = None
    ) -> None:
        if phase == "sheets" and completed == 1:
            state["after_first"] = True

    with pytest.raises(JobCancelRequested):
        asyncio.run(
            db_generate.generate(
                cfg, "topher", {"max_workers": 1}, progress, cancel
            )
        )
    rows = _sql(
        cfg,
        "SELECT zite_record_id, generated_info_sheet FROM final_child"
        " ORDER BY zite_record_id",
    )
    # c1 completed and stayed committed; c2's conversion was abandoned.
    assert dict(rows) == {"c1": 1, "c2": 0}
    assert (
        tmp_path
        / "artifacts"
        / "child_sheets"
        / "hancock"
        / f"AT{cfg.season}_1A.pdf"
    ).is_file()


def test_rerun_skips_marker_but_reconciles_orphan_artifact(
    tmp_path: Path, fake_convert: list[str]
) -> None:
    """A false marker with an existing file regenerates safely (D20)."""
    cfg = _prepare(tmp_path)
    children = [_angel_child("c1", "1A"), _angel_child("c2", "1B")]
    asyncio.run(
        _seed(
            cfg,
            children=children,
            families=[_family(1)],
            sponsors=[_assigned_sponsor()],
        )
    )
    first = _run(cfg, {})
    # Sheets are generated concurrently, so the order is not a contract.
    assert sorted(first.sheets_generated) == [
        f"AT{cfg.season}_1A",
        f"AT{cfg.season}_1B",
    ]

    # Simulate the §6.3 crash window: the artifact exists but the
    # marker commit never happened for c2.
    async def reset_marker() -> None:
        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "migration") as repo:
                rows = await repo.final_rows("child")
                for row in rows:
                    if row["zite_record_id"] == "c2":
                        row["generated_info_sheet"] = False
                await repo.commit_finals("migrate", "child", rows, "migration")
        finally:
            await uow.engine.dispose()

    asyncio.run(reset_marker())
    fake_convert.clear()
    second = _run(cfg, {})
    # c1 is skipped (true marker); the orphan artifact for c2 is
    # regenerated (overwritten) and re-registered.
    assert fake_convert == [f"AT{cfg.season}_1B"]
    assert second.sheets_generated == [f"AT{cfg.season}_1B"]
    rows = _sql(
        cfg,
        "SELECT zite_record_id, generated_info_sheet FROM final_child"
        " ORDER BY zite_record_id",
    )
    assert dict(rows) == {"c1": 1, "c2": 1}


def test_test_mode_single_sheet_no_packets(
    tmp_path: Path, fake_convert: list[str]
) -> None:
    """--test renders one sheet and skips packets entirely."""
    cfg = _prepare(tmp_path)
    children = [_angel_child("c1", "1A"), _angel_child("c2", "1B")]
    asyncio.run(
        _seed(
            cfg,
            children=children,
            families=[_family(1)],
            sponsors=[
                _assigned_sponsor(
                    assigned_angel_ids="1A,1B", num_angels_assigned=2
                )
            ],
        )
    )
    result = _run(cfg, {"test": True})
    assert fake_convert == [f"AT{cfg.season}_1A"]
    assert result.packets_built == 0
    assert not (tmp_path / "artifacts" / "sponsor_packets").exists()


def test_timeout_config_reaches_convert(
    tmp_path: Path, fake_generation: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The configured conversion timeout reaches the convert seam."""
    base = _prepare(tmp_path)
    cfg = base.model_copy(
        update={
            "info_sheet": base.info_sheet.model_copy(
                update={"conversion_timeout_seconds": 123.5}
            )
        }
    )
    children = [_angel_child("c1", "1A")]
    asyncio.run(
        _seed(
            cfg,
            children=children,
            families=[_family(1)],
            sponsors=[_assigned_sponsor()],
        )
    )
    seen: list[float] = []

    def fake_convert(
        odt_path: Path,
        out_dir: Path,
        soffice_binary: str,
        profile: Path,
        timeout: float,
        stop: threading.Event | None = None,
    ) -> Path:
        seen.append(timeout)
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        pdf_path = out_dir / f"{odt_path.stem}.pdf"
        with pdf_path.open("wb") as output:
            writer.write(output)
        return pdf_path

    monkeypatch.setattr(db_generate, "_convert_to_pdf", fake_convert)
    _run(cfg, {})
    assert seen == [123.5]


def test_convert_timeout_kills_process(tmp_path: Path) -> None:
    """The real converter terminates a hung soffice at the deadline."""
    slow = tmp_path / "slow_soffice"
    slow.write_text("#!/bin/sh\nexec sleep 30\n")
    slow.chmod(0o755)
    odt_path = tmp_path / "x.odt"
    odt_path.write_bytes(b"odt")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    profile = tmp_path / "profile"
    profile.mkdir()
    import time

    started = time.monotonic()
    with pytest.raises(InfoSheetError, match="timed out"):
        db_generate._convert_to_pdf(odt_path, out_dir, str(slow), profile, 1.0)
    assert time.monotonic() - started < 10


def test_max_workers_bounds(tmp_path: Path) -> None:
    """Worker counts outside 1..16 fail with a typed error (§9.5)."""
    cfg = _prepare(tmp_path)
    for bad in (0, -1, 17):
        with pytest.raises(InfoSheetError, match="max_workers"):
            _run(cfg, {"max_workers": bad})


# ---------------------------------------------------------------------------
# Packets (second phase).
# ---------------------------------------------------------------------------


def _full_seed(cfg: Config, tmp_path: Path) -> None:
    """Two audited sponsored children with an assigned sponsor."""
    children = [
        _angel_child("c1", "1A"),
        _angel_child("c2", "1B"),
        _angel_child("c3", "1Z"),
        _angel_child("c4", "1AA"),
    ]
    asyncio.run(
        _seed(
            cfg,
            children=children,
            families=[_family(1)],
            sponsors=[
                _assigned_sponsor(
                    assigned_angel_ids="1AA,1Z,1A,1B", num_angels_assigned=4
                )
            ],
        )
    )


def test_packet_artifacts_registered(
    tmp_path: Path,
    packets_cfg: Any,
    fake_convert: list[str],
) -> None:
    """A full run registers sheet and packet artifacts (§6.3)."""
    cfg = packets_cfg(_prepare(tmp_path))
    _full_seed(cfg, tmp_path)
    result = _run(cfg, {})
    assert result.packets_built == 1
    packet_dir = tmp_path / "artifacts" / "sponsor_packets" / "7"
    assert (packet_dir / "sponsor_packet_7.html").is_file()
    assert (
        packet_dir / f"Sponsor 7 - Child Information {cfg.season}.pdf"
    ).is_file()
    assert Path(cfg.sponsor_packets.instructions_file).name in {
        p.name for p in packet_dir.iterdir()
    }
    kinds = _sql(cfg, "SELECT DISTINCT kind FROM artifacts ORDER BY kind")
    assert {kind for (kind,) in kinds} >= {
        "child_sheet",
        "sponsor_packet",
        "sponsor_instructions",
    }


def test_packets_only_generates_missing_sheets_before_packet(
    tmp_path: Path,
    packets_cfg: Any,
    fake_convert: list[str],
) -> None:
    """A scoped packets-only run builds child sheets before its packet."""
    cfg = packets_cfg(_prepare(tmp_path))
    _full_seed(cfg, tmp_path)

    result = _run(cfg, {"only": "packets", "sponsor": 7})

    stems = [
        f"AT{cfg.season}_{angel_id}" for angel_id in ("1A", "1B", "1Z", "1AA")
    ]
    assert sorted(result.sheets_generated) == sorted(stems)
    assert sorted(fake_convert) == sorted(stems)
    for stem in stems:
        assert (
            tmp_path / "artifacts" / "child_sheets" / "hancock" / f"{stem}.pdf"
        ).is_file()
    assert result.packets_built == 1
    assert (
        tmp_path
        / "artifacts"
        / "sponsor_packets"
        / "7"
        / f"Sponsor 7 - Child Information {cfg.season}.pdf"
    ).is_file()


def test_packets_only_overwrite_regenerates_sheets_and_packet_files(
    tmp_path: Path,
    packets_cfg: Any,
    fake_convert: list[str],
) -> None:
    """Packets-only overwrite re-renders all sheets and packet files."""
    cfg = packets_cfg(_prepare(tmp_path))
    _full_seed(cfg, tmp_path)
    options = {"only": "packets", "sponsor": 7}
    _run(cfg, options)
    sheet_artifacts_before = _sql(
        cfg, "SELECT COUNT(*) FROM artifacts WHERE kind = 'child_sheet'"
    )[0][0]
    packet_dir = tmp_path / "artifacts" / "sponsor_packets" / "7"
    html_path = packet_dir / "sponsor_packet_7.html"
    pdf_path = packet_dir / f"Sponsor 7 - Child Information {cfg.season}.pdf"
    manifest_path = packet_dir / "manifest.json"
    html_path.write_text("stale packet")
    pdf_path.write_bytes(b"stale packet")
    manifest_path.write_text("stale manifest")
    fake_convert.clear()

    result = _run(cfg, options | {"overwrite": True})

    stems = [
        f"AT{cfg.season}_{angel_id}" for angel_id in ("1A", "1B", "1Z", "1AA")
    ]
    assert sorted(result.sheets_generated) == sorted(stems)
    assert sorted(fake_convert) == sorted(stems)
    sheet_artifacts_after = _sql(
        cfg, "SELECT COUNT(*) FROM artifacts WHERE kind = 'child_sheet'"
    )[0][0]
    assert sheet_artifacts_after == sheet_artifacts_before + len(stems)
    assert result.packets_built == 1
    assert html_path.is_file() and "stale packet" not in html_path.read_text()
    assert PdfReader(str(pdf_path)).pages
    assert (
        manifest_path.is_file()
        and "stale manifest" not in manifest_path.read_text()
    )


def test_packet_backup_replaces_a_read_only_previous_copy(
    tmp_path: Path,
    packets_cfg: Any,
    fake_convert: list[str],
) -> None:
    """A read-only backup from an earlier run does not break the next one."""
    cfg = packets_cfg(_prepare(tmp_path))
    _full_seed(cfg, tmp_path)
    _run(cfg, {})
    packet_dir = tmp_path / "artifacts" / "sponsor_packets" / "7"
    manual = packet_dir / "sponsor_packet_7.manual.html"
    manual.write_text("<html>previous draft</html>")
    manual.chmod(0o444)
    result = _run(cfg, {"overwrite": True})
    assert result.packets_built == 1
    assert manual.is_file()
    assert "previous draft" not in manual.read_text()


def test_packet_build_reports_an_unwritable_packet_directory(
    tmp_path: Path,
    packets_cfg: Any,
    fake_convert: list[str],
) -> None:
    """An unwritable sponsor is reported while other packets still build."""
    if os.geteuid() == 0:
        pytest.skip("root ignores the directory mode")
    cfg = packets_cfg(_prepare(tmp_path))
    children = [
        _angel_child("c1", "1A", assigned_sponsor_id=7),
        _angel_child("c2", "1B", assigned_sponsor_id=8),
    ]
    asyncio.run(
        _seed(
            cfg,
            children=children,
            families=[family_row(1, assigned_sponsor_id=None)],
            sponsors=[
                _assigned_sponsor(7, assigned_angel_ids="1A"),
                _assigned_sponsor(8, assigned_angel_ids="1B"),
            ],
        )
    )
    _run(cfg, {})
    packet_dir = tmp_path / "artifacts" / "sponsor_packets" / "7"
    packet_dir.chmod(0o555)
    try:
        result = _run(cfg, {"overwrite": True})
    finally:
        packet_dir.chmod(0o755)
    assert result.packets_built == 1
    assert len(result.packets_failed) == 1
    assert result.packets_failed[0]["sponsor_id"] == 7
    assert "not writable" in result.packets_failed[0]["error"]
    assert (
        tmp_path
        / "artifacts"
        / "sponsor_packets"
        / "8"
        / "sponsor_packet_8.html"
    ).is_file()


def test_packet_split_family_two_sponsors(
    tmp_path: Path, packets_cfg: Any, fake_convert: list[str]
) -> None:
    """A split family produces one packet per sponsor, right children."""
    cfg = packets_cfg(_prepare(tmp_path))
    children = [
        _angel_child("c1", "1A", assigned_sponsor_id=7),
        _angel_child("c2", "1B", assigned_sponsor_id=8),
    ]
    asyncio.run(
        _seed(
            cfg,
            children=children,
            families=[family_row(1, assigned_sponsor_id=None)],
            sponsors=[
                _assigned_sponsor(7, assigned_angel_ids="1A"),
                _assigned_sponsor(8, assigned_angel_ids="1B"),
            ],
        )
    )
    result = _run(cfg, {})
    assert result.packets_built == 2
    packets_dir = tmp_path / "artifacts" / "sponsor_packets"
    seven = (packets_dir / "7" / "sponsor_packet_7.html").read_text()
    eight = (packets_dir / "8" / "sponsor_packet_8.html").read_text()
    assert "<strong>1A</strong>" in seven
    assert "<strong>1B</strong>" not in seven
    assert "<strong>1B</strong>" in eight
    assert "<strong>1A</strong>" not in eight


def test_packet_shared_angel_letter_keeps_every_child(
    tmp_path: Path, packets_cfg: Any, fake_convert: list[str]
) -> None:
    """A shared angel letter no longer drops a child from the packet."""
    cfg = packets_cfg(_prepare(tmp_path))
    first = _angel_child("c1", "1A", family_id=1, assigned_sponsor_id=7)
    second = _angel_child("c2", "1A", family_id=2, assigned_sponsor_id=7)
    asyncio.run(
        _seed(
            cfg,
            children=[first, second],
            families=[_family(1), _family(2)],
            sponsors=[_assigned_sponsor(7, assigned_angel_ids="1A,1A")],
        )
    )
    result = _run(cfg, {})
    assert result.packets_built == 1
    html = (
        tmp_path
        / "artifacts"
        / "sponsor_packets"
        / "7"
        / "sponsor_packet_7.html"
    ).read_text()
    assert html.count("<strong>1A</strong>") == 2


def test_packet_validation_ignores_unaudited_missing_sheet(
    tmp_path: Path,
    packets_cfg: Any,
    fake_convert: list[str],
) -> None:
    """An unaudited child without a sheet does not block its sponsor."""
    cfg = packets_cfg(_prepare(tmp_path))
    children = [
        _angel_child("c1", "1A"),  # audited: sheet generated
        _angel_child("c2", "1B", status="new"),  # unaudited: no sheet
    ]
    asyncio.run(
        _seed(
            cfg,
            children=children,
            families=[_family(1)],
            sponsors=[_assigned_sponsor(assigned_angel_ids="1A,1B")],
        )
    )
    result = _run(cfg, {})
    # The audited sibling's sheet still committed (per-item durability).
    rows = _sql(
        cfg,
        "SELECT zite_record_id, generated_info_sheet FROM final_child"
        " ORDER BY zite_record_id",
    )
    assert dict(rows) == {"c1": 1, "c2": 0}
    assert result.packets_built == 1
    assert result.packets_blocked == []


def test_unaudited_child_is_logged_and_packet_omits_its_pages(
    tmp_path: Path,
    packets_cfg: Any,
    fake_convert: list[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unaudited assigned child gets no sheet and does not block packet."""
    cfg = packets_cfg(_prepare(tmp_path))
    children = [
        _angel_child("c1", "1A"),
        _angel_child("c2", "1Z", status="pending"),
    ]
    asyncio.run(
        _seed(
            cfg,
            children=children,
            families=[_family(1)],
            sponsors=[_assigned_sponsor(assigned_angel_ids="1A,1Z")],
        )
    )
    caplog.set_level(logging.WARNING, logger="ccat.child_info_generate")
    caplog.set_level(logging.WARNING, logger="ccat.services.db_generate")

    result = _run(cfg, {})

    assert result.packets_built == 1
    assert fake_convert == [f"AT{cfg.season}_1A"]
    assert not (
        tmp_path
        / "artifacts"
        / "child_sheets"
        / "hancock"
        / f"AT{cfg.season}_1Z.pdf"
    ).exists()
    packet_pdf = (
        tmp_path
        / "artifacts"
        / "sponsor_packets"
        / "7"
        / f"Sponsor 7 - Child Information {cfg.season}.pdf"
    )
    titles = [
        outline.title if hasattr(outline, "title") else None
        for outline in PdfReader(str(packet_pdf)).outline
    ]
    assert titles == ["Angel 1A"]
    assert any(
        "child 1Z" in record.message
        and "not audited" in record.message
        and "pending" in record.message
        for record in caplog.records
        if record.levelno == logging.WARNING
    )


def test_failed_sheet_blocks_only_its_sponsor(
    tmp_path: Path,
    packets_cfg: Any,
    fake_generation: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed child sheet blocks its sponsor, not the other sponsors."""
    cfg = packets_cfg(_prepare(tmp_path))
    children = [
        _angel_child("c1", "1A", assigned_sponsor_id=7),
        _angel_child("c2", "1B", assigned_sponsor_id=8),
    ]
    asyncio.run(
        _seed(
            cfg,
            children=children,
            families=[family_row(1, assigned_sponsor_id=None)],
            sponsors=[
                _assigned_sponsor(7, assigned_angel_ids="1A"),
                _assigned_sponsor(8, assigned_angel_ids="1B"),
            ],
        )
    )

    def fail_first_sheet(
        odt_path: Path,
        out_dir: Path,
        soffice_binary: str,
        profile: Path,
        timeout: float,
        stop: threading.Event | None = None,
    ) -> Path:
        if odt_path.stem.endswith("1A"):
            raise InfoSheetError("sheet conversion failed")
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        pdf_path = out_dir / f"{odt_path.stem}.pdf"
        with pdf_path.open("wb") as output:
            writer.write(output)
        return pdf_path

    monkeypatch.setattr(db_generate, "_convert_to_pdf", fail_first_sheet)

    result = _run(cfg, {})

    assert result.sheets_failed == [f"AT{cfg.season}_1A"]
    assert result.packets_blocked == [{"sponsor_id": 7, "angel_ids": ["1A"]}]
    assert result.packets_built == 1
    packets_dir = tmp_path / "artifacts" / "sponsor_packets"
    assert not (packets_dir / "7" / "sponsor_packet_7.html").exists()
    assert (packets_dir / "8" / "sponsor_packet_8.html").is_file()


def test_packet_independent_filters_ignored_family(
    tmp_path: Path, packets_cfg: Any, fake_convert: list[str]
) -> None:
    """Filters never cascade: an ignored family's child stays, blank."""
    cfg = packets_cfg(_prepare(tmp_path))
    children = [_angel_child("c1", "1A", generated_info_sheet=True)]
    asyncio.run(
        _seed(
            cfg,
            children=children,
            families=[_family(1, ignore=True)],
            sponsors=[_assigned_sponsor(assigned_angel_ids="1A")],
        )
    )

    async def frames() -> dict[str, Any]:
        from ccat.audit.store import build_final_frame, eligible_output_rows

        uow = UnitOfWork(create_async_engine(cfg.storage.database_url))
        try:
            async with uow.store(cfg, "system") as repo:
                built = {}
                for entity in ("child", "family", "sponsor"):
                    rows = await repo.final_rows(entity)
                    built[entity] = build_final_frame(
                        entity, eligible_output_rows(rows, entity)
                    )
                return built
        finally:
            await uow.engine.dispose()

    built = asyncio.run(frames())
    packets = db_generate._sponsors_with_angels(
        cfg, built["sponsor"], built["child"], built["family"]
    )
    assert len(packets) == 1
    assert packets[0]["angels"][0]["angel_id"] == "1A"
    assert packets[0]["angels"][0]["dropoff"] is None  # blank, not dropped


def test_packet_skip_predicate(
    tmp_path: Path, packets_cfg: Any, fake_convert: list[str]
) -> None:
    """A rerun skips existing packets (HTML + combined PDF)."""
    cfg = packets_cfg(_prepare(tmp_path))
    _full_seed(cfg, tmp_path)
    first = _run(cfg, {})
    assert first.packets_built == 1
    second = _run(cfg, {})
    assert second.packets_built == 0
    assert second.packets_skipped == 1


def test_packet_angel_ordering(
    tmp_path: Path, packets_cfg: Any, fake_convert: list[str]
) -> None:
    """Angels order by family then letter: 1A < 1B < 1Z < 1AA."""
    cfg = packets_cfg(_prepare(tmp_path))
    _full_seed(cfg, tmp_path)
    _run(cfg, {})
    pdf_path = (
        tmp_path
        / "artifacts"
        / "sponsor_packets"
        / "7"
        / f"Sponsor 7 - Child Information {cfg.season}.pdf"
    )
    reader = PdfReader(str(pdf_path))
    titles = [
        outline.title if hasattr(outline, "title") else None
        for outline in reader.outline
    ]
    assert titles == [
        "Angel 1A",
        "Angel 1B",
        "Angel 1Z",
        "Angel 1AA",
    ]


def test_packet_stored_angel_ids_ignored(
    tmp_path: Path,
    packets_cfg: Any,
    fake_convert: list[str],
) -> None:
    """Packet membership is the child's column; stored ids do not count."""
    cfg = packets_cfg(_prepare(tmp_path))
    children = [_angel_child("c1", "1A")]
    asyncio.run(
        _seed(
            cfg,
            children=children,
            families=[_family(1)],
            sponsors=[_assigned_sponsor(assigned_angel_ids="1A,9X")],
        )
    )
    result = _run(cfg, {})
    html = (
        tmp_path
        / "artifacts"
        / "sponsor_packets"
        / "7"
        / "sponsor_packet_7.html"
    ).read_text()
    assert "9X" not in html
    assert "<strong>1A</strong>" in html
    assert result.packets_built == 1


def test_overwrite_preserves_manual_edits(
    tmp_path: Path, packets_cfg: Any, fake_convert: list[str]
) -> None:
    """Overwriting a packet copies the existing HTML to .manual.html."""
    cfg = packets_cfg(_prepare(tmp_path))
    _full_seed(cfg, tmp_path)
    _run(cfg, {})
    packet_dir = tmp_path / "artifacts" / "sponsor_packets" / "7"
    html = packet_dir / "sponsor_packet_7.html"
    html.write_text("<p>manual edit</p>", encoding="utf-8")
    result = _run(cfg, {"overwrite": True})
    manual = packet_dir / "sponsor_packet_7.manual.html"
    assert manual.is_file()
    assert manual.read_text() == "<p>manual edit</p>"
    assert html.read_text() != "<p>manual edit</p>"
    manual_artifacts = _sql(
        cfg, "SELECT path FROM artifacts WHERE kind = 'sponsor_packet_manual'"
    )
    assert len(manual_artifacts) == 1
    assert result.packets_built == 1
