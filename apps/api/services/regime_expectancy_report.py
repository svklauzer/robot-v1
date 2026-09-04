"""Матожидание по режимам: что гейт требует против того, что режим даёт
(#regime-expectancy-report-2026-09-04).

Зачем
-----
04.09 система сутки не открыла ни одной сделки. Главный блокер —
`tp2_reached_too_rarely`: гейт требует, чтобы доля сделок с MFE до TP2 была не
ниже `1/(1+RR)`. У XRP это 33.7% при измеренных 7.7%.

Но `1/(1+RR)` — безубыточная частота для ставки «всё или ничего», а мы так не
торгуем: на TP1 фиксируется половина позиции, после TP1 стоп переносится в
безубыток, защитные выходы банкуют часть хода. Сделка, дошедшая до TP1 и
вернувшаяся, — маленький плюс, а не полный минус. Докстринг tp_reachability это
признаёт прямо: «частичная фиксация на TP1 и стопы, которые не всегда
добираются, в неё не входят».

То есть гейт требует окупаемости по модели, по которой мы не торгуем. Прежде
чем двигать пороги, надо увидеть ФАКТ.

Что считается
-------------
    expectancy_r = Σ closed_net_pnl / Σ |net_pnl_stop|

`closed_net_pnl` — итог сделки после комиссий и проскальзывания, включая
частичную фиксацию TP1. `net_pnl_stop` — плановый убыток на стопе, то есть 1R
этой сделки. Отношение даёт средний результат в R.

Сделки без планового риска исключаются из ОБЕИХ сумм: молча считать их нулевыми
значит занизить знаменатель и раздуть ожидание (та же оговорка, что в
regime_expectancy_sizer).

Отличие от сайзера
------------------
Сайзер прячет режимы с выборкой меньше REGIME_EXP_MIN_HISTORY, потому что
принимает по ним РЕШЕНИЕ. Здесь решений нет — только показания приборов, и
маленькая выборка это тоже показание. Она отдаётся с честным размером, чтобы
читатель судил сам.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from models.signal import Signal
from services.phantom_fill import phantom_adjustment

# (#regime-expectancy-honest-2026-09-04) Доля пути до TP2, на которой ветка
# `tp2_reached` реально закрывает сделку (exit_policy: current_pct >= tp2*0.92),
# книжа при этом ПОЛНУЮ цену TP2. Отсюда расхождение, которое иначе выглядит
# необъяснимым: 11 сделок закрыты как tp2_reached, а MFE дотянулся до цели у
# одной. Разрыв между двумя измерениями ниже — это и есть зона наценки.
TP2_TRIGGER_SHARE = 0.92


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _f(value) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _bootstrap_ci(pairs: list[tuple[float, float]], *, iters: int = 2000,
                  seed: int = 20260904) -> tuple[float | None, float | None]:
    """95% интервал для Σnet/Σrisk бутстрапом по сделкам.

    (#expectancy-ci-2026-09-04) Точечная оценка на 40–50 сделках не отличает
    убыток от нуля, и без интервала легко объявить находкой то, что является
    разбросом. В коде уже записан прецедент: 30.07 грейд измерили как
    A +0.090R [−0.210; +0.434] против B −0.070R [−0.181; +0.048] — оба
    интервала накрывают ноль, ось ничего не предсказывает. Сейчас та же ось
    показывает противоположный знак, что для шума нормально. Чтобы это было
    видно, а не выводилось на глаз, интервал считается здесь.

    Бутстрап, а не нормальное приближение: распределение результата сделки
    имеет тяжёлый хвост (редкие крупные плюсы), и симметричный интервал вокруг
    среднего ему не подходит.

    Seed фиксирован: один и тот же набор сделок обязан давать один и тот же
    интервал, иначе отчёт выглядит пляшущим и ему перестают верить.
    """
    if len(pairs) < 5:
        return None, None

    rng = random.Random(seed)
    n = len(pairs)
    samples: list[float] = []
    for _ in range(iters):
        net_sum = 0.0
        risk_sum = 0.0
        for _ in range(n):
            net, risk = pairs[rng.randrange(n)]
            net_sum += net
            risk_sum += risk
        if risk_sum > 0:
            samples.append(net_sum / risk_sum)

    if len(samples) < iters // 2:
        return None, None

    samples.sort()
    lo = samples[int(0.025 * len(samples))]
    hi = samples[min(int(0.975 * len(samples)), len(samples) - 1)]
    return round(lo, 4), round(hi, 4)


def _dist_pct(entry: float | None, level: float | None) -> float | None:
    """Дистанция до уровня в процентах входа — та же величина, по которой
    tp_reachability сравнивает MFE."""
    if not entry or entry <= 0 or not level or level <= 0:
        return None
    return abs(level - entry) / entry * 100.0


def build(db: Session, *, window_hours: float = 720.0, bot_id: int | None = None,
          max_rows: int = 4000) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=float(window_hours))

    query = db.query(Signal).filter(
        Signal.status == "closed",
        Signal.closed_at.isnot(None),
        Signal.closed_net_pnl.isnot(None),
    )
    if cutoff is not None:
        query = query.filter(Signal.closed_at >= cutoff)
    if bot_id is not None:
        query = query.filter(Signal.bot_id == bot_id)

    rows = query.order_by(Signal.id.desc()).limit(int(max_rows)).all()

    buckets: dict[str, list[Signal]] = {}
    for signal in rows:
        regime = str((signal.plan_json or {}).get("regime") or "").strip() or "unknown"
        buckets.setdefault(regime, []).append(signal)

    regimes = [_regime_row(name, items) for name, items in buckets.items()]
    regimes.sort(key=lambda r: r["sample"], reverse=True)

    # (#expectancy-ci-2026-09-04) Грейд — ось, по которой раздаются пороги
    # входа и срок жизни сигнала. 30.07 её уже измеряли и признали
    # непредсказывающей (оба интервала накрывали ноль). Считаем ту же величину
    # тем же способом, чтобы сравнение было like-for-like, а не «на глаз».
    grade_buckets: dict[str, list[Signal]] = {}
    for signal in rows:
        grade = str(signal.grade or "unknown").upper()
        grade_buckets.setdefault(grade, []).append(signal)
    grades = [_regime_row(name, items) for name, items in grade_buckets.items()]
    grades.sort(key=lambda r: r["sample"], reverse=True)
    for row in grades:
        row["grade"] = row.pop("regime")

    return {
        "window_hours": float(window_hours),
        "closed_signals": len(rows),
        "regimes": regimes,
        "grades": grades,
        "note": (
            "expectancy_r = Σ closed_net_pnl / Σ |net_pnl_stop| — средний "
            "результат сделки в R, издержки и частичная фиксация TP1 уже "
            "внутри. tp_reach_required — что требует гейт входа "
            "(1/(1+RR), модель «всё или ничего»); tp2_reach_realized — как "
            "часто MFE реально доходил до TP2. Их расхождение и есть цена "
            "модели, по которой мы не торгуем."
        ),
    }


def _regime_row(regime: str, signals: list[Signal]) -> dict:
    net_total = 0.0
    net_total_raw = 0.0
    risk_total = 0.0
    counted = 0
    wins = 0
    losses = 0
    phantom_count = 0
    phantom_delta = 0.0

    pairs: list[tuple[float, float]] = []
    by_reason: dict[str, dict] = {}
    mfe_values: list[float] = []
    rr_values: list[float] = []
    tp1_reached = 0
    tp2_reached = 0
    tp2_triggered = 0
    reach_measured = 0

    for s in signals:
        plan = s.plan_json or {}
        lifecycle = plan.get("lifecycle") or {}

        risk = abs(_f(s.net_pnl_stop) or 0.0)
        net_raw = _f(s.closed_net_pnl)

        # (#regime-expectancy-honest-2026-09-04) Фантомный филл: записанный
        # result_pct выше, чем сделка вообще доходила по MFE — исполнение
        # лучше рынка. Ветка tp2_reached делает это систематически, закрывая
        # на 92% пути и книжа полную цель. Считать матожидание по сырому
        # closed_net_pnl значит мерить край по прибыли, которой не было.
        # Дашборд эту поправку уже применяет (total_net_pnl_honest_usdt) —
        # первая версия этого отчёта её потеряла.
        is_phantom, adjustment = phantom_adjustment(s)
        net = (net_raw + adjustment) if (net_raw is not None and is_phantom) else net_raw

        if risk > 0 and net is not None:
            net_total += net
            net_total_raw += (net_raw or 0.0)
            risk_total += risk
            pairs.append((net, risk))
            counted += 1
            if is_phantom:
                phantom_count += 1
                phantom_delta += adjustment
            if net > 0:
                wins += 1
            elif net < 0:
                losses += 1

        reason = str(s.closed_reason or "unknown")
        slot = by_reason.setdefault(reason, {"n": 0, "net_usdt": 0.0, "phantom": 0})
        slot["n"] += 1
        if net is not None:
            slot["net_usdt"] = round(slot["net_usdt"] + net, 6)
        if is_phantom:
            slot["phantom"] += 1

        rr = _f(s.net_rr_tp2)
        if rr and rr > 0:
            rr_values.append(rr)

        # Достижимость: сравниваем MFE с плановыми дистанциями ЭТОЙ сделки.
        mfe = _f(lifecycle.get("mfe_pct"))
        entry = _f(lifecycle.get("entry_price"))
        tp = s.tp_json or {}
        d1 = _dist_pct(entry, _f(tp.get("tp1")))
        d2 = _dist_pct(entry, _f(tp.get("tp2")))
        if mfe is not None and d1 and d2:
            reach_measured += 1
            mfe_values.append(mfe)
            if mfe >= d1:
                tp1_reached += 1
            if mfe >= d2:
                tp2_reached += 1
            # Порог, на котором ветка tp2_reached закрывает НА САМОМ ДЕЛЕ.
            if mfe >= d2 * TP2_TRIGGER_SHARE:
                tp2_triggered += 1

    expectancy_r = (net_total / risk_total) if risk_total > 0 else None
    ci_low, ci_high = _bootstrap_ci(pairs)
    # Отличим ли результат от нуля вообще. Без этого точечная оценка на
    # четырёх десятках сделок читается как факт, которым не является.
    significant = (
        None if (ci_low is None or ci_high is None)
        else not (ci_low <= 0.0 <= ci_high)
    )
    median_rr = _median(rr_values)
    required = (1.0 / (1.0 + median_rr)) if median_rr else None
    tp2_realized = (tp2_reached / reach_measured) if reach_measured else None

    # Главное сопоставление: гейт говорит «не окупится», факт говорит своё.
    verdict = "no_data"
    if expectancy_r is not None and required is not None and tp2_realized is not None:
        gate_would_block = tp2_realized < required
        if gate_would_block and expectancy_r > 0:
            verdict = "gate_blocks_but_regime_is_profitable"
        elif gate_would_block and expectancy_r <= 0:
            verdict = "gate_blocks_and_regime_loses"
        elif expectancy_r > 0:
            verdict = "gate_passes_and_regime_is_profitable"
        else:
            verdict = "gate_passes_but_regime_loses"

    return {
        "regime": regime,
        "sample": counted,
        "wins": wins,
        "losses": losses,
        "winrate_pct": round(wins / counted * 100, 2) if counted else None,
        "net_pnl_usdt": round(net_total, 6),
        "net_pnl_usdt_raw": round(net_total_raw, 6),
        "phantom_fills": phantom_count,
        "phantom_overstatement_usdt": round(abs(phantom_delta), 6),
        "risk_usdt": round(risk_total, 6),
        "expectancy_r": round(expectancy_r, 4) if expectancy_r is not None else None,
        "expectancy_r_ci": [ci_low, ci_high],
        "significant": significant,
        "expectancy_r_raw": (
            round(net_total_raw / risk_total, 4) if risk_total > 0 else None
        ),
        "expectancy_usdt_per_trade": round(net_total / counted, 4) if counted else None,
        "median_net_rr_tp2": round(median_rr, 4) if median_rr else None,
        "tp_reach_required": round(required, 4) if required else None,
        "tp1_reach_realized": round(tp1_reached / reach_measured, 4) if reach_measured else None,
        "tp2_reach_realized": round(tp2_realized, 4) if tp2_realized is not None else None,
        # Доля, дошедшая до порога, на котором tp2_reached закрывает фактически.
        "tp2_trigger_realized": (
            round(tp2_triggered / reach_measured, 4) if reach_measured else None
        ),
        "median_mfe_pct": round(_median(mfe_values), 4) if mfe_values else None,
        "reach_measured": reach_measured,
        "verdict": verdict,
        "by_close_reason": dict(sorted(
            by_reason.items(), key=lambda kv: kv[1]["net_usdt"]
        )),
    }
