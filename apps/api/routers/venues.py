"""Роутер /venues — кросс-биржевая телеметрия HTX ↔ Kraken (P1 read-only).

(#kraken-p1-2026-07-18) Только чтение публичных данных: funding-спреды и
health обеих площадок. Никаких ордеров/ключей — торговый контур не затронут.
"""

from fastapi import APIRouter, Depends

from core.config import settings
from core.security import require_owner_action

router = APIRouter(prefix="/venues", tags=["venues"])


@router.get("/compare", dependencies=[Depends(require_owner_action)])
def venues_compare(symbols: str | None = None, fresh: bool = False):
    """Funding-спред HTX↔Kraken по вселенной символов (кэш 60с, ?fresh=true — мимо кэша)."""
    if not bool(getattr(settings, "KRAKEN_ENABLED", True)):
        return {"status": "disabled", "note": "KRAKEN_ENABLED=false"}
    try:
        from services.venue_compare import VenueCompareService
        symbol_list = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else None
        return VenueCompareService().compare(symbol_list, use_cache=not fresh)
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": str(e)}


@router.get("/compare/history", dependencies=[Depends(require_owner_action)])
def venues_compare_history(days: int = 7):
    """История funding-спредов (почасовые снапшоты воркера): агрегаты по символам,
    устойчивость направления, дневная динамика. Основание для решения по P2."""
    if not bool(getattr(settings, "KRAKEN_ENABLED", True)):
        return {"status": "disabled", "note": "KRAKEN_ENABLED=false"}
    try:
        from services.venue_compare import VenueSpreadHistory
        return VenueSpreadHistory().history(days=min(max(days, 1), 90))
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": str(e)}


@router.get("/health", dependencies=[Depends(require_owner_action)])
def venues_health():
    """Доступность/латентность HTX и Kraken (задел под data-failover, P3)."""
    if not bool(getattr(settings, "KRAKEN_ENABLED", True)):
        return {"status": "disabled", "note": "KRAKEN_ENABLED=false"}
    try:
        from services.venue_compare import VenueCompareService
        return VenueCompareService().health()
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": str(e)}
