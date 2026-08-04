from dataclasses import dataclass
from core.config import settings
from services.cost_engine import CostEngine
from services.htx_client import HTXClient
from services.market_routing import resolve as resolve_route


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

    # Куда физически идёт сделка: рынок, биржевой символ, плечо. Кладётся в
    # Signal.plan_json и читается сопровождением/выходом, чтобы позиция
    # закрывалась там же, где открылась.
    routing: dict | None = None


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
        scalp: bool = False,
        position_margin_usdt_cap: float | None = None,
    ) -> TradePlan:
        risk_pct_value = risk_pct if risk_pct is not None else settings.RISK_PER_TRADE_PCT

        # Рынок определяет СТОРОНА сделки, а не общая настройка: лонг живёт на
        # споте (0.2% taker), шорт возможен только на деривативе (0.05% + фандинг).
        # Пока рынок был один на всех, половина сделок считалась по чужой
        # комиссии — вчетверо мимо на споте.
        route = resolve_route(symbol, side)
        market_type = route.market_type

        # Плечо приходит из маршрута: на споте оно всегда 1, каким бы ни было
        # FUTURES_LEVERAGE.
        leverage_value = leverage if leverage is not None else route.leverage
        if market_type == "spot":
            leverage_value = 1

        balance_usdt = float(balance_usdt)
        risk_usdt = balance_usdt * (float(risk_pct_value) / 100)

        # Точность и лимиты берём с ТОГО рынка, где сделка исполнится: у
        # спотовой пары и её перпетуала разные шаг цены и минимальный лот, и
        # план, построенный по спотовым, биржа отклонит на контракте.
        exch_symbol = route.exchange_symbol

        entry_price = float(self.htx.price_to_precision(exch_symbol, entry_price))
        stop_price = float(self.htx.price_to_precision(exch_symbol, stop_price))
        tp1 = float(self.htx.price_to_precision(exch_symbol, tp1))
        tp2 = float(self.htx.price_to_precision(exch_symbol, tp2))

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
        # Динамический бюджет (position_margin_usdt_cap) ЗАМЕЩАЕТ статический %-кап:
        # robot_loop передаёт сюда долю свободной маржи цикла (free/N или всю free,
        # если кандидат один). Если не передан — старая логика по %.
        if position_margin_usdt_cap is not None and float(position_margin_usdt_cap) > 0:
            max_position_margin_usdt = float(position_margin_usdt_cap)
        else:
            if scalp:
                max_position_margin_pct = float(getattr(settings, "SCALP_MAX_POSITION_MARGIN_PCT", 0.10))
            else:
                max_position_margin_pct = float(getattr(settings, "MAX_POSITION_MARGIN_PCT", 0.35))
            max_position_margin_usdt = balance_usdt * max(0.01, min(max_position_margin_pct, 1.0))
        max_position_notional = max_position_margin_usdt * leverage_value
        qty_by_position_cap = max_position_notional / entry_price

        # (#live-notional-parity-2026-08-04) Кэп нотионала ордера. Раньше он жил
        # ТОЛЬКО в LIVE_EXECUTOR и срабатывал лишь в live: бумага планировала
        # полный размер (ADA $188.92), а live отклонял ордер по кэпу $25 —
        # «бумага разошлась бы с биржей» вне зависимости от монеты. Заводим тот
        # же кэп В САЙЗИНГ: план ограничивает нотионал ДО исполнения, поэтому
        # бумага и live берут один размер, и ни один ордер не упирается в кэп на
        # отправке. cap<=0 — выключено. Применяется в ОБОИХ режимах намеренно:
        # смысл кэпа на этапе ramp-up — маленькие ордера и там, и там.
        _notional_cap = float(getattr(settings, "LIVE_MAX_ORDER_NOTIONAL_USDT", 0.0) or 0.0)
        qty_by_notional_cap = (_notional_cap / entry_price) if _notional_cap > 0 else float("inf")

        # Берём меньшее, чтобы не открыть позицию больше допустимого размера.
        qty = min(qty_by_risk, qty_by_balance, qty_by_position_cap, qty_by_notional_cap)

        # Приводим qty к точности биржи.
        qty = float(self.htx.amount_to_precision(exch_symbol, qty))
        qty = float(qty)

        # Предохранитель на случай биржи, которая при квантовании ОКРУГЛЯЕТ объём
        # вверх (ccxt по умолчанию усекает, но не все маршруты). Тогда нотионал
        # мог бы вылезти за кэп на один шаг — а LIVE_EXECUTOR режет по строгому
        # «>», и такой ордер отклонился бы в live. Ужимаем на шаг вниз до кэпа.
        if _notional_cap > 0 and entry_price > 0:
            _guard = 0
            while qty > 0 and entry_price * qty > _notional_cap and _guard < 32:
                qty = float(self.htx.amount_to_precision(exch_symbol, qty * 0.999))
                _guard += 1

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

        limits = self.htx.market_limits(exch_symbol)
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

        else:
            if scalp:
                base_min_tp1 = float(getattr(settings, "SCALP_MIN_NET_PNL_TP1_USDT", 0.5))
                base_min_tp2 = float(getattr(settings, "SCALP_MIN_NET_PNL_TP2_USDT", 1.0))
            else:
                base_min_tp1 = float(getattr(settings, "MIN_NET_PNL_TP1_USDT", 2.5))
                base_min_tp2 = float(getattr(settings, "MIN_NET_PNL_TP2_USDT", 6.0))
            relax_margin_pct = max(float(getattr(settings, "MIN_NET_PNL_RELAX_MARGIN_PCT", 0.01)), 0.0)
            relaxed_min_tp1 = required_margin * relax_margin_pct
            relaxed_min_tp2 = relaxed_min_tp1 * 1.5
            min_tp1_threshold = min(base_min_tp1, relaxed_min_tp1) if relax_margin_pct > 0 else base_min_tp1
            min_tp2_threshold = min(base_min_tp2, relaxed_min_tp2) if relax_margin_pct > 0 else base_min_tp2

            if tp1_preview.net_pnl < min_tp1_threshold:
                is_valid = False
                reject_reason = "tp1_net_pnl_below_min_usdt"

            elif tp2_preview.net_pnl < min_tp2_threshold:
                is_valid = False
                reject_reason = "tp2_net_pnl_below_min_usdt"

            elif net_rr_tp2 < (float(getattr(settings, "SCALP_MIN_NET_RR_TP2", 1.0)) if scalp else 1.2):
                is_valid = False
                reject_reason = "net_rr_too_low"

            else:
                # (#tp1-partial-2026-07-09) Гейт ОЖИДАЕМОЙ экономики: на TP1
                # реализуется share позиции, остаток целится в TP2 → реальная
                # награда = share·netTP1 + (1−share)·netTP2. Судить всю сделку по
                # TP2 (достигается ~5% случаев) — завышать edge. Требуем, чтобы
                # смесь платила ≥ MIN_NET_RR_BLENDED × |стоп|.
                if bool(getattr(settings, "TP1_PARTIAL_ENABLED", True)) and net_risk > 0:
                    share = max(0.0, min(float(getattr(settings, "TP1_PARTIAL_CLOSE_SHARE", 0.5)), 1.0))
                    blended_reward = share * tp1_preview.net_pnl + (1.0 - share) * tp2_preview.net_pnl
                    net_rr_blended = blended_reward / net_risk
                    if net_rr_blended < float(getattr(settings, "MIN_NET_RR_BLENDED", 1.10)):
                        is_valid = False
                        reject_reason = "net_rr_blended_too_low"

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
            routing=route.as_dict(),
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
# (sync-touch 2026-07-09)
