"""Смысл причин закрытия, общий для меток, статистики и пауз (#tp2-stage-2026-09-12).

С 12.09 TP2 — этап: на нём фиксируется доля остатка, хвост едет под трейлом и
закрывается как `tp2_trail_stop` или `tp2_trail_giveback`. Раньше дошедшая до
TP2 сделка почти всегда закрывалась одной причиной `tp2_reached` (правило на
92% пути перехватывало этап), и метки ML, счётчик TP2 в аналитике и пауза
перед повторным входом узнавали TP2 только по ней. Без общего правила сделки,
прошедшие TP2, для обучения стали бы «не дошедшими».
"""
from __future__ import annotations

# Закрытия хвоста после фиксации доли на TP2: сделка до TP2 дошла.
TP2_STAGE_REASONS: frozenset[str] = frozenset({"tp2_trail_stop", "tp2_trail_giveback"})


def reached_tp2(reason: str | None, plan: dict | None = None) -> bool:
    """Дошла ли сделка до TP2: закрыта на нём, на этапе после него или несёт
    запись о фиксации доли на TP2."""
    reason = str(reason or "")
    if reason == "tp2_reached" or reason in TP2_STAGE_REASONS:
        return True
    return bool((plan or {}).get("tp2_partial"))
