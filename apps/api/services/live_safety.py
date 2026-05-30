from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from core.config import settings
from models.bot import Bot
from models.signal import Signal
from services.audit_log import AuditLogService


class LiveSafetyService:
    """Runtime circuit breakers that keep the robot out of live risk when loss limits are hit."""

    def __init__(self, audit_log: AuditLogService | None = None):
        self.audit_log = audit_log or AuditLogService()

    def _equity_usdt(self, equity_usdt: float | None = None) -> float:
        configured_equity = equity_usdt if equity_usdt is not None else settings.RISK_EQUITY_USDT
        return max(float(configured_equity or 0), 1.0)

    def daily_net_pnl_usdt(self, db: Session, hours: int = 24) -> float:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        signals = (
            db.query(Signal)
            .filter(Signal.status == "closed")
            .filter(Signal.closed_at.isnot(None))
            .filter(Signal.closed_at >= since)
            .all()
        )
        return round(sum(float(signal.closed_net_pnl or 0.0) for signal in signals), 6)

    def snapshot(self, db: Session, bot: Bot | None, equity_usdt: float | None = None, hours: int = 24) -> dict:
        config = dict(bot.config_json or {}) if bot else {}
        daily_net_pnl = self.daily_net_pnl_usdt(db, hours=hours)
        equity = self._equity_usdt(equity_usdt)
        daily_loss_pct = round(max(0.0, -daily_net_pnl / equity * 100), 4)
        max_daily_loss_pct = float(settings.MAX_DAILY_LOSS_PCT)
        daily_loss_blocked = daily_loss_pct >= max_daily_loss_pct
        kill_switch_enabled = bool(config.get("kill_switch_enabled"))

        blockers: list[str] = []
        if kill_switch_enabled:
            blockers.append("owner kill switch is enabled")
        if daily_loss_blocked:
            blockers.append("daily loss circuit breaker is active")

        return {
            "blocked": bool(blockers),
            "blockers": blockers,
            "bot_status": bot.status if bot else None,
            "kill_switch_enabled": kill_switch_enabled,
            "kill_switch_reason": config.get("kill_switch_reason"),
            "kill_switch_updated_at": config.get("kill_switch_updated_at"),
            "daily_net_pnl_usdt": daily_net_pnl,
            "daily_loss_pct": daily_loss_pct,
            "max_daily_loss_pct": max_daily_loss_pct,
            "daily_loss_blocked": daily_loss_blocked,
            "equity_usdt": equity,
            "window_hours": hours,
        }

    def enforce(self, db: Session, bot: Bot | None, equity_usdt: float | None = None) -> dict:
        state = self.snapshot(db=db, bot=bot, equity_usdt=equity_usdt)
        state["action_taken"] = None

        if not bot:
            return state

        if state["daily_loss_blocked"] and bot.status == "running":
            bot.status = "stopped_by_risk"
            self.audit_log.record(
                db,
                action="daily_loss_circuit_breaker",
                resource_type="bot",
                resource_id=bot.id,
                details={
                    "daily_loss_pct": state["daily_loss_pct"],
                    "daily_net_pnl_usdt": state["daily_net_pnl_usdt"],
                    "max_daily_loss_pct": state["max_daily_loss_pct"],
                },
            )
            state["bot_status"] = bot.status
            state["action_taken"] = "bot_stopped_by_risk"

        return state

    def set_kill_switch(self, db: Session, bot: Bot, enabled: bool, reason: str | None = None) -> dict:
        config = dict(bot.config_json or {})
        config["kill_switch_enabled"] = bool(enabled)
        config["kill_switch_reason"] = reason or ("owner_enabled" if enabled else "owner_disabled")
        config["kill_switch_updated_at"] = datetime.now(timezone.utc).isoformat()
        bot.config_json = config

        action = "kill_switch_enabled" if enabled else "kill_switch_disabled"
        if enabled and bot.status == "running":
            bot.status = "stopped_by_owner"

        self.audit_log.record(
            db,
            action=action,
            resource_type="bot",
            resource_id=bot.id,
            details={
                "reason": config["kill_switch_reason"],
                "bot_status": bot.status,
            },
        )
        return self.snapshot(db=db, bot=bot)
