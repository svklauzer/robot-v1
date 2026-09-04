"""Сколько стоит сделка, дошедшая до TP1 (#tp1-conditional-2026-09-04).

Зачем
-----
Гейт достижимости решает по одной формуле: `tp2_hit >= 1/(1+RR_tp2)`. Это точка
безубыточности БИНАРНОЙ ставки «взял цель или получил стоп». Наш выход давно не
бинарный: частичная фиксация на TP1, перенос стопа, трейл, ещё частичная на TP2,
трейл остатка. Модель гейта перестала соответствовать машине выхода.

Насколько перестала — видно на числах 04.09. У трендовых символов TP1 берётся в
15–33% случаев, TP2 — НИ РАЗУ (0 при запрошенных 15–26%). При этом RR до TP1
около 0.32–0.47 (XRP: стоп в 2.47%, TP1 в 0.80%), то есть бинарная формула
потребовала бы 68–76%. Сама нога TP1 убыточна; сделка окупается тем, что
зарабатывает трейл ПОСЛЕ неё. Гейт же проверяет фиксированную точку и не даёт
трейлу ни разу запуститься.

Что здесь считается
-------------------
Закрытые сделки делятся по признаку «дошла до TP1», и по каждой группе считается
ожидание в R. Из двух ожиданий выводится ЧЕСТНЫЙ порог — та частота достижения
TP1, при которой сделка выходит в ноль:

    p * E[R | дошла] + (1-p) * E[R | не дошла] = 0
    p_required = -E[не дошла] / (E[дошла] - E[не дошла])

Свободных параметров нет: обе ветви измерены, а не предположены. Это и есть
замена `1/(1+RR)`, где выигрыш и проигрыш ПОСТУЛИРОВАНЫ.

Два признака «дошла до TP1»
---------------------------
`actual` — реальная частичная фиксация (`plan_json.tp1_partial`), то, что
произошло на самом деле. `geometric` — `lifecycle.mfe_pct >= tp1_dist_pct`, то,
о чём рассуждает гейт. Считаются оба: расхождение между ними само по себе
диагноз (цена дошла, а фиксация не сработала).

Оговорка о выборке
------------------
Групп две, и меньшая из них — десятки сделок. Точечная оценка тут ничего не
значит, поэтому у каждой величины и у выведенного порога считается 95%
интервал бутстрапом. Если интервал порога накрывает всё от 0 до 1, замер
говорит «не знаю» — и это ответ, а не повод взять середину.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from models.signal import Signal
from services.phantom_fill import phantom_adjustment

_SEED = 20260904
_ITERS = 2000
_MIN_GROUP = 5


def _honest_net(signal: Signal) -> float:
    """Тот же честный PnL, что в regime_expectancy_report и stop_loss_forensics.

    Ветка `tp2_reached` книжит полную цену TP2, закрываясь на 92% пути, то есть
    исполняется лучше рынка. Наценка попадает только в группу дошедших до TP1 —
    там же, где мы измеряем выигрыш, — и без поправки ожидание этой ветви было
    бы завышено ровно в том месте, ради которого отчёт и написан.
    """
    try:
        net = float(signal.closed_net_pnl or 0.0)
    except (TypeError, ValueError):
        return 0.0
    is_phantom, adjustment = phantom_adjustment(signal)
    return net + adjustment if is_phantom else net


def _risk(signal: Signal) -> float | None:
    try:
        risk = abs(float(signal.net_pnl_stop or 0.0))
    except (TypeError, ValueError):
        return None
    return risk if risk > 0 else None


def _num(value) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _tp1_dist_pct(signal: Signal, plan: dict) -> float | None:
    """Дистанция до TP1 в процентах — из плана, иначе из геометрии сигнала."""
    from_plan = _num((plan.get("tp_reach") or {}).get("tp1_dist_pct"))
    if from_plan:
        return from_plan

    zone = signal.entry_zone_json if isinstance(signal.entry_zone_json, dict) else {}
    entry = _num(zone.get("from"))
    tp_json = signal.tp_json if isinstance(signal.tp_json, dict) else {}
    tp1 = _num(tp_json.get("tp1"))
    if not entry or not tp1 or entry <= 0:
        return None
    return abs(tp1 - entry) / entry * 100.0


def _reached(signal: Signal, plan: dict, marker: str) -> bool | None:
    if marker == "actual":
        return bool(plan.get("tp1_partial"))

    mfe = _num((plan.get("lifecycle") or {}).get("mfe_pct"))
    dist = _tp1_dist_pct(signal, plan)
    if mfe is None or dist is None:
        return None
    return mfe >= dist


def _expectancy(pairs: list[tuple[float, float]]) -> float | None:
    """Σnet/Σrisk — как в regime_expectancy_report. Взвешено по риску."""
    risk = sum(r for _, r in pairs)
    return (sum(n for n, _ in pairs) / risk) if risk > 0 else None


def _mean_r(pairs: list[tuple[float, float]]) -> float | None:
    """Среднее R ПО СДЕЛКАМ, а не по риску.

    (#tp1-weighting-2026-09-04) Порог выводится из уравнения
    p*E[дошла] + (1-p)*E[не дошла] = 0, где p — доля СДЕЛОК. Подставлять туда
    Σnet/Σrisk нельзя: это средневзвешенное по риску, и сравнивать полученный
    порог с долей по числу сделок значит сравнивать разные величины. На замере
    04.09 это стоило заметного искажения: у дошедших до TP1 риск на сделку
    1.572, у недошедших 1.144, так что доля по сделкам 0.271, а по риску 0.338.

    Для решения «брать ли ЭТУ сделку» правильна именно посделочная величина:
    вопрос не о вкладе в портфель, а об ожидании одного входа.
    """
    if not pairs:
        return None
    return sum(net / risk for net, risk in pairs) / len(pairs)


def _required_rate(e_reached: float | None, e_missed: float | None) -> float | None:
    """Частота достижения TP1, при которой сделка выходит в ноль."""
    if e_reached is None or e_missed is None:
        return None
    spread = e_reached - e_missed
    if spread <= 0:
        # Дошедшие до TP1 не лучше остальных — порога не существует, и это
        # утверждение сильнее любого числа: чинить надо не гейт.
        return None
    return -e_missed / spread


def _bootstrap(reached: list[tuple[float, float]],
               missed: list[tuple[float, float]]) -> dict:
    """Интервалы для обоих ожиданий и для выведенного из них порога.

    Порог бутстрапится ЦЕЛИКОМ, а не собирается из краёв двух интервалов: он
    нелинейная функция обеих ветвей, и интервал, склеенный из их границ, был бы
    шире правды — то есть позволил бы отмахнуться от результата.
    """
    if len(reached) < _MIN_GROUP or len(missed) < _MIN_GROUP:
        return {}

    rng = random.Random(_SEED)
    e_hit: list[float] = []
    e_miss: list[float] = []
    p_req: list[float] = []

    for _ in range(_ITERS):
        sample_hit = [reached[rng.randrange(len(reached))] for _ in range(len(reached))]
        sample_miss = [missed[rng.randrange(len(missed))] for _ in range(len(missed))]
        a, b = _mean_r(sample_hit), _mean_r(sample_miss)
        if a is None or b is None:
            continue
        e_hit.append(a)
        e_miss.append(b)
        required = _required_rate(a, b)
        if required is not None:
            p_req.append(required)

    def ci(values: list[float]) -> list[float | None]:
        if len(values) < _ITERS // 2:
            return [None, None]
        values.sort()
        return [round(values[int(0.025 * len(values))], 4),
                round(values[min(int(0.975 * len(values)), len(values) - 1)], 4)]

    return {
        "e_reached_ci": ci(e_hit),
        "e_missed_ci": ci(e_miss),
        "required_rate_ci": ci(p_req),
        # Доля выборок, где дошедшие вообще оказались лучше недошедших. Меньше
        # 0.95 — порог не установлен, каким бы ни было точечное значение.
        "spread_positive_share": round(len(p_req) / max(len(e_hit), 1), 4),
    }


def _group(pairs: list[tuple[float, float]], signals: list[Signal]) -> dict:
    by_reason: dict[str, dict] = {}
    for signal in signals:
        key = str(signal.closed_reason or "unknown")
        slot = by_reason.setdefault(key, {"n": 0, "net_usdt": 0.0})
        slot["n"] += 1
        slot["net_usdt"] = round(slot["net_usdt"] + _honest_net(signal), 4)

    expectancy = _expectancy(pairs)
    mean_r = _mean_r(pairs)
    return {
        "n": len(pairs),
        # Взвешенное по риску — для сопоставимости с остальными отчётами.
        "expectancy_r": round(expectancy, 4) if expectancy is not None else None,
        # Посделочное — из него и выводится порог.
        "mean_r": round(mean_r, 4) if mean_r is not None else None,
        "risk_usdt": round(sum(r for _, r in pairs), 4),
        "net_usdt": round(sum(n for n, _ in pairs), 4),
        "by_close_reason": dict(
            sorted(by_reason.items(), key=lambda kv: kv[1]["n"], reverse=True)
        ),
    }


def build(db: Session, *, window_hours: float = 720.0, marker: str = "geometric",
          max_rows: int = 4000) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=float(window_hours))

    rows = (
        db.query(Signal)
        .filter(Signal.status == "closed", Signal.closed_at.isnot(None),
                Signal.closed_at >= cutoff)
        .order_by(Signal.id.desc())
        .limit(int(max_rows))
        .all()
    )

    reached_pairs: list[tuple[float, float]] = []
    missed_pairs: list[tuple[float, float]] = []
    reached_signals: list[Signal] = []
    missed_signals: list[Signal] = []
    unusable = 0

    for signal in rows:
        plan = signal.plan_json or {}
        risk = _risk(signal)
        hit = _reached(signal, plan, marker)
        if risk is None or hit is None:
            unusable += 1
            continue
        pair = (_honest_net(signal), risk)
        if hit:
            reached_pairs.append(pair)
            reached_signals.append(signal)
        else:
            missed_pairs.append(pair)
            missed_signals.append(signal)

    e_reached = _mean_r(reached_pairs)
    e_missed = _mean_r(missed_pairs)
    required = _required_rate(e_reached, e_missed)
    total = len(reached_pairs) + len(missed_pairs)
    observed = (len(reached_pairs) / total) if total else None

    risk_reached = sum(r for _, r in reached_pairs)
    risk_total = risk_reached + sum(r for _, r in missed_pairs)
    observed_risk_share = (risk_reached / risk_total) if risk_total > 0 else None

    return {
        "window_hours": float(window_hours),
        "marker": marker,
        "closed": len(rows),
        "unusable": unusable,
        "reached_tp1": _group(reached_pairs, reached_signals),
        "missed_tp1": _group(missed_pairs, missed_signals),
        "observed_rate": round(observed, 4) if observed is not None else None,
        # Та же доля, но взвешенная по риску. Печатается рядом, потому что
        # сравнивать порог надо с ОДНОЙ из них, и разница между ними бывает
        # велика: 04.09 это 0.271 против 0.338.
        "observed_risk_share": (round(observed_risk_share, 4)
                                if observed_risk_share is not None else None),
        # Честный порог: частота достижения TP1, при которой сделка в нуле.
        # Заменяет 1/(1+RR), где выигрыш и проигрыш постулированы.
        "required_rate": round(required, 4) if required is not None else None,
        "verdict": _verdict(observed, required, len(reached_pairs), len(missed_pairs)),
        **_bootstrap(reached_pairs, missed_pairs),
        "note": (
            "required_rate — доля сделок, доходящих до TP1, при которой "
            "ожидание сделки равно нулю ПРИ ИЗМЕРЕННЫХ исходах обеих ветвей. "
            "Сравнивать с observed_rate. Если required_rate = null, дошедшие до "
            "TP1 не лучше недошедших: порога не существует и чинить надо не "
            "гейт, а сам выход. Интервал порога бутстрапится целиком; при "
            "spread_positive_share < 0.95 порог не установлен. Порог и "
            "observed_rate — обе величины ПОСДЕЛОЧНЫЕ; expectancy_r и "
            "observed_risk_share взвешены по риску и приведены для сравнения с "
            "остальными отчётами, но между собой эти две пары не смешивать."
        ),
    }


def _verdict(observed: float | None, required: float | None,
             n_reached: int, n_missed: int) -> str:
    if n_reached < _MIN_GROUP or n_missed < _MIN_GROUP:
        return "sample_too_thin"
    if required is None:
        return "reaching_tp1_does_not_pay"
    if observed is None:
        return "unknown"
    return "clears_the_bar" if observed >= required else "below_the_bar"
