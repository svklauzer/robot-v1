"""Вход против импульса 15m — ошибочная сторона разворота (#momentum-reversal-2026-11-23).

Исходная версия (#momentum-late-2026-09-12) блокировала ALIGNED-импульс
(up+bullish, down+bearish) по замеру 12.09. Свежая когорта (ноябрь, API)
премису перевернула:

    trend_down + bearish (продолжение)  октябрь  +0.44 R/сделку  -> НЕ режем
    trend_down + bullish (контр-импульс) ноябрь  −0.54 USDT/сделку -> режем
    long + bullish за ноябрь = 0 сделок (система не покупает молодой аптренд)

Поэтому гейт развёрнут: блокируем OPPOSED (импульс ПРОТИВ стороны сделки),
а ALIGNED оставляем в тени. Оба поведения — флаги, откат без деплоя кода.

Причина входа собирается как `mtf_trend_<up|down>_<импульс 15m>_<объём>_structure_confirmed`.
Режим `MOMENTUM_GATE_MODE`: off | shadow | enforce.
"""
from __future__ import annotations

from core.config import settings

# Импульс по направлению тренда: ход уже идёт (продолжение).
_ALIGNED = {
    "up": frozenset({"bullish", "overheated"}),
    "down": frozenset({"bearish", "oversold"}),
}

# Импульс ПРОТИВ стороны сделки: ошибочная сторона разворота.
# шорт при бычьем 15m = продаём молодой аптренд; лонг при медвежьем = ловим нож.
_OPPOSED = {
    "up": frozenset({"bearish", "oversold"}),
    "down": frozenset({"bullish", "overheated"}),
}


def mode() -> str:
    value = str(getattr(settings, "MOMENTUM_GATE_MODE", "shadow") or "shadow").lower()
    return value if value in ("off", "shadow", "enforce") else "shadow"


def evaluate(reason: str | None) -> dict | None:
    """Вердикт по причине входа трендового движка. None — не тренд-вход или
    гейт выключен: другие движки импульс в причину не пишут."""
    current = mode()
    if current == "off":
        return None
    tokens = str(reason or "").split("_")
    if len(tokens) < 4 or tokens[0] != "mtf" or tokens[1] != "trend" \
            or tokens[2] not in _ALIGNED:
        return None
    direction, momentum = tokens[2], tokens[3]
    aligned = momentum in _ALIGNED[direction]
    opposed = momentum in _OPPOSED[direction]

    block_aligned = bool(getattr(settings, "MOMENTUM_GATE_BLOCK_ALIGNED", False))
    block_opposed = bool(getattr(settings, "MOMENTUM_GATE_BLOCK_OPPOSED", True))
    would_block = (block_aligned and aligned) or (block_opposed and opposed)

    return {
        "mode": current,
        "direction": direction,
        "momentum": momentum,
        "aligned": aligned,
        "opposed": opposed,
        # Вердикт независимо от режима — тень сравнивается с исходами по нему.
        "would_block": would_block,
        "blocks": current == "enforce" and would_block,
    }
