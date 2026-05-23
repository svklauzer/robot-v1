# apps/api/services/signal_freshness.py

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class FreshnessDecision:
    allowed: bool
    reason: str
    score: float
    payload: dict


class SignalFreshnessService:
    def _result_pct(self, side: str, entry: float, price: float) -> float:
        if side == "long":
            return ((price - entry) / entry) * 100
        return ((entry - price) / entry) * 100

    def _distance_from_entry_zone_pct(self, price: float, entry_from: float, entry_to: float) -> float:
        low = min(entry_from, entry_to)
        high = max(entry_from, entry_to)

        if low <= price <= high:
            return 0.0

        if price < low:
            return abs((low - price) / low) * 100

        return abs((price - high) / high) * 100

    def validate_signal(
        self,
        *,
        symbol: str,
        side: str,
        price: float,
        entry_zone: dict,
        stop_price: float,
        tp: dict,
        expires_at=None,
        max_distance_pct: float = 0.35,
    ) -> FreshnessDecision:
        now = datetime.now(timezone.utc)

        if expires_at is not None:
            exp = expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)

            if exp < now:
                return FreshnessDecision(
                    allowed=False,
                    reason="signal_expired",
                    score=0.0,
                    payload={"expires_at": str(expires_at), "now": str(now)},
                )

        entry_from = float(entry_zone["from"])
        entry_to = float(entry_zone["to"])
        stop = float(stop_price)
        tp1 = float(tp["tp1"])
        tp2 = float(tp["tp2"])
        price = float(price)

        distance_pct = self._distance_from_entry_zone_pct(price, entry_from, entry_to)

        if distance_pct > max_distance_pct:
            return FreshnessDecision(
                allowed=False,
                reason="price_too_far_from_entry_zone",
                score=max(0.0, 100.0 - distance_pct * 50),
                payload={
                    "symbol": symbol,
                    "side": side,
                    "price": price,
                    "entry_zone": entry_zone,
                    "distance_pct": round(distance_pct, 4),
                    "max_distance_pct": max_distance_pct,
                },
            )

        entry_mid = (entry_from + entry_to) / 2

        if side == "long":
            risk = abs(entry_mid - stop)
            reward = abs(tp2 - entry_mid)
        else:
            risk = abs(stop - entry_mid)
            reward = abs(entry_mid - tp2)

        rr = reward / risk if risk > 0 else 0.0

        if rr < 1.2:
            return FreshnessDecision(
                allowed=False,
                reason="fresh_rr_too_low",
                score=30.0,
                payload={
                    "rr": round(rr, 4),
                    "entry_mid": entry_mid,
                    "stop": stop,
                    "tp2": tp2,
                },
            )

        score = 100.0
        score -= distance_pct * 25
        score += min(rr, 3.0) * 5

        return FreshnessDecision(
            allowed=True,
            reason="fresh",
            score=round(score, 2),
            payload={
                "symbol": symbol,
                "side": side,
                "price": price,
                "entry_zone": entry_zone,
                "distance_pct": round(distance_pct, 4),
                "rr": round(rr, 4),
            },
        )