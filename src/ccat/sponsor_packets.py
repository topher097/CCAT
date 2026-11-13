"""Module 5b — sponsor packet generation (§13b).

For every ``final_sponsor`` row with assigned angels, builds one
**sponsor packet** directory under ``sponsor_packets.output_dir``:

- ``<filename_prefix>_<sponsor_id>.html`` — the email draft: a greeting
  with the
  sponsor's first name, a table of their assigned angels (ID, gender,
  age at Christmas, drop-off location), and instructions referencing
  the two attached PDFs.  Meant for manual sending: open the file in a
  browser, copy everything below the subject header, and paste into
  any email client -- the formatting (greeting, table styling) is
  inline CSS, so it survives the paste.
- ``Sponsor <sponsor_id> - Child Information <season>.pdf`` — the
  sponsor's angel child information sheets combined into one PDF,
  ordered by family ID then angel letter.
- a copy of the sponsor instruction file
  (``sponsor_packets.instructions_file``, default ``data/Sponsor
  Instruction 2026.pdf``).

The HTML template lives at ``sponsor_packets.email_template_html_file``
(default
``data/sponsor_packet_email_2026.html``) and is a Jinja2 template
receiving ``season``, ``subject``, ``sponsor`` (the sponsor row),
``angels`` (each with ``angel_id``/``gender``/``age``/``dropoff``/
``family_id``), ``angel_count``, ``instructions_filename`` and
``child_information_filename`` (the two attached PDFs' names), and
``bcc_emails`` (the configured BCC address list).

A rerun skips sponsor directories whose files already exist
(``--overwrite`` regenerates them).
"""

import logging
import re
import shutil
from pathlib import Path
from typing import Any

from jinja2 import Environment
from pypdf import PdfWriter

from ccat.config import Config
from ccat.io import read_sheet

logger = logging.getLogger(__name__)


class SponsorPacketError(Exception):
    """Raised when sponsor packet generation cannot complete."""


#: Per-angel columns shown in the draft's table (§13).
ANGEL_COLUMNS = ("angel_id", "gender", "age_at_xmas", "dropoff")

_SUBJECT_TEMPLATE = (
    "Angel Tree {{ season }} — your {{ angel_count }} angel"
    "{{ 's' if angel_count != 1 }}"
)


def _angel_sort_key(angel: dict[str, Any]) -> tuple[int, str, int, str]:
    """Sort angels by family ID then angel letter (§13b).

    Args:
        angel: One angel payload row (needs ``angel_id``,
            ``family_id``).

    Returns:
        The (family_id, numeric part, letter length, letters) key, so
        ``1A`` < ``1B`` < ``1Z`` < ``1AA`` regardless of string order.
    """
    match = re.fullmatch(r"(\d+)([A-Z]+)", str(angel.get("angel_id", "")))
    if match:
        family_digits, letters = match.groups()
        return (
            int(angel.get("family_id") or 0),
            family_digits,
            len(letters),
            letters,
        )
    return (
        int(angel.get("family_id") or 0),
        str(angel.get("angel_id", "")),
        0,
        "",
    )


def _sponsors_with_angels(cfg: Config) -> list[dict[str, Any]]:
    """Join each assigned sponsor with their angels' details (§13b).

    Args:
        cfg: The pipeline configuration.

    Returns:
        One packet payload per sponsor with assigned angels, angels
        ordered by family ID then angel letter.  Sponsors whose
        ``assigned_angel_ids`` are empty or unparseable are skipped.

    Raises:
        SponsorPacketError: If ``final_sponsor``/``final_child`` is
            missing, or the workbook has no matched sponsors.
    """
    sponsors = read_sheet(cfg.excel_file, "final_sponsor")
    if sponsors is None or sponsors.is_empty():
        raise SponsorPacketError(
            "final_sponsor sheet is missing or empty; run `ccat match` first"
        )
    children = read_sheet(cfg.excel_file, "final_child")
    if children is None or children.is_empty():
        raise SponsorPacketError(
            "final_child sheet is missing or empty; run `ccat audit` first"
        )
    family = read_sheet(cfg.excel_file, "final_family")
    location_by_family: dict[Any, Any] = {}
    if family is None or family.is_empty():
        logger.warning(
            "final_family sheet is missing or empty; angel drop-off"
            " locations render blank (run `ccat match` first)"
        )
    else:
        location_by_family = {
            row.get("family_id"): row.get("gift_pickup_location")
            for row in family.to_dicts()
        }
    angels_by_id = {
        row["angel_id"]: row
        for row in children.to_dicts()
        if row.get("angel_id")
    }
    packets: list[dict[str, Any]] = []
    for row in sponsors.to_dicts():
        raw_ids = row.get("assigned_angel_ids") or ""
        angel_ids = [
            angel_id.strip()
            for angel_id in str(raw_ids).split(",")
            if angel_id.strip()
        ]
        if not angel_ids:
            continue
        unknown = [
            angel_id for angel_id in angel_ids if angel_id not in angels_by_id
        ]
        if unknown:
            logger.warning(
                "sponsor %s assigned unknown angel id(s) %s; skipping them",
                row.get("sponsor_id"),
                unknown,
            )
        angels = [
            {
                "angel_id": angel_id,
                "family_id": angels_by_id[angel_id].get("family_id"),
                "gender": angels_by_id[angel_id].get("gender"),
                "age": angels_by_id[angel_id].get("age_at_xmas"),
                "dropoff": location_by_family.get(
                    angels_by_id[angel_id].get("family_id")
                ),
            }
            for angel_id in angel_ids
            if angel_id in angels_by_id
        ]
        angels.sort(key=_angel_sort_key)
        packets.append(
            {
                "sponsor_id": row.get("sponsor_id"),
                "first_name": row.get("first_name") or "",
                "email": row.get("email"),
                "dropoff_location": row.get("dropoff_location"),
                "angels": angels,
            }
        )
    if not packets:
        raise SponsorPacketError(
            "no sponsor has assigned angels; run `ccat match` first"
        )
    return packets


def _sheet_pdf(cfg: Config, angel_id: str) -> Path:
    """Path of one angel's generated child information sheet PDF.

    Args:
        cfg: The pipeline configuration.
        angel_id: The angel's ID (names the file, §13).

    Returns:
        The PDF path under ``info_sheet.output_dir``.
    """
    return Path(cfg.info_sheet.output_dir) / f"AT{cfg.season}_{angel_id}.pdf"


def _merge_sheets_pdf(
    cfg: Config, packet_dir: Path, sponsor_id: Any, angels: list[dict[str, Any]]
) -> tuple[Path, list[str]]:
    """Combine the angel child information sheets into one PDF (§13b).

    Pages: each angel's child information sheet, ordered by family ID
    then angel letter.  Named
    ``Sponsor <sponsor_id> - Child Information <season>.pdf``.

    Args:
        cfg: The pipeline configuration.
        packet_dir: Destination directory (created by the caller).
        sponsor_id: The sponsor's ID (names the PDF).
        angels: The sponsor's ordered angel rows.

    Returns:
        A tuple of (the written PDF path, angel ids whose info sheet
        PDF was missing and therefore contributed no pages).
    """
    writer = PdfWriter()
    missing: list[str] = []
    for angel in angels:
        sheet = _sheet_pdf(cfg, str(angel["angel_id"]))
        if sheet.is_file():
            writer.append(str(sheet), outline_item=f"Angel {angel['angel_id']}")
        else:
            missing.append(str(angel["angel_id"]))
            logger.warning(
                "child information sheet not found for angel %s (%s);"
                " the combined PDF skips its pages",
                angel["angel_id"],
                sheet,
            )
    pdf_path = packet_dir / (
        f"Sponsor {sponsor_id} - Child Information {cfg.season}.pdf"
    )
    with pdf_path.open("wb") as output:
        writer.write(output)
    return pdf_path, missing


def run(cfg: Config, overwrite: bool = False) -> None:
    """Build one sponsor packet directory per assigned sponsor (§13b).

    Each packet directory holds the HTML email draft, the combined
    child-information PDF (all the sponsor's angel sheets, ordered by
    family ID then angel letter), and a copy of the sponsor
    instruction PDF.  Sponsors without assigned angels produce no
    directory.

    Args:
        cfg: The pipeline configuration.
        overwrite: Regenerate packet files that already exist; the
            default skips sponsors whose files exist so manual edits
            are never lost.

    Raises:
        SponsorPacketError: If the sheets are unusable, the template
            is missing, or no sponsor has assigned angels.
    """
    spec = cfg.sponsor_packets
    template_file = Path(spec.email_template_html_file)
    if not template_file.is_file():
        raise SponsorPacketError(
            f"email template file not found: {template_file}"
        )
    environment = Environment(autoescape=True)
    template = environment.from_string(
        template_file.read_text(encoding="utf-8")
    )
    instructions = Path(spec.instructions_file)
    if not instructions.is_file():
        logger.warning(
            "sponsor instruction file not found: %s; packets are"
            " written without the instruction copy",
            instructions,
        )
    out_dir = Path(spec.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    packets = _sponsors_with_angels(cfg)
    written = 0
    for packet in packets:
        packet_dir = out_dir / str(packet["sponsor_id"])
        html_path = packet_dir / (
            f"{spec.filename_prefix}_{packet['sponsor_id']}.html"
        )
        sheets_pdf_name = (
            f"Sponsor {packet['sponsor_id']} - Child Information"
            f" {cfg.season}.pdf"
        )
        sheets_pdf_path = packet_dir / sheets_pdf_name
        if not overwrite and html_path.exists() and sheets_pdf_path.exists():
            logger.info(
                "packet for sponsor %s exists; skipping",
                packet["sponsor_id"],
            )
            continue
        packet_dir.mkdir(parents=True, exist_ok=True)
        subject = environment.from_string(_SUBJECT_TEMPLATE).render(
            season=cfg.season,
            angel_count=len(packet["angels"]),
        )
        html_path.write_text(
            template.render(
                season=cfg.season,
                sponsor=packet,
                angels=packet["angels"],
                angel_count=len(packet["angels"]),
                subject=subject,
                instructions_filename=instructions.name,
                child_information_filename=sheets_pdf_name,
                bcc_emails=spec.bcc_emails,
            ),
            encoding="utf-8",
        )
        written += 1
        logger.info(
            "wrote email draft %s for sponsor %s (%d angel(s))",
            html_path,
            packet["sponsor_id"],
            len(packet["angels"]),
        )
        if instructions.is_file():
            shutil.copy2(instructions, packet_dir / instructions.name)
            logger.info("copied sponsor instructions into %s", packet_dir.name)
        sheets_pdf_path, missing = _merge_sheets_pdf(
            cfg, packet_dir, packet["sponsor_id"], packet["angels"]
        )
        if missing:
            logger.warning(
                "child information PDF %s written without sheet(s) for %s",
                sheets_pdf_path,
                missing,
            )
        else:
            logger.info("wrote child information PDF %s", sheets_pdf_path)
    logger.info(
        "wrote %d sponsor packet(s); %d skipped (already exist)",
        written,
        len(packets) - written,
    )
