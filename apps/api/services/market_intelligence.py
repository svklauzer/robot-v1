from dataclasses import dataclass, asdict
from typing import Any

import pandas as pd

from core.config import settings
from services.market_data import MarketDataService
from services.range_strategy import RangeStrategyService
from services.crt_strategy import CRTStrategyService
from core.strategy_profiles import get_profiles
from services.micro_scalp import MicroScalpService
from services.orderbook_feed import ORDERBOOK_STORE
from services.orderbook_analyzer import OrderBookAnalyzer


def _df_to_crt_candles(df, n: int = 20):
    """DataFrame OHLCV → список ЗАКРЫТЫХ свечей (последняя строка = формирующаяся,
    отбрасываем, чтобы исключить lookahead/repaint). Старые→новые."""
    try:
        if df is None or len(df) < 2:
            return []
        closed = df.iloc[:-1].tail(int(n))
        return [
            {"open": float(r["open"]), "high": float(r["high"]),
             "low": float(r["low"]), "close": float(r["close"])}
            for _, r in closed.iterrows()
        ]
    except Exception:
        return []



@dataclass
class TimeframeContext:
    timeframe: str
    last_close: float

    ema20: float
    ema50: float
    ema200: float

    rsi14: float
    macd: float
    macd_signal: float
    macd_hist: float

    atr14: float
    volume: float
    volume_ma20: float
    volume_ratio: float

    support: float
    resistance: float

    trend: str
    momentum: str
    volatility: str
    volume_state: str


@dataclass
class MarketIntelligenceResult:
    symbol: str
    source: str
    action: str
    regime: str

    entry_zone: list[float] | None
    stop_price: float | None
    tp: dict[str, float] | None

    confidence_hint: float
    reason: str

    scores: dict[str, float]
    timeframes: dict[str, dict[str, Any]]

    setup_quality: dict[str, float | str] | None
    setup_decision: str
    radar_state: str

class MarketIntelligenceEngine:
    """
    Первый настоящий слой рыночного интеллекта.

    Он не открывает сделки.
    Он анализирует рынок и возвращает candidate:
    - long
    - short
    - hold

    Дальше candidate обязан пройти:
    MLScorer / SignalQuality / TradePlan / CostEngine.
    """

    def _ctx_value(self, ctx, key: str, default=None):
        if ctx is None:
            return default

        if isinstance(ctx, dict):
            return ctx.get(key, default)

        return getattr(ctx, key, default)

    def _tf(self, contexts, timeframe: str, default=None):
        if contexts is None:
            return default

        if isinstance(contexts, dict):
            return contexts.get(timeframe, default)

        # Если передали один TimeframeContext вместо словаря.
        ctx_tf = getattr(contexts, "timeframe", None)

        if ctx_tf == timeframe:
            return contexts

        # Для legacy single-timeframe вызовов считаем один ctx как base.
        if timeframe == "base":
            return contexts

        return default        

    def __init__(self):
        self.market = MarketDataService()

    def analyze_symbol(self, symbol: str) -> MarketIntelligenceResult:
        """
        Реальный multi-timeframe анализ:
        1m  — микродвижение
        5m  — локальный импульс
        15m — рабочий сетап
        1h  — основной тренд
        4h  — старший контекст
        """

        snap = self.market.multi_timeframe_snapshot(symbol)
        source = snap.get("source", "unknown")
        tf_data = snap["timeframes"]

        contexts = {}

        for tf, df in tf_data.items():
            try:
                contexts[tf] = self._analyze_timeframe(df, tf)
            except Exception as e:
                print(f"[INTELLIGENCE TF ERROR] {symbol} {tf}: {e}")

        if not contexts:
            raise ValueError(f"No valid timeframe contexts for {symbol}")

        scores = self._score_multi_timeframe(contexts)
        regime = self._detect_multi_timeframe_regime(contexts, scores)

        candidate = self._build_multi_timeframe_candidate(
            symbol=symbol,
            source=source,
            contexts=contexts,
            scores=scores,
            regime=regime,
        )

        # ── CRT (Candle Range Theory) — приоритетнее грубого range ──────────
        # Трендовый путь не дал approve → пробуем 3-свечной CRT (свип + close-back
        # на 4h, вход на LTF по MSS/FVG). Несёт regime="crt" → trade_mode="trend".
        if bool(getattr(settings, "ENABLE_CRT_STRATEGY", False)) and (
            candidate.action == "hold" or candidate.setup_decision != "approve"
        ):
            try:
                htf_tf = str(getattr(settings, "CRT_HTF_TF", "4h"))
                ltf_tf = str(getattr(settings, "CRT_LTF_TF", "15m"))
                htf_c = _df_to_crt_candles(tf_data.get(htf_tf), 6)
                ltf_c = _df_to_crt_candles(tf_data.get(ltf_tf), 24)
                cur_px = (
                    float(tf_data[ltf_tf].iloc[-1]["close"])
                    if ltf_tf in tf_data and len(tf_data[ltf_tf])
                    else (ltf_c[-1]["close"] if ltf_c else 0.0)
                )
                def _tf_trend(tf):
                    c = contexts.get(tf) if isinstance(contexts, dict) else None
                    if c is None:
                        return ""
                    return str(c.get("trend", "") if isinstance(c, dict) else getattr(c, "trend", ""))
                crt_sig = CRTStrategyService().evaluate(
                    htf_c, ltf_c, symbol=symbol, current_price=cur_px,
                    htf_trend=_tf_trend("4h"), mtf_trend=_tf_trend("1h"),
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[CRT STRATEGY ERROR] {symbol}: {exc}")
                crt_sig = None

            if crt_sig is not None and crt_sig.setup_decision == "approve":
                candidate = MarketIntelligenceResult(
                    symbol=symbol,
                    source=source,
                    action=crt_sig.action,
                    regime=crt_sig.regime,
                    entry_zone=crt_sig.entry_zone,
                    stop_price=crt_sig.stop_price,
                    tp=crt_sig.tp,
                    confidence_hint=crt_sig.confidence_hint,
                    reason=crt_sig.reason,
                    scores=scores,
                    timeframes=candidate.timeframes,
                    setup_quality=crt_sig.setup_quality,
                    setup_decision=crt_sig.setup_decision,
                    radar_state="crt",
                )

        # ── RANGE-стратегия (скальп в боковике) ──────────────────────────────
        # Если трендовый путь не дал торгуемого кандидата (hold или не approve),
        # а 4h в боковике — пробуем range-вход от поддержки. Под флагом, OFF по
        # умолчанию. Range-сделка несёт regime="range" → trade_mode="scalp".
        if bool(getattr(settings, "ENABLE_RANGE_STRATEGY", False)) and (
            candidate.action == "hold" or candidate.setup_decision != "approve"
        ):
            try:
                range_sig = RangeStrategyService().evaluate(contexts, symbol)
            except Exception as exc:  # noqa: BLE001
                print(f"[RANGE STRATEGY ERROR] {symbol}: {exc}")
                range_sig = None

            if range_sig is not None and range_sig.setup_decision == "approve":
                candidate = MarketIntelligenceResult(
                    symbol=symbol,
                    source=source,
                    action=range_sig.action,
                    regime=range_sig.regime,
                    entry_zone=range_sig.entry_zone,
                    stop_price=range_sig.stop_price,
                    tp=range_sig.tp,
                    confidence_hint=range_sig.confidence_hint,
                    reason=range_sig.reason,
                    scores=scores,
                    timeframes=candidate.timeframes,
                    setup_quality=range_sig.setup_quality,
                    setup_decision=range_sig.setup_decision,
                    radar_state="range",
                )

        # ── SCALP (micro-flow) — последний в каскаде: 5m микроструктура + стакан ──
        # Запускается, если старшие движки не дали approve. Читает поток ордеров.
        if bool(getattr(settings, "ENABLE_SCALP_STRATEGY", False)) and (
            candidate.action == "hold" or candidate.setup_decision != "approve"
        ):
            try:
                ob_snap = ORDERBOOK_STORE.snapshot(symbol)
                if ob_snap and ob_snap.get("age_sec", 1e9) > float(getattr(settings, "OB_DATA_MAX_AGE_SEC", 15.0)):
                    ob_snap = None
                depth_sig = OrderBookAnalyzer.analyze(ob_snap, levels=int(getattr(settings, "OB_DEPTH_LEVELS", 10)))
                scalp_sig = MicroScalpService().evaluate(contexts, depth_sig.as_dict(), symbol)
            except Exception as exc:  # noqa: BLE001
                print(f"[SCALP STRATEGY ERROR] {symbol}: {exc}")
                scalp_sig = None

            if scalp_sig is not None and scalp_sig.setup_decision == "approve":
                candidate = MarketIntelligenceResult(
                    symbol=symbol,
                    source=source,
                    action=scalp_sig.action,
                    regime=scalp_sig.regime,
                    entry_zone=scalp_sig.entry_zone,
                    stop_price=scalp_sig.stop_price,
                    tp=scalp_sig.tp,
                    confidence_hint=scalp_sig.confidence_hint,
                    reason=scalp_sig.reason,
                    scores=scores,
                    timeframes=candidate.timeframes,
                    setup_quality=scalp_sig.setup_quality,
                    setup_decision=scalp_sig.setup_decision,
                    radar_state="scalp",
                )

        return candidate

    def _analyze_timeframe(self, df: pd.DataFrame, timeframe: str) -> TimeframeContext:
        work = df.copy()

        if len(work) < 60:
            raise ValueError(f"Not enough candles for analysis: {len(work)}")

        close = work["close"].astype(float)
        high = work["high"].astype(float)
        low = work["low"].astype(float)
        volume = work["volume"].astype(float)

        ema20 = self._ema(close, 20)
        ema50 = self._ema(close, 50)
        ema200 = self._ema(close, 200)

        rsi14 = self._rsi(close, 14)

        macd_line, macd_signal, macd_hist = self._macd(close)

        atr14 = self._atr(high, low, close, 14)

        # Volume confirmation должен считаться по ПОСЛЕДНЕЙ ЗАКРЫТОЙ свече.
        # ccxt fetch_ohlcv отдаёт текущую (формирующуюся) свечу последней строкой;
        # в начале периода её объём почти нулевой и хронически занижал volume_ratio,
        # из-за чего setup застревал в "wait_more_confirmation" (неделя без сигналов).
        if len(volume) >= 2:
            last_volume = float(volume.iloc[-2])
            volume_ma20 = volume.iloc[:-1].rolling(20).mean().iloc[-1]
        else:
            last_volume = float(volume.iloc[-1])
            volume_ma20 = volume.rolling(20).mean().iloc[-1]
        volume_ratio = last_volume / volume_ma20 if volume_ma20 and volume_ma20 > 0 else 0.0

        support = low.tail(50).min()
        resistance = high.tail(50).max()

        last_close = close.iloc[-1]

        trend = self._trend_state(last_close, ema20, ema50, ema200)
        momentum = self._momentum_state(rsi14, macd_hist)
        volatility = self._volatility_state(atr14, last_close)
        volume_state = self._volume_state(volume_ratio)

        return TimeframeContext(
            timeframe=timeframe,
            last_close=round(float(last_close), 8),

            ema20=round(float(ema20), 8),
            ema50=round(float(ema50), 8),
            ema200=round(float(ema200), 8),

            rsi14=round(float(rsi14), 4),
            macd=round(float(macd_line), 8),
            macd_signal=round(float(macd_signal), 8),
            macd_hist=round(float(macd_hist), 8),

            atr14=round(float(atr14), 8),
            volume=round(float(last_volume), 8),
            volume_ma20=round(float(volume_ma20), 8),
            volume_ratio=round(float(volume_ratio), 4),

            support=round(float(support), 8),
            resistance=round(float(resistance), 8),

            trend=trend,
            momentum=momentum,
            volatility=volatility,
            volume_state=volume_state,
        )

    def _score_context(self, ctx: TimeframeContext) -> dict[str, float]:
        trend_score = 50.0
        momentum_score = 50.0
        volume_score = 50.0
        structure_score = 50.0
        volatility_score = 50.0

        if ctx.trend == "trend_up":
            trend_score = 75.0
        elif ctx.trend == "trend_down":
            trend_score = 25.0
        elif ctx.trend == "flat":
            trend_score = 45.0

        if ctx.momentum == "bullish":
            momentum_score = 70.0
        elif ctx.momentum == "bearish":
            momentum_score = 30.0
        elif ctx.momentum == "overheated":
            momentum_score = 45.0
        elif ctx.momentum == "oversold":
            momentum_score = 55.0

        if ctx.volume_state == "strong":
            volume_score = 75.0
        elif ctx.volume_state == "weak":
            volume_score = 35.0

        distance_to_support = abs(ctx.last_close - ctx.support)
        distance_to_resistance = abs(ctx.resistance - ctx.last_close)
        price_range = max(ctx.resistance - ctx.support, 1e-9)

        support_position = distance_to_support / price_range
        resistance_position = distance_to_resistance / price_range

        # Хорошо для long, когда цена ближе к support, но не пробила его.
        if 0.10 <= support_position <= 0.45:
            structure_score = 70.0
        elif resistance_position < 0.12:
            structure_score = 40.0

        atr_pct = ctx.atr14 / ctx.last_close if ctx.last_close > 0 else 0.0

        if 0.002 <= atr_pct <= 0.025:
            volatility_score = 70.0
        elif atr_pct < 0.001:
            volatility_score = 35.0
        elif atr_pct > 0.04:
            volatility_score = 30.0

        total = (
            trend_score * 0.30
            + momentum_score * 0.20
            + volume_score * 0.20
            + structure_score * 0.20
            + volatility_score * 0.10
        )

        return {
            "trend": round(trend_score, 2),
            "momentum": round(momentum_score, 2),
            "volume": round(volume_score, 2),
            "structure": round(structure_score, 2),
            "volatility": round(volatility_score, 2),
            "total": round(total, 2),
        }

    def _detect_regime(self, ctx: TimeframeContext, scores: dict[str, float]) -> str:
        if ctx.volatility == "extreme":
            return "volatile"

        if ctx.trend == "trend_up" and scores["total"] >= 62:
            return "trend_up_candidate"

        if ctx.trend == "trend_down" and scores["total"] <= 42:
            return "trend_down_candidate"

        if ctx.trend == "flat":
            return "flat"

        return "mixed"

    def _build_candidate(
        self,
        symbol: str,
        source: str,
        ctx: TimeframeContext,
        scores: dict[str, float],
        regime: str,
    ) -> MarketIntelligenceResult:
        action = "hold"
        reason = "no_trade_conditions"
        entry_zone = None
        stop_price = None
        tp = None
        confidence_hint = scores["total"]   # updated below after action is known

        if regime == "trend_up_candidate":
            action = "long"
            reason = self._reason_join([
                ctx.trend,
                ctx.momentum,
                ctx.volume_state,
                "support_resistance_context",
            ])

            levels = self._build_long_levels(ctx)
            entry_zone = levels.get("entry_zone")
            stop_price = levels.get("stop_price")
            tp = levels.get("tp")

        elif regime == "trend_down_candidate":
            # Пока futures отключены, short-кандидаты можно видеть, но не исполнять.
            action = "short"
            reason = self._reason_join([
                ctx.trend,
                ctx.momentum,
                ctx.volume_state,
                "resistance_context",
            ])

            levels = self._build_short_levels(ctx)
            entry_zone = levels.get("entry_zone")
            stop_price = levels.get("stop_price")
            tp = levels.get("tp")

        # Direction-aware confidence (same logic as multi-TF builder)
        if action in ("long", "short"):
            _rt = float(scores.get("trend", 50.0))
            _rm = float(scores.get("momentum", 50.0))
            dt = (100.0 - _rt) if action == "short" else _rt
            dm = (100.0 - _rm) if action == "short" else _rm
            confidence_hint = round(
                dt * 0.30 + dm * 0.20
                + float(scores.get("volume", 50.0)) * 0.20
                + float(scores.get("structure", 50.0)) * 0.20
                + float(scores.get("volatility", 50.0)) * 0.10,
                2,
            )

        setup_quality = {
            "trend_alignment": 0.0,
            "entry_timing": 0.0,
            "volume_confirmation": 0.0,
            "structure_quality": 0.0,
            "volatility_quality": 0.0,
            "penalty": 0.0,
            "final_score": confidence_hint,
            "decision": "approve" if action != "hold" and confidence_hint >= 70 else "hold",
            "comment": "legacy_single_timeframe",
        }

        setup_decision = str(setup_quality.get("decision", "hold"))

        return MarketIntelligenceResult(
            symbol=symbol,
            source=source,
            action=action,
            regime=regime,

            entry_zone=entry_zone,
            stop_price=stop_price,
            tp=tp,

            confidence_hint=round(confidence_hint, 2),
            reason=reason,

            scores=scores,
            timeframes={
                "base": asdict(ctx),
            },

            setup_quality=setup_quality,
            setup_decision=setup_decision,

            radar_state="none",
        )

    def _build_long_levels(self, contexts):
        entry_tf = str(getattr(settings, "LEVELS_ENTRY_TF", "5m"))
        signal_tf = str(getattr(settings, "LEVELS_SIGNAL_TF", "15m"))
        context_tf = str(getattr(settings, "LEVELS_CONTEXT_TF", "1h"))

        m5 = self._tf(contexts, entry_tf) or self._tf(contexts, "5m") or self._tf(contexts, "base")
        m15 = self._tf(contexts, signal_tf) or self._tf(contexts, "15m") or m5
        h1 = self._tf(contexts, context_tf) or self._tf(contexts, "1h") or m15

        last = float(self._ctx_value(m5, "last_close", 0))
        atr = float(
            self._ctx_value(m15, "atr14", None)
            or self._ctx_value(m5, "atr14", 0)
            or 0
        )

        support = float(
            self._ctx_value(m15, "support", None)
            or self._ctx_value(m5, "support", last)
            or last
        )

        resistance = float(
            self._ctx_value(h1, "resistance", None)
            or self._ctx_value(m15, "resistance", None)
            or self._ctx_value(m5, "resistance", last)
            or last
        )

        if last <= 0:
            return {
                "entry_zone": None,
                "stop_price": None,
                "tp": None,
            }

        if atr <= 0:
            atr = last * 0.003

        entry_from = round(last * 0.997, 4)
        entry_to = round(last * 1.003, 4)

        tcfg = get_profiles().trend  # Фаза 3: крупные трендовые цели из профиля
        stop_atr_mult = tcfg.stop_atr_mult
        min_stop_pct = tcfg.min_stop_pct / 100.0

        # Стоп ТОЛЬКО по ATR (структурный support как стоп не используем).
        atr_stop = last - atr * stop_atr_mult
        pct_stop = last * (1 - min_stop_pct)
        stop_price = round(min(atr_stop, pct_stop), 4)

        risk = max(last - stop_price, atr)

        tp1 = round(last + risk * tcfg.tp1_r_mult, 4)
        tp2 = round(last + risk * tcfg.tp2_r_mult, 4)

        # Полы целей — крупнее (тренд реально едет и проходит net RR после костов).
        tp1 = max(tp1, round(last * (1 + tcfg.tp1_floor_pct / 100.0), 4))
        tp2 = max(tp2, round(last * (1 + tcfg.tp2_floor_pct / 100.0), 4))

        # FIX: убрана привязка tp1/tp2 к resistance.tail(50) на 1h.
        # resistance за 50 часов = 2-дневный максимум (+7-15% от цены).
        # TP на таком расстоянии не достигаются за время сигнала.

        return {
            "entry_zone": [entry_from, entry_to],
            "stop_price": stop_price,
            "tp": {
                "tp1": tp1,
                "tp2": tp2,
            },
        }

    def _build_short_levels(self, contexts):
        entry_tf = str(getattr(settings, "LEVELS_ENTRY_TF", "5m"))
        signal_tf = str(getattr(settings, "LEVELS_SIGNAL_TF", "15m"))
        context_tf = str(getattr(settings, "LEVELS_CONTEXT_TF", "1h"))

        m5 = self._tf(contexts, entry_tf) or self._tf(contexts, "5m") or self._tf(contexts, "base")
        m15 = self._tf(contexts, signal_tf) or self._tf(contexts, "15m") or m5
        h1 = self._tf(contexts, context_tf) or self._tf(contexts, "1h") or m15

        last = float(self._ctx_value(m5, "last_close", 0))
        atr = float(
            self._ctx_value(m15, "atr14", None)
            or self._ctx_value(m5, "atr14", 0)
            or 0
        )

        resistance = float(
            self._ctx_value(m15, "resistance", None)
            or self._ctx_value(m5, "resistance", last)
            or last
        )

        support = float(
            self._ctx_value(h1, "support", None)
            or self._ctx_value(m15, "support", None)
            or self._ctx_value(m5, "support", last)
            or last
        )

        if last <= 0:
            return {
                "entry_zone": None,
                "stop_price": None,
                "tp": None,
            }

        if atr <= 0:
            atr = last * 0.003

        entry_from = round(last * 0.997, 4)
        entry_to = round(last * 1.003, 4)

        tcfg = get_profiles().trend  # Фаза 3: крупные трендовые цели из профиля
        stop_atr_mult = tcfg.stop_atr_mult
        min_stop_pct = tcfg.min_stop_pct / 100.0

        # Стоп ТОЛЬКО по ATR (структурный resistance как стоп не используем).
        atr_stop = last + atr * stop_atr_mult
        pct_stop = last * (1 + min_stop_pct)
        stop_price = round(max(atr_stop, pct_stop), 4)

        risk = max(stop_price - last, atr)

        tp1 = round(last - risk * tcfg.tp1_r_mult, 4)
        tp2 = round(last - risk * tcfg.tp2_r_mult, 4)

        # Полы целей — крупнее (симметрично лонгу).
        tp1 = min(tp1, round(last * (1 - tcfg.tp1_floor_pct / 100.0), 4))
        tp2 = min(tp2, round(last * (1 - tcfg.tp2_floor_pct / 100.0), 4))

        # FIX: убрана привязка tp1/tp2 к support.tail(50) на 1h.
        # support за 50 часов = 2-дневный минимум (-7-15% от цены).
        # TP на таком расстоянии цена не достигает за время сигнала.
        # TP теперь строятся только по risk * multiplier от входа.

        return {
            "entry_zone": [entry_from, entry_to],
            "stop_price": stop_price,
            "tp": {
                "tp1": tp1,
                "tp2": tp2,
            },
        }

    def _trend_state(self, price: float, ema20: float, ema50: float, ema200: float) -> str:
        if price > ema20 > ema50:
            return "trend_up"

        if price < ema20 < ema50:
            return "trend_down"

        if abs(ema20 - ema50) / price < 0.0015:
            return "flat"

        return "mixed"

    def _momentum_state(self, rsi: float, macd_hist: float) -> str:
        if rsi >= 72:
            return "overheated"

        if rsi <= 28:
            return "oversold"

        if rsi > 52 and macd_hist > 0:
            return "bullish"

        if rsi < 48 and macd_hist < 0:
            return "bearish"

        return "neutral"

    def _volatility_state(self, atr: float, price: float) -> str:
        atr_pct = atr / price if price > 0 else 0.0

        if atr_pct < 0.001:
            return "low"

        if atr_pct > 0.04:
            return "extreme"

        return "normal"

    def _volume_state(self, volume_ratio: float) -> str:
        if volume_ratio >= 1.4:
            return "strong"

        if volume_ratio <= 0.65:
            return "weak"

        return "normal"

    def _ema(self, series: pd.Series, period: int) -> float:
        return series.ewm(span=period, adjust=False).mean().iloc[-1]

    def _rsi(self, close: pd.Series, period: int = 14) -> float:
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)

        avg_gain = gain.rolling(period).mean()
        avg_loss = loss.rolling(period).mean()

        rs = avg_gain / avg_loss.replace(0, 1e-9)
        rsi = 100 - (100 / (1 + rs))

        return rsi.iloc[-1]

    def _macd(self, close: pd.Series):
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()

        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        hist = macd_line - signal_line

        return macd_line.iloc[-1], signal_line.iloc[-1], hist.iloc[-1]

    def _atr(self, high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> float:
        prev_close = close.shift(1)

        tr = pd.concat(
            [
                high - low,
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)

        return tr.rolling(period).mean().iloc[-1]

    def _reason_join(self, parts: list[str]) -> str:
        return "_".join([p for p in parts if p])

    def _score_multi_timeframe(self, contexts: dict[str, TimeframeContext]) -> dict[str, float]:
        """
        Взвешенная оценка по таймфреймам.
        4h и 1h отвечают за направление,
        15m за сетап,
        5m/1m за вход.
        """

        weights = {
            "1m": 0.08,
            "5m": 0.14,
            "15m": 0.28,
            "1h": 0.30,
            "4h": 0.20,
        }

        aggregate = {
            "trend": 0.0,
            "momentum": 0.0,
            "volume": 0.0,
            "structure": 0.0,
            "volatility": 0.0,
            "total": 0.0,
        }

        total_weight = 0.0

        for tf, ctx in contexts.items():
            weight = weights.get(tf, 0.1)
            score = self._score_context(ctx)

            for key in aggregate:
                aggregate[key] += score[key] * weight

            total_weight += weight

        if total_weight <= 0:
            total_weight = 1.0

        return {
            key: round(value / total_weight, 2)
            for key, value in aggregate.items()
        }

    def _detect_multi_timeframe_regime(
        self,
        contexts: dict[str, TimeframeContext],
        scores: dict[str, float],
    ) -> str:
        h4 = self._tf(contexts, "4h")
        h1 = self._tf(contexts, "1h")
        m15 = self._tf(contexts, "15m")
        m5 = self._tf(contexts, "5m")
        m1 = self._tf(contexts, "1m")

        if self._ctx_value(h1, "volatility") == "extreme":
            return "volatile"

        trading_mode = str(getattr(settings, "TRADING_MODE", "paper_signal")).lower()
        signal_profile = str(getattr(settings, "SIGNAL_PROFILE", "learning")).lower()

        learning_mode = trading_mode in ["paper_signal", "paper_trade"] or signal_profile in [
            "learning",
            "aggressive",
            "dev",
        ]

        trend_up_votes = 0
        trend_down_votes = 0
        flat_votes = 0

        for ctx in [h4, h1, m15, m5, m1]:
            if not ctx:
                continue

            trend = self._ctx_value(ctx, "trend")

            if trend == "trend_up":
                trend_up_votes += 1
            elif trend == "trend_down":
                trend_down_votes += 1
            elif trend == "flat":
                flat_votes += 1

        h4_trend = self._ctx_value(h4, "trend")
        h1_trend = self._ctx_value(h1, "trend")
        m15_trend = self._ctx_value(m15, "trend")
        m5_trend = self._ctx_value(m5, "trend")

        h4_momentum = self._ctx_value(h4, "momentum")
        h1_momentum = self._ctx_value(h1, "momentum")
        m15_momentum = self._ctx_value(m15, "momentum")
        m5_momentum = self._ctx_value(m5, "momentum")

        total_score = float(scores.get("total", 0))
        trend_score = float(scores.get("trend", 0))
        volume_score = float(scores.get("volume", 0))

        # Строгий классический long.
        if (
            h4_trend == "trend_up"
            and h1_trend == "trend_up"
            and m15_momentum in ["bullish", "neutral"]
        ):
            return "trend_up_candidate"

        # Строгий классический short.
        if (
            h4_trend == "trend_down"
            and h1_trend == "trend_down"
            and m15_momentum in ["bearish", "neutral"]
        ):
            return "trend_down_candidate"

        # Learning/dev: разрешаем кандидат, если 4h mixed,
        # но 1h + 15m + 5m уже дают рабочее направление.
        if learning_mode:
            long_learning_ok = (
                h4_trend in ["trend_up", "mixed"]            # flat убран
                and h1_trend == "trend_up"                   # FIX: h1 обязан быть trend_up
                and m15_trend == "trend_up"
                and m5_trend in ["trend_up", "flat", "mixed"]
                and m15_momentum in ["bullish", "neutral"]
                and m5_momentum in ["bullish", "neutral"]
                and total_score >= 55
                and trend_score >= 55
            )

            if long_learning_ok:
                return "trend_up_candidate"

            short_learning_ok = (
                h4_trend in ["trend_down", "mixed"]          # flat убран: flat на 4h = боковик, не short
                and h1_trend == "trend_down"                 # FIX: h1 обязан быть trend_down
                and m15_trend == "trend_down"
                and m5_trend in ["trend_down", "flat", "mixed"]
                and m15_momentum in ["bearish", "neutral"]
                and m5_momentum in ["bearish", "neutral"]
                and total_score >= 50
                and trend_score <= 55
            )

            if short_learning_ok:
                return "trend_down_candidate"

        # Голосование по тренду.
        if trend_up_votes >= 3 and total_score >= 58:
            return "trend_up_candidate"

        if trend_down_votes >= 3 and total_score <= 50:
            return "trend_down_candidate"

        # Для learning не называем всё flat слишком рано.
        if flat_votes >= 3 and not learning_mode:
            return "flat"

        if flat_votes >= 2 and total_score < 52:
            return "flat"

        return "mixed"

    def _build_multi_timeframe_candidate(
        self,
        symbol: str,
        source: str,
        contexts: dict[str, TimeframeContext],
        scores: dict[str, float],
        regime: str,
    ) -> MarketIntelligenceResult:
        work_ctx = (
            self._tf(contexts, "15m")
            or self._tf(contexts, "5m")
            or self._tf(contexts, "1h")
            or next(iter(contexts.values()))
        )

        action = "hold"
        reason = "no_trade_conditions"
        entry_zone = None
        stop_price = None
        tp = None

        # confidence_hint is computed after action is determined (see below)
        # so we initialise to raw total for hold/unknown cases.
        confidence_hint = scores.get("total", 0)

        radar_state = self._detect_radar_state(
            contexts=contexts,
            scores=scores,
            regime=regime,
        )

        if action == "hold" and radar_state in ["watch_long", "watch_short"]:
            reason = radar_state

        if regime == "trend_up_candidate":
            action = "long"
            reason = self._reason_join([
                "mtf_trend_up",
                self._ctx_value(work_ctx, "momentum"),
                self._ctx_value(work_ctx, "volume_state"),
                "structure_confirmed",
            ])

            levels = self._build_long_levels(contexts)
            entry_zone = levels.get("entry_zone")
            stop_price = levels.get("stop_price")
            tp = levels.get("tp")

        elif regime == "trend_down_candidate":
            action = "short"
            reason = self._reason_join([
                "mtf_trend_down",
                self._ctx_value(work_ctx, "momentum"),
                self._ctx_value(work_ctx, "volume_state"),
                "structure_confirmed",
            ])

            levels = self._build_short_levels(contexts)
            entry_zone = levels.get("entry_zone")
            stop_price = levels.get("stop_price")
            tp = levels.get("tp")

        if action == "hold" and radar_state in ["watch_long", "watch_short"]:
            escalated = self._try_escalate_watch_to_candidate(
                symbol=symbol,
                source=source,
                action=action,
                regime=regime,
                contexts=contexts,
                scores=scores,
                radar_state=radar_state,
            )

            if escalated:
                action = escalated["action"]
                entry_zone = escalated["entry_zone"]
                stop_price = escalated["stop_price"]
                tp = escalated["tp"]
                reason = escalated["reason"]
                regime = f"{radar_state}_escalated_candidate"

        if action == "hold" and radar_state in ["watch_long", "watch_short"]:
            reason = radar_state

        # ── Direction-aware confidence_hint ─────────────────────────────────
        # _score_context uses a "bullishness" scale (trend_up=75, trend_down=25).
        # For SHORT signals this means bearish alignment scores LOW — the opposite
        # of what we want. We flip trend and momentum for shorts so that a strong
        # downtrend correctly yields high confidence for a short candidate.
        raw_trend      = float(scores.get("trend", 50.0))
        raw_momentum   = float(scores.get("momentum", 50.0))
        raw_volume     = float(scores.get("volume", 50.0))
        raw_structure  = float(scores.get("structure", 50.0))
        raw_volatility = float(scores.get("volatility", 50.0))

        if action == "short":
            dir_trend    = 100.0 - raw_trend       # 25 → 75 for trend_down
            dir_momentum = 100.0 - raw_momentum    # 30 → 70 for bearish
        else:
            dir_trend    = raw_trend
            dir_momentum = raw_momentum

        if action in ("long", "short"):
            confidence_hint = round(
                dir_trend    * 0.30
                + dir_momentum * 0.20
                + raw_volume   * 0.20
                + raw_structure * 0.20
                + raw_volatility * 0.10,
                2,
            )
        # hold keeps the raw total computed above

        setup_quality = self._score_setup_quality(
            action=action,
            regime=regime,
            contexts=contexts,
            scores=scores,
        )

        setup_decision = str(setup_quality.get("decision", "hold"))

        return MarketIntelligenceResult(
            symbol=symbol,
            source=source,
            action=action,
            regime=regime,

            entry_zone=entry_zone,
            stop_price=stop_price,
            tp=tp,

            confidence_hint=round(confidence_hint, 2),
            reason=reason,

            scores=scores,
            timeframes={
                tf: asdict(ctx) if not isinstance(ctx, dict) else ctx
                for tf, ctx in contexts.items()
            },

            setup_quality=setup_quality,
            setup_decision=setup_decision,
            radar_state=radar_state,
        )  

    def _score_setup_quality(
        self,
        action: str,
        regime: str,
        contexts: dict[str, TimeframeContext],
        scores: dict[str, float],
    ) -> dict:
        if action == "hold":
            return {
                "trend_alignment": 0.0,
                "entry_timing": 0.0,
                "volume_confirmation": 0.0,
                "structure_quality": 0.0,
                "volatility_quality": 0.0,
                "penalty": 0.0,
                "raw_score": 0.0,
                "final_score": 0.0,
                "decision": "hold",
                "comment": "no_candidate",
            }

        trading_mode = str(getattr(settings, "TRADING_MODE", "paper_signal")).lower()
        signal_profile = str(getattr(settings, "SIGNAL_PROFILE", "learning")).lower()

        learning_mode = trading_mode in ["paper_signal", "paper_trade"] or signal_profile in [
            "learning",
            "aggressive",
            "dev",
        ]

        h4 = self._tf(contexts, "4h")
        h1 = self._tf(contexts, "1h")
        m15 = self._tf(contexts, "15m")
        m5 = self._tf(contexts, "5m")
        m1 = self._tf(contexts, "1m")

        trend_alignment = 0.0
        entry_timing = 0.0
        volume_confirmation = 0.0
        penalty = 0.0

        if action == "long":
            if self._ctx_value(h4, "trend") == "trend_up":
                trend_alignment += 20
            if self._ctx_value(h1, "trend") == "trend_up":
                trend_alignment += 20
            if self._ctx_value(m15, "trend") == "trend_up":
                trend_alignment += 10

            if self._ctx_value(h4, "trend") == "trend_down":
                penalty += 20
            if self._ctx_value(h1, "trend") == "trend_down":
                penalty += 12 if learning_mode else 15

            if self._ctx_value(m15, "momentum") in ["bullish", "neutral", "oversold"]:
                entry_timing += 12
            if self._ctx_value(m5, "momentum") in ["bullish", "neutral"]:
                entry_timing += 8
            if self._ctx_value(m1, "momentum") in ["bullish", "neutral"]:
                entry_timing += 5

            if self._ctx_value(m5, "momentum") == "bearish":
                penalty += 5 if learning_mode else 10
            if self._ctx_value(m1, "momentum") == "bearish":
                penalty += 3 if learning_mode else 7

            if self._ctx_value(h4, "momentum") == "overheated":
                penalty += 8 if learning_mode else 12
            if self._ctx_value(m15, "momentum") == "overheated":
                penalty += 6 if learning_mode else 10

            # (#7) Не покупаем краткосрочную вершину: штраф за перегрев 1m/5m.
            if self._ctx_value(m1, "momentum") == "overheated":
                penalty += float(getattr(settings, "OVERHEAT_ENTRY_PENALTY_M1", 8.0))
            if self._ctx_value(m5, "momentum") == "overheated":
                penalty += float(getattr(settings, "OVERHEAT_ENTRY_PENALTY_M5", 5.0))

        elif action == "short":
            if self._ctx_value(h4, "trend") == "trend_down":
                trend_alignment += 20
            if self._ctx_value(h1, "trend") == "trend_down":
                trend_alignment += 20
            if self._ctx_value(m15, "trend") == "trend_down":
                trend_alignment += 10

            if self._ctx_value(h4, "trend") == "trend_up":
                penalty += 20
            if self._ctx_value(h1, "trend") == "trend_up":
                penalty += 12 if learning_mode else 15

            if self._ctx_value(m15, "momentum") in ["bearish", "neutral", "overheated"]:
                entry_timing += 12
            if self._ctx_value(m5, "momentum") in ["bearish", "neutral"]:
                entry_timing += 8
            if self._ctx_value(m1, "momentum") in ["bearish", "neutral"]:
                entry_timing += 5

            if self._ctx_value(m5, "momentum") == "bullish":
                penalty += 5 if learning_mode else 10
            if self._ctx_value(m1, "momentum") == "bullish":
                penalty += 3 if learning_mode else 7

            if self._ctx_value(h4, "momentum") == "oversold":
                penalty += 8 if learning_mode else 12
            if self._ctx_value(m15, "momentum") == "oversold":
                penalty += 6 if learning_mode else 10

            # (#7) Не шортим краткосрочное дно: штраф за перепроданность 1m/5m.
            if self._ctx_value(m1, "momentum") == "oversold":
                penalty += float(getattr(settings, "OVERHEAT_ENTRY_PENALTY_M1", 8.0))
            if self._ctx_value(m5, "momentum") == "oversold":
                penalty += float(getattr(settings, "OVERHEAT_ENTRY_PENALTY_M5", 5.0))

        volume_contexts = [ctx for ctx in [m1, m5, m15, h1, h4] if ctx]

        strong_volume_count = sum(
            1 for ctx in volume_contexts
            if self._ctx_value(ctx, "volume_state") == "strong"
        )

        normal_volume_count = sum(
            1 for ctx in volume_contexts
            if self._ctx_value(ctx, "volume_state") == "normal"
        )

        weak_volume_count = sum(
            1 for ctx in volume_contexts
            if self._ctx_value(ctx, "volume_state") == "weak"
        )

        volume_confirmation += strong_volume_count * 7
        volume_confirmation += normal_volume_count * 3

        # В learning/dev слабый объём не должен убивать сделку полностью.
        if learning_mode:
            if weak_volume_count >= 5:
                penalty += 18
            elif weak_volume_count == 4:
                penalty += 12
            elif weak_volume_count == 3:
                penalty += 7
        else:
            if weak_volume_count >= 3:
                penalty += 20
            elif weak_volume_count == 2:
                penalty += 12

        structure_quality = min(scores.get("structure", 0), 100) * 0.25
        volatility_quality = min(scores.get("volatility", 0), 100) * 0.15

        if "watch_long" in regime and action == "long":
            if self._ctx_value(h4, "trend") == "trend_up":
                entry_timing += 5

        if "watch_short" in regime and action == "short":
            if self._ctx_value(h4, "trend") == "trend_down":
                entry_timing += 5

        raw_score = (
            trend_alignment
            + entry_timing
            + volume_confirmation
            + structure_quality
            + volatility_quality
            - penalty
        )

        final_score = max(0.0, min(100.0, raw_score))

        is_trend_candidate = (
            regime in ["trend_up_candidate", "trend_down_candidate"]
            or "escalated_candidate" in str(regime)
        )

        if learning_mode:
            learning_min_score = float(getattr(settings, "LEARNING_SETUP_MIN_SCORE", 62.0))
            learning_min_trend_alignment = float(getattr(settings, "LEARNING_SETUP_MIN_TREND_ALIGNMENT", 45.0))
            learning_min_volume_confirmation = float(getattr(settings, "LEARNING_SETUP_MIN_VOLUME_CONFIRMATION", 6.0))

            if (
                final_score >= learning_min_score
                and trend_alignment >= learning_min_trend_alignment
                and volume_confirmation >= learning_min_volume_confirmation
            ):
                decision = "approve"
                comment = "learning_setup_approved"
            elif (
                is_trend_candidate
                and trend_alignment >= float(getattr(settings, "LEARNING_TREND_CONTINUATION_MIN_TREND_ALIGNMENT", 35.0))
                and volume_confirmation >= float(getattr(settings, "LEARNING_TREND_CONTINUATION_MIN_VOLUME_CONFIRMATION", 2.0))
                and structure_quality >= float(getattr(settings, "LEARNING_TREND_CONTINUATION_MIN_STRUCTURE_QUALITY", 12.0))
                and final_score >= float(getattr(settings, "LEARNING_TREND_CONTINUATION_MIN_FINAL_SCORE", 50.0))
            ):
                decision = "approve"
                comment = "learning_trend_continuation_approved"
            elif final_score >= 45:
                decision = "wait"
                comment = "learning_wait_more_confirmation"
            else:
                decision = "reject"
                comment = "learning_setup_too_low"
        else:
            if final_score >= 70:
                decision = "approve"
                comment = "setup_confirmed"
            elif final_score >= 55:
                decision = "wait"
                comment = "candidate_but_wait_confirmation"
            else:
                decision = "reject"
                comment = "setup_quality_too_low"

        if not bool(getattr(settings, "ALLOW_WEAK_VOLUME_TREND_ENTRIES", False)):
            if weak_volume_count >= 4 and normal_volume_count == 0 and strong_volume_count == 0 and decision == "approve":
                decision = "wait"
                comment = "weak_volume_block_applied"

        return {
            "trend_alignment": round(trend_alignment, 2),
            "entry_timing": round(entry_timing, 2),
            "volume_confirmation": round(volume_confirmation, 2),
            "weak_volume_count": weak_volume_count,
            "normal_volume_count": normal_volume_count,
            "strong_volume_count": strong_volume_count,
            "structure_quality": round(structure_quality, 2),
            "volatility_quality": round(volatility_quality, 2),
            "penalty": round(penalty, 2),
            "raw_score": round(raw_score, 2),
            "final_score": round(final_score, 2),
            "learning_mode": learning_mode,
            "decision": decision,
            "comment": comment,
        }              

    def _detect_radar_state(
        self,
        contexts: dict[str, TimeframeContext],
        scores: dict[str, float],
        regime: str,
    ) -> str:
        h4 = self._tf(contexts, "4h")
        h1 = self._tf(contexts, "1h")
        m15 = self._tf(contexts, "15m")
        m5 = self._tf(contexts, "5m")
        m1 = self._tf(contexts, "1m")

        if regime in ["trend_up_candidate", "trend_down_candidate"]:
            return "none"

        trading_mode = str(getattr(settings, "TRADING_MODE", "paper_signal")).lower()
        signal_profile = str(getattr(settings, "SIGNAL_PROFILE", "learning")).lower()

        learning_mode = trading_mode in ["paper_signal", "paper_trade"] or signal_profile in [
            "learning",
            "aggressive",
            "dev",
        ]

        total_score = float(scores.get("total", 0))
        trend_score = float(scores.get("trend", 0))
        volume_score = float(scores.get("volume", 0))

        h4_trend = self._ctx_value(h4, "trend")
        h1_trend = self._ctx_value(h1, "trend")
        m15_trend = self._ctx_value(m15, "trend")
        m5_trend = self._ctx_value(m5, "trend")
        m1_trend = self._ctx_value(m1, "trend")

        h4_momentum = self._ctx_value(h4, "momentum")
        h1_momentum = self._ctx_value(h1, "momentum")
        m15_momentum = self._ctx_value(m15, "momentum")
        m5_momentum = self._ctx_value(m5, "momentum")
        m1_momentum = self._ctx_value(m1, "momentum")

        h4_volume = self._ctx_value(h4, "volume_state")
        h1_volume = self._ctx_value(h1, "volume_state")
        m15_volume = self._ctx_value(m15, "volume_state")
        m5_volume = self._ctx_value(m5, "volume_state")
        m1_volume = self._ctx_value(m1, "volume_state")

        long_bias = (
            h4_trend in ["trend_up", "mixed", "flat"]
            and h1_trend in ["trend_up", "mixed", "flat"]
            and (
                m15_trend == "trend_up"
                or m5_trend == "trend_up"
                or trend_score >= 55
            )
            and m15_momentum in ["bullish", "neutral", "oversold"]
            and h1_momentum in ["bullish", "neutral", "overheated"]
        )

        long_volume_ok = (
            volume_score >= 38
            or h4_volume in ["normal", "strong"]
            or h1_volume in ["normal", "strong"]
            or m15_volume in ["normal", "strong"]
        )

        long_waiting_entry = (
            m1_momentum in ["bearish", "neutral", "oversold"]
            or m5_momentum in ["bearish", "neutral", "oversold"]
            or m5_volume == "weak"
            or m1_volume == "weak"
        )

        if long_bias and long_volume_ok and long_waiting_entry and total_score >= 50:
            return "watch_long"

        short_bias = (
            h4_trend in ["trend_down", "mixed", "flat"]
            and h1_trend in ["trend_down", "mixed", "flat"]
            and (
                m15_trend == "trend_down"
                or m5_trend == "trend_down"
                or trend_score <= 50
            )
            and m15_momentum in ["bearish", "neutral", "overheated"]
            and h1_momentum in ["bearish", "neutral", "oversold"]
        )

        short_volume_ok = (
            volume_score >= 38
            or h4_volume in ["normal", "strong"]
            or h1_volume in ["normal", "strong"]
            or m15_volume in ["normal", "strong"]
        )

        short_waiting_entry = (
            m1_momentum in ["bullish", "neutral", "overheated"]
            or m5_momentum in ["bullish", "neutral", "overheated"]
            or m5_volume == "weak"
            or m1_volume == "weak"
        )

        if short_bias and short_volume_ok and short_waiting_entry and total_score >= 47:
            return "watch_short"

        # Learning fallback: не даём системе молчать, если картина почти готова.
        if learning_mode:
            if (
                h1_trend in ["trend_up", "mixed", "flat"]
                and m15_trend == "trend_up"
                and m15_momentum in ["bullish", "neutral"]
                and total_score >= 54
            ):
                return "watch_long"

            if (
                h1_trend in ["trend_down", "mixed", "flat"]
                and m15_trend == "trend_down"
                and m15_momentum in ["bearish", "neutral"]
                and total_score >= 48
            ):
                return "watch_short"

        return "none"

    def _try_escalate_watch_to_candidate(
        self,
        symbol: str,
        source: str,
        action: str,
        regime: str,
        contexts: dict,
        scores: dict[str, float],
        radar_state: str,
    ) -> dict | None:
        """
        Пробует повысить watch_long/watch_short до candidate.
        В learning/dev режиме работает мягче:
        - 4h может быть mixed/flat
        - слабый объём на 1m/5m не блокирует, если 15m/1h подтверждают
        - финальный фильтр по RR всё равно делает TradePlan
        """

        if radar_state not in ["watch_long", "watch_short"]:
            return None

        trading_mode = str(getattr(settings, "TRADING_MODE", "paper_signal")).lower()
        signal_profile = str(getattr(settings, "SIGNAL_PROFILE", "learning")).lower()

        learning_mode = trading_mode in ["paper_signal", "paper_trade"] or signal_profile in [
            "learning",
            "aggressive",
            "dev",
        ]

        h4 = self._tf(contexts, "4h")
        h1 = self._tf(contexts, "1h")
        m15 = self._tf(contexts, "15m")
        m5 = self._tf(contexts, "5m")
        m1 = self._tf(contexts, "1m")

        if not all([h4, h1, m15, m5, m1]):
            return None

        h4_trend = self._ctx_value(h4, "trend")
        h1_trend = self._ctx_value(h1, "trend")
        m15_trend = self._ctx_value(m15, "trend")
        m5_trend = self._ctx_value(m5, "trend")

        h4_momentum = self._ctx_value(h4, "momentum")
        h1_momentum = self._ctx_value(h1, "momentum")
        m15_momentum = self._ctx_value(m15, "momentum")
        m5_momentum = self._ctx_value(m5, "momentum")
        m1_momentum = self._ctx_value(m1, "momentum")

        h4_volume = self._ctx_value(h4, "volume_state")
        h1_volume = self._ctx_value(h1, "volume_state")
        m15_volume = self._ctx_value(m15, "volume_state")
        m5_volume = self._ctx_value(m5, "volume_state")
        m1_volume = self._ctx_value(m1, "volume_state")

        total_score = float(scores.get("total", 0))
        trend_score = float(scores.get("trend", 0))
        volume_score = float(scores.get("volume", 0))

        if radar_state == "watch_long":
            if learning_mode:
                higher_ok = (
                    h4_trend in ["trend_up", "mixed"]        # flat убран
                    and h1_trend in ["trend_up", "mixed"]    # flat убран
                    and h4_momentum not in ["oversold"]
                )

                middle_ok = (
                    m15_trend in ["trend_up", "mixed", "flat"]
                    and m15_momentum in ["bullish", "neutral", "oversold"]
                )

                lower_ok = (
                    m5_momentum in ["bullish", "neutral", "oversold"]
                    or m1_momentum in ["bullish", "neutral", "oversold"]
                )

                volume_ok = (
                    volume_score >= 35
                    or h4_volume in ["normal", "strong"]
                    or h1_volume in ["normal", "strong"]
                    or m15_volume in ["normal", "strong"]
                    or m5_volume in ["normal", "strong"]
                )

                score_ok = total_score >= 52 and trend_score >= 50

            else:
                higher_ok = (
                    h4_trend == "trend_up"
                    and h4_momentum in ["bullish", "neutral"]
                    and h1_trend in ["trend_up", "mixed", "flat"]
                )

                middle_ok = (
                    m15_momentum in ["bullish", "neutral", "oversold"]
                    or m15_volume == "strong"
                )

                lower_ok = (
                    m5_momentum in ["bullish", "neutral"]
                    and m1_momentum in ["bullish", "neutral"]
                )

                volume_ok = m5_volume in ["normal", "strong"] or m15_volume == "strong"
                score_ok = total_score >= 58 and trend_score >= 55 and volume_score >= 45

            if higher_ok and middle_ok and lower_ok and volume_ok and score_ok:
                levels = self._build_long_levels(contexts)

                if not levels or not levels.get("entry_zone") or not levels.get("stop_price") or not levels.get("tp"):
                    return None

                return {
                    "action": "long",
                    "entry_zone": levels["entry_zone"],
                    "stop_price": levels["stop_price"],
                    "tp": levels["tp"],
                    "reason": "watch_long_escalated_to_candidate",
                }

        if radar_state == "watch_short":
            if learning_mode:
                higher_ok = (
                    h4_trend in ["trend_down", "mixed"]      # flat убран
                    and h1_trend in ["trend_down", "mixed"]  # flat убран: flat+flat = боковик
                    and h4_momentum not in ["overheated"]
                )

                middle_ok = (
                    m15_trend in ["trend_down", "mixed", "flat"]
                    and m15_momentum in ["bearish", "neutral", "overheated"]
                )

                lower_ok = (
                    m5_momentum in ["bearish", "neutral", "overheated"]
                    or m1_momentum in ["bearish", "neutral", "overheated"]
                )

                volume_ok = (
                    volume_score >= 35
                    or h4_volume in ["normal", "strong"]
                    or h1_volume in ["normal", "strong"]
                    or m15_volume in ["normal", "strong"]
                    or m5_volume in ["normal", "strong"]
                )

                score_ok = total_score >= 48 and trend_score <= 58

            else:
                higher_ok = (
                    h4_trend == "trend_down"
                    and h4_momentum in ["bearish", "neutral"]
                    and h1_trend in ["trend_down", "mixed", "flat"]
                )

                middle_ok = (
                    m15_momentum in ["bearish", "neutral", "overheated"]
                    or m15_volume == "strong"
                )

                lower_ok = (
                    m5_momentum in ["bearish", "neutral"]
                    and m1_momentum in ["bearish", "neutral"]
                )

                volume_ok = m5_volume in ["normal", "strong"] or m15_volume == "strong"
                score_ok = total_score >= 52 and trend_score <= 58 and volume_score >= 45

            if higher_ok and middle_ok and lower_ok and volume_ok and score_ok:
                levels = self._build_short_levels(contexts)

                if not levels or not levels.get("entry_zone") or not levels.get("stop_price") or not levels.get("tp"):
                    return None

                return {
                    "action": "short",
                    "entry_zone": levels["entry_zone"],
                    "stop_price": levels["stop_price"],
                    "tp": levels["tp"],
                    "reason": "watch_short_escalated_to_candidate",
                }

        return None

    def _weak_volume_block(self, contexts: dict[str, TimeframeContext]) -> bool:
        """
        Блокирует публикацию, если объём слабый почти везде.
        В слабом объёме цена часто пилит стопы без продолжения импульса.
        """

        tfs = ["1m", "5m", "15m", "1h", "4h"]

        weak_count = 0
        normal_or_strong_count = 0

        for tf in tfs:
            ctx = self._tf(contexts, tf)
            if not ctx:
                continue

            volume_state = self._ctx_value(ctx, "volume_state")

            if volume_state == "weak":
                weak_count += 1

            if volume_state in ["normal", "strong"]:
                normal_or_strong_count += 1

        return weak_count >= 4 and normal_or_strong_count == 0