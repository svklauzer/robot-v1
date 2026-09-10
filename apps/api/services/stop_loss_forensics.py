"""Чем сделка, дошедшая до стопа, отличалась НА ВХОДЕ от остальных
(#stop-forensics-2026-09-04).

Замер 04.09 по 91 закрытой сделке двух основных режимов:

    stop_loss        37 сделок   −53.82 USDT   (−1.45 на сделку)
    всё остальное    54 сделки   +34.16 USDT   (+0.63 на сделку)

Без стопов система прибыльна. Весь минус в одном ведре, и сопровождение тут ни
при чём: tz_kama закрывает в среднем по −0.38, breakeven_stop вообще в плюс.
Убивают сделки, которые идут против сразу и доходят до стопа, не дав логике
выхода ни одного шанса. Значит вопрос к ОТБОРУ ВХОДОВ.

Что здесь считается
-------------------
Закрытые сделки делятся на две группы — дошедшие до `stop_loss` и все прочие, —
и по каждому признаку входа сравниваются распределения.

Мера разделения — AUC (доля пар, в которых значение у стопнутой сделки выше,
чем у выжившей). 0.5 — признак не различает группы вовсе; 0 или 1 — различает
идеально. Выбрана намеренно: она ранговая, не требует предположений о
распределении и не ломается от выбросов, которых в 37 наблюдениях достаточно,
чтобы среднее врало.

Знак имеет значение
-------------------
`obi` и `cvd_ratio` — направленные величины: «поток в сторону сделки» для лонга
и шорта имеет ПРОТИВОПОЛОЖНЫЙ знак. Без нормировки по стороне лонги и шорты
взаимно гасятся, и разделение пропадает даже там, где оно есть. Такие признаки
приводятся к виду «больше — значит благоприятнее для ЭТОЙ сделки».

Что уже найдено этим отчётом (04.09)
------------------------------------
Ни один признак из `plan_json` не разделил группы — все AUC внутри 0.40–0.60.
Разделяет не отдельный признак, а СОБСТВЕННАЯ композитная оценка, и разделяет
в обратную сторону: грейд A измерен как значимо убыточный (−0.43R, ДИ
[−0.75; −0.07]) при вдвое меньшем MFE и вдвое меньшей достижимости TP1, чем у
B. Поэтому в разбор добавлены поля самой записи (`signal.confidence`) и
разбивка по стороне сделки — см. `side`.

Почему у гейта достижимости мерится запас, а не вердикт (07.09)
---------------------------------------------------------------
Разрез `tp_reach.would_block` заводился, чтобы сравнить исходы сделок, которые
гейт остановил бы, с теми, что пропустил бы. Ответа он дать не может: из восьми
последних закрытых сделок вердикт «остановил бы» стоит у ВОСЬМИ. Ветви для
сравнения не существует — в этом окне гейт не отбирает, а запрещает.

Сумма по этим восьми отрицательная, но избирательности это не доказывает: при
осуждении 100% выборки «выигрыш» гейта равен знаку недели, а не умению выбирать.

Поэтому ось переведена в непрерывную — `tp_reach.margin`. Она различает сделки
между собой там, где вердикт одинаков у всех, и считается по всей истории: свои
входные числа гейт писал задолго до того, как начал писать вердикт.

Второй исход: дошла ли сделка до TP1 (11.09)
-------------------------------------------
`outcome=tp1` делит те же сделки иначе — дошла ли цена до TP1 (MFE ≥ дистанции
TP1, тот же признак, что в отчёте после TP1). Вопрос перед гейтом на TP1: до
TP1 доходят 22% сделок при безубыточных ~43–46%, и гейт имеет смысл, только
если на входе есть признак, отличающий дошедших. Первым в этом ряду стоит
`tp_reach.tp1_hit_rate` — измеренная частота TP1 для символа и режима на
дистанции TP1 самой сделки, то есть ровно то, по чему такой гейт и судил бы.
Его AUC и есть избирательность будущего гейта. Гейт по TP2 этого замера не
проходил: он останавливал 27 сделок из 27, и стопы у остановленных были на
уровне базы.

У AUC теперь есть 95% интервал (Hanley–McNeil), у долей по уровням — Уилсона:
на 100 против 370 сделок пороги «0.40–0.60» слишком грубы в обе стороны.

Оговорка о выборке
------------------
37 против 54 — это мало. AUC в пределах 0.40–0.60 здесь не значит ничего, и
отчёт помечает такие признаки как неразличимые явно, чтобы их не приняли за
находку. Цель — не доказать, а сузить круг поиска.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

import math

from models.signal import Signal
from services.phantom_fill import phantom_adjustment
from services.tp1_overshoot import _entry_price, _tp1_dist_pct

STOP_REASON = "stop_loss"

# Исход, по которому делятся сделки: (группа события, остальные, ключ доли).
# Для stop ключи прежние — на них построены команды разбора.
_OUTCOMES: dict[str, tuple[str, str, str]] = {
    "stop": ("stopped", "survived", "stop_rate"),
    "tp1": ("reached", "not_reached", "reach_rate"),
}


def _reached_tp1(signal: Signal) -> bool | None:
    """Дошла ли цена до TP1 — тем же признаком, что в отчёте после TP1.

    Два отчёта по одним и тем же сделкам обязаны давать одно число, поэтому
    вход и дистанция берутся из тех же функций. Нет траектории — None: сделка
    не угадывается ни в одну из групп.
    """
    plan = signal.plan_json or {}
    lifecycle = plan.get("lifecycle") or {}
    mfe = _num(lifecycle.get("mfe_pct"))
    entry = _entry_price(signal, lifecycle)
    if mfe is None or not entry:
        return None
    dist = _tp1_dist_pct(signal, entry)
    if dist is None:
        return None
    return mfe >= dist


def _honest_net(signal: Signal) -> float:
    """(#stop-forensics-honest-2026-09-04) Тот же честный PnL, что и в
    regime_expectancy_report.

    Ветка `tp2_reached` закрывает на 92% пути до цели и книжит полную цену TP2,
    то есть исполняется лучше рынка. Наценка попадает ТОЛЬКО в группу выживших
    (стопы фантомными не бывают), поэтому на сыром PnL разрыв между группами
    выглядел бы шире, чем он есть. Два отчёта по одним и тем же сделкам обязаны
    давать одно число.
    """
    try:
        net = float(signal.closed_net_pnl or 0.0)
    except (TypeError, ValueError):
        return 0.0
    is_phantom, adjustment = phantom_adjustment(signal)
    return net + adjustment if is_phantom else net

# Признаки, у которых благоприятная сторона зависит от направления сделки.
_SIDE_SIGNED = {"entry_depth.obi", "entry_depth.cvd_ratio", "entry_depth.cvd"}

_NUMERIC: tuple[tuple[str, str], ...] = (
    # (#confidence-axis-2026-09-04) Собственная оценка качества. Проверялась
    # последней, а оказалась главной: грейд — пороговая функция от неё, и
    # A-грейд измерен как значимо убыточный (−0.43R, ДИ [−0.75; −0.07]) при
    # вдвое меньшем MFE и вдвое меньшей достижимости TP1, чем у B. Признаки из
    # plan_json не разделяли группы вовсе — потому что разделяет композитная
    # оценка, и разделяет в ОБРАТНУЮ сторону.
    ("signal.confidence", "confidence сигнала"),
    # (#confidence-ratchet-2026-09-04) Ноги уверенности по отдельности. Итоговое
    # число разделяет группы (AUC 0.6605, ДИ [0.549; 0.772] на 97 сделках), но
    # по нему нельзя понять, ЧТО именно в нём вредит. leg_gap — расхождение
    # рынка и чек-листа: величина, которую храповик игнорировал, беря большую.
    ("confidence.base", "нога рынка"),
    ("confidence.setup_leg", "нога чек-листа"),
    ("confidence.leg_gap", "расхождение ног"),
    ("signal.net_rr_tp1", "плановый RR до TP1"),
    ("signal.net_rr_tp2", "плановый RR до TP2"),
    ("signal.required_margin", "маржа позиции"),
    # микроструктура на момент входа
    ("entry_depth.spread_pct", "спред в стакане"),
    ("entry_depth.obi", "перекос стакана В СТОРОНУ сделки"),
    ("entry_depth.cvd_ratio", "поток сделок В СТОРОНУ сделки"),
    ("entry_depth.cvd_trades", "сделок в окне CVD"),
    ("entry_zone_plan.drift_pct", "снос входа от рынка"),
    ("entry_zone_plan.depth.near_depth_share", "объём вблизи входа"),
    # приборы тренда
    ("tz_shadow.adx", "ADX на входе"),
    # (#adx-delta-2026-09-04) Уровень ADX не отличает разгорающийся тренд от
    # затухающего — только производная. Композитная оценка, по которой ставится
    # грейд, не содержит НИ ОДНОЙ производной: trend/momentum/volume — уровни,
    # trend_alignment — счётчик согласных ТФ. Тренд, подтверждённый на 4h+1h+15m,
    # получает максимум и на третьем баре, и на трёхсотом.
    ("tz_shadow.adx_delta", "прирост ADX на входе"),
    ("tz_shadow.di_spread", "разведение DI по стороне"),
    ("tz_shadow.stoch_k", "Stoch %K"),
    ("trend_trigger.extension_atr", "растянутость от опоры, ATR"),
    # качество сетапа
    ("setup_quality.final_score", "итоговый скоринг сетапа"),
    ("setup_quality.trend_alignment", "согласованность ТФ"),
    ("setup_quality.entry_timing", "тайминг входа"),
    ("setup_quality.volume_confirmation", "подтверждение объёмом"),
    ("setup_quality.structure_quality", "качество структуры"),
    ("setup_quality.volatility_quality", "качество волатильности"),
    ("setup_quality.penalty", "штраф сетапа"),
    # экономика плана и ML
    ("ml.ml_score", "ML score"),
    ("tp_reach.tp1_dist_pct", "дистанция до TP1"),
    # (#tp1-forensics-2026-09-11) Частота TP1 для символа и режима на дистанции
    # TP1 этой сделки, измеренная по сделкам, закрытым ДО входа. Её разделение
    # по исходу tp1 — избирательность гейта на TP1, если строить его на ней.
    ("tp_reach.tp1_hit_rate", "измеренная частота достижения TP1"),
    ("tp_reach.tp2_dist_pct", "дистанция до TP2"),
    # (#tp-reach-margin-2026-09-07) Запас гейта достижимости: насколько
    # измеренная частота достижения TP2 выше требуемой. Ноль — ровно порог,
    # минус — гейт бы остановил.
    #
    # Замер 07.09 показал, что бинарный разрез по `tp_reach.would_block`
    # ответить на свой вопрос не может: из восьми последних сделок гейт осудил
    # ВОСЕМЬ, контрольной ветви не существует. Гейт в этом окне не фильтр, а
    # выключатель, и сравнивать «остановленные» с «пропущенными» не с чем.
    #
    # Запас — та же величина, но непрерывная: у каждой сделки своя, и вопрос
    # «связана ли близость к порогу с исходом» становится измеримым. Вдобавок
    # он считается по ВСЕЙ истории — гейт писал свои входные числа задолго до
    # того, как начал писать вердикт.
    ("tp_reach.margin", "запас гейта достижимости"),
    ("tp_reach.tp2_hit_rate", "измеренная частота достижения TP2"),
    ("tp_reach.required_hit_rate", "требуемая частота достижения TP2"),
    ("sizing.conviction", "conviction сайзинга"),
)

_CATEGORICAL: tuple[tuple[str, str], ...] = (
    ("signal.grade", "грейд"),
    # (#structure-mirror-2026-09-04) Сторона — не косметика. structure_score
    # считается по шкале «хорошо для лонга» (близко к support = 70) и для
    # шортов НЕ зеркалится ни в confidence, ни в setup_quality. Значит у шортов
    # компонент structure работает в обратную сторону, и разбор по стороне —
    # прямая проверка этой гипотезы.
    ("signal.side", "сторона сделки"),
    ("signal.symbol", "символ"),
    ("trade_mode", "режим сделки"),
    ("regime", "режим рынка"),
    ("entry_reason", "причина входа"),
    ("entry_zone_plan.mode", "способ входа"),
    ("tz_shadow.would_pass", "условия ТЗ пройдены"),
    # (#latch-cohort-2026-09-05) Как именно вход прошёл условия ТЗ. Значение
    # `adx_rising_latched` помечает сделки, впущенные защёлкой импульса — то
    # есть ровно ту когорту, ради которой она и написана. Без этого разреза
    # вооружение защёлки нельзя оценить: новые входы растворятся в общей массе,
    # и вопрос «стали ли они лучше» останется без ответа.
    ("tz_shadow.enforce_reason", "как пройдены условия ТЗ"),
    # (#shadow-verdict-2026-09-06) Вердикт гейта достижимости независимо от
    # режима. С 06.09 гейт в shadow: он судит по достижению TP2, а сделка
    # платится через TP1 и трейл. Разрез отвечает на вопрос, ради которого его
    # и отпустили, — были ли остановленные им сделки действительно хуже.
    ("tp_reach.would_block", "гейт достижимости остановил бы"),
    ("performance_guard.reason", "вердикт performance guard"),
)


def _tp_reach_margin(signal: Signal, plan: dict) -> float | None:
    """Запас гейта достижимости: измеренная частота минус требуемая.

    Повторяет арифметику самого гейта, включая ветвь спасения: `evaluate()`
    считает вход допустимым, если порог берёт ЛИБО сырая частота, ЛИБО
    нецензурированная. Без max(...) сделки, впущенные именно спасением,
    выглядели бы здесь остановленными — то есть признак противоречил бы
    собственному вердикту гейта, записанному рядом в `would_block`.
    """
    reach = _dig(plan, "tp_reach")
    if not isinstance(reach, dict):
        return None
    required = _num(reach.get("required_hit_rate"))
    if required is None:
        return None
    measured = [
        rate for rate in (_num(reach.get("tp2_hit_rate")),
                          _num(reach.get("tp2_hit_rate_uncensored")))
        if rate is not None
    ]
    if not measured:
        return None
    return max(measured) - required


# Признаки, которых в плане нет готовыми: считаются из того, что там лежит.
# Держатся реестром, а не ветками в _value, чтобы следующий такой признак был
# строкой, а не третьим местом, где решается, откуда брать число.
_DERIVED = {
    "tp_reach.margin": _tp_reach_margin,
}


def _value(signal: Signal, plan: dict, path: str):
    """Префикс `signal.` — поле самой записи, всё прочее — путь внутри плана.

    (#confidence-axis-2026-09-04) Пока разбор умел читать только план, ось
    confidence — та самая, на которой стоит грейд, — оставалась непроверенной.
    Одно правило вместо частных исключений: следующее поле записи добавляется
    строкой в список, а не веткой в трёх местах.
    """
    derived = _DERIVED.get(path)
    if derived is not None:
        return derived(signal, plan)
    if path.startswith("signal."):
        return getattr(signal, path.split(".", 1)[1], None)
    return _dig(plan, path)


def _dig(source: dict, path: str):
    node = source
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _num(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _auc(a: list[float], b: list[float]) -> float | None:
    """Доля пар (a_i, b_j), где a_i > b_j; ничьи считаются половиной.

    Это ровно статистика Манна–Уитни, нормированная на число пар. Ранговая:
    один выброс не сдвигает её так, как сдвинул бы среднее.
    """
    if not a or not b:
        return None
    wins = 0.0
    for x in a:
        for y in b:
            if x > y:
                wins += 1.0
            elif x == y:
                wins += 0.5
    return wins / (len(a) * len(b))


def _auc_ci(auc: float, n1: int, n2: int, z: float = 1.96) -> tuple[float, float]:
    """95% интервал AUC по Hanley–McNeil (1982). Для малых выборок грубоват,
    но честнее фиксированных порогов: на 100 против 370 сделок полоса
    «0.40–0.60 ничего не значит» слишком широка, на 20 против 20 — узка."""
    q1 = auc / (2.0 - auc)
    q2 = 2.0 * auc * auc / (1.0 + auc)
    var = (auc * (1 - auc) + (n1 - 1) * (q1 - auc * auc)
           + (n2 - 1) * (q2 - auc * auc)) / float(n1 * n2)
    se = math.sqrt(max(var, 0.0))
    return max(0.0, auc - z * se), min(1.0, auc + z * se)


def _wilson(k: int, n: int, z: float = 1.96) -> list[float] | None:
    """95% интервал Уилсона для доли k/n."""
    if n <= 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def _oriented(value: float, side: str) -> float:
    """Приводит знаковый признак к виду «больше — благоприятнее для сделки»."""
    return value if str(side).lower() == "long" else -value


def build(db: Session, *, window_hours: float = 720.0, regime: str | None = None,
          side: str | None = None, max_rows: int = 4000,
          min_group: int = 5, outcome: str = "stop") -> dict:
    if outcome not in _OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}; expected one of {sorted(_OUTCOMES)}")
    a_name, b_name, rate_key = _OUTCOMES[outcome]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=float(window_hours))

    query = db.query(Signal).filter(
        Signal.status == "closed",
        Signal.closed_at.isnot(None),
        Signal.closed_at >= cutoff,
    )
    rows = query.order_by(Signal.id.desc()).limit(int(max_rows)).all()

    stopped: list[Signal] = []
    survived: list[Signal] = []
    skipped = 0
    for s in rows:
        plan = s.plan_json or {}
        if regime and str(plan.get("regime") or "") != regime:
            continue
        if side and str(s.side or "").lower() != str(side).lower():
            continue
        if outcome == "tp1":
            hit = _reached_tp1(s)
            if hit is None:
                skipped += 1
                continue
        else:
            hit = str(s.closed_reason or "") == STOP_REASON
        (stopped if hit else survived).append(s)

    names = (a_name, b_name)
    numeric = [
        _numeric_row(path, label, stopped, survived, min_group, names)
        for path, label in _NUMERIC
    ]
    numeric = [r for r in numeric if r is not None]
    # Сильнее всего разделяющие — наверх, вне зависимости от направления.
    numeric.sort(key=lambda r: abs(r["auc"] - 0.5), reverse=True)

    categorical = [
        _categorical_row(path, label, stopped, survived, names, rate_key)
        for path, label in _CATEGORICAL
    ]
    categorical = [r for r in categorical if r is not None]

    total = len(stopped) + len(survived)
    return {
        "window_hours": float(window_hours),
        "regime": regime,
        "side": side,
        "outcome": outcome,
        a_name: _group_summary(stopped),
        b_name: _group_summary(survived),
        # Доля группы события во всей выборке — база, с которой сравниваются
        # доли по уровням категориальных признаков.
        "base_rate": round(len(stopped) / total, 4) if total else None,
        "base_rate_ci": _wilson(len(stopped), total),
        "skipped_no_trajectory": skipped,
        "numeric": numeric,
        "categorical": categorical,
        "note": (
            "auc — доля пар, где значение у стопнутой сделки выше, чем у "
            "выжившей. 0.5 = признак не различает группы. При выборке ~37/54 "
            "диапазон 0.40–0.60 не значит ничего (verdict=indistinguishable); "
            "смотреть стоит только на края. Знаковые признаки (obi, cvd) "
            "приведены к виду «больше = благоприятнее для этой сделки». "
            "side=short/long делит выборку по стороне: structure_score считается "
            "по шкале «хорошо для лонга» и для шортов нигде не зеркалится, так "
            "что расхождение AUC у setup_quality.structure_quality между "
            "сторонами — прямая проверка этой асимметрии. "
            "tp_reach.margin — запас гейта достижимости (измеренная частота "
            "TP2 минус требуемая, с учётом ветви спасения по нецензурированной): "
            "минус означает, что гейт остановил бы вход. Он заведён взамен "
            "разреза по tp_reach.would_block, который вырожден — 07.09 вердикт "
            "«остановил бы» стоял у восьми последних сделок из восьми. "
            "outcome=tp1 делит сделки по достижению TP1 (MFE ≥ дистанции TP1): "
            "auc > 0.5 — у дошедших значение выше. auc_ci — 95% интервал "
            "Hanley–McNeil; признак что-то значит, только если интервал не "
            "накрывает 0.5. tp_reach.tp1_hit_rate — то, по чему судил бы гейт "
            "на TP1: его auc и есть избирательность такого гейта."
        ),
    }


def _group_summary(signals: list[Signal]) -> dict:
    net = sum(_honest_net(s) for s in signals)
    return {
        "n": len(signals),
        "net_usdt": round(net, 6),
        "avg_usdt": round(net / len(signals), 4) if signals else None,
    }


def _numeric_row(path: str, label: str, stopped: list[Signal],
                 survived: list[Signal], min_group: int,
                 names: tuple[str, str] = ("stopped", "survived")) -> dict | None:
    def collect(items: list[Signal]) -> list[float]:
        out: list[float] = []
        for s in items:
            plan = s.plan_json or {}
            value = _num(_value(s, plan, path))
            if value is None:
                continue
            if path in _SIDE_SIGNED:
                value = _oriented(value, s.side)
            out.append(value)
        return out

    a, b = collect(stopped), collect(survived)
    if len(a) < min_group or len(b) < min_group:
        return None

    auc = _auc(a, b)
    if auc is None:
        return None

    gap = abs(auc - 0.5)
    verdict = (
        "separates_strongly" if gap >= 0.20
        else "separates_weakly" if gap >= 0.15
        else "indistinguishable"
    )

    lo, hi = _auc_ci(auc, len(a), len(b))
    ev, rest = names
    return {
        "feature": path,
        "label": label,
        f"n_{ev}": len(a),
        f"n_{rest}": len(b),
        f"median_{ev}": round(_median(a), 6),
        f"median_{rest}": round(_median(b), 6),
        "auc": round(auc, 4),
        "auc_ci": [round(lo, 4), round(hi, 4)],
        "ci_excludes_half": hi < 0.5 or lo > 0.5,
        # >0.5 — у группы события значение ВЫШЕ; <0.5 — ниже.
        "higher_in": ev if auc > 0.5 else rest,
        "verdict": verdict,
    }


def _categorical_row(path: str, label: str, stopped: list[Signal],
                     survived: list[Signal],
                     names: tuple[str, str] = ("stopped", "survived"),
                     rate_key: str = "stop_rate") -> dict | None:
    levels: dict[str, dict] = {}
    ev, rest = names

    for group, items in ((ev, stopped), (rest, survived)):
        for s in items:
            plan = s.plan_json or {}
            raw = _value(s, plan, path)
            if raw is None:
                continue
            key = str(raw)
            slot = levels.setdefault(key, {ev: 0, rest: 0, "net_usdt": 0.0})
            slot[group] += 1
            slot["net_usdt"] = round(slot["net_usdt"] + _honest_net(s), 6)

    if not levels:
        return None

    for slot in levels.values():
        total = slot[ev] + slot[rest]
        slot["n"] = total
        slot[rate_key] = round(slot[ev] / total, 4) if total else None
        slot[f"{rate_key}_ci"] = _wilson(slot[ev], total)

    return {
        "feature": path,
        "label": label,
        "levels": dict(sorted(levels.items(), key=lambda kv: kv[1]["n"], reverse=True)),
    }
