"""add the sponsor packet draft and the send-attempt log.

Revision ID: 20261122_0009
Revises: 20261121_0008
Create Date: 2026-11-22

Two tables for the dispatch page:

- ``sponsor_drafts`` -- one row per (season, sponsor): the current
  editable email (subject, generated body, note, recipient, BCC list,
  attachments) with a version for optimistic concurrency.  It holds no
  delivery state.
- ``sponsor_send_attempts`` -- one row per send attempt, with the state
  machine ``sending`` -> ``sent`` | ``failed`` | ``unknown``.  The
  partial unique index makes "one send in flight per sponsor" a database
  rule.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20261122_0009"
down_revision: str | None = "20261121_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the sponsor draft and send-attempt tables."""
    op.create_table(
        "sponsor_drafts",
        sa.Column("season_id", sa.Integer(), nullable=False),
        sa.Column("sponsor_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "version", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("to_email", sa.Text(), nullable=True),
        sa.Column("bcc_emails", sa.JSON(), nullable=True),
        sa.Column("attachments", sa.JSON(), nullable=True),
        sa.Column("build_id", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("edited_by", sa.Text(), nullable=True),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["season_id"], ["seasons.id"]),
        sa.PrimaryKeyConstraint("season_id", "sponsor_id"),
    )
    op.create_index(
        "ix_sponsor_drafts_season_sponsor",
        "sponsor_drafts",
        ["season_id", "sponsor_id"],
    )
    op.create_table(
        "sponsor_send_attempts",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("season_id", sa.Integer(), nullable=False),
        sa.Column("sponsor_id", sa.BigInteger(), nullable=False),
        sa.Column("draft_version", sa.Integer(), nullable=True),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'sending'"),
        ),
        sa.Column("message_id", sa.Text(), nullable=True),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column("to_email", sa.Text(), nullable=True),
        sa.Column("bcc_emails", sa.JSON(), nullable=True),
        sa.Column("attachments", sa.JSON(), nullable=True),
        sa.Column("actor", sa.Text(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["season_id"], ["seasons.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_sponsor_send_attempts_season_sponsor",
        "sponsor_send_attempts",
        ["season_id", "sponsor_id"],
    )
    op.create_index(
        "uq_sponsor_send_attempts_active",
        "sponsor_send_attempts",
        ["season_id", "sponsor_id"],
        unique=True,
        sqlite_where=sa.text("state = 'sending'"),
        postgresql_where=sa.text("state = 'sending'"),
    )


def downgrade() -> None:
    """Drop the sponsor draft and send-attempt tables."""
    op.drop_index(
        "uq_sponsor_send_attempts_active",
        table_name="sponsor_send_attempts",
    )
    op.drop_index(
        "ix_sponsor_send_attempts_season_sponsor",
        table_name="sponsor_send_attempts",
    )
    op.drop_table("sponsor_send_attempts")
    op.drop_index(
        "ix_sponsor_drafts_season_sponsor", table_name="sponsor_drafts"
    )
    op.drop_table("sponsor_drafts")
