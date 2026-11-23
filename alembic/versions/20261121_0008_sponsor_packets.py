"""add the sponsor budget/packet flags and the packet-dispatch stamp.

Revision ID: 20261121_0008
Revises: 20261121_0007
Create Date: 2026-11-21

Three nullable ``final_sponsor`` columns:

- ``elevated_budget`` / ``physical_packet`` -- auditor flags; NULL means
  "not decided".
- ``packets_dispatched_at`` -- ISO 8601 stamp written when the manual
  matcher dispatches the sponsor's info-sheet packets.  Once set, the
  sponsor's children are frozen.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20261121_0008"
down_revision: str | None = "20261121_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the sponsor flag and dispatch columns."""
    op.add_column(
        "final_sponsor",
        sa.Column("elevated_budget", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "final_sponsor",
        sa.Column("physical_packet", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "final_sponsor",
        sa.Column("packets_dispatched_at", sa.String(), nullable=True),
    )


def downgrade() -> None:
    """Drop the sponsor flag and dispatch columns."""
    op.drop_column("final_sponsor", "packets_dispatched_at")
    op.drop_column("final_sponsor", "physical_packet")
    op.drop_column("final_sponsor", "elevated_budget")
