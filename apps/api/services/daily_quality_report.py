"""
Daily Trading Quality Report Service.

Агрегирует ключевые метрики за последние N часов:
- net PnL и win rate по закрытым сигналам
- доля failed_setup_exit и positive_then_negative
- Telegram delivery SLA
- активные сигналы
- Readiness gate snapshot

Вызывается из GET /analytics/daily-quality-report (owner-only).
Данные берутся из БД + trade_outcomes.jsonl.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from core.config import settings
from models.signal import Signal
from models.telegram_delivery import TelegramDelivery
from services.ml_outcome_stats import MLOutcomeStatsService
from services.live_safety import LiveSafetyService
from services.validation_gates import ValidationGateService


class DailyQualityReportService:
    """Build a one-page quality snapshot for the owner."""

    def __init__(self, hours: int = 24):
        self.hours = int(hours)

    # ── helpers ─────────────────────────────────────────────────────────────

    def _since(self) -> datetime:
        return datetime.now(timezone.utc) - timedelta(hours=self.hours)

    def _as_aware(self, dt: datetime | None) -> datetime | None:
        if dt is None:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    # ── sections ─────────────────────────────────────────────────────────────

    def _trading_section(self, db: Session) -> dict[str, Any]:
        since = self._since()
        closed = (
            db.query(Signal)
            .filter(
                Signal.status == "closed",
                Signal.closed_at.isnot(None),
                Signal.closed_at >= since,
            )
            .all()
        )

        total = len(closed)
        if total == 0:
            return {
                "window_hours": self.hours,
                "closed_count": 0,
                "net_pnl_usdt": 0.0,
                "win_count": 0,
                "loss_count": 0,
                "winrate_pct": None,
                "failed_setup_count": 0,
                "failed_setup_share_pct": None,
                "positive_then_negative_count": 0,
                "positive_then_negative_share_pct": None,
                "avg_net_pnl_usdt": None,
                "reasons": {},
            }

        net_pnl = round(sum(float(s.closed_net_pnl or 0) for s in closed), 4)
        wins = sum(1 for s in closed if float(s.closed_net_pnl or 0) > 0)
        losses = total - wins
        failed_setup = sum(1 for s in closed if s.closed_reason == "failed_setup_exit")
        pos_then_neg = sum(
            1 for s in closed
            if isinstance(s.plan_json, dict)
            and (s.plan_json.get("lifecycle") or {}).get("positive_then_negative")
        )

        reasons: dict[str, int] = {}
        for s in closed:
            r = str(s.closed_reason or "unknown")
            reasons[r] = reasons.get(r, 0) + 1

        return {
            "window_hours": self.hours,
            "closed_count": total,
            "net_pnl_usdt": net_pnl,
            "win_count": wins,
            "loss_count": losses,
            "winrate_pct": round(wins / total * 100, 1),
            "failed_setup_count": failed_setup,
            "failed_setup_share_pct": round(failed_setup / total * 100, 1),
            "positive_then_negative_count": pos_then_neg,
            "positive_then_negative_share_pct": round(pos_then_neg / total * 100, 1),
            "avg_net_pnl_usdt": round(net_pnl / total, 4),
            "reasons": dict(sorted(reasons.items(), key=lambda x: -x[1])),
        }

    def _active_signals_section(self, db: Session) -> dict[str, Any]:
        active = (
            db.query(Signal)
            .filter(Signal.status.in_(["published", "opened", "tp1", "breakeven"]))
            .all()
        )
        by_symbol: dict[str, list[str]] = {}
        for s in active:
            sym = str(s.symbol or "?")
            by_symbol.setdefault(sym, []).append(f"{s.side}:{s.status}")
        return {
            "total_active": len(active),
            "by_symbol": by_symbol,
        }

    def _telegram_sla_section(self, db: Session) -> dict[str, Any]:
        since = self._since()
        deliveries = (
            db.query(TelegramDelivery)
            .filter(TelegramDelivery.created_at >= since)
            .all()
        )
        total = len(deliveries)
        if total == 0:
            return {"total_attempted": 0, "success": 0, "failed": 0, "sla_pct": None}

        success = sum(1 for d in deliveries if d.status == "sent")
        failed = total - success
        return {
            "total_attempted": total,
            "success": success,
            "failed": failed,
            "sla_pct": round(success / total * 100, 2),
        }

    def _validation_section(self, db: Session) -> dict[str, Any]:
        try:
            result = ValidationGateService().evaluate(db)
            return {
                "ready": result.get("ready"),
                "blockers": result.get("blockers", []),
                "closed_count": result.get("closed_count"),
                "net_pnl_usdt": result.get("net_pnl_usdt"),
                "failed_setup_share_pct": result.get("failed_setup_share_pct"),
                "positive_then_negative_rate_pct": result.get("positive_then_negative_rate_pct"),
            }
        except Exception as e:
            return {"ready": None, "error": str(e)}

    def _live_safety_section(self, db: Session, equity_usdt: float = 1000.0) -> dict[str, Any]:
        try:
            snapshot = LiveSafetyService().snapshot(db=db, bot=None, equity_usdt=equity_usdt)
            return {
                "blocked": snapshot.get("blocked"),
                "blockers": snapshot.get("blockers", []),
                "daily_loss_pct": snapshot.get("daily_loss_pct"),
                "daily_net_pnl_usdt": snapshot.get("daily_net_pnl_usdt"),
                "kill_switch_enabled": snapshot.get("kill_switch_enabled"),
            }
        except Exception as e:
            return {"blocked": None, "error": str(e)}

    def _ml_outcomes_section(self) -> dict[str, Any]:
        try:
            summary = MLOutcomeStatsService().safe_summary()
            if summary.get("status") != "ok":
                return {"status": summary.get("status"), "reason": summary.get("reason")}
            return {
                "status": "ok",
                "total_closed": summary.get("total"),
                "net_pnl_sum": summary.get("net_pnl_sum"),
                "winrate_pct": summary.get("winrate_pct"),
                "top_reason": (summary.get("closed_reason_top") or [[None]])[0][0],
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    # ── public API ────────────────────────────────────────────────────────────

    def build(self, db: Session, equity_usdt: float = 1000.0) -> dict[str, Any]:
        generated_at = datetime.now(timezone.utc).isoformat()

        trading = self._trading_section(db)
        active = self._active_signals_section(db)
        telegram = self._telegram_sla_section(db)
        validation = self._validation_section(db)
        safety = self._live_safety_section(db, equity_usdt=equity_usdt)
        ml = self._ml_outcomes_section()

        # ── overall status ───────────────────────────────────────────────────
        issues: list[str] = []
        fss = trading.get("failed_setup_share_pct")
        if fss is not None and fss > 35:
            issues.append(f"failed_setup_exit share {fss:.1f}% > 35% threshold")

        ptn = trading.get("positive_then_negative_share_pct")
        if ptn is not None and ptn > 25:
            issues.append(f"positive_then_negative share {ptn:.1f}% > 25% threshold")

        if trading.get("net_pnl_usdt", 0) < 0 and (trading.get("closed_count") or 0) >= 5:
            issues.append(f"negative net PnL in window: {trading['net_pnl_usdt']} USDT")

        tg_sla = telegram.get("sla_pct")
        if tg_sla is not None and tg_sla < 99.0:
            issues.append(f"Telegram delivery SLA {tg_sla:.1f}% < 99%")

        if safety.get("blocked"):
            issues.extend(safety.get("blockers", []))

        if validation.get("blockers"):
            issues.extend([f"validation: {b}" for b in validation["blockers"]])

        return {
            "generated_at": generated_at,
            "window_hours": self.hours,
            "status": "ok" if not issues else "attention_required",
            "issues": issues,
            "trading": trading,
            "active_signals": active,
            "telegram_sla": telegram,
            "validation_gates": validation,
            "live_safety": safety,
            "ml_outcomes_summary": ml,
        }
