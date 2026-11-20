"""retire the cmendres400 dev account (plan §8 accounts).

Revision ID: 20261119_0003
Revises: 20261119_0002
Create Date: 2026-11-19

The development auto-login identity is ``cmendres`` again; the temporary
``cmendres400`` bootstrap account is retired.  Its security-audit rows
go with it: they carry the retired user's id as a foreign key, so the
event rows are removed before the account itself.  Nothing else in the
schema changes, and the downgrade restores the account (audit rows are
not recreated — the trail for a retired account is gone for good).
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20261119_0003"
down_revision: str | None = "20261119_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The retired development account.
RETIRED_USERNAME = "cmendres400"


def upgrade() -> None:
    """Remove the retired account and the audit rows that reference it."""
    connection = op.get_bind()
    user_id = connection.scalar(
        sa.text("SELECT id FROM users WHERE username = :username"),
        {"username": RETIRED_USERNAME},
    )
    if user_id is None:
        return
    connection.execute(
        sa.text("DELETE FROM security_audit_events WHERE user_id = :user_id"),
        {"user_id": user_id},
    )
    connection.execute(
        sa.text("DELETE FROM sessions WHERE user_id = :user_id"),
        {"user_id": user_id},
    )
    connection.execute(
        sa.text("DELETE FROM users WHERE id = :user_id"),
        {"user_id": user_id},
    )


def downgrade() -> None:
    """Restore the retired account (audit rows stay deleted)."""
    connection = op.get_bind()
    exists = connection.scalar(
        sa.text("SELECT id FROM users WHERE username = :username"),
        {"username": RETIRED_USERNAME},
    )
    if exists is not None:
        return
    connection.execute(
        sa.text(
            "INSERT INTO users (username, display_name, active, roles,"
            " dev_no_password, otp_enrolled)"
            " VALUES (:username, NULL, 1, :roles, 1, 0)"
        ),
        {
            "username": RETIRED_USERNAME,
            "roles": '["admin", "coordinator", "auditor"]',
        },
    )
