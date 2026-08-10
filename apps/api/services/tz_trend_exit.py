"""Выходы трендового движка по ТЗ. (#tz-trend-engine-2026-08-03)
это файл tz_trend_exit.py
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
    atr: float | None = None,  # <-- Новый параметр: текущая волатильность
    entry_price: float | None = None,  # <-- Новый параметр: цена входа (для аварийного стопа)    
) -> TZExit:
    """Пора ли закрывать по условиям ТЗ + Dynamic ATR Buffer + Hard Stop."""
    is_long = str(side or "").lower() in ("long", "buy")
    close_v = _num(close)
    kama_v = _num(kama)
    adx_v = _num(adx)
    peak_v = _num(adx_peak)
    obv_v = _num(obv)
    obv_ema_v = _num(obv_ema)
    atr_v = _num(atr)
    entry_v = _num(entry_price)

    triggers: list[str] = []

    # 0. АВАРИЙНЫЙ СТОП (Hard Stop) - Защита от краха/сквиза
    # Если цена ушла против нас больше чем на X% от входа, выходим немедленно.
    # Это страховка, если KAMA не успела развернуться.
    if entry_v is not None and close_v is not None:
        max_loss_pct = float(getattr(settings, "TZ_HARD_STOP_LOSS_PCT", 5.0)) / 100.0
        if is_long:
            hard_stop_level = entry_v * (1 - max_loss_pct)
            if close_v < hard_stop_level:
                triggers.append(f"hard_stop_loss:{max_loss_pct*100:.1f}%")
        else:
            hard_stop_level = entry_v * (1 + max_loss_pct)
            if close_v > hard_stop_level:
                triggers.append(f"hard_stop_loss:{max_loss_pct*100:.1f}%")

    # 1. Экстренный выход: цена ПРОБИЛА KAMA с буфером.
    if close_v is not None and kama_v is not None and kama_v > 0:
        # Проверяем, включен ли режим ATR
        use_atr = bool(getattr(settings, "TZ_USE_DYNAMIC_ATR_STOPS", False))
        
        if use_atr and atr_v is not None and atr_v > 0:
            # Динамический буфер: ATR * множитель
            # Увеличен дефолт с 0.8 до 2.0, чтобы не выбивало шумом
            mult = float(getattr(settings, "TZ_EXIT_KAMA_BUFFER_ATR_MULT", 2.0))
            buffer_value = atr_v * mult
            level = kama_v - buffer_value if is_long else kama_v + buffer_value
        else:
            # Legacy: процент от цены
            buf = float(getattr(settings, "TZ_EXIT_KAMA_BUFFER_PCT",
                                getattr(settings, "TZ_STOP_KAMA_BUFFER_PCT", 0.15))) / 100.0
            level = kama_v * (1 - buf) if is_long else kama_v * (1 + buf)
            
        broken = close_v < level if is_long else close_v > level
        if broken:
            triggers.append("kama_broken")

    # 2. Ослабление тренда (ADX) - порог снижен до 4.0 для более раннего выхода
    peak_min = float(getattr(settings, "TZ_EXIT_ADX_PEAK_MIN", 50.0))
    fade = float(getattr(settings, "TZ_EXIT_ADX_FADE", 4.0))
    if adx_v is not None and peak_v is not None and peak_v >= peak_min:
        if (peak_v - adx_v) >= fade:
            triggers.append(f"adx_fading_from_peak:{peak_v:.1f}->{adx_v:.1f}")

    # 3. Объёмный разворот (OBV)
    if obv_v is not None and obv_ema_v is not None:
        against = obv_v < obv_ema_v if is_long else obv_v > obv_ema_v
        if against:
            triggers.append("obv_reversed")

    if not triggers:
        return TZExit(False, "trend_intact", (), kama_v, adx_v, peak_v,
                      (obv_v - obv_ema_v) if (obv_v is not None and obv_ema_v is not None) else None)

    armed = {
        x.strip().lower()
        for x in str(getattr(settings, "TZ_EXIT_CONDITIONS", "kama") or "").split(",")
        if x.strip()
    }
    fired = [t for t in triggers if _family(t) in armed or t.startswith("hard_stop")]

    return TZExit(
        exit=bool(fired),
        reason=("tz_" + _family(fired[0])) if fired else "trigger_not_armed",
        triggers=tuple(triggers),
        kama=kama_v,
        adx=adx_v,
        adx_peak=peak_v,
        obv_vs_ema=(obv_v - obv_ema_v) if (obv_v is not None and obv_ema_v is not None) else None,
    )

# Функция расчета стопа также обновляется для использования в сайзинге
def stop_from_kama(
    *, 
    side: str, 
    kama: float | None, 
    buffer_pct: float | None = None,
    atr: float | None = None, # Новый параметр
) -> float | None:
    kama_v = _num(kama)
    if kama_v is None or kama_v <= 0:
        return None
    
    use_atr = bool(getattr(settings, "TZ_USE_DYNAMIC_ATR_STOPS", False))
    
    if use_atr and atr is not None:
        mult = float(getattr(settings, "TZ_STOP_LOSS_ATR_MULT", 2))
        buffer_value = atr * mult
        stop_price = kama_v - buffer_value if str(side).lower() in ("long", "buy") else kama_v + buffer_value
    else:
        buf = float(buffer_pct if buffer_pct is not None
                    else getattr(settings, "TZ_STOP_KAMA_BUFFER_PCT", 0.15)) / 100.0
        stop_price = kama_v * (1 - buf) if str(side).lower() in ("long", "buy") else kama_v * (1 + buf)
        
    return round(stop_price, 8)
