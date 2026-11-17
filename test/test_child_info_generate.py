"""Tests for module 5 — child info sheet generation (§13)."""

import shutil
import threading
import time
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from pypdf import PdfReader, PdfWriter

import ccat.child_info_generate as gen
from ccat.child_info_generate import InfoSheetError
from ccat.config import Config, InfoSheetSpec
from ccat.audit.store import build_final_frame, read_final
from ccat.io import _final_child_formula_caches, read_sheet, write_sheets
from ccat.sponsor_packets import SponsorPacketError

#: The real season template, for the end-to-end PDF placeholder test.
REAL_TEMPLATE = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "child_information_sheet_2026.ott"
)


def child_row(
    record_id: str,
    angel_id: str,
    family_id: int | None,
    status: str = "audited",
) -> dict[str, Any]:
    """One final_child row for generate tests."""
    return {
        "zite_record_id": record_id,
        "family_id": family_id,
        "angel_id": angel_id,
        "status": status,
        "audited": status == "audited",
        "ignore": False,
        "generated_info_sheet": False,
        "dob": "2018-05-10",
        "age_at_xmas": 8,
        "gender": "female",
        "favorite_colors": "blue",
        "interests": "legos",
        "other_notes": None,
        "need_1": "coat",
        "need_2": None,
        "desire_1": "blocks",
        "desire_2": None,
        "shoe_size": "13",
        "shoe_size_type": "Baby/Infant",
        "pant_waist_size": None,
        "pant_inseam": None,
        "pant_size_letter": None,
        "pant_size_type": None,
        "jean_size": None,
        "shirt_size_letter": "XS",
        "shirt_size_type": "Youth",
    }


def _seed(
    cfg: Config,
    children: list[dict[str, Any]],
    families: list[dict[str, Any]],
    *,
    flags: list[bool] | None = None,
    sponsors: list[dict[str, Any]] | None = None,
) -> None:
    """Write full persistent child, family, and sponsor final stores."""
    if flags is not None:
        for row, flag in zip(children, flags, strict=True):
            row["generated_info_sheet"] = flag
    write_sheets(
        cfg.excel_file,
        "audit",
        {"final_child": build_final_frame("child", children)},
        xmas=cfg.xmas,
    )
    write_sheets(
        cfg.excel_file,
        "match",
        {
            "final_family": build_final_frame("family", families),
            "final_sponsor": build_final_frame(
                "sponsor", [_sponsor()] if sponsors is None else sponsors
            ),
        },
    )


def _assert_only_generated_flags_changed(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    expected_flags: dict[str, bool],
) -> None:
    """Check row survival, unchanged fields, and exact generation markers."""
    assert [row["zite_record_id"] for row in after] == [
        row["zite_record_id"] for row in before
    ]
    for old, new in zip(before, after, strict=True):
        record_id = old["zite_record_id"]
        assert new["generated_info_sheet"] is expected_flags[record_id]
        for column in old.keys() - {"generated_info_sheet"}:
            assert new[column] == old[column], (record_id, column)


def _family() -> list[dict[str, Any]]:
    """One final_family row joining the seeded children."""
    return [
        {
            "family_id": 1,
            "gift_pickup_location": "Hancock",
            "assigned_sponsor_id": 7,
        }
    ]


def _unassigned_family() -> list[dict[str, Any]]:
    """One final_family row with no assigned sponsor yet."""
    return [
        {
            "family_id": 1,
            "gift_pickup_location": "Hancock",
            "assigned_sponsor_id": None,
        }
    ]


def _sponsor(
    sponsor_id: int = 7, sponsor_type: str = "Individual", **overrides: Any
) -> dict[str, Any]:
    """One final_sponsor row for generate tests."""
    row: dict[str, Any] = {
        "sponsor_id": sponsor_id,
        "sponsor_type": sponsor_type,
        "first_name": "Sam",
        "last_name": "Sponsor",
        "organization_name": None,
        "email": "sam@example.com",
        "phone": "555-0100",
        "assigned_angel_ids": "1A,1B",
    }
    row.update(overrides)
    return row


@pytest.fixture
def gen_cfg(cfg: Config, tmp_path: Path) -> Config:
    """Config pointing at a stub template and temp output directories."""
    template = tmp_path / "template.ott"
    template.touch()
    spec = cfg.info_sheet.model_copy(
        update={
            "template_file": str(template),
            "output_dir": str(tmp_path / "sheets"),
        }
    )
    packets = cfg.sponsor_packets.model_copy(
        update={
            "output_dir": str(tmp_path / "packets"),
            "instructions_file": str(_instructions_pdf(tmp_path)),
        }
    )
    return cfg.model_copy(
        update={"info_sheet": spec, "sponsor_packets": packets}
    )


def _instructions_pdf(tmp_path: Path) -> Path:
    """Write a minimal real instruction PDF for the packet phase."""
    path = tmp_path / "instructions.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with path.open("wb") as output:
        writer.write(output)
    return path


@pytest.fixture
def two_children() -> list[dict[str, Any]]:
    """Two pending children in one family."""
    return [
        child_row("c1", "1A", 1),
        child_row("c2", "1B", 1),
    ]


class TestGenCfg:
    """The cfg fixture loads the real info_sheet section (§3)."""

    def test_cfg_loads_info_sheet(self, gen_cfg: Config) -> None:
        """The real season config validates the new section."""
        assert gen_cfg.info_sheet.template_file.endswith(".ott")
        assert len(gen_cfg.info_sheet.mappings) == 28
        assert gen_cfg.info_sheet.mappings["entry.colors"] == "favorite_colors"
        assert (
            gen_cfg.info_sheet.mappings["sponsor.full_name"] == "sponsor_name"
        )


class TestConfigMappings:
    """Mapping validation (§3)."""

    def _spec(self, **updates: Any) -> InfoSheetSpec:
        base: dict[str, Any] = {
            "template_file": "t.ott",
            "output_dir": "out",
            "mappings": {"info.angel_id": "angel_id"},
        }
        base.update(updates)
        return InfoSheetSpec(**base)

    def test_empty_mappings_rejected(self) -> None:
        """An empty mappings dict fails validation."""
        with pytest.raises(ValueError, match="must not be empty"):
            self._spec(mappings={})

    def test_undotted_variable_rejected(self) -> None:
        """Variables without a namespace fail validation."""
        with pytest.raises(ValueError, match="dotted py3o path"):
            self._spec(mappings={"colors": "favorite_colors"})

    def test_empty_column_rejected(self) -> None:
        """An empty mapping target fails validation."""
        with pytest.raises(ValueError, match="empty"):
            self._spec(mappings={"entry.colors": ""})


class TestNestedData:
    """Nested py3o data construction (§13.2)."""

    def test_dotted_expansion(self) -> None:
        """Dotted variables expand into namespace dicts."""
        row = {"favorite_colors": "blue", "age_at_xmas": 8}
        data = gen._nested_data(
            row, {"entry.colors": "favorite_colors", "info.age": "age_at_xmas"}
        )
        assert data == {
            "entry": {"colors": "blue"},
            "info": {"age": 8},
            "sponsor": {
                "full_name": "",
                "email": "",
                "phone": "",
                "sponsor_id": "",
                "angel_id": "",
                "family_size": "",
            },
        }

    def test_null_renders_empty(self) -> None:
        """Null values render as empty strings."""
        data = gen._nested_data({}, {"entry.other_info": "other_notes"})
        assert data["entry"]["other_info"] == ""

    def test_deeply_nested_variable(self) -> None:
        """Three-level variables expand fully (entry.footwear.size)."""
        data = gen._nested_data(
            {"shoe_size": "1", "shoe_size_type": "Youth"},
            {
                "entry.footwear.size": "shoe_size",
                "entry.footwear.size_type": "shoe_size_type",
            },
        )
        assert data == {
            "entry": {"footwear": {"size": "1", "size_type": "Youth"}},
            "sponsor": {
                "full_name": "",
                "email": "",
                "phone": "",
                "sponsor_id": "",
                "angel_id": "",
                "family_size": "",
            },
        }

    def test_sponsor_blank(self) -> None:
        """The sponsor hand-fill section always renders blank."""
        data = gen._nested_data({}, {"info.angel_id": "angel_id"})
        assert all(v == "" for v in data["sponsor"].values())

    def test_sponsor_contact_missing_renders_placeholder(self) -> None:
        """A listed-affiliation sponsor missing contact gets placeholder.

        Underscore line (§13.2).
        """
        mappings = {
            "sponsor.full_name": "sponsor_name",
            "sponsor.email": "sponsor_email",
            "sponsor.phone": "sponsor_phone",
            "sponsor.sponsor_id": "assigned_sponsor_id",
        }
        row = {
            "assigned_sponsor_id": 7,
            "sponsor_type": "Organization",
            "sponsor_name": None,
            "sponsor_email": "",
            "sponsor_phone": "   ",
        }
        data = gen._nested_data(
            row,
            mappings,
            placeholder_affiliations=frozenset({"Organization", "Walmart"}),
        )
        placeholder = gen.SPONSOR_CONTACT_PLACEHOLDER
        assert placeholder == "_" * 30
        assert data["sponsor"]["full_name"] == placeholder
        assert data["sponsor"]["email"] == placeholder
        assert data["sponsor"]["phone"] == placeholder
        # Always-known values stay blank, never placeholders.
        assert data["sponsor"]["sponsor_id"] == 7

    def test_sponsor_contact_individual_renders_blank(self) -> None:
        """An Individual sponsor with missing contact info stays blank."""
        mappings = {
            "sponsor.full_name": "sponsor_name",
            "sponsor.email": "sponsor_email",
        }
        row = {
            "assigned_sponsor_id": 7,
            "sponsor_type": "Individual",
            "sponsor_name": None,
            "sponsor_email": None,
        }
        data = gen._nested_data(
            row,
            mappings,
            placeholder_affiliations=frozenset({"Organization", "Walmart"}),
        )
        assert data["sponsor"]["full_name"] == ""
        assert data["sponsor"]["email"] == ""

    def test_sponsor_contact_partial_placeholders_all_three(
        self,
    ) -> None:
        """One missing field placeholders all three (§13.2)."""
        mappings = {
            "sponsor.full_name": "sponsor_name",
            "sponsor.email": "sponsor_email",
            "sponsor.phone": "sponsor_phone",
        }
        row = {
            "assigned_sponsor_id": 7,
            "sponsor_type": "Organization",
            "sponsor_name": "Sam Sponsor",
            "sponsor_email": "sam@example.com",
            "sponsor_phone": None,
        }
        data = gen._nested_data(
            row,
            mappings,
            placeholder_affiliations=frozenset({"Organization", "Walmart"}),
        )
        placeholder = gen.SPONSOR_CONTACT_PLACEHOLDER
        assert data["sponsor"]["full_name"] == placeholder
        assert data["sponsor"]["email"] == placeholder
        assert data["sponsor"]["phone"] == placeholder

    def test_sponsor_contact_complete_renders_values(self) -> None:
        """A fully documented sponsor keeps all real values."""
        mappings = {
            "sponsor.full_name": "sponsor_name",
            "sponsor.email": "sponsor_email",
            "sponsor.phone": "sponsor_phone",
        }
        row = {
            "assigned_sponsor_id": 7,
            "sponsor_type": "Organization",
            "sponsor_name": "Sam Sponsor",
            "sponsor_email": "sam@example.com",
            "sponsor_phone": "555-0100",
        }
        data = gen._nested_data(
            row,
            mappings,
            placeholder_affiliations=frozenset({"Organization", "Walmart"}),
        )
        assert data["sponsor"]["full_name"] == "Sam Sponsor"
        assert data["sponsor"]["email"] == "sam@example.com"
        assert data["sponsor"]["phone"] == "555-0100"

    def test_sponsor_contact_unassigned_child_stays_blank(self) -> None:
        """A child without an assigned sponsor keeps blank contact."""
        mappings = {"sponsor.phone": "sponsor_phone"}
        data = gen._nested_data({"sponsor_phone": None}, mappings)
        assert data["sponsor"]["phone"] == ""

    def test_sponsor_contact_placeholder_length_configurable(self) -> None:
        """The placeholder length comes from the config (§13.2)."""
        mappings = {"sponsor.phone": "sponsor_phone"}
        data = gen._nested_data(
            {
                "assigned_sponsor_id": 7,
                "sponsor_type": "Walmart",
                "sponsor_phone": None,
            },
            mappings,
            contact_placeholder="_" * 10,
            placeholder_affiliations=frozenset({"Walmart"}),
        )
        assert data["sponsor"]["phone"] == "_" * 10

    def test_sponsor_angel_id_matches_info_angel_id(self) -> None:
        """``sponsor.angel_id`` fills with the child's own angel id."""
        mappings = {"info.angel_id": "angel_id", "sponsor.angel_id": "angel_id"}
        data = gen._nested_data({"angel_id": "3C"}, mappings)
        assert data["sponsor"]["angel_id"] == "3C"
        assert data["sponsor"]["angel_id"] == data["info"]["angel_id"]


class TestLoadEnriched:
    """final_child enrichment with family data (§13.1)."""

    def test_family_size_and_joined_columns(
        self, gen_cfg: Config, two_children: list[dict[str, Any]]
    ) -> None:
        """family_size counts rows per family_id; join columns land."""
        _seed(gen_cfg, two_children, _family())
        _base, enriched = gen._load_enriched_rows(gen_cfg)
        assert enriched["family_size"].to_list() == [2, 2]
        assert enriched["gift_pickup_location"].to_list() == [
            "Hancock",
            "Hancock",
        ]
        assert enriched["assigned_sponsor_id"].to_list() == [7, 7]
        assert enriched["sponsor_email"].to_list() == [
            "sam@example.com",
            "sam@example.com",
        ]
        assert enriched["sponsor_name"].to_list() == [
            "Sam Sponsor",
            "Sam Sponsor",
        ]
        # sponsor.family_size maps to the child's own family_size.
        assert enriched["family_size"].to_list() == [2, 2]

    def test_missing_final_child(self, gen_cfg: Config) -> None:
        """An empty workbook raises a typed error."""
        with pytest.raises(InfoSheetError, match="final_child sheet"):
            gen._load_enriched_rows(gen_cfg)

    def test_missing_final_family(
        self, gen_cfg: Config, two_children: list[dict[str, Any]]
    ) -> None:
        """A missing final_family sheet raises a typed error."""
        _seed(gen_cfg, two_children, _family())
        (Path(gen_cfg.excel_file)).unlink()
        write_sheets(
            gen_cfg.excel_file,
            "audit",
            {"final_child": build_final_frame("child", two_children)},
            xmas=gen_cfg.xmas,
        )
        with pytest.raises(InfoSheetError, match="final_family sheet"):
            gen._load_enriched_rows(gen_cfg)

    def test_unmatched_family_id(
        self, gen_cfg: Config, two_children: list[dict[str, Any]]
    ) -> None:
        """A family_id with no final_family row raises a typed error."""
        _seed(
            gen_cfg,
            two_children,
            [
                {
                    "family_id": 2,
                    "gift_pickup_location": "Calumet",
                    "assigned_sponsor_id": 9,
                }
            ],
        )
        with pytest.raises(InfoSheetError, match="no row for family_id"):
            gen._load_enriched_rows(gen_cfg)

    def test_duplicate_family_id(
        self,
        gen_cfg: Config,
        two_children: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Duplicate final_family family_id rows raise a typed error."""
        _seed(gen_cfg, two_children, _family())
        read_eligible = gen.read_eligible_final_frame

        def duplicate_family(cfg: Config, entity: str) -> pl.DataFrame:
            frame = read_eligible(cfg, entity)
            return pl.concat([frame, frame]) if entity == "family" else frame

        monkeypatch.setattr(gen, "read_eligible_final_frame", duplicate_family)
        with pytest.raises(InfoSheetError, match="not unique"):
            gen._load_enriched_rows(gen_cfg)

    def test_null_family_id(
        self, gen_cfg: Config, two_children: list[dict[str, Any]]
    ) -> None:
        """A child without family_id is ignored (orphaned), not an error."""
        two_children[0]["family_id"] = None
        _seed(gen_cfg, two_children, _family())
        base, enriched = gen._load_enriched_rows(gen_cfg)
        assert base.height == 1  # the orphaned row is dropped
        assert enriched["family_id"].to_list() == [1]
        assert base["zite_record_id"].to_list() == [
            two_children[1]["zite_record_id"]
        ]
        assert len(read_final(gen_cfg, "child")) == 2

    def test_ignored_and_orphan_children_filtered_before_enrichment(
        self, gen_cfg: Config
    ) -> None:
        """Output filters omit ineligible children without deleting them."""
        children = [
            child_row("c1", "1A", 1),
            child_row("c2", "1B", 1, status="ignored"),
            child_row("c3", "1C", None),
        ]
        children[1]["ignore"] = True
        _seed(gen_cfg, children, _family())
        base, enriched = gen._load_enriched_rows(gen_cfg)
        assert base["zite_record_id"].to_list() == ["c1"]
        assert enriched["family_size"].to_list() == [1]
        assert [
            row["zite_record_id"] for row in read_final(gen_cfg, "child")
        ] == [
            "c1",
            "c2",
            "c3",
        ]

    def test_all_null_family_id(
        self, gen_cfg: Config, two_children: list[dict[str, Any]]
    ) -> None:
        """Every child orphaned -> no eligible output population."""
        two_children[0]["family_id"] = None
        two_children[1]["family_id"] = None
        _seed(gen_cfg, two_children, _family())
        with pytest.raises(InfoSheetError, match="no eligible rows"):
            gen._load_enriched_rows(gen_cfg)
        assert len(read_final(gen_cfg, "child")) == 2

    def test_final_family_missing_join_column(
        self,
        gen_cfg: Config,
        two_children: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """final_family without the join columns raises a typed error."""
        _seed(gen_cfg, two_children, _family())
        read_eligible = gen.read_eligible_final_frame

        def missing_family_column(cfg: Config, entity: str) -> pl.DataFrame:
            frame = read_eligible(cfg, entity)
            return (
                frame.drop("gift_pickup_location")
                if entity == "family"
                else frame
            )

        monkeypatch.setattr(
            gen, "read_eligible_final_frame", missing_family_column
        )
        with pytest.raises(InfoSheetError, match="lacks column"):
            gen._load_enriched_rows(gen_cfg)

    def test_missing_final_sponsor(
        self, gen_cfg: Config, two_children: list[dict[str, Any]]
    ) -> None:
        """A missing or empty final_sponsor sheet raises a typed error."""
        _seed(gen_cfg, two_children, _family(), sponsors=[])
        with pytest.raises(InfoSheetError, match="final_sponsor sheet"):
            gen._load_enriched_rows(gen_cfg)

    def test_final_sponsor_missing_join_column(
        self,
        gen_cfg: Config,
        two_children: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """final_sponsor without the join columns raises a typed error."""
        _seed(gen_cfg, two_children, _family())
        read_eligible = gen.read_eligible_final_frame

        def missing_sponsor_column(cfg: Config, entity: str) -> pl.DataFrame:
            frame = read_eligible(cfg, entity)
            return frame.drop("email") if entity == "sponsor" else frame

        monkeypatch.setattr(
            gen, "read_eligible_final_frame", missing_sponsor_column
        )
        with pytest.raises(InfoSheetError, match="lacks column"):
            gen._load_enriched_rows(gen_cfg)

    def test_duplicate_sponsor_id(
        self,
        gen_cfg: Config,
        two_children: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Duplicate final_sponsor sponsor_id rows raise a typed error."""
        _seed(gen_cfg, two_children, _family())
        read_eligible = gen.read_eligible_final_frame

        def duplicate_sponsor(cfg: Config, entity: str) -> pl.DataFrame:
            frame = read_eligible(cfg, entity)
            return pl.concat([frame, frame]) if entity == "sponsor" else frame

        monkeypatch.setattr(gen, "read_eligible_final_frame", duplicate_sponsor)
        with pytest.raises(InfoSheetError, match="not unique"):
            gen._load_enriched_rows(gen_cfg)

    def test_unassigned_child_sponsor_columns_null(
        self, gen_cfg: Config, two_children: list[dict[str, Any]]
    ) -> None:
        """A family without an assigned sponsor leaves sponsor sources null."""
        _seed(gen_cfg, two_children, _unassigned_family())
        _base, enriched = gen._load_enriched_rows(gen_cfg)
        assert enriched["sponsor_email"].to_list() == [None, None]
        assert enriched["sponsor_name"].to_list() == ["", ""]
        # family_size is still computed (number of angels in family).
        assert enriched["family_size"].to_list() == [2, 2]


class TestPendingRows:
    """Pending-row selection and guards (§13.4)."""

    def _frame(
        self,
        flags: list[Any],
        angel_ids: list[str | None],
        sponsor_ids: list[int | None] | None = None,
    ) -> pl.DataFrame:
        if sponsor_ids is None:
            sponsor_ids = [None] * len(flags)
        rows = [
            {
                "zite_record_id": f"c{i}",
                "angel_id": angel,
                "generated_info_sheet": flag,
                "assigned_sponsor_id": sponsor,
                "status": "audited",
            }
            for i, (flag, angel, sponsor) in enumerate(
                zip(flags, angel_ids, sponsor_ids, strict=True)
            )
        ]
        return pl.DataFrame(
            rows,
            schema={
                "zite_record_id": pl.String,
                "angel_id": pl.String,
                "generated_info_sheet": pl.Boolean,
                "assigned_sponsor_id": pl.Int64,
                "status": pl.String,
            },
        )

    def test_filters_false_rows(self) -> None:
        """Only False-flagged, sponsored rows are pending."""
        pending, _skipped = gen._pending_rows(
            self._frame(
                [True, False, False],
                ["1A", "1B", "1C"],
                [7, 7, None],
            )
        )
        assert pending["zite_record_id"].to_list() == ["c1"]

    def test_unsponsored_rows_not_pending(self) -> None:
        """Children without an assigned sponsor are skipped, flag ignored."""
        pending, skipped = gen._pending_rows(
            self._frame(
                [False, False, False],
                ["1A", "1B", "1C"],
                [None, 8, None],
            )
        )
        assert pending["zite_record_id"].to_list() == ["c1"]
        assert skipped == 2

    def test_unsponsored_rows_still_skipped_on_overwrite(self) -> None:
        """--overwrite re-renders flagged rows, never unsponsored ones."""
        pending, skipped = gen._pending_rows(
            self._frame(
                [True, True],
                ["1A", "1B"],
                [None, 7],
            ),
            overwrite=True,
        )
        assert pending["zite_record_id"].to_list() == ["c1"]
        assert skipped == 1

    def test_null_angel_id_rejected(self) -> None:
        """A pending row without angel_id raises a typed error."""
        with pytest.raises(InfoSheetError, match="usable angel_id"):
            gen._pending_rows(self._frame([False], [None], [7]))

    def test_path_separator_angel_id_rejected(self) -> None:
        """angel_id with a path separator raises a typed error."""
        with pytest.raises(InfoSheetError, match="usable angel_id"):
            gen._pending_rows(self._frame([False], ["../evil"], [7]))


class TestRun:
    """The run() orchestration (§13.4)."""

    @pytest.fixture
    def render_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> list[tuple[str, dict[str, Any]]]:
        """Stub out py3o rendering and soffice, recording renders."""
        calls: list[tuple[str, dict[str, Any]]] = []

        def fake_render(
            template_file: Path, data: dict[str, Any], odt_path: Path
        ) -> None:
            calls.append((odt_path.name, data))

        def fake_convert(
            odt_path: Path,
            out_dir: Path,
            soffice_binary: str,
            profile: Path,
            stop: threading.Event | None = None,
        ) -> Path:
            # Real PDF bytes: the packet phase merges these files.
            writer = PdfWriter()
            writer.add_blank_page(width=612, height=792)
            pdf_path = out_dir / f"{odt_path.stem}.pdf"
            with pdf_path.open("wb") as output:
                writer.write(output)
            return pdf_path

        monkeypatch.setattr(gen, "_render_one", fake_render)
        monkeypatch.setattr(gen, "_convert_to_pdf", fake_convert)
        return calls

    def test_generates_pending_and_flags(
        self,
        gen_cfg: Config,
        two_children: list[dict[str, Any]],
        render_calls: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """Pending rows are rendered, flagged True, and written back."""
        _seed(gen_cfg, two_children, _family())
        before = read_final(gen_cfg, "child")
        formula_caches = _final_child_formula_caches(Path(gen_cfg.excel_file))
        gen.run(gen_cfg)
        stem = f"AT{gen_cfg.season}_"
        names = sorted(name for name, _data in render_calls)
        assert names == [f"{stem}1A.odt", f"{stem}1B.odt"]
        pdfs = sorted(
            str(p.relative_to(gen_cfg.info_sheet.output_dir))
            for p in Path(gen_cfg.info_sheet.output_dir).rglob("*.pdf")
        )
        assert pdfs == [
            f"hancock/{stem}1A.pdf",
            f"hancock/{stem}1B.pdf",
        ]
        frame = read_sheet(gen_cfg.excel_file, "final_child")
        assert frame is not None
        assert frame["generated_info_sheet"].to_list() == [True, True]
        after = read_final(gen_cfg, "child")
        _assert_only_generated_flags_changed(
            before,
            after,
            {"c1": True, "c2": True},
        )
        # Enrichment is transient; writes keep the complete final schema.
        assert "gift_pickup_location" not in frame.columns
        assert "family_size" not in frame.columns
        # Generation writes supply xmas and retain live formulas/cached ages.
        new_caches = _final_child_formula_caches(Path(gen_cfg.excel_file))
        assert formula_caches
        assert new_caches.keys() == formula_caches.keys()
        assert new_caches == formula_caches

    def test_render_data_contents(
        self,
        gen_cfg: Config,
        two_children: list[dict[str, Any]],
        render_calls: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """The rendered data dict carries the enriched row values (§13.2)."""
        _seed(gen_cfg, two_children, _family())
        gen.run(gen_cfg)
        _name, data = next(
            call
            for call in render_calls
            if call[0] == f"AT{gen_cfg.season}_1A.odt"
        )
        assert data["info"] == {
            "angel_id": "1A",
            "age": 8,
            "gender": "female",
            "family_size": 2,
            "drop_off_location": "Hancock",
            "sponsor_id": 7,
        }
        assert data["entry"]["colors"] == "blue"
        assert data["entry"]["other_info"] == ""
        assert data["sponsor"] == {
            "full_name": "Sam Sponsor",
            "email": "sam@example.com",
            "phone": "555-0100",
            "sponsor_id": 7,
            # The child's own angel id, same as info.angel_id.
            "angel_id": "1A",
            "family_size": 2,
        }

    def test_unsponsored_children_skipped_end_to_end(
        self,
        gen_cfg: Config,
        two_children: list[dict[str, Any]],
        render_calls: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """Children without an assigned sponsor get no sheet (§13.4)."""
        _seed(
            gen_cfg,
            two_children,
            _unassigned_family(),
            flags=[False, False],
        )
        gen.run(gen_cfg)
        assert render_calls == []
        frame = read_sheet(gen_cfg.excel_file, "final_child")
        assert frame is not None
        assert frame["generated_info_sheet"].to_list() == [False, False]

    def test_unaudited_children_skipped_end_to_end(
        self,
        gen_cfg: Config,
        two_children: list[dict[str, Any]],
        render_calls: list[tuple[str, dict[str, Any]]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Children whose status is not audited get no sheet (§7.5)."""
        unaudited = [
            {**two_children[0], "status": "new"},
            two_children[1],
        ]
        _seed(gen_cfg, unaudited, _family())
        # Focus is the status gate; stub out the packet phase (its
        # combined PDFs would fail validation for the skipped child).
        monkeypatch.setattr(
            gen.sponsor_packets,
            "validate_all_sheets_exist",
            lambda cfg: None,
        )
        monkeypatch.setattr(
            gen.sponsor_packets, "build_packets", lambda cfg, overwrite: None
        )
        gen.run(gen_cfg)
        assert [name for name, _data in render_calls] == [
            f"AT{gen_cfg.season}_1B.odt"
        ]

    def test_organization_sponsor_uses_organization_name(
        self,
        gen_cfg: Config,
        two_children: list[dict[str, Any]],
        render_calls: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """Organization sponsors render organization_name as full_name (§13.2)."""
        _seed(
            gen_cfg,
            two_children,
            _family(),
            sponsors=[
                _sponsor(
                    first_name=None,
                    last_name=None,
                    organization_name="CCAM Group",
                    email="group@example.org",
                )
            ],
        )
        gen.run(gen_cfg)
        _name, data = render_calls[0]
        assert data["sponsor"]["full_name"] == "CCAM Group"
        assert data["sponsor"]["email"] == "group@example.org"

    def test_missing_sponsor_contact_renders_placeholder(
        self,
        gen_cfg: Config,
        two_children: list[dict[str, Any]],
        render_calls: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """A sponsor lacking contact info renders placeholders.

        Full run passes them into the template data (§13.2).
        """
        _seed(
            gen_cfg,
            two_children,
            _family(),
            sponsors=[
                _sponsor(
                    sponsor_type="Organization",
                    email=None,
                    phone=None,
                )
            ],
        )
        gen.run(gen_cfg)
        _name, data = render_calls[0]
        placeholder = gen.SPONSOR_CONTACT_PLACEHOLDER
        # All-or-nothing: missing email/phone placeholders the name too.
        assert data["sponsor"]["full_name"] == placeholder
        assert data["sponsor"]["email"] == placeholder
        assert data["sponsor"]["phone"] == placeholder

    def test_individual_sponsor_missing_contact_renders_blank(
        self,
        gen_cfg: Config,
        two_children: list[dict[str, Any]],
        render_calls: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """An Individual sponsor missing contact info stays blank.

        Not in the configured placeholder affiliations (§13.2).
        """
        _seed(
            gen_cfg,
            two_children,
            _family(),
            sponsors=[_sponsor(email=None, phone=None)],
        )
        gen.run(gen_cfg)
        _name, data = render_calls[0]
        assert data["sponsor"]["full_name"] == "Sam Sponsor"
        assert data["sponsor"]["email"] == ""
        assert data["sponsor"]["phone"] == ""

    def test_contact_placeholder_length_from_config(
        self,
        gen_cfg: Config,
        two_children: list[dict[str, Any]],
        render_calls: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """The rendered placeholder uses the configured length (§13.2)."""
        spec = gen_cfg.info_sheet.model_copy(
            update={"contact_placeholder_length": 12}
        )
        scoped = gen_cfg.model_copy(update={"info_sheet": spec})
        _seed(
            scoped,
            two_children,
            _family(),
            sponsors=[
                _sponsor(sponsor_type="Organization", email=None, phone=None)
            ],
        )
        gen.run(scoped)
        _name, data = render_calls[0]
        assert data["sponsor"]["email"] == "_" * 12
        assert data["sponsor"]["phone"] == "_" * 12

    def test_skips_true_rows(
        self,
        gen_cfg: Config,
        two_children: list[dict[str, Any]],
        render_calls: list[tuple[str, dict[str, Any]]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Rows already flagged True are not re-rendered (§13.4)."""
        _seed(
            gen_cfg,
            two_children,
            _family(),
            flags=[True, False],
        )
        # Focus is the flag filter; stub out the packet phase (its
        # sheets come from earlier runs in real pipelines).
        monkeypatch.setattr(
            gen.sponsor_packets,
            "validate_all_sheets_exist",
            lambda cfg: None,
        )
        monkeypatch.setattr(
            gen.sponsor_packets, "build_packets", lambda cfg, overwrite: None
        )
        gen.run(gen_cfg)
        assert [name for name, _data in render_calls] == [
            f"AT{gen_cfg.season}_1B.odt"
        ]

    def test_test_mode_stops_after_first(
        self,
        gen_cfg: Config,
        two_children: list[dict[str, Any]],
        render_calls: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """--test stops after the first generated sheet (§13.4)."""
        _seed(gen_cfg, two_children, _family())
        gen.run(gen_cfg, test=True)
        assert [name for name, _data in render_calls] == [
            f"AT{gen_cfg.season}_1A.odt"
        ]
        frame = read_sheet(gen_cfg.excel_file, "final_child")
        assert frame is not None
        assert frame["generated_info_sheet"].to_list() == [True, False]

    def test_overwrite_rerenders_all_rows(
        self,
        gen_cfg: Config,
        two_children: list[dict[str, Any]],
        render_calls: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """--overwrite regenerates flagged rows too (§13.4)."""
        _seed(
            gen_cfg,
            two_children,
            _family(),
            flags=[True, True],
        )
        gen.run(gen_cfg, overwrite=True)
        stem = f"AT{gen_cfg.season}_"
        assert sorted(name for name, _data in render_calls) == [
            f"{stem}1A.odt",
            f"{stem}1B.odt",
        ]
        frame = read_sheet(gen_cfg.excel_file, "final_child")
        assert frame is not None
        assert frame["generated_info_sheet"].to_list() == [True, True]

    def test_overwrite_test_mode_stops_after_first(
        self,
        gen_cfg: Config,
        two_children: list[dict[str, Any]],
        render_calls: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """--overwrite --test re-renders only the first sheet (§13.4)."""
        _seed(
            gen_cfg,
            two_children,
            _family(),
            flags=[True, True],
        )
        gen.run(gen_cfg, test=True, overwrite=True)
        assert [name for name, _data in render_calls] == [
            f"AT{gen_cfg.season}_1A.odt"
        ]

    def test_unsponsored_children_skipped_overwrite_end_to_end(
        self,
        gen_cfg: Config,
        render_calls: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """--overwrite regenerates sponsored children only."""
        children = [
            child_row("c1", "1A", 1),
            child_row("c2", "2A", 2),
        ]
        families = [
            {
                "family_id": 1,
                "gift_pickup_location": "Hancock",
                "assigned_sponsor_id": None,
            },
            {
                "family_id": 2,
                "gift_pickup_location": "Calumet",
                "assigned_sponsor_id": 7,
            },
        ]
        _seed(gen_cfg, children, families)
        gen.run(gen_cfg, overwrite=True)
        assert [name for name, _data in render_calls] == [
            f"AT{gen_cfg.season}_2A.odt"
        ]
        pdfs = sorted(
            str(p.relative_to(gen_cfg.info_sheet.output_dir))
            for p in Path(gen_cfg.info_sheet.output_dir).rglob("*.pdf")
        )
        assert pdfs == [f"calumet/AT{gen_cfg.season}_2A.pdf"]

    def test_failure_keeps_flag_and_raises(
        self,
        gen_cfg: Config,
        two_children: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed row keeps False, is reported, successes still flag (§13.4)."""

        def failing_render(
            template_file: Path, data: dict[str, Any], odt_path: Path
        ) -> None:
            if odt_path.stem.endswith("1A"):
                raise InfoSheetError("boom")

        def fake_convert(
            odt_path: Path,
            out_dir: Path,
            soffice_binary: str,
            profile: Path,
            stop: threading.Event | None = None,
        ) -> Path:
            writer = PdfWriter()
            writer.add_blank_page(width=612, height=792)
            pdf_path = out_dir / f"{odt_path.stem}.pdf"
            with pdf_path.open("wb") as output:
                writer.write(output)
            return pdf_path

        monkeypatch.setattr(gen, "_render_one", failing_render)
        monkeypatch.setattr(gen, "_convert_to_pdf", fake_convert)
        _seed(gen_cfg, two_children, _family())
        before = read_final(gen_cfg, "child")
        with pytest.raises(InfoSheetError, match="1A"):
            gen.run(gen_cfg)
        after = read_final(gen_cfg, "child")
        _assert_only_generated_flags_changed(
            before,
            after,
            {"c1": False, "c2": True},
        )

    def test_commit_preserves_full_store_and_formula_caches(
        self,
        gen_cfg: Config,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Marker commits retain ignored, orphan, and raw-missing rows."""
        children = [
            child_row("c1", "1A", 1),
            child_row("c2", "1B", 1, status="ignored"),
            child_row("c3", "1C", None),
            child_row("c4", "1D", 1),
        ]
        children[1].update(ignore=True, other_notes="ignored manual edit")
        children[3].update(other_notes="raw-missing manual edit", source=None)
        _seed(gen_cfg, children, _family())
        before = read_final(gen_cfg, "child")
        caches = _final_child_formula_caches(Path(gen_cfg.excel_file))
        writes: list[tuple[str, str, int]] = []
        original = gen.write_final_rows

        def recording_write(
            cfg: Config,
            module: str,
            entity: str,
            rows: list[dict[str, Any]],
        ) -> None:
            writes.append((module, entity, len(rows)))
            original(cfg, module=module, entity=entity, rows=rows)

        monkeypatch.setattr(gen, "write_final_rows", recording_write)
        gen._commit_generated_flags(gen_cfg, ["c1"])
        after = read_final(gen_cfg, "child")
        assert writes == [("generate", "child", 4)]
        _assert_only_generated_flags_changed(
            before,
            after,
            {"c1": True, "c2": False, "c3": False, "c4": False},
        )
        assert after[1]["other_notes"] == "ignored manual edit"
        assert after[3]["other_notes"] == "raw-missing manual edit"
        assert _final_child_formula_caches(Path(gen_cfg.excel_file)) == caches

    def test_no_pending_no_write(
        self,
        gen_cfg: Config,
        two_children: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """All done -> nothing rendered and the workbook is untouched."""
        calls: list[Path] = []
        original = gen.write_final_rows

        def counting_write(
            cfg: Config,
            module: str,
            entity: str,
            rows: list[dict[str, Any]],
        ) -> None:
            calls.append(Path(cfg.excel_file))
            original(cfg, module=module, entity=entity, rows=rows)

        monkeypatch.setattr(gen, "write_final_rows", counting_write)
        _seed(
            gen_cfg,
            two_children,
            _family(),
            flags=[True, True],
        )
        # Focus is the workbook untouched; stub out the packet phase.
        monkeypatch.setattr(
            gen.sponsor_packets,
            "validate_all_sheets_exist",
            lambda cfg: None,
        )
        monkeypatch.setattr(
            gen.sponsor_packets, "build_packets", lambda cfg, overwrite: None
        )
        gen.run(gen_cfg)
        assert calls == []

    def test_missing_template(self, gen_cfg: Config) -> None:
        """A missing template file raises a typed error up front."""
        spec = gen_cfg.info_sheet.model_copy(
            update={"template_file": "nowhere.ott"}
        )
        broken = gen_cfg.model_copy(update={"info_sheet": spec})
        with pytest.raises(InfoSheetError, match="template file not found"):
            gen.run(broken)

    def test_sponsor_option_limits_sheets_and_packet(
        self,
        gen_cfg: Config,
        render_calls: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """--sponsor renders only that sponsor's children and packet."""
        children = [child_row("c1", "1A", 1), child_row("c2", "2A", 2)]
        families = [
            {
                "family_id": 1,
                "gift_pickup_location": "Hancock",
                "assigned_sponsor_id": 7,
            },
            {
                "family_id": 2,
                "gift_pickup_location": "Calumet",
                "assigned_sponsor_id": 8,
            },
        ]
        _seed(
            gen_cfg,
            children,
            families,
            sponsors=[_sponsor(sponsor_id=7), _sponsor(sponsor_id=8)],
        )
        gen.run(gen_cfg, sponsor=7)
        # Only sponsor 7's child was rendered.
        assert [name for name, _data in render_calls] == [
            f"AT{gen_cfg.season}_1A.odt"
        ]
        # Only sponsor 7's packet directory was built.
        packets_root = Path(gen_cfg.sponsor_packets.output_dir)
        assert [d.name for d in sorted(packets_root.iterdir())] == ["7"]

    def test_sponsor_option_unknown_sponsor_raises(
        self,
        gen_cfg: Config,
        two_children: list[dict[str, Any]],
        render_calls: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """--sponsor with an unknown sponsor id raises a typed error."""
        _seed(gen_cfg, two_children, _family())
        with pytest.raises(SponsorPacketError, match="sponsor 9"):
            gen.run(gen_cfg, sponsor=9)
        assert render_calls == []


class TestParallelGeneration:
    """The thread-pool sheet phase (§13.4): parallelism and Ctrl+C."""

    @pytest.fixture
    def pdf_convert(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> list[tuple[str, Path]]:
        """Stub rendering and conversion, recording conversions."""
        converts: list[tuple[str, Path]] = []

        def fake_render(
            template_file: Path, data: dict[str, Any], odt_path: Path
        ) -> None:
            pass

        def fake_convert(
            odt_path: Path,
            out_dir: Path,
            soffice_binary: str,
            profile: Path,
            stop: threading.Event | None = None,
        ) -> Path:
            converts.append((odt_path.stem, profile))
            writer = PdfWriter()
            writer.add_blank_page(width=612, height=792)
            pdf_path = out_dir / f"{odt_path.stem}.pdf"
            with pdf_path.open("wb") as output:
                writer.write(output)
            return pdf_path

        monkeypatch.setattr(gen, "_render_one", fake_render)
        monkeypatch.setattr(gen, "_convert_to_pdf", fake_convert)
        return converts

    def test_parallel_run_flags_every_child(
        self,
        gen_cfg: Config,
        pdf_convert: list[tuple[str, Path]],
    ) -> None:
        """Concurrent workers still flag every child exactly once."""
        children = [
            child_row("c1", "1A", 1),
            child_row("c2", "1B", 1),
            child_row("c3", "1C", 1),
            child_row("c4", "1D", 1),
        ]
        _seed(gen_cfg, children, _family())
        gen.run(gen_cfg, max_workers=3)
        frame = read_sheet(gen_cfg.excel_file, "final_child")
        assert frame is not None
        assert frame["generated_info_sheet"].to_list() == [True] * 4
        assert sorted(stem for stem, _profile in pdf_convert) == [
            f"AT{gen_cfg.season}_1A",
            f"AT{gen_cfg.season}_1B",
            f"AT{gen_cfg.season}_1C",
            f"AT{gen_cfg.season}_1D",
        ]
        # One LibreOffice profile per worker thread, none shared.
        assert len({profile for _stem, profile in pdf_convert}) <= 3

    def test_keyboard_interrupt_records_finished_sheets(
        self,
        gen_cfg: Config,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Ctrl+C flags completed sheets and leaves the rest pending."""
        children = [
            child_row("c1", "1A", 1),
            child_row("c2", "1B", 1),
            child_row("c3", "1C", 1),
        ]
        converts: list[str] = []

        def fake_render(
            template_file: Path, data: dict[str, Any], odt_path: Path
        ) -> None:
            if odt_path.stem.endswith("1C"):
                # Give the main thread time to react to 1B's interrupt
                # and set the stop event before 1C converts.
                time.sleep(0.3)

        def fake_convert(
            odt_path: Path,
            out_dir: Path,
            soffice_binary: str,
            profile: Path,
            stop: threading.Event | None = None,
        ) -> Path:
            if odt_path.stem.endswith("1B"):
                # Simulates the interrupt arriving while 1B converts.
                raise KeyboardInterrupt
            converts.append(odt_path.stem)
            writer = PdfWriter()
            writer.add_blank_page(width=612, height=792)
            pdf_path = out_dir / f"{odt_path.stem}.pdf"
            with pdf_path.open("wb") as output:
                writer.write(output)
            return pdf_path

        monkeypatch.setattr(gen, "_render_one", fake_render)
        monkeypatch.setattr(gen, "_convert_to_pdf", fake_convert)
        _seed(gen_cfg, children, _family())
        before = read_final(gen_cfg, "child")
        with pytest.raises(KeyboardInterrupt):
            gen.run(gen_cfg, max_workers=1)
        # 1A finished and is flagged; 1B was interrupted mid-flight and
        # 1C never converted (cancelled through the stop event).
        assert converts == [f"AT{gen_cfg.season}_1A"]
        after = read_final(gen_cfg, "child")
        _assert_only_generated_flags_changed(
            before,
            after,
            {"c1": True, "c2": False, "c3": False},
        )

    def test_progress_bar_tracks_every_sheet(
        self,
        gen_cfg: Config,
        two_children: list[dict[str, Any]],
        pdf_convert: list[tuple[str, Path]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One tqdm bar covers exactly the pending rows and closes."""

        class FakeBar:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.updates = 0
                self.kwargs = kwargs
                self.closed = False
                bars.append(self)

            def update(self, n: int = 1) -> None:
                self.updates += n

            def set_postfix_str(self, text: str, refresh: bool = True) -> None:
                pass

            def close(self) -> None:
                self.closed = True

        bars: list[FakeBar] = []
        monkeypatch.setattr(gen, "tqdm", FakeBar)
        _seed(gen_cfg, two_children, _family())
        gen.run(gen_cfg)
        assert len(bars) == 1
        assert bars[0].kwargs["total"] == 2
        assert bars[0].updates == 2
        assert bars[0].closed

    def test_convert_stop_terminates_subprocess(self, tmp_path: Path) -> None:
        """A set stop event kills the LibreOffice subprocess promptly."""
        slow = tmp_path / "slow_soffice"
        slow.write_text("#!/bin/sh\nexec sleep 30\n")
        slow.chmod(0o755)
        odt_path = tmp_path / "x.odt"
        odt_path.write_bytes(b"odt")
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        profile = tmp_path / "profile"
        profile.mkdir()
        stop = threading.Event()
        threading.Timer(0.3, stop.set).start()
        started = time.monotonic()
        with pytest.raises(gen.InfoSheetCancelledError):
            gen._convert_to_pdf(
                odt_path, out_dir, str(slow), profile, stop=stop
            )
        # Cancelled within a poll interval (not the 300s timeout, not
        # the 30s sleep).
        assert time.monotonic() - started < 10


@pytest.mark.skipif(
    shutil.which("soffice") is None, reason="LibreOffice not installed"
)
def test_placeholder_shown_in_converted_pdf(
    gen_cfg: Config, tmp_path: Path
) -> None:
    """The underscore placeholder survives the ODT -> PDF conversion.

    Real template + real headless LibreOffice (§13.2).
    """
    spec = gen_cfg.info_sheet.model_copy(
        update={"template_file": str(REAL_TEMPLATE)}
    )
    real = gen_cfg.model_copy(update={"info_sheet": spec})
    _seed(
        real,
        [child_row("c1", "1A", 1)],
        _family(),
        sponsors=[
            _sponsor(sponsor_type="Organization", email=None, phone=None)
        ],
    )

    gen.run(real, test=True)

    pdfs = list(Path(real.info_sheet.output_dir).rglob("*.pdf"))
    assert len(pdfs) == 1
    text = "".join(
        page.extract_text() or "" for page in PdfReader(str(pdfs[0])).pages
    )
    assert gen.SPONSOR_CONTACT_PLACEHOLDER in text
