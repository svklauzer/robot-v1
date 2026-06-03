from __future__ import annotations

from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from sqlalchemy.orm import Session

from core.config import settings
from models.order import Order
from models.position import Position
from services.htx_client import HTXClient


class ExchangeReconciliationService:
    """Dry-run exchange reconnect and DB-vs-exchange reconciliation gate.

    This service deliberately does not mutate orders/positions.  It is a
    readiness/smoke layer: prove that the exchange can be contacted and that
    locally tracked live orders/positions are still visible remotely before live
    trading is considered safe.
    """

    OPEN_ORDER_STATUSES = {"new", "open", "submitted", "partially_filled"}

    def __init__(self, client: Any | None = None):
        self.client = client or HTXClient()

    def check(self, db: Session, symbol: str | None = None, force: bool = False) -> dict[str, Any]:
        enabled = bool(getattr(settings, "EXCHANGE_RECONCILIATION_ENABLED", False))
        live_enabled = bool(settings.is_live_enabled)
        checked_at = datetime.now(timezone.utc).isoformat()

        if not enabled and not force:
            blockers = ["exchange reconciliation is disabled"] if live_enabled else []
            return {
                "status": "disabled",
                "enabled": False,
                "ok": not blockers,
                "checked_at": checked_at,
                "blockers": blockers,
                "mismatches": [],
                "live_enabled": live_enabled,
            }

        started = perf_counter()
        try:
            self.client.load_markets()
            balance = self.client.fetch_balance()
            remote_orders = self.client.fetch_open_orders(symbol)
            remote_positions = self.client.fetch_positions()

            local_orders = self._local_live_orders(db, symbol=symbol)
            local_positions = self._local_live_positions(db, local_orders=local_orders, symbol=symbol)
            mismatches = self._find_mismatches(local_orders, local_positions, remote_orders, remote_positions)
            blockers = ["exchange reconciliation has mismatches"] if mismatches and live_enabled else []

            return {
                "status": "mismatch" if mismatches else "ok",
                "enabled": True,
                "ok": not blockers and not mismatches,
                "checked_at": checked_at,
                "latency_ms": round((perf_counter() - started) * 1000, 2),
                "blockers": blockers,
                "mismatches": mismatches,
                "counts": {
                    "local_open_orders": len(local_orders),
                    "exchange_open_orders": len(remote_orders or []),
                    "local_live_positions": len(local_positions),
                    "exchange_positions": len(self._active_remote_positions(remote_positions)),
                },
                "reconnect": {
                    "markets_ok": True,
                    "balance_ok": isinstance(balance, dict),
                    "open_orders_ok": isinstance(remote_orders, list),
                    "positions_ok": isinstance(remote_positions, list),
                },
                "live_enabled": live_enabled,
            }
        except Exception as exc:
            return {
                "status": "degraded",
                "enabled": True,
                "ok": False,
                "checked_at": checked_at,
                "blockers": ["exchange reconnect/reconciliation failed"] if live_enabled else [],
                "mismatches": [],
                "error": f"{type(exc).__name__}: {exc}",
                "live_enabled": live_enabled,
            }

    def _local_live_orders(self, db: Session, symbol: str | None = None) -> list[Order]:
        query = db.query(Order).filter(Order.exchange_order_id.isnot(None))
        query = query.filter(Order.status.in_(self.OPEN_ORDER_STATUSES))
        if symbol:
            query = query.filter(Order.symbol == symbol)
        return query.all()

    def _local_live_positions(self, db: Session, local_orders: list[Order], symbol: str | None = None) -> list[Position]:
        signal_ids = {order.signal_id for order in local_orders if order.signal_id is not None}
        if not signal_ids:
            return []
        query = db.query(Position).filter(Position.status == "open").filter(Position.signal_id.in_(signal_ids))
        if symbol:
            query = query.filter(Position.symbol == symbol)
        return query.all()

    def _find_mismatches(
        self,
        local_orders: list[Order],
        local_positions: list[Position],
        remote_orders: list[dict] | None,
        remote_positions: list[dict] | None,
    ) -> list[dict[str, Any]]:
        mismatches: list[dict[str, Any]] = []
        remote_order_keys = self._remote_order_keys(remote_orders or [])
        remote_position_keys = self._remote_position_keys(remote_positions or [])

        for order in local_orders:
            keys = {str(order.exchange_order_id)}
            if order.client_order_id:
                keys.add(str(order.client_order_id))
            if not keys.intersection(remote_order_keys):
                mismatches.append(
                    {
                        "type": "missing_exchange_order",
                        "order_id": order.id,
                        "exchange_order_id": order.exchange_order_id,
                        "client_order_id": order.client_order_id,
                        "symbol": order.symbol,
                        "status": order.status,
                    }
                )

        for position in local_positions:
            key = (position.symbol, self._normalize_side(position.side))
            if key not in remote_position_keys:
                mismatches.append(
                    {
                        "type": "missing_exchange_position",
                        "position_id": position.id,
                        "signal_id": position.signal_id,
                        "symbol": position.symbol,
                        "side": position.side,
                    }
                )

        return mismatches

    def _remote_order_keys(self, remote_orders: list[dict]) -> set[str]:
        keys: set[str] = set()
        for order in remote_orders:
            info = order.get("info") or {}
            for value in [
                order.get("id"),
                order.get("clientOrderId"),
                order.get("clientOrderID"),
                info.get("clientOrderId"),
                info.get("client_order_id"),
            ]:
                if value:
                    keys.add(str(value))
        return keys

    def _remote_position_keys(self, remote_positions: list[dict]) -> set[tuple[str, str]]:
        keys: set[tuple[str, str]] = set()
        for position in self._active_remote_positions(remote_positions):
            symbol = position.get("symbol")
            side = self._normalize_side(position.get("side") or (position.get("info") or {}).get("positionSide"))
            if symbol and side:
                keys.add((str(symbol), side))
        return keys

    def _active_remote_positions(self, remote_positions: list[dict] | None) -> list[dict]:
        active = []
        for position in remote_positions or []:
            info = position.get("info") or {}
            raw_size = next(
                (
                    value
                    for value in [
                        position.get("contracts"),
                        position.get("contractSize"),
                        position.get("amount"),
                        position.get("size"),
                        info.get("volume"),
                    ]
                    if value is not None
                ),
                None,
            )
            contracts = self._to_float(raw_size)
            if contracts is None or abs(contracts) > 0:
                active.append(position)
        return active

    def _normalize_side(self, side: Any) -> str:
        text = str(side or "").lower()
        if text in {"buy", "long"}:
            return "long"
        if text in {"sell", "short"}:
            return "short"
        return text

    def _to_float(self, value: Any) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except Exception:
            return None
