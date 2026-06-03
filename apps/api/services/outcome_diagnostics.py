from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy.orm import Session

from models.signal import Signal


class OutcomeDiagnosticsService:
    """Post-trade diagnostics for roadmap Phase 1 observability."""

    def root_cause(self, db: Session, *, reason: str = "failed_setup_exit", limit: int = 500) -> dict[str, Any]:
        limit = min(max(int(limit or 500), 50), 5000)
        reason = str(reason or "failed_setup_exit")

        signals = (
            db.query(Signal)
            .filter(Signal.status == "closed")
            .order_by(Signal.id.desc())
            .limit(limit)
            .all()
        )

        target = [s for s in signals if str(s.closed_reason or "unknown") == reason]
        total_closed = len(signals)
        total_net = round(sum(self._float(s.closed_net_pnl) for s in signals), 6)
        target_net = round(sum(self._float(s.closed_net_pnl) for s in target), 6)
        target_costs = round(sum(self._float(s.closed_total_cost) for s in target), 6)

        lifecycle_values = self._lifecycle_metrics(target)

        return {
            "status": "ok",
            "reason": reason,
            "sample_closed_signals": total_closed,
            "target_count": len(target),
            "target_share_pct": round((len(target) / total_closed * 100), 2) if total_closed else 0.0,
            "total_net_pnl_usdt": total_net,
            "target_net_pnl_usdt": target_net,
            "target_costs_usdt": target_costs,
            "avg_target_net_pnl_usdt": round((target_net / len(target)), 6) if target else 0.0,
            "metrics": lifecycle_values,
            "by_symbol": self._group(target, lambda s: s.symbol),
            "by_side": self._group(target, lambda s: s.side),
            "by_grade": self._group(target, lambda s: s.grade or "unknown"),
            "by_symbol_side": self._group(target, lambda s: f"{s.symbol}:{s.side}"),
            "worst_symbols": self._group(target, lambda s: s.symbol)[:8],
            "recommendations": self._recommendations(
                reason=reason,
                target_share_pct=round((len(target) / total_closed * 100), 2) if total_closed else 0.0,
                target_net=target_net,
                metrics=lifecycle_values,
            ),
        }

    def _group(self, rows: list[Signal], key_fn) -> list[dict[str, Any]]:
        buckets: dict[str, dict[str, Any]] = defaultdict(lambda: {
            "key": "unknown",
            "count": 0,
            "net_pnl_usdt": 0.0,
            "costs_usdt": 0.0,
            "avg_result_pct": 0.0,
            "_sum_result_pct": 0.0,
        })

        for signal in rows:
            key = str(key_fn(signal) or "unknown")
            bucket = buckets[key]
            bucket["key"] = key
            bucket["count"] += 1
            bucket["net_pnl_usdt"] += self._float(signal.closed_net_pnl)
            bucket["costs_usdt"] += self._float(signal.closed_total_cost)
            bucket["_sum_result_pct"] += self._float(signal.result_pct)

        items = []
        for bucket in buckets.values():
            count = bucket["count"] or 1
            items.append({
                "key": bucket["key"],
                "count": bucket["count"],
                "net_pnl_usdt": round(bucket["net_pnl_usdt"], 6),
                "costs_usdt": round(bucket["costs_usdt"], 6),
                "avg_result_pct": round(bucket["_sum_result_pct"] / count, 4),
                "avg_net_pnl_usdt": round(bucket["net_pnl_usdt"] / count, 6),
            })

        return sorted(items, key=lambda item: (item["net_pnl_usdt"], -item["count"], item["key"]))

    def _lifecycle_metrics(self, rows: list[Signal]) -> dict[str, Any]:
        mfe_values = []
        mae_values = []
        missed_values = []
        positive_then_negative = 0

        for signal in rows:
            lifecycle = {}
            try:
                lifecycle = (signal.plan_json or {}).get("lifecycle") or {}
            except Exception:
                lifecycle = {}

            if lifecycle.get("positive_then_negative"):
                positive_then_negative += 1

            for source, target in [
                (lifecycle.get("mfe_pct"), mfe_values),
                (lifecycle.get("mae_pct"), mae_values),
                (lifecycle.get("missed_profit_pct"), missed_values),
            ]:
                value = self._maybe_float(source)
                if value is not None:
                    target.append(value)

        count = len(rows)
        return {
            "positive_then_negative_count": positive_then_negative,
            "positive_then_negative_rate": round((positive_then_negative / count * 100), 2) if count else 0.0,
            "avg_mfe_pct": self._avg(mfe_values),
            "avg_mae_pct": self._avg(mae_values),
            "avg_missed_profit_pct": self._avg(missed_values),
        }

    def _recommendations(self, *, reason: str, target_share_pct: float, target_net: float, metrics: dict[str, Any]) -> list[str]:
        items = []
        if reason == "failed_setup_exit" and target_share_pct > 35:
            items.append("Снизить поток публикаций по символам с failed_setup_exit > 35% до watch_only/cooldown.")
        if target_net < 0:
            items.append("Пересчитать entry/stop/TP после costs: текущий reason дает отрицательный net PnL.")
        if self._float(metrics.get("positive_then_negative_rate")) > 25:
            items.append("Ускорить partial/trailing: высокий positive→negative внутри целевой причины.")
        if self._float(metrics.get("avg_missed_profit_pct")) > 0.5:
            items.append("Добавить раннюю фиксацию MFE: средняя упущенная прибыль выше 0.5%.")
        if not items:
            items.append("Держать наблюдение: целевая причина пока не превышает go/no-go пороги.")
        return items

    @staticmethod
    def _float(value: Any) -> float:
        try:
            return float(value or 0.0)
        except Exception:
            return 0.0

    @staticmethod
    def _maybe_float(value: Any) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except Exception:
            return None

    def _avg(self, values: list[float]) -> float | None:
        if not values:
            return None
        return round(sum(values) / len(values), 4)
