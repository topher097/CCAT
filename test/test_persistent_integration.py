"""Golden integration fixture for persistent-final storage and AI resume."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from ccat import screening
from ccat.audit.store import (
    build_final_frame,
    read_final,
)
from ccat.config import Config
from ccat.io import list_sheets, write_sheets


def _raw(cfg: Config, entity: str, **values: Any) -> dict[str, Any]:
    """Build one raw row containing every configured column."""
    row = {column: None for column in cfg.tables[entity].columns}
    row.update(values)
    return row


def _final(entity: str, **values: Any) -> dict[str, Any]:
    """Build one complete entity row with supplied state overrides."""
    return dict(build_final_frame(entity, [values]).to_dicts()[0])


def _raw_frame(
    cfg: Config, entity: str, rows: list[dict[str, Any]]
) -> pl.DataFrame:
    """Convert raw values to a stable all-text source frame."""
    schema = {column: pl.String for column in cfg.tables[entity].columns}
    return pl.DataFrame(
        [
            {
                key: None if value is None else str(value)
                for key, value in row.items()
            }
            for row in rows
        ],
        schema=schema,
    )


def _seed_golden(cfg: Config) -> dict[str, list[dict[str, Any]]]:
    """Seed the persistent golden state and return replaceable raw rows."""
    sponsor_time = (
        "create" if "create" in cfg.tables["sponsor"].columns else "created"
    )
    raw_sponsor = _raw(
        cfg,
        "sponsor",
        zite_record_id="s-current",
        email="current@example.com",
        first_name="Current",
        last_name="Sponsor",
        age_preference="Any Age",
        gender_preference="Any Gender",
        num_children="1",
        **{sponsor_time: "2026-01-01T00:00:00Z"},
    )
    raw_families = [
        _raw(
            cfg,
            "family",
            zite_record_id=record_id,
            created=created,
            zite_created_at=created,
            primary_email="duplicate@example.com",
            first_name="Family",
            last_name="One",
            gift_pickup_location="Hancock",
        )
        for record_id, created in (
            ("f-early", "2026-01-01T00:00:00Z"),
            ("f-later", "2026-01-03T00:00:00Z"),
        )
    ]
    raw_children = [
        _raw(
            cfg,
            "child",
            zite_record_id="c-old-original",
            created="2026-01-02T00:00:00Z",
            zite_created_at="2026-01-02T00:00:00Z",
            primary_email="duplicate@example.com",
            first_name="Alpha",
            dob="2020-01-01",
            age_reported="6",
            gender="Male",
            need_1="raw coat",
            desire_1="blocks",
        ),
        _raw(
            cfg,
            "child",
            zite_record_id="c-new-duplicate",
            created="2026-01-04T00:00:00Z",
            zite_created_at="2026-01-04T00:00:00Z",
            primary_email="duplicate@example.com",
            first_name="Alpha",
            dob="2020-01-02",
            age_reported="6",
            gender="Male",
            need_1="duplicate coat",
            desire_1="blocks",
        ),
        _raw(
            cfg,
            "child",
            zite_record_id="c-sibling-conflict",
            created="2026-01-02T01:00:00Z",
            zite_created_at="2026-01-02T01:00:00Z",
            primary_email="duplicate@example.com",
            first_name="Beta",
            dob="2020-02-01",
            age_reported="6",
            gender="Female",
            need_1="boots",
            desire_1="books",
        ),
        _raw(
            cfg,
            "child",
            zite_record_id="c-orphan",
            created="2026-01-02T02:00:00Z",
            zite_created_at="2026-01-02T02:00:00Z",
            primary_email="missing@example.com",
            first_name="Orphan",
            dob="2018-03-01",
            age_reported="8",
            gender="Non-binary",
            need_1="shoes",
            desire_1="art",
        ),
    ]
    sponsors = [
        _final(
            "sponsor",
            sponsor_id=1,
            zite_record_id="s-current",
            email="current@example.com",
            first_name="Human",
            age_preference="Any Age",
            gender_preference="Any Gender",
            num_children=1,
            assigned_angel_ids="1A",
            num_angels_assigned=1,
            status="audited",
        ),
        _final(
            "sponsor",
            sponsor_id=8,
            zite_record_id="s-raw-missing-ignored",
            email="ignored@example.com",
            status="ignored",
            ignore=True,
        ),
    ]
    families = [
        _final(
            "family",
            family_id=1,
            zite_record_id="f-early",
            primary_email="duplicate@example.com",
            first_name="Family",
            gift_pickup_location="Hancock",
            assigned_sponsor_id=1,
            assigned_angel_ids="1A",
            num_angels_in_family=1,
            status="audited",
        ),
        _final(
            "family",
            family_id=8,
            zite_record_id="f-raw-missing-ignored",
            primary_email="ignored-family@example.com",
            status="ignored",
            ignore=True,
        ),
    ]
    children = [
        _final(
            "child",
            zite_record_id="c-old-original",
            family_id=1,
            angel_id="1A",
            primary_email="duplicate@example.com",
            first_name="Alpha",
            dob="2020-01-01",
            age_reported=6,
            gender="Male",
            need_1="HUMAN CORRECTED COAT",
            desire_1="blocks",
            generated_info_sheet=True,
            screened=True,
            status="audited",
        ),
        _final(
            "child",
            zite_record_id="c-raw-missing-ignored",
            family_id=1,
            angel_id="1Z",
            first_name="Historical",
            need_1="retain historical work",
            generated_info_sheet=True,
            screened=True,
            status="ignored",
            ignore=True,
        ),
    ]
    write_sheets(
        cfg.excel_file,
        "migrate",
        {
            "raw_sponsor": _raw_frame(cfg, "sponsor", [raw_sponsor]),
            "raw_family": _raw_frame(cfg, "family", raw_families),
            "raw_child": _raw_frame(cfg, "child", raw_children),
            "final_sponsor": build_final_frame("sponsor", sponsors),
            "final_family": build_final_frame("family", families),
            "final_child": build_final_frame("child", children),
        },
        xmas=cfg.xmas,
    )
    return {
        "sponsor": [raw_sponsor],
        "family": raw_families,
        "child": raw_children,
    }


def _replace_raw_children(cfg: Config, rows: list[dict[str, Any]]) -> None:
    """Simulate a later acquisition replacing only ``raw_child``."""
    write_sheets(
        cfg.excel_file,
        "acquire",
        {"raw_child": _raw_frame(cfg, "child", rows)},
    )


def _fake_ai(
    calls: list[list[str]],
    *,
    interrupt_after_first: bool = False,
) -> Callable[..., list[dict[str, Any]]]:
    """Return a screening stub that records IDs and persists through callback."""

    def fake(
        targets: list[dict[str, Any]],
        cfg: Config,
        on_result: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        ids = [str(row["zite_record_id"]) for row in targets]
        calls.append(ids)
        completed: list[dict[str, Any]] = []
        for record_id in ids:
            result = {"zite_record_id": record_id}
            if on_result is not None:
                on_result(result)
            completed.append(result)
            if interrupt_after_first:
                raise KeyboardInterrupt
        return completed

    return fake


def test_golden_append_merge_resume_and_raw_missing(
    screen_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Golden populations and human/module state survive every ordinary rerun."""
    raw = _seed_golden(screen_cfg)
    calls: list[list[str]] = []
    monkeypatch.setattr(screening, "_require_opencode_v2", lambda: None)
    monkeypatch.setattr(screening, "_screen_targets", _fake_ai(calls))

    screening.run(screen_cfg, skip_email_check=True, skip_address_check=True)
    assert calls == [["c-sibling-conflict", "c-new-duplicate", "c-orphan"]]
    children = {
        row["zite_record_id"]: row for row in read_final(screen_cfg, "child")
    }
    assert children["c-old-original"]["angel_id"] == "1A"
    assert children["c-new-duplicate"]["angel_id"] is None
    assert children["c-sibling-conflict"]["angel_id"] == "1B"
    assert children["c-orphan"]["family_id"] is None
    assert (
        "Sibling DOB conflict"
        in children["c-old-original"]["potential_anomoly"]
    )
    assert (
        "Sibling DOB conflict"
        in children["c-new-duplicate"]["potential_anomoly"]
    )
    assert children["c-old-original"]["need_1"] == "HUMAN CORRECTED COAT"
    assert children["c-old-original"]["generated_info_sheet"] is True
    assert children["c-raw-missing-ignored"]["generated_info_sheet"] is True
    assert len(children) == 5
    families = read_final(screen_cfg, "family")
    assert [row["family_id"] for row in families] == [1, 8]
    assert families[0]["assigned_sponsor_id"] == 1
    assert read_final(screen_cfg, "sponsor")[0]["first_name"] == "Human"
    assert not any(
        sheet.startswith("adj_") or sheet.endswith("_submissions")
        for sheet in list_sheets(screen_cfg.excel_file)
    )

    calls.clear()
    screening.run(screen_cfg, skip_email_check=True, skip_address_check=True)
    assert calls == []
    protected = {
        row["zite_record_id"]: row for row in read_final(screen_cfg, "child")
    }
    for key in (
        "need_1",
        "angel_id",
        "generated_info_sheet",
        "status",
        "ignore",
    ):
        assert (
            protected["c-old-original"][key] == children["c-old-original"][key]
        )

    _replace_raw_raw = raw["child"]
    _replace_raw_children(screen_cfg, _replace_raw_raw[1:])
    calls.clear()
    screening.run(screen_cfg, skip_email_check=True, skip_address_check=True)
    retained = {
        row["zite_record_id"]: row for row in read_final(screen_cfg, "child")
    }
    assert retained["c-old-original"]["need_1"] == "HUMAN CORRECTED COAT"
    assert retained["c-old-original"]["angel_id"] == "1A"
    assert retained["c-new-duplicate"]["angel_id"] is None
    assert retained["c-raw-missing-ignored"]["ignore"] is True


def test_interrupted_ai_commits_completed_and_resumes_pending(
    screen_cfg: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An interrupted batch never removes pending or raw-missing entities."""
    _seed_golden(screen_cfg)
    monkeypatch.setattr(screening, "_require_opencode_v2", lambda: None)
    first_calls: list[list[str]] = []
    monkeypatch.setattr(
        screening,
        "_screen_targets",
        _fake_ai(first_calls, interrupt_after_first=True),
    )
    with pytest.raises(KeyboardInterrupt):
        screening.run(
            screen_cfg, skip_email_check=True, skip_address_check=True
        )
    rows = {
        row["zite_record_id"]: row for row in read_final(screen_cfg, "child")
    }
    assert rows["c-sibling-conflict"]["screened"] is True
    assert rows["c-new-duplicate"]["screened"] is False
    assert rows["c-orphan"]["screened"] is False
    assert rows["c-old-original"]["need_1"] == "HUMAN CORRECTED COAT"
    assert "c-raw-missing-ignored" in rows

    second_calls: list[list[str]] = []
    monkeypatch.setattr(screening, "_screen_targets", _fake_ai(second_calls))
    screening.run(screen_cfg, skip_email_check=True, skip_address_check=True)
    assert second_calls == [["c-new-duplicate", "c-orphan"]]
    assert all(
        row["screened"] is True for row in read_final(screen_cfg, "child")
    )
