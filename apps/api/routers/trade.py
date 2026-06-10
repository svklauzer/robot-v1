from fastapi import APIRouter, Depends
from pydantic import BaseModel
from core.config import settings
from core.security import require_owner_action
from services.cost_engine import CostEngine
from services.trade_plan import TradePlanBuilder

router = APIRouter(prefix="/trade", tags=["trade"])


class CostPreviewRequest(BaseModel):
    symbol: str = "BTC/USDT"
    market_type: str = "spot"
    side: str = "long"
    entry_price: float
    exit_price: float
    qty: float
    liquidity: str = "taker"
    holding_funding_periods: int = 1
    leverage: int | None = None


class TradePlanRequest(BaseModel):
    symbol: str
    side: str = "long"
    entry: float
    stop: float
    tp1: float
    tp2: float
    balance_usdt: float = 1000


@router.post("/cost-preview", dependencies=[Depends(require_owner_action)])
def trade_cost_preview(payload: CostPreviewRequest):
    try:
        engine = CostEngine()
        preview = engine.estimate(
            symbol=payload.symbol,
            market_type=payload.market_type,
            side=payload.side,
            entry_price=payload.entry_price,
            exit_price=payload.exit_price,
            qty=payload.qty,
            liquidity=payload.liquidity,
            holding_funding_periods=payload.holding_funding_periods,
            leverage=payload.leverage,
        )
        return {
            "status": "ok",
            "cost": engine.to_dict(preview),
            "config": {
                "trading_mode": settings.TRADING_MODE,
                "market_type": settings.MARKET_TYPE,
                "enable_live_orders": settings.ENABLE_LIVE_ORDERS,
                "enable_futures": settings.ENABLE_FUTURES,
                "spot_taker_fee": settings.SPOT_TAKER_FEE,
                "spot_maker_fee": settings.SPOT_MAKER_FEE,
                "futures_taker_fee": settings.FUTURES_TAKER_FEE,
                "futures_maker_fee": settings.FUTURES_MAKER_FEE,
                "slippage_buffer_pct": settings.SLIPPAGE_BUFFER_PCT,
                "funding_buffer_pct": settings.FUNDING_BUFFER_PCT,
                "allow_shorts": settings.ALLOW_SHORTS,
                "execution_market": settings.EXECUTION_MARKET,
            },
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@router.post("/build-plan", dependencies=[Depends(require_owner_action)])
def build_trade_plan(payload: TradePlanRequest):
    builder = TradePlanBuilder()
    plan = builder.build_plan(
        symbol=payload.symbol,
        side=payload.side,
        entry_price=payload.entry,
        stop_price=payload.stop,
        tp1=payload.tp1,
        tp2=payload.tp2,
        balance_usdt=payload.balance_usdt,
    )
    return {"status": "ok", "trade_plan": plan.__dict__}
