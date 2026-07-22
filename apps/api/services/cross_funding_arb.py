"""P2: paper cross-venue funding-arb HTX ↔ Kraken (#cross-farb-2026-07-19).

Идея (подтверждена телеметрией P1: у всех пар доминирует short_htx_long_kraken,
HTX прижат к базовой ставке 0.01%/8ч, Kraken плавает почасово ниже): две ноги
перпов на разных биржах, дельта-нейтрально, без переводов монет. Доход = разница
фандинга (carry), риски = комиссии двух ног и дрейф базиса HTX−Kraken.

Строго PAPER и строго изолированно: собственный JSON-state на persistent-диске
(без миграций БД), никаких ордеров, никакого пересечения с Trade/Grid/Funding
Arb движками. Флаг CROSS_FARB_ENABLED (дефолт False) — включение отдельным
коммитом после подтверждения устойчивости спреда за ≥3 дня.

Механика начисления: шаг движка идёт с почасовым снапшот-воркером; carry
начисляется pro-rata по фактически прошедшим часам от ТЕКУЩЕГО спреда
(honest-приближение: HTX платит раз в 8ч — мы амортизируем rate/8 в час).
Базис учитывается mark-to-market: для short_htx_long_kraken прибыль, когда
premium HTX к Kraken сжимается.
"""

import json
import os
import time
import uuid
from datetime import datetime, timezone

from core.config import settings


# ── Чистые функции (тестируются без сети/файлов) ─────────────────────────────

def signed_carry_pct(item_spread: dict, direction: str) -> tuple[float, float]:
    """(carry_hourly_pct, carry_ann_pct) В СТОРОНУ позиции: положительный —
    позиция зарабатывает фандинг, отрицательный — платит."""
    hourly = float(item_spread.get("spread_hourly_pct") or 0.0)
    ann = float(item_spread.get("spread_annualized_pct") or 0.0)
    if direction == "short_kraken_long_htx":
        hourly, ann = -hourly, -ann
    return hourly, ann


def accrue_funding_usdt(notional: float, carry_hourly_pct: float, hours: float) -> float:
    """Carry за фактически прошедшие часы (может быть отрицательным)."""
    return round(float(notional) * carry_hourly_pct / 100.0 * max(0.0, float(hours)), 6)


def basis_pnl_usdt(notional: float, entry_basis_pct: float, now_basis_pct: float, direction: str) -> float:
    """Базис = price_diff_pct (HTX vs Kraken). short_htx_long_kraken зарабатывает
    на сжатии premium HTX: pnl = notional × (basis_вход − basis_сейчас)."""
    delta = float(entry_basis_pct or 0.0) - float(now_basis_pct or 0.0)
    if direction == "short_kraken_long_htx":
        delta = -delta
    return round(float(notional) * delta / 100.0, 6)


def round_trip_fees_usdt(notional: float) -> float:
    """Вход+выход, тейкер обеих ног (консервативно, maker-вход — апсайд)."""
    htx_taker = float(getattr(settings, "FUTURES_TAKER_FEE", 0.0005))
    kraken_taker = float(getattr(settings, "KRAKEN_TAKER_FEE", 0.0005))
    return round(float(notional) * (htx_taker + kraken_taker) * 2, 6)


def entry_allowed(item: dict, history_row: dict | None) -> tuple[bool, str]:
    """Гейт входа: текущий спред ≥ порога И направление устойчиво за lookback
    (история P1). Без истории — не входим (нет доказательства устойчивости)."""
    spread = item.get("spread") or {}
    ann = float(spread.get("spread_annualized_pct") or 0.0)
    min_ann = float(getattr(settings, "CROSS_FARB_MIN_ANN_PCT", 12.0))
    if abs(ann) < min_ann:
        return False, f"spread_below_min:{ann:.2f}<{min_ann}"
    if not history_row:
        return False, "no_history_for_symbol"
    stability = float(history_row.get("direction_stability_pct") or 0.0)
    min_stab = float(getattr(settings, "CROSS_FARB_MIN_STABILITY_PCT", 80.0))
    if stability < min_stab:
        return False, f"stability_below_min:{stability:.1f}<{min_stab}"
    if history_row.get("dominant_direction") != spread.get("direction"):
        return False, "current_direction_vs_dominant_mismatch"
    avg_ann = float(history_row.get("avg_spread_ann_pct") or 0.0)
    if abs(avg_ann) < min_ann * 0.5:
        return False, f"avg_spread_too_thin:{avg_ann:.2f}"
    return True, "ok"


def exit_reason(position: dict, item: dict, now_ts: float) -> str | None:
    """Выход: сжатие carry ниже порога / разворот / максимальный возраст."""
    spread = item.get("spread") or {}
    _, ann_signed = signed_carry_pct(spread, str(position.get("direction")))
    close_ann = float(getattr(settings, "CROSS_FARB_CLOSE_ANN_PCT", 3.0))
    if ann_signed <= 0:
        return "spread_flipped"
    if ann_signed < close_ann:
        return f"spread_compressed:{ann_signed:.2f}<{close_ann}"
    max_hold_days = float(getattr(settings, "CROSS_FARB_MAX_HOLD_DAYS", 14.0))
    # НЕ `or now_ts`: opened_ts=0.0 falsy — возраст обнулялся бы.
    _opened_raw = position.get("opened_ts")
    opened_ts = float(_opened_raw) if _opened_raw is not None else now_ts
    if now_ts - opened_ts > max_hold_days * 86400:
        return "max_hold_reached"
    return None


# ── Состояние (JSON на persistent-диске) ─────────────────────────────────────

class CrossFundingArbStore:
    def __init__(self, path: str | None = None):
        self.path = path or str(getattr(settings, "CROSS_FARB_STATE_PATH", "storage/ml/cross_funding_arb_state.json"))

    def load(self) -> dict:
        if not os.path.exists(self.path):
            return {"open": [], "closed": [], "realized_total_usdt": 0.0}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            data.setdefault("open", [])
            data.setdefault("closed", [])
            data.setdefault("realized_total_usdt", 0.0)
            return data
        except Exception:  # noqa: BLE001 — битый файл не валит движок
            return {"open": [], "closed": [], "realized_total_usdt": 0.0}

    def save(self, state: dict) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
        os.replace(tmp, self.path)


# ── Движок ───────────────────────────────────────────────────────────────────

class CrossFundingArbEngine:
    def __init__(self, store: CrossFundingArbStore | None = None, history=None):
        self.store = store or CrossFundingArbStore()
        if history is None:
            from services.venue_compare import VenueSpreadHistory
            history = VenueSpreadHistory()
        self.history = history

    def _symbols(self) -> list[str]:
        raw = str(getattr(settings, "CROSS_FARB_SYMBOLS", "AVAX/USDT,XRP/USDT,TRX/USDT,SOL/USDT"))
        return [s.strip().upper() for s in raw.split(",") if s.strip()]

    def step(self, compare_payload: dict, now: float | None = None) -> dict:
        """Один шаг: начислить carry открытым, закрыть по условиям, открыть новые.
        Вызывается из почасового воркера со СВЕЖИМ compare-payload."""
        now = float(now or time.time())
        items = {
            i["symbol"]: i
            for i in (compare_payload or {}).get("items", [])
            if "error" not in i and i.get("spread")
        }
        state = self.store.load()
        actions: list[dict] = []

        # 1) ведение открытых
        still_open = []
        for pos in state["open"]:
            item = items.get(pos.get("symbol"))
            if not item:
                # нет свежих данных по символу — carry не начисляем (честно), позицию держим
                still_open.append(pos)
                continue
            hours = max(0.0, (now - float(pos.get("last_accrual_ts") or now)) / 3600.0)
            carry_hourly, _ = signed_carry_pct(item["spread"], str(pos.get("direction")))
            pos["funding_accrued_usdt"] = round(
                float(pos.get("funding_accrued_usdt") or 0.0)
                + accrue_funding_usdt(float(pos["notional_usdt"]), carry_hourly, hours),
                6,
            )
            pos["last_accrual_ts"] = now
            pos["last_spread_ann_pct"] = item["spread"].get("spread_annualized_pct")
            pos["unrealized_basis_usdt"] = basis_pnl_usdt(
                float(pos["notional_usdt"]),
                float(pos.get("entry_basis_pct") or 0.0),
                float(item.get("price_diff_pct") or 0.0),
                str(pos.get("direction")),
            )

            # (#cross-farb-exit-confirm-2026-07-22) Выход требует ПОДТВЕРЖДЕНИЯ,
            # как вход требовал устойчивости: первые 12ч в бою дали 3 закрытия
            # spread_flipped по ОДНОМУ шумному снапшоту (−0.60 = 3× комиссии),
            # спред возвращался через час-два. Держать сквозь шумный флип стоит
            # ~0.002/час отрицательного carry, перезаход — 0.20 комиссий.
            # max_hold закрывает сразу (это не шум), остальное — N шагов подряд.
            reason = exit_reason(pos, item, now)
            if reason == "max_hold_reached":
                do_close = True
            elif reason:
                pos["exit_streak"] = int(pos.get("exit_streak") or 0) + 1
                pos["exit_streak_reason"] = reason
                do_close = pos["exit_streak"] >= int(getattr(settings, "CROSS_FARB_EXIT_CONFIRM_STEPS", 3))
            else:
                pos["exit_streak"] = 0
                pos.pop("exit_streak_reason", None)
                do_close = False
            if do_close:
                realized = round(
                    float(pos["funding_accrued_usdt"])
                    + float(pos["unrealized_basis_usdt"])
                    - float(pos["fees_round_trip_usdt"]),
                    6,
                )
                closed = {
                    **pos,
                    "closed_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
                    "close_reason": reason,
                    "close_basis_pct": item.get("price_diff_pct"),
                    "realized_usdt": realized,
                }
                state["closed"].append(closed)
                state["realized_total_usdt"] = round(float(state["realized_total_usdt"]) + realized, 6)
                actions.append({"action": "close", "symbol": pos["symbol"], "reason": reason, "realized_usdt": realized})
            else:
                still_open.append(pos)
        state["open"] = still_open

        # 2) новые входы
        max_pos = int(getattr(settings, "CROSS_FARB_MAX_POSITIONS", 2))
        if len(state["open"]) < max_pos:
            lookback_days = int(getattr(settings, "CROSS_FARB_LOOKBACK_DAYS", 1))
            try:
                hist_rows = {r["symbol"]: r for r in self.history.history(days=lookback_days).get("by_symbol", [])}
            except Exception:  # noqa: BLE001
                hist_rows = {}
            # (#cross-farb-exit-confirm-2026-07-22) Кулдаун перезахода: после
            # закрытия по сжатию/флипу тот же символ не открываем N часов —
            # вторая половина защиты от чурна комиссий.
            cooldown_sec = float(getattr(settings, "CROSS_FARB_REENTRY_COOLDOWN_HOURS", 6.0)) * 3600
            last_closed_ts: dict[str, float] = {}
            for closed_pos in state["closed"][-50:]:
                try:
                    ts = datetime.fromisoformat(str(closed_pos.get("closed_at"))).timestamp()
                    sym = str(closed_pos.get("symbol"))
                    last_closed_ts[sym] = max(last_closed_ts.get(sym, 0.0), ts)
                except Exception:  # noqa: BLE001
                    continue
            open_symbols = {p["symbol"] for p in state["open"]}
            candidates = [
                items[s] for s in self._symbols()
                if s in items
                and s not in open_symbols
                and (now - last_closed_ts.get(s, 0.0)) >= cooldown_sec
            ]
            candidates.sort(
                key=lambda i: abs(float((i.get("spread") or {}).get("spread_annualized_pct") or 0.0)),
                reverse=True,
            )
            for item in candidates:
                if len(state["open"]) >= max_pos:
                    break
                allowed, why = entry_allowed(item, hist_rows.get(item["symbol"]))
                if not allowed:
                    continue
                notional = float(getattr(settings, "CROSS_FARB_NOTIONAL_USDT", 100.0))
                spread = item["spread"]
                pos = {
                    "id": uuid.uuid4().hex[:12],
                    "symbol": item["symbol"],
                    "direction": spread.get("direction"),
                    "notional_usdt": notional,
                    "opened_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
                    "opened_ts": now,
                    "entry_spread_ann_pct": spread.get("spread_annualized_pct"),
                    "entry_basis_pct": item.get("price_diff_pct"),
                    "htx_entry_mark": item.get("htx_mark"),
                    "kraken_entry_mark": item.get("kraken_mark"),
                    "fees_round_trip_usdt": round_trip_fees_usdt(notional),
                    "funding_accrued_usdt": 0.0,
                    "unrealized_basis_usdt": 0.0,
                    "last_accrual_ts": now,
                    "mode": "paper",
                }
                state["open"].append(pos)
                actions.append({
                    "action": "open",
                    "symbol": pos["symbol"],
                    "direction": pos["direction"],
                    "spread_ann_pct": pos["entry_spread_ann_pct"],
                    "notional_usdt": notional,
                })

        self.store.save(state)
        return {
            "status": "ok",
            "actions": actions,
            "open_count": len(state["open"]),
            "closed_count": len(state["closed"]),
            "realized_total_usdt": state["realized_total_usdt"],
        }

    def summary(self) -> dict:
        """Для GET /venues/cross-arb: состояние движка + текущая экономика."""
        state = self.store.load()
        open_positions = []
        for pos in state["open"]:
            unreal = round(
                float(pos.get("funding_accrued_usdt") or 0.0)
                + float(pos.get("unrealized_basis_usdt") or 0.0)
                - float(pos.get("fees_round_trip_usdt") or 0.0),
                6,
            )
            open_positions.append({**pos, "unrealized_net_usdt": unreal})
        closed_tail = state["closed"][-20:]
        return {
            "status": "ok",
            "enabled": bool(getattr(settings, "CROSS_FARB_ENABLED", False)),
            "mode": "paper",
            "symbols": self._symbols(),
            "gates": {
                "min_ann_pct": float(getattr(settings, "CROSS_FARB_MIN_ANN_PCT", 12.0)),
                "min_stability_pct": float(getattr(settings, "CROSS_FARB_MIN_STABILITY_PCT", 80.0)),
                "close_ann_pct": float(getattr(settings, "CROSS_FARB_CLOSE_ANN_PCT", 3.0)),
                "max_hold_days": float(getattr(settings, "CROSS_FARB_MAX_HOLD_DAYS", 14.0)),
                "notional_usdt": float(getattr(settings, "CROSS_FARB_NOTIONAL_USDT", 100.0)),
                "max_positions": int(getattr(settings, "CROSS_FARB_MAX_POSITIONS", 2)),
            },
            "open": open_positions,
            "closed_recent": closed_tail,
            "closed_count": len(state["closed"]),
            "realized_total_usdt": state["realized_total_usdt"],
            "note": "PAPER. Carry почасовой от текущего спреда (HTX 8ч амортизируется), базис mark-to-market, комиссии — тейкер обеих ног вход+выход.",
        }
