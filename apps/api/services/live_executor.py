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
from services.exchange_factory import get_exchange_client

logger = get_logger(__name__)

_DERIVATIVE_TYPES = ("swap", "future", "futures", "linear")
_MARGIN_MODES = ("cross", "isolated")
# Режим позиций аккаунта нельзя сменить при открытых позициях и ордерах, поэтому
# между сделками он меняется редко; десяти минут хватает, чтобы не спрашивать
# биржу на каждый ордер.
_POSITION_MODE_TTL_SEC = 600.0


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
        self.client = get_exchange_client()
        self._leverage_set: set[tuple] = set()  # (symbol, margin_mode, leverage, position_side)
        self._bal_cache: dict[str, tuple[float, float]] = {}  # market_type -> (free_usdt, ts)
        self._account_state: tuple[dict, float] | None = None  # (режим счёта, ts)

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
                # HTX отдаёт номер то строкой, то числом — сравниваем как строки.
                if cid is not None and str(cid) == str(client_id):
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
    @staticmethod
    def _is_derivative(market_type: str | None) -> bool:
        return str(market_type or "").lower() in _DERIVATIVE_TYPES

    @staticmethod
    def resolve_margin_mode(margin_mode: str | None) -> str:
        """Режим маржи сделки: из маршрута, иначе LIVE_MARGIN_MODE.

        Неизвестное значение — ошибка, а не молчаливый cross: режим маржи решает,
        чем рискует позиция — своей маржой или всем счётом.
        """
        value = str(margin_mode or getattr(settings, "LIVE_MARGIN_MODE", "cross") or "").lower().strip()
        if value not in _MARGIN_MODES:
            raise ValueError(f"margin_mode_invalid:{value or 'empty'}")
        return value

    def derivatives_account(self) -> dict:
        """Режим деривативного счёта. (#live-margin-posmode-2026-09-16)

        {"hedged": bool | None, "blocker": str | None}

        hedged: True — Long/Short mode (OKX long_short_mode, HTX dual_side): у
        каждого ордера обязана быть сторона позиции, reduceOnly не применяется.
        False — One-way. None — режим неизвестен (нет метода или запрос не
        прошёл): ордер уходит как для One-way. Ошибка здесь безопасна — биржа
        отклонит ордер, и отказ остановит робота; позицию не того размера или
        направления она не откроет.

        blocker: счёт в режиме, где свопы через наш API не торгуются (OKX
        «только спот», HTX старый одновалютный залог при ccxt на API v5).
        Открытие в таком режиме не отправляется — владелец получает причину,
        а не отказ биржи без объяснений.
        """
        empty = {"hedged": None, "blocker": None}
        cached = getattr(self, "_account_state", None)
        if cached and (time.time() - cached[1]) < _POSITION_MODE_TTL_SEC:
            return cached[0]
        fetch = getattr(self.client, "fetch_derivatives_account", None)
        if not callable(fetch):
            return empty
        try:
            raw = fetch() or {}
            hedged = raw.get("hedged")
            state = {
                "hedged": None if hedged is None else bool(hedged),
                "blocker": raw.get("blocker") or None,
            }
        except Exception as exc:  # noqa: BLE001
            log_event(logger, logging.WARNING, "live_account_mode_fetch_failed",
                      error=f"{type(exc).__name__}: {exc}")
            return cached[0] if cached else empty
        if not cached or cached[0] != state:
            log_event(logger, logging.WARNING if state["blocker"] else logging.INFO,
                      "live_account_mode", hedged=state["hedged"], blocker=state["blocker"],
                      mode=("long_short" if state["hedged"] else
                            "one_way" if state["hedged"] is False else "unknown"))
        self._account_state = (state, time.time())
        return state

    def position_hedged(self) -> bool | None:
        return self.derivatives_account()["hedged"]

    def _client_order_id(self, purpose: str) -> str:
        """Номер ордера в формате биржи: у OKX буквы и цифры до 32 символов,
        у HTX-свопа только целое число — буквенный ccxt htx не передаёт вовсе."""
        maker = getattr(getattr(self, "client", None), "make_client_order_id", None)
        if callable(maker):
            return str(maker(purpose))
        return self._make_client_id(purpose)

    @staticmethod
    def _position_side(side: str, reduce_only: bool) -> str:
        """Сторона позиции в Long/Short mode: buy открывает лонг или закрывает шорт."""
        is_buy = str(side).lower() == "buy"
        if reduce_only:
            return "short" if is_buy else "long"
        return "long" if is_buy else "short"

    @classmethod
    def order_params(cls, *, client_id: str, market_type: str | None, side: str,
                     reduce_only: bool, margin_mode: str | None,
                     hedged: bool | None,
                     position_side_key: str = "positionSide") -> dict[str, Any]:
        """Параметры ордера для ccxt — одинаковые для любого этапа сделки.

        Без `marginMode` ccxt подставляет cross: у OKX tdMode=cross, у HTX
        margin_mode=cross. План же сделки — isolated (TREND_MARGIN_MODE), и
        плечо настраивалось под isolated, а позиция открывалась бы в cross и
        рисковала всем счётом. Закрытие обязано идти в том же режиме, что и
        открытие: у OKX reduce-only в cross не закрывает isolated-позицию.

        Сторона позиции у бирж называется по-разному: ccxt okx разбирает
        `positionSide`, ccxt htx для v5 такого ключа не знает и передаёт бирже
        родной `position_side` как есть (ключ задаёт клиент биржи).

        `defaultType` не передаём: рынок ccxt определяет по символу, а ключ
        ccxt okx/htx не разбирает и отправляет бирже в теле ордера как есть.
        """
        params: dict[str, Any] = {"clientOrderId": client_id}
        if cls._is_derivative(market_type):
            params["marginMode"] = margin_mode
            if hedged:
                params[position_side_key] = cls._position_side(side, reduce_only)
                return params
        if reduce_only:
            params["reduceOnly"] = True
        return params

    @staticmethod
    def _leverage_value(leverage: float | None) -> float | int:
        lev = float(leverage or getattr(settings, "FUTURES_LEVERAGE", 1) or 1)
        lev = max(1.0, min(lev, float(getattr(settings, "LIVE_MAX_LEVERAGE", 5.0))))  # потолок-предохранитель
        return int(lev) if lev.is_integer() else lev

    def _ensure_leverage(self, symbol: str, market_type: str, leverage: float | None,
                         margin_mode: str | None = None,
                         position_side: str | None = None) -> str | None:
        """Плечо под режим маржи сделки. None — готово (или настройка выключена),
        строка — причина, по которой открывать позицию нельзя.

        Прежде ошибка только писалась в лог, и ордер уходил при том плече, что
        стояло на бирже: у isolated-позиции с плечом 10× ликвидация в ~10% от
        входа, а стопы у робота программные.
        """
        if not self._is_derivative(market_type) or not bool(getattr(settings, "LIVE_SET_LEVERAGE", True)):
            return None
        lev_out = self._leverage_value(leverage)
        try:
            mm = self.resolve_margin_mode(margin_mode)
        except ValueError as exc:
            return str(exc)
        key = (symbol, mm, lev_out, position_side)
        if key in self._leverage_set:
            return None
        try:
            setter = getattr(self.client, "set_swap_leverage", None)
            if callable(setter):
                setter(symbol, lev_out, mm, position_side)
            else:
                params: dict[str, Any] = {"marginMode": mm}
                if position_side:
                    params["posSide"] = position_side
                self.client.set_leverage(lev_out, symbol, params)
        except Exception as exc:  # noqa: BLE001
            log_event(logger, logging.ERROR, "live_leverage_set_fail", symbol=symbol,
                      leverage=lev_out, margin=mm, position_side=position_side,
                      error=f"{type(exc).__name__}: {exc}")
            return f"leverage_setup_failed:{type(exc).__name__}: {exc}"
        self._leverage_set.add(key)
        log_event(logger, logging.INFO, "live_leverage_set", symbol=symbol, leverage=lev_out,
                  margin=mm, position_side=position_side)
        return None

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
            # (#okx-live-double-conversion-2026-09-16) Клиент биржи округляет ОБЪЁМ
            # В МОНЕТАХ по шагу лота своего рынка и монеты же возвращает. Прежде
            # сюда передавались КОНТРАКТЫ, и клиент делил их на размер контракта
            # второй раз. При размере контракта больше монеты ордер раздувался:
            # на OKX план 100 XRP уходил как 100 контрактов = 10 000 XRP
            # (~14 000 USDT), 3000 DOGE — как 1000 контрактов = 1 000 000 DOGE.
            # Кэп нотионала проверяется до перевода, на плановом объёме, и этого
            # не ловил.
            base_q = float(self.client.amount_to_precision(symbol, amount))
            if base_q > amount * (1 + 1e-9):
                # Клиент вернул больше плана — не верим ему: на биржу никогда
                # не уходит больше, чем рассчитано.
                raise ValueError(f"precision_above_plan:{base_q}>{amount}")
            contracts = round(base_q / float(size), 8)
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

    def exchange_position_base(self, symbol: str, side: str, market_type: str,
                               margin_mode: str | None = None) -> float | None:
        """Размер позиции НА БИРЖЕ в базовой монете. (#live-close-safety-2026-09-16)

        None — узнать не удалось (спот, нет метода, сбой запроса): вызывающий
        обязан закрывать по своему учёту. 0.0 — запрос прошёл, позиции нет.
        Клиенты бросают исключение при сбое, а пустой список отдают только на
        успешный ответ — поэтому ноль здесь означает «на бирже пусто», а не
        «не спросили».

        Считаются только позиции в режиме маржи сделки: у OKX по одному символу
        cross и isolated — разные позиции, и reduce-only в isolated не закроет
        объём, лежащий в cross.
        """
        if not self._is_derivative(market_type):
            return None
        try:
            want_margin = self.resolve_margin_mode(margin_mode)
        except ValueError:
            want_margin = None
        fetch = getattr(self.client, "fetch_positions", None)
        if not callable(fetch):
            return None
        try:
            positions = fetch() or []
        except Exception as exc:  # noqa: BLE001
            log_event(logger, logging.WARNING, "live_position_fetch_failed",
                      symbol=symbol, error=f"{type(exc).__name__}: {exc}")
            return None

        getter = getattr(self.client, "contract_size", None)
        default_size = getter(symbol) if callable(getter) else None
        want_side = str(side or "").lower()
        total = 0.0
        for p in positions:
            if not isinstance(p, dict) or p.get("symbol") != symbol:
                continue
            p_side = str(p.get("side") or "").lower()
            if p_side and want_side and p_side != want_side:
                continue
            p_margin = str(p.get("marginMode") or "").lower()
            if p_margin and want_margin and p_margin != want_margin:
                continue
            try:
                contracts = abs(float(p.get("contracts") or 0.0))
                size = float(p.get("contractSize") or default_size or 0.0)
            except (TypeError, ValueError):
                continue
            total += contracts * size
        return round(total, 12)

    # ── стоп на бирже (#exchange-stop-2026-09-16) ───────────────────────────────
    @staticmethod
    def _close_side(position_side: str) -> str:
        return "sell" if str(position_side).lower() in ("long", "buy") else "buy"

    def exchange_stop_trigger(self, symbol: str, position_side: str, stop_price: float) -> float:
        """Цена срабатывания биржевого стопа: программный стоп, отодвинутый на
        LIVE_EXCHANGE_STOP_BUFFER_PCT дальше от входа.

        Биржевой стоп — страховка на время, когда робот не работает. Пока робот
        жив, первым должен срабатывать программный стоп со всей своей логикой
        (цена выхода, учёт, алерты); стоп на той же цене гонялся бы с ним за
        одну и ту же позицию.
        """
        buffer = max(0.0, float(getattr(settings, "LIVE_EXCHANGE_STOP_BUFFER_PCT", 0.5))) / 100.0
        stop = float(stop_price)
        raw = stop * (1 - buffer) if str(position_side).lower() in ("long", "buy") else stop * (1 + buffer)
        getter = getattr(self.client, "price_to_precision", None)
        try:
            return float(getter(symbol, raw)) if callable(getter) else raw
        except Exception:  # noqa: BLE001
            return raw

    def contracts_for(self, symbol: str, base_qty: float, market_type: str) -> float:
        """Объём позиции в единицах биржевого ордера (контракты для свопа)."""
        amount, _meta = self._to_exchange_amount(symbol, float(base_qty), market_type)
        return float(amount)

    @staticmethod
    def _normalize_stop(order: dict) -> dict | None:
        if not isinstance(order, dict) or not order.get("id"):
            return None
        trigger = order.get("stopLossPrice") or order.get("triggerPrice") or order.get("stopPrice")
        try:
            return {
                "order_id": str(order["id"]),
                "side": str(order.get("side") or "").lower(),
                "trigger": float(trigger) if trigger is not None else None,
                "contracts": float(order.get("amount") or 0.0),
            }
        except (TypeError, ValueError):
            return None

    def open_stop_orders(self, symbol: str, market_type: str) -> list[dict] | None:
        """Стоп-ордера на бирже по символу. None — узнать не удалось.

        Все условные стопы символа считаются робота: на торговом счёте не
        держим ручных позиций и ордеров по символам робота.
        """
        if not self._is_derivative(market_type):
            return []
        fetch = getattr(self.client, "fetch_open_stop_orders", None)
        if not callable(fetch):
            return None
        try:
            raw = fetch(symbol) or []
        except Exception as exc:  # noqa: BLE001
            log_event(logger, logging.WARNING, "live_stop_fetch_failed", symbol=symbol,
                      error=f"{type(exc).__name__}: {exc}")
            return None
        return [s for s in (self._normalize_stop(o) for o in raw) if s]

    def place_stop_order(self, symbol: str, position_side: str, contracts: float,
                         trigger_price: float, *, market_type: str,
                         margin_mode: str | None) -> dict:
        """Поставить стоп-лосс на бирже. {'ok', 'order_id', 'error', ...}"""
        close_side = self._close_side(position_side)
        base = {"symbol": symbol, "side": close_side, "trigger": float(trigger_price),
                "contracts": float(contracts)}
        create = getattr(self.client, "create_stop_loss_order", None)
        if not callable(create):
            return {**base, "ok": False, "error": "exchange_stop_not_supported"}
        if contracts <= 0:
            return {**base, "ok": False, "error": "amount_below_min_contract"}
        try:
            mm = self.resolve_margin_mode(margin_mode)
        except ValueError as exc:
            return {**base, "ok": False, "error": str(exc)}
        hedged = self.derivatives_account()["hedged"]
        client_id = self._client_order_id("slexch")
        params = self.order_params(
            client_id=client_id, market_type=market_type, side=close_side,
            reduce_only=True, margin_mode=mm, hedged=hedged,
            position_side_key=getattr(self.client, "POSITION_SIDE_PARAM", "positionSide"),
        )
        try:
            order = create(symbol, close_side, float(contracts), float(trigger_price), params) or {}
        except Exception as exc:  # noqa: BLE001
            # Неоднозначно: стоп мог встать. Следующая сверка увидит его в
            # открытых стопах и примет, а не поставит второй.
            log_event(logger, logging.ERROR, "live_stop_place_failed", symbol=symbol,
                      side=close_side, trigger=trigger_price, contracts=contracts,
                      error=f"{type(exc).__name__}: {exc}")
            return {**base, "ok": False, "client_order_id": client_id,
                    "error": f"{type(exc).__name__}: {exc}"}
        order_id = order.get("id")
        if not order_id:
            return {**base, "ok": False, "client_order_id": client_id, "error": "no_order_id"}
        log_event(logger, logging.INFO, "live_stop_placed", symbol=symbol, side=close_side,
                  trigger=trigger_price, contracts=contracts, order_id=order_id)
        return {**base, "ok": True, "order_id": str(order_id), "client_order_id": client_id}

    def cancel_stop_order(self, symbol: str, order_id: str, market_type: str) -> bool:
        cancel = getattr(self.client, "cancel_stop_order", None)
        if not callable(cancel) or not self._is_derivative(market_type):
            return False
        try:
            cancel(order_id, symbol)
        except Exception as exc:  # noqa: BLE001
            text = f"{type(exc).__name__}: {exc}"
            # Уже исполнен или снят — цель достигнута, стопа больше нет.
            if type(exc).__name__ == "OrderNotFound":
                log_event(logger, logging.INFO, "live_stop_already_gone", symbol=symbol,
                          order_id=order_id)
                return True
            log_event(logger, logging.WARNING, "live_stop_cancel_failed", symbol=symbol,
                      order_id=order_id, error=text)
            return False
        log_event(logger, logging.INFO, "live_stop_cancelled", symbol=symbol, order_id=order_id)
        return True

    # ── сборка ордера: одна для live и dry_run (#dry-run-parity-2026-09-16) ─────
    def prepare_order(self, symbol: str, side: str, amount: float, *, market_type: str,
                      reduce_only: bool, leverage: float | None, margin_mode: str | None,
                      client_id: str, hedged: bool | None) -> dict:
        """Всё, что уйдёт на биржу, без обращения к ней.

        {"ok", "error", "error_event", "margin_mode", "amount", "unit_meta",
         "params", "leverage"}

        Прежде dry_run выходил ДО перевода в контракты, режима маржи и сборки
        параметров: в paper нельзя было увидеть ни объём в контрактах, ни
        marginMode, ни отказ «меньше одного контракта» — всё это впервые
        случилось бы на живой бирже. Теперь обе ветки собирают ордер здесь.
        """
        out: dict[str, Any] = {"ok": False, "error": None, "error_event": None,
                               "margin_mode": None, "amount": None, "unit_meta": None,
                               "params": None, "leverage": None}
        derivative = self._is_derivative(market_type)
        if derivative:
            try:
                out["margin_mode"] = self.resolve_margin_mode(margin_mode)
            except ValueError as exc:
                return {**out, "error": str(exc), "error_event": "live_order_margin_mode_invalid"}

        # Перевод объёма в единицы рынка. Ошибка здесь означала бы позицию
        # кратно больше расчётной, поэтому неизвестный размер контракта —
        # отказ, а не отправка «как есть».
        try:
            send_amount, unit_meta = self._to_exchange_amount(symbol, float(amount), market_type)
        except ValueError as exc:
            return {**out, "error": str(exc), "error_event": "live_order_unit_unresolved"}
        out.update(amount=send_amount, unit_meta=unit_meta)
        if send_amount <= 0:
            return {**out, "error": "amount_below_min_contract",
                    "error_event": "live_order_amount_below_one_contract"}

        out["params"] = self.order_params(
            client_id=client_id, market_type=market_type, side=side, reduce_only=reduce_only,
            margin_mode=out["margin_mode"], hedged=hedged,
            position_side_key=getattr(self.client, "POSITION_SIDE_PARAM", "positionSide"),
        )
        # Плечо — только для открытия: закрытие риск не добавляет.
        if derivative and not reduce_only and bool(getattr(settings, "LIVE_SET_LEVERAGE", True)):
            out["leverage"] = self._leverage_value(leverage)
        out["ok"] = True
        return out

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

        client_id = self._client_order_id(purpose)

        # DRY-RUN: проходим всю логику, но НЕ отправляем. Возвращаем синтетический ack.
        if mode == "dry_run":
            if over_cap:
                log_event(logger, logging.WARNING, "live_order_notional_cap",
                          symbol=symbol, notional=amount * float(reference_price), cap=cap,
                          blocked=False,
                          note="в LIVE этот ордер был бы ОТКЛОНЁН кэпом. Поднять "
                               "LIVE_MAX_ORDER_NOTIONAL_USDT или снизить размер позиции "
                               "ДО включения live — иначе бумага разойдётся с биржей")
            # (#dry-run-parity-2026-09-16) Та же сборка, что в live: контракты,
            # режим маржи, параметры, плечо. Режим счёта (приватный запрос) в
            # dry_run не спрашивается — сторона позиции собирается как для
            # One-way. Бумага не меняется: результат прежний, а отказ, который
            # случился бы в live, виден в логе и в поле error.
            try:
                prepared = self.prepare_order(
                    symbol, side, amount, market_type=market_type, reduce_only=reduce_only,
                    leverage=leverage, margin_mode=margin_mode, client_id=client_id, hedged=None,
                )
            except Exception as exc:  # noqa: BLE001 — сборка не должна ронять бумагу
                prepared = {"ok": False, "error": f"prepare_failed:{type(exc).__name__}: {exc}"}
            would_reject = None if prepared.get("ok") else prepared.get("error")
            if over_cap and would_reject is None:
                would_reject = f"notional>{cap}"
            unit_meta = prepared.get("unit_meta") or {}
            log_event(logger, logging.WARNING if would_reject else logging.INFO,
                      "live_dry_run_order", symbol=symbol, side=side,
                      qty=amount, market_type=market_type, reduce_only=reduce_only,
                      ref_price=reference_price, purpose=purpose, client_order_id=client_id,
                      margin_mode=prepared.get("margin_mode"),
                      exchange_amount=prepared.get("amount"),
                      submitted_unit=unit_meta.get("submitted_unit"),
                      contract_size=unit_meta.get("contract_size"),
                      params=prepared.get("params"), leverage=prepared.get("leverage"),
                      account_mode="not_checked_in_dry_run",
                      would_reject=would_reject)
            return OrderResult(ok=True, mode="dry_run", sent=False, status="dry_run",
                               client_order_id=client_id, filled_qty=amount,
                               avg_price=float(reference_price) if reference_price else None,
                               error=f"would_reject:{would_reject}" if would_reject else None,
                               **base)

        # LIVE: режим счёта → сборка ордера → плечо → отправка (одна попытка) → сверка → подтверждение
        hedged: bool | None = None
        if self._is_derivative(market_type):
            # Неизвестный режим маржи — отказ до любых запросов к бирже.
            try:
                self.resolve_margin_mode(margin_mode)
            except ValueError as exc:
                log_event(logger, logging.ERROR, "live_order_margin_mode_invalid",
                          symbol=symbol, error=str(exc))
                return OrderResult(ok=False, mode=mode, sent=False, status="error",
                                   client_order_id=client_id, error=str(exc), **base)
            account = self.derivatives_account()
            hedged = account["hedged"]
            # Режим счёта, в котором свопы не торгуются, блокирует только
            # открытие: закрытие отправляется всегда — ошибочная блокировка
            # выхода опаснее отказа биржи.
            if account["blocker"] and not reduce_only:
                log_event(logger, logging.ERROR, "live_order_account_mode_blocked",
                          symbol=symbol, blocker=account["blocker"])
                return OrderResult(ok=False, mode=mode, sent=False, status="error",
                                   client_order_id=client_id, error=account["blocker"], **base)

        prepared = self.prepare_order(
            symbol, side, amount, market_type=market_type, reduce_only=reduce_only,
            leverage=leverage, margin_mode=margin_mode, client_id=client_id, hedged=hedged,
        )
        if not prepared["ok"]:
            unit_meta = prepared.get("unit_meta") or {}
            log_event(logger, logging.ERROR, prepared["error_event"], symbol=symbol,
                      market_type=market_type, base_amount=amount,
                      contract_size=unit_meta.get("contract_size"), error=prepared["error"])
            return OrderResult(ok=False, mode=mode, sent=False, status="error",
                               client_order_id=client_id, error=prepared["error"], **base)

        # Плечо — только для открытия: закрытие риск не добавляет, и сбой
        # настройки плеча не должен мешать выйти из позиции.
        if prepared["leverage"] is not None:
            leverage_error = self._ensure_leverage(
                symbol, market_type, leverage, prepared["margin_mode"],
                self._position_side(side, False) if hedged else None,
            )
            if leverage_error:
                return OrderResult(ok=False, mode=mode, sent=False, status="error",
                                   client_order_id=client_id, error=leverage_error, **base)

        send_amount, unit_meta, params = prepared["amount"], prepared["unit_meta"], prepared["params"]
        if unit_meta["submitted_unit"] == "contracts":
            log_event(logger, logging.INFO, "live_order_amount_in_contracts",
                      symbol=symbol, base_amount=amount,
                      contract_size=unit_meta["contract_size"], contracts=send_amount)

        try:
            order = self.client.create_order_once(symbol, "market", side, send_amount, None, params)
        except Exception as exc:  # noqa: BLE001 — НЕОДНОЗНАЧНО: мог пройти. Сверяем.
            log_event(logger, logging.ERROR, "live_create_ambiguous", symbol=symbol,
                      client_order_id=client_id, error=str(exc))
            found = self._find_by_client_id(symbol, client_id)
            if not found:
                # Отказ мог быть из-за смены режима счёта — следующий ордер
                # спросит биржу заново, а не возьмёт кеш.
                self._account_state = None
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
