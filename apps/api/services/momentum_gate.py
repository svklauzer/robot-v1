"""Вход, когда импульс уже смотрит по тренду (#momentum-late-2026-09-12).

Разбор стопов 12.09 по причинам входа трендового движка. Причина собирается
как `mtf_trend_<up|down>_<импульс 15m>_<объём>_structure_confirmed`, импульс —
`_momentum_state` на 15m: bullish (RSI > 52 и гистограмма MACD > 0), bearish —
зеркально, overheated/oversold — края RSI, иначе neutral.

    импульс по тренду (up+bullish, down+bearish)   79 сделок  стопов 46%  −43.4 USDT
    нейтральный                                     60 сделок  стопов 30%   +5.8 USDT

Разрыв держится в обеих половинах по времени (ранняя: −16.3 против +0.1,
поздняя: −27.1 против +5.7) и в обе стороны (лонги −21.7, шорты −21.6). Это та
же картина, что «входы приходятся на конец хода»: бычий импульс на 15m в
восходящем тренде — ход уже случился. При этом оценка уверенности за такой
импульс ДОПЛАЧИВАЕТ (momentum_score 70 против 45 у нейтрального) — та же
ловушка, что с грейдом A.

Режим `MOMENTUM_GATE_MODE`: off | shadow | enforce. В shadow вердикт пишется в
план сделки и ничего не блокирует; в enforce такой вход не открывается. Решение
о включении — за владельцем, по данным тени.
"""
from __future__ import annotations

from core.config import settings

# Импульс, совпадающий с направлением тренда: ход уже идёт.
_ALIGNED = {
    "up": frozenset({"bullish", "overheated"}),
    "down": frozenset({"bearish", "oversold"}),
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
    return {
        "mode": current,
        "direction": direction,
        "momentum": momentum,
        "aligned": aligned,
        # Вердикт гейта независимо от режима — чтобы тень можно было сравнить
        # с исходами, не повторяя логику гейта по памяти.
        "would_block": aligned,
        "blocks": current == "enforce" and aligned,
    }
