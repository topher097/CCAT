"""Tests for module 1 — data acquisition and validation (§5)."""

import json
from datetime import date
from typing import Any

from pathlib import Path

import polars as pl
import pytest

from ccat import acquire
from ccat.acquire import (
    AcquisitionError,
    _dob_gap_months,
    assign_angel_ids,
    assign_family_ids,
    assign_sponsor_ids,
    clean_value,
    compute_age_at_xmas,
    dedup_children,
    dedup_families,
    import_table,
    index_to_letters,
    process_table,
    run,
    sibling_dob_conflicts,
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
    kept, rejected, _dedup = process_table(
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
    kept, rejected, _dedup = process_table(
        zite_frame(SPONSOR_BASE, [{"Email": "broken"}, {}]), "sponsor", cfg
    )
    assert len(kept) == 1
    assert kept[0]["email"] == "sponsor@example.com"
    assert len(rejected) == 1
    assert rejected[0]["column"] == "email"
    assert rejected[0]["reason"] == "invalid email"


def test_process_table_rejects_invalid_enum_value(cfg: Config) -> None:
    """Enum values outside the vocabulary reject the row (§5.3)."""
    kept, rejected, _dedup = process_table(
        zite_frame(SPONSOR_BASE, [{"Affiliation": "Sole Proprietor"}]),
        "sponsor",
        cfg,
    )
    assert kept == []
    assert rejected[0]["column"] == "sponsor_type"


def test_process_table_keeps_null_without_default(cfg: Config) -> None:
    """Nulls in columns without a default are kept as null (§5.2)."""
    kept, rejected, _dedup = process_table(
        zite_frame(SPONSOR_BASE, [{"First Name": None}]), "sponsor", cfg
    )
    assert rejected == []
    assert len(kept) == 1
    assert kept[0]["first_name"] is None


def test_process_table_fills_default_on_null(cfg: Config) -> None:
    """Nulls take the configured default when one is specified (§5.2)."""
    kept, rejected, _dedup = process_table(
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
    kept, rejected, _dedup = process_table(
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
    merged, _log = dedup_families(rows, cfg)
    emails = [row["primary_email"] for row in merged]
    assert emails.count(None) == 2  # kept apart
    assert emails.count("a@x.com") == 1  # deduped by email
    assert _log == [
        {
            "sheet": "family",
            "primary_email": "a@x.com",
            "kept_zite_record_id": "r3",
            "kept_created": None,
            "duplicate_zite_record_id": "r4",
            "duplicate_created": None,
            "conflict_columns": "",
            "conflict_details": "",
            "kept_record": _log[0]["kept_record"],
            "duplicate_record": _log[0]["duplicate_record"],
        }
    ]


def test_process_table_coerces_free_text_reported_age(cfg: Config) -> None:
    """Free-text age_reported degrades to an int or null (§5.5).

    The computed age_at_xmas is authoritative, so a family typing
    "4 turning 5" must not reject the whole child row.
    """
    children = zite_frame(
        CHILD_BASE,
        [
            {"Age at Christmas 2026": "8"},
            {"First Name": "kiddo2", "Age at Christmas 2026": "4 turning 5"},
            {"First Name": "kiddo3", "Age at Christmas 2026": "2.5"},
            {"First Name": "kiddo4", "Age at Christmas 2026": "young"},
        ],
    )
    kept, rejected, _dedup = process_table(children, "child", cfg)
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
    merged, _log = dedup_families([late, other, early], cfg)
    assert len(merged) == 2
    survivor = next(row for row in merged if row["primary_email"] == "a@x.com")
    assert survivor["phone"] == "(906) 555-0000"  # coalesced from later row
    assert survivor["city"] == "Hancock"
    assert survivor["created"] == "2026-01-01T00:00:00Z"  # earliest timestamp


def test_dedup_families_logs_conflict(cfg: Config, caplog) -> None:
    """Conflicting non-null values keep the earliest and log a warning (§5.4)."""
    early = {
        "zite_record_id": "r1",
        "created": "2026-01-01T00:00:00Z",
        "primary_email": "a@x.com",
        "city": "Hancock",
    }
    late = {
        "zite_record_id": "r2",
        "created": "2026-01-02T00:00:00Z",
        "primary_email": "a@x.com",
        "city": "Calumet",
    }
    with caplog.at_level("WARNING", logger="ccat.acquire"):
        merged, log = dedup_families([late, early], cfg)
    assert merged[0]["city"] == "Hancock"
    assert any("dedup conflict" in message for message in caplog.messages)
    # the log row records the conflict and both records (§5.4)
    assert len(log) == 1
    assert log[0]["primary_email"] == "a@x.com"
    assert log[0]["kept_zite_record_id"] == "r1"
    assert log[0]["duplicate_zite_record_id"] == "r2"
    assert log[0]["conflict_columns"] == "city"
    assert (
        log[0]["conflict_details"]
        == "city: kept 'Hancock' / duplicate 'Calumet'"
    )
    assert json.loads(log[0]["kept_record"])["city"] == "Hancock"
    assert json.loads(log[0]["duplicate_record"])["city"] == "Calumet"


def test_dedup_children_keeps_most_recent(cfg: Config) -> None:
    """Child dedup keeps the latest submission per family + name (§5.4)."""
    early = {
        "zite_record_id": "r1",
        "created": "2026-01-01T00:00:00Z",
        "primary_email": "a@x.com",
        "first_name": "Kiddo",
        "dob": "2018-05-10",
        "need_1": "winter coat",
    }
    late = {
        "zite_record_id": "r2",
        "created": "2026-01-02T00:00:00Z",
        "primary_email": "a@x.com",
        "first_name": "Kiddo",
        "dob": "2018-05-10",
        "need_1": "winter coat, mittens",
    }
    other = {
        "zite_record_id": "r3",
        "created": "2026-01-03T00:00:00Z",
        "primary_email": "a@x.com",
        "first_name": "Sibling",
        "dob": "2016-03-01",
        "need_1": "hat",
    }
    merged, log = dedup_children([early, late, other], cfg)
    assert sorted(row["zite_record_id"] for row in merged) == ["r2", "r3"]
    kept = next(row for row in merged if row["zite_record_id"] == "r2")
    assert kept["need_1"] == "winter coat, mittens"  # latest entry as-is
    assert len(log) == 1
    assert log[0]["sheet"] == "child"
    assert log[0]["primary_email"] == "a@x.com"
    assert log[0]["kept_zite_record_id"] == "r2"
    assert log[0]["kept_created"] == "2026-01-02T00:00:00Z"
    assert log[0]["duplicate_zite_record_id"] == "r1"
    assert log[0]["duplicate_created"] == "2026-01-01T00:00:00Z"
    assert log[0]["conflict_columns"] == "need_1"
    assert json.loads(log[0]["kept_record"])["need_1"] == (
        "winter coat, mittens"
    )
    assert json.loads(log[0]["duplicate_record"])["need_1"] == "winter coat"


def test_dedup_children_never_merges_distinct_or_unkeyed(cfg: Config) -> None:
    """Distinct children and unkeyed rows stay separate (§5.4)."""
    rows = [
        {  # same name, different family -> kept apart
            "zite_record_id": "r1",
            "created": "2026-01-01T00:00:00Z",
            "primary_email": "a@x.com",
            "first_name": "Kiddo",
        },
        {
            "zite_record_id": "r2",
            "created": "2026-01-02T00:00:00Z",
            "primary_email": "b@x.com",
            "first_name": "Kiddo",
        },
        {  # different name, same family -> kept apart
            "zite_record_id": "r3",
            "created": "2026-01-03T00:00:00Z",
            "primary_email": "a@x.com",
            "first_name": "Sibling",
        },
        {  # no email -> never merged
            "zite_record_id": "r4",
            "created": "2026-01-04T00:00:00Z",
            "primary_email": None,
            "first_name": "Kiddo",
        },
        {  # no name -> never merged
            "zite_record_id": "r5",
            "created": "2026-01-05T00:00:00Z",
            "primary_email": "a@x.com",
            "first_name": None,
        },
    ]
    merged, log = dedup_children(rows, cfg)
    assert len(merged) == 5
    assert log == []


def test_process_table_dedups_duplicate_child(cfg: Config) -> None:
    """Same child submitted twice in one family: latest entry survives."""
    children = zite_frame(
        CHILD_BASE,
        [
            {"id": "rec_c1", "Need #1": "winter coat"},
            {
                "id": "rec_c2",
                "Creation Time": "2026-02-02T09:00:00Z",
                "Need #1": "winter coat, mittens",
            },
        ],
    )
    kept, rejected, dedup = process_table(children, "child", cfg)
    assert rejected == []
    assert len(kept) == 1
    assert kept[0]["zite_record_id"] == "rec_c2"
    assert kept[0]["need_1"] == "winter coat, mittens"
    assert len(dedup) == 1
    assert dedup[0]["sheet"] == "child"
    assert dedup[0]["duplicate_zite_record_id"] == "rec_c1"


def _dob_check_child(
    record_id: str,
    family_id: int | None,
    dob: str | None,
    angel_id: str | None = None,
    first_name: str = "Kiddo",
) -> dict[str, Any]:
    """One child row for sibling-dob checks (§5.6)."""
    return {
        "zite_record_id": record_id,
        "family_id": family_id,
        "angel_id": angel_id,
        "first_name": first_name,
        "primary_email": ("fam@example.com" if family_id is not None else None),
        "dob": dob,
    }


def test_dob_gap_months_calendar_arithmetic() -> None:
    """Completed months are day-of-month aware; equal dates yield 0 (§5.6)."""
    assert _dob_gap_months(date(2020, 1, 15), date(2020, 10, 16)) == 9
    assert _dob_gap_months(date(2020, 1, 15), date(2020, 10, 14)) == 8
    assert _dob_gap_months(date(2020, 1, 31), date(2020, 2, 1)) == 0
    assert _dob_gap_months(date(2020, 1, 15), date(2020, 1, 15)) == 0
    assert _dob_gap_months(date(2019, 1, 15), date(2020, 1, 15)) == 12


def test_sibling_dob_conflicts_flags_close_pairs(cfg: Config) -> None:
    """Same-family pairs <=9 months apart are logged once, in order (§5.6)."""
    rows = [
        _dob_check_child("c1", 1, "2020-01-15", "1A", "Kiddo One"),
        _dob_check_child("c2", 1, "2020-08-20", "1B", "Kiddo Two"),
        _dob_check_child("c3", 1, "2020-11-20", "1C", "Kiddo Three"),
        _dob_check_child("c7", 1, "2021-12-15", "1D", "Kiddo Far"),
        _dob_check_child("c4", 2, "2020-01-15", "2A", "Other Kiddo"),
        _dob_check_child("c5", 1, None, None),
        _dob_check_child("c6", None, "2020-01-15", None, "Orphan"),
    ]
    log = sibling_dob_conflicts(rows, cfg)
    pairs = [
        (
            row["child_a_zite_record_id"],
            row["child_b_zite_record_id"],
            row["gap_months"],
        )
        for row in log
    ]
    # c1->c2 is 7 months, c2->c3 is 3 months; c1->c3 (10) and everything
    # involving the far/distant/orphan/null-dob children are not flagged.
    # c4 shares c1's dob but belongs to another family.
    assert pairs == [("c1", "c2", 7), ("c2", "c3", 3)]
    assert log[0]["family_id"] == 1
    assert log[0]["child_a_first_name"] == "Kiddo One"
    assert log[0]["child_b_angel_id"] == "1B"


def test_sibling_dob_conflicts_flags_twins(cfg: Config) -> None:
    """Same-date siblings (twins) are recorded with a 0-month gap (§5.6)."""
    rows = [
        _dob_check_child("t1", 1, "2020-05-01", "1A", "Twin One"),
        _dob_check_child("t2", 1, "2020-05-01", "1B", "Twin Two"),
    ]
    log = sibling_dob_conflicts(rows, cfg)
    assert len(log) == 1
    assert log[0]["gap_months"] == 0
    assert log[0]["gap_days"] == 0
    assert log[0]["child_a_zite_record_id"] == "t1"
    assert log[0]["child_b_zite_record_id"] == "t2"


def test_sibling_dob_conflicts_never_repeats_a_pair(cfg: Config) -> None:
    """A pair reaching the check twice logs once, in one direction."""
    rows = [
        _dob_check_child("c1", 1, "2020-01-15", "1A", "Kiddo One"),
        _dob_check_child("c2", 1, "2020-08-20", "1B", "Kiddo Two"),
        # Duplicated submission of the same child.
        _dob_check_child("c2", 1, "2020-08-20", "1B", "Kiddo Two"),
    ]
    log = sibling_dob_conflicts(rows, cfg)
    unordered = [
        frozenset(
            (row["child_a_zite_record_id"], row["child_b_zite_record_id"])
        )
        for row in log
    ]
    assert len(log) == len(set(unordered)) == 1
    assert log[0]["child_a_zite_record_id"] == "c1"
    assert log[0]["child_b_zite_record_id"] == "c2"


def test_sibling_dob_conflicts_day_threshold_boundary(cfg: Config) -> None:
    """Pairs at exactly the configured threshold flag; one day more does not (§5.6)."""
    from datetime import timedelta

    base = date(2020, 1, 15)
    max_days = cfg.acquire.sibling_dob_max_gap_days
    rows = [
        # family 1: exactly at the threshold -> flagged
        _dob_check_child("b1", 1, base.isoformat(), "1A"),
        _dob_check_child(
            "b2", 1, (base + timedelta(days=max_days)).isoformat(), "1B"
        ),
        # family 2: one day past the threshold -> not flagged
        _dob_check_child("c1", 2, base.isoformat(), "2A"),
        _dob_check_child(
            "c2", 2, (base + timedelta(days=max_days + 1)).isoformat(), "2B"
        ),
    ]
    log = sibling_dob_conflicts(rows, cfg)
    pairs = [
        (row["child_a_zite_record_id"], row["child_b_zite_record_id"])
        for row in log
    ]
    assert pairs == [("b1", "b2")]
    assert log[0]["gap_days"] == max_days


def test_sibling_dob_conflicts_threshold_from_config(cfg: Config) -> None:
    """A custom ``acquire.sibling_dob_max_gap_days`` changes the flags (§5.6)."""
    from datetime import timedelta

    from ccat.config import AcquireSpec

    tight = cfg.model_copy(
        update={"acquire": AcquireSpec(sibling_dob_max_gap_days=100)}
    )
    base = date(2020, 1, 15)
    rows = [
        _dob_check_child("b1", 1, base.isoformat(), "1A"),
        _dob_check_child(
            "b2", 1, (base + timedelta(days=150)).isoformat(), "1B"
        ),
    ]
    assert len(sibling_dob_conflicts(rows, cfg)) == 1  # 150 <= 274 default
    assert sibling_dob_conflicts(rows, tight) == []  # 150 > 100


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
    """Three consistent Zite frames: 2 sponsors, 1 family, 4 children (1 orphan, 1 duplicate submission)."""
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
                "First Name": "sibling",
                "Primary Email": "fam@example.com",
                "Date of Birth": "2019-03-01",
                "Age at Christmas 2026": "7",
            },
            {
                "id": "rec_c3",
                "Primary Email": "orphan@example.com",
                "Date of Birth": "2014-03-01",
            },
            {
                # duplicate submission of rec_c1 (same family + name);
                # the most recent entry (rec_c4) must win (§5.4)
                "id": "rec_c4",
                "Creation Time": "2026-02-03T09:00:00Z",
                "Need #1": "winter coat, mittens",
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
    # dedup_log records the merged family duplicate (§5.4) and the
    # dropped child duplicate (§5.4b)
    dedup_log = read_sheet(cfg.excel_file, "dedup_log")
    assert dedup_log is not None and dedup_log.height == 2
    log_row = dedup_log.to_dicts()[0]
    assert log_row["sheet"] == "family"
    assert log_row["primary_email"] == "fam@example.com"
    assert log_row["kept_zite_record_id"] == "rec_f1"
    assert log_row["duplicate_zite_record_id"] == "rec_f2"
    # the phone was coalesced (kept null), not a conflict (empty cells
    # read back as null from xlsx)
    assert not log_row["conflict_columns"]
    assert not log_row["conflict_details"]
    assert json.loads(log_row["duplicate_record"])["phone"] == "(906) 555-9876"
    child_log_row = dedup_log.to_dicts()[1]
    assert child_log_row["sheet"] == "child"
    assert child_log_row["primary_email"] == "fam@example.com"
    assert child_log_row["kept_zite_record_id"] == "rec_c4"
    assert child_log_row["duplicate_zite_record_id"] == "rec_c1"
    assert json.loads(child_log_row["kept_record"])["need_1"] == (
        "winter coat, mittens"
    )
    assert json.loads(child_log_row["duplicate_record"])["need_1"] == (
        "winter coat"
    )
    # children: the kept "kiddo" (rec_c4, age 8) is older than "sibling"
    # (age 7), so it takes 1A; the orphan keeps null ids.  Rows are
    # ordered by family (age-descending), orphans last.
    assert raw_child["angel_id"].to_list() == ["1A", "1B", None]
    assert raw_child["age_at_xmas"].to_list() == [8, 7, 12]
    # age_reported mismatch logged for c3 (reported 8, computed 12)
    assert any("age mismatch" in m for m in caplog.messages)
    # orphan logged
    assert any("orphaned child" in m for m in caplog.messages)


def test_run_writes_dedup_log_with_conflicts(tmp_path, monkeypatch) -> None:
    """dedup_log records duplicates, conflicts, and raw contents (§5.4)."""
    cfg = _acquire_cfg(tmp_path)
    sponsor = zite_frame(
        SPONSOR_BASE,
        [{"id": "rec_s1", "createdAt": "2026-01-01T10:00:00Z"}],
    )
    family = zite_frame(
        FAMILY_BASE,
        [
            {
                "id": "rec_f1",
                "createdAt": "2026-01-05T10:00:00Z",
                "Created time": "2026-01-05T10:00:00Z",
                "Phone Number": "(906) 555-1111",
                "City": "Hancock",
            },
            {
                "id": "rec_f2",
                "createdAt": "2026-01-06T10:00:00Z",
                "Created time": "2026-01-06T10:00:00Z",
                "Phone Number": "(906) 555-2222",
                "City": "Calumet",
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
            }
        ],
    )
    frames = {"sponsor": sponsor, "family": family, "child": child}
    monkeypatch.setattr(
        acquire.zite, "get_table_data", lambda ref: frames[ref.split("::")[1]]
    )
    run(cfg)
    dedup_log = read_sheet(cfg.excel_file, "dedup_log")
    assert dedup_log is not None and dedup_log.height == 1
    log_row = dedup_log.to_dicts()[0]
    assert log_row["sheet"] == "family"
    assert log_row["primary_email"] == "fam@example.com"
    assert log_row["kept_zite_record_id"] == "rec_f1"
    assert log_row["kept_created"] == "2026-01-05T10:00:00+00:00"
    assert log_row["duplicate_zite_record_id"] == "rec_f2"
    assert log_row["duplicate_created"] == "2026-01-06T10:00:00+00:00"
    assert log_row["conflict_columns"] == "phone,city"
    assert log_row["conflict_details"] == (
        "phone: kept '(906) 555-1111' / duplicate '(906) 555-2222';"
        " city: kept 'Hancock' / duplicate 'Calumet'"
    )
    assert json.loads(log_row["kept_record"])["city"] == "Hancock"
    assert json.loads(log_row["duplicate_record"])["phone"] == "(906) 555-2222"
    # raw_family keeps the merged survivor with the earliest values
    raw_family = read_sheet(cfg.excel_file, "raw_family")
    assert raw_family is not None and raw_family.height == 1
    assert raw_family["phone"].to_list() == ["(906) 555-1111"]
    assert raw_family["city"].to_list() == ["Hancock"]


def test_run_writes_dob_conflict_log(tmp_path, monkeypatch) -> None:
    """dob_conflict_log records same-family children born <=9 months apart (§5.6)."""
    cfg = _acquire_cfg(tmp_path)
    sponsor = zite_frame(
        SPONSOR_BASE,
        [{"id": "rec_s1", "createdAt": "2026-01-01T10:00:00Z"}],
    )
    family = zite_frame(
        FAMILY_BASE,
        [{"id": "rec_f1", "createdAt": "2026-01-05T10:00:00Z"}],
    )
    child = zite_frame(
        CHILD_BASE,
        [
            {
                "id": "rec_c1",
                "Primary Email": "fam@example.com",
                "First Name": "older kiddo",
                "Date of Birth": "2020-01-15",
                "Age at Christmas 2026": "6",
            },
            {
                "id": "rec_c2",
                "Primary Email": "fam@example.com",
                "First Name": "younger kiddo",
                "Date of Birth": "2020-08-20",
                "Age at Christmas 2026": "6",
            },
            {
                "id": "rec_c3",
                "Primary Email": "fam@example.com",
                "First Name": "distant kiddo",
                "Date of Birth": "2022-03-01",
                "Age at Christmas 2026": "4",
            },
        ],
    )
    frames = {"sponsor": sponsor, "family": family, "child": child}
    monkeypatch.setattr(
        acquire.zite, "get_table_data", lambda ref: frames[ref.split("::")[1]]
    )
    run(cfg)
    dob_log = read_sheet(cfg.excel_file, "dob_conflict_log")
    assert dob_log is not None and dob_log.height == 1
    log_row = dob_log.to_dicts()[0]
    assert log_row["family_id"] == 1
    assert log_row["primary_email"] == "fam@example.com"
    assert log_row["child_a_zite_record_id"] == "rec_c1"
    assert log_row["child_a_angel_id"] == "1A"
    assert log_row["child_a_first_name"] == "Older Kiddo"
    assert log_row["child_a_dob"] == "2020-01-15"
    assert log_row["child_b_zite_record_id"] == "rec_c2"
    assert log_row["child_b_angel_id"] == "1B"
    assert log_row["child_b_dob"] == "2020-08-20"
    assert log_row["gap_months"] == 7
    assert log_row["gap_days"] == 218
    # the distant sibling produced no row; raw_child still holds all three
    raw_child = read_sheet(cfg.excel_file, "raw_child")
    assert raw_child is not None and raw_child.height == 3


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
