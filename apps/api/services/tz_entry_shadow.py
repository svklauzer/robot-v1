"""Условия входа по ТЗ в режиме наблюдения (#tz-shadow-2026-08-03).

Зачем
-----
Замер по 333 закрытым (`/analytics/mfe-mae`) даёт по режимам отношение среднего
хода в свою сторону к среднему ходу против:

    reversal_long   3.451
    scalp           1.926
    crt             1.803
    trend_down      1.250
    range           1.119
    trend_up        0.943   ← ниже единицы

У trend_up цена в среднем уходит ПРОТИВ сделки дальше, чем за неё: MFE 0.787%
против MAE 0.835%, capture −20.6%. Выходом это не лечится — нечего фиксировать.
Значит чинить надо отбор входа, а вход сейчас устроен так:

    условие:  h4.trend == up AND h1.trend == up AND m15.momentum ok
    зона:     entry_from = last * 0.997,  entry_to = last * 1.003

Первое истинно сутками (на 4h EMA20 ≈ 3.3 дня), второе — «цена в момент, когда
до символа дошёл скан». Меры силы движения нет вообще, точки входа нет вообще.

Что проверяется
---------------
Три условия из ТЗ, каждое закрывает свою дыру:

    ADX > порог и +DI/−DI по стороне   — есть ли движение вообще
    Stoch RSI: %K пересёк %D из зоны   — точка входа после отката
    OBV относительно своей EMA(20)     — куда идёт объём, а не сколько его

KAMA не проверяется: это замена сглаживания, а не новая информация, и аргумент
про шум в ТЗ написан под DEX. Раздел 4 ТЗ (газ, TVL, price impact, допуск
проскальзывания 1–3%) к централизованной бирже не применяется — при валовом
edge 0.084% на сделку допуск в 1% это двенадцать edge.

Режим
-----
`TZ_MODE`: shadow (по умолчанию) | enforce.

(#tz-enforce-2026-08-03) Замер на первых наблюдениях: ADX по трендовым сетапам
шёл 16.1 / 18.0 / 19.4 при пороге ТЗ 23. Ни один сетап порога не достигал —
значит enforce на пороге 23 это не «стать разборчивее», а выключить трендовый
контур параметром. Порог из ТЗ написан под DEX-таймфреймы и к нашим не подошёл.

Отсюда два предохранителя, оба обязательны:

  * `TZ_ENFORCE_MIN_SAMPLE` — enforce НЕ включается, пока не накоплено столько
    оценок. Иначе порог калибруется по трём точкам, а это подгонка с другим
    названием. Счётчик берётся из журнала, а не из веры.
  * `TZ_ENFORCE_CONDITIONS` — какие именно условия блокируют. Условия с
    некалиброванным порогом можно оставить в тени, включив только те, что
    порога не требуют (направление DI, OBV против своей EMA).

Пока выборки нет, `should_block` возвращает False с причиной — поведение
не меняется, но в `plan_json` видно, ЧТО именно удерживает enforce выключенным.

Чистые функции над словарями таймфреймов — тестируется без pandas и рынка.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from core.config import settings

TREND_REGIMES = frozenset({"trend_up_candidate", "trend_down_candidate"})

# Условие → к какому семейству относится. Нужно, чтобы enforce можно было
# включить частично: у `adx` порог не откалиброван, у `di`/`obv` порога нет
# вовсе — они сравнивают величины между собой и работают на любых таймфреймах.
CONDITION_FAMILY = {
    "adx_below_min": "adx",
    "di_against_side": "di",
    "stoch_not_in_pullback": "stoch",
    "stoch_k_below_d": "stoch",
    "stoch_k_above_d": "stoch",
    "obv_below_ema": "obv",
    "obv_above_ema": "obv",
}


def _family(code: str) -> str:
    return CONDITION_FAMILY.get(str(code).split(":", 1)[0], "unknown")


@dataclass(frozen=True)
class TZShadow:
    regime: str
    side: str
    evaluated: bool
    would_pass: bool | None
    failed: tuple[str, ...]
    adx: float | None
    di_spread: float | None
    stoch_k: float | None
    stoch_d: float | None
    obv_vs_ema: float | None

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["failed"] = list(self.failed)
        return payload


def _num(ctx, key: str) -> float | None:
    if not isinstance(ctx, dict):
        ctx = getattr(ctx, "__dict__", {}) or {}
    value = ctx.get(key)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pick(timeframes, *names):
    if not isinstance(timeframes, dict):
        return None
    for name in names:
        ctx = timeframes.get(name)
        if ctx:
            return ctx
    return None


def evaluate(timeframes, *, regime: str, side: str) -> TZShadow:
    """Прошёл бы вход по условиям ТЗ. Ничего не блокирует.

    fail-open по смыслу: нет данных → evaluated=False, would_pass=None.
    Отсутствие индикатора не должно выглядеть как «условие не выполнено».
    """
    regime_value = str(regime or "")
    side_value = str(side or "").lower()
    is_long = side_value in ("long", "buy")

    if regime_value not in TREND_REGIMES:
        return TZShadow(regime_value, side_value, False, None, (), None, None, None, None, None)

    trend_tf = _pick(timeframes, str(getattr(settings, "TZ_TREND_TF", "1h")), "1h", "4h")
    entry_tf = _pick(timeframes, str(getattr(settings, "TZ_ENTRY_TF", "15m")), "15m", "5m")
    if trend_tf is None or entry_tf is None:
        return TZShadow(regime_value, side_value, False, None, (), None, None, None, None, None)

    adx = _num(trend_tf, "adx14")
    plus_di = _num(trend_tf, "plus_di")
    minus_di = _num(trend_tf, "minus_di")
    stoch_k = _num(entry_tf, "stoch_rsi_k")
    stoch_d = _num(entry_tf, "stoch_rsi_d")
    obv = _num(trend_tf, "obv")
    obv_ema = _num(trend_tf, "obv_ema20")

    if adx is None or plus_di is None or minus_di is None or stoch_k is None:
        return TZShadow(regime_value, side_value, False, None, (), adx, None, stoch_k, stoch_d, None)

    failed: list[str] = []

    # 1. Сила движения. Порог из ТЗ.
    if adx < float(getattr(settings, "TZ_ADX_MIN", 23.0)):
        failed.append(f"adx_below_min:{adx:.1f}")

    # 2. Направление подтверждено разностью DI по стороне сделки.
    di_spread = (plus_di - minus_di) if is_long else (minus_di - plus_di)
    if di_spread <= 0:
        failed.append(f"di_against_side:{di_spread:.1f}")

    # 3. Точка входа: цена в откате, а не в растяжении. Полноценное
    #    пересечение %K/%D требует предыдущего бара — его в срезе нет,
    #    поэтому здесь проверяется зона и взаимное положение линий.
    zone = float(getattr(settings, "TZ_STOCH_ZONE", 30.0))
    in_zone = stoch_k <= zone if is_long else stoch_k >= (100.0 - zone)
    if not in_zone:
        failed.append(f"stoch_not_in_pullback:{stoch_k:.1f}")
    if stoch_d is not None:
        crossing_up = stoch_k > stoch_d
        if is_long and not crossing_up:
            failed.append("stoch_k_below_d")
        if not is_long and crossing_up:
            failed.append("stoch_k_above_d")

    # 4. Поток капитала. Отсутствие OBV не считается провалом условия.
    obv_vs_ema = None
    if obv is not None and obv_ema is not None:
        obv_vs_ema = obv - obv_ema
        if is_long and obv_vs_ema <= 0:
            failed.append("obv_below_ema")
        if not is_long and obv_vs_ema >= 0:
            failed.append("obv_above_ema")

    return TZShadow(
        regime=regime_value,
        side=side_value,
        evaluated=True,
        would_pass=not failed,
        failed=tuple(failed),
        adx=round(adx, 4),
        di_spread=round(di_spread, 4),
        stoch_k=round(stoch_k, 4),
        stoch_d=round(stoch_d, 4) if stoch_d is not None else None,
        obv_vs_ema=round(obv_vs_ema, 4) if obv_vs_ema is not None else None,
    )


_SAMPLE_CACHE: dict = {"ts": 0.0, "value": 0}


def observed_sample_size(ttl_sec: float = 600.0) -> int:
    """Сколько РЕАЛЬНО посчитанных оценок накоплено в журнале сделок.

    Считаем из файла, а не из памяти процесса: счётчик в памяти обнулялся бы
    при каждом рестарте, и enforce то включался бы, то выключался сам по себе.
    Результат кешируется — гейт зовут на каждом символе каждого прохода.
    """
    import time

    now = time.time()
    if now - float(_SAMPLE_CACHE["ts"]) < ttl_sec:
        return int(_SAMPLE_CACHE["value"])

    count = 0
    try:
        import json
        from pathlib import Path

        from services.ml_trade_logger import MLTradeLogger

        path = Path(MLTradeLogger().path)
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if '"tz_shadow"' not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    plan = row.get("plan") or row.get("plan_json") or {}
                    shadow = plan.get("tz_shadow") if isinstance(plan, dict) else None
                    if isinstance(shadow, dict) and shadow.get("evaluated"):
                        count += 1
    except Exception:
        # Не смогли прочитать — считаем выборку нулевой. Это выключает enforce,
        # то есть ошибка чтения делает систему мягче, а не жёстче.
        count = 0

    _SAMPLE_CACHE["ts"] = now
    _SAMPLE_CACHE["value"] = count
    return count


def enforced_families() -> frozenset[str]:
    raw = str(getattr(settings, "TZ_ENFORCE_CONDITIONS", "") or "")
    return frozenset(x.strip().lower() for x in raw.split(",") if x.strip())


def should_block(shadow: TZShadow, *, sample_size: int) -> tuple[bool, str]:
    """Блокировать ли вход. Возвращает (блокировать, причина решения).

    Причина возвращается ВСЕГДА, в том числе когда не блокируем: в разборе
    нужно видеть, что удержало enforce — режим, размер выборки или то, что
    сработавшие условия не входят в список включённых.
    """
    if str(getattr(settings, "TZ_MODE", "shadow")).lower() != "enforce":
        return False, "mode_shadow"
    if not shadow.evaluated:
        # Нет индикаторов — не повод не пускать. Fail-open, как и в тени.
        return False, "not_evaluated"

    min_sample = int(getattr(settings, "TZ_ENFORCE_MIN_SAMPLE", 40))
    if int(sample_size) < min_sample:
        return False, f"sample_too_small:{sample_size}<{min_sample}"

    families = enforced_families()
    if not families:
        return False, "no_conditions_enabled"

    hit = sorted({_family(code) for code in shadow.failed} & families)
    if not hit:
        return False, "enabled_conditions_passed"
    return True, "blocked_by:" + ",".join(hit)
