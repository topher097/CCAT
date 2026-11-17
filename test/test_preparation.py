"""Isolated tests for pure local preparation and stable reconciliation."""

from copy import deepcopy
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from ccat import preparation
from ccat.acquire import (
    _cross_check_created,
    assign_angel_ids,
    assign_family_ids,
    assign_sponsor_ids,
    sibling_dob_conflicts,
)
from ccat.config import Config
from ccat.preparation import (
    PreparationError,
    dob_notes_by_child,
    prepare_rows,
    prepare_workbook,
    prepared_frame,
)


def _sponsor(record_id: str, created: str, **overrides: Any) -> dict[str, Any]:
    """Build one minimal valid raw sponsor row.

    Args:
        record_id: Unique zite_record_id.
        created: ISO source creation timestamp.
        **overrides: Optional cell overrides.

    Returns:
        A raw sponsor dictionary accepted by the shared test Config.
    """
    row: dict[str, Any] = {
        "zite_record_id": record_id,
        "create": created,
        "email": f"{record_id}@example.com",
        "age_preference": "Any Age",
    }
    row.update(overrides)
    return row


def _family(
    record_id: str,
    created: str,
    email: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Build one minimal valid raw family row.

    Args:
        record_id: Unique zite_record_id.
        created: ISO source creation timestamp.
        email: Primary email, defaulting to a record-specific address.
        **overrides: Optional cell overrides.

    Returns:
        A raw family dictionary accepted by the shared test Config.
    """
    row: dict[str, Any] = {
        "zite_record_id": record_id,
        "zite_created_at": created,
        "created": created,
        "primary_email": email or f"{record_id}@example.com",
    }
    row.update(overrides)
    return row


def _child(
    record_id: str,
    created: str,
    dob: str = "2018-05-10",
    email: str = "fam@example.com",
    first_name: str = "Kiddo",
    **overrides: Any,
) -> dict[str, Any]:
    """Build one minimal valid raw child row.

    Args:
        record_id: Unique zite_record_id.
        created: ISO source creation timestamp.
        dob: ISO birth date.
        email: Family primary_email linkage.
        first_name: Child first name and duplicate-group component.
        **overrides: Optional cell overrides.

    Returns:
        A raw child dictionary accepted by the shared test Config.
    """
    row: dict[str, Any] = {
        "zite_record_id": record_id,
        "zite_created_at": created,
        "created": created,
        "first_name": first_name,
        "primary_email": email,
        "dob": dob,
    }
    row.update(overrides)
    return row


def _raw(
    sponsors: list[dict[str, Any]] | None = None,
    families: list[dict[str, Any]] | None = None,
    children: list[dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Assemble a complete raw table mapping.

    Args:
        sponsors: Raw sponsor rows, default empty.
        families: Raw family rows, default empty.
        children: Raw child rows, default empty.

    Returns:
        A mapping containing all three required table keys.
    """
    return {
        "sponsor": sponsors or [],
        "family": families or [],
        "child": children or [],
    }


def _by_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index prepared rows by zite_record_id for behavior assertions.

    Args:
        rows: Prepared rows.

    Returns:
        A source-record-keyed mapping.
    """
    return {str(row["zite_record_id"]): row for row in rows}


def _screening_preparation(
    raw_rows: dict[str, list[dict[str, Any]]], cfg: Config
) -> preparation.Preparation:
    """Reproduce screening's original local sequence as an equivalence oracle.

    Args:
        raw_rows: Verbatim source rows.
        cfg: Shared test configuration.

    Returns:
        A Preparation-shaped result produced only by old screening helpers.
    """
    rows: dict[str, list[dict[str, Any]]] = {}
    notes: dict[str, dict[str, list[str]]] = {}
    for table_name in ("sponsor", "family", "child"):
        rows[table_name], notes[table_name] = preparation.clean_rows(
            raw_rows[table_name], table_name, cfg
        )
    duplicate_log: list[dict[str, Any]] = []
    survivors: dict[str, dict[str, dict[str, Any]]] = {}
    for table_name in ("sponsor", "family", "child"):
        log_rows, survivor_of = preparation.detect_duplicates(
            rows[table_name], table_name
        )
        duplicate_log.extend(log_rows)
        survivors[table_name] = survivor_of
    rows["sponsor"] = assign_sponsor_ids(rows["sponsor"])
    rows["family"] = assign_family_ids(rows["family"])
    rows["child"] = assign_angel_ids(rows["child"], cfg, rows["family"])
    _cross_check_created(rows["family"], "family")
    _cross_check_created(rows["child"], "child")
    return preparation.Preparation(
        rows=rows,
        validation_notes=notes,
        duplicate_log=duplicate_log,
        survivors=survivors,
        dob_log=sibling_dob_conflicts(rows["child"], cfg),
    )


def test_prepare_rows_matches_screening_extraction(cfg: Config) -> None:
    """No-existing preparation preserves rows, notes, payloads, and survivors."""
    raw_rows = _raw(
        sponsors=[
            _sponsor(
                "s2", "2026-02-01T10:00:00+00:00", email="DUP@EXAMPLE.COM"
            ),
            _sponsor(
                "s1", "2026-01-01T10:00:00+00:00", email="dup@example.com"
            ),
        ],
        families=[
            _family("f2", "2026-02-01T10:00:00+00:00", "dup@example.com"),
            _family("f1", "2026-01-01T10:00:00+00:00", "DUP@example.com"),
        ],
        children=[
            _child("c-old", "2026-01-01T10:00:00+00:00"),
            _child("c-new", "2026-02-01T10:00:00+00:00"),
            _child(
                "c-sibling",
                "2026-01-15T10:00:00+00:00",
                dob="2018-05-15",
                first_name="Sibling",
            ),
        ],
    )
    raw_snapshot = deepcopy(raw_rows)
    expected = _screening_preparation(raw_rows, cfg)
    actual = prepare_rows(raw_rows, cfg)

    assert actual == expected
    assert raw_rows == raw_snapshot
    assert {row["sheet"] for row in actual.duplicate_log} == {
        "sponsor_submissions",
        "family_submissions",
        "child_submissions",
    }
    for table_name in ("sponsor", "family", "child"):
        expected_frame = prepared_frame(
            expected.rows[table_name], table_name, cfg
        )
        assert prepared_frame(actual.rows[table_name], table_name, cfg).equals(
            expected_frame
        )
    assert preparation.duplicate_log_frame(actual.duplicate_log).height == len(
        actual.duplicate_log
    )


@pytest.mark.parametrize("bad_id", [None, "", "   "])
def test_prepare_rows_rejects_missing_or_blank_raw_ids(
    cfg: Config, bad_id: str | None
) -> None:
    """Every blank raw ID fails structurally instead of dropping its row."""
    raw_rows = _raw(sponsors=[_sponsor("placeholder", "2026-01-01")])
    raw_rows["sponsor"][0]["zite_record_id"] = bad_id
    with pytest.raises(PreparationError, match="missing or blank"):
        prepare_rows(raw_rows, cfg)


def test_prepare_rows_rejects_duplicate_raw_ids_before_cleaning(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad child ID prevents cleaning of every table, not just that child."""
    raw_rows = _raw(
        children=[
            _child("duplicate", "2026-01-01"),
            _child(" duplicate ", "2026-01-02"),
        ]
    )
    called = False

    def fail_if_cleaned(*args: Any, **kwargs: Any) -> None:
        """Record an unexpected cleaning attempt.

        Args:
            *args: Ignored positional arguments.
            **kwargs: Ignored keyword arguments.
        """
        nonlocal called
        called = True

    monkeypatch.setattr(preparation, "clean_rows", fail_if_cleaned)
    with pytest.raises(PreparationError, match="duplicate zite_record_id"):
        prepare_rows(raw_rows, cfg)
    assert not called


def test_clean_rows_also_rejects_bad_raw_ids(cfg: Config) -> None:
    """The public cleaner cannot silently discard an ID-less standalone row."""
    with pytest.raises(PreparationError, match="missing or blank"):
        preparation.clean_rows(
            [_sponsor("s1", "2026-01-01", zite_record_id="")],
            "sponsor",
            cfg,
        )


def test_stable_sponsor_and_family_ids_survive_reordering_and_disappearance(
    cfg: Config,
) -> None:
    """Earlier/new rows append above max; raw-missing IDs are never reused."""
    raw_rows = _raw(
        sponsors=[
            _sponsor("s-existing", "2026-02-01T10:00:00+00:00"),
            _sponsor("s-new-late", "2026-03-01T10:00:00+00:00"),
            _sponsor("s-new-early", "2026-01-01T10:00:00+00:00"),
        ],
        families=[
            _family(
                "f-existing", "2026-02-01T10:00:00+00:00", "one@example.com"
            ),
            _family(
                "f-dup-old", "2026-02-15T10:00:00+00:00", "dup@example.com"
            ),
            _family(
                "f-dup-new", "2026-01-15T10:00:00+00:00", "dup@example.com"
            ),
            _family(
                "f-new-late", "2026-03-01T10:00:00+00:00", "late@example.com"
            ),
            _family(
                "f-new-early", "2026-01-01T10:00:00+00:00", "early@example.com"
            ),
        ],
    )
    existing = {
        "sponsor": [
            {"zite_record_id": "s-existing", "sponsor_id": 8},
            {"zite_record_id": "s-raw-missing", "sponsor_id": 10},
        ],
        "family": [
            {
                "zite_record_id": "f-existing",
                "family_id": 7,
                "primary_email": "one@example.com",
            },
            {
                "zite_record_id": "f-dup-old",
                "family_id": 8,
                "primary_email": "dup@example.com",
            },
            {
                "zite_record_id": "f-raw-missing",
                "family_id": 9,
                "primary_email": "gone@example.com",
            },
        ],
        "child": [],
    }
    existing_snapshot = deepcopy(existing)
    result = prepare_rows(raw_rows, cfg, existing)
    sponsors = _by_id(result.rows["sponsor"])
    families = _by_id(result.rows["family"])

    assert sponsors["s-existing"]["sponsor_id"] == 8
    assert sponsors["s-new-early"]["sponsor_id"] == 11
    assert sponsors["s-new-late"]["sponsor_id"] == 12
    assert families["f-existing"]["family_id"] == 7
    assert families["f-dup-old"]["family_id"] == 8
    assert families["f-dup-new"]["family_id"] == 8
    assert families["f-new-early"]["family_id"] == 10
    assert families["f-new-late"]["family_id"] == 11
    assert existing == existing_snapshot


def test_family_email_fallback_cannot_steal_another_current_group(
    cfg: Config,
) -> None:
    """Normalized fallback excludes a final source row in another raw group."""
    raw_rows = _raw(
        families=[
            _family("new-source", "2026-01-01", "fam@example.com"),
            _family("current-other", "2026-02-01", "changed@example.com"),
        ]
    )
    existing = {
        "family": [
            {
                "zite_record_id": "old-source",
                "family_id": 4,
                "primary_email": " FAM@example.com ",
            },
            {
                "zite_record_id": "current-other",
                "family_id": 3,
                "primary_email": "fam@example.com",
            },
        ]
    }
    families = _by_id(prepare_rows(raw_rows, cfg, existing).rows["family"])
    assert families["new-source"]["family_id"] == 4
    assert families["current-other"]["family_id"] == 3


def test_family_direct_match_prefers_current_survivor(cfg: Config) -> None:
    """Current survivor's existing entity wins over a lower legacy ID."""
    raw_rows = _raw(
        families=[
            _family("survivor", "2026-01-01", "same@example.com"),
            _family("extra", "2026-02-01", "same@example.com"),
        ]
    )
    existing = {
        "family": [
            {"zite_record_id": "survivor", "family_id": 20},
            {"zite_record_id": "extra", "family_id": 10},
        ]
    }
    families = prepare_rows(raw_rows, cfg, existing).rows["family"]
    assert {row["family_id"] for row in families} == {20}


def test_new_duplicate_never_steals_current_or_raw_missing_angel(
    cfg: Config,
) -> None:
    """Angel ownership survives survivor flips, disappearance, and new siblings."""
    family = _family("f1", "2026-01-01", "fam@example.com")
    old = _child("c-old", "2026-01-01")
    new = _child("c-new", "2026-03-01")
    sibling = _child(
        "c-sibling",
        "2026-02-01",
        dob="2015-01-01",
        first_name="Sibling",
    )
    existing = {
        "family": [{"zite_record_id": "f1", "family_id": 1}],
        "child": [
            {
                "zite_record_id": "c-old",
                "family_id": 1,
                "primary_email": "fam@example.com",
                "first_name": "Kiddo",
                "angel_id": "1A",
            },
            {
                "zite_record_id": "c-reserve",
                "family_id": 1,
                "primary_email": "fam@example.com",
                "first_name": "Reserved",
                "angel_id": "1B",
            },
        ],
    }

    current = _by_id(
        prepare_rows(
            _raw(families=[family], children=[old, new, sibling]),
            cfg,
            existing,
        ).rows["child"]
    )
    assert current["c-old"]["angel_id"] == "1A"
    assert current["c-new"]["angel_id"] is None
    assert current["c-sibling"]["angel_id"] == "1C"

    # Once the old owner itself disappears from raw, its group ownership and
    # both raw-missing angel reservations still prevent transfer/reuse.
    disappeared = _by_id(
        prepare_rows(
            _raw(families=[family], children=[new, sibling]), cfg, existing
        ).rows["child"]
    )
    assert disappeared["c-new"]["angel_id"] is None
    assert disappeared["c-sibling"]["angel_id"] == "1C"


def test_child_existing_family_stays_and_null_family_can_link(
    cfg: Config,
) -> None:
    """Stable child linkage overrides regrouping; null linkage may be filled."""
    raw_rows = _raw(
        families=[_family("f-new", "2026-01-01", "new@example.com")],
        children=[
            _child(
                "c-move",
                "2026-01-01",
                dob="2010-01-01",
                email="new@example.com",
                first_name="Move",
            ),
            _child(
                "c-link",
                "2026-01-02",
                dob="2012-01-01",
                email="new@example.com",
                first_name="Link",
            ),
        ],
    )
    existing = {
        "family": [{"zite_record_id": "f-new", "family_id": 2}],
        "child": [
            {"zite_record_id": "c-move", "family_id": 1, "angel_id": "1Q"},
            {"zite_record_id": "c-link", "family_id": None, "angel_id": None},
        ],
    }
    children = _by_id(prepare_rows(raw_rows, cfg, existing).rows["child"])
    assert children["c-move"]["family_id"] == 1
    assert children["c-move"]["angel_id"] == "1Q"
    assert children["c-link"]["family_id"] == 2
    assert children["c-link"]["angel_id"] == "2A"


def test_dob_log_uses_effective_stable_ids(cfg: Config) -> None:
    """DOB diagnostics run after stable family and angel reconciliation."""
    raw_rows = _raw(
        families=[_family("f1", "2026-01-01", "fam@example.com")],
        children=[
            _child(
                "c-a",
                "2026-01-01",
                dob="2020-01-01",
                first_name="Alpha",
            ),
            _child(
                "c-b",
                "2026-01-02",
                dob="2020-01-11",
                first_name="Beta",
            ),
        ],
    )
    existing = {
        "family": [{"zite_record_id": "f1", "family_id": 7}],
        "child": [
            {"zite_record_id": "c-a", "family_id": 7, "angel_id": "7X"},
            {"zite_record_id": "c-b", "family_id": 7, "angel_id": "7Z"},
        ],
    }
    result = prepare_rows(raw_rows, cfg, existing)
    assert len(result.dob_log) == 1
    conflict = result.dob_log[0]
    assert conflict["family_id"] == 7
    assert conflict["child_a_angel_id"] == "7X"
    assert conflict["child_b_angel_id"] == "7Z"
    notes = dob_notes_by_child(result.dob_log)
    assert notes["c-a"] == (
        "Sibling DOB conflict: born 0 month(s) (10 day(s)) apart from"
        " sibling Beta (7Z, dob 2020-01-11)"
    )
    assert notes["c-b"] == (
        "Sibling DOB conflict: born 0 month(s) (10 day(s)) apart from"
        " sibling Alpha (7X, dob 2020-01-01)"
    )


def _empty_raw_frames(cfg: Config) -> dict[str, pl.DataFrame]:
    """Build empty raw frames containing every configured column.

    Args:
        cfg: Shared test configuration.

    Returns:
        Sheet-name-to-frame mapping for mocked workbook reads.
    """
    return {
        f"raw_{table_name}": pl.DataFrame(
            schema={key: pl.String for key in cfg.tables[table_name].columns}
        )
        for table_name in ("sponsor", "family", "child")
    }


def _mock_sheets(
    monkeypatch: pytest.MonkeyPatch, sheets: dict[str, pl.DataFrame]
) -> None:
    """Replace preparation's low-level workbook reader with in-memory frames.

    Args:
        monkeypatch: Pytest patch fixture.
        sheets: Present sheets; all other names return None.
    """

    def fake_read_sheet(path: str | Path, sheet: str) -> pl.DataFrame | None:
        """Return one mocked sheet.

        Args:
            path: Ignored workbook path.
            sheet: Requested sheet name.

        Returns:
            The mocked frame or None when absent.
        """
        return sheets.get(sheet)

    monkeypatch.setattr(preparation, "read_sheet", fake_read_sheet)


@pytest.mark.parametrize("entity", ["sponsor", "family", "child"])
def test_prepare_workbook_rejects_legacy_adj_without_final(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, entity: str
) -> None:
    """Any legacy adj/final mismatch requires explicit consolidation."""
    sheets = _empty_raw_frames(cfg)
    sheets[f"adj_{entity}"] = pl.DataFrame({"legacy": [1]})
    _mock_sheets(monkeypatch, sheets)
    with pytest.raises(PreparationError, match="consolidate-migrate"):
        prepare_workbook(cfg)


def test_prepare_workbook_validates_raw_sheet_columns_and_ids(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workbook structural checks cover absent sheets, columns, and raw IDs."""
    sheets = _empty_raw_frames(cfg)
    del sheets["raw_sponsor"]
    _mock_sheets(monkeypatch, sheets)
    with pytest.raises(PreparationError, match="raw_sponsor sheet is missing"):
        prepare_workbook(cfg)

    sheets = _empty_raw_frames(cfg)
    sheets["raw_sponsor"] = sheets["raw_sponsor"].drop("email")
    _mock_sheets(monkeypatch, sheets)
    with pytest.raises(PreparationError, match="lacks column"):
        prepare_workbook(cfg)

    sheets = _empty_raw_frames(cfg)
    columns = list(cfg.tables["sponsor"].columns)
    rows = []
    for _ in range(2):
        row = _sponsor("duplicate", "2026-01-01")
        rows.append({key: row.get(key) for key in columns})
    sheets["raw_sponsor"] = pl.DataFrame(rows)
    _mock_sheets(monkeypatch, sheets)
    with pytest.raises(PreparationError, match="duplicate zite_record_id"):
        prepare_workbook(cfg)


def test_prepare_workbook_uses_untyped_final_and_supplied_existing(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final controls need no schema validation; explicit existing skips adj."""
    sheets = _empty_raw_frames(cfg)
    sponsor = _sponsor("s1", "2026-01-01")
    sheets["raw_sponsor"] = pl.DataFrame(
        [{key: sponsor.get(key) for key in cfg.tables["sponsor"].columns}]
    )
    sheets["final_sponsor"] = pl.DataFrame(
        {
            "zite_record_id": ["s1"],
            "sponsor_id": [42],
            "audited": [True],
            "unknown_future_control": ["kept opaque"],
        }
    )
    sheets["adj_family"] = pl.DataFrame({"legacy": [1]})
    _mock_sheets(monkeypatch, sheets)
    with pytest.raises(PreparationError, match="consolidate-migrate"):
        prepare_workbook(cfg)

    supplied = {
        "sponsor": [{"zite_record_id": "s1", "sponsor_id": 42}],
        "family": [],
        "child": [],
    }
    result = prepare_workbook(cfg, supplied)
    assert result.rows["sponsor"][0]["sponsor_id"] == 42
