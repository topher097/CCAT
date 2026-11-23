"""record which row carries a duplicate group's merged entry.

Revision ID: 20261123_0010
Revises: 20261122_0009
Create Date: 2026-11-23

The Duplications section folds a group's submissions into one merged
entry and lets the auditor choose which member's row carries it.  The
choice has to survive the audit: without it, a later read of the group
would bind to the surviving member's row instead of the row the
adjudication landed on.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20261123_0010"
down_revision: str | None = "20261122_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the group's entry key to the duplication-group state table."""
    with op.batch_alter_table("duplication_state", schema=None) as batch_op:
        batch_op.add_column(sa.Column("entry_key", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the group's entry key."""
    with op.batch_alter_table("duplication_state", schema=None) as batch_op:
        batch_op.drop_column("entry_key")
