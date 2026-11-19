"""import batches table for the workbook importer (plan §10).

Revision ID: 20261119_0002
Revises: 20261118_0001
Create Date: 2026-11-19

Adds ``import_batches``: one row per ``ccat db-import`` attempt, keyed
to the imported source file's SHA-256.  The partial unique index lets a
re-run of the exact same source reuse its completed batch while failed
or staged attempts never block a retry.  Deliberately NOT part of
``ccat.db.models`` metadata: the table is migration bookkeeping owned
by the importer (raw SQL), so the ORM contract tests stay unchanged.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20261119_0002"
down_revision: str | None = "20261118_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create import_batches and its completed-batch unique index."""
    op.create_table(
        "import_batches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("season_id", sa.Integer(), nullable=False),
        sa.Column("source_sha256", sa.Text(), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=True),
        sa.Column("config_hash", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("report", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column("actor", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["season_id"],
            ["seasons.id"],
            name=op.f("fk_import_batches_season_id_seasons"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_batches")),
    )
    op.create_index(
        "uq_import_batches_season_source_completed",
        "import_batches",
        ["season_id", "source_sha256"],
        unique=True,
        sqlite_where=sa.text("status = 'completed'"),
        postgresql_where=sa.text("status = 'completed'"),
    )


def downgrade() -> None:
    """Drop the import_batches table and its index."""
    op.drop_index(
        "uq_import_batches_season_source_completed",
        table_name="import_batches",
    )
    op.drop_table("import_batches")
