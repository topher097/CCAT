"""Tests for the sponsors' Assigned Angel ID claims.

A sponsor's ``prior_assigned_angel_ids`` names the children it claims.
The claim is honoured when the data is loaded: it is placed on the child,
so the audit stage sees it.  A claim another sponsor also makes waits
for the Duplications section, and a claim that names no child leaves a
clarification note for the audit stage to fix.
"""

from typing import Any

from ccat.audit.store import ANGEL_CLAIM_NOTE
from ccat.services.db_assignments import apply_angel_claims


def _sponsor(sponsor_id: int, claim: str | None, **overrides: Any) -> dict:
    """One final-sponsor row carrying a claim."""
    row: dict[str, Any] = {
        "sponsor_id": sponsor_id,
        "zite_record_id": f"rec_s{sponsor_id}",
        "email": f"sponsor{sponsor_id}@example.com",
        "prior_assigned_angel_ids": claim,
        "status": "new",
        "ignore": False,
        "clarification_notes": None,
        "assigned_sponsor_id": None,
        "assigned_angel_ids": None,
        "num_angels_assigned": 0,
    }
    row.update(overrides)
    return row


def _child(angel_id: str | None, **overrides: Any) -> dict:
    """One final-child row carrying an angel id."""
    row: dict[str, Any] = {
        "zite_record_id": f"rec_c{angel_id}",
        "angel_id": angel_id,
        "family_id": 1,
        "assigned_sponsor_id": None,
        "assignment_source": None,
        "ignore": False,
        "status": "new",
    }
    row.update(overrides)
    return row


def test_an_uncontested_claim_is_placed_on_the_child() -> None:
    """The claiming sponsor gets the child the claim names."""
    sponsors = [_sponsor(1, "12A")]
    children = [_child("12A")]
    unusable = apply_angel_claims(sponsors, children)
    assert unusable == []
    assert children[0]["assigned_sponsor_id"] == 1
    assert children[0]["assignment_source"] == "auto"


def test_a_claim_naming_no_child_leaves_a_note() -> None:
    """The sponsor cannot be given the child, so the audit must fix it."""
    sponsors = [_sponsor(1, "99Z")]
    children = [_child("12A")]
    unusable = apply_angel_claims(sponsors, children)
    assert unusable == ["99Z"]
    assert sponsors[0]["clarification_notes"] == ANGEL_CLAIM_NOTE.format(
        angel_id="99Z"
    )
    assert sponsors[0]["status"] == "needs_clarification"


def test_a_contested_claim_is_left_to_the_duplications_section() -> None:
    """Two sponsors claiming one child place nothing and note nothing."""
    sponsors = [_sponsor(1, "12A"), _sponsor(2, "12A")]
    children = [_child("12A")]
    unusable = apply_angel_claims(sponsors, children)
    assert unusable == []
    assert children[0]["assigned_sponsor_id"] is None
    assert sponsors[0]["clarification_notes"] is None
    assert sponsors[1]["clarification_notes"] is None


def test_a_contested_claim_on_no_child_is_not_noted() -> None:
    """The claim is another sponsor's duplicate, so no note is raised."""
    sponsors = [_sponsor(1, "99Z"), _sponsor(2, "99Z")]
    children = [_child("12A")]
    unusable = apply_angel_claims(sponsors, children)
    assert unusable == []
    assert sponsors[0]["clarification_notes"] is None
    assert sponsors[1]["clarification_notes"] is None


def test_a_placed_child_keeps_its_placement() -> None:
    """The claim is a first placement, not a move."""
    sponsors = [_sponsor(1, "12A")]
    children = [
        _child("12A", assigned_sponsor_id=9, assignment_source="manual")
    ]
    apply_angel_claims(sponsors, children)
    assert children[0]["assigned_sponsor_id"] == 9
    assert children[0]["assignment_source"] == "manual"


def test_an_ignored_sponsor_claims_nothing() -> None:
    """An ignored sponsor is out of the pool, so its claim does not count."""
    sponsors = [_sponsor(1, "12A", ignore=True)]
    children = [_child("12A")]
    apply_angel_claims(sponsors, children)
    assert children[0]["assigned_sponsor_id"] is None
    assert sponsors[0]["clarification_notes"] is None


def test_an_ignored_child_is_not_claimed() -> None:
    """An ignored child holds no claimable angel."""
    sponsors = [_sponsor(1, "12A")]
    children = [_child("12A", ignore=True)]
    unusable = apply_angel_claims(sponsors, children)
    assert unusable == ["12A"]
    assert sponsors[0]["clarification_notes"] is not None


def test_a_claim_list_places_every_claimed_child() -> None:
    """A sponsor may claim several children."""
    sponsors = [_sponsor(1, "12A, 12B")]
    children = [_child("12A"), _child("12B")]
    apply_angel_claims(sponsors, children)
    assert children[0]["assigned_sponsor_id"] == 1
    assert children[1]["assigned_sponsor_id"] == 1


def test_the_note_is_appended_once() -> None:
    """Re-running the screen does not pile the same note up."""
    sponsors = [_sponsor(1, "99Z")]
    children = [_child("12A")]
    apply_angel_claims(sponsors, children)
    apply_angel_claims(sponsors, children)
    assert sponsors[0]["clarification_notes"] == ANGEL_CLAIM_NOTE.format(
        angel_id="99Z"
    )
