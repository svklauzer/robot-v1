"""add operational billing telegram and audit domains

Revision ID: 20260530_0001
Revises: 
Create Date: 2026-05-30 00:01:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260530_0001"
down_revision = None
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _has_index(table_name: str, index_name: str) -> bool:
    return any(index.get("name") == index_name for index in sa.inspect(op.get_bind()).get_indexes(table_name))


def _create_index_once(index_name: str, table_name: str, columns: list[str], unique: bool = False) -> None:
    if _has_table(table_name) and not _has_index(table_name, index_name):
        op.create_index(index_name, table_name, columns, unique=unique)


def _drop_index_if_exists(index_name: str, table_name: str) -> None:
    if _has_table(table_name) and _has_index(table_name, index_name):
        op.drop_index(index_name, table_name=table_name)


def upgrade() -> None:
    if not _has_table("users"):
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("email", sa.String(length=255), nullable=False),
            sa.Column("password_hash", sa.String(length=255), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.UniqueConstraint("email", name="uq_users_email"),
        )
    _create_index_once("ix_users_email", "users", ["email"], unique=True)

    if not _has_table("bots"):
        op.create_table(
            "bots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column("mode", sa.String(length=20), nullable=False),
            sa.Column("config_json", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        )

    if not _has_table("subscribers"):
        op.create_table(
            "subscribers",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("telegram_user_id", sa.String(length=100), nullable=False),
            sa.Column("username", sa.String(length=100), nullable=True),
            sa.Column("full_name", sa.String(length=255), nullable=True),
            sa.Column("plan", sa.String(length=50), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column("starts_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("is_trial", sa.Boolean(), nullable=False),
            sa.Column("notes", sa.String(length=1000), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        )
    _create_index_once("ix_subscribers_telegram_user_id", "subscribers", ["telegram_user_id"])

    if not _has_table("signals"):
        op.create_table(
            "signals",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("bot_id", sa.Integer(), sa.ForeignKey("bots.id"), nullable=False),
            sa.Column("symbol", sa.String(length=50), nullable=False),
            sa.Column("side", sa.String(length=10), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("entry_zone_json", sa.JSON(), nullable=False),
            sa.Column("stop_price", sa.Float(), nullable=False),
            sa.Column("tp_json", sa.JSON(), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("rationale", sa.String(length=1000), nullable=False),
            sa.Column("result_pct", sa.Float(), nullable=True),
            sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.Column("grade", sa.String(length=10), nullable=True),
            sa.Column("is_public", sa.Boolean(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("qty", sa.Float(), nullable=True),
            sa.Column("required_margin", sa.Float(), nullable=True),
            sa.Column("net_rr_tp1", sa.Float(), nullable=True),
            sa.Column("net_rr_tp2", sa.Float(), nullable=True),
            sa.Column("net_pnl_tp1", sa.Float(), nullable=True),
            sa.Column("net_pnl_tp2", sa.Float(), nullable=True),
            sa.Column("net_pnl_stop", sa.Float(), nullable=True),
            sa.Column("plan_json", sa.JSON(), nullable=True),
            sa.Column("closed_exit_price", sa.Float(), nullable=True),
            sa.Column("closed_net_pnl", sa.Float(), nullable=True),
            sa.Column("closed_total_cost", sa.Float(), nullable=True),
            sa.Column("closed_reason", sa.String(length=100), nullable=True),
        )
    _create_index_once("ix_signals_symbol", "signals", ["symbol"])

    if not _has_table("orders"):
        op.create_table(
            "orders",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("bot_id", sa.Integer(), sa.ForeignKey("bots.id"), nullable=False),
            sa.Column("signal_id", sa.Integer(), sa.ForeignKey("signals.id"), nullable=True),
            sa.Column("symbol", sa.String(length=50), nullable=False),
            sa.Column("side", sa.String(length=10), nullable=False),
            sa.Column("order_type", sa.String(length=20), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("qty", sa.Float(), nullable=False),
            sa.Column("price", sa.Float(), nullable=True),
            sa.Column("filled_qty", sa.Float(), nullable=False),
            sa.Column("avg_fill_price", sa.Float(), nullable=True),
            sa.Column("client_order_id", sa.String(length=100), nullable=True),
            sa.Column("exchange_order_id", sa.String(length=100), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.UniqueConstraint("client_order_id", name="uq_orders_client_order_id"),
        )

    if not _has_table("positions"):
        op.create_table(
            "positions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("bot_id", sa.Integer(), sa.ForeignKey("bots.id"), nullable=False),
            sa.Column("signal_id", sa.Integer(), sa.ForeignKey("signals.id"), nullable=True),
            sa.Column("symbol", sa.String(length=50), nullable=False),
            sa.Column("side", sa.String(length=10), nullable=False),
            sa.Column("qty", sa.Float(), nullable=False),
            sa.Column("entry_price", sa.Float(), nullable=False),
            sa.Column("mark_price", sa.Float(), nullable=True),
            sa.Column("unrealized_pnl", sa.Float(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("opened_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        )
    _create_index_once("ix_positions_symbol", "positions", ["symbol"])

    if not _has_table("intelligence_events"):
        op.create_table(
            "intelligence_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("symbol", sa.String(length=50), nullable=False),
            sa.Column("status", sa.String(length=50), nullable=False),
            sa.Column("decision", sa.String(length=255), nullable=True),
            sa.Column("action", sa.String(length=20), nullable=True),
            sa.Column("regime", sa.String(length=100), nullable=True),
            sa.Column("radar_state", sa.String(length=100), nullable=True),
            sa.Column("confidence_hint", sa.Float(), nullable=True),
            sa.Column("setup_score", sa.Float(), nullable=True),
            sa.Column("payload_json", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        )
    _create_index_once("ix_intelligence_events_symbol", "intelligence_events", ["symbol"])
    _create_index_once("ix_intelligence_events_status", "intelligence_events", ["status"])

    if not _has_table("audit_events"):
        op.create_table(
            "audit_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("actor", sa.String(length=120), nullable=False),
            sa.Column("action", sa.String(length=120), nullable=False),
            sa.Column("resource_type", sa.String(length=80), nullable=True),
            sa.Column("resource_id", sa.String(length=120), nullable=True),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("details_json", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        )
    _create_index_once("ix_audit_events_actor", "audit_events", ["actor"])
    _create_index_once("ix_audit_events_action", "audit_events", ["action"])
    _create_index_once("ix_audit_events_resource_type", "audit_events", ["resource_type"])
    _create_index_once("ix_audit_events_resource_id", "audit_events", ["resource_id"])
    _create_index_once("ix_audit_events_status", "audit_events", ["status"])

    if not _has_table("billing_plans"):
        op.create_table(
            "billing_plans",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("code", sa.String(length=50), nullable=False),
            sa.Column("title", sa.String(length=120), nullable=False),
            sa.Column("amount_usdt", sa.Float(), nullable=False),
            sa.Column("currency", sa.String(length=20), nullable=False),
            sa.Column("duration_days", sa.Integer(), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("description", sa.String(length=500), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.UniqueConstraint("code", name="uq_billing_plans_code"),
        )
    _create_index_once("ix_billing_plans_code", "billing_plans", ["code"], unique=True)

    if not _has_table("payments"):
        op.create_table(
            "payments",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("subscriber_id", sa.Integer(), sa.ForeignKey("subscribers.id"), nullable=True),
            sa.Column("telegram_user_id", sa.String(length=100), nullable=False),
            sa.Column("username", sa.String(length=100), nullable=True),
            sa.Column("full_name", sa.String(length=255), nullable=True),
            sa.Column("plan_code", sa.String(length=50), nullable=False),
            sa.Column("amount", sa.Float(), nullable=False),
            sa.Column("currency", sa.String(length=20), nullable=False),
            sa.Column("duration_days", sa.Integer(), nullable=False),
            sa.Column("provider", sa.String(length=50), nullable=False),
            sa.Column("provider_payment_id", sa.String(length=120), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column("checkout_url", sa.String(length=1000), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("raw_payload", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("provider", "provider_payment_id", name="uq_payment_provider_payment_id"),
        )
    _create_index_once("ix_payments_subscriber_id", "payments", ["subscriber_id"])
    _create_index_once("ix_payments_telegram_user_id", "payments", ["telegram_user_id"])
    _create_index_once("ix_payments_plan_code", "payments", ["plan_code"])
    _create_index_once("ix_payments_provider", "payments", ["provider"])
    _create_index_once("ix_payments_status", "payments", ["status"])

    if not _has_table("payment_events"):
        op.create_table(
            "payment_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("payment_id", sa.Integer(), sa.ForeignKey("payments.id"), nullable=True),
            sa.Column("subscriber_id", sa.Integer(), sa.ForeignKey("subscribers.id"), nullable=True),
            sa.Column("provider", sa.String(length=50), nullable=False),
            sa.Column("provider_event_id", sa.String(length=160), nullable=False),
            sa.Column("event_type", sa.String(length=80), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column("amount", sa.Float(), nullable=True),
            sa.Column("currency", sa.String(length=20), nullable=True),
            sa.Column("raw_payload", sa.Text(), nullable=True),
            sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.UniqueConstraint("provider", "provider_event_id", name="uq_payment_event_provider_event_id"),
        )
    _create_index_once("ix_payment_events_payment_id", "payment_events", ["payment_id"])
    _create_index_once("ix_payment_events_subscriber_id", "payment_events", ["subscriber_id"])
    _create_index_once("ix_payment_events_provider", "payment_events", ["provider"])
    _create_index_once("ix_payment_events_provider_event_id", "payment_events", ["provider_event_id"])
    _create_index_once("ix_payment_events_status", "payment_events", ["status"])

    if not _has_table("telegram_deliveries"):
        op.create_table(
            "telegram_deliveries",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("chat_id", sa.String(length=100), nullable=False),
            sa.Column("message_type", sa.String(length=80), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column("text", sa.Text(), nullable=True),
            sa.Column("text_preview", sa.String(length=500), nullable=True),
            sa.Column("reply_markup_json", sa.Text(), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("max_attempts", sa.Integer(), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        )
    _create_index_once("ix_telegram_deliveries_chat_id", "telegram_deliveries", ["chat_id"])
    _create_index_once("ix_telegram_deliveries_message_type", "telegram_deliveries", ["message_type"])
    _create_index_once("ix_telegram_deliveries_status", "telegram_deliveries", ["status"])
    _create_index_once("ix_telegram_deliveries_next_retry_at", "telegram_deliveries", ["next_retry_at"])

    if not _has_table("telegram_profiles"):
        op.create_table(
            "telegram_profiles",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("telegram_user_id", sa.String(length=100), nullable=False),
            sa.Column("username", sa.String(length=100), nullable=True),
            sa.Column("full_name", sa.String(length=255), nullable=True),
            sa.Column("chat_id", sa.String(length=100), nullable=True),
            sa.Column("funnel_stage", sa.String(length=50), nullable=False),
            sa.Column("last_command", sa.String(length=100), nullable=True),
            sa.Column("source", sa.String(length=100), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.UniqueConstraint("telegram_user_id", name="uq_telegram_profiles_telegram_user_id"),
        )
    _create_index_once("ix_telegram_profiles_telegram_user_id", "telegram_profiles", ["telegram_user_id"], unique=True)
    _create_index_once("ix_telegram_profiles_chat_id", "telegram_profiles", ["chat_id"])
    _create_index_once("ix_telegram_profiles_funnel_stage", "telegram_profiles", ["funnel_stage"])


def downgrade() -> None:
    for index_name, table_name in [
        ("ix_telegram_profiles_funnel_stage", "telegram_profiles"),
        ("ix_telegram_profiles_chat_id", "telegram_profiles"),
        ("ix_telegram_profiles_telegram_user_id", "telegram_profiles"),
        ("ix_telegram_deliveries_next_retry_at", "telegram_deliveries"),
        ("ix_telegram_deliveries_status", "telegram_deliveries"),
        ("ix_telegram_deliveries_message_type", "telegram_deliveries"),
        ("ix_telegram_deliveries_chat_id", "telegram_deliveries"),
        ("ix_payment_events_status", "payment_events"),
        ("ix_payment_events_provider_event_id", "payment_events"),
        ("ix_payment_events_provider", "payment_events"),
        ("ix_payment_events_subscriber_id", "payment_events"),
        ("ix_payment_events_payment_id", "payment_events"),
        ("ix_payments_status", "payments"),
        ("ix_payments_provider", "payments"),
        ("ix_payments_plan_code", "payments"),
        ("ix_payments_telegram_user_id", "payments"),
        ("ix_payments_subscriber_id", "payments"),
        ("ix_billing_plans_code", "billing_plans"),
        ("ix_audit_events_status", "audit_events"),
        ("ix_audit_events_resource_id", "audit_events"),
        ("ix_audit_events_resource_type", "audit_events"),
        ("ix_audit_events_action", "audit_events"),
        ("ix_audit_events_actor", "audit_events"),
        ("ix_intelligence_events_status", "intelligence_events"),
        ("ix_intelligence_events_symbol", "intelligence_events"),
        ("ix_positions_symbol", "positions"),
        ("ix_signals_symbol", "signals"),
        ("ix_subscribers_telegram_user_id", "subscribers"),
        ("ix_users_email", "users"),
    ]:
        _drop_index_if_exists(index_name, table_name)

    for table_name in [
        "telegram_profiles",
        "telegram_deliveries",
        "payment_events",
        "payments",
        "billing_plans",
        "audit_events",
        "intelligence_events",
        "positions",
        "orders",
        "signals",
        "subscribers",
        "bots",
        "users",
    ]:
        if _has_table(table_name):
            op.drop_table(table_name)
