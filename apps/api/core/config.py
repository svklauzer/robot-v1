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
    # Доп. origins для CORS (прод-домен дашборда), через запятую.
    # localhost:3000 всегда разрешён (см. property cors_origins).
    CORS_ORIGINS: str = ""

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
    # Optional full connection string. When set (e.g. Render's Internal
    # Database URL), it overrides the split POSTGRES_* values above.
    DATABASE_URL: str = ""
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
    # Proxy for HTX/Huobi API (optional). Same format as TELEGRAM_PROXY_URL.
    HTX_PROXY_URL: str = ""

    # =========================
    # TELEGRAM
    # =========================
    TELEGRAM_BOT_TOKEN: str = ""
    # Username бота без @ (например finmt_bot) — для deep-link в FREE-тизере.
    TELEGRAM_BOT_USERNAME: str = ""
    TELEGRAM_OWNER_CHAT_ID: int = 0
    TELEGRAM_SIGNALS_CHAT_ID: int = 0
    TELEGRAM_FREE_SIGNALS_CHAT_ID: int = 0
    TELEGRAM_VIP_SIGNALS_CHAT_ID: int = 0

    # Proxy for Telegram API (optional).
    # Set to socks5://user:pass@host:port or http://user:pass@host:port
    # if the server cannot reach api.telegram.org directly.
    TELEGRAM_PROXY_URL: str = ""
    # Separate connect timeout to detect network-level blocks quickly.
    TELEGRAM_CONNECT_TIMEOUT: float = 15.0
    TELEGRAM_READ_TIMEOUT: float = 30.0

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
    LIVE_SHADOW_MAX_ENTRY_DRIFT_PCT: float = 0.35
    LIVE_SHADOW_SLIPPAGE_PCT: float = 0.10

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
    FAILED_SETUP_MFE_SOFT_PCT: float = 0.50
    FAILED_SETUP_MFE_MID_PCT: float = 0.80
    FAILED_SETUP_MFE_DEEP_PCT: float = 1.10
    FAILED_SETUP_MFE_ABSOLUTE_MIN_PCT: float = 0.50

    # Пороги убытка для принудительного закрытия слабого setup до TP1.
    FAILED_SETUP_LOSS_SOFT_PCT: float = -0.40
    FAILED_SETUP_LOSS_MID_PCT: float = -0.65
    FAILED_SETUP_LOSS_DEEP_PCT: float = -0.90
    FAILED_SETUP_MIN_AGE_SEC: int = 600

    # MFE-протекция и частичная фиксация в процентах.
    PROTECTIVE_MFE_START_PCT: float = 0.80
    PROTECTIVE_DRAWDOWN_SHARE: float = 0.35
    ADAPTIVE_TRAIL_MFE_START_PCT: float = 0.90
    ADAPTIVE_TRAIL_DRAWDOWN_PCT: float = 0.35

    # Adaptive MFE capture experiment: earlier before-TP1 profit lock when
    # fresh paper data shows positive->negative giveback.
    MFE_CAPTURE_ENABLED: bool = True
    MFE_CAPTURE_START_PCT: float = 0.90
    MFE_CAPTURE_DRAWDOWN_PCT: float = 0.30
    MFE_CAPTURE_PROTECT_SHARE: float = 0.40

    # ML outcome memory. The relative default resolves under /app in Docker and
    # under the repo root in local runs, so the compose bind mount writes to
    # ./storage/ml/trade_outcomes.jsonl on the host.
    TRADE_OUTCOMES_PATH: str = "storage/ml/trade_outcomes.jsonl"
    # If trade_outcomes.jsonl exists but has no recent closed trades, readiness
    # should show that the learning memory is stale.
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
    SYMBOL_PERF_BLOCK_MAX_WINRATE: float = 42.0
    SYMBOL_PERF_REDUCE_MAX_WINRATE: float = 50.0
    SYMBOL_PERF_COOLDOWN_STREAK: int = 4
    SYMBOL_PERF_COOLDOWN_STOPS: int = 3
    SYMBOL_PERF_COOLDOWN_FAILED_SETUPS: int = 3
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
    ANTI_DRAIN_MIN_CONFIDENCE: float = 60.0
    ANTI_DRAIN_MIN_NET_RR_TP1: float = 0.55       # spot 0.2% paper
    ANTI_DRAIN_MIN_NET_RR_TP2: float = 0.90       # spot 0.2% paper
    ANTI_DRAIN_MIN_EDGE_AFTER_COSTS_USDT: float = 1.20
    ANTI_DRAIN_MAX_POSITION_MARGIN_PCT: float = 12.0
    ANTI_DRAIN_MAX_USED_MARGIN_PCT: float = 30.0
    ANTI_DRAIN_MAX_OPEN_POSITIONS: int = 2
    ANTI_DRAIN_MAX_ACTIVE_PER_SYMBOL: int = 1
    ANTI_DRAIN_MAX_DAILY_LOSS_PCT: float = 2.0
    ANTI_DRAIN_MAX_DRAWDOWN_PCT: float = 10.0

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
    PROD_GATE_A_MIN_SETUP: float = 65.0
    PROD_GATE_A_MIN_CONFIDENCE: float = 62.0
    PROD_GATE_A_MIN_RR_TP1: float = 0.90          # live
    PROD_GATE_A_MIN_RR_TP1_PAPER: float = 0.55    # spot 0.2% paper
    PROD_GATE_A_MIN_RR_TP2: float = 1.35          # live
    PROD_GATE_A_MIN_RR_TP2_PAPER: float = 1.05    # spot 0.2% paper

    # Grade B: setup_score >= 58, confidence >= 60
    PROD_GATE_B_MIN_SETUP: float = 58.0
    PROD_GATE_B_MIN_CONFIDENCE: float = 60.0
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
    LEARNING_SETUP_MIN_SCORE: float = 62.0
    LEARNING_SETUP_MIN_TREND_ALIGNMENT: float = 32.0
    LEARNING_SETUP_MIN_VOLUME_CONFIRMATION: float = 8.0
    ALLOW_WEAK_VOLUME_TREND_ENTRIES: bool = True
    MIN_TREND_CONTINUATION_SCORE: float = 62.0
    MIN_TREND_STRUCTURE_SCORE: float = 16.0
    LEARNING_TREND_CONTINUATION_MIN_TREND_ALIGNMENT: float = 32.0
    LEARNING_TREND_CONTINUATION_MIN_VOLUME_CONFIRMATION: float = 4.0
    LEARNING_TREND_CONTINUATION_MIN_STRUCTURE_QUALITY: float = 12.0
    LEARNING_TREND_CONTINUATION_MIN_FINAL_SCORE: float = 55.0

    # Минимальная защищаемая прибыль для exit-политики, чтобы не фиксировать микро-движения.
    MIN_PROTECTIVE_EXIT_PCT: float = 1.80
    MIN_POST_TP1_EXIT_PCT: float = 0.80
    MIN_PROTECTIVE_NET_USDT: float = 2.50
    MIN_PROTECTIVE_R_MULT: float = 0.30

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

    # Entry thresholds
    FUNDING_ARB_MIN_RATE_PCT: float = 0.015     # min funding rate per period (8h) to consider
    FUNDING_ARB_MAX_BASIS_PCT: float = 0.50     # max abs(basis) allowed at entry
    # Min net yield PER PERIOD after estimated fees (funding_pct - fee_amortized_pct).
    # Positive means profitable; negative means fees exceed income.
    FUNDING_ARB_MIN_NET_YIELD_PCT: float = 0.005

    # Position sizing
    FUNDING_ARB_DEFAULT_NOTIONAL_USDT: float = 100.0
    FUNDING_ARB_MAX_NOTIONAL_USDT: float = 500.0
    FUNDING_ARB_MAX_OPEN_HEDGES: int = 2         # max concurrent paper/live positions

    # Exit thresholds
    FUNDING_ARB_CLOSE_RATE_PCT: float = 0.005   # close when funding rate drops below this
    FUNDING_ARB_MAX_HOLD_HOURS: int = 240        # max hold time (10 days = 30 funding periods)
    FUNDING_ARB_MIN_HOLD_PERIODS: int = 3        # don't close before collecting at least 3 periods

    # Scan settings
    FUNDING_ARB_SCAN_INTERVAL_HOURS: int = 8
    # Automatically open paper positions when a profitable candidate is found
    FUNDING_ARB_AUTO_OPEN_PAPER: bool = True
    # Expected hold periods for annualized return calculation (default 10 × 8h = 80h)
    FUNDING_ARB_ASSUMED_HOLD_PERIODS: int = 10

    # Legacy — kept for compatibility
    FUNDING_ARB_MIN_EDGE_PCT: float = 0.01

    # =========================
    # AFFILIATE / VIP
    # =========================
    HTX_AFFILIATE_LINK: str = ""
    AFFILIATE_FREE_VIP_DAYS: int = 30
    VIP_INVITE_LINK: str = ""

    # ── HTX affiliate auto-verification ──────────────────────────────────────
    # Когда True — перед выдачей триала бот спрашивает HTX UID и проверяет его
    # через affiliate-API. Когда False — прежнее поведение (self-claim).
    HTX_AFFILIATE_VERIFY_ENABLED: bool = False
    HTX_AFFILIATE_API_KEY: str = ""
    HTX_AFFILIATE_API_SECRET: str = ""
    HTX_AFFILIATE_API_HOST: str = "api.huobi.pro"
    # Путь эндпоинта со списком приглашённых — ПОДСТАВИТЬ из affiliate-доков HTX.
    # Должен возвращать список UID, приглашённых владельцем ключа.
    HTX_AFFILIATE_INVITEES_PATH: str = "/v2/affiliate/invitees"

    # =========================
    # PAYMENTS / CHECKOUTS
    # =========================
    PAYMENT_PENDING_EXPIRE_HOURS: int = 48

    # ── Telegram Stars (XTR) ─────────────────────────────────────────────────
    # Цена каждого тарифа в звёздах Telegram (целое число XTR).
    # Задать реальные значения в env; 0 = тариф недоступен для Stars-оплаты.
    VIP_STARS_PRICE_30: int = 0
    VIP_STARS_PRICE_90: int = 0
    # Сколько часов живёт одноразовая invite-ссылка в приватный VIP-канал.
    VIP_INVITE_EXPIRE_HOURS: int = 24

    def stars_price_for_plan(self, plan_code: str) -> int:
        return {
            "vip_30": self.VIP_STARS_PRICE_30,
            "vip_90": self.VIP_STARS_PRICE_90,
        }.get(plan_code, 0)

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
        if self.DATABASE_URL:
            url = self.DATABASE_URL
            # Render/Heroku-style URLs use the legacy "postgres://" scheme,
            # which SQLAlchemy no longer recognizes — normalize it.
            if url.startswith("postgres://"):
                url = "postgresql://" + url[len("postgres://"):]
            return url
        return (
            f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def cors_origins(self) -> List[str]:
        defaults = ["http://localhost:3000", "http://127.0.0.1:3000"]
        extra = [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]
        # dedupe, сохраняя порядок
        return list(dict.fromkeys(defaults + extra))

    @property
    def symbols(self) -> List[str]:
        return [s.strip() for s in self.HTX_SYMBOLS.split(",") if s.strip()]

    @property
    def funding_arb_symbols(self) -> List[str]:
        return [s.strip() for s in self.FUNDING_ARB_SYMBOLS.split(",") if s.strip()]

settings = Settings()