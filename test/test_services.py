"""Characterization tests for the Phase 1b service layer (§5.1, §9.3)."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ccat.audit import AuditError
from ccat.audit.store import (
    STATUS_AUDITED,
    STATUS_IGNORED,
    STATUS_NEEDS_CLARIFICATION,
    STATUS_NEW,
    build_final_frame,
    read_final,
    save_child,
    save_family,
    save_sponsor,
)
from ccat.config import Config
from ccat.io import write_sheets
from ccat.services import pipeline
from ccat.services.audit import (
    SavePatch,
    ignore_entity,
    reconcile,
    save_entity,
    status_of_row,
    unignore_entity,
)
from ccat.services.special_needs import (
    SpecialNeeds,
    parse_special_needs,
    serialize_special_needs,
)


def _child(cfg: Config, record_id: str) -> dict[str, Any]:
    """Return one stored final child row by id."""
    return next(
        row
        for row in read_final(cfg, "child")
        if row["zite_record_id"] == record_id
    )


def _set_child_module_flags(cfg: Config, record_id: str) -> None:
    """Mark one child screened/generated/duplicate like module writes."""
    children = read_final(cfg, "child")
    for row in children:
        if row["zite_record_id"] == record_id:
            row["screened"] = True
            row["generated_info_sheet"] = True
            row["potential_duplicate"] = True
    write_sheets(
        cfg.excel_file,
        "generate",
        {"final_child": build_final_frame("child", children)},
        xmas=cfg.xmas,
    )


# ---------------------------------------------------------------------------
# Tri-state save_entity (§9.3).
# ---------------------------------------------------------------------------


def test_save_entity_child_tri_state(cfg: Config) -> None:
    """Omitted keeps stored, null clears, value updates (child)."""
    from test_audit_store import seed_final_sheets

    seed_final_sheets(cfg)
    save_child(
        cfg,
        "c1",
        {
            "need_1": "stored coat",
            "clarification_notes": "call family",
            "status": "new",
        },
    )

    kept = save_entity(cfg, "child", "c1", SavePatch(shoe_size="13Y"))
    row = _child(cfg, "c1")
    assert kept.applied == {"shoe_size": "13Y"}
    assert kept.status == STATUS_NEEDS_CLARIFICATION
    assert kept.audited_at is not None
    assert row["shoe_size"] == "13Y"
    assert row["need_1"] == "stored coat"
    assert row["clarification_notes"] == "call family"

    cleared = save_entity(
        cfg, "child", "c1", SavePatch(clarification_notes=None)
    )
    row = _child(cfg, "c1")
    assert cleared.applied == {"clarification_notes": None}
    assert row["clarification_notes"] is None
    assert row["need_1"] == "stored coat"
    assert cleared.status == STATUS_AUDITED or cleared.status in {
        "in_progress",
        STATUS_NEW,
    }

    updated = save_entity(
        cfg, "child", "c1", SavePatch(need_1="final coat", age_reported=9)
    )
    row = _child(cfg, "c1")
    assert updated.applied == {"need_1": "final coat", "age_reported": 9}
    assert row["need_1"] == "final coat"
    assert row["age_reported"] == 9
    assert row["shoe_size"] == "13Y"


def test_save_entity_omitted_status_preserves_audited(cfg: Config) -> None:
    """A field patch never touches the audited authority."""
    from test_audit_store import seed_final_sheets

    seed_final_sheets(cfg)
    save_child(cfg, "c1", {"need_1": "stored", "status": "audited"})

    result = save_entity(cfg, "child", "c1", SavePatch(shoe_size="10"))

    assert result.status == STATUS_AUDITED
    assert _child(cfg, "c1")["status"] == STATUS_AUDITED


def test_save_entity_family_tri_state(cfg: Config) -> None:
    """Omitted keeps stored, null clears, value updates (family)."""
    from test_audit_store import seed_final_sheets

    seed_final_sheets(cfg)
    save_family(
        cfg,
        1,
        {
            "city": "Hancock",
            "clarification_notes": "verify address",
            "status": "new",
        },
    )

    kept = save_entity(cfg, "family", 1, SavePatch(phone="(906) 555-0000"))
    row = read_final(cfg, "family")[0]
    assert kept.applied == {"phone": "(906) 555-0000"}
    assert row["phone"] == "(906) 555-0000"
    assert row["city"] == "Hancock"
    assert row["clarification_notes"] == "verify address"

    save_entity(
        cfg, "family", 1, SavePatch(clarification_notes=None, city=None)
    )
    row = read_final(cfg, "family")[0]
    assert row["clarification_notes"] is None
    assert row["city"] is None
    assert row["phone"] == "(906) 555-0000"


def test_save_entity_sponsor_tri_state(cfg: Config) -> None:
    """Omitted keeps stored, null clears, value updates (sponsor)."""
    from test_audit_store import seed_final_sheets

    seed_final_sheets(cfg)
    save_sponsor(
        cfg,
        1,
        {
            "organization_name": "Toys Inc",
            "clarification_notes": "verify",
            "status": "new",
        },
    )

    kept = save_entity(cfg, "sponsor", 1, SavePatch(num_children=3))
    row = read_final(cfg, "sponsor")[0]
    assert kept.applied == {"num_children": 3}
    assert row["num_children"] == 3
    assert row["organization_name"] == "Toys Inc"
    assert row["clarification_notes"] == "verify"

    save_entity(cfg, "sponsor", 1, SavePatch(organization_name=None))
    row = read_final(cfg, "sponsor")[0]
    assert row["organization_name"] is None
    assert row["clarification_notes"] == "verify"

    recomputed = save_entity(
        cfg,
        "sponsor",
        1,
        SavePatch(age_preference="Middle School (11-13 yrs)"),
    )
    row = read_final(cfg, "sponsor")[0]
    assert recomputed.applied == {"age_preference": "Middle School (11-13 yrs)"}
    assert row["pref_age_min"] == 11
    assert row["pref_age_max"] == 13


# ---------------------------------------------------------------------------
# Ignore / unignore actions (§5.5).
# ---------------------------------------------------------------------------


def test_ignore_entity_requires_confirm_and_skeletonizes(
    cfg: Config,
) -> None:
    """Without confirm nothing changes; with confirm the row skeletonizes."""
    from test_audit_store import seed_final_sheets

    seed_final_sheets(cfg)
    save_child(cfg, "c1", {"need_1": "human edit", "status": "new"})
    _set_child_module_flags(cfg, "c1")

    with pytest.raises(AuditError, match="confirm=True"):
        ignore_entity(cfg, "child", "c1", confirm=False)
    assert _child(cfg, "c1")["ignore"] is False

    result = ignore_entity(cfg, "child", "c1", confirm=True)

    row = _child(cfg, "c1")
    assert result.status == STATUS_IGNORED
    assert result.applied == {"ignore": True}
    assert result.audited_at is not None
    assert row["ignore"] is True
    assert row["status"] == STATUS_IGNORED
    assert row["need_1"] is None
    assert row["age_at_xmas"] is None
    assert row["first_name"] == "Kiddo"
    assert row["primary_email"] == "fam@example.com"
    assert row["screened"] is True
    assert row["generated_info_sheet"] is True
    assert row["potential_duplicate"] is True


def test_unignore_entity_requires_confirm_and_reseeds(
    cfg: Config,
) -> None:
    """Unignore refuses without confirm and reseeds from the baseline."""
    from test_audit_store import seed_final_sheets

    seed_final_sheets(cfg)
    _set_child_module_flags(cfg, "c1")
    save_child(cfg, "c1", {"need_1": "human edit", "status": "new"})
    ignore_entity(cfg, "child", "c1", confirm=True)

    with pytest.raises(AuditError, match="confirm=True"):
        unignore_entity(cfg, "child", "c1", confirm=False)
    assert _child(cfg, "c1")["ignore"] is True

    result = unignore_entity(cfg, "child", "c1", confirm=True)

    row = _child(cfg, "c1")
    assert result.applied == {"ignore": False}
    assert result.audited_at is not None
    assert row["ignore"] is False
    assert row["need_1"] == "winter coat taht fits"
    assert row["desire_1"] == "N/A"
    assert row["status"] == STATUS_NEW
    assert row["screened"] is True
    assert row["generated_info_sheet"] is True
    assert row["potential_duplicate"] is True


def test_unignore_entity_refuses_non_ignored_row(cfg: Config) -> None:
    """Unignoring a row that is not ignored never clears its data."""
    from test_audit_store import seed_final_sheets

    seed_final_sheets(cfg)
    save_child(cfg, "c1", {"need_1": "kept", "status": "new"})

    with pytest.raises(AuditError, match="is not ignored"):
        unignore_entity(cfg, "child", "c1", confirm=True)

    assert _child(cfg, "c1")["need_1"] == "kept"


def test_ignore_entity_family_skeleton_keeps_identity(cfg: Config) -> None:
    """A family ignore keeps names/email and nulls the rest."""
    from test_audit_store import seed_final_sheets

    seed_final_sheets(cfg)
    save_family(cfg, 1, {"phone": "(906) 555-0000", "status": "new"})

    ignore_entity(cfg, "family", 1, confirm=True)

    row = read_final(cfg, "family")[0]
    assert row["ignore"] is True
    assert row["status"] == STATUS_IGNORED
    assert row["first_name"] == "Jane"
    assert row["primary_email"] == "fam@example.com"
    assert row["phone"] is None


# ---------------------------------------------------------------------------
# Validation and errors.
# ---------------------------------------------------------------------------


def test_save_patch_rejects_unknown_fields() -> None:
    """The patch DTO forbids fields outside the editable vocabulary."""
    with pytest.raises(ValidationError):
        SavePatch.model_validate({"nickname": "x"})


def test_save_entity_rejects_cross_entity_fields(cfg: Config) -> None:
    """A family field on a child save fails against the editable list."""
    from test_audit_store import seed_final_sheets

    seed_final_sheets(cfg)
    with pytest.raises(AuditError, match="unknown child fields"):
        save_entity(cfg, "child", "c1", SavePatch(city="Houghton"))


def test_save_entity_rejects_unknown_entity_and_key(
    cfg: Config,
) -> None:
    """Unknown entities and keys fail with typed errors."""
    from test_audit_store import seed_final_sheets

    seed_final_sheets(cfg)
    with pytest.raises(AuditError, match="unknown entity"):
        save_entity(cfg, "dog", 1, SavePatch())
    with pytest.raises(AuditError, match="unknown child"):
        save_entity(cfg, "child", "nope", SavePatch())
    with pytest.raises(AuditError, match="unknown family"):
        save_entity(cfg, "family", "not-a-number", SavePatch())


# ---------------------------------------------------------------------------
# Special-needs parse/serialize (moved from the NiceGUI selector).
# ---------------------------------------------------------------------------


def test_parse_special_needs_fixed_and_other() -> None:
    """Fixed pieces split from the "other: custom" remainder."""
    parsed = parse_special_needs("ADHD; other: speech delay")
    assert parsed.fixed == {"ADHD"}
    assert parsed.other == "speech delay"
    assert parse_special_needs(None) == SpecialNeeds()
    assert parse_special_needs("") == SpecialNeeds()


def test_serialize_special_needs_sorted_with_other_last() -> None:
    """Fixed pieces sort; "other: {custom}" (or bare) always goes last."""
    # The custom text is emitted only when the "other" piece is itself
    # selected (legacy checkbox semantics: parsing "other: x" never
    # re-selects the "other" piece).
    assert (
        serialize_special_needs(
            SpecialNeeds(fixed={"ADHD", "other"}, other="delay")
        )
        == "ADHD; other: delay"
    )
    assert serialize_special_needs(SpecialNeeds(fixed={"ADHD"}, other="d")) == (
        "ADHD"
    )
    assert (
        serialize_special_needs(SpecialNeeds(fixed={"Down Syndrome", "ADHD"}))
        == "ADHD; Down Syndrome"
    )
    assert (
        serialize_special_needs(SpecialNeeds(fixed={"Autism", "other"}))
        == "Autism; other"
    )
    assert (
        serialize_special_needs(SpecialNeeds(fixed={"other"}, other="delay"))
        == "other: delay"
    )
    # "other" stays last even when sorted fixed text would outrank it.
    assert (
        serialize_special_needs(
            SpecialNeeds(fixed={"Zebra", "other"}, other="x")
        )
        == "Zebra; other: x"
    )
    assert serialize_special_needs(SpecialNeeds()) is None


@pytest.mark.parametrize("text", ["ADHD", "Autism; Down Syndrome"])
def test_special_needs_round_trip(text: str) -> None:
    """parse(serialize(parse(x))) is stable for fixed-only values."""
    parsed = parse_special_needs(text)
    assert parse_special_needs(serialize_special_needs(parsed)) == parsed


def test_special_needs_parse_does_not_reselect_other() -> None:
    """Legacy quirk kept byte-identical.

    Parsing "other: x" fills the custom text but leaves the "other"
    piece unselected, so a plain serialize drops the custom text until
    the piece is selected.
    """
    parsed = parse_special_needs("ADHD; other: speech delay")
    assert parsed.fixed == {"ADHD"}
    assert parsed.other == "speech delay"
    assert serialize_special_needs(parsed) == "ADHD"
    assert (
        serialize_special_needs(
            SpecialNeeds(fixed={"ADHD", "other"}, other=parsed.other)
        )
        == "ADHD; other: speech delay"
    )


# ---------------------------------------------------------------------------
# Reconciliation service.
# ---------------------------------------------------------------------------


def test_reconcile_counts_appended_and_unchanged_rows(cfg: Config) -> None:
    """A new prepared child appends; existing rows are never merged."""
    from test_audit_store import (
        default_raw_children,
        seed_final_sheets,
        seed_raw,
    )

    seed_final_sheets(cfg)
    added = dict(default_raw_children()[0])
    added["zite_record_id"] = "c9"
    added["first_name"] = "Newest"
    seed_raw(cfg, children=[*default_raw_children(), added])

    result = reconcile(cfg)

    assert result.counts.appended == {
        "sponsor": 0,
        "family": 0,
        "child": 1,
    }
    assert result.counts.merged == {"sponsor": 0, "family": 0, "child": 0}
    assert "c9" in {row["zite_record_id"] for row in read_final(cfg, "child")}


# ---------------------------------------------------------------------------
# Status evaluator wrapper (D11).
# ---------------------------------------------------------------------------


def test_status_of_row_ignores_before_audited(cfg: Config) -> None:
    """The API wrapper keeps the unified precedence (D11)."""
    baseline = {"need_1": "raw"}
    row = {"status": STATUS_AUDITED, "ignore": True, "need_1": "raw"}
    assert status_of_row(cfg, "child", row, baseline) == STATUS_IGNORED


# ---------------------------------------------------------------------------
# Pipeline wrappers.
# ---------------------------------------------------------------------------


def test_acquire_wrapper_counts_raw_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acquire service reports per-table raw row counts."""
    from ccat import acquire as acquire_module
    from test_acquire import _acquire_cfg, _scenario_frames

    cfg = _acquire_cfg(tmp_path)
    frames = _scenario_frames()
    monkeypatch.setattr(
        acquire_module.zite,
        "get_table_data",
        lambda ref: frames[ref.split("::")[1]],
    )

    result = pipeline.acquire(cfg)

    assert result.raw_rows == {"sponsor": 2, "family": 2, "child": 4}
    assert result.workbook == str(cfg.excel_file)
    assert Path(result.workbook).is_file()


def test_match_wrapper_reports_clean_assignment(cfg: Config) -> None:
    """A clean exact-count match fills every DTO count."""
    from test_match import (
        _seed_match_workbook,
        child_row,
        family_row,
        sponsor_row,
    )

    _seed_match_workbook(
        cfg,
        [sponsor_row(1, num_children=2), sponsor_row(2, num_children=1)],
        [family_row(1), family_row(2)],
        [
            child_row("1A", 1, 10),
            child_row("1B", 1, 8, gender="Female"),
            child_row("2A", 2, 5),
        ],
    )

    result = pipeline.match(cfg)

    assert result.matched_families == 2
    assert result.matched_sponsors == 2
    assert result.assigned_angel_ids == ["1A", "1B", "2A"]
    assert result.unmatched_family_ids == []
    assert result.unmatched_sponsor_ids == []
    assert result.report_rows == 0
    assert result.archived is True
    assert result.csv_path == str(cfg.match_report_csv)
    assert Path(result.csv_path).is_file()


def test_match_wrapper_reports_unmatched_and_report_rows(
    cfg: Config,
) -> None:
    """A family without audited children stays unmatched and reported."""
    from test_match import (
        _seed_match_workbook,
        child_row,
        family_row,
        sponsor_row,
    )

    _seed_match_workbook(
        cfg,
        [sponsor_row(1, num_children=1)],
        [family_row(1), family_row(2)],
        [child_row("1A", 1, 8)],
    )

    result = pipeline.match(cfg)

    assert result.matched_families == 1
    assert result.matched_sponsors == 1
    assert result.assigned_angel_ids == ["1A"]
    assert result.unmatched_family_ids == [2]
    assert result.report_rows >= 1


def test_match_wrapper_split_family_is_not_unmatched(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A family whose children span two sponsors is split, not unmatched."""
    from ccat import match as match_module
    from test_match import (
        _seed_match_workbook,
        child_row,
        family_row,
        sponsor_row,
    )

    _seed_match_workbook(
        cfg,
        [sponsor_row(1, num_children=1), sponsor_row(2, num_children=1)],
        [family_row(1), family_row(2)],
        [
            {**child_row("1A", 1, 8), "assigned_sponsor_id": 1},
            {**child_row("1B", 1, 8), "assigned_sponsor_id": 2},
            child_row("2A", 2, 8),
        ],
    )
    monkeypatch.setattr(match_module, "run", lambda *a, **k: None)

    result = pipeline.match(cfg)

    assert result.matched_families == 0
    # Family 1 has placed children, so it is not "unassigned"; family 2
    # has none.
    assert result.unmatched_family_ids == [2]
    assert result.split_family_ids == [1]


def test_screen_wrapper_reports_targets_and_reconcile_counts(
    screen_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first screening run reports AI targets, successes, and appends."""
    from ccat import screening as screening_module
    from test_screening import (
        KeyedFakeOpencode,
        _seed_workbook,
        _use_keyed_opencode,
        child,
    )

    _seed_workbook(
        screen_cfg,
        children=[
            child(zite_record_id="f1c1", angel_id="1A"),
            child(zite_record_id="f1c2", first_name="Two", angel_id="1B"),
        ],
    )
    KeyedFakeOpencode.responses = {"f1c1": "null", "f1c2": "null"}
    _use_keyed_opencode(monkeypatch)
    monkeypatch.setattr(screening_module, "check_emails", lambda cfg, rows: [])
    monkeypatch.setattr(
        screening_module, "check_addresses", lambda cfg, rows: []
    )

    result = pipeline.screen(screen_cfg)

    assert sorted(result.ai_targets) == ["f1c1", "f1c2"]
    assert sorted(result.ai_succeeded) == ["f1c1", "f1c2"]
    assert result.ai_failed == []
    assert result.email_checked == 0
    assert result.address_checked == 0
    assert result.counts.appended == {"sponsor": 1, "family": 1, "child": 2}
    assert result.counts.merged == {"sponsor": 0, "family": 0, "child": 0}
    # The two same-dob siblings produce one DOB-conflict snapshot row.
    assert result.diagnostics_rows == 1


class TestMatchesQuery:
    """All-text audit search: substring everywhere, fuzzy token fallback."""

    ROW = {
        "zite_record_id": "c1",
        "first_name": "Kiddo",
        "last_name": "Lee",
        "primary_email": "fam@example.com",
        "other_notes": "loves trains and dinosaurs",
        "_row_version": 3,
    }

    def test_substring_hits_any_text_column(self) -> None:
        """Any text column matches by substring."""
        from ccat.services.db_audit import matches_query

        assert matches_query(self.ROW, "trains")
        assert matches_query(self.ROW, "lee")
        assert matches_query(self.ROW, "fam@example.com")

    def test_metadata_columns_are_not_searched(self) -> None:
        """Internal ``_``-prefixed columns are excluded."""
        from ccat.services.db_audit import matches_query

        assert not matches_query(self.ROW, "_row_version")

    def test_fuzzy_token_fallback(self) -> None:
        """Near-miss tokens match via the similarity fallback."""
        from ccat.services.db_audit import matches_query

        # typo of "trains": close enough under the 0.75 token ratio
        assert matches_query(self.ROW, "trins")
        assert matches_query(self.ROW, "kiddo".replace("d", "t", 1))

    def test_miss(self) -> None:
        """Unrelated queries fail; empty query matches everything."""
        from ccat.services.db_audit import matches_query

        assert not matches_query(self.ROW, "zebra")
        assert matches_query(self.ROW, "")
