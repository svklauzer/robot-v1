"""Grid API — управление и статистика умной сетки (owner-only).

Сетка изолирована от тренд-движка: вкл/выкл не трогает открытые тренд-позиции.
"""
from fastapi import APIRouter, Body, Depends

from core.config import settings
from core.security import require_owner_action
from services.grid_engine import GridEngine
from services.grid_store import GridStore

router = APIRouter(prefix="/grid", tags=["grid"])


def _config() -> dict:
    return {
        "symbols": settings.grid_symbols,
        "timeframe": settings.GRID_TIMEFRAME,
        "lines": settings.GRID_LINES,
        "base_order_usdt": settings.GRID_BASE_ORDER_USDT,
        "vol_multiplier": settings.GRID_VOL_MULTIPLIER,
        "step_multiplier": settings.GRID_STEP_MULTIPLIER,
        "vol_coeff_k": settings.GRID_VOL_COEFF,
        "atr_period": settings.GRID_ATR_PERIOD,
        "tp_pct": settings.GRID_TP_PCT,
        "sl_atr_mult": settings.GRID_SL_ATR_MULT,
        "max_safety_orders": settings.GRID_MAX_SAFETY_ORDERS,
        "max_used_margin_pct": settings.GRID_MAX_USED_MARGIN_PCT,
        "leverage": settings.GRID_LEVERAGE,
        "rearm": settings.GRID_REARM,
        "market": settings.execution_market_type,
        "flip_confirm_ticks": settings.GRID_FLIP_CONFIRM_TICKS,
        "flip_cooldown_sec": settings.GRID_FLIP_COOLDOWN_SEC,
        "regime_band_pct": settings.GRID_REGIME_EMA_BAND_PCT,
    }


@router.get("/state", dependencies=[Depends(require_owner_action)])
def grid_state():
    """Полное состояние сетки: статус, активные циклы, уровни, PnL, история."""
    return {"config": _config(), **GridEngine().snapshot()}


@router.get("/config", dependencies=[Depends(require_owner_action)])
def grid_config():
    return _config()


@router.post("/enable", dependencies=[Depends(require_owner_action)])
def grid_enable():
    """Включить сетку. Открытые тренд-ордера не трогаются — сетка идёт параллельно."""
    GridStore().set_enabled(True)
    return GridStore().summary()


@router.post("/disable", dependencies=[Depends(require_owner_action)])
def grid_disable():
    """Выключить сетку (новые уровни не открываются; активные циклы можно закрыть вручную)."""
    GridStore().set_enabled(False)
    return GridStore().summary()


@router.post("/run-once", dependencies=[Depends(require_owner_action)])
def grid_run_once():
    """Ручной прогон одного тика по всем grid-символам (для отладки)."""
    return GridEngine().tick_all()


@router.post("/close/{symbol:path}", dependencies=[Depends(require_owner_action)])
def grid_close(symbol: str):
    """Закрыть цикл по символу по агрегату корзины (без пыли)."""
    return GridEngine().close_now(symbol, reason="manual_close")


@router.get("/history", dependencies=[Depends(require_owner_action)])
def grid_history(limit: int = 50):
    h = GridStore().history
    return {"count": len(h), "items": h[-int(limit):][::-1]}
