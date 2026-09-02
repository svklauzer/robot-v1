"""(#okx-satellite-2026-09-02) Единственная точка выбора активного биржевого
клиента. Каждый сервис, который раньше жёстко конструировал HTXClient(),
переключается на get_exchange_client() — так переключение биржи (settings.
ACTIVE_EXCHANGE, только вручную через Render env + редеплой) достаточно
изменить в одном месте, а не в восьми.

Ленивые импорты внутри веток — тот же стиль, что уже используют grid_engine.py
и venue_compare.py: не тянуть ccxt.okx-конфигурацию, если OKX ни разу не
активна.
"""
from __future__ import annotations

from core.config import settings


def get_exchange_client(exchange: str | None = None):
    """Возвращает HTXClient() или OKXClient().

    Без аргумента — по settings.active_exchange (текущая активная биржа,
    поведение как раньше). С явным `exchange` — под конкретную биржу,
    независимо от того, что сейчас активно. Это нужно, чтобы вести уже
    открытый Signal через биржу, на которой он реально открыт
    (#okx-satellite-exchange-routing-2026-09-02, Signal.exchange), а не через
    ту, что стала активной ПОСЛЕ его открытия — иначе переключение
    ACTIVE_EXCHANGE задним числом "переносит" уже открытую позицию на чужой
    клиент (цена, комиссии и само исполнение закрытия уйдут не туда).
    """
    ex = str(exchange or settings.active_exchange or "htx").strip().lower()
    if ex == "okx":
        from services.okx_client import OKXClient

        return OKXClient()

    from services.htx_client import HTXClient

    return HTXClient()
