"""Единый источник правды по торговым параметрам (Фаза 1 редизайна входов).

Цель: убрать «кашу» из разбросанных getattr(settings, "...") по всему коду.
Здесь — структурированные профили ДВИЖКОВ ВХОДА и ПРОФИЛЕЙ ВЕДЕНИЯ.

ВАЖНО (Фаза 1): значения берутся из текущего settings (env-override сохраняется),
поэтому поведение НЕ меняется. Потребители мигрируют на эти объекты помодульно.
Позже (после миграции) лишние переменные уйдут из env, а дефолты останутся тут.

Доступ: get_profiles() -> Profiles (кэш). profiles.scalp_mgmt.arm_pct и т.п.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.config import settings


def _f(name: str, default: float) -> float:
    try:
        return float(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default


def _b(name: str, default: bool) -> bool:
    return bool(getattr(settings, name, default))


def _s(name: str, default: str) -> str:
    return str(getattr(settings, name, default))


# ── ДВИЖКИ ВХОДА ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RangeEngine:
    enabled: bool
    min_width_pct: float
    support_zone: float
    rsi_min: float
    rsi_max: float
    min_tp1_net_pct: float
    tp2_resistance_buffer: float
    stop_atr_mult: float
    min_setup_score: float
    allow_short: bool

    @classmethod
    def load(cls) -> "RangeEngine":
        return cls(
            enabled=_b("ENABLE_RANGE_STRATEGY", False),
            min_width_pct=_f("RANGE_MIN_WIDTH_PCT", 2.5),
            support_zone=_f("RANGE_SUPPORT_ZONE", 0.30),
            rsi_min=_f("RANGE_ENTRY_RSI_MIN", 25.0),
            rsi_max=_f("RANGE_ENTRY_RSI_MAX", 52.0),
            min_tp1_net_pct=_f("RANGE_MIN_TP1_NET_PCT", 0.8),
            tp2_resistance_buffer=_f("RANGE_TP2_RESISTANCE_BUFFER", 0.10),
            stop_atr_mult=_f("RANGE_STOP_ATR_MULT", 0.5),
            min_setup_score=_f("RANGE_MIN_SETUP_SCORE", 60.0),
            allow_short=_b("RANGE_ALLOW_SHORT", False),
        )


@dataclass(frozen=True)
class CrtEngine:
    enabled: bool
    htf_tf: str
    ltf_tf: str
    min_range_pct: float
    ltf_confirm: str
    require_premium_discount: bool
    stop_buffer_pct: float
    tp2_rr: float
    min_tp1_net_pct: float
    allow_long: bool
    allow_short: bool
    min_setup_score: float

    @classmethod
    def load(cls) -> "CrtEngine":
        return cls(
            enabled=_b("ENABLE_CRT_STRATEGY", False),
            htf_tf=_s("CRT_HTF_TF", "4h"),
            ltf_tf=_s("CRT_LTF_TF", "15m"),
            min_range_pct=_f("CRT_MIN_RANGE_PCT", 1.5),
            ltf_confirm=_s("CRT_LTF_CONFIRM", "either"),
            require_premium_discount=_b("CRT_REQUIRE_PREMIUM_DISCOUNT", True),
            stop_buffer_pct=_f("CRT_STOP_BUFFER_PCT", 0.05),
            tp2_rr=_f("CRT_TP2_RR", 2.0),
            min_tp1_net_pct=_f("CRT_MIN_TP1_NET_PCT", 0.5),
            allow_long=_b("CRT_ALLOW_LONG", True),
            allow_short=_b("CRT_ALLOW_SHORT", True),
            min_setup_score=_f("CRT_MIN_SETUP_SCORE", 55.0),
        )


@dataclass(frozen=True)
class TrendEngine:
    ride_enabled: bool
    ride_min_mfe_pct: float
    ride_trail_drawdown: float

    @classmethod
    def load(cls) -> "TrendEngine":
        return cls(
            ride_enabled=_b("TREND_RIDE_ENABLED", True),
            ride_min_mfe_pct=_f("TREND_RIDE_MIN_MFE_TO_PROTECT_PCT", 1.2),
            ride_trail_drawdown=_f("TREND_RIDE_TRAIL_DRAWDOWN_PCT", 0.50),
        )


# ── ПРОФИЛИ ВЕДЕНИЯ ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ScalpMgmt:
    # сайзинг/экономика (scalp-профиль)
    max_position_margin_pct: float
    min_net_pnl_tp1_usdt: float
    min_net_pnl_tp2_usdt: float
    min_net_rr_tp2: float
    ad_min_edge_usdt: float
    ad_max_position_margin_pct: float
    ad_min_net_rr_tp1: float
    ad_min_net_rr_tp2: float
    # безубыток-замок
    breakeven_enabled: bool
    breakeven_arm_pct: float
    breakeven_giveback_share: float
    # post-loss cooldown
    cooldown_enabled: bool
    cooldown_min: float

    @classmethod
    def load(cls) -> "ScalpMgmt":
        return cls(
            max_position_margin_pct=_f("SCALP_MAX_POSITION_MARGIN_PCT", 0.10),
            min_net_pnl_tp1_usdt=_f("SCALP_MIN_NET_PNL_TP1_USDT", 0.5),
            min_net_pnl_tp2_usdt=_f("SCALP_MIN_NET_PNL_TP2_USDT", 1.0),
            min_net_rr_tp2=_f("SCALP_MIN_NET_RR_TP2", 1.0),
            ad_min_edge_usdt=_f("SCALP_ANTI_DRAIN_MIN_EDGE_AFTER_COSTS_USDT", 0.0),
            ad_max_position_margin_pct=_f("SCALP_ANTI_DRAIN_MAX_POSITION_MARGIN_PCT", 20.0),
            ad_min_net_rr_tp1=_f("SCALP_ANTI_DRAIN_MIN_NET_RR_TP1", 0.40),
            ad_min_net_rr_tp2=_f("SCALP_ANTI_DRAIN_MIN_NET_RR_TP2", 0.85),
            breakeven_enabled=_b("SCALP_BREAKEVEN_ENABLED", True),
            breakeven_arm_pct=_f("SCALP_BREAKEVEN_ARM_PCT", 0.5),
            breakeven_giveback_share=_f("SCALP_BREAKEVEN_GIVEBACK_SHARE", 0.5),
            cooldown_enabled=_b("POST_LOSS_COOLDOWN_ENABLED", True),
            cooldown_min=_f("POST_LOSS_COOLDOWN_MIN", 25.0),
        )


@dataclass(frozen=True)
class PositionMgmt:
    # дефолтный (трендовый/CRT) сайзинг и пороги
    max_position_margin_pct: float
    min_net_pnl_tp1_usdt: float
    min_net_pnl_tp2_usdt: float
    risk_per_trade_pct: float

    @classmethod
    def load(cls) -> "PositionMgmt":
        return cls(
            max_position_margin_pct=_f("MAX_POSITION_MARGIN_PCT", 0.35),
            min_net_pnl_tp1_usdt=_f("MIN_NET_PNL_TP1_USDT", 1.5),
            min_net_pnl_tp2_usdt=_f("MIN_NET_PNL_TP2_USDT", 3.5),
            risk_per_trade_pct=_f("RISK_PER_TRADE_PCT", 0.4),
        )


# ── ОБЩИЙ СЛОЙ РИСКА/ИСПОЛНЕНИЯ ──────────────────────────────────────────────
@dataclass(frozen=True)
class DepthConfig:
    enabled: bool
    gate_entries: bool
    accelerate_exits: bool
    ws_url: str
    depth_levels: int
    max_spread_pct: float
    obi_confirm: float
    wall_confirm_share: float
    data_max_age_sec: float
    cvd_window_sec: int
    cvd_exit_ratio: float
    cvd_min_trades: int

    @classmethod
    def load(cls) -> "DepthConfig":
        return cls(
            enabled=_b("ENABLE_ORDERBOOK_ENGINE", False),
            gate_entries=_b("OB_GATE_ENTRIES", True),
            accelerate_exits=_b("OB_ACCELERATE_EXITS", True),
            ws_url=_s("OB_WS_URL", "wss://api-aws.huobi.pro/ws"),
            depth_levels=_i("OB_DEPTH_LEVELS", 10),
            max_spread_pct=_f("OB_MAX_SPREAD_PCT", 0.08),
            obi_confirm=_f("OB_OBI_CONFIRM", 0.15),
            wall_confirm_share=_f("OB_WALL_CONFIRM_SHARE", 0.30),
            data_max_age_sec=_f("OB_DATA_MAX_AGE_SEC", 15.0),
            cvd_window_sec=_i("OB_CVD_WINDOW_SEC", 60),
            cvd_exit_ratio=_f("OB_CVD_EXIT_RATIO", 0.6),
            cvd_min_trades=_i("OB_CVD_MIN_TRADES", 15),
        )


@dataclass(frozen=True)
class AntiDrainCfg:
    enabled: bool
    min_confidence: float
    min_net_rr_tp1: float
    min_net_rr_tp2: float
    min_edge_usdt: float
    max_position_margin_pct: float
    max_used_margin_pct: float
    max_open_positions: int
    max_active_per_symbol: int
    max_daily_loss_pct: float
    max_drawdown_pct: float

    @classmethod
    def load(cls) -> "AntiDrainCfg":
        return cls(
            enabled=_b("ANTI_DRAIN_ENABLED", True),
            min_confidence=_f("ANTI_DRAIN_MIN_CONFIDENCE", 55.0),
            min_net_rr_tp1=_f("ANTI_DRAIN_MIN_NET_RR_TP1", 0.55),
            min_net_rr_tp2=_f("ANTI_DRAIN_MIN_NET_RR_TP2", 0.90),
            min_edge_usdt=_f("ANTI_DRAIN_MIN_EDGE_AFTER_COSTS_USDT", 1.20),
            max_position_margin_pct=_f("ANTI_DRAIN_MAX_POSITION_MARGIN_PCT", 12.0),
            max_used_margin_pct=_f("ANTI_DRAIN_MAX_USED_MARGIN_PCT", 30.0),
            max_open_positions=_i("ANTI_DRAIN_MAX_OPEN_POSITIONS", 2),
            max_active_per_symbol=_i("ANTI_DRAIN_MAX_ACTIVE_PER_SYMBOL", 1),
            max_daily_loss_pct=_f("ANTI_DRAIN_MAX_DAILY_LOSS_PCT", 3.0),
            max_drawdown_pct=_f("ANTI_DRAIN_MAX_DRAWDOWN_PCT", 12.0),
        )


@dataclass(frozen=True)
class Profiles:
    range: RangeEngine
    crt: CrtEngine
    trend: TrendEngine
    scalp_mgmt: ScalpMgmt
    position_mgmt: PositionMgmt
    depth: DepthConfig
    anti_drain: AntiDrainCfg

    @classmethod
    def load(cls) -> "Profiles":
        return cls(
            range=RangeEngine.load(),
            crt=CrtEngine.load(),
            trend=TrendEngine.load(),
            scalp_mgmt=ScalpMgmt.load(),
            position_mgmt=PositionMgmt.load(),
            depth=DepthConfig.load(),
            anti_drain=AntiDrainCfg.load(),
        )


_cache: Profiles | None = None


def get_profiles(refresh: bool = False) -> Profiles:
    """Кэшированный доступ к профилям. refresh=True перечитать из settings."""
    global _cache
    if _cache is None or refresh:
        _cache = Profiles.load()
    return _cache
