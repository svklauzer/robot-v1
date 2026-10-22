import json
import math
import os
import shutil
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from core.config import settings
from services.close_reasons import reached_tp2


def _as_dict(raw) -> dict:
    """plan_json бывает dict (Postgres JSON) или строкой (текстовые бэкенды)."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _num(v, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


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
        from sqlalchemy import and_
        from models.signal import Signal

        logged_ids = self._iter_logged_signal_ids()

        # (#catchup-starvation-2026-10-10) Раньше: `status == closed ORDER BY id ASC
        # LIMIT 500`. Это 500 САМЫХ СТАРЫХ закрытых сигналов; как только их набралось
        # больше 500 и все они уже в файле, добор не видел ни одного нового — молча,
        # а заодно каждый вызов тянул из БД до 500 полных plan_json впустую.
        # Теперь в запрос идут только ещё не записанные, а limit ограничивает число
        # НОВЫХ записей за вызов.
        if logged_ids:
            cond = and_(Signal.status == "closed", ~Signal.id.in_(sorted(logged_ids)))
        else:
            cond = Signal.status == "closed"
        closed = (
            db.query(Signal)
            .filter(cond)
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

        # (#two-legs-revert-2026-10-17) backfill второй ноги убран: closed_net_pnl её уже содержит.
        backfill = {"status": "disabled"}

        return {
            "status": "ok" if not errors else "partial_error",
            "path": str(self.path),
            "checked": len(closed),
            "logged": logged,
            "skipped": skipped,
            "errors": errors,
            "backfill": backfill,
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

        plan = _as_dict(signal.plan_json)
        lifecycle = _as_dict(plan.get("lifecycle"))
        ml_data = _as_dict(plan.get("ml"))

        closed_net_pnl = signal.closed_net_pnl
        closed_reason = signal.closed_reason

        # Первая нога — справочно; closed_net_pnl УЖЕ итог по всем ногам.
        tp1_part = _as_dict(plan.get("tp1_partial"))
        partial_net = _num(tp1_part.get("net_pnl"))
        partial_cost = _num(tp1_part.get("total_cost"))
        total_net = None if closed_net_pnl is None else _num(closed_net_pnl)  # уже включает ноги

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

            # Контекст входа для обучения: режим и микроструктура стакана.
            "regime": plan.get("regime"),
            "trade_mode": plan.get("trade_mode"),
            "radar_state": plan.get("radar_state"),
            "entry_depth": plan.get("entry_depth"),

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

            # Первая нога (частичное закрытие на TP1) и полный результат сделки.
            # `closed_net_pnl` выше УЖЕ итог по всем ногам; tp1_partial_* — справочно,
            # прибавлять их нельзя (двойной счёт).
            "tp1_partial_net_pnl": partial_net,
            "tp1_partial_total_cost": partial_cost,
            "total_net_pnl": total_net,

            "opened_at": str(signal.opened_at) if signal.opened_at else None,
            "closed_at": str(signal.closed_at) if signal.closed_at else None,
            "created_at": str(signal.created_at) if signal.created_at else None,

            # (#ml-score-logging-fix) ML-данные для shadow-валидации
            "ml_score": ml_data.get("ml_score"),
            "ml_mode": ml_data.get("mode"),
            "ml_action": ml_data.get("action"),
            # (#model-version-2026-10-22) Какая версия модели считала score на входе.
            "ml_model_trained_at": ml_data.get("model_trained_at"),
            "ml_feature_version": ml_data.get("feature_version"),

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
                # (#audit-traj) Траектория для offline exit-replay.
                "traj": lifecycle.get("traj"),
                "traj_step": lifecycle.get("traj_step"),
            },

            # (#gates-in-export-2026-08-03) Вердикты гейтов входа. Раньше они
            # жили ТОЛЬКО в signal.plan_json в базе, а в выгрузку не попадали —
            # то есть накопить по ним статистику было нельзя в принципе, сколько
            # ни ждать. Обнаружилось при первой же попытке откалибровать пороги
            # ТЗ: scripts/tz_calibrate.py нашёл 0 записей на 101 строке.
            #
            # Это ровно то, ради чего гейты и делались в режиме наблюдения:
            # посчитать, что каждое условие отсекло бы, и чем те сделки
            # закончились. Без этих полей режим наблюдения бессмысленен.
            "gates": {
                "tz_shadow": plan.get("tz_shadow"),
                "trend_trigger": plan.get("trend_trigger"),
                "setup_reach": plan.get("setup_reach"),
                # Причина входа: без неё разворот от поддержки и пробой лежат
                # в одной куче «trend», и разрез результата по сетапу невозможен.
                "entry_reason": plan.get("entry_reason"),
                # Отпечаток конфига на момент входа — чтобы сделки до и после
                # правок не смешивались в одной статистике незаметно.
                "config_fingerprint": ((plan.get("config") or {}).get("fingerprint")
                                       if isinstance(plan.get("config"), dict) else None),
            },

            "labels": {
                # is_win / is_loss — по ПОЛНОМУ результату (обе ноги).
                "is_win": bool(total_net is not None and total_net > 0),
                "is_loss": bool(total_net is not None and total_net < 0),
                "hit_stop": closed_reason == "stop_loss",
                # (#tp2-stage-2026-09-12) Этап TP2 закрывает хвост своими
                # причинами — сделка до TP2 при этом дошла.
                "hit_tp2": reached_tp2(closed_reason, plan),
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
