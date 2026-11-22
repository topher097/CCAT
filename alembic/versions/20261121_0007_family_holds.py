"""add the family-hold table.

Revision ID: 20261121_0007
Revises: 20261121_0006
Create Date: 2026-11-21

The automatic matcher has no way to express "we looked at this family
and it is not a fit right now".  A hold does: the family leaves the
automatic matcher's pool without being placed, and it carries the note
saying why.  The note is required -- a hold without a reason is not a
decision, it is a stall.

A hold is matching state, not a final-entity attribute, so it does not
live in the ``final_*`` stores and does not round-trip through the
workbook export.  It is the one exception to "no new table for
assignments".

The partial unique index gives at most one ACTIVE hold per family; a
released hold keeps its row and note, because the decision is history.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20261121_0007"
down_revision: str | None = "20261121_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the family-hold table."""
    op.create_table(
        "family_holds",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("season_id", sa.Integer(), nullable=False),
        sa.Column("family_id", sa.BigInteger(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_by", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["season_id"],
            ["seasons.id"],
            name=op.f("fk_family_holds_season_id_seasons"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_family_holds")),
    )
    with op.batch_alter_table("family_holds", schema=None) as batch_op:
        batch_op.create_index(
            "ix_family_holds_season_family",
            ["season_id", "family_id"],
            unique=False,
        )
        batch_op.create_index(
            "uq_family_holds_active",
            ["season_id", "family_id"],
            unique=True,
            sqlite_where=sa.text("released_at IS NULL"),
            postgresql_where=sa.text("released_at IS NULL"),
        )


def downgrade() -> None:
    """Drop the family-hold table."""
    with op.batch_alter_table("family_holds", schema=None) as batch_op:
        batch_op.drop_index("uq_family_holds_active")
        batch_op.drop_index("ix_family_holds_season_family")
    op.drop_table("family_holds")
