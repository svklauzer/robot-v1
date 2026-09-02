"""(#okx-satellite-2026-09-02) Не дать переключению ACTIVE_EXCHANGE молча
осиротить открытые позиции/ордера на бирже, с которой ушли.

Нет столбца "какая биржа" в Signal/Position/Order (и не нужен — торгует
всегда ровно одна биржа), поэтому проверяем НЕактивную биржу напрямую через
её API, а не через БД: если она отвечает и на ней есть что-то открытое,
значит переключение застало сделки врасплох.

Два разных отказа:
  - неактивная биржа НЕДОСТУПНА (сеть/DNS/размыкатель) → fail-open. Она и так
    не торгует, временная недоступность её API не повод останавливать
    активную биржу.
  - неактивная биржа ОТВЕТИЛА и показала открытые позиции/ордера → fail-closed
    для НОВОЙ торговли на активной бирже, пока владелец не разберётся руками.
    API/дашборд при этом продолжают работать — это гейт входа, не крах.

Результат кэшируется (see _CACHE_TTL_SEC): при живом ACTIVE_EXCHANGE=htx
переключение не бывает горячим путём, и дёргать сеть неактивной OKX на
каждый тик торгового цикла (раз в SCAN_INTERVAL_SEC) — лишняя стоимость без
пользы.
"""
from __future__ import annotations

import logging
import time

from core.config import settings
from core.logging import get_logger, log_event

logger = get_logger(__name__)

_CACHE_TTL_SEC = 300.0  # 5 минут

_cache: dict | None = None
_cache_at: float = 0.0


def _inactive_client():
    if settings.active_exchange == "okx":
        from services.htx_client import HTXClient

        return "htx", HTXClient()
    from services.okx_client import OKXClient

    return "okx", OKXClient()


def _position_is_open(p: dict) -> bool:
    for key in ("contracts", "contractSize", "size", "amount"):
        val = p.get(key)
        if val is None:
            continue
        try:
            # Первое известное и разбираемое поле размера — решающее: не
            # продолжаем искать другое поле, если это уже сказало "ноль".
            return abs(float(val)) > 1e-12
        except (TypeError, ValueError):
            continue
    # Ни одно известное поле размера не распозналось — безопаснее считать
    # запись открытой, чем молча пропустить реальную позицию.
    return True


def check(*, force: bool = False) -> dict:
    """Есть ли открытая активность на неактивной бирже. Кэшируется на
    _CACHE_TTL_SEC; force=True обходит кэш (используется дашбордом по кнопке
    "проверить сейчас")."""
    global _cache, _cache_at
    now = time.time()
    if not force and _cache is not None and (now - _cache_at) < _CACHE_TTL_SEC:
        return _cache

    inactive_name, client = _inactive_client()
    result: dict = {
        "checked_at": now,
        "active_exchange": settings.active_exchange,
        "inactive_exchange": inactive_name,
        "reachable": False,
        "open_orders": 0,
        "open_positions": 0,
        "safe": True,
        "error": None,
    }

    try:
        orders = client.fetch_open_orders() or []
        try:
            positions = client.fetch_positions() or []
        except Exception:
            # fetch_positions не у всех рынков доступен (см. HTXClient/OKXClient
            # — уже сами fail-open в None/[]); открытые ордера всё равно проверены.
            positions = []
        open_positions = [p for p in positions if _position_is_open(p)]

        result["reachable"] = True
        result["open_orders"] = len(orders)
        result["open_positions"] = len(open_positions)
        result["safe"] = not (orders or open_positions)

        if not result["safe"]:
            log_event(
                logger, logging.WARNING, "exchange_switch_unsafe",
                inactive_exchange=inactive_name,
                open_orders=result["open_orders"],
                open_positions=result["open_positions"],
                note=(
                    f"{inactive_name} has open orders/positions while "
                    f"{settings.active_exchange} is ACTIVE_EXCHANGE — new "
                    f"entries on the active exchange are held until this "
                    f"is resolved manually"
                ),
            )
    except Exception as e:  # noqa: BLE001 — недоступность неактивной биржи не блокер
        result["error"] = f"{type(e).__name__}: {e}"
        log_event(
            logger, logging.INFO, "exchange_switch_guard_unreachable",
            inactive_exchange=inactive_name, error=result["error"],
            note="inactive exchange unreachable — fail-open, it isn't trading anyway",
        )

    _cache = result
    _cache_at = now
    return result
