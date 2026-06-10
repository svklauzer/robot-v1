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
    """Exit policy v4: stop paper-deposit drain from premature weak exits.

    The ML outcome sample shows two expensive patterns:
    - failed_setup_exit closed many trades before they ever proved meaningful MFE;
    - protective exits captured micro-profit that did not cover round-trip costs.

    This policy therefore requires a real MFE sample and a strict age before the
    failed-setup guard is allowed to fire, and it raises minimum protected-profit
    floors before closing pre-TP1 pullbacks.
    """

    K_FAILED_SOFT = 0.50
    K_FAILED_MID = 0.80
    K_FAILED_DEEP = 1.10
    K_LOSS_SOFT = 0.30
    K_LOSS_MID = 0.50
    K_LOSS_DEEP = 0.75
    K_PROTECT = 0.60
    K_TRAIL = 0.90
    K_CAPTURE = 0.90   # raised from 0.75 — MFE capture starts later, lets winners run longer
    DEFAULT_MFE_ABSOLUTE_MIN_FOR_GUARD = 0.50

    def __init__(self):
        self.htx = HTXClient()

    @classmethod
    def runtime_guard(cls) -> dict:
        import inspect
        import re

        source = inspect.getsource(cls.before_tp1_decision)
        stale_exit_pct = re.search(r"(?<!protective_)\bexit_pct\b", source) is not None

        return {
            "ok": not stale_exit_pct,
            "runtime": "protected_pct_v4",
            "stale_exit_pct_reference": stale_exit_pct,
        }

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
        slippage = position_notional_usdt * float(getattr(settings, "SLIPPAGE_BUFFER_PCT", 0.0))
        return gross - fees - slippage

    def _fee_rate(self, symbol: str | None, market_type: str | None = None) -> tuple[float, str]:
        market_type_value = market_type or settings.MARKET_TYPE
        if symbol:
            try:
                rates = self.htx.trading_fee_rates(symbol, market_type_value)
                taker = rates.get("taker")
                if taker is not None:
                    return float(taker), str(rates.get("source", "exchange_or_metadata"))
            except Exception:
                pass
        if market_type_value in ["swap", "futures", "perp"]:
            return float(settings.FUTURES_TAKER_FEE), "fallback_futures_settings"
        return float(settings.SPOT_TAKER_FEE), "fallback_spot_settings"

    def _net_safe_profit_pct(self, symbol: str | None = None, market_type: str | None = None) -> tuple[float, str]:
        """Minimum price move that should cover fees, slippage and a safety buffer."""
        fee_rate, fee_source = self._fee_rate(symbol, market_type)
        calculated = fee_rate * 2 * 100 + float(settings.SLIPPAGE_BUFFER_PCT) * 100 + 0.15
        return round(max(calculated, 0.60), 4), fee_source

    def _dynamic_thresholds(self, stop_distance_pct: float) -> dict:
        sd = abs(float(stop_distance_pct or 0.0))
        net_safe_floor = 0.60
        mfe_absolute_min = float(
            getattr(settings, "FAILED_SETUP_MFE_ABSOLUTE_MIN_PCT", self.DEFAULT_MFE_ABSOLUTE_MIN_FOR_GUARD)
        )

        abs_cap_soft = abs(float(getattr(settings, "FAILED_SETUP_LOSS_SOFT_PCT", -0.40)))
        abs_cap_mid = abs(float(getattr(settings, "FAILED_SETUP_LOSS_MID_PCT", -0.65)))
        abs_cap_deep = abs(float(getattr(settings, "FAILED_SETUP_LOSS_DEEP_PCT", -0.90)))

        # K_CAPTURE is configurable via MFE_CAPTURE_START_PCT (default = class constant K_CAPTURE).
        k_capture = float(getattr(settings, "MFE_CAPTURE_START_PCT", self.K_CAPTURE))

        return {
            "failed_mfe_soft": max(sd * self.K_FAILED_SOFT, mfe_absolute_min),
            "failed_mfe_mid": max(sd * self.K_FAILED_MID, mfe_absolute_min),
            "failed_mfe_deep": max(sd * self.K_FAILED_DEEP, mfe_absolute_min),
            "failed_loss_soft": -min(sd * self.K_LOSS_SOFT, abs_cap_soft),
            "failed_loss_mid": -min(sd * self.K_LOSS_MID, abs_cap_mid),
            "failed_loss_deep": -min(sd * self.K_LOSS_DEEP, abs_cap_deep),
            "protect_start": max(sd * self.K_PROTECT, net_safe_floor, float(settings.PROTECTIVE_MFE_START_PCT)),
            "trail_start": max(sd * self.K_TRAIL, net_safe_floor + 0.40),
            "capture_start": max(sd * k_capture, net_safe_floor + 0.20),
            "mfe_absolute_min": mfe_absolute_min,
        }

    def _get_thresholds(self, stop_distance_pct: float | None) -> tuple[dict, str]:
        if stop_distance_pct is not None and stop_distance_pct > 0:
            return self._dynamic_thresholds(stop_distance_pct), f"dynamic(stop={round(stop_distance_pct, 3)}%)"
        fallback_stop = 1.5
        return self._dynamic_thresholds(fallback_stop), "dynamic_fallback(stop=1.5%+capped)"

    def before_tp1_decision(
        self,
        side: str,
        entry_price: float,
        current_price: float,
        stop_price: float | None = None,
        tp1_price: float | None = None,
        mfe_pct: float | None = None,
        max_profit_price: float | None = None,
        symbol: str | None = None,
        market_type: str | None = None,
        position_notional_usdt: float | None = None,
        signal_age_sec: float | None = None,
    ) -> ExitDecision:
        side = str(side).lower()
        entry_price = float(entry_price)
        current_price = float(current_price)
        mfe = float(mfe_pct or 0.0)
        current_pct = self._result_pct(side, entry_price, current_price)

        stop_distance_pct = (
            abs(entry_price - float(stop_price)) / entry_price * 100
            if stop_price is not None and float(stop_price) > 0
            else None
        )
        thr, threshold_source = self._get_thresholds(stop_distance_pct)
        drawdown_from_mfe = self._drawdown_from_mfe(current_pct, mfe)
        net_safe_pct, fee_source = self._net_safe_profit_pct(symbol=symbol, market_type=market_type)
        min_protective_exit_pct = float(getattr(settings, "MIN_PROTECTIVE_EXIT_PCT", 1.20))
        min_protective_net_usdt = float(getattr(settings, "MIN_PROTECTIVE_NET_USDT", 1.50))
        protective_drawdown_share = float(settings.PROTECTIVE_DRAWDOWN_SHARE)
        adaptive_drawdown_pct = float(settings.ADAPTIVE_TRAIL_DRAWDOWN_PCT)
        min_age_sec = float(getattr(settings, "FAILED_SETUP_MIN_AGE_SEC", 300))

        age_ok = signal_age_sec is not None and float(signal_age_sec) >= min_age_sec
        tp1_dist_pct = (
            abs(float(tp1_price) - entry_price) / entry_price * 100
            if tp1_price is not None and float(tp1_price) > 0
            else None
        )

        if mfe_pct is not None and age_ok and mfe >= thr["mfe_absolute_min"]:
            if mfe < thr["failed_mfe_soft"] and current_pct <= thr["failed_loss_soft"]:
                return ExitDecision(
                    exit=True,
                    reason="failed_setup_exit",
                    exit_price=current_price,
                    note=f"soft: mfe={mfe:.4f}<{thr['failed_mfe_soft']:.4f} loss={current_pct:.4f}<={thr['failed_loss_soft']:.4f} src={threshold_source}",
                )
            if mfe < thr["failed_mfe_mid"] and current_pct <= thr["failed_loss_mid"]:
                return ExitDecision(
                    exit=True,
                    reason="failed_setup_exit",
                    exit_price=current_price,
                    note=f"mid: mfe={mfe:.4f}<{thr['failed_mfe_mid']:.4f} loss={current_pct:.4f}<={thr['failed_loss_mid']:.4f} src={threshold_source}",
                )
            if mfe < thr["failed_mfe_deep"] and current_pct <= thr["failed_loss_deep"]:
                return ExitDecision(
                    exit=True,
                    reason="failed_setup_exit",
                    exit_price=current_price,
                    note=f"deep: mfe={mfe:.4f}<{thr['failed_mfe_deep']:.4f} loss={current_pct:.4f}<={thr['failed_loss_deep']:.4f} src={threshold_source}",
                )

        if bool(getattr(settings, "MFE_CAPTURE_ENABLED", True)):
            capture_drawdown = float(getattr(settings, "MFE_CAPTURE_DRAWDOWN_PCT", 0.30))
            capture_share = float(getattr(settings, "MFE_CAPTURE_PROTECT_SHARE", 0.40))
            tp1_guard_ok = tp1_dist_pct is None or current_pct >= tp1_dist_pct * 0.90
            if (
                mfe >= thr["capture_start"]
                and current_pct > net_safe_pct
                and drawdown_from_mfe >= mfe * capture_drawdown
                and tp1_guard_ok
            ):
                protected_pct = max(mfe * capture_share, net_safe_pct, min_protective_exit_pct)
                est_net = self._estimated_net_usdt(protected_pct, position_notional_usdt)
                if est_net is not None and est_net < min_protective_net_usdt:
                    return ExitDecision(exit=False)
                exit_price = self._price_from_result_pct(side, entry_price, protected_pct)
                return ExitDecision(
                    exit=True,
                    reason="adaptive_mfe_capture",
                    exit_price=round(exit_price, 8),
                    note=(
                        f"mfe={round(mfe, 4)} current={round(current_pct, 4)} "
                        f"drawdown={round(drawdown_from_mfe, 4)} protected={round(protected_pct, 4)} "
                        f"net_safe={round(net_safe_pct, 4)} fee_source={fee_source} "
                        f"tp1_dist={round(tp1_dist_pct, 4) if tp1_dist_pct else None}"
                    ),
                )

        if mfe >= float(settings.PROTECTIVE_MFE_START_PCT) and current_pct <= net_safe_pct:
            protected_pct = max(net_safe_pct, min_protective_exit_pct)
            est_net = self._estimated_net_usdt(protected_pct, position_notional_usdt)
            if est_net is not None and est_net < min_protective_net_usdt:
                return ExitDecision(exit=False)
            exit_price = self._price_from_result_pct(side, entry_price, protected_pct)
            return ExitDecision(
                exit=True, reason="protective_breakeven_profit_guard",
                exit_price=round(exit_price, 8),
                note=(
                    f"mfe={round(mfe, 4)} current={round(current_pct, 4)} "
                    f"protected={round(protected_pct, 4)} net_safe={round(net_safe_pct, 4)} fee_source={fee_source}"
                ),
            )

        if mfe >= thr["protect_start"] and drawdown_from_mfe >= mfe * protective_drawdown_share:
            protected_pct = max(mfe * (1.0 - protective_drawdown_share), net_safe_pct, min_protective_exit_pct)
            est_net = self._estimated_net_usdt(protected_pct, position_notional_usdt)
            if est_net is not None and est_net < min_protective_net_usdt:
                return ExitDecision(exit=False)
            exit_price = self._price_from_result_pct(side, entry_price, protected_pct)
            return ExitDecision(
                exit=True, reason="protective_trailing_stop",
                exit_price=round(exit_price, 8),
                note=(
                    f"mfe={mfe:.4f} cur={current_pct:.4f} dd={drawdown_from_mfe:.4f} "
                    f"prot={protected_pct:.4f} prot_start={thr['protect_start']:.4f} src={threshold_source}"
                ),
            )

        if mfe >= thr["trail_start"] and drawdown_from_mfe >= mfe * adaptive_drawdown_pct:
            protected_pct = max(mfe * (1.0 - adaptive_drawdown_pct), net_safe_pct, min_protective_exit_pct)
            est_net = self._estimated_net_usdt(protected_pct, position_notional_usdt)
            if est_net is not None and est_net < min_protective_net_usdt:
                return ExitDecision(exit=False)
            exit_price = self._price_from_result_pct(side, entry_price, protected_pct)
            return ExitDecision(
                exit=True, reason="adaptive_trailing_stop",
                exit_price=round(exit_price, 8),
                note=(
                    f"mfe={mfe:.4f} cur={current_pct:.4f} dd={drawdown_from_mfe:.4f} "
                    f"prot={protected_pct:.4f} trail_start={thr['trail_start']:.4f} src={threshold_source}"
                ),
            )

        return ExitDecision(exit=False)

    # ------------------------------------------------------------------
    # Основной метод: после TP1
    # ------------------------------------------------------------------

    def after_tp1_decision(
        self,
        side: str,
        entry_price: float,
        current_price: float,
        tp2_price: float,
        stop_price: float | None = None,
        lifecycle: dict | None = None,
        symbol: str | None = None,
        market_type: str | None = None,
        signal_age_sec: float | None = None,
    ) -> ExitDecision:
        side = str(side).lower()
        lifecycle = lifecycle or {}
        entry_price = float(entry_price)
        current_price = float(current_price)
        tp2_price = float(tp2_price)
        current_pct = self._result_pct(side, entry_price, current_price)
        tp2_pct = self._result_pct(side, entry_price, tp2_price)
        mfe = float(lifecycle.get("mfe_pct") or current_pct or 0.0)
        drawdown_from_mfe = self._drawdown_from_mfe(current_pct, mfe)
        net_safe_pct, fee_source = self._net_safe_profit_pct(symbol=symbol, market_type=market_type)
        min_post_tp1_exit_pct = float(getattr(settings, "MIN_POST_TP1_EXIT_PCT", 0.80))

        stop_distance_pct = (
            abs(entry_price - float(stop_price)) / entry_price * 100
            if stop_price is not None and float(stop_price) > 0
            else None
        )
        threshold_source = f"dynamic(stop={round(stop_distance_pct, 3)}%)" if stop_distance_pct else "static_fallback"

        if tp2_pct > 0 and current_pct >= tp2_pct * 0.92:
            return ExitDecision(
                exit=True, reason="tp2_reached",
                exit_price=round(float(tp2_price), 8),
                note=f"cur={current_pct:.4f} tp2={tp2_pct:.4f}",
            )

        if stop_distance_pct is not None and stop_distance_pct >= 3.0:
            tp2_progress = current_pct / tp2_pct if tp2_pct > 0 else 0
            if tp2_progress >= 0.80 and drawdown_from_mfe >= mfe * 0.20:
                protected_pct = max(mfe * 0.70, net_safe_pct, min_post_tp1_exit_pct)
                exit_price = self._price_from_result_pct(side, entry_price, protected_pct)
                return ExitDecision(
                    exit=True,
                    reason="wide_stop_tp2_guard",
                    exit_price=round(exit_price, 8),
                    note=(
                        f"mfe={mfe:.4f} cur={current_pct:.4f} tp2_prog={tp2_progress:.3f} "
                        f"dd={drawdown_from_mfe:.4f} prot={protected_pct:.4f} src={threshold_source}"
                    ),
                )

        if mfe >= 3.0 and drawdown_from_mfe >= mfe * 0.30:
            protected_pct = max(mfe * 0.60, net_safe_pct, min_post_tp1_exit_pct)
            exit_price = self._price_from_result_pct(side, entry_price, protected_pct)
            return ExitDecision(
                exit=True,
                reason="trend_trailing_stop",
                exit_price=round(exit_price, 8),
                note=(
                    f"mfe={mfe:.4f} cur={current_pct:.4f} dd={drawdown_from_mfe:.4f} "
                    f"prot={protected_pct:.4f} src={threshold_source} fee={fee_source}"
                ),
            )

        if mfe >= 2.0 and drawdown_from_mfe >= mfe * 0.35:
            protected_pct = max(mfe * 0.60, net_safe_pct, min_post_tp1_exit_pct)
            exit_price = self._price_from_result_pct(side, entry_price, protected_pct)
            return ExitDecision(
                exit=True,
                reason="adaptive_post_tp1_stop",
                exit_price=round(exit_price, 8),
                note=(
                    f"mfe={mfe:.4f} cur={current_pct:.4f} dd={drawdown_from_mfe:.4f} "
                    f"prot={protected_pct:.4f} fee={fee_source}"
                ),
            )

        return ExitDecision(exit=False)