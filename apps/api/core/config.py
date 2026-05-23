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
    JWT_SECRET: str
    NEXT_PUBLIC_API_URL: str

    OWNER_EMAIL: str
    OWNER_PASSWORD: str

    # =========================
    # DATABASE
    # =========================
    POSTGRES_DB: str
    POSTGRES_USER: str
    POSTGRES_PASSWORD: str
    POSTGRES_HOST: str
    POSTGRES_PORT: int

    REDIS_URL: str

    # =========================
    # HTX API
    # =========================
    HTX_API_KEY: str
    HTX_API_SECRET: str
    HTX_MARKET_TYPE: str = "spot"
    HTX_SYMBOLS: str
    ALLOW_MARKET_MOCK: bool = False

    # =========================
    # TELEGRAM
    # =========================
    TELEGRAM_BOT_TOKEN: str
    TELEGRAM_OWNER_CHAT_ID: int
    TELEGRAM_SIGNALS_CHAT_ID: int
    TELEGRAM_FREE_SIGNALS_CHAT_ID: int
    TELEGRAM_VIP_SIGNALS_CHAT_ID: int

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
    # spot | margin | futures
    EXECUTION_MARKET: str = "spot"
    SHORT_ALERT_THROTTLE_MINUTES: int = 60

    # =========================
    # RISK MANAGEMENT
    # =========================
    MAX_DAILY_LOSS_PCT: float = 3
    MAX_DRAWDOWN_PCT: float = 15
    MAX_OPEN_POSITIONS: int = 3
    RISK_PER_TRADE_PCT: float = 0.5

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