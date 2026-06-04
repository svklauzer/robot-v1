import json
from pathlib import Path
from datetime import datetime, timezone
from core.config import settings


class MLTradeLogger:
    def __init__(self, path: str | Path | None = None):
        self.path = self._resolve_path(path)

    def _resolve_path(self, path: str | Path | None) -> Path:
        configured = path or getattr(settings, "TRADE_OUTCOMES_PATH", "storage/ml/trade_outcomes.jsonl")
        p = Path(configured)

        # Backward compatibility for older configs that used /app explicitly in
        # containers while tests/local runs execute from the repository root.
        if p.is_absolute() and not p.parent.exists() and str(p).startswith("/app/"):
            return Path(str(p).replace("/app/", "", 1))

        return p

    def _iter_logged_signal_ids(self) -> set[int]:
        if not self.path.exists():
            return set()

        ids: set[int] = set()
        try:
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        payload = json.loads(line)
                    except Exception:
                        continue
                    signal_id = payload.get("signal_id") if isinstance(payload, dict) else None
                    if signal_id is not None:
                        try:
                            ids.add(int(signal_id))
                        except Exception:
                            continue
        except Exception as e:
            print(f"[ML TRADE LOGGER DEDUP ERROR] path={self.path}: {e}")

        return ids

    def _already_logged(self, signal_id: int) -> bool:
        return int(signal_id) in self._iter_logged_signal_ids()

    def log_unlogged_closed_signals(self, db, limit: int = 500) -> dict:
        from models.signal import Signal

        logged_ids = self._iter_logged_signal_ids()
        closed = (
            db.query(Signal)
            .filter(Signal.status == "closed")
            .order_by(Signal.id.asc())
            .limit(max(int(limit), 1))
            .all()
        )

        logged = 0
        skipped = 0
        errors: list[dict] = []

        for signal in closed:
            if int(signal.id) in logged_ids:
                skipped += 1
                continue
            try:
                result = self.log_closed_signal(signal, known_logged_ids=logged_ids)
                if result.get("status") == "logged":
                    logged += 1
                    logged_ids.add(int(signal.id))
                else:
                    skipped += 1
            except Exception as exc:
                errors.append({
                    "signal_id": signal.id,
                    "error": f"{type(exc).__name__}: {exc}",
                })

        return {
            "status": "ok" if not errors else "partial_error",
            "path": str(self.path),
            "checked": len(closed),
            "logged": logged,
            "skipped": skipped,
            "errors": errors,
        }

    def log_closed_signal(self, signal, known_logged_ids: set[int] | None = None):
        self.path.parent.mkdir(parents=True, exist_ok=True)

        if known_logged_ids is not None:
            already_logged = int(signal.id) in known_logged_ids
        else:
            already_logged = self._already_logged(signal.id)

        if already_logged:
            return {
                "status": "skipped",
                "reason": "already_logged",
                "signal_id": signal.id,
                "path": str(self.path),
            }

        plan = signal.plan_json or {}
        lifecycle = plan.get("lifecycle") or {}

        closed_net_pnl = signal.closed_net_pnl
        closed_reason = signal.closed_reason

        item = {
            "logged_at": datetime.now(timezone.utc).isoformat(),

            "signal_id": signal.id,
            "bot_id": signal.bot_id,
            "symbol": signal.symbol,
            "side": signal.side,
            "grade": signal.grade,
            "confidence": signal.confidence,
            "rationale": signal.rationale,

            "status": signal.status,
            "closed_reason": closed_reason,

            "entry_zone": signal.entry_zone_json,
            "stop_price": signal.stop_price,
            "tp": signal.tp_json,

            "qty": signal.qty,
            "required_margin": signal.required_margin,

            "net_rr_tp1": signal.net_rr_tp1,
            "net_rr_tp2": signal.net_rr_tp2,
            "net_pnl_tp1": signal.net_pnl_tp1,
            "net_pnl_tp2": signal.net_pnl_tp2,
            "net_pnl_stop": signal.net_pnl_stop,

            "result_pct": signal.result_pct,
            "closed_exit_price": signal.closed_exit_price,
            "closed_net_pnl": closed_net_pnl,
            "closed_total_cost": signal.closed_total_cost,

            "opened_at": str(signal.opened_at) if signal.opened_at else None,
            "closed_at": str(signal.closed_at) if signal.closed_at else None,
            "created_at": str(signal.created_at) if signal.created_at else None,

            "lifecycle": {
                "entry_price": lifecycle.get("entry_price"),
                "exit_price": lifecycle.get("exit_price"),
                "mfe_pct": lifecycle.get("mfe_pct"),
                "mae_pct": lifecycle.get("mae_pct"),
                "missed_profit_pct": lifecycle.get("missed_profit_pct"),
                "positive_then_negative": lifecycle.get("positive_then_negative"),
                "max_profit_price": lifecycle.get("max_profit_price"),
                "max_drawdown_price": lifecycle.get("max_drawdown_price"),
                "updates": lifecycle.get("updates"),
                "close_reason": lifecycle.get("close_reason"),
            },

            "labels": {
                "is_win": bool(closed_net_pnl is not None and float(closed_net_pnl) > 0),
                "is_loss": bool(closed_net_pnl is not None and float(closed_net_pnl) < 0),
                "hit_stop": closed_reason == "stop_loss",
                "hit_tp2": closed_reason == "tp2_reached",
                "protected_profit": closed_reason in [
                    "protective_breakeven_profit_guard",
                    "adaptive_mfe_capture",
                    "protective_trailing_stop",
                    "adaptive_trailing_stop",
                    "adaptive_post_tp1_stop",
                    "trend_trailing_stop",
                ],
                "went_positive": bool(lifecycle.get("went_positive")),
                "positive_then_negative": bool(lifecycle.get("positive_then_negative")),
            },
        }

        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

        return {
            "status": "logged",
            "signal_id": signal.id,
            "path": str(self.path),
        }
