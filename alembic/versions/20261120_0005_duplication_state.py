"""add the duplication-group state table.

Revision ID: 20261120_0005
Revises: 20261120_0004
Create Date: 2026-11-20

The Duplications section adjudicates a duplicate group as a whole: every
member is resolved, then the group is marked audited.  The per-member
decisions live in ``duplication_audit``; this table holds the group's
own state, so "marked audited" survives and stays the flag that the
affected ``final_*`` tables were reconciled with the adjudication.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20261120_0005"
down_revision: str | None = "20261120_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the duplication-group state table."""
    op.create_table(
        "duplication_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("season_id", sa.Integer(), nullable=False),
        sa.Column("sheet", sa.Text(), nullable=False),
        sa.Column("group_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("audited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("audited_by", sa.Text(), nullable=True),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["season_id"],
            ["seasons.id"],
            name=op.f("fk_duplication_state_season_id_seasons"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_duplication_state")),
        sa.UniqueConstraint(
            "season_id",
            "sheet",
            "group_key",
            name="uq_duplication_state_group",
        ),
    )
    with op.batch_alter_table("duplication_state", schema=None) as batch_op:
        batch_op.create_index(
            "ix_duplication_state_season_status",
            ["season_id", "status"],
            unique=False,
        )


def downgrade() -> None:
    """Drop the duplication-group state table."""
    with op.batch_alter_table("duplication_state", schema=None) as batch_op:
        batch_op.drop_index("ix_duplication_state_season_status")
    op.drop_table("duplication_state")
