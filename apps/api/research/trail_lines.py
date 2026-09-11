"""Линии трейла после TP1: KAMA против VWAP и их сочетания (#trail-lines-2026-09-11).

Вопрос владельца (11.09): чем вести сделку после TP1 вместо потолка TP2 — KAMA,
VWAP или сигналом обеих линий сразу.

Чем линии различаются по устройству
-----------------------------------
KAMA(10,2,30) смотрит только на цену и меняет скорость по эффективности хода:
на прямом движении почти догоняет цену (сглаживание ~2 бара), на топтании почти
стоит (~30). Объём ей неизвестен — тихий откат и обвал на объёме для неё одно
и то же.

VWAP — средняя цена, взвешенная объёмом, то есть средняя цена входа всех, кто
торговал с точки привязки. Скорость у неё не адаптивная, а накопительная: чем
больше объёма прошло с привязки, тем медленнее она двигается. Закрытие за VWAP
значит «средний участник с точки привязки уже в минусе». Что это за участники,
решает привязка:

  session      сброс в 00:00 UTC. Классический внутридневной VWAP, но у крипты
               нет сессии: граница суток для сделки — случайный момент, в начале
               суток линия дёргается за ценой, к концу почти стоит;
  roll20       скользящее окно 20 свечей 15m (5 ч) — как признак vwap_dist в
               ml_market_research; по сути SMA, взвешенная объёмом;
  avwap_entry  привязка к входу: средняя цена всех, кто торговал с нашего входа;
  avwap_tp1    привязка к касанию TP1: средняя цена продолжения. Стартует ровно
               у уровня фиксации и дальше идёт за ценой с весом объёма.

Сочетания
---------
  and   выход, только когда закрытие за обеими линиями: пробой KAMA должен
        подтвердиться объёмом. Меньше ложных выходов, но выход позже;
  or    выход по первой пробитой линии: плотнее, больше ложных выходов;
  kama15_2bar  контроль к «and»: пробой KAMA подтверждается второй свечой, а не
        объёмом. Если «and» выигрывает у KAMA, а контроль выигрывает так же, то
        дело в самой задержке выхода, а не в информации объёма.

Геометрия, в которой линии работают
-----------------------------------
После TP1 стоп всей позиции стоит на TP1 (живая схема с 11.09). Линия ниже
этого уровня не действует вовсе: сначала сработает фиксация. Поэтому медленная
линия (avwap_entry, session к концу суток) у коротких сделок — пустое место, и
стенд это показывает долей свечей, где линия выше фиксации.

Формы
-----
  ride   после TP1 вся позиция едет по линии, TP2 нет;
  tp2    как живая схема до TP2 (половина на TP2), хвост — по линии вместо
         храповика с отдачей; пол хвоста — TP2 минус буфер, как в живой.
  live   эталон: живая схема — стоп на TP1, половина на TP2, хвост под
         храповиком max(0.20%, ½·(TP2−TP1)) от пика и выход при отдаче 40%
         прироста сверх TP2 (signal_lifecycle._manage_tp2_trail,
         exit_policy.after_tp2_decision).

Все линии считаются по свечам ДО текущей: значение для свечи i посчитано по
свечам ≤ i−1, решение — по закрытию свечи i. Пути цены — в % от входа в сторону
сделки (research.candle_replay.path_pct), поэтому «закрытие ниже линии» значит
«против сделки» для обеих сторон.
"""
from __future__ import annotations

from dataclasses import dataclass

from research.candle_replay import Exit, Result, Trade, _first_touch, _side_pct

DAY_MS = 86_400_000
HOUR_MS = 3_600_000

# Живая схема этапа TP2 — те же значения, что в core/config.py.
LIVE_TP2_SHARE = 0.5
LIVE_LEG_SHARE = 0.5
LIVE_MIN_BUFFER = 0.20
LIVE_GIVEBACK_SHARE = 0.40


# ── линии ───────────────────────────────────────────────────────────────────

def typical(path: list[tuple[float, float, float, float]]) -> list[float]:
    """Типичная цена свечи (H+L+C)/3 в % от входа. Перевод цены в проценты
    линейный, поэтому VWAP процентов = проценты VWAP."""
    return [(fav + adv + c) / 3.0 for _, fav, adv, c in path]


def rolling_vwap(typ: list[float], vol: list[float], n: int) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(typ)):
        lo = max(0, i - n)
        v = sum(vol[lo:i])
        out.append(sum(t * w for t, w in zip(typ[lo:i], vol[lo:i])) / v if i - lo >= n and v > 0 else None)
    return out


def session_vwap(typ: list[float], vol: list[float], ts_ms: list[int]) -> list[float | None]:
    """VWAP текущих суток UTC по свечам до текущей. Первая свеча суток — None."""
    out: list[float | None] = []
    pv = v = 0.0
    day = None
    for i, ts in enumerate(ts_ms):
        d = ts // DAY_MS
        if d != day:
            day, pv, v = d, 0.0, 0.0
        out.append(pv / v if v > 0 else None)
        pv += typ[i] * vol[i]
        v += vol[i]
    return out


def anchored_vwap(typ: list[float], vol: list[float], anchor: int) -> list[float | None]:
    """VWAP со свечи `anchor` (включительно) по свечи i−1."""
    out: list[float | None] = []
    pv = v = 0.0
    for i in range(len(typ)):
        out.append(pv / v if i > anchor and v > 0 else None)
        if i >= anchor:
            pv += typ[i] * vol[i]
            v += vol[i]
    return out


def kama_hourly(trade: Trade, candles: list[list[float]]) -> list[float | None]:
    """KAMA(10,2,30) часовых закрытий — как у живого выхода по ТЗ (TZ_TREND_TF=1h),
    но без буфера 2·ATR: здесь это линия трейла, а не аварийный уровень.

    Часы собираются из 15m свечей; для свечи i берётся KAMA последнего часа,
    закрывшегося не позже её начала."""
    from services.kama import kama_series

    hours: dict[int, float] = {}
    for ts, _o, _h, _l, c, *_ in candles:
        hours[int(ts) // HOUR_MS] = float(c)       # последняя 15m свеча часа — его закрытие
    keys = sorted(hours)
    series = kama_series([hours[k] for k in keys])
    by_hour = {k: v for k, v in zip(keys, series)}

    out: list[float | None] = []
    for ts, *_ in candles:
        last_closed = int(ts) // HOUR_MS - 1         # час, закрывшийся к началу свечи
        v = by_hour.get(last_closed)
        out.append(None if v is None else _side_pct(trade.side, trade.entry, float(v)))
    return out


# ── правила ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Rule:
    name: str
    lines: tuple[str, ...]            # ключи рядов линий; "avwap_tp1" считается по ходу
    combine: str = "single"           # single | and | or
    confirm: int = 1                  # закрытий подряд за линией
    shape: str = "ride"               # ride | tp2 | live


@dataclass
class TrailResult(Result):
    tp1_bar: int | None = None
    exit_bar: int | None = None
    reason: str = ""
    peak: float | None = None                     # лучшая точка после TP1 до выхода
    binding_bars: int = 0                         # свечей, где линия выше фиксации
    watched_bars: int = 0


def _broken(rule: Rule, values: list[float | None], close: float) -> bool:
    hits = [close < v for v in values if v is not None]
    if not hits:
        return False
    if rule.combine == "and":
        return len(hits) == len(values) and all(hits)
    return any(hits)


def replay_trail(trade: Trade, path: list[tuple[float, float, float, float]], rule: Rule, *,
                 lines: dict[str, list[float | None]] | None = None,
                 vol: list[float] | None = None, pessimistic: bool = True,
                 stop_slip: float = 0.0) -> TrailResult:
    """Проигрыш по пути сделки. До TP1 — начальный стоп; на TP1 стоп всей позиции
    переносится на TP1 (фиксации на TP1 нет — как в живой схеме с 11.09)."""
    lines = lines or {}
    vol = vol if vol is not None else [1.0] * len(path)
    typ = typical(path)
    res = TrailResult()
    stop = -trade.stop_dist_pct
    rest = 1.0
    stage = "pre"
    buffer = max(LIVE_MIN_BUFFER, LIVE_LEG_SHARE * (trade.tp2_pct - trade.tp1_pct))
    tail_peak = 0.0
    streak = 0
    av_pv = av_v = 0.0

    def close_rest(pct: float, reason: str, i: int) -> TrailResult:
        res.exits.append(Exit(rest, pct, reason))
        res.exit_bar, res.reason = i, reason
        return res

    for i, (o, fav, adv, c) in enumerate(path):
        if stage == "pre":
            touch = _first_touch(fav, adv, trade.tp1_pct, stop, pessimistic)
            if touch == "stop":
                return close_rest(stop - stop_slip, "stop", i)
            if touch == "target":
                stage, stop, res.tp1_bar, res.peak = "locked", trade.tp1_pct, i, fav
                av_pv, av_v = typ[i] * vol[i], vol[i]
                # TP2 в той же свече — по порядку не узнать: только в оптимистичном.
                if rule.shape in ("tp2", "live") and not pessimistic and fav >= trade.tp2_pct:
                    res.exits.append(Exit(rest * LIVE_TP2_SHARE, trade.tp2_pct, "tp2"))
                    rest *= 1 - LIVE_TP2_SHARE
                    stage, tail_peak = "tail", trade.tp2_pct
                    stop = max(stop, trade.tp2_pct - buffer)
            continue

        target = trade.tp2_pct if (rule.shape in ("tp2", "live") and stage == "locked") else None
        touch = _first_touch(fav, adv, target, stop, pessimistic)
        if touch == "stop":
            return close_rest(stop - stop_slip, "lock_stop" if stage == "locked" else "tail_stop", i)
        if touch == "target":
            res.exits.append(Exit(rest * LIVE_TP2_SHARE, trade.tp2_pct, "tp2"))
            rest *= 1 - LIVE_TP2_SHARE
            stage, tail_peak = "tail", trade.tp2_pct
            stop = max(stop, trade.tp2_pct - buffer)
            res.peak = max(res.peak, fav)
            av_pv += typ[i] * vol[i]
            av_v += vol[i]
            continue

        res.peak = max(res.peak, fav)
        avwap_tp1 = av_pv / av_v if av_v > 0 else None
        av_pv += typ[i] * vol[i]
        av_v += vol[i]

        if rule.shape == "live":
            if stage == "tail":
                tail_peak = max(tail_peak, fav)
                stop = max(stop, tail_peak - buffer)
                run = tail_peak - trade.tp2_pct
                if run > 0 and (tail_peak - c) >= LIVE_GIVEBACK_SHARE * run:
                    return close_rest(c, "tail_giveback", i)
            continue

        if (rule.shape == "ride" and stage == "locked") or (rule.shape == "tp2" and stage == "tail"):
            values = [avwap_tp1 if key == "avwap_tp1" else lines[key][i] for key in rule.lines]
            res.watched_bars += 1
            if any(v is not None and v > stop for v in values):
                res.binding_bars += 1
            streak = streak + 1 if _broken(rule, values, c) else 0
            if streak >= rule.confirm:
                return close_rest(c, "line", i)

    return close_rest(path[-1][3] if path else 0.0, "time_cap", len(path) - 1)


def after_exit(trade: Trade, path: list[tuple[float, float, float, float]],
               res: TrailResult) -> str:
    """Что было после выхода по линии: цена обновила пик раньше, чем вернулась
    к уровню фиксации (premature), вернулась к фиксации раньше нового пика
    (justified), или ни то ни другое до конца пути (open). Обе вещи в одной
    свече — порядок неизвестен (ambiguous)."""
    if res.reason != "line" or res.exit_bar is None or res.peak is None:
        return "n/a"
    for _, fav, adv, _ in path[res.exit_bar + 1:]:
        new_peak, back = fav > res.peak, adv <= trade.tp1_pct
        if new_peak and back:
            return "ambiguous"
        if new_peak:
            return "premature"
        if back:
            return "justified"
    return "open"

