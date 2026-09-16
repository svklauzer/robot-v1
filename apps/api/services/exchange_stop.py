"""Стоп-лосс на бирже для live-позиций (#exchange-stop-2026-09-16).

Зачем. Стопы робота программные: цикл сопровождения раз в ~10 с сравнивает цену
со стопом и закрывает позицию рыночным ордером. Пока робот работает, это лучше
биржевого стопа — выход знает цену, учёт и алерты. Но рестарт, деплой, OOM или
обрыв сети оставляют live-позицию на бирже без защиты: цена может пройти стоп и
уйти сколько угодно далеко, пока процесс поднимается.

Что делает. Держит на бирже один условный стоп-лосс по рынку на каждую открытую
позицию свопа:

  • цена срабатывания — программный стоп, отодвинутый на
    LIVE_EXCHANGE_STOP_BUFFER_PCT: пока робот жив, первым срабатывает
    программный стоп, биржевой ему не конкурент;
  • объём — позиция в учёте; после частичного закрытия стоп переставляется;
  • безубыток, фиксация после TP1 и трейл двигают программный стоп — биржевой
    переставляется, когда расхождение больше LIVE_EXCHANGE_STOP_MIN_MOVE_PCT;
  • полное закрытие снимает стопы символа.

Как устроено. Не «событие → действие», а сверка с желаемым состоянием: после
каждого прохода сопровождения желаемый стоп (цена, объём) сравнивается с тем,
что стоит на бирже, и расхождение устраняется. Поэтому одна логика покрывает
открытие, переносы, частичные закрытия, рестарт робота и ручное вмешательство.
Новый стоп ставится ДО снятия старого — позиция не остаётся без защиты между
вызовами.

Режимы. live — настоящие ордера. dry_run — тот же путь сверки, но «биржа» —
запись в сделке, а постановка и снятие только пишутся в лог
`live_dry_run_exchange_stop` (#dry-run-parity-2026-09-16): в paper видно, какой
стоп, когда и почему встал бы на бирже, без единого запроса к ней. off — ничего.
Бумажная торговля не меняется ни в одном режиме.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from core.config import settings
from core.logging import get_logger, log_event
from services.market_routing import from_payload as route_from_payload

logger = get_logger(__name__)

STATE_KEY = "exchange_stop"
Alert = Callable[[str, str], Awaitable[Any]]


def _enabled() -> bool:
    return bool(getattr(settings, "LIVE_EXCHANGE_STOP_ENABLED", True))


def _executor():
    from services.live_executor import LIVE_EXECUTOR

    return LIVE_EXECUTOR


def stops_mode(executor=None) -> str | None:
    """Как ведётся стоп на бирже: "live", "dry_run" или None (off/выключено)."""
    try:
        if not _enabled():
            return None
        mode = (executor or _executor()).effective_mode()
    except Exception:  # noqa: BLE001
        return None
    return mode if mode in ("live", "dry_run") else None


class _DryRunStops:
    """Биржа для dry_run: стоп «стоит» в записи сделки, ордера только в лог.

    Сверка идёт тем же путём, что в live, поэтому в paper видно ровно то, что
    делал бы live: постановку, перенос вслед за программным стопом, пересчёт
    после частичного закрытия, снятие при закрытии.
    """

    def __init__(self, executor, signal):
        self.executor = executor
        self.signal = signal

    def open_stop_orders(self, symbol: str, market_type: str) -> list[dict]:
        state = (self.signal.plan_json or {}).get(STATE_KEY) or {}
        if state.get("mode") != "dry_run" or not state.get("order_id"):
            return []
        return [{"order_id": str(state["order_id"]), "side": state.get("side"),
                 "trigger": state.get("trigger"), "contracts": float(state.get("contracts") or 0.0)}]

    def place_stop_order(self, symbol: str, position_side: str, contracts: float,
                         trigger_price: float, *, market_type: str, margin_mode: str | None) -> dict:
        order_id = f"dry{uuid.uuid4().hex[:12]}"
        try:
            close_side, _client_id, params = self.executor.stop_order_params(
                position_side, margin_mode, hedged=None, market_type=market_type)
        except ValueError as exc:
            log_event(logger, logging.WARNING, "live_dry_run_exchange_stop", action="place",
                      signal_id=getattr(self.signal, "id", None), symbol=symbol,
                      would_reject=str(exc))
            return {"ok": True, "order_id": order_id}
        log_event(logger, logging.INFO, "live_dry_run_exchange_stop", action="place",
                  signal_id=getattr(self.signal, "id", None), symbol=symbol, side=close_side,
                  trigger=trigger_price, contracts=contracts, params=params, order_id=order_id)
        return {"ok": True, "order_id": order_id}

    def cancel_stop_order(self, symbol: str, order_id: str, market_type: str) -> bool:
        log_event(logger, logging.INFO, "live_dry_run_exchange_stop", action="cancel",
                  signal_id=getattr(self.signal, "id", None), symbol=symbol, order_id=order_id)
        return True


# Сделки, по которым уже предупредили, что объём в контрактах не считается, —
# чтобы не повторять одно и то же каждые 10 секунд.
_UNIT_WARNED: set[tuple] = set()


def _save_state(signal, state: dict | None) -> None:
    plan = dict(signal.plan_json or {})
    if state is None:
        plan.pop(STATE_KEY, None)
    else:
        plan[STATE_KEY] = state
    signal.plan_json = plan
    try:
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(signal, "plan_json")
    except Exception:  # noqa: BLE001 — не ORM-объект (тесты)
        pass


def _close_enough(a: float | None, b: float | None, tolerance_pct: float) -> bool:
    if a is None or b is None or b == 0:
        return False
    return abs(float(a) - float(b)) / abs(float(b)) * 100.0 <= tolerance_pct


def _same_contracts(a: float, b: float) -> bool:
    return abs(float(a) - float(b)) <= max(1e-9, abs(float(b)) * 1e-6)


def _foreign_order_ids(db, signal) -> set[str]:
    """Стопы, записанные за ДРУГИМИ открытыми сделками того же символа.

    Сейчас на символ одна активная сделка (MAX_ACTIVE_SIGNALS_PER_SYMBOL=1), но
    это настройка: поднимут лимит — сверка одной сделки не должна снимать стоп
    соседней.
    """
    if db is None:
        return set()
    try:
        from models.signal import Signal
        from services.exposure_guard import ACTIVE_SIGNAL_STATUSES

        rows = (
            db.query(Signal)
            .filter(
                Signal.bot_id == signal.bot_id,
                Signal.symbol == signal.symbol,
                Signal.id != signal.id,
                Signal.status.in_(ACTIVE_SIGNAL_STATUSES),
            )
            .all()
        )
    except Exception:  # noqa: BLE001
        return set()
    ids = set()
    for row in rows:
        order_id = ((row.plan_json or {}).get(STATE_KEY) or {}).get("order_id")
        if order_id:
            ids.add(str(order_id))
    return ids


class ExchangeStopService:
    def __init__(self, executor=None):
        self.executor = executor or _executor()

    # ── желаемое состояние ──────────────────────────────────────────────────────
    def desired(self, signal, position) -> dict | None:
        """Какой стоп должен стоять на бирже. None — никакой."""
        route = route_from_payload(signal.plan_json, position.symbol, position.side)
        if not self.executor._is_derivative(route.market_type):
            # Спотовых позиций у live нет: при ENABLE_FUTURES_EXECUTION все
            # сделки идут через своп. Спотовый стоп блокировал бы монеты.
            return None
        stop = float(signal.stop_price or 0.0)
        qty = float(position.qty or 0.0)
        if stop <= 0 or qty <= 0:
            return None
        side = str(position.side).lower()
        contracts = self.executor.contracts_for(route.exchange_symbol, qty, route.market_type)
        return {
            "symbol": route.exchange_symbol,
            "market_type": route.market_type,
            "margin_mode": route.margin_mode,
            "position_side": side,
            "side": self.executor._close_side(side),
            "trigger": self.executor.exchange_stop_trigger(route.exchange_symbol, side, stop),
            "contracts": contracts,
            "software_stop": stop,
        }

    # ── сверка ─────────────────────────────────────────────────────────────────
    async def sync(self, db, signal, position, *, price: float | None = None,
                   alert: Alert | None = None) -> dict:
        """Привести стоп на бирже к желаемому. Никогда не бросает исключение."""
        try:
            return await self._sync(db, signal, position, price=price, alert=alert)
        except Exception as exc:  # noqa: BLE001 — сопровождение не должно падать из-за стопа
            log_event(logger, logging.ERROR, "exchange_stop_sync_error",
                      signal_id=getattr(signal, "id", None), error=f"{type(exc).__name__}: {exc}")
            return {"action": "error", "error": f"{type(exc).__name__}: {exc}"}

    def _ops(self, mode: str, signal):
        return self.executor if mode == "live" else _DryRunStops(self.executor, signal)

    async def _sync(self, db, signal, position, *, price, alert) -> dict:
        mode = stops_mode(self.executor)
        if mode is None:
            return {"action": "inactive"}
        ops = self._ops(mode, signal)
        state = dict((signal.plan_json or {}).get(STATE_KEY) or {})

        if position is None or str(getattr(position, "status", "open")) != "open":
            if state.get("order_id"):
                return await self.cancel_all(db, signal, alert=alert, reason="position_closed")
            return {"action": "no_position"}

        try:
            want = self.desired(signal, position)
        except ValueError as exc:
            # Размер контракта неизвестен (рынки не загружены): стоп не из чего
            # считать. Предупреждаем один раз на сделку, а не каждый проход.
            key = (getattr(signal, "id", None), str(exc))
            if key not in _UNIT_WARNED:
                _UNIT_WARNED.add(key)
                log_event(logger, logging.WARNING, "exchange_stop_unit_unresolved",
                          signal_id=key[0], mode=mode, error=str(exc))
            return {"action": "unit_unresolved", "error": str(exc)}
        if want is None:
            return {"action": "not_applicable"}
        if want["contracts"] <= 0:
            return {"action": "below_min_contract"}

        # Цена уже за точкой срабатывания: позицию закрывает программный стоп
        # (или закрытие повторяется после отказа). Стоп с такой ценой биржа
        # отклонит — не шумим отказами.
        if price is not None and self._beyond_trigger(want["position_side"], float(price), want["trigger"]):
            return {"action": "price_beyond_trigger"}

        move_pct = float(getattr(settings, "LIVE_EXCHANGE_STOP_MIN_MOVE_PCT", 0.1))
        verify_sec = float(getattr(settings, "LIVE_EXCHANGE_STOP_VERIFY_SEC", 60.0))
        if mode == "dry_run":
            # Сверять запись саму с собой незачем — только лишние записи в БД.
            verify_sec = float("inf")
        now = time.time()
        if (
            state.get("order_id")
            and _close_enough(state.get("trigger"), want["trigger"], move_pct)
            and _same_contracts(float(state.get("contracts") or 0.0), want["contracts"])
            and now - float(state.get("checked_ts") or 0.0) < verify_sec
        ):
            return {"action": "unchanged"}

        existing = ops.open_stop_orders(want["symbol"], want["market_type"])
        if existing is None:
            return {"action": "fetch_failed"}
        if mode == "live":
            foreign = _foreign_order_ids(db, signal)
            existing = [s for s in existing if s["order_id"] not in foreign]

        matching = [
            s for s in existing
            if s["side"] == want["side"]
            and _close_enough(s["trigger"], want["trigger"], move_pct)
            and _same_contracts(s["contracts"], want["contracts"])
        ]
        if matching:
            keep = next((s for s in matching if s["order_id"] == state.get("order_id")), matching[0])
            extra = [s for s in existing if s["order_id"] != keep["order_id"]]
            self._cancel_many(ops, want, extra)
            self._record(signal, want, keep["order_id"], keep["trigger"], keep["contracts"], state, now, mode)
            return {"action": "verified", "order_id": keep["order_id"], "cancelled": len(extra), "mode": mode}

        placed = ops.place_stop_order(
            want["symbol"], want["position_side"], want["contracts"], want["trigger"],
            market_type=want["market_type"], margin_mode=want["margin_mode"],
        )
        if not placed.get("ok"):
            await self._on_place_failed(db, signal, want, placed, state, alert)
            return {"action": "place_failed", "error": placed.get("error")}

        # Новый стоп стоит — только теперь снимаем прежние.
        cancelled = self._cancel_many(ops, want, existing)
        self._record(signal, want, placed["order_id"], want["trigger"], want["contracts"], state, now, mode)
        return {"action": "replaced" if existing else "placed",
                "order_id": placed["order_id"], "cancelled": cancelled, "mode": mode}

    @staticmethod
    def _beyond_trigger(position_side: str, price: float, trigger: float) -> bool:
        if position_side in ("long", "buy"):
            return price <= trigger
        return price >= trigger

    @staticmethod
    def _cancel_many(ops, want: dict, orders: list[dict]) -> int:
        done = 0
        for order in orders:
            if ops.cancel_stop_order(want["symbol"], order["order_id"], want["market_type"]):
                done += 1
        return done

    @staticmethod
    def _record(signal, want: dict, order_id: str, trigger, contracts, state: dict, now: float,
                mode: str = "live") -> None:
        changed = state.get("order_id") != order_id
        new_state = {
            "mode": mode,
            "order_id": order_id,
            "symbol": want["symbol"],
            "side": want["side"],
            "trigger": float(trigger) if trigger is not None else want["trigger"],
            "contracts": float(contracts),
            "software_stop": want["software_stop"],
            "placed_at": (datetime.now(timezone.utc).isoformat() if changed
                          else state.get("placed_at")),
            "checked_ts": now,
            "failures": 0,
        }
        if changed or any(state.get(k) != new_state[k] for k in ("trigger", "contracts", "failures")):
            log_event(logger, logging.INFO, "exchange_stop_state", signal_id=signal.id, mode=mode,
                      order_id=order_id, trigger=new_state["trigger"],
                      contracts=new_state["contracts"], software_stop=want["software_stop"])
        _save_state(signal, new_state)

    async def _on_place_failed(self, db, signal, want: dict, placed: dict, state: dict,
                               alert: Alert | None) -> None:
        """Стоп не встал. Программный стоп продолжает работать, повтор — на
        следующем проходе. Владелец узнаёт на первом отказе и на каждом десятом;
        с третьего подряд отказа kill switch останавливает новые входы: позиции
        без страховки на бирже множить нельзя."""
        failures = int(state.get("failures") or 0) + 1
        state = {**state, "failures": failures, "last_error": placed.get("error"),
                 "last_failed_at": datetime.now(timezone.utc).isoformat()}
        _save_state(signal, state)
        log_event(logger, logging.ERROR, "exchange_stop_place_failed", signal_id=signal.id,
                  symbol=want["symbol"], attempt=failures, trigger=want["trigger"],
                  contracts=want["contracts"], error=placed.get("error"))

        halt_after = int(getattr(settings, "LIVE_EXCHANGE_STOP_HALT_AFTER_FAILURES", 3))
        if failures == halt_after and db is not None:
            try:
                from models.bot import Bot
                from services.live_safety import LiveSafetyService

                bot = db.query(Bot).filter(Bot.id == signal.bot_id).first()
                if bot:
                    LiveSafetyService().set_kill_switch(
                        db, bot, enabled=True,
                        reason=f"exchange_stop_place_failed:{placed.get('error')}",
                    )
                    db.flush()
            except Exception as exc:  # noqa: BLE001
                print(f"[EXCHANGE STOP] kill-switch failed: {type(exc).__name__}: {exc}")

        if alert and (failures == 1 or failures == halt_after or failures % 10 == 0):
            try:
                await alert(
                    "LIVE: СТОП НА БИРЖЕ НЕ ПОСТАВЛЕН",
                    (
                        f"Signal #{signal.id} · {signal.symbol} {signal.side}\n"
                        f"Стоп {want['trigger']} на {want['contracts']} контр. · попытка {failures}\n"
                        f"Ошибка: {placed.get('error')}\n\n"
                        f"Программный стоп {want['software_stop']} работает, пока робот жив; "
                        f"при рестарте позиция без защиты. Повтор каждый проход."
                        + (f"\nKill switch включён — новых входов нет." if failures >= halt_after else "")
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[EXCHANGE STOP] owner alert failed: {type(exc).__name__}: {exc}")

    # ── снятие ─────────────────────────────────────────────────────────────────
    async def cancel_all(self, db, signal, *, alert: Alert | None = None,
                         reason: str = "closed", route=None) -> dict:
        """Снять стопы символа после полного закрытия. Никогда не бросает.

        Оставшийся стоп закрытой позиции опасен: по reduce-only он ничего не
        откроет, но следующую сделку в ту же сторону по тому же символу закрыл
        бы по чужой цене. Поэтому снимаются все стопы символа (кроме записанных
        за другими открытыми сделками), а не только записанный, и неудача
        доходит до владельца.
        """
        try:
            mode = stops_mode(self.executor)
            if mode is None:
                return {"action": "inactive"}
            ops = self._ops(mode, signal)
            state = dict((signal.plan_json or {}).get(STATE_KEY) or {})
            if route is None:
                route = route_from_payload(signal.plan_json, signal.symbol, signal.side)
            if not self.executor._is_derivative(route.market_type):
                return {"action": "not_applicable"}
            want = {"symbol": route.exchange_symbol, "market_type": route.market_type}

            existing = ops.open_stop_orders(want["symbol"], want["market_type"])
            if existing is None:
                existing = [{"order_id": state["order_id"]}] if state.get("order_id") else []
            if mode == "live":
                foreign = _foreign_order_ids(db, signal)
                existing = [o for o in existing if o["order_id"] not in foreign]
            left = [o for o in existing
                    if not ops.cancel_stop_order(want["symbol"], o["order_id"], want["market_type"])]
            if not left:
                if state:
                    _save_state(signal, {**state, "order_id": None, "cancelled_at":
                                         datetime.now(timezone.utc).isoformat(),
                                         "cancel_reason": reason})
                return {"action": "cancelled", "count": len(existing)}

            _save_state(signal, {**state, "order_id": left[0]["order_id"], "cancel_failed": True})
            log_event(logger, logging.ERROR, "exchange_stop_cancel_failed", signal_id=signal.id,
                      symbol=want["symbol"], left=[o["order_id"] for o in left], reason=reason)
            if alert:
                try:
                    await alert(
                        "LIVE: СТОП НА БИРЖЕ НЕ СНЯТ",
                        (
                            f"Signal #{signal.id} · {signal.symbol} {signal.side} закрыт, "
                            f"но стоп-ордера на бирже остались: {', '.join(o['order_id'] for o in left)}\n"
                            f"Снимите их вручную: следующую сделку в ту же сторону такой стоп "
                            f"закроет по старой цене. Робот снимет их при следующем входе по символу."
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"[EXCHANGE STOP] owner alert failed: {type(exc).__name__}: {exc}")
            return {"action": "cancel_failed", "left": len(left)}
        except Exception as exc:  # noqa: BLE001
            log_event(logger, logging.ERROR, "exchange_stop_cancel_error",
                      signal_id=getattr(signal, "id", None), error=f"{type(exc).__name__}: {exc}")
            return {"action": "error", "error": f"{type(exc).__name__}: {exc}"}
