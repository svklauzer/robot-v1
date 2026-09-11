"""Динамические SL/TP на свечах истории (#dynamic-levels-2026-09-12).

Владелец (11.09): динамические уровни — это когда сделка входит с начальными
SL/TP1/TP2, а дальше система по меняющейся картине ДВИГАЕТ их: тренд жив —
стоп и цели в сторону тренда, тренд разворачивается — меняются и цель, и стоп.
Сейчас уровни живут только при входе; в жизни сделки стоп двигается дважды
(на TP1 и храповиком после TP2).

Правила движения уровней проверяются здесь до всякого включения: по каждой
закрытой сделке берутся 15-минутные свечи за её жизнь (и дальше — на случай,
если правило держит дольше, чем держали мы), и на одном и том же пути цены
проигрываются правила:

  current      нынешняя схема: стоп, половина на TP1, стоп остатка на TP1,
               остаток — на TP2 (якорь сравнения);
  atr_trail    как current до TP1, дальше остаток под трейлом k·ATR от
               максимума закрытия (не ниже TP1), без потолка TP2;
  chandelier   без частичной фиксации: стоп = max(начальный, максимум − k·ATR),
               выход только по стопу — чистое следование тренду;
  kama         как current до TP1, остаток выходит на закрытии за KAMA(10,2,30);
  be_atr       стоп в безубыток после +k·ATR, дальше трейл 2·ATR, половина на TP1.

Внутри свечи неизвестно, что было раньше — максимум или минимум. Поэтому два
режима: pessimistic (при одновременном касании сначала стоп) и optimistic
(сначала цель). Правда — между ними.

Индикаторы считаются только по закрытым свечам ДО текущей — без заглядывания
вперёд. Вход — по цене входа сделки, со следующей после входа свечи. Издержки —
круг самой сделки, один раз на сделку. Свечи — своп OKX для всех сделок: цены
бирж отличаются на сотые доли процента, пути одинаковы.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

BAR_SEC = 15 * 60
ATR_PERIOD = 14


@dataclass
class Trade:
    id: int
    symbol: str
    side: str
    entry: float
    stop_dist_pct: float
    tp1_pct: float
    tp2_pct: float
    opened_ts: float
    closed_ts: float
    cost_pct: float
    actual_pct: float
    trade_mode: str = "unknown"
    reason: str = "unknown"


@dataclass
class Exit:
    share: float
    pct: float
    reason: str


@dataclass
class Result:
    exits: list[Exit] = field(default_factory=list)

    def gross_pct(self) -> float:
        return sum(e.share * e.pct for e in self.exits)


# ── загрузка сделок из выгрузки /signals ────────────────────────────────────

def _num(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _ts(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _side_pct(side: str, entry: float, price: float) -> float:
    raw = (price - entry) / entry * 100.0
    return raw if str(side).lower() == "long" else -raw


def trade_from_item(item: dict) -> Trade | None:
    """Сделка из элемента выгрузки /signals. None — данных для проигрыша нет."""
    if item.get("status") != "closed":
        return None
    plan = item.get("plan") or {}
    lc = plan.get("lifecycle") or {}
    entry = _num(lc.get("entry_price"))
    opened, closed = _ts(lc.get("first_seen_at")), _ts(lc.get("closed_at"))
    qty = _num(item.get("qty")) or _num(plan.get("qty"))
    tp = item.get("tp") or {}
    tp1, tp2 = _num(tp.get("tp1")), _num(tp.get("tp2"))
    risk = _num(item.get("net_pnl_stop"))
    cost = _num(item.get("closed_total_cost"))
    net = _num(item.get("closed_net_pnl"))
    if None in (entry, opened, closed, qty, tp1, tp2, risk, net) or entry <= 0 or qty <= 0:
        return None
    notional = qty * entry
    cost_pct = (cost / notional * 100.0) if cost else 0.14
    stop_dist = (abs(risk) - notional * cost_pct / 100.0) / notional * 100.0
    if stop_dist <= 0:
        return None

    # Честный факт: фантомная наценка tp2_reached снимается тем же правилом,
    # что в отчётах.
    from services.phantom_fill import phantom_adjustment

    shim = SimpleNamespace(result_pct=item.get("result_pct"), plan_json=plan,
                           required_margin=item.get("required_margin"))
    is_phantom, adj = phantom_adjustment(shim)
    honest = net + adj if is_phantom else net

    return Trade(
        id=int(item["id"]), symbol=str(item["symbol"]), side=str(item["side"]).lower(),
        entry=entry, stop_dist_pct=stop_dist,
        tp1_pct=_side_pct(item["side"], entry, tp1), tp2_pct=_side_pct(item["side"], entry, tp2),
        opened_ts=opened, closed_ts=closed, cost_pct=cost_pct,
        actual_pct=honest / notional * 100.0,
        trade_mode=str(plan.get("trade_mode") or "unknown"),
        reason=str(item.get("closed_reason") or "unknown"),
    )


def load_trades(paths: list[Path]) -> list[Trade]:
    seen, out = set(), []
    for path in paths:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        for item in payload.get("items", []):
            trade = trade_from_item(item)
            if trade and trade.id not in seen and trade.tp1_pct > 0:
                seen.add(trade.id)
                out.append(trade)
    return sorted(out, key=lambda t: t.opened_ts)


# ── свечи: путь цены сделки в процентах в её сторону ────────────────────────

def path_pct(trade: Trade, candles: list[list[float]]) -> list[tuple[float, float, float, float]]:
    """Свечи [ts_ms, o, h, l, c] → (open, fav, adv, close) в % от входа в сторону
    сделки: fav — лучшая точка свечи, adv — худшая. Для шорта максимум цены —
    худшая точка."""
    out = []
    for _, o, h, l, c, *_ in candles:
        po, ph, pl, pc = (_side_pct(trade.side, trade.entry, x) for x in (o, h, l, c))
        out.append((po, max(ph, pl), min(ph, pl), pc))
    return out


def atr_pct(path: list[tuple[float, float, float, float]], period: int = ATR_PERIOD) -> list[float | None]:
    """ATR в % от входа по закрытым свечам ДО текущей: значение для бара i
    считается по барам < i — без заглядывания вперёд."""
    trs, out = [], []
    prev_close = None
    for i, (o, fav, adv, c) in enumerate(path):
        out.append(sum(trs[-period:]) / period if len(trs) >= period else None)
        tr = fav - adv
        if prev_close is not None:
            tr = max(tr, abs(fav - prev_close), abs(adv - prev_close))
        trs.append(tr)
        prev_close = c
    return out


# ── правила ─────────────────────────────────────────────────────────────────

def _first_touch(fav: float, adv: float, target: float | None, stop: float,
                 pessimistic: bool) -> str | None:
    hit_stop = adv <= stop
    hit_target = target is not None and fav >= target
    if hit_stop and hit_target:
        return "stop" if pessimistic else "target"
    if hit_stop:
        return "stop"
    if hit_target:
        return "target"
    return None


def replay(trade: Trade, path: list[tuple[float, float, float, float]], policy: str, *,
           pessimistic: bool = True, k: float = 2.0, kama: list[float | None] | None = None,
           atr: list[float | None] | None = None, partial: float = 0.5) -> Result:
    """Проигрыш одного правила по пути сделки. Путь начинается со свечи ПОСЛЕ
    входа; конец пути — ограничение удержания (выход по закрытию). `atr` —
    готовый ряд с прогревом на свечах до входа; без него ATR считается по
    самому пути и первые 14 свечей его нет."""
    atr = atr if atr is not None else atr_pct(path)
    stop = -trade.stop_dist_pct
    res = Result()
    rest = 1.0
    tp1_done = policy == "chandelier"          # у chandelier частичной нет
    peak_close = 0.0
    be_armed = False
    for i, (o, fav, adv, c) in enumerate(path):
        # ── до TP1 ──────────────────────────────────────────────────────────
        if not tp1_done:
            if policy == "be_atr" and not be_armed and atr[i] and fav >= k * atr[i]:
                be_armed = True
            eff_stop = max(stop, trade.cost_pct) if be_armed else stop
            touch = _first_touch(fav, adv, trade.tp1_pct, eff_stop, pessimistic)
            if touch == "stop":
                res.exits.append(Exit(rest, eff_stop, "stop"))
                return res
            if touch == "target":
                res.exits.append(Exit(partial, trade.tp1_pct, "tp1"))
                rest -= partial
                tp1_done = True
                stop = trade.tp1_pct                     # стоп остатка на TP1
                peak_close = max(peak_close, c)
                # TP2 в той же свече — по порядку не узнать: только в оптимистичном.
                if policy in ("current",) and not pessimistic and fav >= trade.tp2_pct:
                    res.exits.append(Exit(rest, trade.tp2_pct, "tp2"))
                    return res
                continue
            continue

        # ── после TP1 (или весь путь у chandelier) ─────────────────────────
        target = trade.tp2_pct if policy == "current" else None
        touch = _first_touch(fav, adv, target, stop, pessimistic)
        if touch == "stop":
            res.exits.append(Exit(rest, stop, "stop_after_tp1" if policy != "chandelier" else "trail"))
            return res
        if touch == "target":
            res.exits.append(Exit(rest, target, "tp2"))
            return res
        # Закрытие свечи: обновить уровни на следующий бар.
        peak_close = max(peak_close, c)
        if policy in ("atr_trail", "chandelier", "be_atr") and atr[i]:
            mult = 2.0 if policy == "be_atr" else k
            stop = max(stop, peak_close - mult * atr[i])
        if policy == "kama" and kama is not None and kama[i] is not None and c < kama[i]:
            res.exits.append(Exit(rest, c, "kama_close"))
            return res

    last_close = path[-1][3] if path else 0.0
    res.exits.append(Exit(rest, last_close, "time_cap"))
    return res


def kama_pct(trade: Trade, candles: list[list[float]]) -> list[float | None]:
    """KAMA(10,2,30) по закрытиям — в % от входа в сторону сделки; значение для
    бара i посчитано по барам ≤ i−1."""
    from services.kama import kama_series

    closes = [float(c[4]) for c in candles]
    series = kama_series(closes)
    out: list[float | None] = [None]
    for v in series[:-1]:
        out.append(None if v is None or (isinstance(v, float) and math.isnan(v))
                   else _side_pct(trade.side, trade.entry, float(v)))
    return out


def net_pct(trade: Trade, result: Result) -> float:
    return result.gross_pct() - trade.cost_pct
