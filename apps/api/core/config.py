from __future__ import annotations
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List

# Допустимые значения строковых режимов. Исключает тихие опечатки вроде "eforce".
_MODE_CHOICES: dict[str, frozenset[str]] = {
    "ENTRY_IMPULSE_LATCH_MODE": frozenset({"shadow", "enforce"}),
    "TZ_MODE": frozenset({"shadow", "enforce"}),
    "TREND_TRIGGER_MODE": frozenset({"shadow", "enforce"}),
    "TP_REACH_MODE": frozenset({"shadow", "enforce"}),
    "MOMENTUM_GATE_MODE": frozenset({"off", "shadow", "enforce"}),
    "ML_MODE": frozenset({"off", "shadow", "advisory", "full_auto"}),
}

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="allow",  # Разрешаем динамические параметры стратегий из Render env
    )

    # ==========================================================================
    # ⚙️ SYSTEM CORE & NETWORKING (APP / DATABASE / REDIS)
    # ==========================================================================
    APP_ENV: str = "development"
    JWT_SECRET: str = "dev-jwt-secret-change-me"
    NEXT_PUBLIC_API_URL: str = "http://localhost:8000"
    CORS_ORIGINS: str = ""
    OWNER_EMAIL: str = "owner@example.com"
    OWNER_PASSWORD: str = "owner-password-change-me"
    OWNER_API_TOKEN: str = ""

    POSTGRES_DB: str = "robot"
    POSTGRES_USER: str = "robot"
    POSTGRES_PASSWORD: str = "robot"
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    DATABASE_URL: str = ""
    DB_AUTO_CREATE_SCHEMA: bool = True
    REDIS_URL: str = "redis://localhost:6379"

    # ==========================================================================
    # 📡 EXCHANGE CONTOURS (HTX & OKX API SATELLITE)
    # ==========================================================================
    ACTIVE_EXCHANGE: str = "htx"
    EXCHANGE_SWITCH_GUARD_ENABLED: bool = True
    LOOP_SKIP_HEARTBEAT_SEC: float = 900.0

    HTX_API_KEY: str = ""
    HTX_API_SECRET: str = ""
    HTX_API_HOSTNAME: str = ""
    HTX_API_HOSTNAME_FALLBACKS: str = "api.huobi.pro,api-aws.huobi.pro,api.htx.com"
    HTX_HTTP_TIMEOUT_MS: int = 15000
    HTX_CIRCUIT_FAILURE_THRESHOLD: int = 2
    HTX_CIRCUIT_OPEN_SECONDS: float = 120.0
    HTX_PROXY_URL: str = ""
    HTX_SYMBOLS: str = "BTC/USDT,ETH/USDT,SOL/USDT,XRP/USDT,AVAX/USDT,TRX/USDT,ADA/USDT,DOT/USDT,LINK/USDT,LTC/USDT"

    OKX_API_KEY: str = ""
    OKX_API_SECRET: str = ""
    OKX_API_PASSPHRASE: str = ""
    OKX_API_HOSTNAME: str = ""
    OKX_API_HOSTNAME_FALLBACKS: str = ""
    OKX_HTTP_TIMEOUT_MS: int = 15000
    OKX_CIRCUIT_FAILURE_THRESHOLD: int = 2
    OKX_CIRCUIT_OPEN_SECONDS: float = 120.0
    OKX_MARKET_TYPE: str = "spot"
    OKX_PROXY_URL: str = ""
    OKX_SYMBOLS: str = "BTC/USDT,ETH/USDT,SOL/USDT,XRP/USDT,DOGE/USDT,HYPE/USDT,LINK/USDT,LTC/USDT,CHIP/USDT,PI/USDT"
    
    EGRESS_GUARD_ENABLED: bool = True
    EGRESS_DNS_TIMEOUT_SEC: float = 3.0
    EGRESS_CACHE_TTL_SEC: float = 30.0
    EGRESS_MONITOR_ENABLED: bool = True
    EGRESS_MONITOR_INTERVAL_SEC: int = 60
    EGRESS_MONITOR_TIMEOUT_SEC: float = 5.0
    EGRESS_MONITOR_PATH: str = "storage/ml/egress_monitor.jsonl"
    EGRESS_MONITOR_MAX_BYTES: int = 20 * 1024 * 1024

    # ==========================================================================
    # 📢 BROADCAST & TELEGRAM GATEWAYS
    # ==========================================================================
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_BOT_USERNAME: str = ""
    TELEGRAM_OWNER_CHAT_ID: int = 0
    TELEGRAM_FREE_SIGNALS_CHAT_ID: int = 0
    TELEGRAM_VIP_SIGNALS_CHAT_ID: int = 0
    TELEGRAM_PROXY_URL: str = ""
    TELEGRAM_CONNECT_TIMEOUT: float = 15.0
    TELEGRAM_READ_TIMEOUT: float = 30.0
    TELEGRAM_WEBHOOK_SECRET: str = ""
    PUBLIC_API_URL: str = ""

    # ==========================================================================
    # 📉 GLOBAL RISK MANAGEMENT & CAPITAL ENVELOPES
    # ==========================================================================
    RISK_EQUITY_USDT: float = 300.0
    RISK_PER_TRADE_PCT: float = 1.0
    MAX_POSITION_MARGIN_PCT: float = 0.13
    MAX_ACTIVE_SIGNALS: int = 100
    MAX_ACTIVE_SIGNALS_PER_SYMBOL: int = 1
    MAX_USED_MARGIN_PCT: float = 0.85
    MAX_DAILY_LOSS_PCT: float = 6.0
    MAX_DRAWDOWN_PCT: float = 15.0
    MAX_TRADES_PER_DAY: int = 100
    
    # Динамический калькулятор маржинальных слотов
    ENABLE_DYNAMIC_MARGIN_ALLOC: bool = True
    DYNAMIC_MARGIN_CAP_PCT_OF_FREE: float = 1.0
    DYNAMIC_MARGIN_FAIR_SHARE: bool = False
    DYNAMIC_MARGIN_LEVERAGE: float = 1.0
    DYNAMIC_MARGIN_B_CAP_PCT_OF_FREE: float = 1.0

    # Автономная сегментация бюджетов (Защита от перекрестного выедания маржи)
    CAPITAL_ENVELOPE_DIRECTIONAL_PCT: float = 70.0  # Бюджет TREND и CRT
    CAPITAL_ENVELOPE_ARB_PCT: float = 20.0          # Бюджет RANGE
    CAPITAL_ENVELOPE_GRID_PCT: float = 5.0           # Бюджет SCALP
    UNIFIED_MARGIN_ACCOUNTING: bool = True

    # Лимиты контроля просадки (Anti-Drain Guard)
    ANTI_DRAIN_ENABLED: bool = True
    ANTI_DRAIN_MIN_CONFIDENCE: float = 60.0
    ANTI_DRAIN_MAX_OPEN_POSITIONS: int = 5
    ANTI_DRAIN_MAX_ACTIVE_PER_SYMBOL: int = 1
    ANTI_DRAIN_MAX_DAILY_LOSS_PCT: float = 6.0
    ANTI_DRAIN_MAX_DRAWDOWN_PCT: float = 10.0
    ANTI_DRAIN_POSITION_MAX_MARGIN_PCT: float = 15.0
    ANTI_DRAIN_POSITION_MAX_USED_MARGIN_PCT: float = 70.0
    ANTI_DRAIN_MIN_NET_RR_TP1: float = 0.10
    ANTI_DRAIN_MIN_NET_RR_TP2: float = 0.90
    ANTI_DRAIN_MIN_EDGE_AFTER_COSTS_USDT: float = 1.20
    ANTI_DRAIN_MIN_EDGE_AFTER_COSTS_PCT: float = 0.0

    # Ограничения перегрузки кластеров и кулдауны Churn-эффекта
    CORR_CLUSTER_ENABLED: bool = True
    CORR_CLUSTER_MAX_SAME_DIR: int = 2
    CORR_CLUSTER_PER_ENGINE: bool = True
    CORR_CLUSTER_PORTFOLIO_MAX_SAME_DIR: int = 5
    CORR_CLUSTER_SYMBOLS: str = ""
    
    REENTRY_COOLDOWN_ENABLED: bool = True
    REENTRY_COOLDOWN_PER_ENGINE: bool = True
    REENTRY_ADVERSE_PRICE_GUARD_ENABLED: bool = True
    REENTRY_ADVERSE_CHURN_MAX_PCT: float = 0.30
    REENTRY_ADVERSE_WINDOW_MINUTES: float = 240.0
    POST_LOSS_COOLDOWN_ENABLED: bool = True
    POST_LOSS_COOLDOWN_MIN: float = 25.0

    DAILY_REPORT_MIN_SAMPLE: int = 5
    TRADEABLE_REGIMES: str = ""
    NEWS_ENABLED: bool = True
    ROBOT_MODE: str = "paper"
    TRADING_MODE: str = "paper_signal"
    MARKET_TYPE: str = "spot"
    ENABLE_LIVE_ORDERS: bool = False
    LIVE_SHADOW_MAX_ENTRY_DRIFT_PCT: float = 0.35
    LIVE_SHADOW_SLIPPAGE_PCT: float = 0.10
    LIVE_EXECUTION_MODE: str = "dry_run"
    LIVE_SET_LEVERAGE: bool = True
    LIVE_MARGIN_MODE: str = "cross"
    LIVE_FILL_POLL_TIMEOUT_SEC: float = 10.0
    LIVE_FILL_POLL_INTERVAL_SEC: float = 1.0
    LIVE_MAX_ORDER_NOTIONAL_USDT: float = 0.0
    LIVE_MAX_ORDER_NOTIONAL_PCT: float = 20.0
    LIVE_SIZE_FROM_BALANCE: bool = True
    LIVE_BALANCE_CACHE_SEC: float = 30.0
    LIVE_EXCHANGE_STOP_ENABLED: bool = True
    LIVE_EXCHANGE_STOP_BUFFER_PCT: float = 0.5
    LIVE_EXCHANGE_STOP_MIN_MOVE_PCT: float = 0.1
    LIVE_EXCHANGE_STOP_VERIFY_SEC: float = 60.0
    LIVE_EXCHANGE_STOP_HALT_AFTER_FAILURES: int = 3
    LIVE_MAX_LEVERAGE: float = 10.0
    TREND_MARGIN_MODE: str = "isolated"
    ENABLE_FUTURES: bool = False
    FUTURES_LEVERAGE: int = 10
    ALLOW_SHORTS: bool = True
    SIGNAL_PROFILE: str = "learning"
    EXECUTION_MARKET: str = "spot"
    SHORT_ALERT_THROTTLE_MINUTES: int = 60
    MIN_NET_PNL_RELAX_MARGIN_PCT: float = 0.01

    VALIDATION_MIN_CLOSED_SIGNALS: int = 50
    VALIDATION_FAILED_SETUP_MAX_PCT: float = 35.0
    VALIDATION_POSITIVE_THEN_NEGATIVE_MAX_PCT: float = 25.0

    # Храповики Продакшн Скоринга (Production Entry Gates)
    PROD_GATE_A_PLUS_MIN_SETUP: float = 76.0
    PROD_GATE_A_PLUS_MIN_CONFIDENCE: float = 68.0
    PROD_GATE_A_PLUS_MIN_RR_TP1: float = 0.95
    PROD_GATE_A_PLUS_MIN_RR_TP1_PAPER: float = 0.10
    PROD_GATE_A_PLUS_MIN_RR_TP2: float = 1.45
    
    PROD_GATE_A_MIN_SETUP: float = 65.0
    PROD_GATE_A_MIN_CONFIDENCE: float = 62.0
    PROD_GATE_A_MIN_RR_TP1: float = 0.90
    PROD_GATE_A_MIN_RR_TP1_PAPER: float = 0.10
    PROD_GATE_A_MIN_RR_TP2: float = 1.35
    
    PROD_GATE_B_MIN_SETUP: float = 58.0
    PROD_GATE_B_MIN_CONFIDENCE: float = 60.0
    PROD_GATE_B_MIN_RR_TP1: float = 0.85
    PROD_GATE_B_MIN_RR_TP1_PAPER: float = 0.10
    PROD_GATE_B_MIN_RR_TP2: float = 1.30
    PROD_GATE_B_MIN_PRIORITY: float = 85.0

    GRADE_A_PLUS_MIN_SCORE: float = 82.0
    GRADE_A_MIN_SCORE: float = 73.0
    GRADE_B_MIN_SCORE: float = 62.0

    # ==========================================================================
    # 🎯 ДВИЖОК №1: TREND (Классическое тренд-фолловинг KAMA контур)
    # ==========================================================================
    TREND_TRIGGER_MODE: str = "enforce"
    TREND_TRIGGER_TF: str = "15m"
    TREND_MAX_EXTENSION_ATR: float = 3.5
    
    TREND_RIDE_ENABLED: bool = True
    TREND_RIDE_MIN_MFE_TO_PROTECT_PCT: float = 0.8
    TREND_RIDE_TRAIL_DRAWDOWN_PCT: float = 0.50
    
    TREND_CAPTURE_BAND_ENABLED: bool = True
    TREND_CAPTURE_ARM_PCT: float = 0.25
    TREND_CAPTURE_GIVEBACK_SHARE: float = 0.25
    TREND_CAPTURE_FLOOR_PCT: float = 0.30
    
    TREND_HTF_EXTREME_VETO: bool = True
    TREND_HTF_RSI_HARD_OVERHEAT: float = 72.0
    TREND_HTF_RSI_HARD_OVERSOLD: float = 28.0
    
    TREND_TP1_R_MULT: float = 1.7
    TREND_TP2_R_MULT: float = 2.0
    TREND_TP1_FLOOR_PCT: float = 1.2
    TREND_TP2_FLOOR_PCT: float = 2.0
    
    # Пороги MI Каскада переключения режимов флэт/тренд
    REGIME_TREND_ADX_MIN: float = 22.0
    REGIME_CHOP_ADX_MAX: float = 18.0

    # ==========================================================================
    # ↔️ ДВИЖОК №2: RANGE (Mean-Reversion канальный флэт)
    # ==========================================================================
    ENABLE_RANGE_STRATEGY: bool = True
    RANGE_MIN_WIDTH_PCT: float = 1.8
    RANGE_SUPPORT_ZONE: float = 0.30
    RANGE_ENTRY_RSI_MIN: float = 25.0
    RANGE_ENTRY_RSI_MAX: float = 52.0
    RANGE_MIN_TP1_NET_PCT: float = 0.8
    RANGE_TP2_RESISTANCE_BUFFER: float = 0.10
    RANGE_TP2_DYNAMIC_ENABLED: bool = False
    RANGE_TP2_DYNAMIC_MIN_BUFFER: float = 0.0
    RANGE_TP2_DYNAMIC_ADX_BASE: float = 15.0
    RANGE_TP2_DYNAMIC_ADX_SPAN: float = 15.0
    RANGE_STOP_ATR_MULT: float = 2.5
    RANGE_MIN_SETUP_SCORE: float = 60.0
    RANGE_ALLOW_SHORT: bool = True
    RANGE_CONFIRMED_ONLY: bool = True
    RANGE_TIME_STOP_MIN: float = 90.0
    RANGE_POS_GATE_ENABLED: bool = True
    RANGE_POS_ANCHOR_TF: str = "1h"
    RANGE_POS_SHORT_MIN: float = 0.40
    RANGE_POS_LONG_MAX: float = 0.60

    # ==========================================================================
    # 🕯️ ДВИЖОК №3: CRT (Candle Range Theory — Premium/Discount сплиты)
    # ==========================================================================
    ENABLE_CRT_STRATEGY: bool = True
    CRT_HTF_TF: str = "4h"
    CRT_LTF_TF: str = "15m"
    CRT_MIN_RANGE_PCT: float = 1.5
    CRT_LTF_CONFIRM: str = "fvg"
    CRT_REQUIRE_CISD: bool = True
    CRT_TARGETS_MODE: str = "extended"
    CRT_REQUIRE_PREMIUM_DISCOUNT: bool = True
    CRT_STOP_BUFFER_PCT: float = 0.05
    CRT_TP2_RR: float = 2.0
    CRT_MIN_TP1_NET_PCT: float = 0.5
    CRT_MIN_RR_TP1: float = 1.0
    CRT_ALLOW_LONG: bool = True
    CRT_ALLOW_SHORT: bool = True
    CRT_MIN_SETUP_SCORE: float = 55.0
    CRT_REQUIRE_MOMENTUM_ALIGN: bool = True
    CRT_REQUIRE_TREND_ALIGN: bool = True
    CRT_TP2_RENDER_RR: float = 2.0

    # Вектор АТР расширения целей CRT
    CRT_TP2_DYNAMIC_ENABLED: bool = True
    CRT_TP2_DYNAMIC_MAX_RE: float = 3.5
    CRT_TP2_DYNAMIC_MAX_RR: float = 3.5
    CRT_TP2_DYNAMIC_ADX_BASE: float = 23.0
    CRT_TP2_DYNAMIC_ADX_SPAN: float = 27.0
    CRT_TP2_DYNAMIC_ATR_EXP_SPAN: float = 0.35

    # ==========================================================================
    # ⚡ ДВИЖОК №4: SCALP (Микроструктура книги стакана)
    # ==========================================================================
    ENABLE_SCALP_STRATEGY: bool = True
    SCALP_EDGE_ZONE: float = 0.25
    SCALP_MIN_MICRO_WIDTH_PCT: float = 1.2
    SCALP_TARGET_PCT: float = 0.5
    SCALP_TP2_MULT: float = 2.0
    SCALP_STOP_BUFFER_ATR: float = 1.0
    SCALP_MIN_OBI: float = 0.10
    SCALP_ENG_MIN_TP1_NET_PCT: float = 0.3
    SCALP_ENG_ALLOW_SHORT: bool = True
    SCALP_MIN_SETUP_SCORE: float = 50.0
    SCALP_MAX_SPREAD_PCT: float = 0.06
    SCALP_REQUIRE_DEPTH: bool = True
    SCALP_HTF_EXTREME_VETO: bool = True
    SCALP_HTF_RSI_OVERHEAT: float = 70.0
    SCALP_HTF_RSI_OVERSOLD: float = 30.0

    # ==========================================================================
    # 🛠️ PROTECTION, VOLATILITY & ELASTIC ATR RATCHETS
    # ==========================================================================
    TZ_USE_DYNAMIC_ATR_STOPS: bool = True
    TZ_STOP_MIN_DIST_ATR_MULT: int = 3
    TZ_EXIT_KAMA_BUFFER_ATR_MULT: float = 2.0
    TZ_STOP_LOSS_ATR_MULT: float = 4             # Дистанция стопа входа: ATR * 4 глубоко за шумом
    TZ_HARD_STOP_LOSS_PCT: float = 2.5           # Аварийная планка принудительно урезана до 2.5% для live
    TZ_DISASTER_STOP_PCT: float = 5.0

    # Временные люфты и шаги Giveback-Трейлов
    ATR_FAILED_SETUP_SOFT_MULT: float = 1.2
    ATR_FAILED_SETUP_MID_MULT: float = 1.8
    ATR_FAILED_SETUP_DEEP_MULT: float = 2.5
    ATR_PROTECT_START_MULT: float = 1.5
    ATR_TRAIL_START_MULT: float = 2.2           # Эластичный следящий поводок остатка
    ATR_CAPTURE_START_MULT: float = 2.0

    # Прогрессивные Тейки и Защита Прибыли
    TP1_PARTIAL_ENABLED: bool = True
    TP1_PARTIAL_CLOSE_SHARE: float = 0.35       # Фиксируем на TP1 всего 35%, 65% отдаем тренду раннеров
    TP2_PROGRESSIVE_ENABLED: bool = True
    TP2_PARTIAL_CLOSE_SHARE: float = 0.50
    TP2_TRAIL_LEG_SHARE: float = 0.5
    TP2_TRAIL_MIN_BUFFER_PCT: float = 0.20
    TP2_TRAIL_GIVEBACK_SHARE: float = 0.40
    POST_TP1_TRAIL_ENABLED: bool = True
    POST_TP1_TRAIL_MIN_MFE_PCT: float = 0.60
    POST_TP1_TRAIL_GIVEBACK_SHARE: float = 0.40
    POST_TP1_LOCK_FRAC: float = 0.0             # Выключаем жесткий зажим стопа на уровне TP1
    
    MIN_NET_RR_BLENDED: float = 1.10
    MIN_NET_RR_BLENDED_TP1_SHARE: float = 0.5
    TP1_MIN_PCT: float = 0.6
    TP1_MAX_PCT: float = 1.8
    TP1_DEFAULT_PCT: float = 1.2
    MIN_POST_TP1_EXIT_PCT: float = 0.80
    MIN_PROTECTIVE_NET_USDT: float = 0.15        # Снизили долларовую окупаемость под микро-сайзы
    MIN_PROTECTIVE_EXIT_PCT: float = 0.40
    MIN_POST_TP1_EXIT_PCT_VALUE: float = 0.80

    # Фильтры Карантина Истории Успеваемости Символов (Symbol Performance Guard)
    SYMBOL_PERF_MIN_HISTORY: int = 10
    SYMBOL_PERF_BLOCK_MIN_HISTORY: int = 12
    SYMBOL_PERF_BLOCK_MAX_WINRATE: float = 48.0
    SYMBOL_PERF_REDUCE_MAX_WINRATE: float = 48.0
    SYMBOL_PERF_COOLDOWN_STREAK: int = 3
    SYMBOL_PERF_COOLDOWN_STOPS: int = 3
    SYMBOL_PERF_COOLDOWN_FAILED_SETUPS: int = 2
    SYMBOL_PERF_SMALL_HISTORY_STOP_MULTIPLIER: float = 1.0
    SYMBOL_PERF_WEAK_MULTIPLIER: float = 0.70
    SYMBOL_PERF_GIVEBACK_MULTIPLIER: float = 0.60
    SYMBOL_PERF_WEAK_PNL_TOLERANCE_USDT: float = 2.0
    SYMBOL_PERF_GIVEBACK_TRIGGER: int = 3
    SYMBOL_PERF_SEVERE_LOSS_PCT: float = 5.0
    SYMBOL_PERF_SEVERE_MIN_HISTORY: int = 5
    SYMBOL_PERF_WINDOW_HOURS: float = 168.0
    SYMBOL_PERF_SUMMARY_WINDOW_HOURS: float = 720.0
    SYMBOL_PERF_PROBE_MULTIPLIER: float = 0.40

    # ==========================================================================
    # 📊 ORDER BOOK WEB_SOCKET INSPECTION (OBI / CVD FLOW)
    # ==========================================================================
    ENABLE_ORDERBOOK_ENGINE: bool = False
    OB_MARKET_TYPE: str = "spot"
    OB_EXCHANGE: str = ""
    OB_OKX_WS_URL: str = ""
    OB_OKX_BOOK_CHANNEL: str = "books"
    OB_BOOK_LEVELS: int = 150
    OB_OKX_SHADOW_ENABLED: bool = True
    OB_COMPARE_SAMPLE_SEC: float = 30.0
    OB_COMPARE_SAMPLES: int = 2880
    OB_WS_URL: str = ""
    OB_DEPTH_LEVELS: int = 10
    OB_MAX_SPREAD_PCT: float = 0.08
    OB_POSITION_MAX_SPREAD_PCT: float = 0.12
    
    # Лимиты OBI гейтов для ТРЕНДОВЫХ (Position) позиций
    OB_POSITION_OBI_CONFIRM: float = 0.05
    OB_POSITION_WALL_CONFIRM_SHARE: float = 0.20
    OB_POSITION_OBI_HARD_VETO: float = 0.80
    OB_POSITION_WALL_RESCUE_MAX_ADVERSE_OBI: float = 0.60
    OB_POSITION_CVD_ENTRY_BLOCK_RATIO: float = 0.70
    OB_POSITION_CVD_MIN_TRADES: int = 25
    OB_POSITION_CVD_THIN_RATIO: float = 1.0
    OB_POSITION_CVD_THIN_MIN_TRADES: int = 15
    
    # Лимиты OBI гейтов для СКАЛЬПИНГА и MR-коридоров
    OB_OBI_CONFIRM: float = 0.15
    OB_WALL_CONFIRM_SHARE: float = 0.30
    OB_OBI_HARD_VETO: float = 0.45
    OB_WALL_RESCUE_MAX_ADVERSE_OBI: float = 0.35
    OB_CVD_THIN_RATIO: float = 0.9
    OB_CVD_THIN_MIN_TRADES: int = 8
    OB_DATA_MAX_AGE_SEC: float = 15.0
    OB_CVD_WINDOW_SEC: int = 60
    OB_CVD_EXIT_RATIO: float = 0.8
    OB_CVD_MIN_TRADES: int = 25
    OB_CVD_ENTRY_BLOCK_RATIO: float = 0.35
    OB_GATE_ENTRIES: bool = True
    OB_ACCELERATE_EXITS: bool = True

    # Сопряжение осей Уверенности (Smart Leverage Контур)
    ENABLE_FUTURES_EXECUTION: bool = False
    ENABLE_SMART_LEVERAGE: bool = False
    MAX_LEVERAGE: float = 3.0
    PORTFOLIO_RISK_BUDGET_PCT: float = 6.0
    GRADE_AXIS_VALIDATED: bool = False
    CONFIDENCE_SYMMETRIC_BLEND: bool = True

    # ==========================================================================
    # 📉 PARALLEL INACTIVE NETWORKS (GRID & FUNDING ARBITRAGE - COLD STORAGE)
    # ==========================================================================
    GRID_ENABLED: bool = False
    GRID_KILL_SWITCH_ENABLED: bool = True
    GRID_NEUTRAL_FLIP_NEEDS_BREAKOUT: bool = True
    GRID_OPEN_NEEDS_RANGE: bool = True
    GRID_STATE_PATH: str = "storage/grid/grid_state.json"
    GRID_SYMBOLS: str = "BTC/USDT,ETH/USDT,SOL/USDT"
    GRID_TIMEFRAME: str = "1h"
    GRID_LINES: int = 6
    GRID_MIN_LEVEL_USDT: float = 5.0
    GRID_VOL_MULTIPLIER: float = 1.2
    GRID_STEP_MULTIPLIER: float = 1.1
    GRID_VOL_COEFF: float = 0.5
    GRID_FLIP_MIN_ATR_DIST: float = 1.0
    GRID_ANTI_MARTINGALE_ENABLED: bool = True
    GRID_SHORT_FILL_RSI_MAX: float = 80.0
    GRID_LONG_FILL_RSI_MIN: float = 20.0
    GRID_OPEN_MIN_EDGE_SPREAD_MULT: float = 1.0
    GRID_FEES_IN_REALIZED: bool = True
    GRID_ATR_PERIOD: int = 14
    GRID_EMA_PERIOD: int = 200
    GRID_RSI_PERIOD: int = 14
    GRID_RSI_HIGH: float = 70.0
    GRID_RSI_LOW: float = 30.0
    GRID_REGIME_EMA_BAND_PCT: float = 0.80
    GRID_TP_PCT: float = 0.6
    GRID_SL_ATR_MULT: float = 1.6
    GRID_MAX_SAFETY_ORDERS: int = 3
    GRID_MAX_USED_MARGIN_PCT: float = 10.0
    GRID_LEVERAGE: float = 1.0
    GRID_FEE_ROUND_PCT: float = 0.06
    GRID_DIRECTIONAL_ENABLED: bool = False
    GRID_HTF_EXTREME_VETO: bool = True
    GRID_HTF_RSI_OVERHEAT: float = 72.0
    GRID_HTF_RSI_OVERSOLD: float = 28.0
    GRID_REARM: bool = True
    GRID_TICK_INTERVAL_SEC: float = 20.0
    GRID_ADAPT_ENABLED: bool = True
    GRID_RESPACING_ENABLED: bool = True
    GRID_RESPACE_ENABLED: bool = True
    GRID_FLIP_ON_REGIME: bool = True
    GRID_FLIP_CONFIRM_TICKS: int = 6
    GRID_FLIP_COOLDOWN_SEC: int = 7200
    GRID_FREEZE_ON_NEUTRAL: bool = False
    GRID_FREEZE_ON_BREAKOUT: bool = True
    GRID_BREAKOUT_ATR_DIST: float = 1.6
    GRID_RANGE_ATR_DIST: float = 0.8
    GRID_RANGE_CONFIRM_TICKS: int = 3
    GRID_MARGIN_MODE: str = "isolated"
    GRID_MARGIN_ISOLATED_MAX_LEV: float = 1.0

    ENABLE_FUNDING_ARB: bool = False
    FUNDING_OBSERVE_ENABLED: bool = True
    FUNDING_OBSERVE_VENUES: str = "htx,okx"
    FUNDING_OBSERVE_INTERVAL_MIN: float = 60.0
    FUNDING_ARB_SYMBOLS: str = "BTC/USDT,ETH/USDT,SOL/USDT,XRP/USDT,DOGE/USDT,SUI/USDT"
    FUNDING_ARB_MIN_RATE_PCT: float = 0.015
    FUNDING_ARB_MAX_BASIS_PCT: float = 0.50
    FUNDING_ARB_MIN_NET_YIELD_PCT: float = 0.005
    FUNDING_ARB_HONEST_ACCRUAL: bool = True
    FUNDING_ARB_DEFAULT_NOTIONAL_USDT: float = 100.0
    FUNDING_ARB_MIN_NOTIONAL_USDT: float = 5.0
    FUNDING_ARB_MAX_NOTIONAL_USDT: float = 500.0
    FUNDING_ARB_MAX_OPEN_HEDGES: int = 2
    FUNDING_ARB_FREE_BUFFER_PCT: float = 95.0
    FUNDING_ARB_CLOSE_RATE_PCT: float = 0.005
    FUNDING_ARB_MAX_HOLD_HOURS: int = 240
    FUNDING_ARB_MIN_HOLD_PERIODS: int = 3
    FUNDING_ARB_SCAN_INTERVAL_HOURS: int = 8
    FUNDING_ARB_AUTO_OPEN_PAPER: bool = True
    FUNDING_ARB_ASSUMED_HOLD_PERIODS_OVERRIDE: int = 0
    FUNDING_LEVERAGE: int = 2
    PAYMENT_PENDING_EXPIRE_HOURS: int = 48
    KRAKEN_ENABLED: bool = True
    KRAKEN_TIMEOUT_MS: int = 20000
    KRAKEN_PROXY_URL: str = ""
    KRAKEN_QUOTE: str = "USD"
    KRAKEN_FUNDING_INTERVAL_HOURS: float = 1.0
    HTX_FUNDING_INTERVAL_HOURS: float = 8.0
    KRAKEN_TAKER_FEE: float = 0.0005
    KRAKEN_COMPARE_CACHE_SEC: int = 60
    KRAKEN_SPREAD_LOG_ENABLED: bool = True
    KRAKEN_SPREAD_LOG_INTERVAL_SEC: int = 3600
    KRAKEN_SPREAD_LOG_PATH: str = "storage/ml/venues_funding_spread.jsonl"
    KRAKEN_SPREAD_HISTORY_MAX_LINES: int = 20000
    CROSS_FARB_ENABLED: bool = False
    CROSS_FARB_CARRY_FLOOR_ENABLED: bool = True
    CROSS_FARB_SYMBOLS: str = "AVAX/USDT,XRP/USDT,TRX/USDT,SOL/USDT"
    CROSS_FARB_NOTIONAL_USDT: float = 100.0
    CROSS_FARB_NOTIONAL_PCT: float = 0.105
    CROSS_FARB_MIN_NOTIONAL_USDT: float = 5.0
    CROSS_FARB_MAX_NOTIONAL_USDT: float = 500.0
    CROSS_FARB_MAX_POSITIONS: int = 2
    CROSS_FARB_MIN_ANN_PCT: float = 12.0
    CROSS_FARB_MIN_STABILITY_PCT: float = 80.0
    CROSS_FARB_CONSERVATIVE_QUANTILE: float = 0.25
    CROSS_FARB_MIN_CONSERVATIVE_ANN_PCT: float = 8.0
    CROSS_FARB_PAYBACK_MARGIN: float = 2.0
    CROSS_FARB_LOOKBACK_DAYS: int = 1
    CROSS_FARB_CLOSE_ANN_PCT: float = 3.0
    CROSS_FARB_EXIT_CONFIRM_STEPS: int = 3
    CROSS_FARB_REENTRY_COOLDOWN_HOURS: float = 6.0
    CROSS_FARB_MAX_HOLD_DAYS: float = 14.0
    CROSS_FARB_STATE_PATH: str = "storage/ml/cross_funding_arb_state.json"

    # Контур Аффилиации и подписок (Billing Infrastructure)
    HTX_AFFILIATE_LINK: str = ""
    AFFILIATE_FREE_VIP_DAYS: int = 30
    VIP_INVITE_LINK: str = ""
    HTX_AFFILIATE_VERIFY_ENABLED: bool = False
    HTX_AFFILIATE_API_KEY: str = ""
    HTX_AFFILIATE_API_SECRET: str = ""
    HTX_AFFILIATE_API_HOST: str = "api.huobi.pro"
    HTX_AFFILIATE_INVITEES_PATH: str = "/v2/affiliate/invitees"
    OKX_AFFILIATE_LINK: str = ""
    OKX_AFFILIATE_CODE: str = ""
    OKX_AFFILIATE_VERIFY_ENABLED: bool = False
    AFFILIATE_TRIAL_ONE_PER_USER: bool = True

    # ==========================================================================
    # ⚙️ ДИНАМИЧЕСКИЕ СВОЙСТВА И МЕТОДЫ КЛАССА SETTINGS (DYNAMIC EVALUATION)
    # ==========================================================================
    def stars_price_for_plan(self, plan_code: str) -> int:
        return {
            "vip_30": self.VIP_STARS_PRICE_30,
            "vip_90": self.VIP_STARS_PRICE_90,
        }.get(plan_code, 0)

    @property
    def cors_origins(self) -> List[str]:
        defaults = ["http://localhost:3000", "http://127.0.0.1:3000"]
        extra = [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]
        return list(dict.fromkeys(defaults + extra))

    def max_order_notional(self, equity_usdt: float | None = None, leverage: float | None = None) -> float:
        pct = float(getattr(self, "LIVE_MAX_ORDER_NOTIONAL_PCT", 0.0) or 0.0)
        absolute = float(getattr(self, "LIVE_MAX_ORDER_NOTIONAL_USDT", 0.0) or 0.0)
        if pct <= 0 or not equity_usdt or float(equity_usdt) <= 0:
            return absolute
        return float(equity_usdt) * max(1.0, float(leverage or 1.0)) * pct / 100.0

    def symbols_for(self, exchange: str | None) -> List[str]:
        raw = self.HTX_SYMBOLS
        if str(exchange or "").strip().lower() == "okx" and str(self.OKX_SYMBOLS or "").strip():
            raw = self.OKX_SYMBOLS
        return [s.strip() for s in raw.split(",") if s.strip()]

    @property
    def universe_source(self) -> str:
        if self.active_exchange == "okx" and str(self.OKX_SYMBOLS or "").strip():
            return "OKX_SYMBOLS"
        return "HTX_SYMBOLS"

    @property
    def symbols(self) -> List[str]:
        return self.symbols_for(self.active_exchange)

    @property
    def funding_arb_symbols(self) -> List[str]:
        return [s.strip() for s in self.FUNDING_ARB_SYMBOLS.split(",") if s.strip()]

    @property
    def grid_symbols(self) -> List[str]:
        return [s.strip().upper() for s in self.GRID_SYMBOLS.split(",") if s.strip()]

    @property
    def execution_market_type(self) -> str:
        return "swap" if (self.ENABLE_LIVE_ORDERS or self.ENABLE_FUTURES_EXECUTION) else self.MARKET_TYPE

    @property
    def active_exchange(self) -> str:
        value = str(getattr(self, "ACTIVE_EXCHANGE", "htx") or "").strip().lower()
        return value if value in ("htx", "okx") else "htx"

    @property
    def execution_leverage(self) -> int:
        if self.execution_market_type == "swap":
            return max(int(self.FUTURES_LEVERAGE), 1)
        return 1

    @property
    def grid_effective_margin_mode(self) -> str:
        lev = float(getattr(self, "GRID_LEVERAGE", 1.0) or 1.0)
        if lev > float(getattr(self, "GRID_MARGIN_ISOLATED_MAX_LEV", 1.0)):
            return "cross"
        return str(getattr(self, "GRID_MARGIN_MODE", "isolated")).lower()

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
            if url.startswith("postgres://"):
                url = "postgresql://" + url[len("postgres://"):]
            return url
        return (
            f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    # Верификатор жестких блокировок Среды Продакшена (Production Blockers)
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
            if not str(getattr(self, "TELEGRAM_WEBHOOK_SECRET", "") or ""):
                blockers.append("TELEGRAM_WEBHOOK_SECRET is not configured: bot menu accepts unverified updates")
            if self.active_exchange == "okx":
                if not self.OKX_API_KEY or not self.OKX_API_SECRET or not self.OKX_API_PASSPHRASE:
                    blockers.append("OKX API credentials are not configured")
            else:
                if not self.HTX_API_KEY or not self.HTX_API_SECRET:
                    blockers.append("HTX API credentials are not configured")
        
        if self.ENABLE_LIVE_ORDERS and self.TRADING_MODE not in ["live", "live_limited"]:
            blockers.append("ENABLE_LIVE_ORDERS requires TRADING_MODE=live or live_limited")
        if self.ENABLE_LIVE_ORDERS and self.ROBOT_MODE == "paper":
            blockers.append("ENABLE_LIVE_ORDERS cannot run with ROBOT_MODE=paper")
        if self.ENABLE_LIVE_ORDERS and not self.TELEGRAM_BOT_TOKEN:
            blockers.append("live orders require Telegram owner alerts")
            
        if self.ENABLE_LIVE_ORDERS:
            lev = max(1.0, min(float(self.FUTURES_LEVERAGE or 1), float(self.LIVE_MAX_LEVERAGE)))
            typical_notional = float(self.RISK_EQUITY_USDT) * float(self.MAX_POSITION_MARGIN_PCT) * lev
            cap = float(self.max_order_notional(self.RISK_EQUITY_USDT, lev) or 0.0)
            if 0 < cap < typical_notional:
                blockers.append(
                    f"order notional cap {cap:.0f} USDT is below the typical position notional "
                    f"~{typical_notional:.0f} USDT (RISK_EQUITY_USDT x MAX_POSITION_MARGIN_PCT x "
                    f"leverage {lev:g}): every live order would be rejected. Raise "
                    f"LIVE_MAX_ORDER_NOTIONAL_PCT (or clear the absolute "
                    f"LIVE_MAX_ORDER_NOTIONAL_USDT), or lower position sizing"
                )
            if float(self.LIVE_MAX_LEVERAGE) > 1.0 and not self.ENABLE_FUTURES:
                blockers.append("LIVE_MAX_LEVERAGE > 1 requires ENABLE_FUTURES=true")
                
        if not self.GRADE_AXIS_VALIDATED:
            grade_sizing = []
            if float(self.DYNAMIC_MARGIN_B_CAP_PCT_OF_FREE) < 1.0:
                grade_sizing.append(f"DYNAMIC_MARGIN_B_CAP_PCT_OF_FREE={self.DYNAMIC_MARGIN_B_CAP_PCT_OF_FREE}")
            if grade_sizing:
                blockers.append(
                    "grade-based sizing is enabled (" + ", ".join(grade_sizing) + ") "
                    "while the grade axis is measured antipredictive: confidence "
                    "separates stopped from survived at AUC 0.66 [0.549; 0.772] and "
                    "grade A expectancy is -0.4257R [-0.75; -0.07] against B at zero. "
                    "These axes give A more than B, so enabling them bets harder on "
                    "the losing bucket. Fix the composite score, re-measure, then set "
                    "GRADE_AXIS_VALIDATED=true deliberately"
                )
                
        # ФИКС: Передаем дефолтное значение "off" в getattr, чтобы отсутствие любой переменной НЕ рушило uvicorn
        for key, allowed in _MODE_CHOICES.items():
            value = str(getattr(self, key, "off") or "").lower().strip()
            if value and value not in allowed:
                blockers.append(
                    f"{key}={value!r} is not a valid mode; expected one of {sorted(allowed)}."
                )
                
        return blockers

settings = Settings()