"""add the duplication-audit table and share pending angel letters.

Revision ID: 20261120_0004
Revises: 20261119_0003
Create Date: 2026-11-20

The Duplications section of the audit tool records every human decision
about a detected duplicate group, so the decisions live in the database
next to the stores they change:

- ``duplication_audit`` is an append-only trail.  One row per decision
  (``not_duplicate`` or ``ignore``), naming the duplicate group's sheet
  and key, the audited submission, the actor, and the unique id the
  decision assigned.

``final_child.angel_id`` loses its uniqueness.  A child that is still a
pending duplicate shares its group's angel letter until the duplication
audit resolves it, so two rows can carry one letter for a while; the
letter becomes unique again as soon as the member is marked "not a
duplicate" and receives a letter of its own.  The old
``uq_final_child_angel`` partial unique index is replaced by a plain
lookup index.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20261120_0004"
down_revision: str | None = "20261119_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the duplication-audit trail and relax the angel index."""
    op.create_table(
        "duplication_audit",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("season_id", sa.Integer(), nullable=False),
        sa.Column("sheet", sa.Text(), nullable=False),
        sa.Column("group_key", sa.Text(), nullable=False),
        sa.Column("zite_record_id", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("assigned_id", sa.Text(), nullable=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["season_id"],
            ["seasons.id"],
            name=op.f("fk_duplication_audit_season_id_seasons"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_duplication_audit")),
    )
    with op.batch_alter_table("duplication_audit", schema=None) as batch_op:
        batch_op.create_index(
            "ix_duplication_audit_season_group",
            ["season_id", "sheet", "group_key"],
            unique=False,
        )
    op.drop_index("uq_final_child_angel", table_name="final_child")
    op.create_index(
        "ix_final_child_season_angel",
        "final_child",
        ["season_id", "angel_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the duplication-audit trail and restore the unique index."""
    op.drop_index("ix_final_child_season_angel", table_name="final_child")
    op.create_index(
        "uq_final_child_angel",
        "final_child",
        ["season_id", "angel_id"],
        unique=True,
        sqlite_where=sa.text("angel_id IS NOT NULL"),
        postgresql_where=sa.text("angel_id IS NOT NULL"),
    )
    with op.batch_alter_table("duplication_audit", schema=None) as batch_op:
        batch_op.drop_index("ix_duplication_audit_season_group")
    op.drop_table("duplication_audit")
