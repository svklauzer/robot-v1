"""Сверка робота с биржей (#exchange-reconciliation-2026-09-16).

Зачем. Учёт робота (Signal/Position) и биржа расходятся не только из-за ошибок
кода: позицию может закрыть стоп на бирже, пока робот перезапускался,
ликвидация, владелец руками. Пока расхождение не видно, робот ведёт сделку,
которой нет, или не видит свой висящий ордер.

Что проверяется — только объекты РОБОТА (#manual-orders-2026-09-16). Владелец
торгует на тех же биржах руками; его ордера и позиции роботу чужие:

  • позиции: объём в учёте против биржи по символу, стороне и режиму маржи.
    На бирже меньше учёта — расхождение (позиция робота закрыта или урезана
    вне робота). Больше — предупреждение: сверху, скорее всего, ручная позиция
    в том же режиме маржи;
  • ордера робота (по префиксу номера клиента): рыночный ордер не живёт
    дольше подтверждения филла — открытый ордер робота есть зависший;
  • стопы робота: стоп по символу без открытой сделки робота — расхождение
    (следующую сделку он закрыл бы по старой цене); стоп сделки не на бирже
    или лишний — предупреждение, их чинит сверка стопа на следующем проходе;
  • позиция в режиме маржи робота на символе робота без сделки робота —
    предупреждение: ручная сделка в том же режиме маржи смешается с роботом
    на бирже, ручные удобнее вести в другом режиме.

Сверка ТОЛЬКО ЧИТАЕТ: ничего не закрывает, не снимает и не переставляет.
Работает в live фоновым циклом раз в EXCHANGE_RECONCILIATION_INTERVAL_SEC;
результат кешируется, и `/system/health` читает кеш, не обращаясь к бирже на
каждый запрос дашборда.
"""
from __future__ import annotations

from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from core.config import settings
from services.market_routing import _swap_symbol, from_payload as route_from_payload

LIVE_STATUSES = ("opened", "tp1", "breakeven")

_LAST: dict | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _enabled() -> bool:
    return bool(getattr(settings, "EXCHANGE_RECONCILIATION_ENABLED", True))


def _has_size(position) -> bool:
    try:
        return isinstance(position, dict) and abs(float(position.get("contracts") or 0.0)) > 0
    except (TypeError, ValueError):
        return False


def _close(a: float, b: float) -> bool:
    return abs(float(a) - float(b)) <= max(1e-9, abs(float(b)) * 0.001)


class ExchangeReconciliationService:
    def __init__(self, client: Any | None = None, executor: Any | None = None):
        self._client = client
        self._executor = executor

    @property
    def executor(self):
        if self._executor is None:
            from services.live_executor import LIVE_EXECUTOR

            self._executor = LIVE_EXECUTOR
        return self._executor

    @property
    def client(self):
        return self._client if self._client is not None else self.executor.client

    # ── для дашборда: только кеш ────────────────────────────────────────────────
    def check(self, db=None, symbol: str | None = None, force: bool = False) -> dict[str, Any]:
        live_enabled = bool(settings.is_live_enabled)
        if not _enabled() and not force:
            blockers = ["exchange reconciliation is disabled"] if live_enabled else []
            return {"status": "disabled", "enabled": False, "ok": not blockers,
                    "checked_at": _now_iso(), "blockers": blockers, "mismatches": [],
                    "warnings": [], "live_enabled": live_enabled}
        if force and db is not None:
            return self.reconcile(db)
        if _LAST is None:
            return {"status": "pending", "enabled": True, "ok": True, "checked_at": None,
                    "blockers": [], "mismatches": [], "warnings": [], "live_enabled": live_enabled,
                    "note": "сверка идёт фоновым циклом только в live"}
        return _LAST

    # ── сверка ─────────────────────────────────────────────────────────────────
    def reconcile(self, db, bot_id: int | None = None) -> dict[str, Any]:
        global _LAST
        live_enabled = bool(settings.is_live_enabled)
        base = {"enabled": True, "checked_at": _now_iso(), "live_enabled": live_enabled,
                "exchange": settings.active_exchange}
        if self.executor.effective_mode() != "live":
            result = {**base, "status": "not_live", "ok": True, "blockers": [], "mismatches": [],
                      "warnings": [], "note": "в paper и dry_run на бирже нет позиций робота"}
            _LAST = result
            return result

        started = perf_counter()
        try:
            result = self._reconcile(db, bot_id)
        except Exception as exc:  # noqa: BLE001 — сверка не имеет права ронять цикл
            result = {"status": "degraded", "ok": False, "mismatches": [], "warnings": [],
                      "error": f"{type(exc).__name__}: {exc}",
                      "blockers": ["exchange reconnect/reconciliation failed"] if live_enabled else []}
        result = {**base, **result, "latency_ms": round((perf_counter() - started) * 1000, 2)}
        _LAST = result
        return result

    def _robot_trades(self, db, bot_id: int | None) -> list[tuple]:
        from models.position import Position
        from models.signal import Signal

        query = db.query(Signal).filter(Signal.status.in_(LIVE_STATUSES))
        if bot_id is not None:
            query = query.filter(Signal.bot_id == bot_id)
        active = settings.active_exchange
        trades = []
        for signal in query.all():
            plan = signal.plan_json or {}
            if (plan.get("execution") or {}).get("mode") != "live":
                continue
            if str(getattr(signal, "exchange", None) or active).lower() != active:
                continue
            position = (db.query(Position)
                        .filter(Position.signal_id == signal.id, Position.status == "open")
                        .first())
            if position is None:
                continue
            route = route_from_payload(plan, position.symbol, position.side)
            if not self.executor._is_derivative(route.market_type):
                continue
            trades.append((signal, position, route))
        return trades

    def _reconcile(self, db, bot_id: int | None) -> dict[str, Any]:
        from services.robot_orders import is_robot_order

        executor = self.executor
        mismatches: list[dict] = []
        warnings: list[dict] = []

        trades = self._robot_trades(db, bot_id)
        groups: dict[tuple, dict] = {}
        for signal, position, route in trades:
            try:
                margin = executor.resolve_margin_mode(route.margin_mode)
            except ValueError:
                margin = route.margin_mode
            key = (route.exchange_symbol, str(position.side).lower(), margin)
            group = groups.setdefault(key, {"book_qty": 0.0, "signal_ids": [], "stop_ids": set()})
            group["book_qty"] += float(position.qty or 0.0)
            group["signal_ids"].append(signal.id)
            stop_id = ((signal.plan_json or {}).get("exchange_stop") or {}).get("order_id")
            if stop_id:
                group["stop_ids"].add(str(stop_id))

        positions = self.client.fetch_positions() or []

        for (symbol, side, margin), group in groups.items():
            on_exchange = executor.position_base_from(positions, symbol, side, margin)
            book = round(group["book_qty"], 12)
            item = {"symbol": symbol, "side": side, "margin_mode": margin, "book_qty": book,
                    "exchange_qty": on_exchange, "signal_ids": group["signal_ids"]}
            if _close(on_exchange, book):
                continue
            if on_exchange < book:
                mismatches.append({"type": "exchange_below_book", **item})
            else:
                warnings.append({"type": "exchange_above_book", **item,
                                 "note": "сверх учёта — вероятно, ручная позиция в том же режиме маржи"})

        robot_margin = None
        try:
            robot_margin = executor.resolve_margin_mode(getattr(settings, "TREND_MARGIN_MODE", None))
        except ValueError:
            pass
        universe = {_swap_symbol(s) for s in settings.symbols_for(settings.active_exchange)}
        tracked = {(sym, side, margin) for sym, side, margin in groups}
        for p in positions:
            if not _has_size(p) or p.get("symbol") not in universe:
                continue
            p_margin = str(p.get("marginMode") or "").lower()
            p_side = str(p.get("side") or "").lower()
            if not p_margin or p_margin != robot_margin or (p["symbol"], p_side, p_margin) in tracked:
                continue
            warnings.append({"type": "untracked_position_in_robot_margin_mode", "symbol": p["symbol"],
                             "side": p_side, "margin_mode": p_margin, "contracts": p.get("contracts"),
                             "note": "позиция без сделки робота в режиме маржи робота — ручную "
                                     "сделку удобнее вести в другом режиме маржи"})

        symbols = sorted(universe | {sym for sym, _side, _margin in groups})
        robot_orders_seen = 0
        robot_stops_seen = 0
        for symbol in symbols:
            open_orders = self.client.fetch_open_orders(symbol) or []
            for order in open_orders:
                if not is_robot_order(order):
                    continue
                robot_orders_seen += 1
                mismatches.append({"type": "stale_robot_order", "symbol": symbol,
                                   "order_id": order.get("id"), "side": order.get("side"),
                                   "amount": order.get("amount"), "status": order.get("status")})

            stops = executor.open_stop_orders(symbol, "swap")
            if stops is None:
                warnings.append({"type": "stop_fetch_failed", "symbol": symbol})
                continue
            robot_stops_seen += len(stops)
            symbol_groups = [g for (sym, _s, _m), g in groups.items() if sym == symbol]
            if not symbol_groups:
                for stop in stops:
                    mismatches.append({"type": "orphan_robot_stop", "symbol": symbol, **stop,
                                       "note": "стоп робота без открытой сделки — следующую сделку "
                                               "в ту же сторону закроет по старой цене"})
                continue
            recorded = set().union(*(g["stop_ids"] for g in symbol_groups))
            on_exchange_ids = {s["order_id"] for s in stops}
            for stop in stops:
                if stop["order_id"] not in recorded:
                    warnings.append({"type": "stray_robot_stop", "symbol": symbol, **stop})
            for group in symbol_groups:
                if not (group["stop_ids"] & on_exchange_ids):
                    warnings.append({"type": "exchange_stop_missing", "symbol": symbol,
                                     "signal_ids": group["signal_ids"]})

        live_enabled = bool(settings.is_live_enabled)
        return {
            "status": "mismatch" if mismatches else "ok",
            "ok": not mismatches,
            "mismatches": mismatches,
            "warnings": warnings,
            "blockers": ["exchange reconciliation has mismatches"] if mismatches and live_enabled else [],
            "counts": {
                "robot_live_positions": len(trades),
                "position_groups": len(groups),
                "exchange_positions": sum(1 for p in positions if _has_size(p)),
                "robot_open_orders": robot_orders_seen,
                "robot_stops": robot_stops_seen,
                "symbols_checked": len(symbols),
            },
        }


def mismatch_key(item: dict) -> str:
    return ":".join(str(item.get(k) or "") for k in ("type", "symbol", "side", "order_id"))
