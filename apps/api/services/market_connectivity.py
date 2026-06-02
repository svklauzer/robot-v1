from __future__ import annotations

from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from core.config import settings
from services.market_data import MarketDataService


class MarketConnectivityService:
    """Exchange connectivity/readiness probe for health and live gates."""

    def __init__(self, market: MarketDataService | None = None):
        self.market = market or MarketDataService()

    def check(self, symbol: str = "BTC/USDT") -> dict[str, Any]:
        started = perf_counter()
        checked_at = datetime.now(timezone.utc).isoformat()
        max_latency_ms = int(getattr(settings, "MARKET_CONNECTIVITY_MAX_LATENCY_MS", 5000))
        max_spread_pct = float(getattr(settings, "MARKET_CONNECTIVITY_MAX_SPREAD_PCT", 0.75))

        try:
            snap = self.market.snapshot(symbol)
            latency_ms = round((perf_counter() - started) * 1000, 2)
            bid = self._to_float(snap.get("bid"))
            ask = self._to_float(snap.get("ask"))
            last = self._to_float(snap.get("last"))
            spread_pct = self._spread_pct(bid, ask)
            source = str(snap.get("source") or "unknown")

            blockers: list[str] = []
            if last is None or last <= 0:
                blockers.append("market last price is missing")
            if latency_ms > max_latency_ms:
                blockers.append("market data latency is above threshold")
            if spread_pct is not None and spread_pct > max_spread_pct:
                blockers.append("market spread is above threshold")
            if settings.is_live_enabled and source == "mock":
                blockers.append("live mode cannot use mock market data")

            return {
                "ok": not blockers,
                "breaker_blocked": bool(blockers),
                "blockers": blockers,
                "symbol": symbol,
                "last": last,
                "bid": bid,
                "ask": ask,
                "spread_pct": spread_pct,
                "latency_ms": latency_ms,
                "source": source,
                "checked_at": checked_at,
            }
        except Exception as exc:
            return {
                "ok": False,
                "breaker_blocked": True,
                "blockers": ["market data snapshot failed"],
                "symbol": symbol,
                "error": f"{type(exc).__name__}: {exc}",
                "source": "unknown",
                "checked_at": checked_at,
            }

    def _to_float(self, value: Any) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except Exception:
            return None

    def _spread_pct(self, bid: float | None, ask: float | None) -> float | None:
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            return None
        mid = (bid + ask) / 2
        if mid <= 0:
            return None
        return round(((ask - bid) / mid) * 100, 4)
