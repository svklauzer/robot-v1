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

        return {
            "hours": hours,
            "total_signals": len(signals),
            "closed_signals": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "winrate": round(winrate, 2),
            "total_result_pct": round(total_result, 2),
            "best": best,
            "worst": worst,
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
        return (
            f"📊 Итоги Finmt за {stats['hours']}ч\n\n"
            f"Сигналов: {stats['total_signals']}\n"
            f"Закрыто: {stats['closed_signals']}\n"
            f"Winrate: {stats['winrate']}%\n"
            f"Итог: {stats['total_result_pct']}%\n\n"
            f"Полные сигналы и сопровождение доступны в VIP: @finmt_vip"
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
