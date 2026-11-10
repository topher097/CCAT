"""Tests for module 1 — data acquisition and validation (§5)."""

from typing import Any

from pathlib import Path

import polars as pl
import pytest

from ccat import acquire
from ccat.acquire import (
    AcquisitionError,
    assign_angel_ids,
    assign_family_ids,
    assign_sponsor_ids,
    clean_value,
    compute_age_at_xmas,
    dedup_families,
    import_table,
    index_to_letters,
    process_table,
    run,
    snake_case,
)
from ccat.config import Config, ZiteSpec, load_config
from ccat.io import SheetOwnershipError, read_sheet, write_sheets

SPONSOR_BASE: dict[str, Any] = {
    "id": "rec_s1",
    "createdAt": "2026-01-01T10:00:00Z",
    "updatedAt": "2026-01-03T10:00:00Z",
    "Email": "Sponsor@Example.COM",
    "I Accept Data Disclaimer": True,
    "Affiliation": "Individual",
    "Organization Name": None,
    "Assigned Angel ID Code": None,
    "First Name": "mary jane",
    "Last Name": "o'brien",
    "Phone Number": "1 (906) 555-1234",
    "Number of Angles to Sponsor": "2",
    "Age Preference": None,
    "Gender Preference": "Any Gender",
    "Gift Drop-Off Location Preference": "Hancock",
    "Source": "web",
    "Updated time": "2026-01-02T10:00:00Z",
}

FAMILY_BASE: dict[str, Any] = {
    "id": "rec_f1",
    "createdAt": "2026-01-05T10:00:00Z",
    "Email": "fam@example.com",
    "I Accept Data Disclaimer": True,
    "First Name": "jane",
    "Last Name": "doe",
    "Phone Number": "(906) 555-9876",
    "Street Address": "1 Main St",
    "Apt / Floor": None,
    "City": "Hancock",
    "Zip Code": "49930",
    "State": "Michigan",
    "First Name (secondary)": None,
    "Last Name (secondary)": None,
    "Email (secondary)": None,
    "Phone Number (secondary)": None,
    "Gift Pickup Location Preference": "TSA (Hancock)",
    "Source": "web",
    "Created time": "2026-01-05T10:00:00Z",
}

CHILD_BASE: dict[str, Any] = {
    "id": "rec_c1",
    "createdAt": "2026-02-01T09:00:00Z",
    "First Name": "kiddo",
    "Date of Birth": "2018-05-10",
    "Gender": "Male",
    "Need #1": "winter coat",
    "Need #2": None,
    "Desire #1": "legos",
    "Desire #2": None,
    "Shoe Size": "13",
    "Shoe Size Type": "Youth",
    "Pant Waist Size": '24"',
    "Pant Inseam": '20"',
    "Pant Size (Letter)": "XS",
    "Pant Size Type": "Youth",
    "Jean Size": "12 slim",
    "Shirt Size (Letter)": "XS",
    "Shirt Size Type": "Youth",
    "Favorite Color(s)": "blue",
    "Interests": "trains",
    "Other Notes": None,
    "Source": "web",
    "Primary Email": "fam@example.com",
    "Creation Time": "2026-02-01T09:00:00Z",
    "Age at Christmas 2026": "8",
}


def zite_frame(
    base: dict[str, Any], rows: list[dict[str, Any]]
) -> pl.DataFrame:
    """Build a Zite-like frame from base defaults and row overrides."""
    return pl.DataFrame([{**base, **row} for row in rows])


def test_snake_case() -> None:
    """Column names normalize to lower snake_case."""
    assert (
        snake_case("Gift Drop-Off Location Preference")
        == "gift_drop_off_location_preference"
    )
    assert snake_case("Apt / Floor") == "apt_floor"


def test_clean_names_preserve_apostrophes_and_hyphens() -> None:
    """Name cleaning title-cases while preserving ' and - (§5.3)."""
    from ccat.config import ColumnSpec

    spec = ColumnSpec(type="name")
    assert clean_value("o'brien", spec) == ("O'Brien", None)
    assert clean_value("smith-jones", spec) == ("Smith-Jones", None)
    assert clean_value("mary jane", spec) == ("Mary Jane", None)


def test_clean_emails_lowercase_and_validate() -> None:
    """Emails are lowercased; invalid ones are failures."""
    from ccat.config import ColumnSpec

    spec = ColumnSpec(type="email")
    assert clean_value("  Foo@Bar.COM ", spec) == ("foo@bar.com", None)
    value, reason = clean_value("not-an-email", spec)
    assert reason == "invalid email"
    assert value == "not-an-email"


def test_clean_phone_us() -> None:
    """US phones normalize to (XXX) XXX-XXXX; others fail."""
    from ccat.config import ColumnSpec

    spec = ColumnSpec(type="phone_us")
    assert clean_value("906-555-1234", spec) == ("(906) 555-1234", None)
    assert clean_value("1 906 555 1234", spec) == ("(906) 555-1234", None)
    assert clean_value("12345", spec)[1] == "not a 10-digit US phone number"


def test_clean_dates_and_datetimes() -> None:
    """Dates normalize to ISO; datetimes parse ISO 8601 with timezone."""
    from ccat.config import ColumnSpec

    date_spec = ColumnSpec(type="date")
    assert clean_value("12/25/2020", date_spec) == ("2020-12-25", None)
    assert clean_value("2020-12-25", date_spec) == ("2020-12-25", None)
    assert (
        clean_value("25/12/2020", date_spec)[1] == "not MM/DD/YYYY or ISO date"
    )
    datetime_spec = ColumnSpec(type="datetime")
    value, reason = clean_value("2026-01-02T03:04:05Z", datetime_spec)
    assert reason is None
    assert value == "2026-01-02T03:04:05+00:00"


def test_clean_int_and_bool() -> None:
    """Integers cast from strings; booleans tolerate string forms."""
    from ccat.config import ColumnSpec

    assert clean_value("3", ColumnSpec(type="int")) == (3, None)
    assert clean_value("yes", ColumnSpec(type="bool")) == (True, None)


def test_import_table_renames_and_allow_list(cfg: Config) -> None:
    """Renames map source columns to keys; extras are dropped (§5.1)."""
    frame = zite_frame(SPONSOR_BASE, [{}])
    frame = frame.with_columns(pl.lit("junk").alias("Some Extra Column"))
    rows = import_table(frame, "sponsor", cfg)
    assert len(rows) == 1
    row = rows[0]
    assert row["email"] == "Sponsor@Example.COM"  # cleaning happens later
    assert row["num_children"] == "2"
    assert row["zite_record_id"] == "rec_s1"
    assert "some_extra_column" not in row
    assert set(row) == set(cfg.tables["sponsor"].columns)


def test_import_table_sponsor_created_from_system_column(cfg: Config) -> None:
    """Sponsor created falls back to Zite system createdAt (§5.1)."""
    rows = import_table(zite_frame(SPONSOR_BASE, [{}]), "sponsor", cfg)
    # config keys the column `create` with rename "Created time"; the
    # fixture has no user column, so the system createdAt is used
    assert rows[0]["create"] == "2026-01-01T10:00:00Z"


def test_import_table_fuzzy_rename_matches_typo(cfg: Config) -> None:
    """A corrected-typo rename still maps to the live Zite label (§5.1)."""
    rows = import_table(zite_frame(SPONSOR_BASE, [{}]), "sponsor", cfg)
    # config rename says "Number of Angels to Sponsor"; the fixture column
    # is the Zite form typo "Number of Angles to Sponsor"
    assert rows[0]["num_children"] == "2"


def test_import_table_missing_column_is_structural_failure(cfg: Config) -> None:
    """A missing required source column aborts the run (§5.3)."""
    frame = zite_frame(SPONSOR_BASE, [{}]).drop("Phone Number")
    with pytest.raises(AcquisitionError, match="Phone Number"):
        import_table(frame, "sponsor", cfg)


def test_process_table_keeps_valid_rows(cfg: Config) -> None:
    """A valid sponsor row survives cleaning and validation."""
    kept, rejected = process_table(
        zite_frame(SPONSOR_BASE, [{}]), "sponsor", cfg
    )
    assert rejected == []
    assert len(kept) == 1
    row = kept[0]
    assert row["email"] == "sponsor@example.com"
    assert row["first_name"] == "Mary Jane"
    assert row["last_name"] == "O'Brien"
    assert row["phone"] == "(906) 555-1234"
    assert row["num_children"] == 2
    assert row["age_preference"] == "Any Age"  # default substituted for null


def test_process_table_rejects_invalid_email_row(cfg: Config) -> None:
    """A row with an invalid email is excluded and reported (§5.3)."""
    kept, rejected = process_table(
        zite_frame(SPONSOR_BASE, [{"Email": "broken"}, {}]), "sponsor", cfg
    )
    assert len(kept) == 1
    assert kept[0]["email"] == "sponsor@example.com"
    assert len(rejected) == 1
    assert rejected[0]["column"] == "email"
    assert rejected[0]["reason"] == "invalid email"


def test_process_table_rejects_invalid_enum_value(cfg: Config) -> None:
    """Enum values outside the vocabulary reject the row (§5.3)."""
    kept, rejected = process_table(
        zite_frame(SPONSOR_BASE, [{"Affiliation": "Sole Proprietor"}]),
        "sponsor",
        cfg,
    )
    assert kept == []
    assert rejected[0]["column"] == "sponsor_type"


def test_process_table_keeps_null_without_default(cfg: Config) -> None:
    """Nulls in columns without a default are kept as null (§5.2)."""
    kept, rejected = process_table(
        zite_frame(SPONSOR_BASE, [{"First Name": None}]), "sponsor", cfg
    )
    assert rejected == []
    assert len(kept) == 1
    assert kept[0]["first_name"] is None


def test_process_table_fills_default_on_null(cfg: Config) -> None:
    """Nulls take the configured default when one is specified (§5.2)."""
    kept, rejected = process_table(
        zite_frame(
            SPONSOR_BASE, [{"First Name": None, "Age Preference": None}]
        ),
        "sponsor",
        cfg,
    )
    assert rejected == []
    assert kept[0]["first_name"] is None
    assert kept[0]["age_preference"] == "Any Age"  # config default


def test_process_table_blank_string_takes_default(cfg: Config) -> None:
    """A blank-string cell is a null and takes the default (§5.3)."""
    kept, rejected = process_table(
        zite_frame(SPONSOR_BASE, [{"Age Preference": "   "}]),
        "sponsor",
        cfg,
    )
    assert rejected == []
    assert kept[0]["age_preference"] == "Any Age"


def test_dedup_families_never_merges_emailless_families(cfg: Config) -> None:
    """Families without an email stay separate (null is not a key)."""
    rows = [
        {
            "zite_record_id": "r1",
            "primary_email": None,
            "created": None,
            "phone": "(906) 555-0001",
        },
        {
            "zite_record_id": "r2",
            "primary_email": None,
            "created": None,
            "phone": "(906) 555-0002",
        },
        {
            "zite_record_id": "r3",
            "primary_email": "a@x.com",
            "created": None,
            "phone": None,
        },
        {
            "zite_record_id": "r4",
            "primary_email": "a@x.com",
            "created": None,
            "phone": "(906) 555-0003",
        },
    ]
    merged = dedup_families(rows, cfg)
    emails = [row["primary_email"] for row in merged]
    assert emails.count(None) == 2  # kept apart
    assert emails.count("a@x.com") == 1  # deduped by email


def test_process_table_coerces_free_text_reported_age(cfg: Config) -> None:
    """Free-text age_reported degrades to an int or null (§5.5).

    The computed age_at_xmas is authoritative, so a family typing
    "4 turning 5" must not reject the whole child row.
    """
    children = zite_frame(
        CHILD_BASE,
        [
            {"Age at Christmas 2026": "8"},
            {"Age at Christmas 2026": "4 turning 5"},
            {"Age at Christmas 2026": "2.5"},
            {"Age at Christmas 2026": "young"},
        ],
    )
    kept, rejected = process_table(children, "child", cfg)
    assert rejected == []
    assert [row["age_reported"] for row in kept] == [8, 4, 2, None]


def test_dedup_families_coalesces_and_keeps_earliest(cfg: Config) -> None:
    """Family dedup coalesces first non-null and keeps earliest created (§5.4)."""
    early = {
        "id": "f1",
        "created": "2026-01-01T00:00:00Z",
        "primary_email": "a@x.com",
        "phone": None,
        "city": "Hancock",
    }
    late = {
        "id": "f2",
        "created": "2026-01-02T00:00:00Z",
        "primary_email": "a@x.com",
        "phone": "(906) 555-0000",
        "city": None,
    }
    other = {
        "id": "f3",
        "created": "2026-01-03T00:00:00Z",
        "primary_email": "b@x.com",
        "phone": "(906) 555-1111",
        "city": "Calumet",
    }
    merged = dedup_families([late, other, early], cfg)
    assert len(merged) == 2
    survivor = next(row for row in merged if row["primary_email"] == "a@x.com")
    assert survivor["phone"] == "(906) 555-0000"  # coalesced from later row
    assert survivor["city"] == "Hancock"
    assert survivor["created"] == "2026-01-01T00:00:00Z"  # earliest timestamp


def test_dedup_families_logs_conflict(cfg: Config, caplog) -> None:
    """Conflicting non-null values keep the earliest and log a warning (§5.4)."""
    early = {
        "id": "f1",
        "created": "2026-01-01T00:00:00Z",
        "primary_email": "a@x.com",
        "city": "Hancock",
    }
    late = {
        "id": "f2",
        "created": "2026-01-02T00:00:00Z",
        "primary_email": "a@x.com",
        "city": "Calumet",
    }
    with caplog.at_level("WARNING", logger="ccat.acquire"):
        merged = dedup_families([late, early], cfg)
    assert merged[0]["city"] == "Hancock"
    assert any("dedup conflict" in message for message in caplog.messages)


def test_index_to_letters() -> None:
    """Letters run A..Z then AA, AB (§5.5)."""
    assert index_to_letters(0) == "A"
    assert index_to_letters(25) == "Z"
    assert index_to_letters(26) == "AA"
    assert index_to_letters(27) == "AB"


def test_compute_age_at_xmas(cfg: Config) -> None:
    """Age is full completed years at Christmas."""
    assert compute_age_at_xmas("2020-12-26", cfg.xmas) == 5
    assert compute_age_at_xmas("2020-12-25", cfg.xmas) == 6


def test_assign_family_ids_orders_by_created_then_email(cfg: Config) -> None:
    """family_id is sequential by created then primary_email (§5.5)."""
    rows = [
        {"created": "2026-01-02T00:00:00+00:00", "primary_email": "a@x.com"},
        {"created": "2026-01-01T00:00:00+00:00", "primary_email": "z@x.com"},
        {"created": "2026-01-01T00:00:00+00:00", "primary_email": "b@x.com"},
    ]
    result = assign_family_ids(rows)
    assert [row["family_id"] for row in result] == [1, 2, 3]
    assert [row["primary_email"] for row in result] == [
        "b@x.com",
        "z@x.com",
        "a@x.com",
    ]


def test_assign_sponsor_ids_orders_by_created_then_email(cfg: Config) -> None:
    """sponsor_id is sequential by created then email (§5.5)."""
    rows = [
        {
            "created": "2026-01-01T00:00:00+00:00",
            "email": "b@x.com",
            "age_preference": "Any Age",
        },
        {
            "created": "2026-01-01T00:00:00+00:00",
            "email": "a@x.com",
            "age_preference": "Any Age",
        },
    ]
    result = assign_sponsor_ids(rows)
    assert [row["email"] for row in result] == ["a@x.com", "b@x.com"]
    assert [row["sponsor_id"] for row in result] == [1, 2]
    assert result[0]["pref_age_min"] == 0 and result[0]["pref_age_max"] == 18


def child_row(
    record_id: str, email: str, dob: str, created: str
) -> dict[str, Any]:
    """Build a post-validation child row for ID assignment tests."""
    return {
        "zite_record_id": record_id,
        "primary_email": email,
        "dob": dob,
        "created": created,
    }


def test_assign_angel_ids_oldest_gets_a(cfg: Config) -> None:
    """Letters go to children by descending age (§5.5)."""
    families = [{"family_id": 1, "primary_email": "f@x.com"}]
    children = [
        child_row("c1", "f@x.com", "2016-01-01", "2026-01-01T00:00:00+00:00"),
        child_row("c2", "f@x.com", "2019-01-01", "2026-01-02T00:00:00+00:00"),
        child_row("c3", "f@x.com", "2012-01-01", "2026-01-03T00:00:00+00:00"),
    ]
    result = assign_angel_ids(children, families, cfg)
    by_id = {row["zite_record_id"]: row for row in result}
    assert by_id["c3"]["angel_id"] == "1A"  # oldest
    assert by_id["c1"]["angel_id"] == "1B"
    assert by_id["c2"]["angel_id"] == "1C"
    assert all(row["age_at_xmas"] is not None for row in result)


def test_assign_angel_ids_age_tie_breaks_by_created(cfg: Config) -> None:
    """Equal ages tie-break by earliest created then dob (§5.5)."""
    families = [{"family_id": 1, "primary_email": "f@x.com"}]
    children = [
        child_row(
            "c_late", "f@x.com", "2018-05-10", "2026-01-02T00:00:00+00:00"
        ),
        child_row(
            "c_early", "f@x.com", "2018-05-10", "2026-01-01T00:00:00+00:00"
        ),
    ]
    result = assign_angel_ids(children, families, cfg)
    by_id = {row["zite_record_id"]: row for row in result}
    assert by_id["c_early"]["angel_id"] == "1A"
    assert by_id["c_late"]["angel_id"] == "1B"


def test_assign_angel_ids_orphans_kept_with_null_ids(cfg: Config) -> None:
    """Orphaned children stay in raw_child with null angel_id (§5.5)."""
    families = [{"family_id": 1, "primary_email": "f@x.com"}]
    children = [
        child_row("c1", "f@x.com", "2016-01-01", "2026-01-01T00:00:00+00:00"),
        child_row(
            "orphan", "missing@x.com", "2015-01-01", "2026-01-01T00:00:00+00:00"
        ),
    ]
    result = assign_angel_ids(children, families, cfg)
    orphan = next(row for row in result if row["zite_record_id"] == "orphan")
    assert orphan["angel_id"] is None
    assert orphan["family_id"] is None
    assert orphan in result  # kept, not dropped
    linked = next(row for row in result if row["zite_record_id"] == "c1")
    assert linked["angel_id"] == "1A"


def _acquire_cfg(tmp_path) -> Config:
    """Config for full-run tests with fake Zite refs."""
    config = load_config(
        Path(__file__).resolve().parent.parent / "config" / "ccat_2026.json"
    )
    return config.model_copy(
        update={
            "excel_file": str(tmp_path / "wb.xlsx"),
            "log_file": str(tmp_path / "ccat.log"),
            "match_report_csv": str(tmp_path / "report.csv"),
            "zite": ZiteSpec(
                sponsor="db::sponsor", family="db::family", child="db::child"
            ),
        }
    )


def _scenario_frames() -> dict[str, pl.DataFrame]:
    """Three consistent Zite frames: 2 sponsors, 1 family, 3 children (1 orphan)."""
    sponsor = zite_frame(
        SPONSOR_BASE,
        [
            {
                "id": "rec_s2",
                "createdAt": "2026-01-02T10:00:00Z",
                "Email": "zzz@example.com",
            },
            {
                "id": "rec_s1",
                "createdAt": "2026-01-01T10:00:00Z",
                "Email": "aaa@example.com",
            },
        ],
    )
    family = zite_frame(
        FAMILY_BASE,
        [
            {
                "id": "rec_f1",
                "createdAt": "2026-01-05T10:00:00Z",
                "Email": "fam@example.com",
                "Created time": "2026-01-05T10:00:00Z",
                "Phone Number": None,
            },
            {
                "id": "rec_f2",
                "createdAt": "2026-01-06T10:00:00Z",
                "Email": "fam@example.com",
                "Created time": "2026-01-06T10:00:00Z",
                "Phone Number": "(906) 555-9876",
            },
        ],
    )
    child = zite_frame(
        CHILD_BASE,
        [
            {
                "id": "rec_c1",
                "Primary Email": "fam@example.com",
                "Date of Birth": "2016-03-01",
                "Age at Christmas 2026": "10",
            },
            {
                "id": "rec_c2",
                "Primary Email": "fam@example.com",
                "Date of Birth": "2019-03-01",
                "Age at Christmas 2026": "7",
            },
            {
                "id": "rec_c3",
                "Primary Email": "orphan@example.com",
                "Date of Birth": "2014-03-01",
            },
        ],
    )
    return {"sponsor": sponsor, "family": family, "child": child}


def test_run_end_to_end(tmp_path, monkeypatch, caplog) -> None:
    """Full acquire run writes raw sheets, IDs, and preserves final_* (§5.6)."""
    import logging

    cfg = _acquire_cfg(tmp_path)
    frames = _scenario_frames()
    monkeypatch.setattr(
        acquire.zite, "get_table_data", lambda ref: frames[ref.split("::")[1]]
    )
    with caplog.at_level(logging.WARNING, logger="ccat.acquire"):
        run(cfg)
    raw_sponsor = read_sheet(cfg.excel_file, "raw_sponsor")
    raw_family = read_sheet(cfg.excel_file, "raw_family")
    raw_child = read_sheet(cfg.excel_file, "raw_child")
    assert (
        raw_sponsor is not None
        and raw_family is not None
        and raw_child is not None
    )
    # sponsor_id ordered by created, not by pull order
    assert raw_sponsor["sponsor_id"].to_list() == [1, 2]
    assert raw_sponsor["email"].to_list() == [
        "aaa@example.com",
        "zzz@example.com",
    ]
    # family dedup: one survivor, phone coalesced from the later duplicate
    assert raw_family.height == 1
    assert raw_family["phone"].to_list() == ["(906) 555-9876"]
    # children: letters by age, orphan kept with null ids
    assert raw_child["angel_id"].to_list() == ["1A", "1B", None]
    assert raw_child["age_at_xmas"].to_list() == [10, 7, 12]
    # age_reported mismatch logged for c3 (reported 8, computed 12)
    assert any("age mismatch" in m for m in caplog.messages)
    # orphan logged
    assert any("orphaned child" in m for m in caplog.messages)


def test_run_is_deterministic_on_rerun(tmp_path, monkeypatch) -> None:
    """Re-running with the same data produces stable IDs (§5.6)."""
    cfg = _acquire_cfg(tmp_path)
    frames = _scenario_frames()
    monkeypatch.setattr(
        acquire.zite, "get_table_data", lambda ref: frames[ref.split("::")[1]]
    )
    run(cfg)
    first = read_sheet(cfg.excel_file, "raw_child")
    run(cfg)
    second = read_sheet(cfg.excel_file, "raw_child")
    assert first is not None and second is not None
    assert first.equals(second)


def test_run_never_touches_final_sheets(tmp_path, monkeypatch) -> None:
    """final_child is byte-identical before and after a run (§5.6)."""
    cfg = _acquire_cfg(tmp_path)
    sentinel = pl.DataFrame({"zite_record_id": ["x"], "audited": [True]})
    write_sheets(cfg.excel_file, "audit", {"final_child": sentinel})
    before = read_sheet(cfg.excel_file, "final_child")
    frames = _scenario_frames()
    monkeypatch.setattr(
        acquire.zite, "get_table_data", lambda ref: frames[ref.split("::")[1]]
    )
    run(cfg)
    after = read_sheet(cfg.excel_file, "final_child")
    assert before is not None and after is not None
    assert before.equals(after)


def test_run_fails_on_todo_zite_reference(tmp_path) -> None:
    """A TODO table reference fails fast with an actionable error (§12)."""
    cfg = _acquire_cfg(tmp_path).model_copy(
        update={
            "zite": ZiteSpec(
                sponsor="TODO", family="db::family", child="db::child"
            )
        }
    )
    with pytest.raises(AcquisitionError, match="config.zite.sponsor"):
        run(cfg)


def test_write_sheets_ownership_enforced(tmp_path) -> None:
    """A module may not write sheets it does not own (§9)."""
    cfg = _acquire_cfg(tmp_path)
    frame = pl.DataFrame({"a": [1]})
    with pytest.raises(SheetOwnershipError, match="ownership"):
        write_sheets(cfg.excel_file, "screen", {"raw_child": frame})


def test_excel_round_trip_preserves_values(tmp_path) -> None:
    """Atomic sheet writes round-trip values through the workbook (§4)."""
    cfg = _acquire_cfg(tmp_path)
    frame = pl.DataFrame(
        {"s": ["a", None], "i": [1, 2], "b": [True, False]},
        schema={"s": pl.String, "i": pl.Int64, "b": pl.Boolean},
    )
    write_sheets(cfg.excel_file, "acquire", {"raw_child": frame})
    back = read_sheet(cfg.excel_file, "raw_child")
    assert back is not None and back.equals(frame)


def test_workbook_created_when_missing(tmp_path) -> None:
    """The workbook is created on first write."""
    cfg = _acquire_cfg(tmp_path)
    write_sheets(
        cfg.excel_file,
        "match",
        {"match_report": pl.DataFrame(schema={"k": pl.String})},
    )
    assert read_sheet(cfg.excel_file, "match_report") is not None
