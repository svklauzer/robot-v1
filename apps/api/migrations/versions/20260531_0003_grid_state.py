"""add grid_state table (persist smart-grid across redeploys)

Revision ID: 20260531_0003
Revises: 20260530_0002
Create Date: 2026-05-31 00:03:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260531_0003"
down_revision = "20260530_0002"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def upgrade() -> None:
    if not _has_table("grid_state"):
        op.create_table(
            "grid_state",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("realized_pnl", sa.Float(), nullable=False, server_default="0"),
            sa.Column("closed_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cycles", sa.JSON(), nullable=True),
            sa.Column("history", sa.JSON(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        )


def downgrade() -> None:
    if _has_table("grid_state"):
        op.drop_table("grid_state")
