"""add htx funding arbitrage tables

Revision ID: 20260530_0002
Revises: 20260530_0001
Create Date: 2026-05-30 00:02:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260530_0002"
down_revision = "20260530_0001"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _has_index(table_name: str, index_name: str) -> bool:
    return any(index.get("name") == index_name for index in sa.inspect(op.get_bind()).get_indexes(table_name))


def _create_index_once(index_name: str, table_name: str, columns: list[str]) -> None:
    if _has_table(table_name) and not _has_index(table_name, index_name):
        op.create_index(index_name, table_name, columns)


def _drop_index_if_exists(index_name: str, table_name: str) -> None:
    if _has_table(table_name) and _has_index(table_name, index_name):
        op.drop_index(index_name, table_name=table_name)


def upgrade() -> None:
    if not _has_table("funding_arb_opportunities"):
        op.create_table(
            "funding_arb_opportunities",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("symbol", sa.String(length=50), nullable=False),
            sa.Column("spot_symbol", sa.String(length=50), nullable=False),
            sa.Column("swap_symbol", sa.String(length=80), nullable=False),
            sa.Column("funding_rate", sa.Float(), nullable=False),
            sa.Column("annualized_rate_pct", sa.Float(), nullable=False),
            sa.Column("spot_price", sa.Float(), nullable=False),
            sa.Column("swap_price", sa.Float(), nullable=False),
            sa.Column("basis_pct", sa.Float(), nullable=False),
            sa.Column("estimated_edge_pct", sa.Float(), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column("next_funding_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("raw_json", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        )
    _create_index_once("ix_funding_arb_opportunities_symbol", "funding_arb_opportunities", ["symbol"])
    _create_index_once("ix_funding_arb_opportunities_status", "funding_arb_opportunities", ["status"])
    _create_index_once("ix_funding_arb_opportunities_created_at", "funding_arb_opportunities", ["created_at"])

    if not _has_table("funding_arb_positions"):
        op.create_table(
            "funding_arb_positions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("opportunity_id", sa.Integer(), sa.ForeignKey("funding_arb_opportunities.id"), nullable=True),
            sa.Column("symbol", sa.String(length=50), nullable=False),
            sa.Column("spot_symbol", sa.String(length=50), nullable=False),
            sa.Column("swap_symbol", sa.String(length=80), nullable=False),
            sa.Column("mode", sa.String(length=20), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column("hedge_side", sa.String(length=40), nullable=False),
            sa.Column("notional_usdt", sa.Float(), nullable=False),
            sa.Column("spot_qty", sa.Float(), nullable=False),
            sa.Column("swap_qty", sa.Float(), nullable=False),
            sa.Column("spot_entry_price", sa.Float(), nullable=False),
            sa.Column("swap_entry_price", sa.Float(), nullable=False),
            sa.Column("spot_exit_price", sa.Float(), nullable=True),
            sa.Column("swap_exit_price", sa.Float(), nullable=True),
            sa.Column("entry_funding_rate", sa.Float(), nullable=False),
            sa.Column("exit_funding_rate", sa.Float(), nullable=True),
            sa.Column("funding_periods", sa.Integer(), nullable=False),
            sa.Column("funding_collected", sa.Float(), nullable=False),
            sa.Column("fees_paid", sa.Float(), nullable=False),
            sa.Column("realized_pnl", sa.Float(), nullable=True),
            sa.Column("raw_json", sa.JSON(), nullable=True),
            sa.Column("opened_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        )
    _create_index_once("ix_funding_arb_positions_opportunity_id", "funding_arb_positions", ["opportunity_id"])
    _create_index_once("ix_funding_arb_positions_symbol", "funding_arb_positions", ["symbol"])
    _create_index_once("ix_funding_arb_positions_mode", "funding_arb_positions", ["mode"])
    _create_index_once("ix_funding_arb_positions_status", "funding_arb_positions", ["status"])
    _create_index_once("ix_funding_arb_positions_opened_at", "funding_arb_positions", ["opened_at"])


def downgrade() -> None:
    for index_name, table_name in [
        ("ix_funding_arb_positions_opened_at", "funding_arb_positions"),
        ("ix_funding_arb_positions_status", "funding_arb_positions"),
        ("ix_funding_arb_positions_mode", "funding_arb_positions"),
        ("ix_funding_arb_positions_symbol", "funding_arb_positions"),
        ("ix_funding_arb_positions_opportunity_id", "funding_arb_positions"),
        ("ix_funding_arb_opportunities_created_at", "funding_arb_opportunities"),
        ("ix_funding_arb_opportunities_status", "funding_arb_opportunities"),
        ("ix_funding_arb_opportunities_symbol", "funding_arb_opportunities"),
    ]:
        _drop_index_if_exists(index_name, table_name)
    if _has_table("funding_arb_positions"):
        op.drop_table("funding_arb_positions")
    if _has_table("funding_arb_opportunities"):
        op.drop_table("funding_arb_opportunities")
