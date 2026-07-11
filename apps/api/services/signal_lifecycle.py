from models.signal import Signal
from sqlalchemy.orm.attributes import flag_modified
from services.market_data import MarketDataService
from services.signal_broadcaster import SignalBroadcaster
from services.telegram_router import TelegramRouter
from datetime import datetime, timezone
from services.execution_engine import ExecutionEngine
from services.decision_event_service import DecisionEventService
from services.exit_policy import ExitPolicyService
from services.ml_trade_logger import MLTradeLogger
# from services.trade_outcome_logger import TradeOutcomeLogger
from services.signal_freshness import SignalFreshnessService

from core.decision_codes import (
    DECISION_POSITION_OPENED,
    DECISION_POSITION_ALREADY_OPEN,
    DECISION_TRADE_PLAN_REJECTED,
    DECISION_TP1_REACHED,
    DECISION_TP2_REACHED,
    DECISION_STOP_LOSS,
    DECISION_BREAKEVEN_STOP,
    DECISION_SIGNAL_EXPIRED,
)

from core.config import settings

from models.bot import Bot
from models.position import Position
from services.orderbook_analyzer import OrderBookAnalyzer
from services.orderbook_feed import ORDERBOOK_STORE


def _depth_flow_against(signal, side: str) -> bool:
    """CVD из стакана развернулся против позиции → ускорить выход. Без движка
    или без свежих данных — False (поведение как раньше)."""
    if not bool(getattr(settings, "ENABLE_ORDERBOOK_ENGINE", False)):
        return False
    if not bool(getattr(settings, "OB_ACCELERATE_EXITS", True)):
        return False
    try:
        snap = ORDERBOOK_STORE.snapshot(signal.symbol)
        if snap and snap.get("age_sec", 1e9) > float(getattr(settings, "OB_DATA_MAX_AGE_SEC", 15.0)):
            snap = None
        sig = OrderBookAnalyzer.analyze(snap, levels=int(getattr(settings, "OB_DEPTH_LEVELS", 10)))
        # Греем LiquidityGuard спредом удерживаемой позиции (для подавления
        # софт-выходов на спайке спреда в exit_policy).
        try:
            from services.liquidity_guard import LIQUIDITY_GUARD
            if sig and sig.spread_pct is not None:
                LIQUIDITY_GUARD.observe_bps(signal.symbol, sig.spread_pct * 100.0)
        except Exception:
            pass
        return OrderBookAnalyzer.flow_against(
            side, sig,
            cvd_exit_ratio=float(getattr(settings, "OB_CVD_EXIT_RATIO", 0.6)),
            min_trades=int(getattr(settings, "OB_CVD_MIN_TRADES", 15)),
        )
    except Exception:
        return False


class SignalLifecycleManager:
    def __init__(self):
        self.market = MarketDataService()
        self.broadcast = SignalBroadcaster()
        self.router = TelegramRouter()
        self.decisions = DecisionEventService()
        self.exit_policy = ExitPolicyService()
        # self.outcome_logger = TradeOutcomeLogger()
        self.freshness = SignalFreshnessService()

    def _signal_age_sec(self, lifecycle: dict | None) -> float | None:
        if not isinstance(lifecycle, dict):
            return None
        first_seen_at = lifecycle.get("first_seen_at")
        if not first_seen_at:
            return None
        try:
            ts = datetime.fromisoformat(str(first_seen_at).replace("Z", "+00:00"))
            return max((datetime.now(timezone.utc) - ts).total_seconds(), 0.0)
        except Exception:
            return None

    def _get_open_position_for_signal(self, db, signal: Signal):
        return (
            db.query(Position)
            .filter(
                Position.signal_id == signal.id,
                Position.status == "open",
            )
            .order_by(Position.id.desc())
            .first()
        )

    def _get_latest_position_for_signal(self, db, signal: Signal):
        return (
            db.query(Position)
            .filter(Position.signal_id == signal.id)
            .order_by(Position.id.desc())
            .first()
        )


    def _fallback_closed_net_pnl(self, db, signal: Signal, exit_price: float, reason: str):
        plan = self._signal_plan_payload(signal) or {}

        if reason == "tp2_reached":
            value = (
                signal.net_pnl_tp2
                if signal.net_pnl_tp2 is not None
                else plan.get("net_pnl_tp2")
            )
            if value is not None:
                return value

        if reason == "stop_loss":
            value = (
                signal.net_pnl_stop
                if signal.net_pnl_stop is not None
                else plan.get("net_pnl_stop")
            )
            if value is not None:
                return value

        if reason == "breakeven_stop":
            return 0.0

        position = self._get_latest_position_for_signal(db, signal)

        if position:
            entry_price = float(position.entry_price)
            qty = float(position.qty)
            side = position.side.lower()

            if side == "long":
                return round((float(exit_price) - entry_price) * qty, 6)

            return round((entry_price - float(exit_price)) * qty, 6)

        return None        

    def _update_open_position_mark(self, db, signal: Signal, price: float):
        position = self._get_open_position_for_signal(db, signal)

        if not position:
            return

        entry_price = float(position.entry_price)
        qty = float(position.qty)
        side = position.side.lower()

        position.mark_price = round(price, 6)

        if side == "long":
            pnl = (price - entry_price) * qty
        else:
            pnl = (entry_price - price) * qty

        position.unrealized_pnl = round(pnl, 6)

        db.flush()

    def _signal_plan_payload(self, signal: Signal):
        plan = signal.plan_json or {}

        if not plan and signal.qty is None:
            return None

        return {
            "qty": signal.qty if signal.qty is not None else plan.get("qty"),
            "required_margin": signal.required_margin if signal.required_margin is not None else plan.get("required_margin"),
            "net_rr_tp1": signal.net_rr_tp1 if signal.net_rr_tp1 is not None else plan.get("net_rr_tp1"),
            "net_rr_tp2": signal.net_rr_tp2 if signal.net_rr_tp2 is not None else plan.get("net_rr_tp2"),
            "net_pnl_tp1": signal.net_pnl_tp1 if signal.net_pnl_tp1 is not None else plan.get("net_pnl_tp1"),
            "net_pnl_tp2": signal.net_pnl_tp2 if signal.net_pnl_tp2 is not None else plan.get("net_pnl_tp2"),
            "net_pnl_stop": signal.net_pnl_stop if signal.net_pnl_stop is not None else plan.get("net_pnl_stop"),
            "is_valid": plan.get("is_valid"),
            "reject_reason": plan.get("reject_reason"),
        }

    def _sync_open_position_with_signal_plan(self, db, signal: Signal, price: float):
        """
        Signal.plan_json / signal.qty — главный источник размера сделки.
        ExecutionEngine может пересчитать qty заново, поэтому после открытия
        синхронизируем открытую paper-position с сохранённым TradePlan.
        """
        position = self._get_open_position_for_signal(db, signal)

        if not position:
            return None

        if signal.qty is not None:
            position.qty = float(signal.qty)

        position.mark_price = round(float(price), 6)

        entry_price = float(position.entry_price)
        qty = float(position.qty)
        side = position.side.lower()

        if side == "long":
            pnl = (float(price) - entry_price) * qty
        else:
            pnl = (entry_price - float(price)) * qty

        position.unrealized_pnl = round(pnl, 6)

        db.flush()
        return position

    async def process_open_signals(self, db, bot):
        await self.expire_stale_signals(db, bot)

        signals = (
            db.query(Signal)
            .filter(
                Signal.bot_id == bot.id,
                Signal.status.in_(["published", "opened", "tp1", "breakeven"])
            )
            .order_by(Signal.id.asc())
            .all()
        )

        for signal in signals:
            await self.process_signal(db, bot, signal)

    async def process_signal(self, db, bot: Bot, signal: Signal):
        """Боевой путь: цена из живого снэпшота рынка."""
        snap = self.market.snapshot(signal.symbol)
        await self._process_signal_core(
            db, bot, signal,
            price=float(snap["last"]),
            price_source=snap.get("source"),
            dirty_decision="dirty_price_skipped",
            alert_dirty=True,
        )

    async def _process_signal_core(
        self,
        db,
        bot: Bot,
        signal: Signal,
        *,
        price: float,
        price_source: str | None,
        dirty_decision: str,
        alert_dirty: bool,
    ):
        """(#audit-lifecycle-merge) ЕДИНЫЙ путь ведения сигнала.

        Раньше process_signal и process_signal_with_price были ~450 строками
        дублированного кода и УЖЕ разошлись: guard low_grade_capital_release
        существовал только в тестовом пути. Теперь оба пути зовут этот core.
        """
        price = float(price)

        entry_from = float(signal.entry_zone_json["from"])
        entry_to = float(signal.entry_zone_json["to"])
        stop = float(signal.stop_price)

        tp1 = float(signal.tp_json["tp1"])
        tp2 = float(signal.tp_json["tp2"])

        side = signal.side.lower()

        sane, guard_reason = self._is_price_sane_for_signal(signal, price)

        terminal_stop_hit = False
        terminal_tp2_hit = False

        if signal.status in ["opened", "tp1", "breakeven"]:
            terminal_stop_hit = self._hit_stop(side, price, stop)
            terminal_tp2_hit = self._hit_tp(side, price, tp2)

        terminal_level_hit = terminal_stop_hit or terminal_tp2_hit

        if not sane and not terminal_level_hit:
            payload = {
                "signal_id": signal.id,
                "symbol": signal.symbol,
                "side": signal.side,
                "price": price,
                "reason": guard_reason,
                "entry_zone": signal.entry_zone_json,
                "stop_price": signal.stop_price,
                "tp": signal.tp_json,
            }
            if price_source is not None:
                payload["source"] = price_source

            self.decisions.record(
                db,
                symbol=signal.symbol,
                status="warning",
                decision=dirty_decision,
                action=signal.side,
                payload=payload,
            )

            if alert_dirty:
                await self.router.owner_alert(
                    "DIRTY PRICE SKIPPED",
                    (
                        f"Signal #{signal.id}\n"
                        f"{signal.symbol} {signal.side}\n"
                        f"Price: {price}\n"
                        f"Reason: {guard_reason}\n"
                        f"Source: {price_source}"
                    )
                )

            db.flush()
            return

        safe_metric_price = price

        if not sane and terminal_stop_hit:
            safe_metric_price = self._stop_exit_price(stop, side)

        if not sane and terminal_tp2_hit:
            safe_metric_price = self._tp_exit_price(tp2)

        self._update_open_position_mark(db, signal, safe_metric_price)

        if signal.status in ["opened", "tp1", "breakeven"]:
            self._update_lifecycle_metrics(db, signal, safe_metric_price)

        if signal.status == "published":
            if self._price_in_entry_zone(price, entry_from, entry_to):
                execution = ExecutionEngine(db)

                fresh = self.freshness.validate_signal(
                    symbol=signal.symbol,
                    side=side,
                    price=price,
                    entry_zone=signal.entry_zone_json,
                    stop_price=signal.stop_price,
                    tp=signal.tp_json,
                    expires_at=signal.expires_at,
                )

                if not fresh.allowed:
                    signal.status = "expired"
                    signal.closed_reason = fresh.reason

                    self.decisions.record(
                        db,
                        symbol=signal.symbol,
                        status="expired",
                        decision=f"signal_freshness_rejected_{fresh.reason}",
                        action=signal.side,
                        payload={
                            "signal_id": signal.id,
                            "freshness": fresh.payload,
                            "freshness_score": fresh.score,
                        },
                    )

                    db.flush()
                    return

                result = await execution.open_paper_position(
                    bot=bot,
                    signal=signal,
                    entry_price=price,
                    balance_usdt=float(settings.RISK_EQUITY_USDT),
                )

                plan = result.get("plan")

                if result.get("status") == "already_open":
                    signal.status = "rejected"
                    signal.closed_reason = "duplicate_position_already_open"

                    self.decisions.record(
                        db,
                        symbol=signal.symbol,
                        status="rejected",
                        decision=DECISION_POSITION_ALREADY_OPEN,
                        action=signal.side,
                        payload={
                            "signal_id": signal.id,
                            "symbol": signal.symbol,
                            "side": signal.side,
                            "entry_price": price,
                            "reason": "position_already_open_duplicate_signal_rejected",
                        },
                    )

                    await self.router.owner_alert(
                        "DUPLICATE SIGNAL REJECTED",
                        (
                            f"Signal #{signal.id}\n"
                            f"{signal.symbol} {signal.side}\n"
                            f"Причина: позиция по этой монете/стороне уже открыта."
                        ),
                    )

                    db.flush()
                    return

                if result.get("status") == "rejected":
                    signal.status = "rejected"

                    signal_plan = self._signal_plan_payload(signal)

                    if not signal_plan and plan:
                        signal_plan = {
                            "qty": getattr(plan, "qty", None),
                            "required_margin": getattr(plan, "required_margin", None),
                            "net_rr_tp1": getattr(plan, "net_rr_tp1", None),
                            "net_rr_tp2": getattr(plan, "net_rr_tp2", None),
                            "net_pnl_tp1": getattr(plan, "net_pnl_tp1", None),
                            "net_pnl_tp2": getattr(plan, "net_pnl_tp2", None),
                            "net_pnl_stop": getattr(plan, "net_pnl_stop", None),
                            "is_valid": getattr(plan, "is_valid", None),
                            "reject_reason": getattr(plan, "reject_reason", None),
                        }

                    extra = (
                        f"Сделка отклонена TradePlan.\n"
                        f"Причина: {result.get('reason')}"
                    )

                    if signal_plan:
                        extra += (
                            f"\nQty: {signal_plan.get('qty')}"
                            f"\nMargin: {signal_plan.get('required_margin')} USDT"
                            f"\nNet TP1: {signal_plan.get('net_pnl_tp1')} USDT"
                            f"\nNet TP2: {signal_plan.get('net_pnl_tp2')} USDT"
                            f"\nNet Stop: {signal_plan.get('net_pnl_stop')} USDT"
                            f"\nRR TP2: {signal_plan.get('net_rr_tp2')}"
                        )

                    await self.router.owner_alert(
                        "TRADE PLAN REJECTED",
                        f"Signal #{signal.id}\n{signal.symbol}\n{extra}",
                    )

                    self.decisions.record(
                        db,
                        symbol=signal.symbol,
                        status="rejected",
                        decision=DECISION_TRADE_PLAN_REJECTED,
                        action=signal.side,
                        payload={
                            "signal_id": signal.id,
                            "symbol": signal.symbol,
                            "side": signal.side,
                            "entry_price": price,
                            "reason": result.get("reason"),
                            "plan": signal_plan,
                        },
                    )

                    db.flush()
                    return

                signal.status = "opened"
                signal.opened_at = datetime.now(timezone.utc)

                synced_position = self._sync_open_position_with_signal_plan(
                    db=db,
                    signal=signal,
                    price=price,
                )

                lifecycle = self._update_lifecycle_metrics(db, signal, price)

                signal_plan = self._signal_plan_payload(signal)

                self.decisions.record(
                    db,
                    symbol=signal.symbol,
                    status="opened",
                    decision=DECISION_POSITION_OPENED,
                    action=signal.side,
                    payload={
                        "signal_id": signal.id,
                        "symbol": signal.symbol,
                        "side": signal.side,
                        "entry_price": price,
                        "entry_zone": signal.entry_zone_json,
                        "stop_price": signal.stop_price,
                        "tp": signal.tp_json,
                        "grade": signal.grade,
                        "confidence": signal.confidence,
                        "plan": signal_plan,
                        "lifecycle": lifecycle,
                        "position": {
                            "id": synced_position.id,
                            "qty": synced_position.qty,
                            "entry_price": synced_position.entry_price,
                            "mark_price": synced_position.mark_price,
                            "unrealized_pnl": synced_position.unrealized_pnl,
                        } if synced_position else None,
                    },
                )

                extra = f"Цена входа: {round(price, 4)}"

                if signal_plan:
                    extra += (
                        f"\nQty: {signal_plan.get('qty')}"
                        f"\nMargin: {signal_plan.get('required_margin')} USDT"
                        f"\nNet TP1: {signal_plan.get('net_pnl_tp1')} USDT"
                        f"\nNet TP2: {signal_plan.get('net_pnl_tp2')} USDT"
                        f"\nNet Stop: {signal_plan.get('net_pnl_stop')} USDT"
                        f"\nRR TP2: {signal_plan.get('net_rr_tp2')}"
                    )

                # Idempotency guard: avoid repeated "position activated" updates
                # when lifecycle processing is retried/concurrent.
                signal.plan_json = signal.plan_json or {}
                lifecycle_state = signal.plan_json.get("lifecycle") or {}
                entry_notified = bool(lifecycle_state.get("entry_notified"))

                if not entry_notified:
                    await self.router.publish_signal_update(
                        symbol=signal.symbol,
                        text_status=f"📥 Позиция активирована | Signal #{signal.id}",
                        extra=extra,
                        grade=signal.grade,
                    )
                    lifecycle_state["entry_notified"] = True
                    signal.plan_json["lifecycle"] = lifecycle_state
                    flag_modified(signal, "plan_json")

                db.flush()

        elif signal.status == "opened":
            entry_price_for_result = self._entry_for_result(db, signal, entry_from)

            if self._hit_stop(side, price, stop):
                exit_price = self._stop_exit_price(stop, side)

                await self._close_signal(
                    db,
                    signal,
                    exit_price=exit_price,
                    fallback_result_pct=self._result_pct(
                        side,
                        entry_price_for_result,
                        exit_price,
                    ),
                    reason="stop_loss",
                )
                return

            lifecycle = (signal.plan_json or {}).get("lifecycle") or {}
            entry_price = self._get_signal_entry_price(db, signal) or entry_from

            # Guard C-грейда: слабый сетап без MFE удерживает капитал — release.
            # (до merge существовал только в тестовом пути — дивергенция путей)
            current_pct_for_grade_guard = self._result_pct_precise(
                side,
                float(entry_price),
                float(safe_metric_price),
            )

            mfe_for_grade_guard = lifecycle.get("mfe_pct")

            if (
                str(signal.grade or "").upper() == "C"
                and mfe_for_grade_guard is not None
                and float(mfe_for_grade_guard) < 0.30
                and current_pct_for_grade_guard <= -0.05
            ):
                await self._close_signal(
                    db,
                    signal,
                    exit_price=float(safe_metric_price),
                    fallback_result_pct=current_pct_for_grade_guard,
                    reason="low_grade_capital_release",
                )
                return

            exit_decision = self.exit_policy.before_tp1_decision(
                side=side,
                entry_price=float(entry_price),
                current_price=float(safe_metric_price),
                stop_price=float(stop),
                tp1_price=float(tp1),
                mfe_pct=lifecycle.get("mfe_pct"),
                max_profit_price=lifecycle.get("max_profit_price"),
                symbol=signal.symbol,
                market_type=settings.execution_market_type,
                signal_age_sec=self._signal_age_sec(lifecycle),
                trade_mode=(signal.plan_json or {}).get("trade_mode", "trend"),
                flow_against=_depth_flow_against(signal, side),
                # (#range-time-stop-2026-07-09) range-режим получает свой таймер.
                regime=(signal.plan_json or {}).get("regime"),
            )

            if exit_decision.exit:
                await self._close_signal(
                    db,
                    signal,
                    exit_price=exit_decision.exit_price,
                    fallback_result_pct=self._result_pct(
                        side,
                        float(entry_price),
                        exit_decision.exit_price,
                    ),
                    reason=exit_decision.reason,
                )
                return

            if self._hit_tp(side, price, tp1):
                signal.status = "tp1"

                # (#tp1-partial-2026-07-09) РЕАЛЬНАЯ частичная фиксация: закрываем
                # долю позиции по цене TP1. Раньше здесь двигался только стоп —
                # «частичная фиксация» была фикцией, и откат TP1→BE приносил ~0.
                partial_result = None
                if bool(getattr(settings, "TP1_PARTIAL_ENABLED", True)):
                    try:
                        partial_result = await ExecutionEngine(db).partial_close_paper_position(
                            signal=signal,
                            exit_price=self._tp_exit_price(tp1),
                            share=float(getattr(settings, "TP1_PARTIAL_CLOSE_SHARE", 0.5)),
                            reason="tp1_partial",
                        )
                    except Exception as exc:  # noqa: BLE001 — фиксация не должна ронять ведение
                        partial_result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
                    if partial_result and partial_result.get("status") == "partial_closed":
                        plan = dict(signal.plan_json or {})
                        plan["tp1_partial"] = {
                            "closed_qty": partial_result.get("closed_qty"),
                            "remaining_qty": partial_result.get("remaining_qty"),
                            "exit_price": self._tp_exit_price(tp1),
                            "net_pnl": partial_result.get("net_pnl"),
                            "total_cost": partial_result.get("total_cost"),
                            "at": datetime.now(timezone.utc).isoformat(),
                        }
                        signal.plan_json = plan
                        flag_modified(signal, "plan_json")

                # BREAKEVEN STOP: после TP1 стоп сдвигается на entry + fee buffer.
                # Гарантирует нулевой убыток если цена откатится после TP1.
                _be_position = self._get_open_position_for_signal(db, signal)
                _be_entry = float(_be_position.entry_price) if _be_position else float((entry_from + entry_to) / 2)
                # (#audit-cost-model) fee-буфер безубытка — по ставке рынка ИСПОЛНЕНИЯ
                # (swap 0.05%, спот 0.2%), а не жёстко SPOT_TAKER_FEE.
                _be_rate, _ = self.exit_policy._fee_rate(signal.symbol, settings.execution_market_type)
                _be_fee = float(_be_rate) * 2 + float(getattr(settings, "SLIPPAGE_BUFFER_PCT", 0.0002))
                if side == "long":
                    _be_new_stop = round(_be_entry * (1 + _be_fee), 8)
                    if _be_new_stop > float(signal.stop_price):
                        signal.stop_price = _be_new_stop
                else:
                    _be_new_stop = round(_be_entry * (1 - _be_fee), 8)
                    if _be_new_stop < float(signal.stop_price):
                        signal.stop_price = _be_new_stop

                self.decisions.record(
                    db,
                    symbol=signal.symbol,
                    status="tp1",
                    decision=DECISION_TP1_REACHED,
                    action=signal.side,
                    payload={
                        "signal_id": signal.id,
                        "symbol": signal.symbol,
                        "side": signal.side,
                        "price": price,
                        "tp1": tp1,
                        "entry_zone": signal.entry_zone_json,
                        "stop_moved_to": "breakeven_after_tp1",
                        "breakeven_stop": float(signal.stop_price),
                        "tp1_partial": (signal.plan_json or {}).get("tp1_partial"),
                        "lifecycle": signal.plan_json.get("lifecycle") if signal.plan_json else None,
                    },
                )

                _partial_note = ""
                _pp = (signal.plan_json or {}).get("tp1_partial")
                if _pp and _pp.get("net_pnl") is not None:
                    _partial_note = (
                        f" Зафиксировано {_pp.get('closed_qty')} @ {_pp.get('exit_price')} "
                        f"(net {_pp.get('net_pnl')} USDT)."
                    )
                await self.router.publish_signal_update(
                    symbol=signal.symbol,
                    text_status=f"✅ TP1 достигнут | Signal #{signal.id}",
                    extra=(
                        f"TP1 достигнут.{_partial_note} Стоп перенесён на breakeven "
                        f"{round(float(signal.stop_price), 6)}. Позиция защищена."
                    ),
                    grade=signal.grade,
                )

                db.flush()

        elif signal.status in ["tp1", "breakeven"]:
            position = self._get_open_position_for_signal(db, signal)
            entry_price = float(position.entry_price) if position else entry_from
            lifecycle = (signal.plan_json or {}).get("lifecycle") or {}

            # ХАРД-СТОП после TP1: breakeven/стоп закрывает позицию явно,
            # цена закрытия = уровень стопа (SOL #96).
            if self._hit_stop(side, price, stop):
                exit_price = self._stop_exit_price(stop, side)
                await self._close_signal(
                    db,
                    signal,
                    exit_price=exit_price,
                    fallback_result_pct=self._result_pct(side, entry_price, exit_price),
                    reason="breakeven_stop",
                )
                return

            if self._hit_tp(side, price, tp2):
                exit_price = self._tp_exit_price(tp2)

                await self._close_signal(
                    db,
                    signal,
                    exit_price=exit_price,
                    fallback_result_pct=self._result_pct(
                        side,
                        entry_price,
                        exit_price,
                    ),
                    reason="tp2_reached",
                )
                return

            exit_decision = self.exit_policy.after_tp1_decision(
                side=side,
                entry_price=entry_price,
                current_price=price,
                tp2_price=tp2,
                lifecycle=lifecycle,
                symbol=signal.symbol,
                market_type=settings.execution_market_type,
                signal_age_sec=self._signal_age_sec(lifecycle),
            )

            if exit_decision.exit:
                await self._close_signal(
                    db,
                    signal,
                    exit_price=exit_decision.exit_price,
                    fallback_result_pct=self._result_pct(
                        side,
                        entry_price,
                        exit_decision.exit_price,
                    ),
                    reason=exit_decision.reason,
                )
                return


    def _safe_float(self, value, default=None):
        try:
            if value is None:
                return default
            return float(value)
        except Exception:
            return default

    def _price_guard_bounds(self, signal: Signal):
        """
        Защита от грязных тиков / чужих цен / API-аномалий.

        Для long/short цена не должна улетать слишком далеко от торгового плана.
        Если улетела — lifecycle не закрывает сделку по этой цене, а пропускает тик.
        """
        entry_zone = signal.entry_zone_json or {}
        tp = signal.tp_json or {}

        entry_from = self._safe_float(entry_zone.get("from"))
        entry_to = self._safe_float(entry_zone.get("to"))
        stop = self._safe_float(signal.stop_price)
        tp1 = self._safe_float(tp.get("tp1"))
        tp2 = self._safe_float(tp.get("tp2"))

        values = [
            v for v in [entry_from, entry_to, stop, tp1, tp2]
            if v is not None and v > 0
        ]

        if not values:
            return None, None

        base_min = min(values)
        base_max = max(values)

        # Буфер 12% от диапазона плана, но минимум 3% от средней цены.
        mid = (base_min + base_max) / 2
        plan_range = max(base_max - base_min, mid * 0.03)
        buffer = max(plan_range * 1.2, mid * 0.03)

        min_allowed = max(0.00000001, base_min - buffer)
        max_allowed = base_max + buffer

        return min_allowed, max_allowed

    def _is_price_sane_for_signal(self, signal: Signal, price: float) -> tuple[bool, str | None]:
        if price is None or price <= 0:
            return False, "price_empty_or_non_positive"

        min_allowed, max_allowed = self._price_guard_bounds(signal)

        if min_allowed is None or max_allowed is None:
            return True, None

        if price < min_allowed:
            return False, f"price_below_guard: {price} < {min_allowed}"

        if price > max_allowed:
            return False, f"price_above_guard: {price} > {max_allowed}"

        return True, None

    def _price_in_entry_zone(self, price: float, entry_from: float, entry_to: float) -> bool:
        low = min(entry_from, entry_to)
        high = max(entry_from, entry_to)
        return low <= price <= high

    def _hit_tp(self, side: str, price: float, tp: float) -> bool:
        if side == "long":
            return price >= tp
        return price <= tp

    def _hit_stop(self, side: str, price: float, stop: float) -> bool:
        if side == "long":
            return price <= stop
        return price >= stop

    def _result_pct(self, side: str, entry: float, exit_price: float) -> float:
        if side == "long":
            return round(((exit_price - entry) / entry) * 100, 2)
        return round(((entry - exit_price) / entry) * 100, 2)

    def _stop_exit_price(self, stop: float, side: str | None = None) -> float:
        """
        В paper/backtest режиме стоп исполняется по уровню стопа,
        а не по последней цене, которая могла сильно перелететь стоп.

        (#paper-slippage-2026-07-09) Плюс адверс-слиппедж: стоп — маркет-ордер,
        на live филл ХУЖЕ уровня. long → ниже стопа, short → выше. Без side —
        прежнее поведение (точный уровень).
        """
        stop = float(stop)
        slip_pct = float(getattr(settings, "PAPER_STOP_ADVERSE_SLIPPAGE_PCT", 0.0)) / 100.0
        if slip_pct <= 0 or not side:
            return stop
        if str(side).lower() == "long":
            return stop * (1.0 - slip_pct)
        return stop * (1.0 + slip_pct)

    def _tp_exit_price(self, tp: float) -> float:
        """
        В paper/backtest режиме TP исполняется по целевому уровню.
        """
        return float(tp)

    def _entry_for_result(self, db, signal: Signal, fallback_entry: float) -> float:
        """
        Для расчёта результата используем реальную цену входа позиции,
        а не entry_from из зоны входа.
        """
        entry_price = self._get_signal_entry_price(db, signal)
        return float(entry_price) if entry_price is not None else float(fallback_entry)

    def _result_pct_precise(self, side: str, entry: float, price: float) -> float:
        if not entry:
            return 0.0

        if side == "long":
            return round(((float(price) - float(entry)) / float(entry)) * 100, 4)

        return round(((float(entry) - float(price)) / float(entry)) * 100, 4)

    def _get_signal_entry_price(self, db, signal: Signal):
        position = self._get_latest_position_for_signal(db, signal)

        if position and position.entry_price is not None:
            return float(position.entry_price)

        entry_zone = signal.entry_zone_json or {}
        entry_from = entry_zone.get("from")
        entry_to = entry_zone.get("to")

        if entry_from is not None and entry_to is not None:
            return float((float(entry_from) + float(entry_to)) / 2)

        return None

    def _update_lifecycle_metrics(self, db, signal: Signal, price: float):
        """
        Сохраняет MFE/MAE по сигналу без новой миграции.

        MFE — maximum favorable excursion:
        максимальный плюс, который давал сигнал после открытия.

        MAE — maximum adverse excursion:
        максимальная просадка после открытия.
        """

        if signal.status not in ["opened", "tp1", "breakeven"]:
            return None

        entry_price = self._get_signal_entry_price(db, signal)

        if entry_price is None:
            return None

        side = signal.side.lower()
        current_pct = self._result_pct_precise(side, entry_price, price)

        plan = dict(signal.plan_json or {})
        lifecycle = dict(plan.get("lifecycle") or {})

        prev_mfe = lifecycle.get("mfe_pct")
        prev_mae = lifecycle.get("mae_pct")

        if prev_mfe is None or current_pct > float(prev_mfe):
            lifecycle["mfe_pct"] = current_pct
            lifecycle["max_profit_price"] = round(float(price), 6)

        if prev_mae is None or current_pct < float(prev_mae):
            lifecycle["mae_pct"] = current_pct
            lifecycle["max_drawdown_price"] = round(float(price), 6)

        # (#audit-traj) Компактная траектория для offline exit-replay: точки
        # [age_sec, pct] с даунсемплингом по шагу и адаптивным прореживанием.
        if bool(getattr(settings, "TRAJ_RECORD_ENABLED", True)):
            try:
                traj = list(lifecycle.get("traj") or [])
                step = float(lifecycle.get("traj_step") or getattr(settings, "TRAJ_MIN_STEP_PCT", 0.05))
                first_seen = lifecycle.get("first_seen_at")
                if first_seen:
                    _t0 = datetime.fromisoformat(str(first_seen).replace("Z", "+00:00"))
                    age = int(max((datetime.now(timezone.utc) - _t0).total_seconds(), 0))
                else:
                    age = 0
                last_pct = float(traj[-1][1]) if traj else None
                if last_pct is None or abs(current_pct - last_pct) >= step:
                    traj.append([age, round(current_pct, 4)])
                    max_pts = int(getattr(settings, "TRAJ_MAX_POINTS", 400))
                    if len(traj) > max_pts:
                        traj = traj[::2]
                        step = step * 2.0
                    lifecycle["traj"] = traj
                    lifecycle["traj_step"] = round(step, 4)
            except Exception:
                pass  # телеметрия не должна ломать ведение сделки

        lifecycle["entry_price"] = round(float(entry_price), 6)
        lifecycle["last_price"] = round(float(price), 6)
        lifecycle["current_pct"] = current_pct
        lifecycle["went_positive"] = bool(lifecycle.get("went_positive") or current_pct > 0)
        lifecycle["updates"] = int(lifecycle.get("updates") or 0) + 1
        lifecycle["last_seen_at"] = datetime.now(timezone.utc).isoformat()

        if not lifecycle.get("first_seen_at"):
            lifecycle["first_seen_at"] = datetime.now(timezone.utc).isoformat()

        plan["lifecycle"] = lifecycle
        signal.plan_json = plan
        flag_modified(signal, "plan_json")

        db.flush()

        return lifecycle

    def _finalize_lifecycle_metrics(
        self,
        db,
        signal: Signal,
        exit_price: float,
        result_pct: float,
        reason: str,
    ):
        plan = dict(signal.plan_json or {})
        lifecycle = dict(plan.get("lifecycle") or {})

        mfe_pct = lifecycle.get("mfe_pct")
        mae_pct = lifecycle.get("mae_pct")

        missed_profit_pct = None

        if mfe_pct is not None and result_pct is not None:
            missed_profit_pct = round(max(float(mfe_pct) - float(result_pct), 0.0), 4)

        lifecycle["closed_at"] = datetime.now(timezone.utc).isoformat()
        lifecycle["exit_price"] = round(float(exit_price), 6)
        lifecycle["final_result_pct"] = result_pct
        lifecycle["close_reason"] = reason
        lifecycle["missed_profit_pct"] = missed_profit_pct
        lifecycle["positive_then_negative"] = bool(
            lifecycle.get("went_positive")
            and result_pct is not None
            and float(result_pct) < 0
        )

        plan["lifecycle"] = lifecycle
        signal.plan_json = plan
        flag_modified(signal, "plan_json")

        db.flush()

        return lifecycle        

    async def _close_signal(self, db, signal: Signal, exit_price: float, fallback_result_pct: float, reason: str):
        if reason == "stop_loss" and signal.stop_price is not None:
            # (#paper-slippage-2026-07-09) Стоп закрывается по уровню С адверс-
            # слиппеджем (маркет-филл на live хуже уровня), а не по last price,
            # который мог перелететь стоп ещё дальше.
            exit_price = self._stop_exit_price(float(signal.stop_price), signal.side)

        execution = ExecutionEngine(db)

        close_result = await execution.close_paper_position(
            signal=signal,
            exit_price=exit_price,
            reason=reason,
        )

        if close_result:
            result_pct = close_result["net_pnl_pct"]
            net_pnl = close_result["net_pnl"]
            total_cost = close_result["total_cost"]
        else:
            result_pct = fallback_result_pct

            plan = signal.plan_json or {}

            if reason == "tp2_reached":
                net_pnl = signal.net_pnl_tp2 if signal.net_pnl_tp2 is not None else plan.get("net_pnl_tp2")
            elif reason == "stop_loss":
                net_pnl = signal.net_pnl_stop if signal.net_pnl_stop is not None else plan.get("net_pnl_stop")
            elif reason == "breakeven_stop":
                net_pnl = 0.0
            else:
                net_pnl = None

            total_cost = None

        # (#tp1-partial-2026-07-09) Итог сделки = закрытие остатка + реализованная
        # на TP1 часть. Без этого частичная фиксация терялась в отчётности.
        tp1_partial = (signal.plan_json or {}).get("tp1_partial") or {}
        partial_net = tp1_partial.get("net_pnl")
        if partial_net is not None:
            net_pnl = round(float(net_pnl or 0.0) + float(partial_net), 6)
            partial_cost = tp1_partial.get("total_cost")
            if partial_cost is not None:
                total_cost = round(float(total_cost or 0.0) + float(partial_cost), 6)
            # Консистентность фронта: result_pct закрытия считался по ОСТАТКУ
            # позиции и противоречил бы суммарному net (Result% мал, Net велик).
            # Пересчитываем % от исходной маржи сделки — та же семантика, что
            # net_pnl_pct у CostEngine (net / base_margin).
            try:
                base_margin = float(signal.required_margin or (signal.plan_json or {}).get("required_margin") or 0.0)
                if base_margin > 0 and net_pnl is not None:
                    result_pct = round(float(net_pnl) / base_margin * 100.0, 4)
            except Exception:
                pass

        signal.status = "closed"
        signal.result_pct = result_pct
        signal.closed_at = datetime.now(timezone.utc)

        signal.closed_exit_price = exit_price
        signal.closed_net_pnl = net_pnl
        signal.closed_total_cost = total_cost
        signal.closed_reason = reason

        lifecycle = self._finalize_lifecycle_metrics(
            db=db,
            signal=signal,
            exit_price=exit_price,
            result_pct=result_pct,
            reason=reason,
        )

        decision_map = {
            "stop_loss": DECISION_STOP_LOSS,
            "breakeven_stop": DECISION_BREAKEVEN_STOP,
            "tp2_reached": DECISION_TP2_REACHED,
            "protective_trailing_stop": "protective_trailing_stop",
            "adaptive_trailing_stop": "adaptive_trailing_stop",
            "trend_trailing_stop": "trend_trailing_stop",
            "adaptive_post_tp1_stop": "adaptive_post_tp1_stop",
            "failed_setup_exit": "failed_setup_exit",
            "low_grade_capital_release": "low_grade_capital_release",
        }

        decision = decision_map.get(reason, reason)

        self.decisions.record(
            db,
            symbol=signal.symbol,
            status="closed",
            decision=decision,
            action=signal.side,
            payload={
                "signal_id": signal.id,
                "symbol": signal.symbol,
                "side": signal.side,
                "exit_price": exit_price,
                "result_pct": result_pct,
                "net_pnl": net_pnl,
                "total_cost": total_cost,
                "reason": reason,
                "grade": signal.grade,
                "lifecycle": lifecycle,
            },
        )

        try:
            ml_log_result = MLTradeLogger().log_closed_signal(signal)
        except Exception as e:
            ml_log_result = {
                "status": "error",
                "error": f"{type(e).__name__}: {repr(e)}",
            }

        if ml_log_result.get("status") not in ["logged", "skipped"]:
            self.decisions.record(
                db,
                symbol=signal.symbol,
                status="warning",
                decision="ml_trade_log_failed",
                action=signal.side,
                payload={
                    "signal_id": signal.id,
                    "symbol": signal.symbol,
                    "side": signal.side,
                    "error": ml_log_result.get("error"),
                    "ml_log_result": ml_log_result,
                },
            )

        emoji = "✅" if result_pct >= 0 else "🛑"

        extra = f"Результат: {result_pct}%\nПричина: {reason}"

        if net_pnl is not None:
            extra += f"\nNet PnL: {net_pnl} USDT"
            # (#conv-pnl-rescale-2026-07-11) Разбивка: сколько дал TP1, сколько остаток —
            # иначе «TP1 дал 0.69, а закрылись на 0.61» выглядит противоречием.
            if partial_net is not None:
                _rest = round(float(net_pnl) - float(partial_net), 6)
                extra += f"\n· фиксация на TP1: {partial_net} USDT · остаток: {_rest} USDT"

        if total_cost is not None:
            extra += f"\nCosts: {total_cost} USDT"

        if lifecycle:
            extra += (
                f"\nMFE: {lifecycle.get('mfe_pct')}%"
                f"\nMAE: {lifecycle.get('mae_pct')}%"
                f"\nMissed profit: {lifecycle.get('missed_profit_pct')}%"
            )

            if lifecycle.get("positive_then_negative"):
                extra += "\n⚠️ Цена была в плюсе, но сделка закрылась в минус."

        await self.router.publish_signal_update(
            symbol=signal.symbol,
            text_status=f"{emoji} Позиция закрыта | Signal #{signal.id}",
            extra=extra,
            grade=signal.grade,
        )

        db.flush()

    async def process_signal_with_price(self, db, signal: Signal, price: float):
        """Тест/инъекция цены (owner endpoint): тот же core, без dirty-алертов."""
        bot = db.query(Bot).filter(Bot.id == signal.bot_id).first()

        if not bot:
            raise RuntimeError(f"Bot not found for signal_id={signal.id}")

        await self._process_signal_core(
            db, bot, signal,
            price=float(price),
            price_source=None,
            dirty_decision="dirty_test_price_skipped",
            alert_dirty=False,
        )


    async def expire_stale_signals(self, db, bot):
        now = datetime.now(timezone.utc)

        stale_signals = (
            db.query(Signal)
            .filter(
                Signal.bot_id == bot.id,
                Signal.status == "published",
                Signal.expires_at != None,
                Signal.expires_at < now
            )
            .all()
        )

        for signal in stale_signals:
            signal.status = "expired"

            self.decisions.record(
                db,
                symbol=signal.symbol,
                status="expired",
                decision=DECISION_SIGNAL_EXPIRED,
                action=signal.side,
                payload={
                    "signal_id": signal.id,
                    "symbol": signal.symbol,
                    "side": signal.side,
                    "entry_zone": signal.entry_zone_json,
                    "stop_price": signal.stop_price,
                    "tp": signal.tp_json,
                    "grade": signal.grade,
                    "expires_at": str(signal.expires_at),
                    "reason": "entry_zone_not_reached_before_expiry",
                },
            )

            await self.router.publish_signal_update(
                symbol=signal.symbol,
                text_status=f"⌛ Сигнал отменён | Signal #{signal.id}",
                extra="Цена не активировала зону входа в заданное время.",
                grade=signal.grade
            )

            db.flush()