"""Tests for module 6 -- statistics report generation."""

from datetime import date, datetime
from pathlib import Path
from typing import Any

import polars as pl
from openpyxl import Workbook
import pytest

import ccat.stats as stats
from ccat.audit.store import build_final_frame
from ccat.config import Config, ReportSpec
from ccat.io import list_sheets, write_sheets
from ccat.stats import StatsError


def sponsor_row(
    sponsor_id: int,
    num_children: int,
    location: str | None,
    *,
    ignore: bool = False,
) -> dict[str, Any]:
    """One full-store sponsor row for stats tests."""
    return {
        "sponsor_id": sponsor_id,
        "num_children": num_children,
        "dropoff_location": location,
        "ignore": ignore,
    }


def child_row(
    record_id: str,
    family_id: int | None,
    created: datetime,
    gender: str | None,
    age: int = 8,
    *,
    ignore: bool = False,
    sponsor: int | None = None,
) -> dict[str, Any]:
    """One full-store child row for stats tests."""
    return {
        "zite_record_id": record_id,
        "family_id": family_id,
        "angel_id": record_id.replace("rec", "A"),
        "created": created,
        "age_at_xmas": age,
        "gender": gender,
        "ignore": ignore,
        "assigned_sponsor_id": sponsor,
    }


def family_row(
    family_id: int,
    location: str | None,
    *,
    ignore: bool = False,
) -> dict[str, Any]:
    """One full-store family row for stats tests."""
    return {
        "family_id": family_id,
        "gift_pickup_location": location,
        "ignore": ignore,
    }


def _seed(
    cfg: Config,
    children: list[dict[str, Any]],
    families: list[dict[str, Any]],
    sponsors: list[dict[str, Any]] | None = None,
) -> None:
    """Write complete persistent-final stores."""
    write_sheets(
        cfg.excel_file,
        "audit",
        {"final_child": build_final_frame("child", children)},
        xmas=cfg.xmas,
    )
    write_sheets(
        cfg.excel_file,
        "match",
        {"final_family": build_final_frame("family", families)},
    )
    if sponsors is not None:
        write_sheets(
            cfg.excel_file,
            "match",
            {"final_sponsor": build_final_frame("sponsor", sponsors)},
        )


def _raw_frame(
    cfg: Config, entity: str, rows: list[dict[str, Any]]
) -> pl.DataFrame:
    """Build a raw sheet containing every configured source column."""
    complete = [
        {**{column: None for column in cfg.tables[entity].columns}, **row}
        for row in rows
    ]
    return pl.DataFrame(complete)


def _seed_raw(cfg: Config, *, include_child: bool = True) -> None:
    """Seed raw forms for ephemeral preparation, not submissions sheets."""
    sheets = {
        "raw_sponsor": _raw_frame(
            cfg,
            "sponsor",
            [
                {
                    "zite_record_id": "sponsor-1",
                    "create": "2026-10-01T08:00:00",
                    "email": "sam@example.com",
                    "first_name": "Sam",
                    "num_children": 2,
                    "dropoff_location": "Hancock",
                }
            ],
        ),
        "raw_family": _raw_frame(
            cfg,
            "family",
            [
                {
                    "zite_record_id": "family-1",
                    "zite_created_at": "2026-10-01T09:00:00",
                    "created": "2026-10-01T09:00:00",
                    "primary_email": "family@example.com",
                    "first_name": "Caroline",
                    "gift_pickup_location": "Hancock",
                }
            ],
        ),
    }
    if include_child:
        sheets["raw_child"] = _raw_frame(
            cfg,
            "child",
            [
                {
                    "zite_record_id": "rec1",
                    "zite_created_at": "2026-10-02T11:00:00",
                    "created": "2026-10-02T12:00:00",
                    "primary_email": "family@example.com",
                    "first_name": "Aspen",
                    "dob": "2018-01-01",
                    "age_reported": 8,
                    "gender": "Male",
                },
                {
                    "zite_record_id": "rec2",
                    "zite_created_at": "2026-10-02T11:00:00",
                    "created": "2026-10-02T12:00:00",
                    "primary_email": "family@example.com",
                    "first_name": "Birch",
                    "dob": "2021-01-01",
                    "age_reported": 5,
                    "gender": "Female",
                },
                {
                    "zite_record_id": "rec-orphan",
                    "zite_created_at": "2026-10-02T11:00:00",
                    "created": "2026-10-02T12:00:00",
                    "primary_email": "orphan@example.com",
                    "first_name": "Cedar",
                    "dob": "2017-01-01",
                    "age_reported": 9,
                    "gender": "Male",
                },
            ],
        )
    write_sheets(cfg.excel_file, "acquire", sheets)


def _report_cfg(cfg: Config, tmp_path: Path) -> Config:
    """Redirect the report output directory into the test temp dir."""
    return cfg.model_copy(
        update={"report": ReportSpec(output_dir=str(tmp_path / "reports"))}
    )


class TestNormalizeHelpers:
    """Tests for the location/gender normalization helpers."""

    def test_normalize_location_buckets_unknown(self) -> None:
        """Null and blank locations normalize to ``Unknown``."""
        assert stats._normalize_location(None) == "Unknown"
        assert stats._normalize_location("  ") == "Unknown"
        assert stats._normalize_location(" Hancock ") == "Hancock"

    def test_normalize_location_merges_aliases(self) -> None:
        """Site-prefixed spellings count as the bare site name."""
        assert stats._normalize_location("TSA (Hancock)") == "Hancock"
        assert stats._normalize_location("CCAM (Calumet)") == "Calumet"
        assert stats._normalize_location("tsa (hancock)") == "Hancock"

    def test_normalize_gender_casings(self) -> None:
        """Gender keys tolerate casing, spacing, and nulls."""
        assert stats._normalize_gender("Female") == "female"
        assert stats._normalize_gender("MALE") == "male"
        assert stats._normalize_gender("Non-binary") == "non-binary"
        assert stats._normalize_gender("NonBinary") == "non-binary"
        assert stats._normalize_gender(None) == "unspecified"
        assert stats._normalize_gender("alien") == "unspecified"

    def test_location_orders_vocabulary_first(self, cfg: Config) -> None:
        """Known vocabulary locations lead, extras follow, Unknown last.

        The vocabulary's aliased spellings collapse onto their
        canonical labels.
        """
        ordered = stats._location_order(
            cfg, ["Calumet", "Zoo", "Unknown", "Hancock"]
        )
        assert ordered[:3] == ["Hancock", "Calumet", "Zoo"]
        assert ordered[-1] == "Unknown"


class TestOutputEligibility:
    """Default report sources match the legacy projected populations."""

    def test_filters_entities_independently(self, cfg: Config) -> None:
        """Ignored rows and orphans vanish; children of ignored families stay."""
        stamp = datetime(2026, 10, 2, 12, 0)
        _seed(
            cfg,
            [
                child_row("rec1", 1, stamp, "Male"),
                child_row("rec2", 1, stamp, "Female", ignore=True),
                child_row("rec3", 2, stamp, "Female"),
                child_row("rec-orphan", None, stamp, "Male"),
            ],
            [
                family_row(1, "Hancock"),
                family_row(2, "Calumet", ignore=True),
            ],
            [
                sponsor_row(7, 5, "Hancock"),
                sponsor_row(8, 10, "Calumet", ignore=True),
            ],
        )
        enriched, family, sponsor, _locations = stats._load_enriched(cfg)
        assert enriched["zite_record_id"].to_list() == ["rec1", "rec3"]
        assert enriched["gift_pickup_location"].to_list() == [
            "Hancock",
            "Unknown",
        ]
        assert family["family_id"].to_list() == [1]
        assert sponsor["sponsor_id"].to_list() == [7]

    def test_child_assignment_wins_over_family_summary(
        self, cfg: Config
    ) -> None:
        """The child's own sponsor is the truth; the family cannot override."""
        stamp = datetime(2026, 10, 2, 12, 0)
        family = {**family_row(1, "Hancock"), "assigned_sponsor_id": 9}
        _seed(
            cfg,
            [child_row("rec1", 1, stamp, "Male", sponsor=7)],
            [family],
        )
        enriched, _family, _sponsor, _locations = stats._load_enriched(cfg)
        assert enriched["assigned_sponsor_id"].to_list() == [7]


class TestDailyAdmissions:
    """Tests for the per-day per-location admissions aggregation."""

    def test_zero_fills_gap_days(self, cfg: Config) -> None:
        """Days without admissions appear with a count of zero."""
        _seed(
            cfg,
            [
                child_row("rec1", 1, datetime(2026, 10, 1, 9, 0), "Male"),
                child_row("rec2", 1, datetime(2026, 10, 3, 9, 0), "Female"),
            ],
            [family_row(1, "Hancock")],
        )
        enriched, _family, _sponsor, _locations = stats._load_enriched(cfg)
        days, per_location = stats._daily_admissions(enriched)
        assert days[0] == date(2026, 10, 1)
        assert days[-1] == date.today()  # range stops at today
        counts = dict(zip(days, per_location["Hancock"], strict=True))
        assert counts[date(2026, 10, 1)] == 1
        assert counts[date(2026, 10, 2)] == 0
        assert counts[date(2026, 10, 3)] == 1
        assert counts[date(2026, 10, 4)] == 0  # gap after last admission
        assert counts[date.today()] == 0  # trailing days through today

    def test_groups_by_location(self, cfg: Config) -> None:
        """Same-day admissions stack into separate location counts."""
        stamp = datetime(2026, 10, 2, 12, 0)
        _seed(
            cfg,
            [
                child_row("rec1", 1, stamp, "Male"),
                child_row("rec2", 1, stamp, "Female"),
                child_row("rec3", 2, stamp, "Male"),
            ],
            [
                family_row(1, "Hancock"),
                family_row(2, "Calumet"),
            ],
        )
        enriched, _family, _sponsor, _locations = stats._load_enriched(cfg)
        days, per_location = stats._daily_admissions(enriched)
        assert days[0] == date(2026, 10, 2)
        index = days.index(date(2026, 10, 2))
        assert per_location["Calumet"][index] == 1
        assert per_location["Hancock"][index] == 2


class TestLocationTable:
    """Tests for the per-location statistics table."""

    def test_counts_per_location_and_totals(self, cfg: Config) -> None:
        """Rows count genders, families, desired angels, sponsored."""
        stamp = datetime(2026, 10, 2, 12, 0)
        _seed(
            cfg,
            [
                child_row("rec1", 1, stamp, "Male", sponsor=7),
                child_row("rec2", 1, stamp, "Female", sponsor=7),
                child_row("rec3", 2, stamp, "Non-binary", sponsor=7),
                child_row("rec4", 3, stamp, "Female", age=4),
                # A split family: its placed children count individually.
                child_row("rec5", 4, stamp, "Male", sponsor=7),
                child_row("rec6", 4, stamp, "Female", sponsor=8),
            ],
            [
                family_row(1, "Hancock"),
                family_row(2, "Calumet"),
                family_row(3, "Calumet"),
                family_row(4, "Hancock"),
            ],
            [
                sponsor_row(7, 5, "Hancock"),
                sponsor_row(8, 10, "Calumet"),
                sponsor_row(9, 3, "Any Location"),
                sponsor_row(10, 4, "TSA (Hancock)"),
            ],
        )
        enriched, family, sponsor, _locations = stats._load_enriched(cfg)
        keys = ["male", "female", "non-binary"]
        rows = stats._location_table(
            enriched, family, sponsor, ["Hancock", "Calumet", "Unknown"], keys
        )
        hancock = next(row for row in rows if row["label"] == "Hancock")
        calumet = next(row for row in rows if row["label"] == "Calumet")
        unknown = next(row for row in rows if row["label"] == "Unknown")
        total = rows[-1]
        assert hancock["genders"] == {
            "male": 2,
            "female": 2,
            "non-binary": 0,
        }
        assert hancock["families"] == 2
        # Desired angels: sponsor 7 wants 5, sponsor 10's
        # "TSA (Hancock)" alias wants 4 more.
        assert hancock["sponsors"] == 9
        # Two children of family 1 plus both placed children of the
        # split family 4; the split family is not one sponsored unit.
        assert hancock["sponsored"] == 4
        assert calumet["genders"] == {
            "male": 0,
            "female": 1,
            "non-binary": 1,
        }
        assert calumet["families"] == 2
        assert calumet["sponsors"] == 10
        assert calumet["sponsored"] == 1
        # "Any Location" sponsors bucket under Unknown.
        assert unknown["sponsors"] == 3
        assert total["label"] == "All locations"
        assert total["genders"] == {"male": 2, "female": 3, "non-binary": 1}
        assert total["families"] == 4
        # Totals sum every site's desired angels once.
        assert total["sponsors"] == 22
        assert total["sponsored"] == 5

    def test_buckets_unknown_location(self, cfg: Config) -> None:
        """Null and unmatched family locations land in ``Unknown``."""
        stamp = datetime(2026, 10, 2, 12, 0)
        _seed(
            cfg,
            [
                child_row("rec1", 1, stamp, "Male"),
                child_row("rec2", 99, stamp, "Female"),
            ],
            [family_row(1, None)],
        )
        enriched, family, sponsor, locations = stats._load_enriched(cfg)
        assert locations[-1] == "Unknown"
        rows = stats._location_table(
            enriched, family, sponsor, locations, ["male", "female"]
        )
        unknown = rows[locations.index("Unknown")]
        assert unknown["genders"] == {"male": 1, "female": 1}
        # The null-location family itself lands in the Unknown bucket.
        assert unknown["families"] == 1
        assert unknown["sponsors"] == 0
        assert unknown["sponsored"] == 0


class TestGenderAge:
    """Tests for the gender and age aggregations behind the figure."""

    def test_gender_counts_normalize_casing(self, cfg: Config) -> None:
        """Gender keys fold mixed casings and nulls together."""
        stamp = datetime(2026, 10, 2, 12, 0)
        _seed(
            cfg,
            [
                child_row("rec1", 1, stamp, "Female"),
                child_row("rec2", 1, stamp, "FEMALE"),
                child_row("rec3", 1, stamp, "Male"),
                child_row("rec4", 1, stamp, None),
            ],
            [family_row(1, "Hancock")],
        )
        enriched, _family, _sponsor, _locations = stats._load_enriched(cfg)
        counts = {
            key: enriched.filter(pl.col("gender_key") == key).height
            for key in ("male", "female", "unspecified")
        }
        assert counts == {"male": 1, "female": 2, "unspecified": 1}

    def test_run_age_counts_bin_ages(self, cfg: Config) -> None:
        """Ages round-trip through run() into the age histogram."""
        stamp = datetime(2026, 10, 2, 12, 0)
        _seed(
            cfg,
            [
                child_row("rec1", 1, stamp, "Male", age=3),
                child_row("rec2", 1, stamp, "Female", age=3),
                child_row("rec3", 1, stamp, "Male", age=11),
            ],
            [family_row(1, "Hancock")],
        )
        enriched, _family, _sponsor, _locations = stats._load_enriched(cfg)
        ages = enriched["age_at_xmas"].drop_nulls().cast(pl.Int64)
        age_counts = {
            int(age): count
            for age, count in ages.value_counts()
            .sort("age_at_xmas")
            .iter_rows()
        }
        assert age_counts == {3: 2, 11: 1}


class TestLocationColors:
    """Tests for the per-location bar color mapping."""

    def test_first_two_locations_blue_green(self) -> None:
        """Colors are blue then green in location order."""
        colors = stats._location_colors(["Hancock", "Calumet"])
        assert colors["Hancock"] == "#1f77b4"
        assert colors["Calumet"] == "#2ca02c"

    def test_cycle_beyond_two_locations(self) -> None:
        """Extra locations cycle back through blue and green."""
        colors = stats._location_colors(
            ["TSA (Hancock)", "CCAM (Calumet)", "Hancock", "Calumet", "X"]
        )
        assert colors["Hancock"] == "#1f77b4"
        assert colors["Calumet"] == "#2ca02c"
        assert colors["X"] == "#1f77b4"


class TestRun:
    """Tests for the end-to-end report run."""

    def test_run_writes_html_and_pdf(self, cfg: Config, tmp_path: Path) -> None:
        """run() writes dated, non-empty HTML and PDF report files."""
        stamp = datetime(2026, 10, 2, 12, 0)
        _seed(
            cfg,
            [
                child_row("rec1", 1, stamp, "Male"),
                child_row("rec2", 1, stamp, "Female", age=5),
            ],
            [family_row(1, "Hancock")],
        )
        report_cfg = _report_cfg(cfg, tmp_path)
        stats.run(report_cfg)
        stem = f"report_{date.today().isoformat()}"
        html_path = tmp_path / "reports" / f"{stem}.html"
        pdf_path = tmp_path / "reports" / f"{stem}.pdf"
        assert html_path.is_file() and html_path.stat().st_size > 0
        assert pdf_path.is_file() and pdf_path.stat().st_size > 0
        html = html_path.read_text(encoding="utf-8")
        assert cfg.season in html
        assert "Pickup location" in html
        assert "All locations" in html
        assert "data:image/png;base64," in html
        # xhtml2pdf needs explicit px sizes or the figures overflow the
        # page and disappear from the PDF (1px = 0.75pt, 672px = 504pt).
        assert "width: 672px" in html

    def test_run_raises_when_final_child_missing(
        self, cfg: Config, tmp_path: Path
    ) -> None:
        """A workbook without final_child raises StatsError."""
        report_cfg = _report_cfg(cfg, tmp_path)
        with pytest.raises(StatsError):
            stats.run(report_cfg)

    def test_run_raw_prepares_ephemerally_without_workbook_writes(
        self, cfg: Config, tmp_path: Path
    ) -> None:
        """--raw cleans/assigns raw rows and creates no submissions/adj state."""
        _seed_raw(cfg)
        report_cfg = _report_cfg(cfg, tmp_path)
        workbook_before = Path(cfg.excel_file).read_bytes()
        sheets_before = list_sheets(cfg.excel_file)
        stats.run(report_cfg, raw=True)
        assert Path(cfg.excel_file).read_bytes() == workbook_before
        assert list_sheets(cfg.excel_file) == sheets_before
        assert not any(
            sheet.endswith("_submissions") or sheet.startswith("adj_")
            for sheet in sheets_before
        )
        stem = f"report_{date.today().isoformat()}"
        html_path = tmp_path / "reports" / f"{stem}.html"
        assert html_path.is_file() and html_path.stat().st_size > 0
        html = html_path.read_text(encoding="utf-8")
        assert "Pickup location" in html
        enriched, family, sponsor, _locations = stats._load_enriched(
            report_cfg, raw=True
        )
        assert Path(cfg.excel_file).read_bytes() == workbook_before
        assert list_sheets(cfg.excel_file) == sheets_before
        assert enriched["zite_record_id"].to_list() == [
            "rec-orphan",
            "rec1",
            "rec2",
        ]
        assert enriched["family_id"].to_list() == [None, 1, 1]
        assert enriched["assigned_sponsor_id"].null_count() == len(enriched)
        assert family["family_id"].to_list() == [1]
        assert sponsor["sponsor_id"].to_list() == [1]

    def test_run_raw_raises_when_raw_child_missing(
        self, cfg: Config, tmp_path: Path
    ) -> None:
        """--raw without raw_child fails during local preparation."""
        _seed_raw(cfg, include_child=False)
        report_cfg = _report_cfg(cfg, tmp_path)
        with pytest.raises(StatsError, match="raw_child"):
            stats.run(report_cfg, raw=True)
        assert not any(
            sheet.endswith("_submissions") or sheet.startswith("adj_")
            for sheet in list_sheets(cfg.excel_file)
        )

    def test_run_rejects_projected_final_schema(
        self, cfg: Config, tmp_path: Path
    ) -> None:
        """A legacy projected child final cannot stand in for the full store."""
        workbook = Workbook()
        sheet = workbook.active
        assert sheet is not None
        sheet.title = "final_child"
        sheet.append(
            [
                "zite_record_id",
                "family_id",
                "angel_id",
                "created",
                "age_at_xmas",
                "gender",
            ]
        )
        sheet.append(["rec1", 1, "1A", "2026-10-02T12:00:00", 8, "Male"])
        workbook.save(cfg.excel_file)
        report_cfg = _report_cfg(cfg, tmp_path)
        with pytest.raises(StatsError, match="persistent full schema"):
            stats.run(report_cfg)


class TestReportSpec:
    """Tests for the report configuration model."""

    def test_defaults(self) -> None:
        """ReportSpec defaults point at output/reports."""
        spec = ReportSpec()
        assert spec.output_dir == "output/reports"
        assert spec.filename_prefix == "report"

    def test_config_accepts_report_section(self, cfg: Config) -> None:
        """The season config loads with its report section intact."""
        assert cfg.report.output_dir == "./output/reports"
        assert cfg.report.filename_prefix == "report"
