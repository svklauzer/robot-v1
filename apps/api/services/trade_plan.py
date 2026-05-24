from dataclasses import dataclass
from core.config import settings
from services.cost_engine import CostEngine
from services.htx_client import HTXClient


@dataclass
class TradePlan:
    symbol: str
    side: str
    qty: float
    entry_price: float
    stop_price: float
    tp1: float
    tp2: float
    leverage: int

    balance_usdt: float
    risk_usdt: float
    entry_notional: float
    required_margin: float

    net_pnl_tp1: float
    net_pnl_tp2: float
    net_pnl_stop: float

    net_rr_tp1: float
    net_rr_tp2: float

    is_valid: bool
    reject_reason: str | None


class TradePlanBuilder:
    def __init__(self):
        self.cost_engine = CostEngine()
        self.htx = HTXClient()

    def build_plan(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        stop_price: float,
        tp1: float,
        tp2: float,
        balance_usdt: float,
        risk_pct: float | None = None,
        leverage: int | None = None,
    ) -> TradePlan:
        risk_pct_value = risk_pct if risk_pct is not None else settings.RISK_PER_TRADE_PCT

        market_type = getattr(settings, "EXECUTION_MARKET", settings.MARKET_TYPE)

        # Важно: если фьючерсы выключены, плечо принудительно = 1.
        # Иначе spot-план может случайно рассчитать позицию как leveraged.
        if settings.ENABLE_FUTURES or market_type == "futures":
            leverage_value = leverage or settings.FUTURES_LEVERAGE
        else:
            leverage_value = 1

        balance_usdt = float(balance_usdt)
        risk_usdt = balance_usdt * (float(risk_pct_value) / 100)

        # Приводим цены к точности биржи ДО расчёта риска и qty.
        entry_price = float(self.htx.price_to_precision(symbol, entry_price))
        stop_price = float(self.htx.price_to_precision(symbol, stop_price))
        tp1 = float(self.htx.price_to_precision(symbol, tp1))
        tp2 = float(self.htx.price_to_precision(symbol, tp2))

        entry_price = float(entry_price)
        stop_price = float(stop_price)
        tp1 = float(tp1)
        tp2 = float(tp2)

        risk_per_unit = abs(entry_price - stop_price)

        if risk_per_unit <= 0:
            return self._invalid_plan(
                symbol=symbol,
                side=side,
                entry_price=entry_price,
                stop_price=stop_price,
                tp1=tp1,
                tp2=tp2,
                leverage=leverage_value,
                balance_usdt=balance_usdt,
                risk_usdt=risk_usdt,
                reason="invalid_stop_distance",
            )

        # Qty по риску.
        qty_by_risk = risk_usdt / risk_per_unit

        # Qty по доступной марже/балансу.
        max_notional = balance_usdt * leverage_value
        qty_by_balance = max_notional / entry_price

        # Дополнительный предохранитель: не даём одной сделке занимать
        # слишком большую долю капитала/маржи.
        max_position_margin_pct = float(getattr(settings, "MAX_POSITION_MARGIN_PCT", 0.35))
        max_position_margin_usdt = balance_usdt * max(0.01, min(max_position_margin_pct, 1.0))
        max_position_notional = max_position_margin_usdt * leverage_value
        qty_by_position_cap = max_position_notional / entry_price

        # Берём меньшее, чтобы не открыть позицию больше допустимого размера.
        qty = min(qty_by_risk, qty_by_balance, qty_by_position_cap)

        # Приводим qty к точности биржи.
        qty = float(self.htx.amount_to_precision(symbol, qty))
        qty = float(qty)

        if qty <= 0:
            return self._invalid_plan(
                symbol=symbol,
                side=side,
                entry_price=entry_price,
                stop_price=stop_price,
                tp1=tp1,
                tp2=tp2,
                leverage=leverage_value,
                balance_usdt=balance_usdt,
                risk_usdt=risk_usdt,
                reason="qty_is_zero_after_precision",
            )

        entry_notional = entry_price * qty
        required_margin = entry_notional / leverage_value if leverage_value > 0 else entry_notional

        limits = self.htx.market_limits(symbol)
        min_amount = limits.get("min_amount")
        min_cost = limits.get("min_cost")

        if min_amount is not None and qty < float(min_amount):
            return self._invalid_plan(
                symbol=symbol,
                side=side,
                entry_price=entry_price,
                stop_price=stop_price,
                tp1=tp1,
                tp2=tp2,
                leverage=leverage_value,
                balance_usdt=balance_usdt,
                risk_usdt=risk_usdt,
                reason="qty_below_exchange_min_amount",
            )

        if min_cost is not None and entry_notional < float(min_cost):
            return self._invalid_plan(
                symbol=symbol,
                side=side,
                entry_price=entry_price,
                stop_price=stop_price,
                tp1=tp1,
                tp2=tp2,
                leverage=leverage_value,
                balance_usdt=balance_usdt,
                risk_usdt=risk_usdt,
                reason="entry_notional_below_exchange_min_cost",
            )

        side_value = str(side or "").lower().strip()

        if side_value in ["long", "buy"]:
            if not (stop_price < entry_price < tp1 < tp2):
                return self._invalid_plan(
                    symbol=symbol,
                    side=side,
                    entry_price=entry_price,
                    stop_price=stop_price,
                    tp1=tp1,
                    tp2=tp2,
                    leverage=leverage_value,
                    balance_usdt=balance_usdt,
                    risk_usdt=risk_usdt,
                    reason="invalid_long_directional_levels",
                )

        elif side_value in ["short", "sell"]:
            if not (tp2 < tp1 < entry_price < stop_price):
                return self._invalid_plan(
                    symbol=symbol,
                    side=side,
                    entry_price=entry_price,
                    stop_price=stop_price,
                    tp1=tp1,
                    tp2=tp2,
                    leverage=leverage_value,
                    balance_usdt=balance_usdt,
                    risk_usdt=risk_usdt,
                    reason="invalid_short_directional_levels",
                )

        else:
            return self._invalid_plan(
                symbol=symbol,
                side=side,
                entry_price=entry_price,
                stop_price=stop_price,
                tp1=tp1,
                tp2=tp2,
                leverage=leverage_value,
                balance_usdt=balance_usdt,
                risk_usdt=risk_usdt,
                reason="unsupported_side",
            )

        tp1_preview = self.cost_engine.estimate(
            symbol=symbol,
            market_type=market_type,
            side=side,
            entry_price=entry_price,
            exit_price=tp1,
            qty=qty,
            liquidity="taker",
            leverage=leverage_value,
        )

        tp2_preview = self.cost_engine.estimate(
            symbol=symbol,
            market_type=market_type,
            side=side,
            entry_price=entry_price,
            exit_price=tp2,
            qty=qty,
            liquidity="taker",
            leverage=leverage_value,
        )

        stop_preview = self.cost_engine.estimate(
            symbol=symbol,
            market_type=market_type,
            side=side,
            entry_price=entry_price,
            exit_price=stop_price,
            qty=qty,
            liquidity="taker",
            leverage=leverage_value,
        )

        net_risk = abs(stop_preview.net_pnl)

        net_rr_tp1 = tp1_preview.net_pnl / net_risk if net_risk > 0 else 0
        net_rr_tp2 = tp2_preview.net_pnl / net_risk if net_risk > 0 else 0

        is_valid = True
        reject_reason = None

        if stop_preview.net_pnl >= 0:
            is_valid = False
            reject_reason = "stop_net_pnl_must_be_negative"

        elif required_margin > balance_usdt and not settings.ENABLE_FUTURES:
            is_valid = False
            reject_reason = "required_margin_exceeds_balance"

        elif tp1_preview.net_pnl <= 0:
            is_valid = False
            reject_reason = "tp1_net_pnl_not_positive"

        elif tp2_preview.net_pnl <= 0:
            is_valid = False
            reject_reason = "tp2_net_pnl_not_positive"

        elif tp1_preview.net_pnl < float(getattr(settings, "MIN_NET_PNL_TP1_USDT", 2.5)):
            is_valid = False
            reject_reason = "tp1_net_pnl_below_min_usdt"

        elif tp2_preview.net_pnl < float(getattr(settings, "MIN_NET_PNL_TP2_USDT", 6.0)):
            is_valid = False
            reject_reason = "tp2_net_pnl_below_min_usdt"

        elif net_rr_tp2 < 1.2:
            is_valid = False
            reject_reason = "net_rr_too_low"

        return TradePlan(
            symbol=symbol,
            side=side,
            qty=round(qty, 6),
            entry_price=entry_price,
            stop_price=stop_price,
            tp1=tp1,
            tp2=tp2,
            leverage=leverage_value,

            balance_usdt=round(balance_usdt, 2),
            risk_usdt=round(risk_usdt, 2),
            entry_notional=round(entry_notional, 6),
            required_margin=round(required_margin, 6),

            net_pnl_tp1=tp1_preview.net_pnl,
            net_pnl_tp2=tp2_preview.net_pnl,
            net_pnl_stop=stop_preview.net_pnl,

            net_rr_tp1=round(net_rr_tp1, 4),
            net_rr_tp2=round(net_rr_tp2, 4),

            is_valid=is_valid,
            reject_reason=reject_reason,
        )

    def _invalid_plan(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        stop_price: float,
        tp1: float,
        tp2: float,
        leverage: int,
        balance_usdt: float,
        risk_usdt: float,
        reason: str,
    ) -> TradePlan:
        return TradePlan(
            symbol=symbol,
            side=side,
            qty=0.0,
            entry_price=entry_price,
            stop_price=stop_price,
            tp1=tp1,
            tp2=tp2,
            leverage=leverage,
            balance_usdt=round(balance_usdt, 2),
            risk_usdt=round(risk_usdt, 2),
            entry_notional=0.0,
            required_margin=0.0,
            net_pnl_tp1=0.0,
            net_pnl_tp2=0.0,
            net_pnl_stop=0.0,
            net_rr_tp1=0.0,
            net_rr_tp2=0.0,
            is_valid=False,
            reject_reason=reason,
        )
