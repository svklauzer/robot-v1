"""add signals.exchange column (which exchange a signal actually opened on)

Revision ID: 20260902_0004
Revises: 20260531_0003
Create Date: 2026-09-02 00:04:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260902_0004"
down_revision = "20260531_0003"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return column_name in [c["name"] for c in inspector.get_columns(table_name)]


def upgrade() -> None:
    # server_default="htx" is not just a safe placeholder -- every row that
    # exists before this migration runs really was opened on HTX, since OKX
    # didn't exist until today.
    if not _has_column("signals", "exchange"):
        op.add_column(
            "signals",
            sa.Column("exchange", sa.String(length=10), nullable=False, server_default="htx"),
        )


def downgrade() -> None:
    if _has_column("signals", "exchange"):
        op.drop_column("signals", "exchange")
