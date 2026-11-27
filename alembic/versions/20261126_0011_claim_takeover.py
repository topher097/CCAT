"""settle contested angel claims by sponsor specificity.

Revision ID: 20261126_0011
Revises: 20261123_0010
Create Date: 2026-11-26

A sponsor's ``prior_assigned_angel_ids`` names the children it claims.
The program runs generic Walmart sponsors that hold the angel ids
allocated to a store, and a person who signs up at that store claims
the same id to take the slot over (angel_claims_plan §1).  That
takeover was never implemented: a contested claim placed nothing and
the matcher kept the earlier sponsor id, so the person who signed up
showed a free slot instead of the claimed child.

The rule, now shared by the screening claim pass and the matcher: one
specific sponsor against generic store placeholders takes the child
over, even from a placeholder the child is already placed with.  Two
specific claimants (or placeholders only) stay a contest the
Duplications section settles.

The data migration applies the same rule to an existing database:
contested claims with exactly one specific claimant move the child off
the placeholder (a manual placement or a specific sponsor's child is
never moved), then the derived family and sponsor assignment summaries
are recomputed from the children for every affected row.  The revision
is idempotent: once the child sits with the specific winner, a second
run finds nothing to move.

The downgrade is a no-op: moving the children back would undo the
takeover the rule itself performs on the next screening run.
"""

from collections.abc import Sequence
import re

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20261126_0011"
down_revision: str | None = "20261123_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The generic store placeholder affiliation (see db_assignments).
_PLACEHOLDER_TYPE = "Walmart"


def _parse_ids(value: str | None) -> list[str]:
    """Parse a comma-joined angel id list, normalized like the app does.

    Args:
        value: The stored comma-joined text.

    Returns:
        The parsed ids, uppercased and stripped.
    """
    if not value:
        return []
    return [
        token.strip().upper()
        for token in str(value).replace(";", ",").split(",")
        if token.strip()
    ]


def _angel_sort_key(angel_id: str) -> tuple[int, str]:
    """Sort key mirroring ccat.domain.angel_sort_key (natural id order).

    Args:
        angel_id: One angel id.

    Returns:
        The sort key: family number, then letter code.
    """
    match = re.fullmatch(r"(\d+)([A-Z]+)", angel_id)
    if match is None:
        return (10**9, angel_id)
    return (int(match.group(1)), match.group(2))


def _takeovers(
    sponsors: list[dict], children: list[dict]
) -> dict[tuple[int, str], int]:
    """Compute the contested-claim takeovers for one database pass.

    Args:
        sponsors: Every final_sponsor row (id, type, claims, ignore).
        children: Every final_child row (angel, placement, ignore).

    Returns:
        Mapping of (season_id, zite_record_id) -> the winning sponsor
        id for the children a specific claimant takes over.
    """
    sponsors_by_id = {
        (int(row["season_id"]), int(row["sponsor_id"])): row for row in sponsors
    }
    claims: dict[int, dict[str, list[dict]]] = {}
    for sponsor in sponsors:
        if sponsor["ignore"]:
            continue
        for angel_id in _parse_ids(sponsor["prior_assigned_angel_ids"]):
            claims.setdefault(int(sponsor["season_id"]), {}).setdefault(
                angel_id, []
            ).append(sponsor)
    children_by_angel: dict[int, dict[str, dict]] = {}
    for child in children:
        if child["ignore"] or not child["angel_id"]:
            continue
        children_by_angel.setdefault(int(child["season_id"]), {})[
            str(child["angel_id"]).upper()
        ] = child

    moves: dict[tuple[int, str], int] = {}
    for season_id, season_claims in claims.items():
        for angel_id, claimants in season_claims.items():
            if len(claimants) < 2:
                continue
            specific = [
                claimant
                for claimant in claimants
                if claimant["sponsor_type"] != _PLACEHOLDER_TYPE
            ]
            if len(specific) != 1:
                # Two specific claimants, or placeholders only: the
                # Duplications section settles it; nothing moves.
                continue
            winner = specific[0]
            child = children_by_angel.get(season_id, {}).get(angel_id)
            if child is None:
                continue
            if child["assignment_source"] == "manual":
                continue
            holder_id = child["assigned_sponsor_id"]
            if holder_id is not None:
                holder = sponsors_by_id.get((season_id, int(holder_id)))
                if (
                    holder is None
                    or holder["sponsor_type"] != _PLACEHOLDER_TYPE
                ):
                    # A specific sponsor keeps its child.
                    continue
            moves[(season_id, str(child["zite_record_id"]))] = int(
                winner["sponsor_id"]
            )
    return moves


def _recompute_summaries(
    bind: sa.Connection,
    season_family_ids: set[tuple[int, int]],
    season_sponsor_ids: set[tuple[int, int]],
) -> None:
    """Rebuild the derived assignment summaries from the children.

    Args:
        bind: The migration connection.
        season_family_ids: The (season, family_id) rows to recompute.
        season_sponsor_ids: The (season, sponsor_id) rows to recompute.
    """
    for season_id, family_id in sorted(season_family_ids):
        children = (
            bind.execute(
                sa.text(
                    "SELECT angel_id FROM final_child"
                    " WHERE season_id = :season AND family_id = :family"
                    " AND ignore IS NOT TRUE AND assigned_sponsor_id"
                    " IS NOT NULL"
                ),
                {"season": season_id, "family": family_id},
            )
            .mappings()
            .all()
        )
        rows = bind.execute(
            sa.text(
                "SELECT assigned_sponsor_id FROM final_child"
                " WHERE season_id = :season AND family_id = :family"
                " AND ignore IS NOT TRUE"
            ),
            {"season": season_id, "family": family_id},
        )
        sponsors = {int(row[0]) for row in rows if row[0] is not None}
        angel_ids = sorted(
            (str(row["angel_id"]) for row in children if row["angel_id"]),
            key=_angel_sort_key,
        )
        bind.execute(
            sa.text(
                "UPDATE final_family SET assigned_sponsor_id = :sponsor,"
                " assigned_angel_ids = :angels WHERE season_id = :season"
                " AND family_id = :family"
            ),
            {
                "sponsor": next(iter(sponsors)) if len(sponsors) == 1 else None,
                "angels": ",".join(angel_ids) if angel_ids else None,
                "season": season_id,
                "family": family_id,
            },
        )
    for season_id, sponsor_id in sorted(season_sponsor_ids):
        children = (
            bind.execute(
                sa.text(
                    "SELECT angel_id FROM final_child"
                    " WHERE season_id = :season AND assigned_sponsor_id"
                    " = :sponsor AND ignore IS NOT TRUE"
                ),
                {"season": season_id, "sponsor": sponsor_id},
            )
            .mappings()
            .all()
        )
        angel_ids = sorted(
            (str(row["angel_id"]) for row in children if row["angel_id"]),
            key=_angel_sort_key,
        )
        bind.execute(
            sa.text(
                "UPDATE final_sponsor SET assigned_angel_ids = :angels,"
                " num_angels_assigned = :count WHERE season_id = :season"
                " AND sponsor_id = :sponsor"
            ),
            {
                "angels": ",".join(angel_ids) if angel_ids else None,
                "count": len(children),
                "season": season_id,
                "sponsor": sponsor_id,
            },
        )


def _apply_takeovers() -> None:
    """Move contested children to their specific claimant and resummarize."""
    bind = op.get_bind()
    sponsors = [
        dict(row)
        for row in bind.execute(
            sa.text(
                "SELECT season_id, sponsor_id, sponsor_type,"
                " prior_assigned_angel_ids, ignore FROM final_sponsor"
            )
        ).mappings()
    ]
    children = [
        dict(row)
        for row in bind.execute(
            sa.text(
                "SELECT season_id, zite_record_id, family_id, angel_id,"
                " assigned_sponsor_id, assignment_source, ignore"
                " FROM final_child"
            )
        ).mappings()
    ]
    moves = _takeovers(sponsors, children)
    if not moves:
        return
    season_family_ids: set[tuple[int, int]] = set()
    season_sponsor_ids: set[tuple[int, int]] = set()
    by_key = {
        (int(child["season_id"]), str(child["zite_record_id"])): child
        for child in children
    }
    for (season_id, zite_record_id), winner_id in moves.items():
        child = by_key[(season_id, zite_record_id)]
        loser_id = child["assigned_sponsor_id"]
        bind.execute(
            sa.text(
                "UPDATE final_child SET assigned_sponsor_id = :sponsor,"
                " assignment_source = 'auto' WHERE season_id = :season"
                " AND zite_record_id = :key"
            ),
            {"sponsor": winner_id, "season": season_id, "key": zite_record_id},
        )
        season_sponsor_ids.add((season_id, winner_id))
        if loser_id is not None:
            season_sponsor_ids.add((season_id, int(loser_id)))
        if child["family_id"] is not None:
            season_family_ids.add((season_id, int(child["family_id"])))
    _recompute_summaries(bind, season_family_ids, season_sponsor_ids)


def upgrade() -> None:
    """Apply the claim-takeover rule to the stored assignments."""
    _apply_takeovers()


def downgrade() -> None:
    """No-op: the takeover is the rule, not a one-time correction."""
