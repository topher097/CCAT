"""make the child row the canonical assignment grain.

Revision ID: 20261121_0006
Revises: 20261120_0005
Create Date: 2026-11-21

Every downstream consumer resolved a child's sponsor by joining the
family row, so "these two of a family's four children go to one
sponsor and the other two to another" could not be represented.  The
child row now carries the assignment:

- ``final_child.assigned_sponsor_id`` -- the child's sponsor.
- ``final_child.assignment_source`` -- ``auto`` or ``manual``; a manual
  assignment is protected from the automatic matcher.

The family and sponsor assignment columns become derived summaries.

The data migration is the risky part of this revision: it reconstructs
child-grain assignments from the family-grain columns, including the
partial pre-assignment case, where a family points at a sponsor but
names only some of its children.  The backfill is idempotent (it only
writes rows whose sponsor is still NULL) and lives in the same
revision as the schema change so the two can never disagree.

The downgrade is lossy: split assignments collapse back into the
family-level columns.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20261121_0006"
down_revision: str | None = "20261120_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _parse_ids(value: str | None) -> set[str]:
    """Parse a comma-joined angel id list.

    Args:
        value: The stored comma-joined text.

    Returns:
        The parsed ids, uppercased and stripped.
    """
    if not value:
        return set()
    return {
        token.strip().upper()
        for token in str(value).replace(";", ",").split(",")
        if token.strip()
    }


def _backfill() -> None:
    """Reconstruct child-grain assignments from the summary columns."""
    bind = op.get_bind()
    children = (
        bind.execute(
            sa.text(
                "SELECT season_id, zite_record_id, family_id, angel_id, ignore"
                " FROM final_child"
            )
        )
        .mappings()
        .all()
    )
    families = (
        bind.execute(
            sa.text(
                "SELECT season_id, family_id, assigned_sponsor_id,"
                " assigned_angel_ids FROM final_family"
            )
        )
        .mappings()
        .all()
    )
    sponsors = (
        bind.execute(
            sa.text(
                "SELECT season_id, sponsor_id, assigned_angel_ids"
                " FROM final_sponsor"
            )
        )
        .mappings()
        .all()
    )

    children_by_family: dict[tuple[int, int], list[dict]] = {}
    for child in children:
        if child["ignore"] or child["family_id"] is None:
            continue
        key = (int(child["season_id"]), int(child["family_id"]))
        children_by_family.setdefault(key, []).append(child)

    assignments: dict[tuple[int, str], int] = {}

    # The family column is the authority: a family names the children
    # it placed through its assigned_angel_ids list.  A list shorter
    # than the family means a partial pre-assignment -- only the named
    # children are placed.
    for family in families:
        sponsor_id = family["assigned_sponsor_id"]
        if sponsor_id is None:
            continue
        key = (int(family["season_id"]), int(family["family_id"]))
        family_children = children_by_family.get(key, [])
        named = _parse_ids(family["assigned_angel_ids"])
        if named:
            targets = [
                child for child in family_children if child["angel_id"] in named
            ]
        else:
            targets = list(family_children)
        for child in targets:
            child_key = (int(child["season_id"]), str(child["zite_record_id"]))
            assignments[child_key] = int(sponsor_id)

    # The sponsor column names the children it holds.  A child the
    # family pass already placed keeps the family's sponsor.
    for sponsor in sponsors:
        named = _parse_ids(sponsor["assigned_angel_ids"])
        if not named:
            continue
        for child in children:
            if child["ignore"] or child["angel_id"] not in named:
                continue
            child_key = (int(child["season_id"]), str(child["zite_record_id"]))
            assignments.setdefault(child_key, int(sponsor["sponsor_id"]))

    for (season_id, zite_record_id), sponsor_id in assignments.items():
        bind.execute(
            sa.text(
                "UPDATE final_child SET assigned_sponsor_id = :sponsor,"
                " assignment_source = 'auto' WHERE season_id = :season"
                " AND zite_record_id = :key AND assigned_sponsor_id IS NULL"
            ),
            {
                "sponsor": sponsor_id,
                "season": season_id,
                "key": zite_record_id,
            },
        )


def upgrade() -> None:
    """Add the child's assignment columns and backfill them."""
    op.add_column(
        "final_child",
        sa.Column("assigned_sponsor_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "final_child",
        sa.Column("assignment_source", sa.String(), nullable=True),
    )
    with op.batch_alter_table("final_child", schema=None) as batch_op:
        batch_op.create_index(
            "ix_final_child_season_sponsor",
            ["season_id", "assigned_sponsor_id"],
            unique=False,
        )
    _backfill()


def downgrade() -> None:
    """Drop the child's assignment columns (lossy for split families)."""
    with op.batch_alter_table("final_child", schema=None) as batch_op:
        batch_op.drop_index("ix_final_child_season_sponsor")
    op.drop_column("final_child", "assignment_source")
    op.drop_column("final_child", "assigned_sponsor_id")
