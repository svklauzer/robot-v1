from dataclasses import dataclass

from core.config import settings
from services.htx_client import HTXClient


@dataclass
class ExitDecision:
    exit: bool
    reason: str | None = None
    exit_price: float | None = None
    note: str | None = None


class ExitPolicyService:
    """
    Интеллектуальная exit policy.

    Цель:
    - не давать прибыльным сделкам превращаться в net-минус после комиссий;
    - защищать MFE до TP1;
    - после TP1 включать адаптивную защиту прибыли;
    - не душить хорошие трендовые сделки слишком рано.

    Важно:
    breakeven здесь = NET breakeven, а не просто цена входа.
    """

    def __init__(self):
        self.htx = HTXClient()

    def _result_pct(self, side: str, entry_price: float, current_price: float) -> float:
        if not entry_price:
            return 0.0

        if side == "long":
            return ((current_price - entry_price) / entry_price) * 100

        return ((entry_price - current_price) / entry_price) * 100

    def _price_from_result_pct(self, side: str, entry_price: float, result_pct: float) -> float:
        if side == "long":
            return entry_price * (1 + result_pct / 100)

        return entry_price * (1 - result_pct / 100)

    def _drawdown_from_mfe(self, current_pct: float, mfe_pct: float) -> float:
        if mfe_pct <= 0:
            return 0.0

        return max(mfe_pct - current_pct, 0.0)

    def _estimated_net_usdt(self, result_pct: float, position_notional_usdt: float | None) -> float | None:
        if position_notional_usdt is None or position_notional_usdt <= 0:
            return None
        gross = position_notional_usdt * (result_pct / 100.0)
        fees = position_notional_usdt * float(settings.SPOT_TAKER_FEE) * 2
        return gross - fees

    def _fee_rate(self, symbol: str | None, market_type: str | None = None) -> tuple[float, str]:
        market_type_value = market_type or settings.MARKET_TYPE

        if symbol:
            try:
                rates = self.htx.trading_fee_rates(symbol, market_type_value)
                taker = rates.get("taker")

                if taker is not None:
                    return float(taker), str(rates.get("source", "exchange_or_metadata"))
            except Exception as e:
                print(f"[EXIT POLICY FEE ERROR] {symbol}: {e}")

        if market_type_value in ["swap", "futures", "perp"]:
            return float(settings.FUTURES_TAKER_FEE), "fallback_futures_settings"

        return float(settings.SPOT_TAKER_FEE), "fallback_spot_settings"

    def _net_safe_profit_pct(
        self,
        symbol: str | None = None,
        market_type: str | None = None,
    ) -> tuple[float, str]:
        """
        Минимальный процент движения, чтобы выход был не просто выше/ниже входа,
        а реально покрывал комиссии и slippage.

        Пример для HTX spot taker 0.002:
        вход 0.2% + выход 0.2% + slippage 0.05% + запас 0.05% = около 0.50%.
        """
        fee_rate, fee_source = self._fee_rate(symbol, market_type)

        round_trip_fee_pct = fee_rate * 2 * 100
        slippage_pct = float(settings.SLIPPAGE_BUFFER_PCT) * 100

        # Небольшой запас, чтобы не закрывать "в ноль" из-за округлений/precision.
        safety_extra_pct = 0.05

        calculated = round_trip_fee_pct + slippage_pct + safety_extra_pct

        # Ниже 0.45% не опускаем, потому текущая статистика уже показала,
        # что около +0.05% по цене превращается в net-минус.
        min_safe = max(calculated, 0.45)

        return round(min_safe, 4), fee_source

    def before_tp1_decision(
        self,
        side: str,
        entry_price: float,
        current_price: float,
        mfe_pct: float | None = None,
        max_profit_price: float | None = None,
        symbol: str | None = None,
        market_type: str | None = None,
        position_notional_usdt: float | None = None,
    ) -> ExitDecision:
        """
        Защита до TP1.

        Если сделка уже дала хороший плюс, но начинает отдавать прибыль,
        закрываем её не в price-breakeven, а в net-breakeven/profit после комиссий.
        """

        side = str(side).lower()
        entry_price = float(entry_price)
        current_price = float(current_price)

        current_pct = self._result_pct(side, entry_price, current_price)

        # Failed setup guard before TP1:
        # 1) Если позиция почти не развилась и пошла против нас — закрываем рано.
        if mfe_pct is not None:
            mfe_value = float(mfe_pct)

            if (
                mfe_value < float(settings.FAILED_SETUP_MFE_SOFT_PCT)
                and current_pct <= float(settings.FAILED_SETUP_LOSS_SOFT_PCT)
            ):
                return ExitDecision(
                    exit=True,
                    reason="failed_setup_exit",
                    exit_price=current_price,
                    note=f"failed_setup_fast: mfe={round(mfe_value, 4)} current={round(current_pct, 4)}",
                )

            if (
                mfe_value < float(settings.FAILED_SETUP_MFE_MID_PCT)
                and current_pct <= float(settings.FAILED_SETUP_LOSS_MID_PCT)
            ):
                return ExitDecision(
                    exit=True,
                    reason="failed_setup_exit",
                    exit_price=current_price,
                    note=f"failed_setup: mfe={round(mfe_value, 4)} current={round(current_pct, 4)}",
                )

            if (
                mfe_value < float(settings.FAILED_SETUP_MFE_DEEP_PCT)
                and current_pct <= float(settings.FAILED_SETUP_LOSS_DEEP_PCT)
            ):
                return ExitDecision(
                    exit=True,
                    reason="failed_setup_exit",
                    exit_price=current_price,
                    note=f"failed_setup_deep: mfe={round(mfe_value, 4)} current={round(current_pct, 4)}",
                )

        mfe = float(mfe_pct or 0.0)
        drawdown_from_mfe = self._drawdown_from_mfe(current_pct, mfe)

        net_safe_pct, fee_source = self._net_safe_profit_pct(
            symbol=symbol,
            market_type=market_type,
        )
        min_protective_exit_pct = float(getattr(settings, "MIN_PROTECTIVE_EXIT_PCT", 0.20))

        # 1. Жёсткая NET-защита:
        # сделка дала >= 0.45%, но возвращается к зоне, где после комиссий уже опасно.
        # Выходим не по +0.05%, а по net_safe_pct.
        if mfe >= float(settings.PROTECTIVE_MFE_START_PCT) and current_pct <= net_safe_pct:
            exit_pct = max(net_safe_pct, min_protective_exit_pct)
            est_net = self._estimated_net_usdt(exit_pct, position_notional_usdt)
            if est_net is not None and est_net < float(getattr(settings, "MIN_PROTECTIVE_NET_USDT", 0.25)):
                return ExitDecision(exit=False)
            exit_price = self._price_from_result_pct(side, entry_price, exit_pct)

            return ExitDecision(
                exit=True,
                reason="protective_breakeven_profit_guard",
                exit_price=round(exit_price, 8),
                note=(
                    f"mfe={round(mfe, 4)} current={round(current_pct, 4)} "
                    f"net_safe={round(net_safe_pct, 4)} fee_source={fee_source}"
                ),
            )

        # 2. Сделка дала >= 0.8%, но отдала больше 60% достигнутой прибыли.
        if mfe >= 0.8 and drawdown_from_mfe >= mfe * float(settings.PROTECTIVE_DRAWDOWN_SHARE):
            protected_pct = max(mfe * 0.30, net_safe_pct, min_protective_exit_pct)
            est_net = self._estimated_net_usdt(protected_pct, position_notional_usdt)
            if est_net is not None and est_net < float(getattr(settings, "MIN_PROTECTIVE_NET_USDT", 0.25)):
                return ExitDecision(exit=False)
            exit_price = self._price_from_result_pct(side, entry_price, protected_pct)

            return ExitDecision(
                exit=True,
                reason="protective_trailing_stop",
                exit_price=round(exit_price, 8),
                note=(
                    f"mfe={round(mfe, 4)} current={round(current_pct, 4)} "
                    f"drawdown={round(drawdown_from_mfe, 4)} "
                    f"protected={round(protected_pct, 4)} "
                    f"net_safe={round(net_safe_pct, 4)} fee_source={fee_source}"
                ),
            )

        # 3. Сделка дала >= 1.2%, но резко откатила.
        if (
            mfe >= float(settings.ADAPTIVE_TRAIL_MFE_START_PCT)
            and drawdown_from_mfe >= float(settings.ADAPTIVE_TRAIL_DRAWDOWN_PCT)
        ):
            protected_pct = max(mfe * 0.40, net_safe_pct, min_protective_exit_pct)
            est_net = self._estimated_net_usdt(protected_pct, position_notional_usdt)
            if est_net is not None and est_net < float(getattr(settings, "MIN_PROTECTIVE_NET_USDT", 0.25)):
                return ExitDecision(exit=False)
            exit_price = self._price_from_result_pct(side, entry_price, protected_pct)

            return ExitDecision(
                exit=True,
                reason="adaptive_trailing_stop",
                exit_price=round(exit_price, 8),
                note=(
                    f"mfe={round(mfe, 4)} current={round(current_pct, 4)} "
                    f"drawdown={round(drawdown_from_mfe, 4)} "
                    f"protected={round(protected_pct, 4)} "
                    f"net_safe={round(net_safe_pct, 4)} fee_source={fee_source}"
                ),
            )

        return ExitDecision(exit=False)

    def after_tp1_decision(
        self,
        side: str,
        entry_price: float,
        current_price: float,
        tp2_price: float,
        lifecycle: dict | None = None,
        symbol: str | None = None,
        market_type: str | None = None,
    ) -> ExitDecision:
        """
        Защита после TP1.

        После TP1 мы уже не должны отдавать рынок обратно.
        Цель — либо TP2, либо умная фиксация части тренда.
        """

        side = str(side).lower()
        lifecycle = lifecycle or {}

        entry_price = float(entry_price)
        current_price = float(current_price)
        tp2_price = float(tp2_price)

        current_pct = self._result_pct(side, entry_price, current_price)
        tp2_pct = self._result_pct(side, entry_price, tp2_price)

        mfe = float(lifecycle.get("mfe_pct") or current_pct or 0.0)
        drawdown_from_mfe = self._drawdown_from_mfe(current_pct, mfe)

        net_safe_pct, fee_source = self._net_safe_profit_pct(
            symbol=symbol,
            market_type=market_type,
        )
        min_post_tp1_exit_pct = float(getattr(settings, "MIN_POST_TP1_EXIT_PCT", 0.35))

        # 1. Если почти дошли до TP2 — фиксируем TP2 по уровню,
        # не даём следующему тику украсть результат.
        if tp2_pct > 0 and current_pct >= tp2_pct * 0.92:
            return ExitDecision(
                exit=True,
                reason="tp2_reached",
                exit_price=round(float(tp2_price), 8),
                note=f"current_pct={round(current_pct, 4)} tp2_pct={round(tp2_pct, 4)}",
            )

        # 2. После TP1 защищаем минимум 40% от лучшей прибыли,
        # но не ниже net_safe_pct.
        if mfe >= 1.0 and drawdown_from_mfe >= mfe * 0.45:
            protected_pct = max(mfe * 0.40, net_safe_pct, min_post_tp1_exit_pct)
            exit_price = self._price_from_result_pct(side, entry_price, protected_pct)

            return ExitDecision(
                exit=True,
                reason="adaptive_post_tp1_stop",
                exit_price=round(exit_price, 8),
                note=(
                    f"mfe={round(mfe, 4)} current={round(current_pct, 4)} "
                    f"drawdown={round(drawdown_from_mfe, 4)} "
                    f"protected={round(protected_pct, 4)} "
                    f"net_safe={round(net_safe_pct, 4)} fee_source={fee_source}"
                ),
            )

        # 3. Если была хорошая прибыль >= 2%, но рынок отдал 35%,
        # фиксируем больше, потому что был сильный импульс.
        if mfe >= 2.0 and drawdown_from_mfe >= mfe * 0.35:
            protected_pct = max(mfe * 0.55, net_safe_pct, min_post_tp1_exit_pct)
            exit_price = self._price_from_result_pct(side, entry_price, protected_pct)

            return ExitDecision(
                exit=True,
                reason="trend_trailing_stop",
                exit_price=round(exit_price, 8),
                note=(
                    f"mfe={round(mfe, 4)} current={round(current_pct, 4)} "
                    f"drawdown={round(drawdown_from_mfe, 4)} "
                    f"protected={round(protected_pct, 4)} "
                    f"net_safe={round(net_safe_pct, 4)} fee_source={fee_source}"
                ),
            )

        return ExitDecision(exit=False)
