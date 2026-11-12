"""Tests for module 5 — child info sheet generation (§13)."""

from pathlib import Path
from typing import Any

import polars as pl
import pytest

import ccat.child_info_generate as gen
from ccat.child_info_generate import InfoSheetError
from ccat.config import Config, InfoSheetSpec
from ccat.io import read_sheet, write_sheets

CHILD_COLUMNS = [
    "zite_record_id",
    "family_id",
    "angel_id",
    "age_at_xmas",
    "gender",
    "favorite_colors",
    "interests",
    "other_notes",
    "need_1",
    "need_2",
    "desire_1",
    "desire_2",
    "shoe_size",
    "shoe_size_type",
    "pant_waist_size",
    "pant_inseam",
    "pant_size_letter",
    "pant_size_type",
    "jean_size",
    "shirt_size_letter",
    "shirt_size_type",
]


def child_row(record_id: str, angel_id: str, family_id: int) -> dict[str, Any]:
    """One final_child row for generate tests."""
    return {
        "zite_record_id": record_id,
        "family_id": family_id,
        "angel_id": angel_id,
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
    with_flag: bool = False,
    flags: list[bool] | None = None,
    sponsors: list[dict[str, Any]] | None = None,
) -> None:
    """Write final_child, final_family, and final_sponsor sheets."""
    schema: dict[str, Any] = {
        "zite_record_id": pl.String,
        "family_id": pl.Int64,
        "angel_id": pl.String,
        "age_at_xmas": pl.Int64,
        "gender": pl.String,
        "favorite_colors": pl.String,
        "interests": pl.String,
        "other_notes": pl.String,
        "need_1": pl.String,
        "need_2": pl.String,
        "desire_1": pl.String,
        "desire_2": pl.String,
        "shoe_size": pl.String,
        "shoe_size_type": pl.String,
        "pant_waist_size": pl.String,
        "pant_inseam": pl.String,
        "pant_size_letter": pl.String,
        "pant_size_type": pl.String,
        "jean_size": pl.String,
        "shirt_size_letter": pl.String,
        "shirt_size_type": pl.String,
    }
    if with_flag:
        schema["generated_info_sheet"] = pl.Boolean
    if flags is not None:
        for row, flag in zip(children, flags, strict=True):
            row["generated_info_sheet"] = flag
    write_sheets(
        cfg.excel_file,
        "audit",
        {"final_child": pl.DataFrame(children, schema=schema)},
    )
    write_sheets(
        cfg.excel_file,
        "match",
        {
            "final_family": pl.DataFrame(
                families,
                schema={
                    "family_id": pl.Int64,
                    "gift_pickup_location": pl.String,
                    "assigned_sponsor_id": pl.Int64,
                },
            ),
            "final_sponsor": pl.DataFrame(
                _sponsor() if sponsors is None else sponsors,
                schema=SPONSOR_SCHEMA,
            ),
        },
    )


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


def _sponsor(sponsor_id: int = 7, **overrides: Any) -> dict[str, Any]:
    """One final_sponsor row for generate tests."""
    row: dict[str, Any] = {
        "sponsor_id": sponsor_id,
        "first_name": "Sam",
        "last_name": "Sponsor",
        "organization_name": None,
        "email": "sam@example.com",
        "phone": "555-0100",
        "assigned_angel_ids": "1A,1B",
    }
    row.update(overrides)
    return row


SPONSOR_SCHEMA: dict[str, Any] = {
    "sponsor_id": pl.Int64,
    "first_name": pl.String,
    "last_name": pl.String,
    "organization_name": pl.String,
    "email": pl.String,
    "phone": pl.String,
    "assigned_angel_ids": pl.String,
}


@pytest.fixture
def gen_cfg(cfg: Config, tmp_path: Path) -> Config:
    """Config pointing at a stub template and a temp output directory."""
    template = tmp_path / "template.ott"
    template.touch()
    spec = cfg.info_sheet.model_copy(
        update={
            "template_file": str(template),
            "output_dir": str(tmp_path / "sheets"),
        }
    )
    return cfg.model_copy(update={"info_sheet": spec})


@pytest.fixture
def two_children() -> list[dict[str, Any]]:
    """Two pending children in one family."""
    return [
        child_row("c1", "1A", 1),
        child_row("c2", "1B", 1),
    ]


def _family() -> list[dict[str, Any]]:
    """One final_family row joining the seeded children."""
    return [
        {
            "family_id": 1,
            "gift_pickup_location": "Hancock",
            "assigned_sponsor_id": 7,
        }
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


class TestNormalizeFlag:
    """generated_info_sheet normalization (§13.3)."""

    def test_missing_column_added_all_false(self) -> None:
        """First run adds the column with every row False."""
        frame = pl.DataFrame(
            [{"zite_record_id": "c1"}], schema={"zite_record_id": pl.String}
        )
        normalized, added = gen._normalize_flag(frame)
        assert added is True
        assert normalized.schema["generated_info_sheet"] == pl.Boolean
        assert normalized["generated_info_sheet"].to_list() == [False]

    def test_nulls_filled_false(self) -> None:
        """Null flag values normalize to False."""
        frame = pl.DataFrame(
            [{"generated_info_sheet": None}, {"generated_info_sheet": True}],
            schema={"generated_info_sheet": pl.Boolean},
        )
        normalized, added = gen._normalize_flag(frame)
        assert added is False
        assert normalized["generated_info_sheet"].to_list() == [False, True]

    def test_string_booleans_coerced(self) -> None:
        """String booleans coming back from the sheet are coerced."""
        frame = pl.DataFrame(
            [
                {"generated_info_sheet": "true"},
                {"generated_info_sheet": "false"},
                {"generated_info_sheet": None},
                {"generated_info_sheet": "1"},
            ],
            schema={"generated_info_sheet": pl.String},
        )
        normalized, _added = gen._normalize_flag(frame)
        assert normalized["generated_info_sheet"].to_list() == [
            True,
            False,
            False,
            False,
        ]


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
        _base, enriched, _added = gen._load_enriched_rows(gen_cfg)
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
        assert enriched["sponsor_family_size"].to_list() == [2, 2]

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
            {"final_child": pl.DataFrame(two_children)},
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
        self, gen_cfg: Config, two_children: list[dict[str, Any]]
    ) -> None:
        """Duplicate final_family family_id rows raise a typed error."""
        _seed(gen_cfg, two_children, _family() + _family())
        with pytest.raises(InfoSheetError, match="not unique"):
            gen._load_enriched_rows(gen_cfg)

    def test_null_family_id(
        self, gen_cfg: Config, two_children: list[dict[str, Any]]
    ) -> None:
        """A child without family_id raises a typed error."""
        two_children[0]["family_id"] = None
        _seed(gen_cfg, two_children, _family())
        with pytest.raises(InfoSheetError, match="without family_id"):
            gen._load_enriched_rows(gen_cfg)

    def test_final_family_missing_join_column(
        self, gen_cfg: Config, two_children: list[dict[str, Any]]
    ) -> None:
        """final_family without the join columns raises a typed error."""
        _seed(gen_cfg, two_children, _family())
        write_sheets(
            gen_cfg.excel_file,
            "match",
            {
                "final_family": pl.DataFrame(
                    [{"family_id": 1}],
                    schema={"family_id": pl.Int64},
                )
            },
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
        self, gen_cfg: Config, two_children: list[dict[str, Any]]
    ) -> None:
        """final_sponsor without the join columns raises a typed error."""
        _seed(gen_cfg, two_children, _family())
        write_sheets(
            gen_cfg.excel_file,
            "match",
            {
                "final_sponsor": pl.DataFrame(
                    [{"sponsor_id": 7}],
                    schema={"sponsor_id": pl.Int64},
                )
            },
        )
        with pytest.raises(InfoSheetError, match="lacks column"):
            gen._load_enriched_rows(gen_cfg)

    def test_duplicate_sponsor_id(
        self, gen_cfg: Config, two_children: list[dict[str, Any]]
    ) -> None:
        """Duplicate final_sponsor sponsor_id rows raise a typed error."""
        _seed(
            gen_cfg, two_children, _family(), sponsors=[_sponsor(), _sponsor()]
        )
        with pytest.raises(InfoSheetError, match="not unique"):
            gen._load_enriched_rows(gen_cfg)

    def test_unassigned_child_sponsor_columns_null(
        self, gen_cfg: Config, two_children: list[dict[str, Any]]
    ) -> None:
        """A family without an assigned sponsor leaves sponsor sources null."""
        _seed(gen_cfg, two_children, _unassigned_family())
        _base, enriched, _added = gen._load_enriched_rows(gen_cfg)
        assert enriched["sponsor_email"].to_list() == [None, None]
        assert enriched["sponsor_name"].to_list() == ["", ""]
        assert enriched["sponsor_family_size"].to_list() == [None, None]


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
        ) -> Path:
            pdf_path = out_dir / f"{odt_path.stem}.pdf"
            pdf_path.write_bytes(b"%PDF-fake")
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
        gen.run(gen_cfg)
        stem = f"AT{gen_cfg.season}_"
        names = sorted(name for name, _data in render_calls)
        assert names == [f"{stem}1A.odt", f"{stem}1B.odt"]
        pdfs = sorted(
            p.name for p in Path(gen_cfg.info_sheet.output_dir).glob("*.pdf")
        )
        assert pdfs == [f"{stem}1A.pdf", f"{stem}1B.pdf"]
        frame = read_sheet(gen_cfg.excel_file, "final_child")
        assert frame is not None
        assert frame["generated_info_sheet"].to_list() == [True, True]
        # enrichment columns never reach the sheet (§13.4)
        assert "gift_pickup_location" not in frame.columns
        assert "family_size" not in frame.columns

    def test_render_data_contents(
        self,
        gen_cfg: Config,
        two_children: list[dict[str, Any]],
        render_calls: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """The rendered data dict carries the enriched row values (§13.2)."""
        _seed(gen_cfg, two_children, _family())
        gen.run(gen_cfg)
        _name, data = render_calls[0]
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
        _seed(gen_cfg, two_children, _unassigned_family())
        gen.run(gen_cfg)
        assert render_calls == []
        frame = read_sheet(gen_cfg.excel_file, "final_child")
        assert frame is not None
        assert frame["generated_info_sheet"].to_list() == [False, False]

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

    def test_skips_true_rows(
        self,
        gen_cfg: Config,
        two_children: list[dict[str, Any]],
        render_calls: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """Rows already flagged True are not re-rendered (§13.4)."""
        _seed(
            gen_cfg,
            two_children,
            _family(),
            with_flag=True,
            flags=[True, False],
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
            with_flag=True,
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
            with_flag=True,
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
            p.name for p in Path(gen_cfg.info_sheet.output_dir).glob("*.pdf")
        )
        assert pdfs == [f"AT{gen_cfg.season}_2A.pdf"]

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
        ) -> Path:
            pdf_path = out_dir / f"{odt_path.stem}.pdf"
            pdf_path.write_bytes(b"%PDF-fake")
            return pdf_path

        monkeypatch.setattr(gen, "_render_one", failing_render)
        monkeypatch.setattr(gen, "_convert_to_pdf", fake_convert)
        _seed(gen_cfg, two_children, _family())
        with pytest.raises(InfoSheetError, match="1A"):
            gen.run(gen_cfg)
        frame = read_sheet(gen_cfg.excel_file, "final_child")
        assert frame is not None
        assert frame["generated_info_sheet"].to_list() == [False, True]

    def test_no_pending_no_write(
        self,
        gen_cfg: Config,
        two_children: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """All done -> nothing rendered and the workbook is untouched."""
        calls: list[Path] = []
        original = gen.write_sheets

        def counting_write(path: Any, module: str, sheets: Any) -> None:
            calls.append(Path(path))
            original(path, module, sheets)

        monkeypatch.setattr(gen, "write_sheets", counting_write)
        _seed(
            gen_cfg,
            two_children,
            _family(),
            with_flag=True,
            flags=[True, True],
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
