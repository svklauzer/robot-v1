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
import math
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
        self._bal_cache: dict[str, tuple[float, float]] = {}  # market_type -> (free_usdt, ts)

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
    def _ensure_leverage(self, symbol: str, market_type: str, leverage: float | None,
                         margin_mode: str | None = None):
        if market_type != "swap" or not bool(getattr(settings, "LIVE_SET_LEVERAGE", True)):
            return
        if symbol in self._leverage_set:
            return
        lev = float(leverage or getattr(settings, "FUTURES_LEVERAGE", 1) or 1)
        lev = max(1.0, min(lev, float(getattr(settings, "LIVE_MAX_LEVERAGE", 5.0))))  # потолок-предохранитель
        margin_mode = str(margin_mode or getattr(settings, "LIVE_MARGIN_MODE", "cross")).lower()
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

    # ── свободный баланс по счёту (SPOT и USDT-M — РАЗНЫЕ счета HTX) ─────────────
    @staticmethod
    def _account_type(market_type: str | None) -> str:
        mt = str(market_type or "").lower()
        return "swap" if mt in ("swap", "future", "futures", "linear", "usdt-m") else "spot"

    def free_usdt(self, market_type: str | None = None) -> float | None:
        """Свободный USDT на СООТВЕТСТВУЮЩЕМ счёте (spot ИЛИ swap). С TTL-кэшем,
        чтобы не дёргать API на каждый сайзинг. None → не удалось получить."""
        acct = self._account_type(market_type)
        ttl = float(getattr(settings, "LIVE_BALANCE_CACHE_SEC", 30.0))
        cached = self._bal_cache.get(acct)
        if cached and (time.time() - cached[1]) < ttl:
            return cached[0]
        try:
            bal = self.client.fetch_balance(params={"type": acct}) or {}
            usdt = bal.get("USDT") or {}
            free = usdt.get("free") if isinstance(usdt, dict) else None
            if free is None:
                free = usdt.get("total") if isinstance(usdt, dict) else None
            if free is None:
                return None
            free = float(free)
            self._bal_cache[acct] = (free, time.time())
            return free
        except Exception as exc:  # noqa: BLE001
            log_event(logger, logging.WARNING, "live_balance_fetch_fail", account=acct, error=str(exc))
            return None

    def account_equity_usdt(self) -> float | None:
        """Свободный USDT счёта исполнения (для /live/state)."""
        return self.free_usdt(getattr(settings, "execution_market_type", "spot"))

    def effective_equity_usdt(self, market_type: str | None = None) -> float:
        """Эквити для сайзинга и экспозиции.

        paper/dry_run/off → RISK_EQUITY_USDT: бумажный капитал не меняется.

        live → реальные свободные USDT. Счёт зависит от рынка: лонги живут на
        споте, шорты на деривативе, и это РАЗНЫЕ счета HTX. Когда market_type
        не задан, считаем общий капитал робота — сумму обоих счетов, иначе
        половина денег невидима для сайзинга и система занижает размер.
        Fallback на RISK_EQUITY_USDT, если баланс недоступен.
        """
        fallback = float(getattr(settings, "RISK_EQUITY_USDT", 950.0))
        if not self.is_live() or not bool(getattr(settings, "LIVE_SIZE_FROM_BALANCE", True)):
            return fallback

        if market_type:
            free = self.free_usdt(market_type)
            return float(free) if free is not None and free > 0 else fallback

        total = 0.0
        seen = False
        accounts = ["spot"]
        if bool(getattr(settings, "ENABLE_FUTURES", False)):
            accounts.append("swap")
        for account in accounts:
            free = self.free_usdt(account)
            if free is not None:
                total += float(free)
                seen = True
        return total if seen and total > 0 else fallback

    # ── единицы объёма ──────────────────────────────────────────────────────────
    def _to_exchange_amount(self, symbol: str, amount: float, market_type: str) -> tuple[float, dict]:
        """Объём в единицах биржи для этого рынка.

        Спот принимает монеты. Linear-своп HTX принимает КОНТРАКТЫ: 1 контракт
        ADA-USDT = 10 ADA, поэтому объём в монетах, отправленный как есть,
        открыл бы позицию в 10 раз больше расчётной. Если размер контракта
        неизвестен, ордер не отправляем: угадывать здесь нельзя — ошибка
        измеряется кратностью позиции, а не процентами.
        """
        meta = {"submitted_unit": "base", "contract_size": None, "base_amount": amount}
        if str(market_type).lower() not in ("swap", "future", "futures", "linear"):
            return amount, meta

        getter = getattr(self.client, "contract_size", None)
        size = getter(symbol) if callable(getter) else None
        if not size or size <= 0:
            raise ValueError(f"contract_size_unknown:{symbol}")

        contracts = amount / float(size)
        try:
            contracts = float(self.client.amount_to_precision(symbol, contracts))
        except Exception:  # noqa: BLE001
            # (#contract-quantize-2026-08-03) Прежде здесь стояло `pass`, и
            # дробное число контрактов уходило на биржу как есть. Биржа их не
            # принимает — но значение было ПОЛОЖИТЕЛЬНЫМ, поэтому предохранитель
            # `send_amount <= 0` в place_market не срабатывал, и ордер уезжал
            # только чтобы вернуться отказом. Именно так выглядит объём меньше
            # одного контракта: 0.0004 BTC = 0.4 контракта.
            #
            # Округляем ВНИЗ сами: своп торгуется целыми контрактами, а вниз —
            # потому что превысить план опаснее, чем недобрать.
            contracts = float(math.floor(contracts))

        meta.update({"submitted_unit": "contracts", "contract_size": float(size)})
        return contracts, meta

    def quantize_base(self, symbol: str, amount: float, market_type: str) -> tuple[float, dict]:
        """Сколько базовой монеты биржа РЕАЛЬНО примет. (#contract-quantize-2026-08-03)

        Зачем отдельный публичный метод. Биржа принимает своп только целыми
        контрактами, и шаг у BTC грубый: 1 контракт = 0.001 BTC ≈ 64 USDT при
        цене 63650. На позицию ~190 USDT это ТРИ шага, и остаток отбрасывается:

            план 0.002979 BTC (189.6 USDT) → 2 контракта = 0.002 BTC (127.3 USDT)

        Пока бумага книжила план, а live отправлял округлённое, бумажная позиция
        по BTC была на 49% крупнее той, что биржа вообще способна открыть. Это
        расхождение не в комиссиях и не в проскальзывании — в размере позиции,
        то есть бумажный PnL по BTC несопоставим с live по построению.

        У ETH шаг мельче (0.01 ≈ 19 USDT), там расхождение 1.8% — поэтому баг и
        не бросался в глаза: он виден только на дорогих монетах.

        Возвращает (объём_в_базовой_монете, meta). Ноль означает, что позиция
        меньше одного контракта — такую сделку открывать нечем.
        """
        amount = float(amount)
        try:
            exchange_amount, meta = self._to_exchange_amount(symbol, amount, market_type)
        except ValueError as exc:
            return amount, {"submitted_unit": "unknown", "contract_size": None,
                            "base_amount": amount, "error": str(exc)}

        size = meta.get("contract_size")
        if not size:
            return exchange_amount, meta

        achievable = float(exchange_amount) * float(size)
        meta = dict(meta)
        meta.update({
            "requested_base": amount,
            "achievable_base": achievable,
            "contracts": float(exchange_amount),
            "shortfall_pct": (
                round((amount - achievable) / amount * 100, 3) if amount > 0 else 0.0
            ),
        })
        return achievable, meta

    # ── публичный вход: рыночный ордер ──────────────────────────────────────────
    def place_market(self, symbol: str, side: str, amount: float, *, market_type: str,
                     reduce_only: bool = False, leverage: float | None = None,
                     margin_mode: str | None = None,
                     reference_price: float | None = None, purpose: str = "") -> OrderResult:
        mode = self.effective_mode()
        amount = float(amount)
        base = dict(symbol=symbol, side=side, requested_qty=amount,
                    market_type=market_type, reduce_only=reduce_only)

        if mode == "off":
            return OrderResult(ok=False, mode="off", sent=False, status="off", **base)

        # предохранитель размера (нотионал)
        cap = float(getattr(settings, "LIVE_MAX_ORDER_NOTIONAL_USDT", 0.0) or 0.0)
        over_cap = bool(cap > 0 and reference_price and amount * float(reference_price) > cap)

        # (#dry-run-cap-2026-07-26) В LIVE кэп блокирует отправку — это его работа.
        # В DRY_RUN блокировать нельзя: весь смысл режима в том, чтобы прогнать
        # живой путь исполнения целиком и увидеть, что он делает. Кэп, стоящий
        # ДО ветки dry_run, обрывал прогон и делал этап 1 плана вывода в live
        # бессмысленным — в логах 26.07 видно ровно это:
        #   {"cap": 25.0, "event": "live_order_notional_cap", "notional": 249.34}
        # Здесь предупреждаем громко (владелец должен видеть, что в live этот
        # ордер был бы отклонён), но прогон продолжаем.
        if over_cap and mode != "dry_run":
            log_event(logger, logging.WARNING, "live_order_notional_cap",
                      symbol=symbol, notional=amount * float(reference_price), cap=cap,
                      blocked=True)
            return OrderResult(ok=False, mode=mode, sent=False, status="error",
                               error=f"notional>{cap}", **base)

        client_id = self._make_client_id(purpose)

        # DRY-RUN: проходим всю логику, но НЕ отправляем. Возвращаем синтетический ack.
        if mode == "dry_run":
            if over_cap:
                log_event(logger, logging.WARNING, "live_order_notional_cap",
                          symbol=symbol, notional=amount * float(reference_price), cap=cap,
                          blocked=False,
                          note="в LIVE этот ордер был бы ОТКЛОНЁН кэпом. Поднять "
                               "LIVE_MAX_ORDER_NOTIONAL_USDT или снизить размер позиции "
                               "ДО включения live — иначе бумага разойдётся с биржей")
            log_event(logger, logging.INFO, "live_dry_run_order", symbol=symbol, side=side,
                      qty=amount, market_type=market_type, reduce_only=reduce_only,
                      ref_price=reference_price, purpose=purpose, client_order_id=client_id)
            return OrderResult(ok=True, mode="dry_run", sent=False, status="dry_run",
                               client_order_id=client_id, filled_qty=amount,
                               avg_price=float(reference_price) if reference_price else None, **base)

        # LIVE: плечо/режим маржи → отправка (одна попытка) → сверка → подтверждение
        self._ensure_leverage(symbol, market_type, leverage, margin_mode)

        # Перевод объёма в единицы рынка. Ошибка здесь означала бы позицию
        # кратно больше расчётной, поэтому неизвестный размер контракта —
        # отказ, а не отправка «как есть».
        try:
            send_amount, unit_meta = self._to_exchange_amount(symbol, amount, market_type)
        except ValueError as exc:
            log_event(logger, logging.ERROR, "live_order_unit_unresolved",
                      symbol=symbol, market_type=market_type, error=str(exc))
            return OrderResult(ok=False, mode=mode, sent=False, status="error",
                               client_order_id=client_id, error=str(exc), **base)

        if send_amount <= 0:
            log_event(logger, logging.ERROR, "live_order_amount_below_one_contract",
                      symbol=symbol, base_amount=amount, contract_size=unit_meta.get("contract_size"))
            return OrderResult(ok=False, mode=mode, sent=False, status="error",
                               client_order_id=client_id,
                               error="amount_below_min_contract", **base)

        if unit_meta["submitted_unit"] == "contracts":
            log_event(logger, logging.INFO, "live_order_amount_in_contracts",
                      symbol=symbol, base_amount=amount,
                      contract_size=unit_meta["contract_size"], contracts=send_amount)

        params: dict[str, Any] = {"clientOrderId": client_id}
        if market_type:
            params["defaultType"] = market_type
        if reduce_only:
            params["reduceOnly"] = True

        try:
            order = self.client.create_order_once(symbol, "market", side, send_amount, None, params)
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
        filled_raw = float((order or {}).get("filled") or 0.0)
        avg = (order or {}).get("average") or (order or {}).get("price") or reference_price

        # Биржа отчиталась в тех же единицах, в которых приняла ордер. Наружу
        # отдаём объём в БАЗОВОЙ монете: учёт позиции, PnL и все проверки
        # системы живут в монетах, и смешивать их с контрактами нельзя.
        contract_size = unit_meta.get("contract_size")
        filled = filled_raw * float(contract_size) if contract_size else filled_raw

        log_event(logger, logging.INFO, "live_order_done", symbol=symbol, side=side,
                  status=status, filled_base=filled, filled_raw=filled_raw,
                  unit=unit_meta["submitted_unit"], avg=avg, client_order_id=client_id,
                  exchange_order_id=(order or {}).get("id"))
        return OrderResult(ok=status in ("closed", "filled"), mode="live", sent=True,
                           status=status, client_order_id=client_id,
                           exchange_order_id=(order or {}).get("id"),
                           filled_qty=filled, avg_price=float(avg) if avg else None,
                           raw=order, **base)


LIVE_EXECUTOR = LiveExecutor()
