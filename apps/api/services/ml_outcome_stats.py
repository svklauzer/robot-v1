import json
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone

from core.config import settings


class MLOutcomeStatsService:
    """
    Читает storage/ml/trade_outcomes.jsonl и строит статистику по закрытым сделкам.

    Пока это не ML-модель, а статистический слой памяти:
    - какие symbol/side чаще дают stop_loss
    - какие symbol/side дают protected profit
    - где positive_then_negative слишком часто
    - где net pnl отрицательный
    """

    def __init__(self, path: str | Path | None = None, stale_hours: int | None = None):
        self.file_path = Path(path) if path is not None else Path(getattr(settings, "TRADE_OUTCOMES_PATH", "storage/ml/trade_outcomes.jsonl"))
        self.stale_hours = int(stale_hours or getattr(settings, "ML_OUTCOMES_STALE_HOURS", 72) or 72)

    def safe_summary(self) -> dict:
        try:
            return self.summary()
        except Exception as e:
            return {
                "status": "degraded",
                "reason": "ml_outcome_stats_failed",
                "error": f"{type(e).__name__}: {e}",
                "source_path": str(self.file_path),
            }

    def _load_rows(self) -> list[dict]:
        if not self.file_path.exists():
            return []

        rows = []
        self._parse_errors = 0

        with self.file_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()

                if not line:
                    continue

                try:
                    payload = json.loads(line)
                    if isinstance(payload, dict):
                        rows.append(payload)
                    else:
                        self._parse_errors = getattr(self, "_parse_errors", 0) + 1
                except Exception:
                    self._parse_errors = getattr(self, "_parse_errors", 0) + 1
                    continue

        return rows

    def summary(self) -> dict:
        rows = self._load_rows()

        total = len(rows)

        if total == 0:
            return {
                "status": "empty",
                "total": 0,
                "parse_errors": getattr(self, "_parse_errors", 0),
                "source_path": str(self.file_path),
                "groups": [],
                **self._freshness_payload(rows),
            }

        freshness = self._freshness_payload(rows)

        groups = defaultdict(lambda: {
            "count": 0,
            "wins": 0,
            "losses": 0,
            "stop_loss": 0,
            "protected_profit": 0,
            "tp2": 0,
            "positive_then_negative": 0,
            "net_pnl": 0.0,
            "costs": 0.0,
            "mfe_sum": 0.0,
            "mae_sum": 0.0,
            "mfe_count": 0,
            "mae_count": 0,
        })

        for row in rows:
            symbol = row.get("symbol") or "unknown"
            side = row.get("side") or "unknown"
            key = f"{symbol}:{side}"

            labels = row.get("labels") or {}
            lifecycle = row.get("lifecycle") or {}

            group = groups[key]
            group["symbol"] = symbol
            group["side"] = side
            group["count"] += 1

            net_pnl = float(row.get("closed_net_pnl") or 0)
            costs = float(row.get("closed_total_cost") or 0)

            group["net_pnl"] += net_pnl
            group["costs"] += costs

            if net_pnl > 0:
                group["wins"] += 1
            elif net_pnl < 0:
                group["losses"] += 1

            if labels.get("hit_stop"):
                group["stop_loss"] += 1

            if labels.get("protected_profit"):
                group["protected_profit"] += 1

            if labels.get("hit_tp2"):
                group["tp2"] += 1

            if labels.get("positive_then_negative"):
                group["positive_then_negative"] += 1

            mfe = lifecycle.get("mfe_pct")
            mae = lifecycle.get("mae_pct")

            if mfe is not None:
                group["mfe_sum"] += float(mfe)
                group["mfe_count"] += 1

            if mae is not None:
                group["mae_sum"] += float(mae)
                group["mae_count"] += 1

        result_groups = []

        for key, group in groups.items():
            count = group["count"]

            wins = group["wins"]
            losses = group["losses"]

            winrate = round((wins / count) * 100, 2) if count else 0.0
            stop_rate = round((group["stop_loss"] / count) * 100, 2) if count else 0.0
            protected_rate = round((group["protected_profit"] / count) * 100, 2) if count else 0.0
            positive_then_negative_rate = round((group["positive_then_negative"] / count) * 100, 2) if count else 0.0

            avg_mfe = (
                round(group["mfe_sum"] / group["mfe_count"], 4)
                if group["mfe_count"]
                else 0.0
            )

            avg_mae = (
                round(group["mae_sum"] / group["mae_count"], 4)
                if group["mae_count"]
                else 0.0
            )

            net_pnl = round(group["net_pnl"], 6)
            costs = round(group["costs"], 6)

            risk_state = "neutral"

            if count >= 2 and net_pnl < 0 and stop_rate >= 50:
                risk_state = "penalize"

            if count >= 2 and net_pnl > 0 and protected_rate >= 50:
                risk_state = "reward"

            if count >= 3 and positive_then_negative_rate >= 50:
                risk_state = "protect_earlier"

            result_groups.append({
                "key": key,
                "symbol": group["symbol"],
                "side": group["side"],

                "count": count,
                "wins": wins,
                "losses": losses,
                "winrate": winrate,

                "stop_loss": group["stop_loss"],
                "stop_rate": stop_rate,

                "protected_profit": group["protected_profit"],
                "protected_rate": protected_rate,

                "tp2": group["tp2"],

                "positive_then_negative": group["positive_then_negative"],
                "positive_then_negative_rate": positive_then_negative_rate,

                "avg_mfe_pct": avg_mfe,
                "avg_mae_pct": avg_mae,

                "net_pnl": net_pnl,
                "costs": costs,

                "risk_state": risk_state,
            })

        result_groups.sort(
            key=lambda item: (
                item["risk_state"] != "penalize",
                item["net_pnl"],
            )
        )

        return {
            "status": "stale" if freshness["stale"] else "ok",
            "total": total,
            "parse_errors": getattr(self, "_parse_errors", 0),
            "source_path": str(self.file_path),
            "groups": result_groups,
            **freshness,
        }


    def _freshness_payload(self, rows: list[dict]) -> dict:
        latest = None
        for row in rows:
            logged_at = self._parse_datetime(row.get("logged_at"))
            if logged_at is not None and (latest is None or logged_at > latest):
                latest = logged_at

        if latest is None:
            return {
                "latest_logged_at": None,
                "latest_age_hours": None,
                "latest_age_days": None,
                "stale": False,
                "is_stale": False,
                "freshness_status": "empty" if not rows else "missing_logged_at",
                "stale_after_hours": self.stale_hours,
            }

        age_hours = max(round((datetime.now(timezone.utc) - latest).total_seconds() / 3600, 2), 0.0)
        stale = age_hours > self.stale_hours
        return {
            "latest_logged_at": latest.isoformat(),
            "latest_age_hours": age_hours,
            "latest_age_days": round(age_hours / 24, 2),
            "stale": stale,
            "is_stale": stale,
            "freshness_status": "stale" if stale else "fresh",
            "stale_after_hours": self.stale_hours,
        }

    def _parse_datetime(self, value) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def penalty_for(self, symbol: str, side: str) -> dict:
        stats = self.summary()

        if stats.get("status") != "ok":
            return {
                "penalty": 0.0,
                "reason": "no_ml_outcome_stats",
                "stats": None,
            }

        key = f"{symbol}:{side}"

        row = None

        for group in stats.get("groups", []):
            if group.get("key") == key:
                row = group
                break

        if not row:
            return {
                "penalty": 0.0,
                "reason": "no_symbol_side_history",
                "stats": None,
            }

        count = int(row.get("count") or 0)

        # Меньше 3 сделок — статистика ещё слабая.
        if count < 3:
            return {
                "penalty": 0.0,
                "reason": "not_enough_history",
                "stats": row,
            }

        penalty = 0.0
        reasons = []

        if row.get("net_pnl", 0) < 0:
            penalty += 4.0
            reasons.append("negative_net_pnl")

        if row.get("stop_rate", 0) >= 50:
            penalty += 5.0
            reasons.append("high_stop_rate")

        if row.get("positive_then_negative_rate", 0) >= 40:
            penalty += 4.0
            reasons.append("positive_then_negative_too_often")

        if row.get("avg_mae_pct", 0) <= -1.0:
            penalty += 3.0
            reasons.append("avg_mae_too_deep")

        reward = 0.0

        if row.get("net_pnl", 0) > 0:
            reward += 2.0

        if row.get("protected_rate", 0) >= 50:
            reward += 2.0

        final_penalty = max(0.0, penalty - reward)

        return {
            "penalty": round(final_penalty, 2),
            "reason": "_".join(reasons) if reasons else "ok",
            "stats": row,
        }