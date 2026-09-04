"""Достижима ли цель сделки. (#tp-reachability-2026-08-03, переписан 24.08.2026)

Зачем
-----
`net_rr_tp2` считается ОТ ЦЕЛИ, и гейт входа (`min_rr_tp2 = 1.3`) пропускает
сделку, опираясь на геометрию. Если цель недостижима, геометрии не существует:
сделка выглядит как «риск 1 ради 1.3», а фактически это «риск 1 ради того, что
почти никогда не случается». Этот модуль проверяет, что заявленная награда
бывает в реальности достаточно часто, чтобы заявленный RR окупался.

Что считается
-------------
Доля закрытых сделок, чей MFE дошёл до плановой дистанции:

    hit_rate(d) = доля сделок с mfe_pct >= d

Порог НЕ задаётся вручную — он выводится из RR, который заявляет сам план.
Сделка с наградой R окупается, если доходит до цели чаще, чем

    required = 1 / (1 + R)

RR 1.3 требует 43%, RR 2.9 — 26%. Никакого подбираемого числа: чем смелее
геометрия, тем реже ей позволено сбываться. Оценка грубая (частичная фиксация
на TP1 и стопы, которые не всегда добираются, в неё не входят) — она отсекает
заведомо выдуманную геометрию, а не измеряет матожидание точно.

Почему не отношение к медиане (прежняя версия)
----------------------------------------------
Считали `tp1_dist / median_mfe` и требовали ≤ 1.5. Две ошибки.

1. Медиана MFE по ВСЕМ закрытым при винрейте 36.75% — это в основном
   проигравшие: у победителей 24.08 MFE 2.2–3.1%, у проигравших 0.00–0.05%.
   Требование «цель внутри 1.5× медианы» означало «цель обязана быть достижима
   для сделок, которые проваливаются».
2. `TP1_MIN_PCT = 0.6` и потолок 1.5 давали безусловный замок: гейт проходим
   только при медиане ≥ 0.4%, а ближе 0.6% цель поставить нельзя. У ETH медиана
   0.3154% при ATR 1h 0.95% — инструмент ходит втрое дальше, чем показывал наш
   замер. Медиана растёт только от закрытых сделок, которых из-за блокировки не
   было: 24.08 ETH блокировался каждый скан.

Отсюда же правило про выборку символа: она НЕ решает единолично. Отказ по
паре (символ, режим) перепроверяется по общережимной выборке, и блокировка
наступает, только если отказали обе. Иначе узкая выборка запирает инструмент
навсегда — сама себя подтверждая.

Оговорка о смещении: MFE ограничен нашими же выходами, поэтому `hit_rate` —
оценка СНИЗУ. Гейт получается строже реальности; это осознанный выбор в пользу
осторожности, но именно он делает замок из п.2 возможным, оттого и страховка.

Режим
-----
`TP_REACH_MODE`: shadow (по умолчанию) | enforce. В shadow пишет вердикт в
`plan_json.tp_reach` и ничего не блокирует. Предохранитель по размеру выборки
обязателен: на трёх сделках частота — это не частота.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from core.config import settings

_CACHE: dict = {"ts": 0.0, "by_key": {}, "by_regime": {}}

# (#tp-reach-censoring-2026-09-04) Разделение промахов на настоящие и наши
# собственные. Докстринг модуля выше честно предупреждал: «MFE ограничен нашими
# же выходами, поэтому hit_rate — оценка СНИЗУ… именно он делает замок
# возможным». 04.09 замок и случился: ETH `0% < 27%` при tp2_hit_rate = 0.0 на
# 21 сделке. Ни одна не дошла до TP2 — но и не могла: их закрывали наши
# защитные выходы раньше цели, и каждое такое закрытие записывалось в
# статистику как «цель недостижима». Контур замкнулся: жёсткий выход режет
# хвост → частота падает → гейт не пускает → новых сделок нет → частота не
# растёт.
#
# ТЕРМИНАЛЬНЫЕ причины — сделка умерла ОТ РЫНКА: цена дошла до уровня, который
# мы заранее объявили провалом. Промах настоящий, наблюдение полное.
#
# Всё остальное при MFE ниже цели — ЦЕНЗУРА: мы вышли сами, пока сделка была
# жива, и что цена сделала бы дальше, мы не знаем. Такое наблюдение говорит
# «MFE был НЕ МЕНЬШЕ записанного», а не «цель недостижима».
#
# НЕИЗВЕСТНАЯ причина считается настоящим промахом, а не цензурой. Отсутствие
# доказательства — не доказательство: право снять отказ даёт только явно
# опознанный СВОЙ выход. Первая версия этой правки делала наоборот, и тесты
# поймали вырождение: в журнале есть строки без closed_reason, при умолчании
# «цензура» оценка становилась 1.0 и пускала вообще всё.
_TERMINAL_REASONS: frozenset[str] = frozenset({
    "stop_loss",       # цена дошла до стопа — движение против нас состоялось
    "breakeven_stop",  # после TP1 вернулась к входу — движение выдохлось
    "tp2_trail_stop",  # трейл хвоста после TP2 — тоже заранее объявленный уровень
})

# Наши добровольные выходы: сделка была жива, уровня провала не касалась, мы
# вышли сами. Что цена сделала бы дальше — не наблюдалось.
_VOLUNTARY_REASONS: frozenset[str] = frozenset({
    "tz_mfe_giveback_backstop", "post_tp1_giveback_trail", "tp2_trail_giveback",
    "adaptive_mfe_capture", "adaptive_post_tp1_stop", "adaptive_trailing_stop",
    "protective_trailing_stop", "protective_breakeven_profit_guard",
    "trend_trailing_stop", "trend_ride_trailing_stop", "trend_capture_band",
    "wide_stop_tp2_guard", "breakeven_lock", "scalp_breakeven_lock",
    "scalp_flow_exit", "scalp_time_stop", "range_time_stop",
    "failed_setup_exit", "low_grade_capital_release",
    "manual_close", "manual_cancel", "manual_profit_close", "manual_loss_close",
})


def _is_terminal(reason: str | None) -> bool:
    """Промах настоящий: сделка умерла от рынка ИЛИ причину опознать нельзя.

    Умолчание намеренно строгое — см. комментарий к _TERMINAL_REASONS.
    """
    return str(reason or "") not in _VOLUNTARY_REASONS


@dataclass(frozen=True)
class TPReach:
    evaluated: bool
    allowed: bool
    reason: str
    tp1_dist_pct: float | None
    median_mfe_pct: float | None
    sample: int
    source: str
    # Плечо, от которого считается связывающий RR, и его частота достижения.
    tp2_dist_pct: float | None = None
    tp2_hit_rate: float | None = None
    # Требуемая частота, выведенная из заявленного RR: 1 / (1 + RR).
    required_hit_rate: float | None = None
    net_rr_tp2: float | None = None
    # TP1 — точка частичной фиксации, не награда: пишется, но не блокирует.
    tp1_hit_rate: float | None = None
    # Заблокировала ли выборка символа до перепроверки по режиму.
    symbol_sample_overridden: bool = False
    # (#tp-reach-censoring-2026-09-04) Та же частота без наших собственных
    # ранних выходов, и сколько наблюдений при этом отброшено как
    # цензурированные. Оба поля выводятся наружу намеренно: разрыв между
    # tp2_hit_rate и tp2_hit_rate_uncensored — это и есть цена, которую гейт
    # входа платит за решения контура выхода. Пока она не на виду, замок
    # каждый раз выглядит как «рынок не даёт».
    tp2_hit_rate_uncensored: float | None = None
    uncensored_sample: int = 0
    censored_out: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _load_mfe(ttl_sec: float = 900.0) -> tuple[dict, dict]:
    """Медианы реализованного MFE по символ+режим и по режиму.

    Читается из журнала закрытых сделок с кешем: гейт зовут на каждом символе
    каждого прохода, а файл переписывается редко.
    """
    now = time.time()
    if now - float(_CACHE["ts"]) < ttl_sec and _CACHE["by_regime"]:
        return _CACHE["by_key"], _CACHE["by_regime"]

    # (#tp-reach-censoring-2026-09-04) Рядом с MFE несём причину закрытия: без
    # неё нельзя отличить настоящий промах от нашего собственного выхода.
    raw_key: dict[tuple[str, str], list[tuple[float, str]]] = {}
    raw_regime: dict[str, list[tuple[float, str]]] = {}
    try:
        from services.ml_trade_logger import MLTradeLogger

        path = Path(MLTradeLogger().path)
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    lc = row.get("lifecycle") or {}
                    try:
                        mfe = float(lc.get("mfe_pct"))
                    except (TypeError, ValueError):
                        continue
                    if mfe <= 0:
                        continue
                    symbol = str(row.get("symbol") or "")
                    regime = str(row.get("regime") or "")
                    reason = str(
                        row.get("closed_reason")
                        or (lc.get("close_reason") if isinstance(lc, dict) else "")
                        or ""
                    )
                    if regime:
                        raw_regime.setdefault(regime, []).append((mfe, reason))
                        if symbol:
                            raw_key.setdefault((symbol, regime), []).append((mfe, reason))
    except Exception:
        return _CACHE["by_key"], _CACHE["by_regime"]

    _CACHE["ts"] = now
    _CACHE["by_key"] = {k: v for k, v in raw_key.items()}
    _CACHE["by_regime"] = {k: v for k, v in raw_regime.items()}
    return _CACHE["by_key"], _CACHE["by_regime"]


def _mfe_only(values) -> list[float]:
    """MFE из пар (mfe, reason). Терпит и старый плоский формат кеша."""
    out: list[float] = []
    for v in values or []:
        out.append(float(v[0]) if isinstance(v, (tuple, list)) else float(v))
    return out


def _hit_rate(values, dist_pct: float) -> float:
    """Доля сделок, чей MFE дошёл до дистанции.

    Оценка СНИЗУ: MFE ограничен нашими же выходами, поэтому реальная частота
    достижения не меньше этой. Гейт из-за этого строже рынка — см. докстринг
    модуля, там же про страховку от вечного замка.
    """
    mfe = _mfe_only(values)
    if not mfe or dist_pct <= 0:
        return 0.0
    return sum(1 for v in mfe if v >= dist_pct) / float(len(mfe))


def _uncensored_hit_rate(values, dist_pct: float) -> tuple[float | None, int, int]:
    """Та же частота, но без наших собственных ранних выходов.

    (#tp-reach-censoring-2026-09-04) Возвращает (частота, размер_выборки,
    сколько_отброшено). Сделка, закрытая нами добровольно ниже цели, — это
    ЦЕНЗУРИРОВАННОЕ наблюдение: она говорит «MFE был не меньше записанного», а
    не «цель недостижима». Считать её промахом — значит учить гейт входа на
    решениях контура выхода.

    Честная оговорка о направлении смещения: выходим мы обычно тогда, когда
    импульс угасает, то есть цензура НЕ независима от исхода. Поэтому оценка
    смещена ВВЕРХ ровно так же, как сырая смещена вниз. Правда между ними, и
    именно поэтому эта величина не блокирует ничего сама — она только снимает
    отказ сырой (см. evaluate).

    None — выборки не хватило, чтобы вообще говорить о частоте.
    """
    if not values or dist_pct <= 0:
        return None, 0, 0

    reached = 0
    genuine_miss = 0
    censored = 0

    for v in values:
        mfe, reason = (v[0], v[1]) if isinstance(v, (tuple, list)) else (v, "")
        if float(mfe) >= dist_pct:
            reached += 1
        elif _is_terminal(reason):
            genuine_miss += 1
        else:
            censored += 1

    usable = reached + genuine_miss
    min_usable = int(getattr(settings, "TP_REACH_MIN_UNCENSORED_SAMPLE", 12))
    if usable < min_usable:
        return None, usable, censored

    return reached / float(usable), usable, censored


def _required_hit_rate(net_rr_tp2: float) -> float:
    """Частота, при которой заявленный RR перестаёт быть выдумкой: 1 / (1 + R).

    Порог выводится из плана, а не подбирается. Это и есть защита от приёма,
    которым уже пользовались: `SCALP_TARGET_PCT` понижали 0.8 → 0.5 ради
    прохождения гейта, то есть подгоняли цель под проверку. Здесь понизить
    цель означает понизить и RR, а с ним подняться требуемой частоте.
    """
    rr = max(0.01, float(net_rr_tp2))
    margin = float(getattr(settings, "TP_REACH_EV_MARGIN", 1.0))
    return (1.0 / (1.0 + rr)) * max(0.1, margin)


def evaluate(*, symbol: str, regime: str, tp1_dist_pct: float | None,
             tp2_dist_pct: float | None = None,
             net_rr_tp2: float | None = None) -> TPReach:
    """Бывает ли заявленная награда достаточно часто, чтобы заявленный RR окупался.

    Блокирует по TP2 — плечу, от которого считается связывающий вход
    `min_rr_tp2`. Прежняя версия проверяла TP1, точку частичной фиксации: не то
    плечо, которым принимается решение. Частота TP1 считается и пишется, но
    входу не мешает.

    fail-open: нет данных или нет TP2 — пропускаем. Отсутствие статистики не
    должно выглядеть как «цель недостижима».
    """
    if not tp1_dist_pct or float(tp1_dist_pct) <= 0:
        return TPReach(False, True, "no_tp1_distance", None, None, 0, "none")
    if not tp2_dist_pct or float(tp2_dist_pct) <= 0 or net_rr_tp2 is None:
        return TPReach(False, True, "no_tp2_geometry", round(float(tp1_dist_pct), 4),
                       None, 0, "none")

    by_key, by_regime = _load_mfe()
    min_sample = int(getattr(settings, "TP_REACH_MIN_SAMPLE", 20))
    regime_values = by_regime.get(str(regime)) or []

    values = by_key.get((str(symbol), str(regime)))
    source = "symbol_regime"
    if not values or len(values) < min_sample:
        values, source = regime_values, "regime"

    if not values or len(values) < min_sample:
        return TPReach(False, True, f"sample_too_small:{len(values or [])}<{min_sample}",
                       round(float(tp1_dist_pct), 4), None, len(values or []), source)

    required = _required_hit_rate(float(net_rr_tp2))
    tp2_hit = _hit_rate(values, float(tp2_dist_pct))
    tp1_hit = _hit_rate(values, float(tp1_dist_pct))
    overridden = False

    # Узкая выборка символа не решает единолично. Если она отказывает, спрашиваем
    # общережимную: символ, которому запретили входить, новых сделок не даёт, его
    # выборка не растёт, и отказ становится вечным — ETH 24.08 блокировался
    # каждый скан при собственной медиане 0.3154% и ATR 1h 0.95%.
    if tp2_hit < required and source == "symbol_regime" and len(regime_values) >= min_sample:
        regime_hit = _hit_rate(regime_values, float(tp2_dist_pct))
        if regime_hit >= required:
            values, source, overridden = regime_values, "regime_override", True
            tp2_hit, tp1_hit = regime_hit, _hit_rate(regime_values, float(tp1_dist_pct))

    within = tp2_hit >= required

    # (#tp-reach-censoring-2026-09-04) Вторая страховка, того же устройства,
    # что и перепроверка по режиму выше: сырая частота отказала — спрашиваем
    # ту же выборку БЕЗ наших собственных ранних выходов. Блокировка наступает,
    # только если отказали обе.
    #
    # Эта оценка не может ужесточить гейт: путь к ней ведёт только из отказа.
    # Иначе её смещение вверх (мы выходим, когда импульс гаснет) начало бы
    # пускать сделки, которых сырая не пускала бы по делу.
    uncensored_hit = None
    uncensored_sample = 0
    censored_out = 0
    if not within and bool(getattr(settings, "TP_REACH_CENSOR_ADJUST_ENABLED", True)):
        uncensored_hit, uncensored_sample, censored_out = _uncensored_hit_rate(
            values, float(tp2_dist_pct)
        )
        if uncensored_hit is not None and uncensored_hit >= required:
            within = True

    median_mfe = _median(_mfe_only(values))
    shadow = str(getattr(settings, "TP_REACH_MODE", "shadow")).lower() != "enforce"

    return TPReach(
        evaluated=True,
        allowed=True if shadow else within,
        reason=(
            "mode_shadow" if shadow
            else "reward_reached_uncensored" if within and uncensored_hit is not None
            else "reward_reached_often_enough" if within
            else f"tp2_reached_too_rarely:{tp2_hit:.0%}<{required:.0%}"
        ),
        tp1_dist_pct=round(float(tp1_dist_pct), 4),
        median_mfe_pct=round(median_mfe, 4) if median_mfe else None,
        sample=len(values),
        source=source,
        tp2_dist_pct=round(float(tp2_dist_pct), 4),
        tp2_hit_rate=round(tp2_hit, 4),
        required_hit_rate=round(required, 4),
        net_rr_tp2=round(float(net_rr_tp2), 4),
        tp1_hit_rate=round(tp1_hit, 4),
        symbol_sample_overridden=overridden,
        tp2_hit_rate_uncensored=(
            round(uncensored_hit, 4) if uncensored_hit is not None else None
        ),
        uncensored_sample=uncensored_sample,
        censored_out=censored_out,
    )
