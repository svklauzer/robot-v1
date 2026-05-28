from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    
    # =========================
    # APP
    # =========================
    APP_ENV: str = "development"
    JWT_SECRET: str = "dev-jwt-secret-change-me"
    NEXT_PUBLIC_API_URL: str = "http://localhost:8000"

    OWNER_EMAIL: str = "owner@example.com"
    OWNER_PASSWORD: str = "owner-password-change-me"

    # =========================
    # DATABASE
    # =========================
    POSTGRES_DB: str = "robot"
    POSTGRES_USER: str = "robot"
    POSTGRES_PASSWORD: str = "robot"
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432

    REDIS_URL: str = "redis://localhost:6379"

    # =========================
    # HTX API
    # =========================
    HTX_API_KEY: str = ""
    HTX_API_SECRET: str = ""
    HTX_MARKET_TYPE: str = "spot"
    HTX_SYMBOLS: str = "BTC/USDT,ETH/USDT"
    ALLOW_MARKET_MOCK: bool = False

    # =========================
    # TELEGRAM
    # =========================
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_OWNER_CHAT_ID: int = 0
    TELEGRAM_SIGNALS_CHAT_ID: int = 0
    TELEGRAM_FREE_SIGNALS_CHAT_ID: int = 0
    TELEGRAM_VIP_SIGNALS_CHAT_ID: int = 0

    MAX_ACTIVE_SIGNALS: int = 4
    MAX_ACTIVE_SIGNALS_PER_SYMBOL: int = 1
    RISK_EQUITY_USDT: float = 950.0
    MAX_USED_MARGIN_PCT: float = 0.85

    # =========================
    # NEWS / ROBOT
    # =========================
    NEWS_ENABLED: bool = True
    ROBOT_MODE: str = "paper"

    # =========================
    # TRADE ENGINE
    # =========================
    TRADING_MODE: str = "paper_signal"
    MARKET_TYPE: str = "spot"
    ENABLE_LIVE_ORDERS: bool = False

    ENABLE_FUTURES: bool = False
    FUTURES_MARGIN_MODE: str = "isolated"
    FUTURES_LEVERAGE: int = 1

    ALLOW_SHORTS: bool = False
    SIGNAL_PROFILE: str = "learning"
    # spot | margin | futures
    EXECUTION_MARKET: str = "spot"
    SHORT_ALERT_THROTTLE_MINUTES: int = 60

    MIN_NET_PNL_RELAX_MARGIN_PCT: float = 0.01


    # =========================
    # EXECUTION PLAN V1 TUNING
    # =========================
    # Минимальный MFE до применения early-failed-setup блока.
    FAILED_SETUP_MFE_SOFT_PCT: float = 0.20
    FAILED_SETUP_MFE_MID_PCT: float = 0.45
    FAILED_SETUP_MFE_DEEP_PCT: float = 0.70

    # Пороги убытка для принудительного закрытия слабого setup до TP1.
    FAILED_SETUP_LOSS_SOFT_PCT: float = -0.25
    FAILED_SETUP_LOSS_MID_PCT: float = -0.45
    FAILED_SETUP_LOSS_DEEP_PCT: float = -0.70
    FAILED_SETUP_MIN_AGE_SEC: int = 180

    # MFE-протекция и частичная фиксация в процентах.
    PROTECTIVE_MFE_START_PCT: float = 0.30
    PROTECTIVE_DRAWDOWN_SHARE: float = 0.50
    ADAPTIVE_TRAIL_MFE_START_PCT: float = 0.80
    ADAPTIVE_TRAIL_DRAWDOWN_PCT: float = 0.45

    # =========================
    # SYMBOL PERFORMANCE GUARD
    # =========================
    SYMBOL_PERF_LOOKBACK: int = 12
    SYMBOL_PERF_MIN_HISTORY: int = 3
    SYMBOL_PERF_BLOCK_MIN_HISTORY: int = 5
    SYMBOL_PERF_BLOCK_MAX_WINRATE: float = 40.0
    SYMBOL_PERF_REDUCE_MAX_WINRATE: float = 45.0
    SYMBOL_PERF_COOLDOWN_STREAK: int = 3
    SYMBOL_PERF_COOLDOWN_STOPS: int = 3
    SYMBOL_PERF_COOLDOWN_FAILED_SETUPS: int = 4
    SYMBOL_PERF_SMALL_HISTORY_STOP_MULTIPLIER: float = 0.65
    SYMBOL_PERF_WEAK_MULTIPLIER: float = 0.45
    SYMBOL_PERF_GIVEBACK_MULTIPLIER: float = 0.60
    SYMBOL_PERF_GIVEBACK_TRIGGER: int = 3

    # =========================
    # ANTI-DRAIN ENTRY GUARD
    # =========================
    ANTI_DRAIN_ENABLED: bool = True
    ANTI_DRAIN_MIN_CONFIDENCE: float = 58.0
    ANTI_DRAIN_MIN_NET_RR_TP1: float = 0.95
    ANTI_DRAIN_MIN_NET_RR_TP2: float = 1.35
    ANTI_DRAIN_MIN_EDGE_AFTER_COSTS_USDT: float = 0.80
    ANTI_DRAIN_MAX_POSITION_MARGIN_PCT: float = 12.0
    ANTI_DRAIN_MAX_USED_MARGIN_PCT: float = 30.0
    ANTI_DRAIN_MAX_OPEN_POSITIONS: int = 2
    ANTI_DRAIN_MAX_ACTIVE_PER_SYMBOL: int = 1
    ANTI_DRAIN_MAX_DAILY_LOSS_PCT: float = 3.0
    ANTI_DRAIN_MAX_DRAWDOWN_PCT: float = 12.0

    # =========================
    # PRODUCTION ENTRY GATE
    # =========================
    PROD_GATE_A_PLUS_MIN_SETUP: float = 82.0
    PROD_GATE_A_PLUS_MIN_CONFIDENCE: float = 74.0
    PROD_GATE_A_PLUS_MIN_RR_TP1: float = 0.95
    PROD_GATE_A_PLUS_MIN_RR_TP1_PAPER: float = 0.84
    PROD_GATE_A_PLUS_MIN_RR_TP2_PAPER: float = 1.30
    PROD_GATE_A_PLUS_MIN_RR_TP2: float = 1.45

    PROD_GATE_A_MIN_SETUP: float = 76.0
    PROD_GATE_A_MIN_CONFIDENCE: float = 70.0
    PROD_GATE_A_MIN_RR_TP1: float = 0.90
    PROD_GATE_A_MIN_RR_TP1_PAPER: float = 0.78
    PROD_GATE_A_MIN_RR_TP2_PAPER: float = 1.20
    PROD_GATE_A_MIN_RR_TP2: float = 1.35

    PROD_GATE_B_MIN_SETUP: float = 70.0
    PROD_GATE_B_MIN_CONFIDENCE: float = 60.0
    PROD_GATE_B_MIN_RR_TP1: float = 0.85
    PROD_GATE_B_MIN_RR_TP1_PAPER: float = 0.75
    PROD_GATE_B_MIN_RR_TP2: float = 1.30
    PROD_GATE_B_MIN_RR_TP2_PAPER: float = 1.15
    PROD_GATE_B_MIN_PRIORITY: float = 85.0

    # =========================
    # RISK MANAGEMENT
    # =========================
    MAX_DAILY_LOSS_PCT: float = 3
    MAX_DRAWDOWN_PCT: float = 15
    MAX_OPEN_POSITIONS: int = 3
    RISK_PER_TRADE_PCT: float = 0.5
    MAX_POSITION_MARGIN_PCT: float = 0.35
    MIN_NET_PNL_TP1_USDT: float = 2.5
    MIN_NET_PNL_TP2_USDT: float = 5.5

    # Таймфрейм и буферы для построения уровней входа/стопа/тейков.
    LEVELS_ENTRY_TF: str = "5m"
    LEVELS_SIGNAL_TF: str = "15m"
    LEVELS_CONTEXT_TF: str = "1h"
    LEVELS_STOP_ATR_MULT: float = 1.6
    LEVELS_MIN_STOP_PCT: float = 0.30

    # Дополнительные фильтры качества setup в learning/paper.
    LEARNING_SETUP_MIN_SCORE: float = 62.0
    LEARNING_SETUP_MIN_TREND_ALIGNMENT: float = 45.0
    LEARNING_SETUP_MIN_VOLUME_CONFIRMATION: float = 6.0
    ALLOW_WEAK_VOLUME_TREND_ENTRIES: bool = False
    MIN_TREND_CONTINUATION_SCORE: float = 58.0
    MIN_TREND_STRUCTURE_SCORE: float = 14.0
    LEARNING_TREND_CONTINUATION_MIN_TREND_ALIGNMENT: float = 35.0
    LEARNING_TREND_CONTINUATION_MIN_VOLUME_CONFIRMATION: float = 2.0
    LEARNING_TREND_CONTINUATION_MIN_STRUCTURE_QUALITY: float = 12.0
    LEARNING_TREND_CONTINUATION_MIN_FINAL_SCORE: float = 50.0
    # Paper/publish soft gates for already approved learning setups.
    # Keep configurable to avoid deadlock when the market produces
    # valid candidates with trend_alignment ~= 30.
    PUBLISH_WEAK_VOLUME_MAX_COUNT: int = 4
    PUBLISH_WEAK_VOLUME_MIN_CONFIRMATION: float = 3.0
    PUBLISH_MIN_TREND_ALIGNMENT: float = 30.0
    PUBLISH_MIN_ENTRY_TIMING: float = 12.0

    # Минимальная защищаемая прибыль для exit-политики, чтобы не фиксировать микро-движения.
    MIN_PROTECTIVE_EXIT_PCT: float = 0.20
    MIN_POST_TP1_EXIT_PCT: float = 0.35
    MIN_PROTECTIVE_NET_USDT: float = 0.25
    MIN_PROTECTIVE_R_MULT: float = 0.05

    # =========================
    # FEES / COST ENGINE
    # =========================
    SPOT_TAKER_FEE: float = 0.002
    SPOT_MAKER_FEE: float = 0.002

    FUTURES_TAKER_FEE: float = 0.0005
    FUTURES_MAKER_FEE: float = 0.0002

    SLIPPAGE_BUFFER_PCT: float = 0.0005
    FUNDING_BUFFER_PCT: float = 0.0003

    # =========================
    # TRAILING STOP
    # =========================
    ENABLE_TRAILING_STOP: bool = True
    TRAILING_AFTER_TP1: bool = True
    TRAILING_CALLBACK_PCT: float = 0.4

    # =========================
    # AFFILIATE / VIP
    # =========================
    HTX_AFFILIATE_LINK: str = ""
    AFFILIATE_FREE_VIP_DAYS: int = 30
    VIP_INVITE_LINK: str = ""


    def production_blockers(self) -> list[str]:
        blockers: list[str] = []

        if self.APP_ENV == "production":
            if self.JWT_SECRET == "dev-jwt-secret-change-me":
                blockers.append("JWT_SECRET uses development default")
            if self.OWNER_PASSWORD == "owner-password-change-me":
                blockers.append("OWNER_PASSWORD uses development default")
            if not self.TELEGRAM_BOT_TOKEN:
                blockers.append("TELEGRAM_BOT_TOKEN is not configured")
            if not self.HTX_API_KEY or not self.HTX_API_SECRET:
                blockers.append("HTX API credentials are not configured")

        if self.ENABLE_LIVE_ORDERS and self.TRADING_MODE not in ["live", "live_limited"]:
            blockers.append("ENABLE_LIVE_ORDERS requires TRADING_MODE=live or live_limited")
        if self.ENABLE_LIVE_ORDERS and self.ROBOT_MODE == "paper":
            blockers.append("ENABLE_LIVE_ORDERS cannot run with ROBOT_MODE=paper")
        if self.ENABLE_LIVE_ORDERS and not self.TELEGRAM_BOT_TOKEN:
            blockers.append("live orders require Telegram owner alerts")

        return blockers

    @property
    def is_live_enabled(self) -> bool:
        return bool(self.ENABLE_LIVE_ORDERS or self.TRADING_MODE in ["live", "live_limited"])

    @property
    def database_url(self) -> str:
        return (
            f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def symbols(self) -> List[str]:
        return [s.strip() for s in self.HTX_SYMBOLS.split(",") if s.strip()]

settings = Settings()