import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from core.config import settings
from services.htx_client import HTXClient
from services.trade_plan import TradePlanBuilder
from services.cost_engine import CostEngine
from services.telegram_router import TelegramRouter

from models.order import Order
from models.position import Position
from models.signal import Signal
from models.bot import Bot


class ExecutionEngine:
    def __init__(self, db=None):
        self.db = db
        self.client = HTXClient()
        self.telegram = TelegramRouter()
        self.cost_engine = CostEngine()
        self.plan_builder = TradePlanBuilder()

    def _plan_from_signal(self, signal: Signal):
        plan_json = signal.plan_json or {}

        qty = signal.qty if signal.qty is not None else plan_json.get("qty")

        if qty is None:
            return None

        return SimpleNamespace(
            symbol=signal.symbol,
            side=signal.side,
            qty=float(qty),
            entry_price=None,
            stop_price=float(signal.stop_price),
            tp1=float(signal.tp_json["tp1"]),
            tp2=float(signal.tp_json["tp2"]),
            leverage=settings.execution_leverage,

            balance_usdt=plan_json.get("balance_usdt"),
            risk_usdt=plan_json.get("risk_usdt"),
            entry_notional=plan_json.get("entry_notional"),
            required_margin=signal.required_margin if signal.required_margin is not None else plan_json.get("required_margin"),

            net_pnl_tp1=signal.net_pnl_tp1 if signal.net_pnl_tp1 is not None else plan_json.get("net_pnl_tp1"),
            net_pnl_tp2=signal.net_pnl_tp2 if signal.net_pnl_tp2 is not None else plan_json.get("net_pnl_tp2"),
            net_pnl_stop=signal.net_pnl_stop if signal.net_pnl_stop is not None else plan_json.get("net_pnl_stop"),

            net_rr_tp1=signal.net_rr_tp1 if signal.net_rr_tp1 is not None else plan_json.get("net_rr_tp1"),
            net_rr_tp2=signal.net_rr_tp2 if signal.net_rr_tp2 is not None else plan_json.get("net_rr_tp2"),

            is_valid=bool(plan_json.get("is_valid", True)),
            reject_reason=plan_json.get("reject_reason"),
        )

    def execute_signal(self, signal: dict, qty: float, mode: str = "paper") -> dict:
        """
        Старый метод оставляем для совместимости с robot_loop.
        Позже заменим его на execute_trade_plan.
        """
        client_order_id = str(uuid.uuid4())

        if mode == "paper":
            side = "buy" if signal["action"] == "long" else "sell"
            entry_mid = sum(signal["entry_zone"]) / 2
            return {
                "mode": "paper",
                "client_order_id": client_order_id,
                "exchange_order_id": None,
                "symbol": signal["symbol"],
                "side": side,
                "status": "filled",
                "qty": qty,
                "avg_fill_price": entry_mid,
            }

        if not settings.ENABLE_LIVE_ORDERS:
            raise RuntimeError("Live orders are disabled by ENABLE_LIVE_ORDERS=false")

        side = "buy" if signal["action"] == "long" else "sell"

        result = self.client.create_market_order(
            signal["symbol"],
            side,
            qty,
            params={"clientOrderId": client_order_id}
        )

        return {
            "mode": "live",
            "client_order_id": client_order_id,
            "exchange_order_id": result.get("id"),
            "symbol": signal["symbol"],
            "side": side,
            "status": result.get("status", "submitted"),
            "qty": result.get("amount", qty),
            "avg_fill_price": result.get("average"),
            "raw": result,
        }

    async def open_paper_position(
        self,
        bot: Bot,
        signal: Signal,
        entry_price: float,
        balance_usdt: float = 1000.0,
    ) -> dict:
        """
        Открывает paper-position по сигналу.
        Создаёт:
        - Order
        - Position
        - TradePlan
        """

        if self.db is None:
            raise RuntimeError("ExecutionEngine requires db session for paper execution")

        existing_position = (
            self.db.query(Position)
            .filter(
                Position.signal_id == signal.id,
                Position.status == "open"
            )
            .first()
        )

        if existing_position:
            return {
                "status": "already_open",
                "position": existing_position,
                "order": None,
                "plan": None,
            }

        tp1 = float(signal.tp_json["tp1"])
        tp2 = float(signal.tp_json["tp2"])
        stop = float(signal.stop_price)

        plan = self._plan_from_signal(signal)

        if plan is None:
            plan = self.plan_builder.build_plan(
                symbol=signal.symbol,
                side=signal.side,
                entry_price=entry_price,
                stop_price=stop,
                tp1=tp1,
                tp2=tp2,
                balance_usdt=balance_usdt,
                leverage=settings.execution_leverage,
                scalp=str((signal.plan_json or {}).get("trade_mode", "")) == "scalp",
            )

        if not plan.is_valid:
            await self.telegram.owner_alert(
                "TRADE PLAN REJECTED",
                (
                    f"Signal #{signal.id}\n"
                    f"{signal.symbol} {signal.side}\n"
                    f"Reason: {plan.reject_reason}\n"
                    f"Entry: {entry_price}\n"
                    f"Stop: {stop}\n"
                    f"TP1: {tp1}\n"
                    f"TP2: {tp2}\n"
                    f"Net TP1: {plan.net_pnl_tp1} USDT\n"
                    f"Net TP2: {plan.net_pnl_tp2} USDT\n"
                    f"Net Stop: {plan.net_pnl_stop} USDT\n"
                    f"RR TP2: {plan.net_rr_tp2}"
                )
            )

            return {
                "status": "rejected",
                "reason": plan.reject_reason,
                "order": None,
                "position": None,
                "plan": plan,
            }

        open_side = self._open_order_side(signal.side)
        client_order_id = f"PAPER-{uuid.uuid4()}"

        order_qty = self.client.amount_to_precision(signal.symbol, float(plan.qty))

        order = Order(
            bot_id=bot.id,
            signal_id=signal.id,
            symbol=signal.symbol,
            side=open_side,
            order_type="market",
            status="filled",
            qty=order_qty,
            price=entry_price,
            filled_qty=order_qty,
            avg_fill_price=entry_price,
            client_order_id=client_order_id,
            exchange_order_id=None,
        )

        position = Position(
            bot_id=bot.id,
            signal_id=signal.id,
            symbol=signal.symbol,
            side=signal.side,
            qty=plan.qty,
            entry_price=entry_price,
            mark_price=entry_price,
            unrealized_pnl=0.0,
            status="open",
        )

        self.db.add(order)
        self.db.add(position)
        self.db.flush()

        return {
            "status": "opened",
            "order": order,
            "position": position,
            "plan": plan,
        }

    async def close_paper_position(
        self,
        signal: Signal,
        exit_price: float,
        reason: str,
    ) -> dict | None:
        """
        Закрывает paper-position по сигналу и создаёт closing order.
        Net PnL считает через CostEngine.
        """

        if self.db is None:
            raise RuntimeError("ExecutionEngine requires db session for paper execution")

        position = (
            self.db.query(Position)
            .filter(
                Position.signal_id == signal.id,
                Position.status == "open"
            )
            .first()
        )

        if not position:
            return None

        preview = self.cost_engine.estimate(
            symbol=position.symbol,
            market_type=settings.execution_market_type,
            side=position.side,
            entry_price=float(position.entry_price),
            exit_price=float(exit_price),
            qty=float(position.qty),
            liquidity="taker",
            holding_funding_periods=1,
            leverage=settings.execution_leverage,
        )

        close_side = self._close_order_side(position.side)
        client_order_id = f"PAPER-CLOSE-{uuid.uuid4()}"

        close_order = Order(
            bot_id=position.bot_id,
            signal_id=signal.id,
            symbol=position.symbol,
            side=close_side,
            order_type="market",
            status="filled",
            qty=position.qty,
            price=exit_price,
            filled_qty=position.qty,
            avg_fill_price=exit_price,
            client_order_id=client_order_id,
            exchange_order_id=None,
        )

        position.status = "closed"
        position.mark_price = exit_price
        position.unrealized_pnl = preview.net_pnl
        position.closed_at = datetime.now(timezone.utc)

        self.db.add(close_order)
        self.db.flush()

        return {
            "status": "closed",
            "position": position,
            "close_order": close_order,
            "net_pnl": preview.net_pnl,
            "net_pnl_pct": preview.net_pnl_pct,
            "total_cost": preview.total_cost,
            "reason": reason,
        }

    def _open_order_side(self, signal_side: str) -> str:
        return "buy" if signal_side == "long" else "sell"

    def _close_order_side(self, signal_side: str) -> str:
        return "sell" if signal_side == "long" else "buy"
