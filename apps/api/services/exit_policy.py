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
    Интеллектуальная exit policy с динамическими порогами.

    Ключевое изменение v2: все MFE/loss пороги теперь рассчитываются
    относительно реального расстояния до стопа (stop_distance_pct),
    а не как фиксированные абсолютные проценты.

    Это устраняет два системных дефекта v1:
    1. Преждевременное закрытие широких стопов (TON, DOT):
       - v1: protective guard срабатывал на 0.30% MFE при стопе 5.58% (4% пути до TP1)
       - v2: срабатывает на 2.79% MFE (35% пути до TP1)

    2. Слишком мягкий failed_setup для плотных стопов (SOL, AVAX):
       - v1: MFE_SOFT=0.20% при стопе 0.95% — позиция считалась "подтверждённой"
             ниже break-even комиссий (0.45%)
       - v2: MFE_SOFT = 0.30 × stop_dist = 0.285% — пропорционально стопу

    Принцип: защита включается не раньше чем цена прошла минимум
    K_PROTECT (50%) от расстояния до стопа в нужную сторону.
    Это гарантирует что позиция реально подтвердила направление
    прежде чем мы начнём её "охранять".

    Цель: дать каждой сделке полностью реализовать потенциал до TP1/TP2
    и не закрывать позиции на рыночном шуме.
    """

    # ------------------------------------------------------------------
    # Коэффициенты — доля от stop_distance_pct
    # ------------------------------------------------------------------

    # Failed setup: сколько % от стопа должен пройти MFE чтобы
    # сетап считался "живым". Если MFE меньше — закрываем при убытке.
    K_FAILED_SOFT  = 0.30   # 30% стопа — мягкий порог
    K_FAILED_MID   = 0.55   # 55% стопа — средний
    K_FAILED_DEEP  = 0.80   # 80% стопа — глубокий

    # Loss пороги для failed_setup — доля от stop_distance
    K_LOSS_SOFT    = 0.25   # закрываем если убыток > 25% расстояния до стопа
    K_LOSS_MID     = 0.45
    K_LOSS_DEEP    = 0.70

    # Protective guard: MFE должен пройти минимум 50% расстояния до стопа
    # прежде чем включится защита. Ниже этого — позиция развивается,
    # не трогаем.
    K_PROTECT      = 0.50

    # Adaptive trail: более агрессивная защита с 80% расстояния до стопа
    K_TRAIL        = 0.80

    # MFE capture: ранняя фиксация с 65% расстояния до стопа
    K_CAPTURE      = 0.65

    # ------------------------------------------------------------------

    def __init__(self):
        self.htx = HTXClient()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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
        fees  = position_notional_usdt * float(settings.SPOT_TAKER_FEE) * 2
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
        Минимальный процент движения чтобы выход реально покрыл комиссии.
        entry fee + exit fee + slippage + запас = ~0.50%.
        """
        fee_rate, fee_source = self._fee_rate(symbol, market_type)
        round_trip_fee_pct   = fee_rate * 2 * 100
        slippage_pct         = float(settings.SLIPPAGE_BUFFER_PCT) * 100
        safety_extra_pct     = 0.05
        calculated           = round_trip_fee_pct + slippage_pct + safety_extra_pct
        min_safe             = max(calculated, 0.45)
        return round(min_safe, 4), fee_source

    def _dynamic_thresholds(self, stop_distance_pct: float) -> dict:
        """
        Рассчитывает все MFE/loss пороги относительно реального stop_distance_pct.

        Принцип: порог = max(K × stop_distance, абсолютный_минимум).
        Абсолютный минимум не даёт порогам быть ниже break-even комиссий
        на экстремально плотных стопах.

        Возвращает словарь со всеми динамическими значениями.
        """
        sd = abs(stop_distance_pct)

        # Break-even комиссий — абсолютный пол для всех "прибыльных" порогов
        net_safe_floor = 0.45

        return {
            # Failed setup MFE пороги
            "failed_mfe_soft":  sd * self.K_FAILED_SOFT,
            "failed_mfe_mid":   sd * self.K_FAILED_MID,
            "failed_mfe_deep":  sd * self.K_FAILED_DEEP,

            # Failed setup loss пороги (отрицательные)
            "failed_loss_soft": -(sd * self.K_LOSS_SOFT),
            "failed_loss_mid":  -(sd * self.K_LOSS_MID),
            "failed_loss_deep": -(sd * self.K_LOSS_DEEP),

            # Защитные пороги — не ниже break-even комиссий
            "protect_start":  max(sd * self.K_PROTECT,  net_safe_floor),
            "trail_start":    max(sd * self.K_TRAIL,    net_safe_floor + 0.30),
            "capture_start":  max(sd * self.K_CAPTURE,  net_safe_floor + 0.15),
        }

    # ------------------------------------------------------------------
    # Основной метод: до TP1
    # ------------------------------------------------------------------

    def before_tp1_decision(
        self,
        side: str,
        entry_price: float,
        current_price: float,
        stop_price: float | None = None,
        mfe_pct: float | None = None,
        max_profit_price: float | None = None,
        symbol: str | None = None,
        market_type: str | None = None,
        position_notional_usdt: float | None = None,
        signal_age_sec: float | None = None,
    ) -> ExitDecision:
        """
        Защита до TP1.

        Все пороги динамические — рассчитаны от реального stop_distance_pct.
        Это гарантирует корректное поведение как для широких стопов (TON 5.6%),
        так и для плотных (SOL 0.9%).

        Параметр stop_price теперь обязателен для динамической логики.
        При его отсутствии используются fallback-значения из config.py.
        """
        side          = str(side).lower()
        entry_price   = float(entry_price)
        current_price = float(current_price)
        mfe           = float(mfe_pct or 0.0)
        current_pct   = self._result_pct(side, entry_price, current_price)

        # Рассчитываем stop_distance для динамических порогов
        if stop_price is not None and float(stop_price) > 0:
            stop_distance_pct = abs(entry_price - float(stop_price)) / entry_price * 100
        else:
            # Fallback на старые абсолютные значения если stop_price не передан
            stop_distance_pct = None

        # Выбираем пороги: динамические или статические (обратная совместимость)
        if stop_distance_pct is not None:
            thr = self._dynamic_thresholds(stop_distance_pct)
            failed_mfe_soft  = thr["failed_mfe_soft"]
            failed_mfe_mid   = thr["failed_mfe_mid"]
            failed_mfe_deep  = thr["failed_mfe_deep"]
            failed_loss_soft = thr["failed_loss_soft"]
            failed_loss_mid  = thr["failed_loss_mid"]
            failed_loss_deep = thr["failed_loss_deep"]
            protect_start    = thr["protect_start"]
            trail_start      = thr["trail_start"]
            capture_start    = thr["capture_start"]
            threshold_source = f"dynamic(stop={round(stop_distance_pct, 3)}%)"
        else:
            # Старая статическая логика — fallback для обратной совместимости
            failed_mfe_soft  = float(settings.FAILED_SETUP_MFE_SOFT_PCT)
            failed_mfe_mid   = float(settings.FAILED_SETUP_MFE_MID_PCT)
            failed_mfe_deep  = float(settings.FAILED_SETUP_MFE_DEEP_PCT)
            failed_loss_soft = float(settings.FAILED_SETUP_LOSS_SOFT_PCT)
            failed_loss_mid  = float(settings.FAILED_SETUP_LOSS_MID_PCT)
            failed_loss_deep = float(settings.FAILED_SETUP_LOSS_DEEP_PCT)
            protect_start    = float(settings.PROTECTIVE_MFE_START_PCT)
            trail_start      = float(settings.ADAPTIVE_TRAIL_MFE_START_PCT)
            capture_start    = float(getattr(settings, "MFE_CAPTURE_START_PCT", 0.65))
            threshold_source = "static_fallback"

        net_safe_pct, fee_source = self._net_safe_profit_pct(symbol=symbol, market_type=market_type)
        min_protective_exit_pct  = float(getattr(settings, "MIN_PROTECTIVE_EXIT_PCT", 0.20))
        min_protective_net_usdt  = float(getattr(settings, "MIN_PROTECTIVE_NET_USDT", 0.25))
        min_r_mult               = float(getattr(settings, "MIN_PROTECTIVE_R_MULT", 0.05))

        drawdown_from_mfe = self._drawdown_from_mfe(current_pct, mfe)

        # ------------------------------------------------------------------
        # Guard: Failed setup — позиция не развивается и уходит в минус
        # ------------------------------------------------------------------
        min_age_sec = float(getattr(settings, "FAILED_SETUP_MIN_AGE_SEC", 300))
        age_ok = signal_age_sec is None or float(signal_age_sec) >= min_age_sec

        if mfe_pct is not None and age_ok:
            # Soft: MFE едва шевельнулся и уже откат
            if mfe < failed_mfe_soft and current_pct <= failed_loss_soft:
                return ExitDecision(
                    exit=True,
                    reason="failed_setup_exit",
                    exit_price=current_price,
                    note=(
                        f"failed_soft: mfe={round(mfe, 4)} < thr={round(failed_mfe_soft, 4)} "
                        f"loss={round(current_pct, 4)} <= {round(failed_loss_soft, 4)} "
                        f"src={threshold_source}"
                    ),
                )

            # Mid: дошёл до середины но разворот
            if mfe < failed_mfe_mid and current_pct <= failed_loss_mid:
                return ExitDecision(
                    exit=True,
                    reason="failed_setup_exit",
                    exit_price=current_price,
                    note=(
                        f"failed_mid: mfe={round(mfe, 4)} < thr={round(failed_mfe_mid, 4)} "
                        f"loss={round(current_pct, 4)} <= {round(failed_loss_mid, 4)} "
                        f"src={threshold_source}"
                    ),
                )

            # Deep: почти до стопа MFE но сильный откат
            if mfe < failed_mfe_deep and current_pct <= failed_loss_deep:
                return ExitDecision(
                    exit=True,
                    reason="failed_setup_exit",
                    exit_price=current_price,
                    note=(
                        f"failed_deep: mfe={round(mfe, 4)} < thr={round(failed_mfe_deep, 4)} "
                        f"loss={round(current_pct, 4)} <= {round(failed_loss_deep, 4)} "
                        f"src={threshold_source}"
                    ),
                )

        # ------------------------------------------------------------------
        # Guard 0: Adaptive MFE capture
        # Ранняя фиксация если сделка дала хороший плюс и быстро отдаёт.
        # Включается только когда MFE >= capture_start (динамический).
        # ------------------------------------------------------------------
        if bool(getattr(settings, "MFE_CAPTURE_ENABLED", True)):
            capture_drawdown = float(getattr(settings, "MFE_CAPTURE_DRAWDOWN_PCT", 0.30))
            capture_share    = float(getattr(settings, "MFE_CAPTURE_PROTECT_SHARE", 0.35))

            if mfe >= capture_start and current_pct > net_safe_pct and drawdown_from_mfe >= capture_drawdown:
                protected_pct = max(mfe * capture_share, net_safe_pct, min_protective_exit_pct)

                # Не выходим если net PnL будет меньше минимума
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
                        f"drawdown={round(drawdown_from_mfe, 4)} "
                        f"protected={round(protected_pct, 4)} "
                        f"capture_start={round(capture_start, 4)} "
                        f"src={threshold_source} fee_src={fee_source}"
                    ),
                )

        # 0. Adaptive MFE capture experiment:
        # если сделка уже дала умеренный плюс, но быстро отдает часть MFE,
        # фиксируем net-safe profit раньше классического trailing.
        if bool(getattr(settings, "MFE_CAPTURE_ENABLED", True)):
            capture_start = float(getattr(settings, "MFE_CAPTURE_START_PCT", 0.65))
            capture_drawdown = float(getattr(settings, "MFE_CAPTURE_DRAWDOWN_PCT", 0.30))
            capture_share = float(getattr(settings, "MFE_CAPTURE_PROTECT_SHARE", 0.35))

            if mfe >= capture_start and current_pct > net_safe_pct and drawdown_from_mfe >= capture_drawdown:
                protected_pct = max(mfe * capture_share, net_safe_pct, min_protective_exit_pct)
                est_net = self._estimated_net_usdt(protected_pct, position_notional_usdt)
                if est_net is not None and est_net < float(getattr(settings, "MIN_PROTECTIVE_NET_USDT", 0.25)):
                    return ExitDecision(exit=False)

                exit_price = self._price_from_result_pct(side, entry_price, protected_pct)

                return ExitDecision(
                    exit=True,
                    reason="adaptive_mfe_capture",
                    exit_price=round(exit_price, 8),
                    note=(
                        f"mfe={round(mfe, 4)} current={round(current_pct, 4)} "
                        f"drawdown={round(drawdown_from_mfe, 4)} "
                        f"protected={round(protected_pct, 4)} "
                        f"net_safe={round(net_safe_pct, 4)} fee_source={fee_source}"
                    ),
                )

        # 0. Adaptive MFE capture experiment:
        # если сделка уже дала умеренный плюс, но быстро отдает часть MFE,
        # фиксируем net-safe profit раньше классического trailing.
        if bool(getattr(settings, "MFE_CAPTURE_ENABLED", True)):
            capture_start = float(getattr(settings, "MFE_CAPTURE_START_PCT", 0.65))
            capture_drawdown = float(getattr(settings, "MFE_CAPTURE_DRAWDOWN_PCT", 0.30))
            capture_share = float(getattr(settings, "MFE_CAPTURE_PROTECT_SHARE", 0.35))

            if mfe >= capture_start and current_pct > net_safe_pct and drawdown_from_mfe >= capture_drawdown:
                protected_pct = max(mfe * capture_share, net_safe_pct, min_protective_exit_pct)
                est_net = self._estimated_net_usdt(protected_pct, position_notional_usdt)
                if est_net is not None and est_net < float(getattr(settings, "MIN_PROTECTIVE_NET_USDT", 0.25)):
                    return ExitDecision(exit=False)

                exit_price = self._price_from_result_pct(side, entry_price, protected_pct)

                return ExitDecision(
                    exit=True,
                    reason="adaptive_mfe_capture",
                    exit_price=round(exit_price, 8),
                    note=(
                        f"mfe={round(mfe, 4)} current={round(current_pct, 4)} "
                        f"drawdown={round(drawdown_from_mfe, 4)} "
                        f"protected={round(protected_pct, 4)} "
                        f"net_safe={round(net_safe_pct, 4)} fee_source={fee_source}"
                    ),
                )

        # 1. Жёсткая NET-защита:
        # сделка дала >= 0.45%, но возвращается к зоне, где после комиссий уже опасно.
        # Выходим не по +0.05%, а по net_safe_pct.
        if mfe >= float(settings.PROTECTIVE_MFE_START_PCT) and current_pct <= net_safe_pct:
            exit_pct = max(net_safe_pct, min_protective_exit_pct)

            est_net = self._estimated_net_usdt(exit_pct, position_notional_usdt)
            if est_net is not None and est_net < min_protective_net_usdt:
                return ExitDecision(exit=False)

            # Дополнительная проверка R-множителя
            if stop_distance_pct is not None:
                r_achieved = exit_pct / stop_distance_pct
                if r_achieved < min_r_mult:
                    return ExitDecision(exit=False)

            exit_price = self._price_from_result_pct(side, entry_price, exit_pct)
            return ExitDecision(
                exit=True,
                reason="protective_breakeven_profit_guard",
                exit_price=round(exit_price, 8),
                note=(
                    f"mfe={round(mfe, 4)} current={round(current_pct, 4)} "
                    f"protect_start={round(protect_start, 4)} "
                    f"net_safe={round(net_safe_pct, 4)} "
                    f"src={threshold_source} fee_src={fee_source}"
                ),
            )

        # ------------------------------------------------------------------
        # Guard 2: Protective trailing
        # Сделка дала >= protect_start и отдала > PROTECTIVE_DRAWDOWN_SHARE.
        # Защищаем 35% от достигнутого MFE — даём тренду дышать.
        # ------------------------------------------------------------------
        protective_drawdown_share = float(settings.PROTECTIVE_DRAWDOWN_SHARE)

        if mfe >= protect_start and drawdown_from_mfe >= mfe * protective_drawdown_share:
            protected_pct = max(mfe * 0.35, net_safe_pct, min_protective_exit_pct)

            est_net = self._estimated_net_usdt(protected_pct, position_notional_usdt)
            if est_net is not None and est_net < min_protective_net_usdt:
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
                    f"protect_start={round(protect_start, 4)} "
                    f"src={threshold_source} fee_src={fee_source}"
                ),
            )

        # ------------------------------------------------------------------
        # Guard 3: Adaptive trailing
        # Сделка прошла >= trail_start и резко откатила.
        # Защищаем 45% MFE — более агрессивная защита на сильном движении.
        # ------------------------------------------------------------------
        adaptive_drawdown = float(settings.ADAPTIVE_TRAIL_DRAWDOWN_PCT)

        if mfe >= trail_start and drawdown_from_mfe >= adaptive_drawdown:
            protected_pct = max(mfe * 0.45, net_safe_pct, min_protective_exit_pct)

            est_net = self._estimated_net_usdt(protected_pct, position_notional_usdt)
            if est_net is not None and est_net < min_protective_net_usdt:
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
                    f"trail_start={round(trail_start, 4)} "
                    f"src={threshold_source} fee_src={fee_source}"
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
    ) -> ExitDecision:
        """
        Защита после TP1.

        После TP1 позиция уже прибыльная. Цель — дать ей дойти до TP2
        и не закрывать на обычном рыночном откате.

        Все пороги также динамические — относительно stop_distance.
        """
        side          = str(side).lower()
        lifecycle     = lifecycle or {}
        entry_price   = float(entry_price)
        current_price = float(current_price)
        tp2_price     = float(tp2_price)

        current_pct   = self._result_pct(side, entry_price, current_price)
        tp2_pct       = self._result_pct(side, entry_price, tp2_price)
        mfe           = float(lifecycle.get("mfe_pct") or current_pct or 0.0)
        drawdown_from_mfe = self._drawdown_from_mfe(current_pct, mfe)

        net_safe_pct, fee_source = self._net_safe_profit_pct(symbol=symbol, market_type=market_type)
        min_post_tp1_exit_pct    = float(getattr(settings, "MIN_POST_TP1_EXIT_PCT", 0.35))

        # Рассчитываем stop_distance для динамических порогов
        if stop_price is not None and float(stop_price) > 0:
            stop_distance_pct = abs(entry_price - float(stop_price)) / entry_price * 100
            threshold_source  = f"dynamic(stop={round(stop_distance_pct, 3)}%)"
        else:
            stop_distance_pct = None
            threshold_source  = "static_fallback"

        # ------------------------------------------------------------------
        # 1. TP2 достигнут — фиксируем немедленно
        # При 92%+ расстояния до TP2 считаем что уровень пробит.
        # Это защита от последнего тика, который может не дойти до TP2.
        # ------------------------------------------------------------------
        if tp2_pct > 0 and current_pct >= tp2_pct * 0.92:
            return ExitDecision(
                exit=True,
                reason="tp2_reached",
                exit_price=round(float(tp2_price), 8),
                note=f"current_pct={round(current_pct, 4)} tp2_pct={round(tp2_pct, 4)}",
            )

        # ------------------------------------------------------------------
        # 2. Защита после TP1 — базовая
        # После TP1 не отдаём больше 40% достигнутого MFE.
        # Ждём более глубокого движения прежде чем защищаться —
        # mfe >= 1.5 вместо 1.0 в v1, чтобы не резать трендовые позиции.
        # ------------------------------------------------------------------
        if mfe >= 1.5 and drawdown_from_mfe >= mfe * 0.40:
            protected_pct = max(mfe * 0.45, net_safe_pct, min_post_tp1_exit_pct)
            exit_price    = self._price_from_result_pct(side, entry_price, protected_pct)
            return ExitDecision(
                exit=True,
                reason="adaptive_post_tp1_stop",
                exit_price=round(exit_price, 8),
                note=(
                    f"mfe={round(mfe, 4)} current={round(current_pct, 4)} "
                    f"drawdown={round(drawdown_from_mfe, 4)} "
                    f"protected={round(protected_pct, 4)} "
                    f"net_safe={round(net_safe_pct, 4)} fee_src={fee_source}"
                ),
            )

        # ------------------------------------------------------------------
        # 3. Трендовая защита — сильное движение
        # Если MFE >= 3% и рынок отдал 30% — фиксируем 60% от пика.
        # Повышен порог с 2% до 3% — не мешаем нормальному тренду.
        # ------------------------------------------------------------------
        if mfe >= 3.0 and drawdown_from_mfe >= mfe * 0.30:
            protected_pct = max(mfe * 0.60, net_safe_pct, min_post_tp1_exit_pct)
            exit_price    = self._price_from_result_pct(side, entry_price, protected_pct)
            return ExitDecision(
                exit=True,
                reason="trend_trailing_stop",
                exit_price=round(exit_price, 8),
                note=(
                    f"mfe={round(mfe, 4)} current={round(current_pct, 4)} "
                    f"drawdown={round(drawdown_from_mfe, 4)} "
                    f"protected={round(protected_pct, 4)} "
                    f"net_safe={round(net_safe_pct, 4)} "
                    f"src={threshold_source} fee_src={fee_source}"
                ),
            )

        # ------------------------------------------------------------------
        # 4. Динамическая защита относительно стопа — широкие позиции
        # Для TON-подобных сетапов со стопом >3%:
        # если прошли > 80% расстояния до TP2 и откат > 20% MFE — фиксируем.
        # ------------------------------------------------------------------
        if stop_distance_pct is not None and stop_distance_pct >= 3.0:
            tp2_progress = current_pct / tp2_pct if tp2_pct > 0 else 0
            if tp2_progress >= 0.80 and drawdown_from_mfe >= mfe * 0.20:
                protected_pct = max(mfe * 0.70, net_safe_pct, min_post_tp1_exit_pct)
                exit_price    = self._price_from_result_pct(side, entry_price, protected_pct)
                return ExitDecision(
                    exit=True,
                    reason="wide_stop_tp2_guard",
                    exit_price=round(exit_price, 8),
                    note=(
                        f"mfe={round(mfe, 4)} current={round(current_pct, 4)} "
                        f"tp2_progress={round(tp2_progress, 3)} "
                        f"drawdown={round(drawdown_from_mfe, 4)} "
                        f"protected={round(protected_pct, 4)} "
                        f"src={threshold_source}"
                    ),
                )

        return ExitDecision(exit=False)