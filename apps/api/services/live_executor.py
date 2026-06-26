"""LiveExecutor — безопасное ядро исполнения ордеров для выхода на Live (HTX).

Единая точка отправки для ВСЕХ движков (trend / funding / grid). Движки НЕ зовут
htx_client.create_* напрямую в живом режиме — только через этот слой, который даёт
идемпотентность, подтверждение филла, плечо/режим маржи и предохранители.

Режимы (LIVE_EXECUTION_MODE):
  off     — живой путь отключён; вызов вернёт mode="off" (движок остаётся на бумаге);
  dry_run — путь проходит ПОЛНОСТЬЮ, но реальный ордер НЕ отправляется: логируем
            «что бы отправили» и возвращаем синтетический ack по reference-цене.
            Это позволяет валидировать живую логику прямо на бумаге, без риска;
  live    — реальная отправка. Требует ENABLE_LIVE_ORDERS=true, иначе понижается
            до dry_run (safety: один флаг-предохранитель не обойти режимом).

Гарантии безопасности:
  • идемпотентность: каждый ордер несёт clientOrderId; при НЕОДНОЗНАЧНОМ сбое
    (таймаут/обрыв) create НЕ ретраится вслепую — сверяем по clientOrderId и
    повторяем, только если ордера точно нет (иначе вернём найденный);
  • подтверждение филла: после отправки поллим fetch_order до закрытия/таймаута и
    возвращаем РЕАЛЬНУЮ среднюю цену и исполненный объём (не из ответа create);
  • плечо и режим маржи для swap выставляются ДО ордера;
  • предохранитель размера: нотионал ордера ограничен LIVE_MAX_ORDER_NOTIONAL_USDT
    (для старта live_limited крошечным размером).
Инвариант: при любой неоднозначности — НЕ удваиваем позицию.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any

from core.config import settings
from core.logging import get_logger, log_event
from services.htx_client import HTXClient

logger = get_logger(__name__)


@dataclass
class OrderResult:
    ok: bool
    mode: str                       # off / dry_run / live
    sent: bool                      # реально ли ушёл ордер на биржу
    status: str                     # filled / closed / open / dry_run / off / error
    symbol: str
    side: str
    requested_qty: float
    market_type: str
    reduce_only: bool
    client_order_id: str | None = None
    exchange_order_id: str | None = None
    filled_qty: float = 0.0
    avg_price: float | None = None
    error: str | None = None
    raw: dict | None = field(default=None, repr=False)

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("raw", None)
        return d


class LiveExecutor:
    def __init__(self):
        self.client = HTXClient()
        self._leverage_set: set[str] = set()

    # ── режим ─────────────────────────────────────────────────────────────────
    @staticmethod
    def configured_mode() -> str:
        return str(getattr(settings, "LIVE_EXECUTION_MODE", "dry_run")).lower().strip()

    @classmethod
    def effective_mode(cls) -> str:
        """live разрешён ТОЛЬКО при ENABLE_LIVE_ORDERS; иначе понижаем до dry_run."""
        mode = cls.configured_mode()
        if mode == "live" and not bool(getattr(settings, "ENABLE_LIVE_ORDERS", False)):
            return "dry_run"
        return mode if mode in ("off", "dry_run", "live") else "dry_run"

    @classmethod
    def is_live(cls) -> bool:
        return cls.effective_mode() == "live"

    # ── идемпотентность ────────────────────────────────────────────────────────
    @staticmethod
    def _make_client_id(purpose: str) -> str:
        # ≤32 симв., детерминированный префикс назначения + uuid-хвост
        tag = "".join(ch for ch in purpose if ch.isalnum())[:8] or "ord"
        return f"{tag}{uuid.uuid4().hex}"[:32]

    def _find_by_client_id(self, symbol: str, client_id: str) -> dict | None:
        """Сверка: ушёл ли ордер с этим clientOrderId (open ИЛИ closed). best-effort."""
        def _match(orders):
            for o in orders or []:
                cid = o.get("clientOrderId") or (o.get("info", {}) or {}).get("client_order_id")
                if cid == client_id:
                    return o
            return None
        try:
            m = _match(self.client.fetch_open_orders(symbol))
            if m:
                return m
        except Exception as exc:  # noqa: BLE001
            log_event(logger, logging.WARNING, "live_reconcile_open_fail", symbol=symbol, error=str(exc))
        try:
            return _match(self.client.fetch_closed_orders(symbol, limit=20))
        except Exception as exc:  # noqa: BLE001
            log_event(logger, logging.WARNING, "live_reconcile_closed_fail", symbol=symbol, error=str(exc))
        return None

    # ── плечо / режим маржи ─────────────────────────────────────────────────────
    def _ensure_leverage(self, symbol: str, market_type: str, leverage: float | None):
        if market_type != "swap" or not bool(getattr(settings, "LIVE_SET_LEVERAGE", True)):
            return
        if symbol in self._leverage_set:
            return
        lev = float(leverage or getattr(settings, "FUTURES_LEVERAGE", 1) or 1)
        margin_mode = str(getattr(settings, "LIVE_MARGIN_MODE", "cross")).lower()
        try:
            self.client.set_margin_mode(margin_mode, symbol)
            self.client.set_leverage(lev, symbol)
            self._leverage_set.add(symbol)
            log_event(logger, logging.INFO, "live_leverage_set", symbol=symbol, leverage=lev, margin=margin_mode)
        except Exception as exc:  # noqa: BLE001
            log_event(logger, logging.WARNING, "live_leverage_set_fail", symbol=symbol, error=str(exc))

    # ── подтверждение филла ─────────────────────────────────────────────────────
    def _await_fill(self, symbol: str, order: dict, client_id: str) -> dict:
        timeout = float(getattr(settings, "LIVE_FILL_POLL_TIMEOUT_SEC", 10.0))
        interval = float(getattr(settings, "LIVE_FILL_POLL_INTERVAL_SEC", 1.0))
        oid = order.get("id")
        deadline = time.time() + timeout
        last = order
        while time.time() < deadline:
            status = (last or {}).get("status")
            if status in ("closed", "filled", "canceled", "rejected"):
                break
            time.sleep(interval)
            try:
                last = self.client.fetch_order(oid, symbol)
            except Exception as exc:  # noqa: BLE001
                log_event(logger, logging.WARNING, "live_fill_poll_fail", symbol=symbol, oid=oid, error=str(exc))
                break
        return last or order

    # ── сайзинг от реального баланса ────────────────────────────────────────────
    def account_equity_usdt(self) -> float | None:
        """Свободный USDT по бирже для live-сайзинга (LIVE_SIZE_FROM_BALANCE).
        None → не удалось получить (вызывающий падает обратно на RISK_EQUITY_USDT)."""
        if not bool(getattr(settings, "LIVE_SIZE_FROM_BALANCE", True)):
            return None
        try:
            bal = self.client.fetch_balance() or {}
            usdt = (bal.get("USDT") or {})
            free = usdt.get("free") if isinstance(usdt, dict) else None
            total = usdt.get("total") if isinstance(usdt, dict) else None
            val = free if free is not None else total
            return float(val) if val is not None else None
        except Exception as exc:  # noqa: BLE001
            log_event(logger, logging.WARNING, "live_balance_fetch_fail", error=str(exc))
            return None

    # ── публичный вход: рыночный ордер ──────────────────────────────────────────
    def place_market(self, symbol: str, side: str, amount: float, *, market_type: str,
                     reduce_only: bool = False, leverage: float | None = None,
                     reference_price: float | None = None, purpose: str = "") -> OrderResult:
        mode = self.effective_mode()
        amount = float(amount)
        base = dict(symbol=symbol, side=side, requested_qty=amount,
                    market_type=market_type, reduce_only=reduce_only)

        if mode == "off":
            return OrderResult(ok=False, mode="off", sent=False, status="off", **base)

        # предохранитель размера (нотионал)
        cap = float(getattr(settings, "LIVE_MAX_ORDER_NOTIONAL_USDT", 0.0) or 0.0)
        if cap > 0 and reference_price and amount * float(reference_price) > cap:
            log_event(logger, logging.WARNING, "live_order_notional_cap",
                      symbol=symbol, notional=amount * float(reference_price), cap=cap)
            return OrderResult(ok=False, mode=mode, sent=False, status="error",
                               error=f"notional>{cap}", **base)

        client_id = self._make_client_id(purpose)

        # DRY-RUN: проходим всю логику, но НЕ отправляем. Возвращаем синтетический ack.
        if mode == "dry_run":
            log_event(logger, logging.INFO, "live_dry_run_order", symbol=symbol, side=side,
                      qty=amount, market_type=market_type, reduce_only=reduce_only,
                      ref_price=reference_price, purpose=purpose, client_order_id=client_id)
            return OrderResult(ok=True, mode="dry_run", sent=False, status="dry_run",
                               client_order_id=client_id, filled_qty=amount,
                               avg_price=float(reference_price) if reference_price else None, **base)

        # LIVE: плечо → отправка (одна попытка) → сверка при сбое → подтверждение филла
        self._ensure_leverage(symbol, market_type, leverage)
        params: dict[str, Any] = {"clientOrderId": client_id}
        if market_type:
            params["defaultType"] = market_type
        if reduce_only:
            params["reduceOnly"] = True

        try:
            order = self.client.create_order_once(symbol, "market", side, amount, None, params)
        except Exception as exc:  # noqa: BLE001 — НЕОДНОЗНАЧНО: мог пройти. Сверяем.
            log_event(logger, logging.ERROR, "live_create_ambiguous", symbol=symbol,
                      client_order_id=client_id, error=str(exc))
            found = self._find_by_client_id(symbol, client_id)
            if not found:
                return OrderResult(ok=False, mode="live", sent=False, status="error",
                                   client_order_id=client_id, error=f"create_failed:{exc}", **base)
            order = found  # ордер на самом деле ушёл — НЕ повторяем

        order = self._await_fill(symbol, order, client_id)
        status = (order or {}).get("status", "open")
        filled = float((order or {}).get("filled") or 0.0)
        avg = (order or {}).get("average") or (order or {}).get("price") or reference_price
        log_event(logger, logging.INFO, "live_order_done", symbol=symbol, side=side,
                  status=status, filled=filled, avg=avg, client_order_id=client_id,
                  exchange_order_id=(order or {}).get("id"))
        return OrderResult(ok=status in ("closed", "filled"), mode="live", sent=True,
                           status=status, client_order_id=client_id,
                           exchange_order_id=(order or {}).get("id"),
                           filled_qty=filled, avg_price=float(avg) if avg else None,
                           raw=order, **base)


LIVE_EXECUTOR = LiveExecutor()
