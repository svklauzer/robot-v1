from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import distinct
from sqlalchemy.orm import Session

from models.bot import Bot
from models.signal import Signal
from services.symbol_performance_guard import SymbolPerformanceGuard


class SymbolPerformanceSummaryService:
    """Owner-facing summary for the per-symbol profitability guard.

    The guard already influences publishing. This service turns those decisions
    into a compact report so operators can see which symbols are blocked,
    de-risked, or safe before changing the bot universe.
    """

    def __init__(self, guard: SymbolPerformanceGuard | None = None):
        self.guard = guard or SymbolPerformanceGuard()

    def summarize(
        self,
        db: Session,
        bot: Bot | None = None,
        symbols: list[str] | None = None,
        lookback: int = 12,
    ) -> dict[str, Any]:
        lookback = min(max(int(lookback or 12), 1), 100)
        resolved_symbols = self._resolve_symbols(db=db, bot=bot, symbols=symbols)
        bot_id = int(bot.id) if bot else 1

        items: list[dict[str, Any]] = []
        for symbol in resolved_symbols:
            decision = self.guard.analyze(db=db, bot_id=bot_id, symbol=symbol, lookback=lookback)
            payload = self.guard.to_dict(decision)
            payload["classification"] = self._classification(payload)
            payload["action"] = self._action(payload)
            items.append(payload)

        items.sort(
            key=lambda item: (
                0 if item["classification"] == "blocked" else 1 if item["classification"] == "reduced" else 2,
                float(item.get("risk_multiplier") or 0),
                float(item.get("total_net_pnl") or 0),
            )
        )

        by_class = Counter(item["classification"] for item in items)
        by_reason = Counter(item["reason"] for item in items)

        return {
            "status": "ok",
            "lookback": lookback,
            "symbols_count": len(items),
            "blocked_count": by_class.get("blocked", 0),
            "reduced_count": by_class.get("reduced", 0),
            "ok_count": by_class.get("ok", 0),
            "by_reason": dict(by_reason),
            "items": items,
        }

    def _resolve_symbols(self, db: Session, bot: Bot | None, symbols: list[str] | None) -> list[str]:
        if symbols:
            return self._dedupe(symbols)

        configured = []
        if bot and isinstance(bot.config_json, dict):
            configured = bot.config_json.get("symbols") or []
        if configured:
            return self._dedupe(configured)

        rows = db.query(distinct(Signal.symbol)).filter(Signal.status == "closed").all()
        return self._dedupe([row[0] for row in rows if row and row[0]])

    def _dedupe(self, symbols: list[str]) -> list[str]:
        seen = set()
        result = []
        for symbol in symbols:
            normalized = str(symbol or "").strip().upper()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return result

    def _classification(self, payload: dict[str, Any]) -> str:
        if not payload.get("allowed"):
            return "blocked"
        if float(payload.get("risk_multiplier") or 0) < 1.0:
            return "reduced"
        return "ok"

    def _action(self, payload: dict[str, Any]) -> str:
        reason = str(payload.get("reason") or "")
        if not payload.get("allowed"):
            if reason == "symbol_negative_expectancy_blocked":
                return "Исключить символ из publish universe до восстановления net PnL/winrate."
            if reason in {"symbol_cooldown_losing_streak", "symbol_cooldown_failed_setup_streak"}:
                return "Оставить символ в cooldown; пересмотреть входы/таймфрейм и дождаться новых paper исходов."
            return "Блокировать новые публикации по символу до ручной проверки."
        if float(payload.get("risk_multiplier") or 0) < 1.0:
            if reason == "symbol_gives_back_profit_reduce_risk":
                return "Снизить риск и включить более раннюю фиксацию MFE/trailing для символа."
            return "Снизить размер позиции по символу и отслеживать следующие закрытия."
        return "Разрешить стандартный риск при сохранении глобальных readiness gates."
