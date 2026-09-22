import logging
from dataclasses import dataclass

from core.config import settings
from core.logging import get_logger, log_event
from services.exchange_factory import get_exchange_client

logger = get_logger(__name__)


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

    def __init__(self, exchange: str | None = None):
        # (#okx-satellite-exchange-routing-2026-09-02) см. CostEngine.__init__ —
        # exchange=None сохраняет текущее поведение, явный exchange нужен для
        # ведения уже открытого сигнала под ЕГО биржей.
        self.htx = get_exchange_client(exchange)

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

    def _achievable_pct(self, protected_pct: float, current_pct: float) -> float:
        """(#phantom-fill-2026-07-25) Реально достижимый результат выхода.

        Защитные/трейл-ветки считают `protected_pct` как
        `max(доля_от_MFE, net_safe_pct, MIN_PROTECTIVE_EXIT_PCT)`. Полы
        `net_safe_pct`/`MIN_PROTECTIVE_EXIT_PCT` — это ЭКОНОМИЧЕСКИЕ ГЕЙТЫ
        («не стоит фиксировать дешевле»), но они попадали прямо в цену филла:
        `exit_price = entry × (1 + protected_pct/100)`. При MIN_PROTECTIVE_EXIT_PCT
        =1.80 и реальном MFE 0.97% сделка закрывалась по цене, которой рынок
        НИКОГДА не видел (TRX #281: филл 0.334561 при максимуме 0.331845 —
        ровно entry×1.018; +8.18 USDT вместо честных ~+1.6).

        Стоп/трейл не может исполниться ЛУЧШЕ рынка: фактический результат
        ограничен текущей ценой. min() корректен для обеих сторон, т.к.
        `_result_pct` уже нормализует знак по side.
        """
        return min(float(protected_pct), float(current_pct))

    def _estimated_net_usdt(
        self,
        result_pct: float,
        position_notional_usdt: float | None,
        fee_rate: float | None = None,
    ) -> float | None:
        # (#audit-cost-model) Ставка комиссии — рыночная (spot/swap), а не
        # захардкоженный SPOT_TAKER_FEE: для swap спот-ставка завышала издержки
        # в 4 раза и глушила защитные выходы через min_protective_net_usdt.
        if position_notional_usdt is None or position_notional_usdt <= 0:
            return None
        rate = float(fee_rate) if fee_rate is not None else float(settings.SPOT_TAKER_FEE)
        gross = position_notional_usdt * (result_pct / 100.0)
        fees = position_notional_usdt * rate * 2
        slippage = position_notional_usdt * float(getattr(settings, "SLIPPAGE_BUFFER_PCT", 0.0))
        return gross - fees - slippage

    def _fee_rate(self, symbol: str | None, market_type: str | None = None) -> tuple[float, str]:
        # Дефолт — рынок ИСПОЛНЕНИЯ (swap при ENABLE_FUTURES_EXECUTION), а не
        # MARKET_TYPE данных: расхождение этих двух и было источником спот-комиссий
        # в swap-расчётах (#audit-cost-model).
        market_type_value = market_type or getattr(settings, "execution_market_type", settings.MARKET_TYPE)
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

    def _net_safe_floor_pct(self, fee_rate: float) -> float:
        """Пол net-safe: спот (fee ≥0.1%/сторона) — 0.60%; деривативы (0.02–0.05%)
        — 0.30%. Единый 0.60% для swap завышал все защитные пороги вдвое."""
        if fee_rate <= 0.001:
            return float(getattr(settings, "NET_SAFE_FLOOR_SWAP_PCT", 0.30))
        return float(getattr(settings, "NET_SAFE_FLOOR_SPOT_PCT", 0.60))

    def _net_safe_profit_pct(self, symbol: str | None = None, market_type: str | None = None) -> tuple[float, str, float]:
        """Minimum price move that should cover fees, slippage and a safety buffer.
        Возвращает (net_safe_pct, fee_source, fee_rate)."""
        fee_rate, fee_source = self._fee_rate(symbol, market_type)
        calculated = fee_rate * 2 * 100 + float(settings.SLIPPAGE_BUFFER_PCT) * 100 + 0.15
        floor = self._net_safe_floor_pct(fee_rate)
        return round(max(calculated, floor), 4), fee_source, fee_rate

    def _dynamic_thresholds(
        self, 
        stop_distance_pct: float | None, 
        net_safe_floor: float = 0.60,
        atr: float | None = None,
        price: float | None = None
    ) -> dict:
        """Расчет пороговых значений.
        
        Если TZ_USE_DYNAMIC_ATR_STOPS=true и передан ATR, пороги считаются от волатильности.
        Иначе — от фиксированной дистанции стопа (legacy).
        """
        use_atr = bool(getattr(settings, "TZ_USE_DYNAMIC_ATR_STOPS", False))
        
        # Конвертируем ATR в проценты, если он передан
        atr_pct = None
        if use_atr and atr is not None and price is not None and price > 0:
            atr_pct = (atr / price) * 100.0

        mfe_absolute_min = float(
            getattr(settings, "FAILED_SETUP_MFE_ABSOLUTE_MIN_PCT", self.DEFAULT_MFE_ABSOLUTE_MIN_FOR_GUARD)
        )

        # Если используем ATR, берем множители из конфига, иначе — старые коэффициенты от stop_distance
        if use_atr and atr_pct is not None:
            # Динамические пороги на основе ATR
            sd = atr_pct  # Базовая единица измерения теперь ATR%
            
            # Множители из конфига (с фоллбэком на классические константы, если нет новых переменных)
            k_failed_soft = float(getattr(settings, "ATR_FAILED_SETUP_SOFT_MULT", self.K_FAILED_SOFT))
            k_failed_mid = float(getattr(settings, "ATR_FAILED_SETUP_MID_MULT", self.K_FAILED_MID))
            k_failed_deep = float(getattr(settings, "ATR_FAILED_SETUP_DEEP_MULT", self.K_FAILED_DEEP))
            k_protect = float(getattr(settings, "ATR_PROTECT_START_MULT", self.K_PROTECT))
            k_trail = float(getattr(settings, "ATR_TRAIL_START_MULT", self.K_TRAIL))
            k_capture = float(getattr(settings, "ATR_CAPTURE_START_MULT", self.K_CAPTURE))
            
            # Абсолютные кэпы для потерь (можно тоже сделать динамическими, но пока оставим как есть или привяжем к ATR)
            abs_cap_soft = abs(float(getattr(settings, "FAILED_SETUP_LOSS_SOFT_PCT", -0.40)))
            abs_cap_mid = abs(float(getattr(settings, "FAILED_SETUP_LOSS_MID_PCT", -0.65)))
            abs_cap_deep = abs(float(getattr(settings, "FAILED_SETUP_LOSS_DEEP_PCT", -0.90)))

            return {
                "failed_mfe_soft": max(sd * k_failed_soft, mfe_absolute_min),
                "failed_mfe_mid": max(sd * k_failed_mid, mfe_absolute_min),
                "failed_mfe_deep": max(sd * k_failed_deep, mfe_absolute_min),
                "failed_loss_soft": -min(sd * k_failed_soft, abs_cap_soft), # Используем тот же множитель для простоты или отдельный
                "failed_loss_mid": -min(sd * k_failed_mid, abs_cap_mid),
                "failed_loss_deep": -min(sd * k_failed_deep, abs_cap_deep),
                "protect_start": max(sd * k_protect, net_safe_floor, float(settings.PROTECTIVE_MFE_START_PCT)),
                "trail_start": max(sd * k_trail, net_safe_floor + 0.40),
                "capture_start": max(sd * k_capture, net_safe_floor + 0.20),
                "mfe_absolute_min": mfe_absolute_min,
                "source": f"dynamic_atr(mult={k_failed_soft})"
            }
        else:
            # Legacy логика (от фиксированного stop_distance_pct)
            sd = abs(float(stop_distance_pct or 0.0))
            abs_cap_soft = abs(float(getattr(settings, "FAILED_SETUP_LOSS_SOFT_PCT", -0.40)))
            abs_cap_mid = abs(float(getattr(settings, "FAILED_SETUP_LOSS_MID_PCT", -0.65)))
            abs_cap_deep = abs(float(getattr(settings, "FAILED_SETUP_LOSS_DEEP_PCT", -0.90)))
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
                "source": f"legacy_fixed(stop={round(sd, 3)}%)"
            }

    def _get_thresholds(
        self, 
        stop_distance_pct: float | None, 
        net_safe_floor: float = 0.60,
        atr: float | None = None,
        price: float | None = None
    ) -> tuple[dict, str]:
        # Приоритет: если передан ATR и включен режим, игнорируем stop_distance_pct
        if atr is not None and price is not None:
             return self._dynamic_thresholds(stop_distance_pct, net_safe_floor, atr, price), "dynamic_atr"
        
        # Фоллбэк на старую логику
        if stop_distance_pct is not None and stop_distance_pct > 0:
            return self._dynamic_thresholds(stop_distance_pct, net_safe_floor), f"dynamic(stop={round(stop_distance_pct, 3)}%)"
        
        fallback_stop = 1.5
        return self._dynamic_thresholds(fallback_stop, net_safe_floor), "dynamic_fallback(stop=1.5%+capped)"

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
        trade_mode: str = "default",
        flow_against: bool = False,
        regime: str | None = None,
        tz_context: dict | None = None,
    ) -> ExitDecision:
        side = str(side).lower()
        entry_price = float(entry_price)
        current_price = float(current_price)
        mfe = float(mfe_pct or 0.0)
        current_pct = self._result_pct(side, entry_price, current_price)
        drawdown_from_mfe = self._drawdown_from_mfe(current_pct, mfe)

        # 1. Абсолютный Fail-Open по ликвидности (спреду)
        if symbol:
            try:
                from services.liquidity_guard import LIQUIDITY_GUARD
                if LIQUIDITY_GUARD.exit_suppressed(symbol):
                    return ExitDecision(exit=False, note="liq_spread_spike_hold")
            except Exception:
                pass

        # Расчет базовых экономических параметров
        net_safe_pct, fee_source, fee_rate = self._net_safe_profit_pct(symbol=symbol, market_type=market_type)
        be_enabled = bool(getattr(settings, "BREAKEVEN_LOCK_ENABLED", True))
        be_arm = float(getattr(settings, "BREAKEVEN_LOCK_ARM_PCT", 0.35))
        be_floor = float(getattr(settings, "BREAKEVEN_LOCK_FLOOR_PCT", 0.18))
        be_hard_floor = float(getattr(settings, "BREAKEVEN_LOCK_HARD_FLOOR_PCT", -0.10))

        # К КРИТИЧЕСКИЙ ПРАВИЛО ЗАЩИТЫ КАПИТАЛА (Выносим BE_LOCK ПЕРЕД индикаторами ТЗ)
        # Если сделка была в хорошем плюсе, но откатила к безубытку — КРОЕМ ОДИНАКОВО для тренда и скальпа!
        if be_enabled and mfe >= be_arm:
            if current_pct <= be_floor or current_pct <= be_hard_floor:
                return ExitDecision(
                    exit=True,
                    reason="breakeven_lock",
                    exit_price=current_price,
                    note=f"CRITICAL_BE_RESCUE: mfe={mfe:.4f}>={be_arm} cur={current_pct:.4f}<=floor={be_floor}"
                )

        # 2. И только теперь отдаем управление приборам ТЗ (если это тренд)
        if (
            bool(getattr(settings, "TZ_TREND_EXIT_ONLY", False))
            and str(trade_mode or "").lower() in ("trend", "trend_up", "trend_down", "ride")
            and isinstance(tz_context, dict)
        ):
            try:
                from services.tz_trend_exit import evaluate as tz_exit_eval

                verdict = tz_exit_eval(
                    side=side,
                    close=current_price,
                    kama=tz_context.get("kama"),
                    adx=tz_context.get("adx"),
                    adx_peak=tz_context.get("adx_peak"),
                    obv=tz_context.get("obv"),
                    obv_ema=tz_context.get("obv_ema20"),
                    atr=tz_context.get("atr"),  # Передаем ATR для динамического буфера
                    entry_price=entry_price,  # Передаем цену входа для аварийного стопа                   
                )
                if verdict.exit:
                    return ExitDecision(
                        exit=True,
                        reason=verdict.reason,
                        exit_price=current_price,
                        note=(
                            f"{verdict.reason} triggers={','.join(verdict.triggers)} "
                            f"kama={verdict.kama} adx={verdict.adx} peak={verdict.adx_peak}"
                        ),
                    )

                # (#tz-mfe-giveback-backstop-2026-09-02) Структурные условия ТЗ
                # отвечают на вопрос «жив ли тренд», а не «сколько прибыли уже
                # отдано». Сделка может показать реальный MFE и скатиться почти
                # к безубытку/минусу, пока структура формально ещё цела (пик ADX
                # не дошёл до TZ_EXIT_ADX_PEAK_MIN, KAMA не пробита буфером) —
                # ни один структурный триггер не срабатывает, и вся
                # промежуточная прибыль отдаётся без единого механизма защиты
                # (подтверждено на боевых траекториях: XRP #452 MFE 1.04%→-1.08%,
                # ADA #453 MFE 0.32%→-0.75%, AVAX #451 MFE 0.065%→-0.66%).
                #
                # Точечный бэкстоп: НЕ трогает структурную логику (KAMA/ADX/OBV
                # остаются приоритетными — эта проверка идёт уже ПОСЛЕ verdict.exit
                # выше). Фиксирует по текущей цене только если сделка (а) показала
                # значимый MFE и (б) отдала бОльшую его часть, скатившись в район
                # net_safe (то есть отдача уже съела экономический смысл держать
                # дальше) — а не при любом откате от пика, чтобы не резать
                # здоровые тренды, которые просто «дышат».
                if bool(getattr(settings, "TZ_MFE_GIVEBACK_BACKSTOP_ENABLED", True)):
                    backstop_min_mfe = float(getattr(settings, "TZ_MFE_GIVEBACK_MIN_MFE_PCT", 0.5))
                    backstop_share = float(getattr(settings, "TZ_MFE_GIVEBACK_SHARE", 0.75))
                    if (
                        mfe >= backstop_min_mfe
                        and drawdown_from_mfe >= mfe * backstop_share
                        and current_pct <= net_safe_pct
                    ):
                        est_net = self._estimated_net_usdt(
                            current_pct, position_notional_usdt, fee_rate=fee_rate
                        )
                        if est_net is None or est_net >= float(
                            getattr(settings, "MIN_PROTECTIVE_NET_USDT", 1.50)
                        ):
                            return ExitDecision(
                                exit=True,
                                reason="tz_mfe_giveback_backstop",
                                exit_price=current_price,
                                note=(
                                    f"tz_mfe_giveback_backstop mfe={mfe:.4f} "
                                    f"cur={current_pct:.4f} dd={drawdown_from_mfe:.4f} "
                                    f">= {backstop_share}*mfe net_safe={net_safe_pct:.4f}"
                                ),
                            )

                # Тренд цел — держим. Прежние ярусы намеренно НЕ вызываются:
                # именно они давали capture 7.08% при среднем ходе 0.816%.
                return ExitDecision(
                    exit=False,
                    note=f"tz_trend_intact triggers={','.join(verdict.triggers) or 'none'}",
                )
            except Exception as exc:  # noqa: BLE001
                # Сбой расчёта не имеет права держать позицию вслепую —
                # проваливаемся на прежнюю лестницу как на бэкстоп.
                log_event(logger, logging.WARNING, "tz_exit_failed",
                          symbol=symbol, error=f"{type(exc).__name__}: {exc}")

        # ── Скальп-режим: безубыток-замок (до failed_setup_exit) ─────────────
        # Маленькая позиция + мелкое движение: тренд-пороги capture/protective
        # не вооружаются, и зелёный скальп отдаёт прибыль и переворачивается в
        # минус. Трейлим от MFE: как только отдали заданную долю пика — выходим
        # в текущую цену (реальный филл), фиксируя остаток прибыли.
        scalp = (
            bool(getattr(settings, "SCALP_BREAKEVEN_ENABLED", True))
            and str(trade_mode or "default").lower() in ("scalp", "range")
        )
        if scalp:
            scalp_arm = float(getattr(settings, "SCALP_BREAKEVEN_ARM_PCT", 0.5))
            scalp_give = float(getattr(settings, "SCALP_BREAKEVEN_GIVEBACK_SHARE", 0.5))
            # (#geometry-arm-2026-07-09) Порог замка масштабируется геометрией
            # сделки: не режем сделку с целью 2% на движении 0.2% (издержки
            # ~0.15% съедали такие фиксации в ноль — #216/#217). Эффективный
            # arm = max(абсолютный, доля дистанции до TP1).
            _arm_share = float(getattr(settings, "SCALP_BE_ARM_TP1_SHARE", 0.30))
            if tp1_dist_pct is not None and _arm_share > 0:
                scalp_arm = max(scalp_arm, tp1_dist_pct * _arm_share)
            gave_back = drawdown_from_mfe >= mfe * scalp_give
            # Поток сделок развернулся против позиции (CVD) → не ждём полного
            # отката, фиксируем у пика. Иначе — обычный трейл-замок.
            if mfe >= scalp_arm and (gave_back or flow_against):
                reason = "scalp_breakeven_lock" if gave_back else "scalp_flow_exit"
                return ExitDecision(
                    exit=True,
                    reason=reason,
                    exit_price=current_price,
                    note=(
                        f"{reason} mfe={mfe:.4f} cur={current_pct:.4f} "
                        f"dd={drawdown_from_mfe:.4f} arm={scalp_arm} give={scalp_give} flow_against={flow_against}"
                    ),
                )
            # ── Скальп тайм-стоп (профиль SCALP: быстро или никак) ───────────
            # Скальп — много мелких быстрых сделок. Если за N минут он так и не
            # вооружился (mfe < arm), это «зависшая» сделка — закрываем.
            #
            # (#audit-time-stop) Cost-aware grace: рубить по ЧИСТОМУ возрасту
            # оказалось дорого (AAVE #181: MFE +0.16% → рыночное закрытие −0.58%
            # + 25 мин кулдауна, следом та же сторона дала +1.35). Если сделка
            # НЕ в значимом минусе (|cur| < net_safe) И поток не против —
            # даём дожить до жёсткого стопа (mult × базовый). Реальный минус
            # или CVD-разворот закрывают сразу, как раньше.
            if bool(getattr(settings, "SCALP_TIME_STOP_ENABLED", True)):
                # (#range-time-stop-2026-07-09) Range-геометрия (стоп ~2.4%, TP1 ~2%)
                # не разрешается за 45 минут микро-скальпа — даём диапазону 90.
                if str(regime or "").lower() == "range":
                    ts_sec = float(getattr(settings, "RANGE_TIME_STOP_MIN", 90.0)) * 60.0
                else:
                    ts_sec = float(getattr(settings, "SCALP_TIME_STOP_MIN", 45.0)) * 60.0
                hard_mult = max(float(getattr(settings, "SCALP_TIME_STOP_HARD_MULT", 2.0)), 1.0)
                hard_sec = ts_sec * hard_mult
                if (
                    signal_age_sec is not None
                    and float(signal_age_sec) >= ts_sec
                    and mfe < scalp_arm
                ):
                    age = float(signal_age_sec)
                    not_losing = current_pct > -net_safe_pct
                    showed_life = mfe >= scalp_arm * 0.5
                    # (#exit-replay-2026-07-09) OR→AND: grace только сделкам, которые
                    # И не в минусе, И показали жизнь (MFE ≥ arm/2). Прежний OR
                    # продлевал болтающиеся у нуля сделки до hard-стопа, где они
                    # закрывались в минус (SOL #213/#214).
                    grace = age < hard_sec and not flow_against and (not_losing and showed_life)
                    if not grace:
                        return ExitDecision(
                            exit=True,
                            reason="scalp_time_stop",
                            exit_price=current_price,
                            note=(
                                f"scalp_time_stop age={age:.0f}s>={ts_sec:.0f}s "
                                f"(hard={hard_sec:.0f}s) mfe={mfe:.4f}<arm={scalp_arm} "
                                f"cur={current_pct:.4f} flow_against={flow_against}"
                            ),
                        )

        # ── Безубыток-замок (#1/#2) ──────────────────────────────────────────
        # Как только сделка показала значимый MFE, она НЕ имеет права закрыться
        # глубоко в минус через failed_setup_exit ниже. Если профит откатил к
        # полу (floor) — фиксируем здесь, у безубытка, сохраняя комиссии.
        # Это напрямую лечит positive_then_negative (был 50-64%).
        be_enabled = bool(getattr(settings, "BREAKEVEN_LOCK_ENABLED", True))
        be_arm = float(getattr(settings, "BREAKEVEN_LOCK_ARM_PCT", 0.45))
        # (#be-floor-cost-2026-07-25) Пол замка ДОЛЖЕН покрывать round-trip издержки,
        # иначе «безубыток» математически не может закрыться в плюс.
        # Факт по телеметрии 19–25.07: round-trip = 0.15% нотионала, а пол стоял
        # 0.10% (откат 0.15→0.10 в коммите 4df5092 от 17.07 отменил фикс #leak-be-lock).
        # Выход при gross ≤ 0.10% → net = 0.10 − 0.15 = −0.05%. Проверено на 7 из
        # последних 20 закрытий: #282 −0.056, #279 −0.068, #275 −0.155, #273 −0.055,
        # #270 −0.102, #268 −0.071, #267 −0.061 — все ровно gross − 0.15.
        # Берём фактическую стоимость round-trip (комиссии обеих ног + слиппедж)
        # плюс буфер: замок обязан оставлять хотя бы символический плюс.
        be_cost_pct = (float(fee_rate or 0.0) * 2 + float(settings.SLIPPAGE_BUFFER_PCT)) * 100
        be_floor = max(
            float(getattr(settings, "BREAKEVEN_LOCK_FLOOR_PCT", 0.18)),
            be_cost_pct + float(getattr(settings, "BREAKEVEN_LOCK_COST_BUFFER_PCT", 0.05)),
        )
        # (#wick) Вик-фильтр: мягкие выходы только при подтверждённом развороте
        # (flow_against) ИЛИ реальном уходе в минус. Иначе тонкий откат = вик, держим.
        require_flow = bool(getattr(settings, "EXIT_REQUIRE_FLOW_CONFIRM", False))
        be_hard_floor = float(getattr(settings, "BREAKEVEN_LOCK_HARD_FLOOR_PCT", -0.10))
        soft_exit_confirmed = (not require_flow) or bool(flow_against)
        # (#2 консолидация экзита) В ТРЕНДЕ failed_setup_exit отключён: он рубил в
        # шумовой полосе РАНЬШЕ структурного smart-стопа, часто на вике, после
        # которого цена шла дальше. Бэкстоп тренда = smart-stop (за уровнем) +
        # breakeven_lock (после хорошего MFE) + ride-трейл. failed_setup остаётся
        # только для non-trend режимов (default), где нет ride/structure-стопа.
        is_trend_mode = str(trade_mode or "default").lower() in ("trend", "trend_up", "trend_down", "ride")
        failed_setup_enabled = (not is_trend_mode) or bool(
            getattr(settings, "FAILED_SETUP_EXIT_TREND_ENABLED", False)
        )
        # Замок обязан иметь запас между вооружением и полом, иначе он
        # срабатывает мгновенно после arm и вырождается в «выход по шуму».
        be_arm = max(be_arm, be_floor * float(getattr(settings, "BREAKEVEN_LOCK_ARM_FLOOR_RATIO", 1.2)))

        # (#be-lock-preempts-ride-2026-08-26 — ОТМЕНЕНО В ТОТ ЖЕ ДЕНЬ)
        #
        # Здесь я поднимал `be_arm` в тренде до TREND_RIDE_MIN_MFE_TO_PROTECT_PCT,
        # считая, что замок перехватывает ride. Правка была неверной и её поймал
        # `test_breakeven_lock_covers_the_band_below_trend_capture_arm`.
        #
        # Две ошибки в одном рассуждении:
        #
        # 1. Я привязался к `close_reason` из телеметрии — там стоит
        #    `breakeven_stop`. Но это НЕ этот блок: тот выдаёт `breakeven_lock`.
        #    `breakeven_stop` — перенос `signal.stop_price` в безубыток после
        #    TP1 (см. signal_lifecycle, ~700). Правил не тот механизм.
        # 2. Даже по своей логике правка ничего не давала: сделки с MFE 1.0–1.6%
        #    вооружали замок и при пороге 0.35, и при 0.8 — выход всё равно шёл
        #    по ПОЛУ 0.18%. Отдачу задаёт пол, а не порог вооружения. Зато в
        #    полосе 0.35–0.8 правка снимала единственную защиту.
        #
        # Настоящая отдача: после TP1 остаток охраняется безубытком и НЕ
        # подтягивается за MFE. Сделка идёт до +1.6% и возвращается к входу —
        # вторая половина даёт ноль. Чинить надо там, а не здесь.
        breakeven_armed = be_enabled and mfe >= be_arm
        if breakeven_armed and current_pct <= be_floor and (soft_exit_confirmed or current_pct <= be_hard_floor):
            # Фиксируем по текущей цене (реальный филл). После хорошего MFE это
            # около безубытка, а не -0.6/-0.9%, куда дотягивал failed_setup_exit.
            return ExitDecision(
                exit=True,
                reason="breakeven_lock",
                exit_price=current_price,
                note=(
                    f"breakeven_lock mfe={mfe:.4f}>=arm={be_arm} "
                    f"cur={current_pct:.4f}<=floor={be_floor} flow_against={flow_against}"
                ),
            )

        if failed_setup_enabled and mfe_pct is not None and age_ok and mfe >= thr["mfe_absolute_min"]:
            # soft/mid failed_setup — под вик-фильтром (мелкий минус без подтверждения
            # потоком = вик). deep — глубокий неблагоприятный ход. Весь блок отключён
            # в тренде (см. failed_setup_enabled выше): там бэкстоп = smart-stop.
            if soft_exit_confirmed and mfe < thr["failed_mfe_soft"] and current_pct <= thr["failed_loss_soft"]:
                return ExitDecision(
                    exit=True,
                    reason="failed_setup_exit",
                    exit_price=current_price,
                    note=f"soft: mfe={mfe:.4f}<{thr['failed_mfe_soft']:.4f} loss={current_pct:.4f}<={thr['failed_loss_soft']:.4f} flow={flow_against} src={threshold_source}",
                )
            if soft_exit_confirmed and mfe < thr["failed_mfe_mid"] and current_pct <= thr["failed_loss_mid"]:
                return ExitDecision(
                    exit=True,
                    reason="failed_setup_exit",
                    exit_price=current_price,
                    note=f"mid: mfe={mfe:.4f}<{thr['failed_mfe_mid']:.4f} loss={current_pct:.4f}<={thr['failed_loss_mid']:.4f} flow={flow_against} src={threshold_source}",
                )
            if mfe < thr["failed_mfe_deep"] and current_pct <= thr["failed_loss_deep"]:
                return ExitDecision(
                    exit=True,
                    reason="failed_setup_exit",
                    exit_price=current_price,
                    note=f"deep: mfe={mfe:.4f}<{thr['failed_mfe_deep']:.4f} loss={current_pct:.4f}<={thr['failed_loss_deep']:.4f} src={threshold_source}",
                )

        # ── Трендовый режим (ride) ────────────────────────────────────────────
        # Едем движение: НЕ выходим у безубытка на микроплюсе и не фиксируем ранний
        # capture. Держим, пока MFE не отдаст широкую долю (trail_dd) — иначе hold.
        # Hard stop и failed_setup_exit (выше) продолжают защищать от убытка.
        ride = (
            bool(getattr(settings, "TREND_RIDE_ENABLED", True))
            and str(trade_mode or "default").lower() in ("trend", "trend_up", "trend_down", "ride")
        )
        if ride:
            ride_min_mfe = float(getattr(settings, "TREND_RIDE_MIN_MFE_TO_PROTECT_PCT", 1.2))
            ride_trail_dd = float(getattr(settings, "TREND_RIDE_TRAIL_DRAWDOWN_PCT", 0.50))
            if mfe >= ride_min_mfe and drawdown_from_mfe >= mfe * ride_trail_dd:
                protected_pct = max(mfe * (1.0 - ride_trail_dd), net_safe_pct, min_protective_exit_pct)
                est_net = self._estimated_net_usdt(protected_pct, position_notional_usdt, fee_rate=fee_rate)
                if est_net is not None and est_net < min_protective_net_usdt:
                    return ExitDecision(exit=False)
                protected_pct = self._achievable_pct(protected_pct, current_pct)
                exit_price = self._price_from_result_pct(side, entry_price, protected_pct)
                return ExitDecision(
                    exit=True, reason="trend_ride_trailing_stop",
                    exit_price=round(exit_price, 8),
                    note=(
                        f"ride mfe={mfe:.4f} cur={current_pct:.4f} dd={drawdown_from_mfe:.4f} "
                        f"prot={protected_pct:.4f} min_mfe={ride_min_mfe} trail_dd={ride_trail_dd}"
                    ),
                )

            # ── Ярус 2: фиксация в «мёртвой зоне» MFE (#trend-capture-band-2026-07-25)
            # Выше стоит ранний `return ExitDecision(exit=False)`, поэтому
            # adaptive_mfe_capture в тренде НЕДОСТИЖИМ. Сделки с MFE в полосе
            # [arm, ride_min_mfe) не имели ни одного механизма фиксации, кроме
            # безубыток-замка — а это модальный случай (медиана MFE тренда 0.64%,
            # ride требует 0.8%). Здесь — тугой трейл ИМЕННО в этой полосе:
            # как только mfe >= ride_min_mfe, ярус 2 отключается и работает
            # широкий ride-трейл выше (раннеров не режем).
            if bool(getattr(settings, "TREND_CAPTURE_BAND_ENABLED", True)):
                band_arm = float(getattr(settings, "TREND_CAPTURE_ARM_PCT", 0.55))
                band_give = float(getattr(settings, "TREND_CAPTURE_GIVEBACK_SHARE", 0.25))
                if (
                    band_arm <= mfe < ride_min_mfe
                    and drawdown_from_mfe >= mfe * band_give
                ):
                    band_pct = self._achievable_pct(mfe * (1.0 - band_give), current_pct)
                    # (#band-floor-2026-07-27) У яруса 2 СВОЙ пол, ниже общего.
                    # Общий MIN_PROTECTIVE_EXIT_PCT задаёт порог вооружения
                    # mfe >= floor/(1-give) = 0.533%, а медиана MFE в бою — 0.489%:
                    # полоса оказывалась пустой и ярус не срабатывал ни разу.
                    band_floor = max(
                        net_safe_pct,
                        float(getattr(settings, "TREND_CAPTURE_FLOOR_PCT", 0.30)),
                    )
                    if band_pct >= band_floor:
                        est_net = self._estimated_net_usdt(
                            band_pct, position_notional_usdt, fee_rate=fee_rate
                        )
                        if est_net is None or est_net >= min_protective_net_usdt:
                            exit_price = self._price_from_result_pct(side, entry_price, band_pct)
                            return ExitDecision(
                                exit=True, reason="trend_capture_band",
                                exit_price=round(exit_price, 8),
                                note=(
                                    f"band mfe={mfe:.4f} cur={current_pct:.4f} "
                                    f"dd={drawdown_from_mfe:.4f} prot={band_pct:.4f} "
                                    f"arm={band_arm} give={band_give} ride_min={ride_min_mfe}"
                                ),
                            )
            return ExitDecision(exit=False)

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
                est_net = self._estimated_net_usdt(protected_pct, position_notional_usdt, fee_rate=fee_rate)
                if est_net is not None and est_net < min_protective_net_usdt:
                    return ExitDecision(exit=False)
                protected_pct = self._achievable_pct(protected_pct, current_pct)
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
            est_net = self._estimated_net_usdt(protected_pct, position_notional_usdt, fee_rate=fee_rate)
            if est_net is not None and est_net < min_protective_net_usdt:
                return ExitDecision(exit=False)
            protected_pct = self._achievable_pct(protected_pct, current_pct)
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
            est_net = self._estimated_net_usdt(protected_pct, position_notional_usdt, fee_rate=fee_rate)
            if est_net is not None and est_net < min_protective_net_usdt:
                return ExitDecision(exit=False)
            protected_pct = self._achievable_pct(protected_pct, current_pct)
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
            est_net = self._estimated_net_usdt(protected_pct, position_notional_usdt, fee_rate=fee_rate)
            if est_net is not None and est_net < min_protective_net_usdt:
                return ExitDecision(exit=False)
            protected_pct = self._achievable_pct(protected_pct, current_pct)
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
        position_notional_usdt: float | None = None,
        signal_age_sec: float | None = None,
        tz_context: dict | None = None, # Прокидываем контекст индикаторов рынка
    ) -> ExitDecision:
        side = str(side).lower()
        lifecycle = lifecycle or {}
        entry_price = float(entry_price)
        current_price = float(current_price)
        tp2_price = float(tp2_price)
        
        current_pct = self._result_pct(side, entry_price, current_price)
        tp2_pct = self._result_pct(side, entry_price, tp2_price)
        
        # Извлекаем MFE (максимальный зафиксированный плюс по траектории)
        mfe = float(lifecycle.get("mfe_pct") or current_pct or 0.0)
        drawdown_from_mfe = self._drawdown_from_mfe(current_pct, mfe)
        
        # Считаем чистый порог окупаемости комиссий
        net_safe_pct, _, fee_rate = self._net_safe_profit_pct(symbol=symbol, market_type=market_type)
        
        # Извлекаем живой ATR из контекста рынка
        atr_v = float((tz_context or {}).get("atr", 0.0))
        
        # ── ЭТАП 1: ФИКСАЦИЯ НА ПОДХОДЕ К TP2 ──
        # Если цена дошла до 92% от цели TP2, и прогрессивный режим выключен — забираем тейк
        if tp2_pct > 0 and current_pct >= tp2_pct * 0.92 and not bool(getattr(settings, "TP2_PROGRESSIVE_ENABLED", True)):
            return ExitDecision(
                exit=True, reason="tp2_reached",
                exit_price=current_price,
                note=f"RUNNER_HIT_TP2: cur={current_pct:.2f} tp2={tp2_pct:.2f}"
            )

        # ── ЭТАП 2: ДИНАМИЧЕСКИЙ АТР-ТРЕЙЛ ДЛЯ РANНЕРОВ (ДЫХАНИЕ РЫНКА) ──
        # Если включен режим динамических стопов по ATR
        if bool(getattr(settings, "TZ_USE_DYNAMIC_ATR_STOPS", True)) and atr_v > 0 and entry_price > 0:
            # Переводим текущий ATR в проценты от цены входа
            atr_pct = (atr_v / entry_price) * 100.0
            
            # Задаем размер люфта (поводка): даем цене дышать на расстоянии 2.2 * ATR от пика
            trail_buffer_pct = atr_pct * float(getattr(settings, "ATR_TRAIL_START_MULT", 2.2))
            
            # Порог активации трейлинга: включаем защиту только когда цена ушла выше TP1 минимум на 1.5 * ATR
            if mfe >= (current_pct - atr_pct * 1.5):
                # Рассчитываем динамический уровень защищенного профита
                protected_pct = mfe - trail_buffer_pct
                
                # КРИТИЧЕСКИЙ ИНВАРИАНТ: Защитный уровень подтягивания НЕ может быть ниже безубытка (net_safe)
                # Но он плавно ползет вверх ЗА ценой, оставляя ей ОГРОМНЫЙ зазор (2.2 * ATR) для дыхания
                protected_pct = max(protected_pct, net_safe_pct)
                
                if current_pct <= protected_pct:
                    return ExitDecision(
                        exit=True,
                        reason="post_tp1_atr_trailing_stop",
                        exit_price=current_price,
                        note=f"ATR_RIDER_EXIT: mfe={mfe:.2f}% текущий={current_pct:.2f}% стоп_уровень={protected_pct:.2f}% (люфт ATR={trail_buffer_pct:.2f}%)"
                    )
                    
                return ExitDecision(exit=False, note=f"ATR_RIDING: цена дышит свободно внутри буфера {trail_buffer_pct:.2f}%")

        # ── ЭТАП 3: РЕЗЕРВНЫЙ АБСОЛЮТНЫЙ БЭКСТОП (Если ATR не готов) ──
        if bool(getattr(settings, "POST_TP1_TRAIL_ENABLED", True)):
            trail_min_mfe = float(getattr(settings, "POST_TP1_TRAIL_MIN_MFE_PCT", 0.60))
            trail_share = float(getattr(settings, "POST_TP1_TRAIL_GIVEBACK_SHARE", 0.40))
            if mfe >= trail_min_mfe and drawdown_from_mfe >= mfe * trail_share:
                protected_pct = self._achievable_pct(max(net_safe_pct, 0.0), current_pct)
                return ExitDecision(
                    exit=True, reason="post_tp1_giveback_trail",
                    exit_price=current_price,
                    note=f"BACKSTOP_EXIT: mfe={mfe:.4f} cur={current_pct:.4f}"
                )

        return ExitDecision(exit=False)

    def after_tp2_decision(
        self,
        side: str,
        entry_price: float,
        current_price: float,
        tp2_price: float,
        peak_price: float | None = None,
        symbol: str | None = None,
        market_type: str | None = None,
    ) -> ExitDecision:
        """Хвост после частичной фиксации на TP2 (#progressive-tp2-2026-09-03).

        Отдельный метод, а не ещё один ярус в after_tp1_decision: там лестница
        построена вокруг «дошли до TP2 или откатились к безубытку», и вплетать
        в неё этап, который начинается ЗА TP2, значит менять смысл её порогов.

        Держим хвост, пока он не отдал слишком много ПРИРОСТА СВЕРХ TP2. Доля
        считается именно от прироста, а не от полного MFE: иначе порог зависел
        бы от того, как далеко стоял TP2, а не от того, сколько дал сам хвост —
        при близком TP2 любая свеча выглядела бы как обвал, при дальнем не
        срабатывало бы вообще.

        Жёсткий пол (не отдать назад уже достигнутый TP2) обеспечивает не этот
        метод, а ратчет `signal.stop_price` в signal_lifecycle: он не опускается
        ниже уровня TP2 минус буфер, и срабатывает через обычный хард-стоп.
        Здесь — только выход по затуханию хвоста.
        """
        side = str(side).lower()
        entry_price = float(entry_price)
        current_price = float(current_price)
        tp2_price = float(tp2_price)

        current_pct = self._result_pct(side, entry_price, current_price)
        tp2_pct = self._result_pct(side, entry_price, tp2_price)

        if peak_price is None:
            return ExitDecision(exit=False, note="tp2_trail_no_peak")

        peak_pct = self._result_pct(side, entry_price, float(peak_price))

        # Прирост сверх TP2: сколько хвост реально добавил к уже зафиксированному.
        run_pct = peak_pct - tp2_pct
        if run_pct <= 0:
            return ExitDecision(exit=False, note=f"tp2_trail_no_run peak={peak_pct:.4f}")

        given_back = peak_pct - current_pct
        share = float(getattr(settings, "TP2_TRAIL_GIVEBACK_SHARE", 0.40))

        if given_back >= run_pct * share:
            return ExitDecision(
                exit=True,
                reason="tp2_trail_giveback",
                exit_price=round(current_price, 8),
                note=(
                    f"peak={peak_pct:.4f} tp2={tp2_pct:.4f} run={run_pct:.4f} "
                    f"cur={current_pct:.4f} back={given_back:.4f} >= {share}*run"
                ),
            )

        return ExitDecision(
            exit=False,
            note=f"tp2_trail_riding run={run_pct:.4f} back={given_back:.4f}",
        )
