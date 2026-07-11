from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session

from models.signal import Signal
from services.telegram_router import TelegramRouter


class ReportService:
    def __init__(self):
        self.telegram = TelegramRouter()

    def collect_stats(self, db: Session, hours: int = 24) -> dict:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)

        signals = (
            db.query(Signal)
            .filter(Signal.created_at >= since)
            .order_by(Signal.id.asc())
            .all()
        )

        closed = [s for s in signals if s.result_pct is not None]
        wins = [s for s in closed if s.result_pct > 0]
        losses = [s for s in closed if s.result_pct <= 0]

        total_result = sum(s.result_pct or 0 for s in closed)
        winrate = (len(wins) / len(closed) * 100) if closed else 0

        best = None
        worst = None

        if closed:
            best = max(closed, key=lambda s: s.result_pct or 0)
            worst = min(closed, key=lambda s: s.result_pct or 0)

        def _signal_to_dict(s) -> dict | None:
            if s is None:
                return None
            return {
                "id": s.id,
                "symbol": s.symbol,
                "side": s.side,
                "status": s.status,
                "grade": s.grade,
                "confidence": float(s.confidence or 0),
                "rationale": s.rationale,
                "closed_reason": s.closed_reason,
                "result_pct": float(s.result_pct or 0),
                "closed_net_pnl": float(s.closed_net_pnl or 0) if s.closed_net_pnl is not None else None,
                "closed_total_cost": float(s.closed_total_cost or 0) if s.closed_total_cost is not None else None,
                "closed_exit_price": float(s.closed_exit_price or 0) if s.closed_exit_price is not None else None,
                "entry_zone_json": s.entry_zone_json,
                "stop_price": float(s.stop_price or 0) if s.stop_price is not None else None,
                "tp_json": s.tp_json,
                "qty": float(s.qty or 0) if s.qty is not None else None,
                "required_margin": float(s.required_margin or 0) if s.required_margin is not None else None,
                "net_rr_tp2": float(s.net_rr_tp2 or 0) if s.net_rr_tp2 is not None else None,
                "net_pnl_tp1": float(s.net_pnl_tp1 or 0) if s.net_pnl_tp1 is not None else None,
                "net_pnl_tp2": float(s.net_pnl_tp2 or 0) if s.net_pnl_tp2 is not None else None,
                "net_pnl_stop": float(s.net_pnl_stop or 0) if s.net_pnl_stop is not None else None,
                "created_at": str(s.created_at) if s.created_at else None,
                "closed_at": str(s.closed_at) if s.closed_at else None,
            }

        # Net PnL fields for richer reporting
        total_net_pnl = sum(float(s.closed_net_pnl or 0) for s in closed if s.closed_net_pnl is not None)
        total_costs = sum(float(s.closed_total_cost or 0) for s in closed if s.closed_total_cost is not None)
        closed_with_pnl = [s for s in closed if s.closed_net_pnl is not None]
        avg_net_pnl = round(total_net_pnl / len(closed_with_pnl), 6) if closed_with_pnl else 0.0
        active_statuses = {"published", "opened", "tp1", "breakeven"}
        active_signals = sum(1 for s in signals if s.status in active_statuses)

        return {
            "hours": hours,
            "total_signals": len(signals),
            "active_signals": active_signals,
            "closed_signals": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "winrate": round(winrate, 2),
            "total_result_pct": round(total_result, 2),
            "total_net_pnl_usdt": round(total_net_pnl, 6),
            "avg_net_pnl_usdt": avg_net_pnl,
            "total_costs_usdt": round(total_costs, 6),
            "best": _signal_to_dict(best),
            "worst": _signal_to_dict(worst),
        }

    async def send_owner_report(self, db: Session, hours: int = 24):
        stats = self.collect_stats(db, hours)

        text = self._owner_report_text(stats)
        await self.telegram.owner_alert("DAILY ROBOT REPORT", text)

        return stats

    async def send_free_report(self, db: Session, hours: int = 24):
        stats = self.collect_stats(db, hours)

        text = self._free_report_text(stats)
        await self.telegram.sender.send_message(
            chat_id=self.telegram_settings_free(),
            text=text
        )

        return stats

    async def send_vip_report(self, db: Session, hours: int = 24):
        stats = self.collect_stats(db, hours)

        text = self._vip_report_text(stats)
        await self.telegram.sender.send_message(
            chat_id=self.telegram_settings_vip(),
            text=text
        )

        return stats

    async def send_all_reports(self, db: Session, hours: int = 24):
        stats = self.collect_stats(db, hours)

        await self.telegram.owner_alert("DAILY ROBOT REPORT", self._owner_report_text(stats))
        await self.telegram.sender.send_message(self.telegram_settings_free(), self._free_report_text(stats))
        await self.telegram.sender.send_message(self.telegram_settings_vip(), self._vip_report_text(stats))

        return stats

    def telegram_settings_free(self):
        from core.config import settings
        return settings.TELEGRAM_FREE_SIGNALS_CHAT_ID

    def telegram_settings_vip(self):
        from core.config import settings
        return settings.TELEGRAM_VIP_SIGNALS_CHAT_ID

    def _owner_report_text(self, stats: dict) -> str:
        return (
            f"📈 OWNER отчёт Finmt за {stats['hours']}ч\n\n"
            f"Всего сигналов: {stats['total_signals']}\n"
            f"Закрыто: {stats['closed_signals']}\n"
            f"Победы: {stats['wins']}\n"
            f"Убытки: {stats['losses']}\n"
            f"Winrate: {stats['winrate']}%\n"
            f"Суммарный результат: {stats['total_result_pct']}%\n\n"
            f"Лучшая сделка: {self._signal_short(stats['best'])}\n"
            f"Худшая сделка: {self._signal_short(stats['worst'])}"
        )

    def _free_report_text(self, stats: dict) -> str:
        # (#free-cta-2026-07-11) CTA как в тизере: deep-link в бота (воронка),
        # а не захардкоженный @finmt_vip (приватный канал, для не-участников
        # обращение по юзернейму не работает).
        from core.config import settings as _settings
        bot_username = (getattr(_settings, "TELEGRAM_BOT_USERNAME", "") or "").lstrip("@")
        cta = (
            f"👉 Полные сигналы и VIP-доступ: https://t.me/{bot_username}?start=vip"
            if bot_username
            else "👉 Полные сигналы и VIP-доступ — напишите боту команду /plans"
        )
        return (
            f"📊 Итоги Finmt за {stats['hours']}ч\n\n"
            f"Сигналов: {stats['total_signals']}\n"
            f"Закрыто: {stats['closed_signals']}\n"
            f"Winrate: {stats['winrate']}%\n"
            f"Итог: {stats['total_result_pct']}%\n\n"
            f"{cta}"
        )

    def _vip_report_text(self, stats: dict) -> str:
        return (
            f"📊 VIP отчёт Finmt за {stats['hours']}ч\n\n"
            f"Всего сигналов: {stats['total_signals']}\n"
            f"Закрыто: {stats['closed_signals']}\n"
            f"Победы: {stats['wins']}\n"
            f"Убытки: {stats['losses']}\n"
            f"Winrate: {stats['winrate']}%\n"
            f"Итоговый результат: {stats['total_result_pct']}%\n\n"
            f"Лучшая сделка: {self._signal_short(stats['best'])}\n"
            f"Худшая сделка: {self._signal_short(stats['worst'])}"
        )

    def _signal_to_dict(self, signal):
        if not signal:
            return None

        return {
            "id": signal.id,
            "symbol": signal.symbol,
            "side": signal.side,
            "status": signal.status,
            "confidence": signal.confidence,
            "grade": signal.grade,
            "result_pct": signal.result_pct,
        }

    def _signal_short(self, signal):
        if not signal:
            return "-"

        if isinstance(signal, dict):
            return f"#{signal['id']} {signal['symbol']} {signal['side']} {signal['result_pct']}%"

        return f"#{signal.id} {signal.symbol} {signal.side} {signal.result_pct}%"
