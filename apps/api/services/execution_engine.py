import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from core.config import settings
from services.exchange_factory import get_exchange_client
from services.trade_plan import TradePlanBuilder
from services.cost_engine import CostEngine
from services.telegram_router import TelegramRouter
from services.market_routing import from_payload as route_from_payload

from models.order import Order
from models.position import Position
from models.signal import Signal
from models.bot import Bot


class ExecutionEngine:
    def __init__(self, db=None, exchange: str | None = None):
        # (#okx-satellite-exchange-routing-2026-09-02) exchange=None -> текущая
        # активная биржа. При ведении УЖЕ ОТКРЫТОГО сигнала вызывающий код
        # (signal_lifecycle.py) обязан передать exchange=signal.exchange —
        # иначе реальное исполнение close/partial-close уйдёт не на ту биржу,
        # где эта позиция физически существует.
        self.db = db
        self.client = get_exchange_client(exchange)
        self.telegram = TelegramRouter()
        self.cost_engine = CostEngine(exchange=exchange)
        self.plan_builder = TradePlanBuilder(exchange=exchange)

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

        # (#htx-amount-precision-fix) Округляем количество по точности биржи
        # ПЕРЕД отправкой ордера, чтобы избежать InvalidOrder из-за неверной
        # точности количества (например, 0.06483416104070641 ETH вместо 0.06).
        qty = float(self.client.amount_to_precision(signal["symbol"], qty))

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

        # (#contract-quantize-2026-08-03) Округляем объём по МАРШРУТУ сделки, а
        # не по базовому символу. Прежняя строка звала amount_to_precision с
        # `signal.symbol` = "BTC/USDT", но ccxt при defaultType=swap резолвит
        # его в контрактный BTC/USDT:USDT, где точность = 1 контракт. Базовые
        # 0.002979 BTC округлялись в ноль → исключение → фолбэк возвращал объём
        # нетронутым. В логах это лилось как htx_amount_precision_fallback.
        #
        # Хуже шума было следствие: live отправлял 2 контракта (0.002 BTC), а
        # бумага книжила план 0.002979 BTC — на 49% больше. Расхождение не в
        # издержках, а в РАЗМЕРЕ позиции, то есть бумажный BTC несопоставим с
        # live по построению. Теперь обе ветки берут один и тот же объём.
        route = route_from_payload(signal.plan_json, signal.symbol, signal.side)
        order_qty, qty_meta = self._quantize_qty(route, float(plan.qty))
        if order_qty <= 0:
            return {
                "status": "rejected",
                "reason": "qty_below_one_contract",
                "order": None,
                "position": None,
                "plan": plan,
                "qty_meta": qty_meta,
            }

        # Ордер ОТПРАВЛЯЕМ ДО записи позиции. Порядок важен: в live отказ биржи
        # (кэп нотионала, недостаток маржи, отклонённый символ) не должен
        # оставлять систему с позицией, которой на бирже нет — иначе все
        # последующие reduceOnly-выходы уходят в пустоту, а PnL книжится по
        # несуществующей сделке.
        # Цена цели живёт в плане сделки; при рыночном входе её там нет, и
        # исполнитель получит None — то есть прежний путь.
        from services.entry_zone import limit_target_price as _limit_target

        _limit_price = _limit_target(signal.plan_json)
        # Лимитные аргументы добавляются, ТОЛЬКО когда лимит действительно есть:
        # при рыночном входе вызов остаётся прежним до последнего аргумента.
        _limit_kwargs = (
            {"limit_price": _limit_price,
             "ttl_sec": ((signal.plan_json or {}).get("entry_zone_plan") or {}).get("ttl_sec")}
            if _limit_price else {}
        )
        live = self._submit_live(open_side, signal.symbol, order_qty, entry_price,
                                 reduce_only=False, purpose="trend_open", route=route,
                                 **_limit_kwargs)

        # (#live-close-safety-2026-09-16) В live исключение внутри live-слоя —
        # тоже отказ: иначе бумага заводила позицию, которой на бирже нет, и все
        # её выходы потом уходили бы в пустоту.
        if live is None and self._live_mode():
            live = {"mode": "live", "ok": False, "status": "error", "error": "live_submit_exception"}

        # Частичный филл — позиция на бирже ЕСТЬ, пусть и меньше плана. Отказ с
        # ненулевым исполнением больше не бросает её без учёта.
        live_filled = float((live or {}).get("filled_qty") or 0.0)
        if (live is not None and live.get("mode") == "live" and not live.get("ok")
                and live_filled > 0):
            from core.logging import get_logger, log_event
            import logging as _logging

            log_event(get_logger(__name__), _logging.WARNING, "live_open_partial_fill",
                      signal_id=signal.id, symbol=signal.symbol, planned=order_qty,
                      filled=live_filled, status=live.get("status"), error=live.get("error"))
            live = {**live, "ok": True, "partial_fill": True}

        if live is not None and live.get("mode") == "live" and not live.get("ok"):
            await self._halt_on_live_divergence(
                signal=signal,
                stage="open",
                live=live,
            )
            return {
                "status": "live_rejected",
                "reason": live.get("error") or live.get("status"),
                "order": None,
                "position": None,
                "plan": plan,
                "live": live,
            }

        # Реальный филл важнее ожидаемой цены: в live средняя цена исполнения
        # отличается от entry_price, и без неё PnL расходится с биржей с первой
        # же сделки. В paper/dry_run поля пустые — остаётся ожидаемая цена.
        fill_price = entry_price
        fill_qty = order_qty
        if live is not None and live.get("mode") == "live" and live.get("ok"):
            if live.get("avg_price"):
                fill_price = float(live["avg_price"])
            if live.get("filled_qty"):
                fill_qty = float(live["filled_qty"])

        order = Order(
            bot_id=bot.id,
            signal_id=signal.id,
            symbol=signal.symbol,
            side=open_side,
            order_type="market",
            status="filled",
            qty=fill_qty,
            price=entry_price,
            filled_qty=fill_qty,
            avg_fill_price=fill_price,
            client_order_id=client_order_id,
            exchange_order_id=(live or {}).get("exchange_order_id"),
        )

        # Позиция несёт ТУ ЖЕ величину, что ушла на биржу: раньше Order.qty был
        # округлён под точность инструмента, а Position.qty оставался сырым, и
        # PnL считался по объёму, которого в сделке не было.
        position = Position(
            bot_id=bot.id,
            signal_id=signal.id,
            symbol=signal.symbol,
            side=signal.side,
            qty=fill_qty,
            entry_price=fill_price,
            mark_price=fill_price,
            unrealized_pnl=0.0,
            status="open",
        )

        self.db.add(order)
        self.db.add(position)
        self._mark_execution(signal, live, entry_liquidity="maker" if _limit_price else "taker")
        self.db.flush()

        return {
            "status": "opened",
            "order": order,
            "position": position,
            "plan": plan,
            "live": live,
        }

    async def _halt_on_live_divergence(self, *, signal: Signal, stage: str, live: dict) -> None:
        """Живой ордер отклонён — останавливаем робота и зовём владельца.

        Молча продолжать нельзя: состояние робота и биржи разошлись, а каждая
        следующая сделка увеличивает расхождение. Kill-switch останавливает
        новые входы, ведение открытых позиций продолжается.
        """
        from core.logging import get_logger, log_event
        import logging as _logging

        log_event(
            get_logger(__name__), _logging.ERROR, "live_order_rejected_halt",
            signal_id=signal.id, symbol=signal.symbol, stage=stage,
            status=live.get("status"), error=live.get("error"),
        )

        try:
            from services.live_safety import LiveSafetyService

            bot = self.db.query(Bot).filter(Bot.id == signal.bot_id).first()
            if bot:
                LiveSafetyService().set_kill_switch(
                    self.db, bot, enabled=True,
                    reason=f"live_order_rejected:{stage}:{live.get('error') or live.get('status')}",
                )
                self.db.flush()
        except Exception as exc:  # noqa: BLE001
            print(f"[LIVE HALT] kill-switch failed: {type(exc).__name__}: {exc}")

        try:
            await self.telegram.owner_alert(
                "LIVE ORDER REJECTED — РОБОТ ОСТАНОВЛЕН",
                (
                    f"Signal #{signal.id} · {signal.symbol} {signal.side}\n"
                    f"Этап: {stage}\n"
                    f"Статус: {live.get('status')}\n"
                    f"Ошибка: {live.get('error')}\n\n"
                    f"Ордер на биржу не прошёл. Позиция НЕ заведена, kill-switch включён.\n"
                    f"Проверьте баланс, LIVE_MAX_ORDER_NOTIONAL_PCT и права ключа."
                ),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[LIVE HALT] owner alert failed: {type(exc).__name__}: {exc}")

    async def partial_close_paper_position(
        self,
        signal: Signal,
        exit_price: float,
        share: float,
        reason: str = "tp1_partial",
    ) -> dict | None:
        """(#tp1-partial-2026-07-09) Частично закрывает paper-position (доля share
        по exit_price). Реализует «TP1 = частичная фиксация» ФАКТИЧЕСКИ: раньше на
        TP1 двигался только стоп, прибыль не реализовывалась. Возвращает realized
        net_pnl закрытой доли; позиция остаётся open с уменьшенным qty.
        """
        if self.db is None:
            raise RuntimeError("ExecutionEngine requires db session for paper execution")

        share = max(0.0, min(float(share), 1.0))
        if share <= 0:
            return None

        position = (
            self.db.query(Position)
            .filter(
                Position.signal_id == signal.id,
                Position.status == "open",
            )
            .first()
        )
        if not position:
            return None

        route = route_from_payload(signal.plan_json, position.symbol, position.side)
        # (#live-close-safety-2026-09-16) Доля округляется по ЛОТУ биржи того
        # рынка, где стоит позиция, — одинаково для бумаги и live. Прежде бралась
        # точность базового символа (спот), а биржа закрывала целыми лотами: у
        # CHIP (лот 100 монет) бумага закрывала 3250, биржа 3200, и финальное
        # закрытие оставляло на бирже хвост.
        close_qty, _ = self._quantize_qty(route, float(position.qty) * share)
        remaining_qty = round(float(position.qty) - close_qty, 10)
        if close_qty <= 0 or remaining_qty <= 0:
            # Слишком мелкая позиция для частичного закрытия (precision/min lot) —
            # ведём как раньше (только breakeven-стоп), без частичной фиксации.
            return None

        close_side = self._close_order_side(position.side)
        exit_fill = float(exit_price)
        live = None
        live_mode = self._live_mode()
        on_exchange = live_mode and self._opened_live(signal)
        if live_mode and not on_exchange:
            await self._book_only_close(signal=signal, position=position, stage="partial")
        if on_exchange:
            # В live сначала биржа, потом учёт: позиция в базе уменьшается только
            # на то, что биржа реально закрыла.
            live = self._submit_live(close_side, position.symbol, close_qty, exit_price,
                                     reduce_only=True, purpose=f"{reason}_close", route=route)
            filled = float((live or {}).get("filled_qty") or 0.0)
            if not (live or {}).get("ok"):
                if 0 < filled < close_qty:
                    close_qty = filled                  # закрылась часть — фиксируем её
                    remaining_qty = round(float(position.qty) - close_qty, 10)
                else:
                    await self._on_live_close_failed(signal=signal, stage="partial", live=live)
                    return {"status": "live_close_failed", "live": live, "reason": reason}
            if (live or {}).get("avg_price"):
                exit_fill = float(live["avg_price"])

        preview = self.cost_engine.estimate(
            symbol=position.symbol,
            market_type=route.market_type,
            side=position.side,
            entry_price=float(position.entry_price),
            exit_price=exit_fill,
            qty=close_qty,
            liquidity="taker",
            # Вход мог быть лимитным — тогда он стоил мейкерской ставки и не
            # платил спред. Выход всегда рыночный.
            entry_liquidity=((signal.plan_json or {}).get("execution") or {}).get("entry_liquidity"),
            holding_funding_periods=1 if route.market_type != "spot" else 0,
            leverage=route.leverage,
            # (#funding-settlements-2026-09-12) Закрываемая доля платит расчёты,
            # которые пересекла от открытия до этой минуты.
            opened_at=position.opened_at,
            closed_at=datetime.now(timezone.utc),
        )

        close_order = Order(
            bot_id=position.bot_id,
            signal_id=signal.id,
            symbol=position.symbol,
            side=close_side,
            order_type="market",
            status="filled",
            qty=close_qty,
            price=exit_price,
            filled_qty=close_qty,
            avg_fill_price=exit_fill,
            client_order_id=f"PAPER-PARTIAL-{uuid.uuid4()}",
            exchange_order_id=(live or {}).get("exchange_order_id"),
        )

        position.qty = remaining_qty
        position.mark_price = exit_fill

        self.db.add(close_order)
        self.db.flush()

        if not live_mode:
            # off/dry_run: бумага уже учла закрытие, ядро только логирует.
            live = self._submit_live(close_side, position.symbol, close_qty, exit_price,
                                     reduce_only=True, purpose=f"{reason}_close", route=route)
            if live is not None:
                close_order.exchange_order_id = live.get("exchange_order_id")
                self.db.flush()

        return {
            "status": "partial_closed",
            "position": position,
            "close_order": close_order,
            "closed_qty": close_qty,
            "remaining_qty": remaining_qty,
            "exit_price": exit_fill,
            "net_pnl": preview.net_pnl,
            "total_cost": preview.total_cost,
            "reason": reason,
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

        # Фандинг платят только держатели контракта: на споте его нет, и
        # закладывать буфер в стоимость спотовой сделки — завышать издержки.
        route = route_from_payload(signal.plan_json, position.symbol, position.side)
        close_side = self._close_order_side(position.side)
        exit_fill = float(exit_price)
        live = None
        live_mode = self._live_mode()
        on_exchange = live_mode and self._opened_live(signal)
        if live_mode and not on_exchange:
            await self._book_only_close(signal=signal, position=position, stage="close")

        if on_exchange:
            # (#live-close-safety-2026-09-16) В live сначала биржа, потом учёт.
            # Прежде позиция помечалась закрытой ДО ордера, а отказ биржи уходил
            # только в лог: на бирже оставалась позиция без стопа и без
            # сопровождения — для системы сделка была закрыта.
            exch_qty = self._exchange_position_base(route, position)
            if exch_qty is not None and exch_qty <= 0:
                # Биржа уже пуста (сработал стоп на бирже, закрыто вручную,
                # ликвидация) — ордер не нужен, но расхождение обязано дойти до
                # владельца.
                live = {"mode": "live", "ok": True, "status": "already_flat",
                        "filled_qty": 0.0, "avg_price": None}
                exchange_stop_fill = self._exchange_stop_exit(signal, position, exit_fill)
                if exchange_stop_fill is not None:
                    exit_fill = exchange_stop_fill
                await self._alert_live_already_flat(signal=signal, position=position,
                                                    exchange_stop_fill=exchange_stop_fill)
            else:
                # (#manual-orders-2026-09-16) Закрываем СВОЮ долю: не больше
                # учёта. Прежде уходил весь размер позиции на бирже — ради хвоста
                # лота после частичного закрытия, — но на том же символе, стороне
                # и режиме маржи может стоять ручная позиция владельца, и она
                # закрылась бы вместе с роботом. Хвоста больше нет: частичные
                # закрытия округляются по лоту биржи (3718a03). Меньше учёта на
                # бирже — закрываем то, что есть.
                book_qty = float(position.qty)
                send_qty = book_qty if exch_qty is None else min(book_qty, float(exch_qty))
                if exch_qty is not None and float(exch_qty) < book_qty * (1 - 1e-9):
                    from core.logging import get_logger, log_event
                    import logging as _logging

                    log_event(get_logger(__name__), _logging.WARNING, "live_close_exchange_below_book",
                              signal_id=signal.id, symbol=signal.symbol,
                              book_qty=book_qty, exchange_qty=float(exch_qty))
                live = self._submit_live(close_side, position.symbol, send_qty, exit_price,
                                         reduce_only=True, purpose="trend_close", route=route)
                if not (live or {}).get("ok"):
                    filled = float((live or {}).get("filled_qty") or 0.0)
                    if 0 < filled < float(position.qty):
                        # Закрылась часть — учёт уменьшаем ровно на неё, остаток
                        # остаётся открытым и закрывается следующим проходом.
                        position.qty = round(float(position.qty) - filled, 10)
                        self.db.flush()
                    await self._on_live_close_failed(signal=signal, stage="close", live=live)
                    return {"status": "live_close_failed", "live": live, "reason": reason}
                if live.get("avg_price"):
                    exit_fill = float(live["avg_price"])

        preview = self.cost_engine.estimate(
            symbol=position.symbol,
            market_type=route.market_type,
            side=position.side,
            entry_price=float(position.entry_price),
            exit_price=exit_fill,
            qty=float(position.qty),
            liquidity="taker",
            # Вход мог быть лимитным — тогда он стоил мейкерской ставки и не
            # платил спред. Выход всегда рыночный.
            entry_liquidity=((signal.plan_json or {}).get("execution") or {}).get("entry_liquidity"),
            holding_funding_periods=1 if route.market_type != "spot" else 0,
            leverage=route.leverage,
            # (#funding-settlements-2026-09-12) Остаток платит все расчёты от
            # открытия до закрытия.
            opened_at=position.opened_at,
            closed_at=datetime.now(timezone.utc),
        )

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
            avg_fill_price=exit_fill,
            client_order_id=client_order_id,
            exchange_order_id=(live or {}).get("exchange_order_id"),
        )

        position.status = "closed"
        position.mark_price = exit_fill
        # (#audit-positions) Закрытая позиция не имеет НЕреализованного PnL —
        # раньше поле держало net_pnl закрытия и путало фронт/аналитику.
        # Реализованный результат живёт в Signal.closed_net_pnl.
        position.unrealized_pnl = 0.0
        position.closed_at = datetime.now(timezone.utc)

        self.db.add(close_order)
        self.db.flush()

        # (#exchange-stop-2026-09-16) Позиция закрыта — стоп на бирже снимаем
        # после закрытия, а не до: выход не ждёт лишнего запроса. Режим решает
        # сервис: live — ордер, dry_run — запись в лог, off — ничего. Позиция,
        # которой робот на бирже не открывал, стопов робота на бирже не имеет.
        if on_exchange or not live_mode:
            await self._cancel_exchange_stops(signal, route, reason)

        if not live_mode:
            # off/dry_run: бумага уже учла закрытие, ядро только логирует.
            live = self._submit_live(close_side, position.symbol, position.qty, exit_price,
                                     reduce_only=True, purpose="trend_close", route=route)
            if live is not None:
                close_order.exchange_order_id = live.get("exchange_order_id")
                self.db.flush()

        return {
            "status": "closed",
            "position": position,
            "close_order": close_order,
            "exit_price": exit_fill,
            "net_pnl": preview.net_pnl,
            "net_pnl_pct": preview.net_pnl_pct,
            "total_cost": preview.total_cost,
            "reason": reason,
        }

    # ── live: закрытие и расхождения (#live-close-safety-2026-09-16) ─────────

    @staticmethod
    def _mark_execution(signal: Signal, live: dict | None, entry_liquidity: str = "taker") -> None:
        """(#manual-orders-2026-09-16) Где открыта позиция: на бирже (live) или
        только в учёте (paper/dry_run). Закрывать и страховать на бирже робот
        имеет право только то, что сам там открыл: позиция, открытая в paper и
        доживающая до включения live, закрывалась бы reduce-only ордером — а на
        том же символе может стоять ручная позиция владельца."""
        mode = "live" if (live or {}).get("mode") == "live" and (live or {}).get("ok") else (
            (live or {}).get("mode") or "paper")
        plan = dict(signal.plan_json or {})
        plan["execution"] = {
            "mode": mode,
            "exchange_order_id": (live or {}).get("exchange_order_id"),
            "opened_at": datetime.now(timezone.utc).isoformat(),
            # (#limit-entry-2026-09-19) Чем платили за вход. Настройка может
            # перевернуться между открытием и закрытием, а издержки закрытой
            # сделки обязаны отражать то, как она открывалась НА САМОМ ДЕЛЕ.
            "entry_liquidity": entry_liquidity,
        }
        signal.plan_json = plan
        try:
            from sqlalchemy.orm.attributes import flag_modified

            flag_modified(signal, "plan_json")
        except Exception:  # noqa: BLE001 — не ORM-объект (тесты)
            pass

    @staticmethod
    def _opened_live(signal: Signal) -> bool:
        return ((signal.plan_json or {}).get("execution") or {}).get("mode") == "live"

    async def _book_only_close(self, *, signal: Signal, position, stage: str) -> None:
        """В live закрывается позиция, которой робот на бирже не открывал: только
        учёт, без ордера. Владелец узнаёт один раз на сделку."""
        from core.logging import get_logger, log_event
        import logging as _logging

        log_event(get_logger(__name__), _logging.WARNING, "live_close_book_only",
                  signal_id=signal.id, symbol=signal.symbol, stage=stage,
                  book_qty=float(position.qty),
                  opened_as=((signal.plan_json or {}).get("execution") or {}).get("mode"))
        plan = dict(signal.plan_json or {})
        execution = dict(plan.get("execution") or {})
        if execution.get("book_only_alerted"):
            return
        execution["book_only_alerted"] = True
        plan["execution"] = execution
        signal.plan_json = plan
        try:
            from sqlalchemy.orm.attributes import flag_modified

            flag_modified(signal, "plan_json")
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.telegram.owner_alert(
                "LIVE: ПОЗИЦИЯ НЕ С БИРЖИ — ЗАКРЫТА ТОЛЬКО В УЧЁТЕ",
                (
                    f"Signal #{signal.id} · {signal.symbol} {signal.side}\n"
                    f"Позиция открыта до включения live (в paper/dry_run), на бирже её "
                    f"робот не открывал. Ордер на бирже не отправлен, чтобы не закрыть "
                    f"ручную позицию по тому же символу. Этап: {stage}."
                ),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[LIVE CLOSE] owner alert failed: {type(exc).__name__}: {exc}")

    @staticmethod
    def _live_mode() -> bool:
        """Ордера реально уходят на биржу. off и dry_run — прежний бумажный поток."""
        try:
            from services.live_executor import LIVE_EXECUTOR

            return bool(LIVE_EXECUTOR.is_live())
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _exchange_position_base(route, position) -> float | None:
        try:
            from services.live_executor import LIVE_EXECUTOR

            return LIVE_EXECUTOR.exchange_position_base(
                route.exchange_symbol, str(position.side).lower(), route.market_type,
                route.margin_mode,
            )
        except Exception:  # noqa: BLE001
            return None

    async def _on_live_close_failed(self, *, signal: Signal, stage: str, live: dict | None) -> None:
        """Биржа не закрыла позицию. Сделка остаётся открытой в учёте — следующий
        проход сопровождения повторит закрытие, пока условие выхода в силе, и
        позиция не останется без присмотра.

        Первый отказ по сделке: kill switch (новые входы стоят) и алерт. Дальше
        алерт на каждый десятый отказ — повтор идёт раз в ~10 с, и сообщение на
        каждую попытку заглушило бы остальные.
        """
        from core.logging import get_logger, log_event
        import logging as _logging

        live = live or {"ok": False, "status": "error", "error": "live_submit_exception"}
        plan = dict(signal.plan_json or {})
        failures = dict(plan.get("live_close_failures") or {})
        count = int(failures.get("count") or 0) + 1
        failures.update({
            "count": count,
            "stage": stage,
            "last_status": live.get("status"),
            "last_error": live.get("error"),
            "last_at": datetime.now(timezone.utc).isoformat(),
        })
        plan["live_close_failures"] = failures
        signal.plan_json = plan
        try:
            from sqlalchemy.orm.attributes import flag_modified

            flag_modified(signal, "plan_json")
        except Exception:  # noqa: BLE001 — не ORM-объект (тесты)
            pass

        log_event(
            get_logger(__name__), _logging.ERROR, "live_close_failed",
            signal_id=signal.id, symbol=signal.symbol, stage=stage, attempt=count,
            status=live.get("status"), error=live.get("error"),
            filled_qty=live.get("filled_qty"),
        )

        if count == 1:
            try:
                from services.live_safety import LiveSafetyService

                bot = self.db.query(Bot).filter(Bot.id == signal.bot_id).first()
                if bot:
                    LiveSafetyService().set_kill_switch(
                        self.db, bot, enabled=True,
                        reason=f"live_close_failed:{stage}:{live.get('error') or live.get('status')}",
                    )
                    self.db.flush()
            except Exception as exc:  # noqa: BLE001
                print(f"[LIVE CLOSE] kill-switch failed: {type(exc).__name__}: {exc}")

        if count == 1 or count % 10 == 0:
            try:
                await self.telegram.owner_alert(
                    "LIVE CLOSE FAILED — ПОЗИЦИЯ НА БИРЖЕ НЕ ЗАКРЫТА",
                    (
                        f"Signal #{signal.id} · {signal.symbol} {signal.side}\n"
                        f"Этап: {stage} · попытка {count}\n"
                        f"Статус: {live.get('status')}\n"
                        f"Ошибка: {live.get('error')}\n"
                        f"Исполнено: {live.get('filled_qty')}\n\n"
                        f"Сделка оставлена открытой, закрытие повторяется каждый проход "
                        f"сопровождения. Kill switch включён — новых входов нет.\n"
                        f"Проверьте позицию на бирже и права ключа."
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[LIVE CLOSE] owner alert failed: {type(exc).__name__}: {exc}")

    @staticmethod
    def _exchange_stop_exit(signal: Signal, position, exit_price: float) -> float | None:
        """Цена выхода, если позицию, по всей видимости, закрыл стоп на бирже.

        Биржевой стоп стоит дальше программного и срабатывает, когда робот не
        успел (рестарт, деплой, резкий ход). Цена, по которой робот увидел
        пустую позицию, уже за точкой срабатывания — книжить выход по ней
        значило бы занизить результат; ближе к правде цена срабатывания.
        """
        state = (signal.plan_json or {}).get("exchange_stop") or {}
        trigger = state.get("trigger")
        if not state.get("order_id") or not trigger:
            return None
        trigger = float(trigger)
        is_long = str(position.side).lower() in ("long", "buy")
        crossed = float(exit_price) <= trigger if is_long else float(exit_price) >= trigger
        return trigger if crossed else None

    async def _cancel_exchange_stops(self, signal: Signal, route, reason: str) -> None:
        try:
            from services.exchange_stop import ExchangeStopService

            await ExchangeStopService().cancel_all(
                self.db, signal, alert=self.telegram.owner_alert, reason=reason, route=route,
            )
        except Exception as exc:  # noqa: BLE001 — закрытие уже состоялось
            print(f"[EXCHANGE STOP] cancel skipped: {type(exc).__name__}: {exc}")

    async def _alert_live_already_flat(self, *, signal: Signal, position,
                                       exchange_stop_fill: float | None = None) -> None:
        """На бирже позиции уже нет, а в учёте она открыта: сработал стоп на
        бирже, закрыли вручную, ликвидировали или разошлись раньше. Учёт
        закрывается, владелец узнаёт."""
        from core.logging import get_logger, log_event
        import logging as _logging

        log_event(
            get_logger(__name__), _logging.WARNING, "live_position_already_flat",
            signal_id=signal.id, symbol=signal.symbol, side=signal.side,
            book_qty=float(position.qty), exchange_stop_fill=exchange_stop_fill,
        )
        cause = (
            f"Цена прошла стоп на бирже ({exchange_stop_fill}) — скорее всего, сработал он; "
            f"выход записан по цене срабатывания.\n"
            if exchange_stop_fill is not None else
            "Сделка закрыта в учёте без ордера.\n"
        )
        try:
            await self.telegram.owner_alert(
                "LIVE: ПОЗИЦИИ НА БИРЖЕ УЖЕ НЕТ",
                (
                    f"Signal #{signal.id} · {signal.symbol} {signal.side}\n"
                    f"В учёте открыто {float(position.qty)}, на бирже — пусто.\n"
                    f"{cause}Проверьте историю ордеров на бирже."
                ),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[LIVE CLOSE] owner alert failed: {type(exc).__name__}: {exc}")

    def _open_order_side(self, signal_side: str) -> str:
        return "buy" if signal_side == "long" else "sell"

    def _close_order_side(self, signal_side: str) -> str:
        return "sell" if signal_side == "long" else "buy"

    def _quantize_qty(self, route, qty: float) -> tuple[float, dict]:
        """Объём, который биржа реально примет, в базовой монете.

        (#contract-quantize-2026-08-03) Единая точка для бумаги и live: если
        округлять их порознь, они разъезжаются молча. Спот проходит через
        обычную точность, своп — через размер контракта.

        Ошибка live-слоя не должна ронять бумажный поток, поэтому при любом
        сбое возвращаем исходный объём: это прежнее поведение, а не новое.
        """
        meta: dict = {"source": "raw"}
        try:
            from services.live_executor import LIVE_EXECUTOR

            quantized, meta = LIVE_EXECUTOR.quantize_base(
                route.exchange_symbol, float(qty), route.market_type
            )
            meta = dict(meta)
            meta["source"] = "quantize_base"
            if quantized > 0:
                return float(quantized), meta
            # Ноль контрактов — сделка меньше минимального шага биржи. Это не
            # повод отправлять «что получится»: пусть отказ будет явным.
            return 0.0, meta
        except Exception as exc:  # noqa: BLE001
            meta = {"source": "raw", "error": f"{type(exc).__name__}: {exc}"}
            return float(qty), meta

    def _submit_live(self, side: str, symbol: str, qty: float, ref_price: float,
                     reduce_only: bool, purpose: str, route=None,
                     limit_price: float | None = None, ttl_sec: float | None = None) -> dict | None:
        """Маршрутизация ордера тренда через безопасное ядро LIVE_EXECUTOR.

        off → пропуск (чистая бумага, как сейчас). dry_run → логирует «что бы
        отправил» (валидация живой логики на бумаге). live → реальный ордер
        (идемпотентность/плечо/подтверждение филла). НИКОГДА не ломает бумажный
        поток: любая ошибка проглатывается и возвращает None.
        """
        try:
            from services.live_executor import LIVE_EXECUTOR
            if LIVE_EXECUTOR.effective_mode() == "off":
                return None
            # Символ и рынок берём из маршрута сделки: шорт уходит на
            # контрактный BTC/USDT:USDT, лонг — на спотовый BTC/USDT. Пока
            # спотовый символ отправлялся с market_type="swap", ccxt резолвил
            # его в СПОТОВЫЙ рынок, и своп-логика (плечо, reduceOnly, шорт)
            # применялась к площадке, которая её не поддерживает.
            route = route or route_from_payload(None, symbol, side)
            # (#limit-entry-2026-09-19) Вход лимитом, когда так решено и когда у
            # сделки есть цена цели. Выход всегда рыночный: стоп и защитные
            # ветки обязаны исполниться, а не ждать своей цены.
            if limit_price and not reduce_only:
                res = LIVE_EXECUTOR.place_limit(
                    route.exchange_symbol, side, float(qty),
                    market_type=route.market_type,
                    limit_price=float(limit_price),
                    ttl_sec=float(ttl_sec or getattr(settings, "ENTRY_ZONE_TTL_SEC", 45.0)),
                    reduce_only=False,
                    leverage=route.leverage,
                    margin_mode=route.margin_mode,
                    purpose=purpose,
                )
            else:
                res = LIVE_EXECUTOR.place_market(
                    route.exchange_symbol, side, float(qty),
                    market_type=route.market_type,
                    reduce_only=reduce_only, reference_price=float(ref_price),
                    leverage=route.leverage,
                    margin_mode=route.margin_mode,
                    purpose=purpose,
                )
            if not res.ok and LIVE_EXECUTOR.is_live():
                from core.logging import get_logger, log_event
                import logging as _logging

                log_event(
                    get_logger(__name__), _logging.ERROR, "live_order_not_filled",
                    symbol=symbol, side=side, qty=float(qty), purpose=purpose,
                    status=res.status, error=res.error,
                )
            return res.as_dict()
        except Exception as exc:  # noqa: BLE001 — бумага не должна падать из-за live-слоя
            print(f"[LIVE submit skip] {symbol} {side}: {type(exc).__name__}: {exc}")
            return None
