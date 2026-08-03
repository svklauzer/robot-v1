"""Выходы трендового движка по ТЗ. (#tz-trend-engine-2026-08-03)

Что говорит ТЗ
--------------
Три условия закрытия, все три — про слом тренда, а не про дистанцию цены:

  1. Экстренный выход: закрытие ниже KAMA на таймфрейме подтверждения.
  2. Ослабление: ADX разворачивается вниз из зоны выше 50.
  3. Объёмный разворот: OBV падает ниже своей EMA(20).

Фиксированного стопа в ТЗ нет вовсе.

Почему это отвечает на замер
----------------------------
107 стопов из 342 сделок, −223.76 USDT, НИ ОДНОЙ прибыльной. Это 76% всего
убытка. При этом средний MAE всего −0.618%, а стопы стоят на 1–3%: типичная
сделка до стопа не доходит близко. Значит стоп не защищает — он срабатывает
только там, где цена пошла жёстко против, и тогда уже поздно.

Стоп по дистанции отвечает на вопрос «сколько я готов потерять». Он не отвечает
на вопрос «жив ли ещё тренд». ТЗ предлагает второе, и по нашим данным это
уместнее.

Развилка, которую нельзя обойти молчанием
-----------------------------------------
Размер позиции у нас считается ОТ дистанции до стопа:

    qty = risk_usdt / (расстояние до стопа)

Убрать стоп — значит убрать якорь сайзинга. Поэтому «просто заменить выход»
нельзя: поменяется и размер позиции, и вся риск-модель.

Решение — не отказ от стопа, а перенос его туда, где ТЗ и так объявляет тренд
сломанным: за линию KAMA плюс буфер. Тогда:

  * стоп и логика выхода ГОВОРЯТ ОДНО И ТО ЖЕ (сейчас они спорят: ATR-стоп
    может выбить сделку, тренд которой по KAMA цел, и наоборот);
  * якорь сайзинга сохраняется — дистанция до KAMA известна на входе;
  * остаётся защита на случай, если цикл сопровождения не отработал.

Дистанция до KAMA становится ЕСТЕСТВЕННЫМ размером риска сетапа: она мала, когда
цена прижата к линии (хороший вход по ТЗ — откат к KAMA), и велика, когда цена
растянута (плохой вход, который заодно получит меньший размер).

Аварийный дальний стоп остаётся сверху как предохранитель от разрыва цены —
это МОЁ добавление, в ТЗ его нет, и оно должно быть видно как отдельное решение.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from core.config import settings


# Условие → семейство. Явная таблица вместо разбора имени по подчёркиванию:
# прежний `code.split("_")[0]` давал "kama" из "kama_broken" по случайности
# именования и молча сломался бы на любом условии вроде "price_below_kama".
EXIT_FAMILY = {
    "kama_broken": "kama",
    "adx_fading_from_peak": "adx",
    "obv_reversed": "obv",
}


def _family(code: str) -> str:
    return EXIT_FAMILY.get(str(code).split(":", 1)[0], "unknown")


@dataclass(frozen=True)
class TZExit:
    exit: bool
    reason: str
    triggers: tuple[str, ...]
    kama: float | None
    adx: float | None
    adx_peak: float | None
    obv_vs_ema: float | None

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["triggers"] = list(self.triggers)
        return payload


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate(
    *,
    side: str,
    close: float,
    kama: float | None,
    adx: float | None,
    adx_peak: float | None,
    obv: float | None,
    obv_ema: float | None,
) -> TZExit:
    """Пора ли закрывать по условиям ТЗ.

    `adx_peak` — максимум ADX за время жизни сделки. Условие ТЗ «разворот вниз
    из зоны выше 50» требует ПАМЯТИ: мгновенное значение не отличает «ADX 45 по
    дороге вверх» от «ADX 45 после пика 60». Без памяти условие превращается в
    «ADX < 50», что закрывало бы каждую сделку, не дошедшую до 50.
    """
    is_long = str(side or "").lower() in ("long", "buy")
    close_v = _num(close)
    kama_v = _num(kama)
    adx_v = _num(adx)
    peak_v = _num(adx_peak)
    obv_v = _num(obv)
    obv_ema_v = _num(obv_ema)

    triggers: list[str] = []

    # 1. Экстренный выход: цена закрылась по ту сторону KAMA.
    if close_v is not None and kama_v is not None:
        broken = close_v < kama_v if is_long else close_v > kama_v
        if broken:
            triggers.append("kama_broken")

    # 2. Ослабление тренда: ADX развернулся вниз из зоны выше порога.
    #    Порог 50 из ТЗ здесь безопаснее, чем 23 на входе: он не запрещает
    #    сделки, а лишь фиксирует прибыль у пика силы.
    peak_min = float(getattr(settings, "TZ_EXIT_ADX_PEAK_MIN", 50.0))
    fade = float(getattr(settings, "TZ_EXIT_ADX_FADE", 3.0))
    if adx_v is not None and peak_v is not None and peak_v >= peak_min:
        if (peak_v - adx_v) >= fade:
            triggers.append(f"adx_fading_from_peak:{peak_v:.1f}->{adx_v:.1f}")

    # 3. Объёмный разворот: OBV ушёл за свою EMA(20).
    if obv_v is not None and obv_ema_v is not None:
        against = obv_v < obv_ema_v if is_long else obv_v > obv_ema_v
        if against:
            triggers.append("obv_reversed")

    if not triggers:
        return TZExit(False, "trend_intact", (), kama_v, adx_v, peak_v,
                      (obv_v - obv_ema_v) if (obv_v is not None and obv_ema_v is not None) else None)

    # Какие из условий имеют право закрывать. По умолчанию — только слом KAMA:
    # это единственное условие ТЗ, которое говорит «тренда больше нет», а не
    # «тренд слабеет». Остальные включаются отдельно, когда наберётся выборка.
    armed = {
        x.strip().lower()
        for x in str(getattr(settings, "TZ_EXIT_CONDITIONS", "kama") or "").split(",")
        if x.strip()
    }
    fired = [t for t in triggers if _family(t) in armed]

    # Причина — по СЕМЕЙСТВУ, а не по полному коду. Отчёт «куда уходят деньги»
    # группирует сделки по close_reason: если в причину попадут значения ADX,
    # каждая сделка получит уникальную строку и группировка развалится.
    # Подробности остаются в `triggers`.
    return TZExit(
        exit=bool(fired),
        reason=("tz_" + _family(fired[0])) if fired else "trigger_not_armed",
        triggers=tuple(triggers),
        kama=kama_v,
        adx=adx_v,
        adx_peak=peak_v,
        obv_vs_ema=(obv_v - obv_ema_v) if (obv_v is not None and obv_ema_v is not None) else None,
    )


def stop_from_kama(*, side: str, kama: float | None, buffer_pct: float | None = None) -> float | None:
    """Стоп на линии слома тренда, а не на произвольной дистанции ATR.

    Сейчас ATR-стоп и логика выхода спорят между собой: стоп может выбить
    сделку, тренд которой по KAMA цел, и наоборот — держать сделку, у которой
    тренд уже сломан. Один источник истины устраняет спор и заодно сохраняет
    якорь сайзинга: дистанция до KAMA известна на входе.
    """
    kama_v = _num(kama)
    if kama_v is None or kama_v <= 0:
        return None
    buf = float(buffer_pct if buffer_pct is not None
                else getattr(settings, "TZ_STOP_KAMA_BUFFER_PCT", 0.15)) / 100.0
    is_long = str(side or "").lower() in ("long", "buy")
    return round(kama_v * (1 - buf) if is_long else kama_v * (1 + buf), 8)
