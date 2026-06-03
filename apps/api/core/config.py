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
    OWNER_API_TOKEN: str = ""

    # =========================
    # DATABASE
    # =========================
    POSTGRES_DB: str = "robot"
    POSTGRES_USER: str = "robot"
    POSTGRES_PASSWORD: str = "robot"
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    DB_AUTO_CREATE_SCHEMA: bool = True

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
    ALLOW_SHORTS: bool = True
    SIGNAL_PROFILE: str = "learning"
    EXECUTION_MARKET: str = "spot"
    SHORT_ALERT_THROTTLE_MINUTES: int = 60

    MIN_NET_PNL_RELAX_MARGIN_PCT: float = 0.01

    # =========================
    # EXIT POLICY v2
    # Все MFE-пороги рассчитываются динамически в exit_policy.py
    # как доля от stop_distance_pct.
    # Здесь хранятся только loss-пороги и минимальные ограничения.
    # =========================
    # Минимальный MFE до применения early-failed-setup блока.
    FAILED_SETUP_MFE_SOFT_PCT: float = 0.20
    FAILED_SETUP_MFE_MID_PCT: float = 0.45
    FAILED_SETUP_MFE_DEEP_PCT: float = 0.70

    # Failed setup: loss пороги (абсолютные, не динамические)
    # Используются как floor — не закрывать раньше этого убытка
 
    FAILED_SETUP_LOSS_SOFT_PCT: float = -0.35
    FAILED_SETUP_LOSS_MID_PCT: float = -0.55
    FAILED_SETUP_LOSS_DEEP_PCT: float = -0.80
    FAILED_SETUP_MIN_AGE_SEC: int = 300

    # Динамические MFE-пороги (K-коэффициенты) — exit_policy.py
    # K_FAILED_SOFT=0.30, K_FAILED_MID=0.55, K_FAILED_DEEP=0.80
    # K_PROTECT=0.50, K_TRAIL=0.80, K_CAPTURE=0.65
    # Менять здесь не нужно — правятся в exit_policy.py напрямую

    # Защитный trailing — доля MFE которую отдаём при откате
    PROTECTIVE_MFE_START_PCT: float = 0.30
    PROTECTIVE_DRAWDOWN_SHARE: float = 0.35
    ADAPTIVE_TRAIL_MFE_START_PCT: float = 0.80
    ADAPTIVE_TRAIL_DRAWDOWN_PCT: float = 0.35

    # Минимальные ограничения на выход — не фиксировать мелочь
    MIN_PROTECTIVE_EXIT_PCT: float = 0.60
    MIN_POST_TP1_EXIT_PCT: float = 0.45
    MIN_PROTECTIVE_NET_USDT: float = 1.50
    MIN_PROTECTIVE_R_MULT: float = 0.30

    # Adaptive MFE capture experiment: earlier before-TP1 profit lock when
    # fresh paper data shows positive->negative giveback.
    MFE_CAPTURE_ENABLED: bool = True
    MFE_CAPTURE_START_PCT: float = 0.65
    MFE_CAPTURE_DRAWDOWN_PCT: float = 0.30
    MFE_CAPTURE_PROTECT_SHARE: float = 0.35

    # ML outcome memory freshness. If trade_outcomes.jsonl exists but has no
    # recent closed trades, readiness should show that the learning memory is stale.
    ML_OUTCOMES_STALE_HOURS: int = 72

    # Paper/live-shadow validation gates before limited live scaling.
    VALIDATION_MIN_CLOSED_SIGNALS: int = 200
    VALIDATION_FAILED_SETUP_MAX_PCT: float = 35.0
    VALIDATION_POSITIVE_THEN_NEGATIVE_MAX_PCT: float = 25.0

    

    

    

    

    

    

    

    

    

    

    

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
    # Дефолты рассчитаны под spot 0.2% fee paper_trade.
    # Для live поднять MIN_NET_RR_TP1 до 0.65+
    # =========================
    ANTI_DRAIN_ENABLED: bool = True
    ANTI_DRAIN_MIN_CONFIDENCE: float = 55.0
    ANTI_DRAIN_MIN_NET_RR_TP1: float = 0.40       # spot 0.2% paper
    ANTI_DRAIN_MIN_NET_RR_TP2: float = 0.85       # spot 0.2% paper
    ANTI_DRAIN_MIN_EDGE_AFTER_COSTS_USDT: float = 0.80
    ANTI_DRAIN_MAX_POSITION_MARGIN_PCT: float = 12.0
    ANTI_DRAIN_MAX_USED_MARGIN_PCT: float = 30.0
    ANTI_DRAIN_MAX_OPEN_POSITIONS: int = 2
    ANTI_DRAIN_MAX_ACTIVE_PER_SYMBOL: int = 1
    ANTI_DRAIN_MAX_DAILY_LOSS_PCT: float = 3.0
    ANTI_DRAIN_MAX_DRAWDOWN_PCT: float = 12.0

    # =========================
    # PRODUCTION ENTRY GATE
    # Дефолты под spot 0.2% fee paper_trade.
    # Для live: поднять *_PAPER пороги до *_live уровней.
    # =========================

    # Grade A+: setup_score >= 76, confidence >= 68
    PROD_GATE_A_PLUS_MIN_SETUP: float = 76.0
    PROD_GATE_A_PLUS_MIN_CONFIDENCE: float = 68.0
    PROD_GATE_A_PLUS_MIN_RR_TP1: float = 0.95     # live
    PROD_GATE_A_PLUS_MIN_RR_TP1_PAPER: float = 0.60   # spot 0.2% paper
    PROD_GATE_A_PLUS_MIN_RR_TP2: float = 1.45     # live
    PROD_GATE_A_PLUS_MIN_RR_TP2_PAPER: float = 1.15   # spot 0.2% paper

    # Grade A: setup_score >= 62, confidence >= 58
    PROD_GATE_A_MIN_SETUP: float = 62.0
    PROD_GATE_A_MIN_CONFIDENCE: float = 58.0
    PROD_GATE_A_MIN_RR_TP1: float = 0.90          # live
    PROD_GATE_A_MIN_RR_TP1_PAPER: float = 0.50    # spot 0.2% paper
    PROD_GATE_A_MIN_RR_TP2: float = 1.35          # live
    PROD_GATE_A_MIN_RR_TP2_PAPER: float = 1.00    # spot 0.2% paper

    # Grade B: setup_score >= 52, confidence >= 54
    PROD_GATE_B_MIN_SETUP: float = 52.0
    PROD_GATE_B_MIN_CONFIDENCE: float = 54.0
    PROD_GATE_B_MIN_RR_TP1: float = 0.85          # live
    PROD_GATE_B_MIN_RR_TP1_PAPER: float = 0.40    # spot 0.2% paper
    PROD_GATE_B_MIN_RR_TP2: float = 1.30          # live
    PROD_GATE_B_MIN_RR_TP2_PAPER: float = 0.85    # spot 0.2% paper
    PROD_GATE_B_MIN_PRIORITY: float = 85.0

    # =========================
    # RISK MANAGEMENT
    # =========================
    MAX_DAILY_LOSS_PCT: float = 3
    MAX_DRAWDOWN_PCT: float = 15
    MAX_OPEN_POSITIONS: int = 3
    RISK_PER_TRADE_PCT: float = 0.5
    MAX_POSITION_MARGIN_PCT: float = 0.35
    MIN_NET_PNL_TP1_USDT: float = 1.5
    MIN_NET_PNL_TP2_USDT: float = 3.5

    LEVELS_ENTRY_TF: str = "5m"
    LEVELS_SIGNAL_TF: str = "15m"
    LEVELS_CONTEXT_TF: str = "1h"
    LEVELS_STOP_ATR_MULT: float = 2.8
    LEVELS_MIN_STOP_PCT: float = 0.30

    # =========================
    # SETUP QUALITY — LEARNING MODE
    # =========================
    LEARNING_SETUP_MIN_SCORE: float = 56.0
    LEARNING_SETUP_MIN_TREND_ALIGNMENT: float = 25.0
    LEARNING_SETUP_MIN_VOLUME_CONFIRMATION: float = 5.0
    ALLOW_WEAK_VOLUME_TREND_ENTRIES: bool = True
    MIN_TREND_CONTINUATION_SCORE: float = 58.0
    MIN_TREND_STRUCTURE_SCORE: float = 14.0
    LEARNING_TREND_CONTINUATION_MIN_TREND_ALIGNMENT: float = 25.0
    LEARNING_TREND_CONTINUATION_MIN_VOLUME_CONFIRMATION: float = 2.0
    LEARNING_TREND_CONTINUATION_MIN_STRUCTURE_QUALITY: float = 12.0
    LEARNING_TREND_CONTINUATION_MIN_FINAL_SCORE: float = 50.0

    # Publish soft gates — снижены для learning mode
    PUBLISH_WEAK_VOLUME_MAX_COUNT: int = 5
    PUBLISH_WEAK_VOLUME_MIN_CONFIRMATION: float = 2.0
    PUBLISH_MIN_TREND_ALIGNMENT: float = 10.0   # снижен: ADA/SOL имеют 10-30
    PUBLISH_MIN_ENTRY_TIMING: float = 10.0      # снижен: симметрично

    # =========================
    # FEES / COST ENGINE
    # =========================
    SPOT_TAKER_FEE: float = 0.002
    SPOT_MAKER_FEE: float = 0.002
    FUTURES_TAKER_FEE: float = 0.0005
    FUTURES_MAKER_FEE: float = 0.0002
    SLIPPAGE_BUFFER_PCT: float = 0.0002
    FUNDING_BUFFER_PCT: float = 0.0003

    # =========================
    # TRAILING STOP
    # =========================
    ENABLE_TRAILING_STOP: bool = True
    TRAILING_AFTER_TP1: bool = True
    TRAILING_CALLBACK_PCT: float = 0.4

    # =========================
    # HTX FUNDING RATE ARBITRAGE
    # =========================
    ENABLE_FUNDING_ARB: bool = False
    FUNDING_ARB_SYMBOLS: str = "BTC/USDT,ETH/USDT"
    FUNDING_ARB_MIN_RATE_PCT: float = 0.03
    FUNDING_ARB_MIN_EDGE_PCT: float = 0.01
    FUNDING_ARB_MAX_BASIS_PCT: float = 0.35
    FUNDING_ARB_DEFAULT_NOTIONAL_USDT: float = 100.0
    FUNDING_ARB_MAX_NOTIONAL_USDT: float = 500.0
    FUNDING_ARB_CLOSE_RATE_PCT: float = 0.005
    FUNDING_ARB_MAX_HOLD_HOURS: int = 72
    FUNDING_ARB_SCAN_INTERVAL_HOURS: int = 8

    # =========================
    # AFFILIATE / VIP
    # =========================
    HTX_AFFILIATE_LINK: str = ""
    AFFILIATE_FREE_VIP_DAYS: int = 30
    VIP_INVITE_LINK: str = ""

    # =========================
    # PAYMENTS / CHECKOUTS
    # =========================
    PAYMENT_PENDING_EXPIRE_HOURS: int = 48

    # =========================
    # MARKET CONNECTIVITY
    # =========================
    MARKET_CONNECTIVITY_MAX_LATENCY_MS: int = 5000
    MARKET_CONNECTIVITY_MAX_SPREAD_PCT: float = 0.75
    EXCHANGE_RECONCILIATION_ENABLED: bool = False


    def production_blockers(self) -> list[str]:
        blockers: list[str] = []

        if self.APP_ENV == "production":
            if self.DB_AUTO_CREATE_SCHEMA:
                blockers.append("DB_AUTO_CREATE_SCHEMA must be disabled in production; run Alembic migrations")
            if self.JWT_SECRET == "dev-jwt-secret-change-me":
                blockers.append("JWT_SECRET uses development default")
            if self.OWNER_PASSWORD == "owner-password-change-me":
                blockers.append("OWNER_PASSWORD uses development default")
            if not self.OWNER_API_TOKEN:
                blockers.append("OWNER_API_TOKEN is not configured")
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
        if self.ENABLE_FUNDING_ARB and not self.ENABLE_FUTURES:
            blockers.append("ENABLE_FUNDING_ARB requires ENABLE_FUTURES=true for HTX swap hedge")

        return blockers

    @property
    def is_live_enabled(self) -> bool:
        return bool(self.ENABLE_LIVE_ORDERS or self.TRADING_MODE in ["live", "live_limited"])

    @property
    def should_auto_create_schema(self) -> bool:
        return bool(self.DB_AUTO_CREATE_SCHEMA and self.APP_ENV != "production")

    @property
    def database_url(self) -> str:
        return (
            f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def symbols(self) -> List[str]:
        return [s.strip() for s in self.HTX_SYMBOLS.split(",") if s.strip()]

    @property
    def funding_arb_symbols(self) -> List[str]:
        return [s.strip() for s in self.FUNDING_ARB_SYMBOLS.split(",") if s.strip()]

settings = Settings()