from dataclasses import dataclass

from core.config import settings
from services.htx_client import HTXClient


@dataclass
class CostPreview:
    symbol: str
    market_type: str
    side: str
    entry_price: float
    exit_price: float
    qty: float
    leverage: int

    entry_notional: float
    exit_notional: float

    entry_fee: float
    exit_fee: float
    slippage_buffer: float
    funding_buffer: float

    gross_pnl: float
    total_cost: float
    net_pnl: float
    net_pnl_pct: float

    fee_rate: float
    fee_source: str


class CostEngine:
    def __init__(self):
        self.htx = HTXClient()

    def fee_rate(
        self,
        symbol: str,
        market_type: str,
        liquidity: str = "taker",
    ) -> tuple[float, str]:
        """
        Комиссия берётся в таком порядке:
        1. HTX/CCXT API
        2. market metadata
        3. settings fallback

        Возвращает:
        (fee_rate, source)
        """
        try:
            rates = self.htx.trading_fee_rates(
                symbol=symbol,
                market_type=market_type,
            )

            rate = rates.get("maker") if liquidity == "maker" else rates.get("taker")
            source = rates.get("source", "unknown")

            if rate is not None:
                return float(rate), source

        except Exception as e:
            print(f"[COST FEE RATE ERROR] {symbol}: {e}")

        if market_type == "spot":
            fallback = settings.SPOT_MAKER_FEE if liquidity == "maker" else settings.SPOT_TAKER_FEE
        elif market_type in ["swap", "futures", "perp"]:
            fallback = settings.FUTURES_MAKER_FEE if liquidity == "maker" else settings.FUTURES_TAKER_FEE
        else:
            fallback = settings.SPOT_TAKER_FEE

        return float(fallback), "fallback_settings"

    def estimate(
        self,
        symbol: str,
        market_type: str,
        side: str,
        entry_price: float,
        exit_price: float,
        qty: float,
        liquidity: str = "taker",
        holding_funding_periods: int = 1,
        leverage: int | None = None,
    ) -> CostPreview:
        entry_price = float(entry_price)
        exit_price = float(exit_price)
        qty = float(qty)

        leverage_value = leverage or (settings.FUTURES_LEVERAGE if market_type != "spot" else 1)

        entry_notional = entry_price * qty
        exit_notional = exit_price * qty

        fee_rate_value, fee_source = self.fee_rate(
            symbol=symbol,
            market_type=market_type,
            liquidity=liquidity,
        )

        entry_fee = entry_notional * fee_rate_value
        exit_fee = exit_notional * fee_rate_value

        slippage_buffer = entry_notional * settings.SLIPPAGE_BUFFER_PCT

        if market_type == "spot":
            funding_buffer = 0.0
        else:
            funding_buffer = entry_notional * settings.FUNDING_BUFFER_PCT * holding_funding_periods

        side_value = str(side or "").lower().strip()

        if side_value in ["long", "buy"]:
            gross_pnl = (exit_price - entry_price) * qty
        elif side_value in ["short", "sell"]:
            gross_pnl = (entry_price - exit_price) * qty
        else:
            raise ValueError(f"Unsupported side for CostEngine.estimate: {side}")

        total_cost = entry_fee + exit_fee + slippage_buffer + funding_buffer
        net_pnl = gross_pnl - total_cost

        base_margin = entry_notional / leverage_value if leverage_value > 0 else entry_notional
        net_pnl_pct = (net_pnl / base_margin) * 100 if base_margin > 0 else 0.0

        return CostPreview(
            symbol=symbol,
            market_type=market_type,
            side=side_value,
            entry_price=entry_price,
            exit_price=exit_price,
            qty=qty,
            leverage=leverage_value,

            entry_notional=round(entry_notional, 6),
            exit_notional=round(exit_notional, 6),

            entry_fee=round(entry_fee, 6),
            exit_fee=round(exit_fee, 6),
            slippage_buffer=round(slippage_buffer, 6),
            funding_buffer=round(funding_buffer, 6),

            gross_pnl=round(gross_pnl, 6),
            total_cost=round(total_cost, 6),
            net_pnl=round(net_pnl, 6),
            net_pnl_pct=round(net_pnl_pct, 4),

            fee_rate=round(fee_rate_value, 8),
            fee_source=fee_source,
        )

    def to_dict(self, preview: CostPreview) -> dict:
        return {
            "symbol": preview.symbol,
            "market_type": preview.market_type,
            "side": preview.side,
            "entry_price": preview.entry_price,
            "exit_price": preview.exit_price,
            "qty": preview.qty,
            "leverage": preview.leverage,

            "entry_notional": preview.entry_notional,
            "exit_notional": preview.exit_notional,

            "entry_fee": preview.entry_fee,
            "exit_fee": preview.exit_fee,
            "slippage_buffer": preview.slippage_buffer,
            "funding_buffer": preview.funding_buffer,

            "gross_pnl": preview.gross_pnl,
            "total_cost": preview.total_cost,
            "net_pnl": preview.net_pnl,
            "net_pnl_pct": preview.net_pnl_pct,

            "fee_rate": preview.fee_rate,
            "fee_source": preview.fee_source,
        }