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


def get_exchange_client():
    """Возвращает HTXClient() или OKXClient() по settings.active_exchange."""
    if settings.active_exchange == "okx":
        from services.okx_client import OKXClient

        return OKXClient()

    from services.htx_client import HTXClient

    return HTXClient()
