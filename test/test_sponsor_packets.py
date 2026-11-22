"""Tests for module 5b — sponsor packet generation."""

from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfReader, PdfWriter

from ccat.audit.store import build_final_frame
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


def sponsor_row(
    sponsor_id: int,
    first_name: str,
    email: str,
    assigned: str | None,
    *,
    ignore: bool = False,
) -> dict[str, Any]:
    """One full-store sponsor row for sponsor-packet tests."""
    return {
        "sponsor_id": sponsor_id,
        "first_name": first_name,
        "email": email,
        "assigned_angel_ids": assigned,
        "ignore": ignore,
    }


def child_row(
    angel_id: str,
    gender: str | None,
    age: int,
    family_id: int | None = 1,
    *,
    ignore: bool = False,
) -> dict[str, Any]:
    """One full-store child row for sponsor-packet tests."""
    return {
        "zite_record_id": f"rec_{angel_id.lower()}",
        "angel_id": angel_id,
        "family_id": family_id,
        "gender": gender,
        "age_at_xmas": age,
        "ignore": ignore,
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
    """Write complete persistent-final stores.

    A child row without its own ``assigned_sponsor_id`` inherits the
    sponsor whose (derived) ``assigned_angel_ids`` lists its letter, so
    a test can state membership through either column.
    """
    ownership = {
        angel_id.strip(): sponsor["sponsor_id"]
        for sponsor in sponsors
        for angel_id in str(sponsor.get("assigned_angel_ids") or "").split(",")
        if angel_id.strip()
    }
    for child in children:
        if (
            "assigned_sponsor_id" not in child
            and child["angel_id"] in ownership
        ):
            child["assigned_sponsor_id"] = ownership[child["angel_id"]]
    if families is None:
        # Default: every seeded child's family at one site, assigned
        # to the first sponsor.  Full stores require unique family IDs.
        families_by_id = {
            child["family_id"]: {
                "family_id": child["family_id"],
                "gift_pickup_location": "Hancock",
                "assigned_sponsor_id": (
                    sponsors[0]["sponsor_id"] if sponsors else None
                ),
            }
            for child in children
            if child["family_id"] is not None
        }
        families = list(families_by_id.values())
    write_sheets(
        cfg.excel_file,
        "match",
        {"final_sponsor": build_final_frame("sponsor", sponsors)},
    )
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

    def test_sponsor_id_restricts_payload(self, cfg: Config) -> None:
        """``sponsor_id`` scopes the payload to one sponsor."""
        _seed(
            cfg,
            [
                sponsor_row(7, "Sam", "sam@x.com", "1A"),
                sponsor_row(8, "Riley", "riley@x.com", "2A"),
            ],
            [
                child_row("1A", "Male", 8, family_id=1),
                child_row("2A", "Female", 9, family_id=2),
            ],
        )
        packets = _sponsors_with_angels(cfg, sponsor_id=8)
        assert [p["sponsor_id"] for p in packets] == [8]
        assert [a["angel_id"] for a in packets[0]["angels"]] == ["2A"]

    def test_unknown_sponsor_id_raises(self, cfg: Config) -> None:
        """A ``sponsor_id`` with no assigned angels raises a typed error."""
        _seed(
            cfg,
            [sponsor_row(7, "Sam", "sam@x.com", "1A")],
            [child_row("1A", "Male", 8, family_id=1)],
        )
        with pytest.raises(SponsorPacketError, match="sponsor 9"):
            _sponsors_with_angels(cfg, sponsor_id=9)

    def test_ignored_and_orphan_filtering_is_independent(
        self, cfg: Config, tmp_path: Path
    ) -> None:
        """Packet and PDF validation populations match old projections."""
        _seed(
            cfg,
            [
                sponsor_row(7, "Sam", "sam@x.com", "1A,2A,3A,4A,5A"),
                sponsor_row(8, "Ignored", "ignored@x.com", "6A", ignore=True),
            ],
            [
                child_row("1A", "Male", 8, family_id=1),
                child_row("2A", "Female", 7, family_id=1, ignore=True),
                child_row("3A", "Male", 6, family_id=None),
                child_row("4A", "Female", 5, family_id=2),
                child_row("5A", "Male", 4, family_id=1),
                child_row("6A", "Female", 3, family_id=1, ignore=True),
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
                    "ignore": True,
                },
            ],
        )
        packets = _sponsors_with_angels(cfg)
        assert [packet["sponsor_id"] for packet in packets] == [7]
        angels = packets[0]["angels"]
        assert [angel["angel_id"] for angel in angels] == ["1A", "5A", "4A"]
        assert angels[2]["dropoff"] is None
        with pytest.raises(SponsorPacketError, match="sponsor 8"):
            _sponsors_with_angels(cfg, sponsor_id=8)

        email_cfg = _email_cfg(cfg, tmp_path)
        sheets_dir = Path(email_cfg.info_sheet.output_dir)
        for angel_id in ("1A", "5A"):
            _blank_pdf(
                sheets_dir / "hancock" / f"AT{cfg.season}_{angel_id}.pdf"
            )
        # 4A now carries its own sponsor, so its sheet is required; its
        # ignored family supplies no pickup location, hence "unknown".
        _blank_pdf(sheets_dir / "unknown" / f"AT{cfg.season}_4A.pdf")
        # Ignored/orphaned children impose no PDF requirement; the
        # independent filters do not cascade.
        validate_all_sheets_exist(email_cfg)


class TestChildGrainMembership:
    """Packets follow ``final_child.assigned_sponsor_id`` (§6.5)."""

    def test_split_family_builds_two_packets(
        self, cfg: Config, tmp_path: Path
    ) -> None:
        """Two children of one family can go to two sponsors."""
        children = [
            child_row("1A", "Male", 8, family_id=1),
            child_row("1B", "Female", 5, family_id=1),
        ]
        children[0]["assigned_sponsor_id"] = 7
        children[1]["assigned_sponsor_id"] = 8
        _seed(
            cfg,
            [
                sponsor_row(7, "Sam", "sam@x.com", None),
                sponsor_row(8, "Riley", "riley@x.com", None),
            ],
            children,
            families=[
                {
                    "family_id": 1,
                    "gift_pickup_location": "Hancock",
                    "assigned_sponsor_id": None,
                }
            ],
        )
        packets = {
            packet["sponsor_id"]: packet
            for packet in _sponsors_with_angels(cfg)
        }
        assert set(packets) == {7, 8}
        assert [a["angel_id"] for a in packets[7]["angels"]] == ["1A"]
        assert [a["angel_id"] for a in packets[8]["angels"]] == ["1B"]

        email_cfg = _email_cfg(cfg, tmp_path)
        build_packets(email_cfg)
        packets_dir = Path(email_cfg.sponsor_packets.output_dir)
        assert {d.name for d in packets_dir.iterdir()} == {"7", "8"}

    def test_shared_angel_letter_keeps_every_child(self, cfg: Config) -> None:
        """Two children sharing a letter both reach the packet."""
        first = child_row("1A", "Male", 8, family_id=1)
        second = child_row("1A", "Female", 6, family_id=2)
        second["zite_record_id"] = "rec_second"
        first["assigned_sponsor_id"] = 7
        second["assigned_sponsor_id"] = 7
        _seed(
            cfg,
            [sponsor_row(7, "Sam", "sam@x.com", "1A,1A")],
            [first, second],
            families=[
                {"family_id": 1, "gift_pickup_location": "Hancock"},
                {"family_id": 2, "gift_pickup_location": "Calumet"},
            ],
        )
        packets = _sponsors_with_angels(cfg)
        assert len(packets) == 1
        assert [
            (a["angel_id"], a["family_id"], a["dropoff"])
            for a in packets[0]["angels"]
        ] == [("1A", 1, "Hancock"), ("1A", 2, "Calumet")]

    def test_validation_reads_child_sponsor(
        self, cfg: Config, tmp_path: Path
    ) -> None:
        """A child's own sponsor decides whether its sheet is required."""
        children = [
            child_row("1A", "Male", 8, family_id=1),
            child_row("1B", "Female", 5, family_id=1),
        ]
        children[0]["assigned_sponsor_id"] = None  # unsponsored
        children[1]["assigned_sponsor_id"] = 7
        _seed(
            cfg,
            [sponsor_row(7, "Sam", "sam@x.com", None)],
            children,
            families=[
                {
                    "family_id": 1,
                    "gift_pickup_location": "Hancock",
                    "assigned_sponsor_id": 7,
                }
            ],
        )
        with pytest.raises(SponsorPacketError, match=r"\['1B'\]"):
            validate_all_sheets_exist(_email_cfg(cfg, tmp_path))


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
        workbook_before = Path(cfg.excel_file).read_bytes()
        build_packets(email_cfg)
        assert Path(cfg.excel_file).read_bytes() == workbook_before
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

    def test_build_only_requested_sponsor(
        self, cfg: Config, tmp_path: Path
    ) -> None:
        """``sponsor_id`` builds only that sponsor's packet."""
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
        )
        email_cfg = _email_cfg(cfg, tmp_path)
        build_packets(email_cfg, sponsor_id=9)
        packets = Path(email_cfg.sponsor_packets.output_dir)
        prefix = email_cfg.sponsor_packets.filename_prefix
        assert (packets / "9" / f"{prefix}_9.html").is_file()
        assert not (packets / "7").exists()

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
