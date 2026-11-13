"""Tests for module 5b — sponsor packet generation."""

from pathlib import Path
from typing import Any

import polars as pl
import pytest
from pypdf import PdfReader, PdfWriter

from ccat.config import Config
from ccat.io import write_sheets
from ccat.sponsor_packets import (
    SponsorPacketError,
    _angel_sort_key,
    _sponsors_with_angels,
    build_packets,
    validate_all_sheets_exist,
)
from pypdf.generic import Destination

SPONSOR_SCHEMA: dict[str, Any] = {
    "sponsor_id": pl.Int64,
    "first_name": pl.String,
    "email": pl.String,
    "assigned_angel_ids": pl.String,
}

CHILD_SCHEMA: dict[str, Any] = {
    "zite_record_id": pl.String,
    "angel_id": pl.String,
    "family_id": pl.Int64,
    "gender": pl.String,
    "age_at_xmas": pl.Int64,
}

FAMILY_SCHEMA: dict[str, Any] = {
    "family_id": pl.Int64,
    "gift_pickup_location": pl.String,
    "assigned_sponsor_id": pl.Int64,
}


def sponsor_row(
    sponsor_id: int,
    first_name: str,
    email: str,
    assigned: str | None,
) -> dict[str, Any]:
    """One final_sponsor row for sponsor-packet tests."""
    return {
        "sponsor_id": sponsor_id,
        "first_name": first_name,
        "email": email,
        "assigned_angel_ids": assigned,
    }


def child_row(
    angel_id: str,
    gender: str | None,
    age: int,
    family_id: int = 1,
) -> dict[str, Any]:
    """One final_child row for sponsor-packet tests."""
    return {
        "zite_record_id": f"rec_{angel_id.lower()}",
        "angel_id": angel_id,
        "family_id": family_id,
        "gender": gender,
        "age_at_xmas": age,
    }


def _email_cfg(cfg: Config, tmp_path: Path) -> Config:
    """Redirect packet output, info sheets, into the test temp dir.

    Template and instructions file keep their repo defaults (the real
    template renders, the real instruction PDF merges).
    """
    from ccat.config import SponsorPacketSpec

    spec = SponsorPacketSpec(output_dir=str(tmp_path / "packets"))
    info_sheet = cfg.info_sheet.model_copy(
        update={"output_dir": str(tmp_path / "sheets")}
    )
    return cfg.model_copy(
        update={"sponsor_packets": spec, "info_sheet": info_sheet}
    )


def _seed(
    cfg: Config,
    sponsors: list[dict[str, Any]],
    children: list[dict[str, Any]],
    families: list[dict[str, Any]] | None = None,
) -> None:
    """Write final_sponsor, final_child, and final_family sheets."""
    if families is None:
        # Default: every seeded child's family at one site, assigned
        # to the first sponsor.
        families = [
            {
                "family_id": child["family_id"],
                "gift_pickup_location": "Hancock",
                "assigned_sponsor_id": (
                    sponsors[0]["sponsor_id"] if sponsors else None
                ),
            }
            for child in children
        ]
    write_sheets(
        cfg.excel_file,
        "match",
        {"final_sponsor": pl.DataFrame(sponsors, schema=SPONSOR_SCHEMA)},
    )
    write_sheets(
        cfg.excel_file,
        "audit",
        {"final_child": pl.DataFrame(children, schema=CHILD_SCHEMA)},
    )
    write_sheets(
        cfg.excel_file,
        "match",
        {"final_family": pl.DataFrame(families, schema=FAMILY_SCHEMA)},
    )


def _blank_pdf(path: Path, pages: int = 1) -> Path:
    """Write a minimal real PDF with the given page count."""
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as output:
        writer.write(output)
    return path


class TestAngelSortKey:
    """Packet page order: family ID, then angel letter (§13b)."""

    def test_orders_by_family_then_letter_naturally(self) -> None:
        """1A < 1B < 1Z < 1AA < 2A regardless of string order."""
        angels = [
            {"angel_id": "1AA", "family_id": 1},
            {"angel_id": "2A", "family_id": 2},
            {"angel_id": "1B", "family_id": 1},
            {"angel_id": "1A", "family_id": 1},
            {"angel_id": "1Z", "family_id": 1},
        ]
        ordered = [a["angel_id"] for a in sorted(angels, key=_angel_sort_key)]
        assert ordered == ["1A", "1B", "1Z", "1AA", "2A"]


class TestSponsorsWithAngels:
    """Sponsor/angel joining behind the packets."""

    def test_joins_sponsor_with_angel_details(self, cfg: Config) -> None:
        """Each sponsor's payload lists their angels, sorted."""
        _seed(
            cfg,
            [
                sponsor_row(7, "Sam", "sam@x.com", "2A,1A,1B"),
                sponsor_row(8, "Riley", "riley@x.com", "3A"),
            ],
            [
                child_row("1A", "Male", 8, family_id=1),
                child_row("1B", "Female", 5, family_id=1),
                child_row("2A", "Non-binary", 12, family_id=2),
                child_row("3A", "Male", 9, family_id=3),
            ],
            families=[
                {"family_id": 1, "gift_pickup_location": "Hancock"},
                {"family_id": 2, "gift_pickup_location": "Calumet"},
                {"family_id": 3, "gift_pickup_location": "Hancock"},
            ],
        )
        packets = _sponsors_with_angels(cfg)
        assert len(packets) == 2
        seven = next(p for p in packets if p["sponsor_id"] == 7)
        assert seven["first_name"] == "Sam"
        assert [
            (a["angel_id"], a["gender"], a["age"], a["dropoff"])
            for a in seven["angels"]
        ] == [
            ("1A", "Male", 8, "Hancock"),
            ("1B", "Female", 5, "Hancock"),
            ("2A", "Non-binary", 12, "Calumet"),
        ]

    def test_skips_sponsors_without_angels(self, cfg: Config) -> None:
        """Sponsors with empty/null assigned_angel_ids produce nothing."""
        _seed(
            cfg,
            [
                sponsor_row(7, "Sam", "sam@x.com", None),
                sponsor_row(8, "Riley", "riley@x.com", ""),
                sponsor_row(9, "Casey", "casey@x.com", "2A"),
            ],
            [child_row("2A", "Male", 7)],
        )
        packets = _sponsors_with_angels(cfg)
        assert [p["sponsor_id"] for p in packets] == [9]

    def test_missing_final_sponsor_raises(self, cfg: Config) -> None:
        """No final_sponsor sheet raises a typed error."""
        with pytest.raises(SponsorPacketError, match="final_sponsor"):
            _sponsors_with_angels(cfg)

    def test_no_assigned_sponsors_returns_empty(self, cfg: Config) -> None:
        """A workbook where nobody is assigned yields no packets."""
        _seed(
            cfg,
            [sponsor_row(7, "Sam", "sam@x.com", None)],
            [child_row("2A", "Male", 7)],
        )
        assert _sponsors_with_angels(cfg) == []


class TestRun:
    """The end-to-end packet build."""

    def test_writes_one_packet_dir_per_sponsor(
        self, cfg: Config, tmp_path: Path
    ) -> None:
        """Each sponsor gets a directory with email draft and PDF."""
        _seed(
            cfg,
            [
                sponsor_row(7, "Sam", "sam@x.com", "1A,1B"),
                sponsor_row(9, "Casey", "casey@x.com", "2A"),
            ],
            [
                child_row("1A", "Male", 8, family_id=1),
                child_row("1B", None, 5, family_id=1),
                child_row("2A", "Female", 12, family_id=2),
            ],
            families=[
                {"family_id": 1, "gift_pickup_location": "Hancock"},
                {"family_id": 2, "gift_pickup_location": "Calumet"},
            ],
        )
        email_cfg = _email_cfg(cfg, tmp_path)
        build_packets(email_cfg)
        packets = Path(email_cfg.sponsor_packets.output_dir)
        prefix = email_cfg.sponsor_packets.filename_prefix
        packet_dir = packets / "7"
        assert (packet_dir / f"{prefix}_7.html").is_file()
        # The instructions PDF is copied into the packet directory.
        assert (
            packet_dir / Path(email_cfg.sponsor_packets.instructions_file).name
        ).is_file()
        assert (packets / "9" / f"{prefix}_9.html").is_file()
        html = (packet_dir / f"{prefix}_7.html").read_text(encoding="utf-8")
        assert "Dear Sam," in html
        assert "Email subject:" in html
        # Recipient (To) line carries the sponsor's email address.
        assert "<strong>To:</strong> sam@x.com" in html
        # No BCC addresses configured: the line renders a placeholder.
        bcc_line = next(
            line
            for line in html.splitlines()
            if "<strong>BCC:</strong>" in line
        )
        assert "—" in bcc_line and "bcc.program@" not in bcc_line
        # Two attachments named in the email body.
        assert "two PDF files" in html
        assert Path(email_cfg.sponsor_packets.instructions_file).name in html
        assert f"Sponsor 7 - Child Information {email_cfg.season}.pdf" in html
        # Table row per angel; null gender renders as an em dash.
        assert "<strong>1A</strong>" in html
        assert "<strong>1B</strong>" in html
        assert "—" in html
        # Per-angel drop-off locations come from each angel's family.
        assert "Drop-off location" in html
        assert ">Hancock<" in html
        # Angels of other sponsors never leak into this packet.
        assert ">Calumet<" not in html
        assert "2A" not in html

    def test_child_information_pdf_order(
        self, cfg: Config, tmp_path: Path
    ) -> None:
        """Combined PDF: angel sheets by family then letter, no instructions."""
        _seed(
            cfg,
            [sponsor_row(7, "Sam", "sam@x.com", "2A,1B,1A")],
            [
                child_row("1A", "Male", 8, family_id=1),
                child_row("1B", "Male", 5, family_id=1),
                child_row("2A", "Female", 12, family_id=2),
            ],
            families=[
                {
                    "family_id": 1,
                    "gift_pickup_location": "Hancock",
                    "assigned_sponsor_id": 7,
                },
                {
                    "family_id": 2,
                    "gift_pickup_location": "Calumet",
                    "assigned_sponsor_id": 7,
                },
            ],
        )
        email_cfg = _email_cfg(cfg, tmp_path)
        # Stand in for module 5's generated child information sheets.
        sheets_dir = Path(email_cfg.info_sheet.output_dir)
        for angel_id, location in (
            ("1A", "hancock"),
            ("1B", "hancock"),
            ("2A", "calumet"),
        ):
            _blank_pdf(sheets_dir / location / f"AT{cfg.season}_{angel_id}.pdf")
        build_packets(email_cfg)
        packet_pdf = (
            Path(email_cfg.sponsor_packets.output_dir)
            / "7"
            / f"Sponsor 7 - Child Information {email_cfg.season}.pdf"
        )
        reader = PdfReader(str(packet_pdf))
        outlines = reader.outline
        titles = [
            item.title for item in outlines if isinstance(item, Destination)
        ]
        assert titles == ["Angel 1A", "Angel 1B", "Angel 2A"]
        # The instructions PDF was copied, not merged.
        assert (
            Path(email_cfg.sponsor_packets.output_dir)
            / "7"
            / Path(email_cfg.sponsor_packets.instructions_file).name
        ).is_file()

    def test_child_information_pdf_skips_missing_sheets(
        self, cfg: Config, tmp_path: Path
    ) -> None:
        """Merging warns and skips when a sheet is absent.

        Only reachable directly: ``build_packets`` validates first, so
        a full run never reaches the merge with sheets missing.
        """
        from ccat.sponsor_packets import _merge_sheets_pdf

        _seed(
            cfg,
            [sponsor_row(7, "Sam", "sam@x.com", "1A,1B")],
            [
                child_row("1A", "Male", 8, family_id=1),
                child_row("1B", "Male", 5, family_id=1),
            ],
        )
        email_cfg = _email_cfg(cfg, tmp_path)
        sheets_dir = Path(email_cfg.info_sheet.output_dir)
        _blank_pdf(
            sheets_dir / "hancock" / f"AT{cfg.season}_1B.pdf"
        )  # 1A missing
        packet_dir = Path(email_cfg.sponsor_packets.output_dir) / "7"
        packet_dir.mkdir(parents=True)
        pdf_path, missing = _merge_sheets_pdf(
            email_cfg,
            packet_dir,
            7,
            [
                {"angel_id": "1A", "location": "hancock"},
                {"angel_id": "1B", "location": "hancock"},
            ],
        )
        assert missing == ["1A"]
        reader = PdfReader(str(pdf_path))
        titles = [
            item.title
            for item in reader.outline
            if isinstance(item, Destination)
        ]
        assert titles == ["Angel 1B"]

    def test_validate_raises_when_sheets_missing(
        self, cfg: Config, tmp_path: Path
    ) -> None:
        """A sponsored angel without a sheet blocks the packet phase."""
        _seed(
            cfg,
            [sponsor_row(7, "Sam", "sam@x.com", "1A,1B")],
            [
                child_row("1A", "Male", 8, family_id=1),
                child_row("1B", "Male", 5, family_id=1),
            ],
        )
        email_cfg = _email_cfg(cfg, tmp_path)
        sheets_dir = Path(email_cfg.info_sheet.output_dir)
        _blank_pdf(
            sheets_dir / "hancock" / f"AT{cfg.season}_1B.pdf"
        )  # 1A missing
        with pytest.raises(SponsorPacketError, match=r"\['1A'\]"):
            validate_all_sheets_exist(email_cfg)

    def test_validate_passes_when_all_sheets_exist(
        self, cfg: Config, tmp_path: Path
    ) -> None:
        """No error when every sponsored angel's sheet is present."""
        _seed(
            cfg,
            [sponsor_row(7, "Sam", "sam@x.com", "1A,1B")],
            [
                child_row("1A", "Male", 8, family_id=1),
                child_row("1B", "Male", 5, family_id=1),
            ],
        )
        email_cfg = _email_cfg(cfg, tmp_path)
        sheets_dir = Path(email_cfg.info_sheet.output_dir)
        for angel_id in ("1A", "1B"):
            _blank_pdf(
                sheets_dir / "hancock" / f"AT{cfg.season}_{angel_id}.pdf"
            )
        validate_all_sheets_exist(email_cfg)  # no raise

    def test_bcc_emails_render_in_draft(
        self, cfg: Config, tmp_path: Path
    ) -> None:
        """Configured BCC addresses render as a copy-paste list."""
        _seed(
            cfg,
            [sponsor_row(7, "Sam", "sam@x.com", "1A")],
            [child_row("1A", "Male", 8)],
        )
        email_cfg = _email_cfg(cfg, tmp_path)
        spec = email_cfg.sponsor_packets.model_copy(
            update={"bcc_emails": ["bcc.program@example.org", "lead@ccat.org"]}
        )
        email_cfg = email_cfg.model_copy(update={"sponsor_packets": spec})
        build_packets(email_cfg)
        draft = (
            Path(email_cfg.sponsor_packets.output_dir)
            / "7"
            / f"{spec.filename_prefix}_7.html"
        )
        html = draft.read_text(encoding="utf-8")
        assert (
            "<strong>BCC:</strong> bcc.program@example.org, lead@ccat.org"
            in html
        )
        assert "<strong>To:</strong> sam@x.com" in html

    def test_rerun_skips_existing_without_overwrite(
        self, cfg: Config, tmp_path: Path
    ) -> None:
        """Existing packets are untouched unless --overwrite."""
        _seed(
            cfg,
            [sponsor_row(7, "Sam", "sam@x.com", "1A")],
            [child_row("1A", "Male", 8)],
        )
        email_cfg = _email_cfg(cfg, tmp_path)
        build_packets(email_cfg)
        packet_dir = Path(email_cfg.sponsor_packets.output_dir) / "7"
        html = packet_dir / (
            f"{email_cfg.sponsor_packets.filename_prefix}_7.html"
        )
        html.write_text("manually edited", encoding="utf-8")
        build_packets(email_cfg)
        assert html.read_text(encoding="utf-8") == "manually edited"
        build_packets(email_cfg, overwrite=True)
        assert "Dear Sam," in html.read_text(encoding="utf-8")

    def test_missing_template_raises(self, cfg: Config, tmp_path: Path) -> None:
        """A missing template file raises a typed error up front."""
        email_cfg = _email_cfg(cfg, tmp_path)
        spec = email_cfg.sponsor_packets.model_copy(
            update={"email_template_html_file": "nowhere.html"}
        )
        broken = email_cfg.model_copy(update={"sponsor_packets": spec})
        with pytest.raises(SponsorPacketError, match="template file not found"):
            build_packets(broken)

    def test_missing_sheets_raise(self, cfg: Config, tmp_path: Path) -> None:
        """An empty workbook raises a typed error."""
        email_cfg = _email_cfg(cfg, tmp_path)
        with pytest.raises(SponsorPacketError, match="final_sponsor"):
            build_packets(email_cfg)
