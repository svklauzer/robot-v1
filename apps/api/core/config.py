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
    # Переопределение хоста HTX API. Для клиентов в AWS HTX рекомендует
    # api-aws.huobi.pro (ниже задержка, сервера HTX в AWS Tokyo). Пусто = дефолт ccxt.
    HTX_API_HOSTNAME: str = ""
    HTX_MARKET_TYPE: str = "spot"
    # Универсум подобран по РЕАЛЬНОЙ ликвидности HTX spot (top-of-book спред):
    # BTC ~0.00002%, ETH ~0.0006%, AVAX ~0.008%, XRP ~0.017%, SOL ~0.036%,
    # TRX ~0.089% (HTX-native, глубокий). DOT убран — спред 0.2–1%+ (хронический
    # неликвид). ADA убран по ЖИВОЙ телеметрии: спред 0.14–0.61% (а не ~0.017%,
    # как считалось) — постоянно бьёт depth-гейт (0.12%), заваливал ленту blocked,
    # а когда проскакивал — слив на спреде (#95 -5.05, #85 -3.85). LINK/LTC/DOGE/BNB
    # НЕ добавлены — их spot спред (0.12–1%) тоже хуже порога.
    HTX_SYMBOLS: str = "BTC/USDT,ETH/USDT,SOL/USDT,XRP/USDT,AVAX/USDT,TRX/USDT"
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
    # (#2 консолидация) failed_setup_exit в ТРЕНДЕ выключен — он рубил в шумовой
    # полосе до структурного smart-стопа, часто на вике. В тренде бэкстоп =
    # smart-stop + breakeven_lock + ride-трейл. True вернёт прежнее поведение.
    FAILED_SETUP_EXIT_TREND_ENABLED: bool = False

    # =========================
    # БЕЗУБЫТОК-ЗАМОК (#1/#2)
    # Корневая проблема телеметрии: positive_then_negative 50-64%. Сделки
    # доходили до +1% MFE и закрывались в минус через failed_setup_exit.
    # Как только сделка показала значимый MFE (ARM), запрещаем закрываться
    # глубоко в минус: фиксируем у безубытка (+комиссии), не отдаём ход.
    # =========================
    BREAKEVEN_LOCK_ENABLED: bool = True
    # MFE (%), после которого вооружается безубыток-замок.
    BREAKEVEN_LOCK_ARM_PCT: float = 0.80
    # Уровень результата (%), на котором фиксируемся после вооружения:
    # как только текущий профит откатил к этому полу — выходим тут, а не
    # ждём failed_setup_exit на -0.6/-0.9%.
    BREAKEVEN_LOCK_FLOOR_PCT: float = 0.10
    # (#wick) Вик-фильтр для мягких выходов. В тренде откат вверх — обычно тонкий
    # вик-пулбэк, а не разворот: выходить по нему = выбиться перед продолжением.
    # Мягкие выходы (breakeven_lock, failed_setup soft/mid) срабатывают ТОЛЬКО
    # если поток подтвердил разворот (flow_against по CVD) ИЛИ цена ушла за
    # hard_floor. Иначе держим — бэкстопом остаётся smart-stop и deep-порог.
    EXIT_REQUIRE_FLOW_CONFIRM: bool = True
    # Глубина минуса, при которой breakeven_lock выходит БЕЗ подтверждения потоком
    # (реальный неблагоприятный ход, а не вик).
    BREAKEVEN_LOCK_HARD_FLOOR_PCT: float = -0.35
    # (#churn) Re-entry cooldown в авто-цикле: не открывать ту же сторону символа
    # сразу после закрытия (особенно стопа). Машинка ReEntryCooldownGuard уже
    # есть, флаг включает её проверку в robot_loop.
    REENTRY_COOLDOWN_ENABLED: bool = True

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

    # =========================
    # ML-СЛОЙ (мета-лейблер + control plane)
    # ML_MODE — единственный тумблер. Дефолт "off" → поведение системы как сейчас,
    # запуск в live НЕ затрагивается (ML ортогонален ENABLE_LIVE_ORDERS, fail-open).
    #   off | shadow | advisory | full_auto
    # =========================
    ML_MODE: str = "off"
    ML_LABEL_KIND: str = "is_win"          # is_win | hit_tp2
    ML_MIN_TRAIN_SAMPLES: int = 150        # меньше — модель не обучается (честно)
    ML_MIN_SCORE_TO_TRADE: float = 0.45    # full_auto/advisory: ниже — skip/block
    ML_SIZE_MULT_MIN: float = 0.7          # full_auto: множитель размера, кэп снизу
    ML_SIZE_MULT_MAX: float = 1.25         # full_auto: множитель размера, кэп сверху
    # Ежесуточный авто-retrain (держит модель свежей; при данных < min — honest skip).
    ML_AUTO_RETRAIN: bool = True
    ML_RETRAIN_INTERVAL_SEC: int = 86400   # раз в сутки
    # ML-алерт в Telegram опционален — off, чтобы НЕ дублировать существующий
    # 2ч-дайджест. Включишь — придёт короткий итог retrain в owner-канал.
    ML_TELEGRAM_ALERTS: bool = False
    # OHLC-research: число walk-forward фолдов и косты (в долях k_atr-хода).
    RESEARCH_WF_FOLDS: int = 5
    RESEARCH_COST_ATR: float = 0.25

    # Paper/live-shadow validation gates before limited live scaling.
    VALIDATION_MIN_CLOSED_SIGNALS: int = 50
    VALIDATION_FAILED_SETUP_MAX_PCT: float = 35.0
    VALIDATION_POSITIVE_THEN_NEGATIVE_MAX_PCT: float = 25.0

    # =========================
    # RANGE STRATEGY — mean-reversion скальп для боковика
    # =========================
    # Включается, когда трендовый путь простаивает (4h не в тренде). На споте —
    # только лонг от нижней границы коридора. Включить после обкатки на paper.
    ENABLE_RANGE_STRATEGY: bool = False
    RANGE_MIN_WIDTH_PCT: float = 2.5        # мин. ширина коридора (нужно куда ехать после комиссий)
    RANGE_SUPPORT_ZONE: float = 0.30        # входим, если цена в нижних 30% диапазона (0=поддержка)
    RANGE_ENTRY_RSI_MIN: float = 25.0       # зона разворота у поддержки
    RANGE_ENTRY_RSI_MAX: float = 52.0
    RANGE_MIN_TP1_NET_PCT: float = 0.8      # мин. чистый ход до TP1 после round-trip комиссий (%)
    RANGE_TP2_RESISTANCE_BUFFER: float = 0.10  # TP2 = на 10% ниже верхней границы
    RANGE_STOP_ATR_MULT: float = 0.5        # стоп = поддержка − 0.5·ATR
    RANGE_MIN_SETUP_SCORE: float = 60.0
    # Range-шорт от верхней границы коридора (требует futures-исполнения).
    RANGE_ALLOW_SHORT: bool = True

    # --- CRT (Candle Range Theory) — 3-свечной вход A→M→D ---
    # C1(4h)=диапазон CRH/CRL, C2=свип+закрытие обратно внутрь, C3=вход на LTF
    # по MSS/FVG в premium/discount. SL за хвост C2, TP1=противоположная
    # ликвидность, TP2=R:R. Приоритетнее грубого range. Под флагом, OFF.
    ENABLE_CRT_STRATEGY: bool = False
    CRT_HTF_TF: str = "4h"                 # старший ТФ для C1/C2
    CRT_LTF_TF: str = "5m"                 # младший ТФ для входа/MSS/FVG
    CRT_MIN_RANGE_PCT: float = 1.5         # мин. ширина C1-диапазона (%)
    CRT_LTF_CONFIRM: str = "either"        # "either" | "both" | "off" (MSS/FVG)
    CRT_REQUIRE_PREMIUM_DISCOUNT: bool = True
    CRT_STOP_BUFFER_PCT: float = 0.05      # буфер за хвостом C2 (доля диапазона)
    CRT_TP2_RR: float = 2.0                # R:R для TP2 (1:2)
    CRT_MIN_TP1_NET_PCT: float = 0.5       # мин. чистый ход до TP1 после комиссий
    # (#5) Минимальный RR для TP1: если ликвидность (CRL/CRH) ближе 1R, TP1
    # тянется к 1R. Иначе gross RR_tp1 ~1.06 после комиссий проседает до ~0.43
    # и downstream блокирует CRT каждый цикл (blocked_low_net_rr_tp1).
    CRT_MIN_RR_TP1: float = 1.0
    CRT_ALLOW_LONG: bool = True
    CRT_ALLOW_SHORT: bool = True
    CRT_MIN_SETUP_SCORE: float = 55.0

    # --- Scalp risk profile (trade_mode="scalp" / regime="range") ---
    # Скальп — маленькая позиция, мелкое движение, мелкие абсолютные суммы.
    # Глобальные пороги риска заточены под крупные трендовые сделки и душат
    # скальп. Эти параметры применяются ТОЛЬКО к range/scalp-входам; тренд
    # продолжает жить на строгих глобальных порогах.
    SCALP_MAX_POSITION_MARGIN_PCT: float = 0.10        # доля эквити на одну скальп-позицию
    SCALP_MIN_NET_PNL_TP1_USDT: float = 0.5            # абсолютный минимум net TP1 (USDT)
    SCALP_MIN_NET_PNL_TP2_USDT: float = 1.0            # абсолютный минимум net TP2 (USDT)
    SCALP_MIN_NET_RR_TP2: float = 1.0                  # min RR до TP2 в плане (тренд: 1.2)
    SCALP_ANTI_DRAIN_MIN_EDGE_AFTER_COSTS_USDT: float = 0.0  # абсолютный edge-флор anti-drain
    SCALP_ANTI_DRAIN_MAX_POSITION_MARGIN_PCT: float = 20.0   # маржевый лимит anti-drain для скальпа
    SCALP_ANTI_DRAIN_MIN_NET_RR_TP1: float = 0.40
    SCALP_ANTI_DRAIN_MIN_NET_RR_TP2: float = 0.85
    # Scalp exit: безубыток-замок (трейл от MFE). Тренд-пороги capture (~0.95%)
    # и protective (1.2% / 1.5 USDT) под маленький скальп не вооружаются — и
    # зелёная сделка переворачивается в убыток (кейс LINK: +0.72% → −1.18%).
    # Замок трейлит от пика и фиксирует остаток в плюсе.
    SCALP_BREAKEVEN_ENABLED: bool = True
    SCALP_BREAKEVEN_ARM_PCT: float = 0.5         # MFE %, с которого включается замок
    SCALP_BREAKEVEN_GIVEBACK_SHARE: float = 0.6  # выходим, отдав эту долю пика MFE
    # Скальп тайм-стоп (профиль ведения SCALP): сделка должна разрешиться быстро.
    # Если за N минут скальп не вооружился (mfe < arm) — закрываем по текущей цене,
    # чтобы «мёртвая» сделка не дрейфовала в свинг-убыток и освободила слот.
    SCALP_TIME_STOP_ENABLED: bool = True
    SCALP_TIME_STOP_MIN: float = 45.0            # минут до тайм-стопа невооружённого скальпа

    # --- Post-loss cooldown (только range-скальп) ---
    # После убыточного закрытия по паре символ+сторона не лезем повторно N минут
    # — режет churn (DOT шортился 6× в аптренд, пока guard не заблокировал).
    # CRT/тренд НЕ трогаем (у них своя селективность).
    POST_LOSS_COOLDOWN_ENABLED: bool = True
    POST_LOSS_COOLDOWN_MIN: float = 25.0

    # --- Split cadence: медленный SCAN (поиск входов) + быстрый MANAGE (выходы) ---
    # Сканирование сетапов на 4h-биасе не нужно чаще раза в минуту, а ведение
    # открытых позиций (скальп-замок, трейлы) должно реагировать быстро. HTX REST
    # допускает до 800 req/s на IP — мы далеко от лимита, узкое место было своё.
    SCAN_INTERVAL_SEC: int = 60
    MANAGE_INTERVAL_SEC: int = 10

    # Периодический дайджест состояния в Telegram (owner). 7200с = каждые 2 часа.
    ENABLE_DIGEST: bool = True
    DIGEST_INTERVAL_SEC: int = 7200

    # --- Order-book / depth engine (HTX WebSocket) ---
    # Не HFT: используем устойчивые дисбалансы стакана как ПОДТВЕРЖДЕНИЕ входов
    # (spread-гейт + OBI + стенки) и ускоритель скальп-выхода (CVD). За флагом;
    # если WS молчит — анализатор уходит в pass-through, торговля как обычно.
    ENABLE_ORDERBOOK_ENGINE: bool = False
    OB_WS_URL: str = "wss://api-aws.huobi.pro/ws"
    OB_DEPTH_LEVELS: int = 10
    OB_MAX_SPREAD_PCT: float = 0.08       # СКАЛЬП/range: шире — скип (слиппедж съест скальп)
    # POSITION (trend/crt) едет 1.5–3%: спред — не главный фильтр, но 0.20 был
    # слишком вольно — DOT #80 при спреде 0.197% (неликвид) прошёл впритык и сразу
    # в стоп. Затянуто 0.20→0.12: широкий спред = тонкий стакан/слиппедж.
    OB_POSITION_MAX_SPREAD_PCT: float = 0.12
    OB_OBI_CONFIRM: float = 0.15          # нужный перекос стакана в сторону входа
    OB_WALL_CONFIRM_SHARE: float = 0.30   # доля уровня в топ-N = «стенка»
    # Жёсткое OBI-вето: при подавляющем перекосе стакана ПРОТИВ входа блокируем
    # независимо от встречной стенки. Раньше long при OBI -0.97 проходил, т.к.
    # bid_wall_share бил порог стенки (#94 ETH → -2.95, #89 XRP → -4.67). Порог
    # высокий (только вопиющие случаи), чтобы не резать пограничные ±0.5 входы.
    # 0 → выкл.
    OB_OBI_HARD_VETO: float = 0.75
    OB_DATA_MAX_AGE_SEC: float = 15.0     # старше — данные не свежие, не гейтим
    OB_CVD_WINDOW_SEC: int = 60           # окно ленты сделок для CVD
    OB_CVD_EXIT_RATIO: float = 0.8        # поток против позиции на эту долю → ускоряем выход
    OB_CVD_MIN_TRADES: int = 25           # меньше сделок в окне → CVD это шум, не сигнал
    # CVD НА ВХОДЕ: не входим против агрессивного исполненного потока. Раньше CVD
    # работал только на выходе — четвёртый (сильнейший) сигнал стакана на входе
    # простаивал. Блокируем шорт при cvd_ratio ≥ +ratio (доминируют покупки),
    # лонг при cvd_ratio ≤ −ratio (доминируют продажи), но только при достаточной
    # выборке (≥ OB_CVD_MIN_TRADES) — иначе CVD это шум.
    OB_CVD_ENTRY_BLOCK_RATIO: float = 0.6
    OB_GATE_ENTRIES: bool = True          # применять ли depth-гейт ко входам
    OB_ACCELERATE_EXITS: bool = True      # применять ли CVD к выходам

    # =========================
    # FUTURES EXECUTION & SMART LEVERAGE — Фаза 4 (каркас, OFF по умолчанию)
    # =========================
    # Перевод основной стратегии на futures (открывает шорты + плечо).
    # Включать ТОЛЬКО на доказанном edge (net PnL > 0 на paper).
    ENABLE_FUTURES_EXECUTION: bool = False
    # Динамическое плечо по conviction (грейд × сила тренда × волатильность).
    # OFF → плечо всегда 1.0 (без эффекта).
    ENABLE_SMART_LEVERAGE: bool = False
    MAX_LEVERAGE: float = 3.0               # жёсткий потолок плеча (догма)
    # Суммарный риск по ВСЕМ открытым сделкам, % эквити (портфельный бюджет).
    PORTFOLIO_RISK_BUDGET_PCT: float = 6.0
    # Множители conviction по грейду (вклад в плечо).
    LEVERAGE_GRADE_A_PLUS: float = 1.0
    LEVERAGE_GRADE_A: float = 0.7
    LEVERAGE_GRADE_B: float = 0.4

    # =========================
    # SYMBOL PERFORMANCE GUARD
    # =========================
    SYMBOL_PERF_LOOKBACK: int = 10
    SYMBOL_PERF_MIN_HISTORY: int = 3
    SYMBOL_PERF_BLOCK_MIN_HISTORY: int = 5
    SYMBOL_PERF_BLOCK_MAX_WINRATE: float = 48.0
    # Снижено 55→48 (#3): при RR>1.5 символ с винрейтом 48-55% прибылен,
    # карать его уменьшением размера незачем — это резало победителей.
    SYMBOL_PERF_REDUCE_MAX_WINRATE: float = 48.0
    SYMBOL_PERF_COOLDOWN_STREAK: int = 3
    SYMBOL_PERF_COOLDOWN_STOPS: int = 3
    SYMBOL_PERF_COOLDOWN_FAILED_SETUPS: int = 2
    SYMBOL_PERF_SMALL_HISTORY_STOP_MULTIPLIER: float = 0.65
    # Повышено 0.45→0.70 (#3): перевёрнутый риск (лоссы в полный размер,
    # профиты в 0.45x) математически гарантировал слив. Множитель мягче.
    SYMBOL_PERF_WEAK_MULTIPLIER: float = 0.70
    SYMBOL_PERF_GIVEBACK_MULTIPLIER: float = 0.60
    # Толеранс PnL у безубытка (#3): символ с net PnL в пределах ±этого
    # значения НЕ считается слабым (DOT при -0.27 USDT получал 0.45x зря).
    SYMBOL_PERF_WEAK_PNL_TOLERANCE_USDT: float = 2.0
    SYMBOL_PERF_GIVEBACK_TRIGGER: int = 3
    # «Смотрим на сейчас, не живём прошлым»: окно guard по ВРЕМЕНИ (часы).
    # Исходы старше выпадают из оценки сами → блок снимается без ручного сброса.
    SYMBOL_PERF_WINDOW_HOURS: float = 24.0
    # Probe-восстановление: заблокированный символ торгует МИКРО-размером
    # (доля риска), чтобы доказать себя на текущей реальности. 0 = жёсткий блок.
    # Повышено 0.15→0.40 (#3): при 0.15x комиссии съедали весь профит
    # probe-сделки (gross +0.39, costs 0.097 = 25%), восстановиться невозможно.
    SYMBOL_PERF_PROBE_MULTIPLIER: float = 0.40

    # =========================
    # ANTI-DRAIN ENTRY GUARD
    # Дефолты рассчитаны под spot 0.2% fee paper_trade.
    # Для live поднять MIN_NET_RR_TP1 до 0.65+
    # =========================
    ANTI_DRAIN_ENABLED: bool = True
    ANTI_DRAIN_MIN_CONFIDENCE: float = 60.0
    # (#9) Снижено 0.55→0.20: TP1 теперь стоит на ДОСТИЖИМОЙ встречной структуре
    # (точка частичной фиксации + перевод в безубыток), а НЕ основная награда —
    # награда контролируется по TP2 (economics_use_tp2=True, min_net_rr_tp2).
    # При прежних 0.55 близкий TP1 резался бы blocked_low_net_rr_tp1.
    ANTI_DRAIN_MIN_NET_RR_TP1: float = 0.20
    ANTI_DRAIN_MIN_NET_RR_TP2: float = 0.90       # реальный гейт награды — на TP2
    ANTI_DRAIN_MIN_EDGE_AFTER_COSTS_USDT: float = 1.20
    ANTI_DRAIN_MAX_POSITION_MARGIN_PCT: float = 12.0
    ANTI_DRAIN_MAX_USED_MARGIN_PCT: float = 50.0
    # POSITION (trend) профиль anti-drain: согласован с trade_plan
    # (MAX_POSITION_MARGIN_PCT=0.35) — иначе plan строит 35%, а anti-drain режет 12%.
    # weak_structure/overheated/economics-по-TP1 для тренда отключаются в robot_loop
    # (тренд растянут и перегрет by design; награда позиции — на TP2).
    ANTI_DRAIN_POSITION_MAX_MARGIN_PCT: float = 35.0
    ANTI_DRAIN_POSITION_MAX_USED_MARGIN_PCT: float = 70.0
    ANTI_DRAIN_MAX_OPEN_POSITIONS: int = 5
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
    # (#9-fix) Было 0.80→0.25 ради накопления данных. Телеметрия (17 закрытых,
    # winrate 29%, net -19.5) показала: сделки с net_rr_tp1 0.2–0.45 — это
    # перевёрнутый RR (на TP1 копейки, на стопе -4..-5), главный источник слива.
    # Единый RR_tp1-пол 0.50 для всех грейдов: меньше сделок, но без инверсии RR.
    PROD_GATE_A_PLUS_MIN_RR_TP1_PAPER: float = 0.50   # spot 0.2% paper (RR-пол)
    PROD_GATE_A_PLUS_MIN_RR_TP2: float = 1.45     # live
    PROD_GATE_A_PLUS_MIN_RR_TP2_PAPER: float = 1.15   # spot 0.2% paper

    # Grade A: setup_score >= 62, confidence >= 58
    PROD_GATE_A_MIN_SETUP: float = 65.0
    PROD_GATE_A_MIN_CONFIDENCE: float = 62.0
    PROD_GATE_A_MIN_RR_TP1: float = 0.90          # live
    PROD_GATE_A_MIN_RR_TP1_PAPER: float = 0.50    # RR-пол (был 0.22), см. A+ выше
    PROD_GATE_A_MIN_RR_TP2: float = 1.35          # live
    PROD_GATE_A_MIN_RR_TP2_PAPER: float = 1.05    # spot 0.2% paper

    # Grade B: setup_score >= 58, confidence >= 60
    PROD_GATE_B_MIN_SETUP: float = 58.0
    PROD_GATE_B_MIN_CONFIDENCE: float = 60.0
    PROD_GATE_B_MIN_RR_TP1: float = 0.85          # live
    PROD_GATE_B_MIN_RR_TP1_PAPER: float = 0.50    # RR-пол (был 0.20), см. A+ выше
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
    # (#6) Снижено 0.35→0.30: trade_plan строил позицию ровно на границе
    # anti-drain (35% маржи), а сайзинг считается от balance, тогда как блок
    # сверяется с equity (RISK_EQUITY_USDT). На границе позицию резало целиком
    # (blocked_position_margin_limit) вместо нормального открытия. 0.30 даёт
    # буфер: план всегда помещается под 35%-порог.
    MAX_POSITION_MARGIN_PCT: float = 0.30
    # (#9) Снижено 1.5→0.5: TP1 — частичная де-риск точка на близкой структуре,
    # его $-награда мала by design. Реальная награда и её гейт — на TP2.
    MIN_NET_PNL_TP1_USDT: float = 0.20
    MIN_NET_PNL_TP2_USDT: float = 3.5

    # (#7) Штраф за вход против краткосрочного перегрева: не покупаем вершину
    # (long при 1m/5m overheated) и не шортим дно (short при 1m/5m oversold).
    # Телеметрия: входы стабильно на 1m RSI 80-85 → MAE сразу после входа.
    # Тренд берём на откатах, когда 1m остыл. Штраф снижает score таких входов
    # ниже порога публикации.
    OVERHEAT_ENTRY_PENALTY_M1: float = 8.0
    OVERHEAT_ENTRY_PENALTY_M5: float = 5.0

    # (#exhaustion) ГЛАВНЫЙ фикс по структурному аудиту. Система — тренд-фолловер,
    # которая отшортила даунтренд донизу и продолжала шортить ДНО (#81/82/85/87 —
    # шорты в перепроданность у поддержки → отскок → большая часть убытка). Не
    # шортим истощённый тренд у поддержки и не лонгуем перегрев у сопротивления.
    TREND_EXHAUSTION_GUARD: bool = True
    EXHAUSTION_RSI_OVERSOLD: float = 30.0     # 4h RSI ниже → даунтренд истощён
    EXHAUSTION_RSI_OVERBOUGHT: float = 70.0   # 4h RSI выше → аптренд перегрет
    EXHAUSTION_LEVEL_DIST_PCT: float = 2.5    # «у поддержки/сопротивления» — в этом % от уровня

    # (#timing-veto) Микро-тайминг входа. Exhaustion-guard выше ловит МАКРО-истощение
    # (4h RSI у S/R). Этот — МИКРО: лосеры по аудиту шли +0.2% и разворачивались,
    # т.е. входили в вершину/дно на 5m. Блок аппрува, когда 5m истощён против сделки
    # (overheated→long / oversold→short), а 1m НЕ подтверждает продолжение. Если 1m
    # импульс подтверждает (bullish для long / bearish для short) — вход остаётся.
    ENTRY_TIMING_VETO_ENABLED: bool = True

    # (#3 reversal) Зеркало exhaustion-guard: РАЗРЕШАЕМ лонг на развороте от дна,
    # когда 4h истощён вниз у поддержки, НО младшие ТФ развернулись вверх с
    # объёмом. Самый рискованный тип входа (контр-4h) — гейты тугие. False = выкл.
    REVERSAL_LONG_ENABLED: bool = True
    REVERSAL_LONG_RSI_MAX: float = 35.0           # 4h RSI ниже → есть истощение для разворота
    REVERSAL_LONG_SUPPORT_DIST_PCT: float = 2.5   # цена в этом % от 4h/1h-поддержки

    LEVELS_ENTRY_TF: str = "5m"
    LEVELS_SIGNAL_TF: str = "15m"
    LEVELS_CONTEXT_TF: str = "1h"
    LEVELS_STOP_ATR_MULT: float = 2.8
    LEVELS_MIN_STOP_PCT: float = 0.30
    # (#8 smart-stop) «Думающий» стоп: ставим за ближайшим swing-уровнем
    # (сопротивление для шорта / поддержка для лонга), а не на голый k*ATR,
    # который садится ВНУТРЬ шума (AVAX #77: стоп 1.21% при вике 1.22% → выбило,
    # затем тот же сетап #79 с более широким стопом поехал в +). Размер при этом
    # ужимается автоматически (qty = risk_usdt / дистанция_стопа), риск в $ — тот же.
    LEVELS_STRUCT_STOP_ENABLED: bool = True
    # Буфер ЗА swing-уровнем (%), чтобы вик ровно по уровню не выбивал.
    LEVELS_STRUCT_STOP_BUFFER_PCT: float = 0.15
    # Потолок дистанции стопа (%): не даём стопу разрастись и сильно ужать размер/RR.
    LEVELS_MAX_STOP_PCT: float = 3.0
    # (#9) TP1 = достижимая встречная структура. Коридор поиска уровня и дефолт,
    # если структуры в коридоре нет. TP1 — точка частичной фиксации + перевод в
    # безубыток, НЕ основная награда (награда на TP2). Должен реально достигаться.
    TP1_MIN_PCT: float = 0.6
    TP1_MAX_PCT: float = 1.8   # < TREND_TP2_FLOOR_PCT(2.4), чтобы TP1<TP2
    TP1_DEFAULT_PCT: float = 1.2

    # =========================
    # VOLUME PROFILE → подгонка УРОВНЕЙ (исполнение, не прогноз)
    # =========================
    # Узлы объёма (HVN/LVN) из OHLCV корректируют ТОЛЬКО постановку TP/стопа,
    # направление сделки они НЕ определяют. fail-open: нет данных/ошибка/флаг off
    # → билдеры уровней работают ровно как раньше. ML/VP никогда не на крит-пути.
    LEVELS_VP_ENABLED: bool = True
    LEVELS_VP_TF: str = "1h"            # таймфрейм профиля (узлы 1h устойчивее шума)
    LEVELS_VP_BINS: int = 50            # ценовых корзин
    LEVELS_VP_TTL_SEC: float = 900.0    # кэш профиля на символ (15 мин) — не душим loop
    # Стоп: ставим чуть ЗА HVN-узел (узел держит; стоп прямо в узле выбьет шумом).
    LEVELS_VP_STOP_BUFFER_PCT: float = 0.10   # буфер за узлом
    LEVELS_VP_STOP_MAX_EXTRA_PCT: float = 0.40  # максимум доп. расширения риска (RR-предохранитель)
    # TP: не целимся СКВОЗЬ HVN — тянем цель к ближней стороне блокирующего узла.
    LEVELS_VP_TP_BUFFER_PCT: float = 0.10
    LEVELS_VP_TP_MIN_DIST_PCT: float = 0.35   # не схлопываем TP1 ближе этого от входа

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

    # =========================
    # TREND RIDE — режим-зависимый выход
    # =========================
    # В трендовом режиме НЕ выходим у безубытка на микроплюсе и не фиксируем ранний
    # capture — даём поездке развиться и трейлим шире, чтобы забирать движение
    # до слома/разворота. В scalp/range-режиме поведение прежнее (быстрый выход).
    TREND_RIDE_ENABLED: bool = True
    # Не трогаем позицию protective-логикой, пока MFE не дошёл до этого порога (%).
    TREND_RIDE_MIN_MFE_TO_PROTECT_PCT: float = 1.2
    # В тренде выходим, отдав эту долю от MFE (шире, чем обычный ~0.35 → едем дольше).
    TREND_RIDE_TRAIL_DRAWDOWN_PCT: float = 0.50

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
    # ВАЖНО: критерий тут НЕ тот же, что у HTX_SYMBOLS. Фандинг-арб платит спред
    # ОДИН раз на входе и амортизирует его на десятки 8ч-периодов сбора фандинга,
    # поэтому широкий спред терпим, а ВЫСОКИЙ фандинг важнее — он живёт на
    # волатильных альтах (DOGE/SUI — перегретые лонги). Поэтому здесь альты
    # уместны (в отличие от трендового универсума). Фильтры min_rate/basis/
    # net_yield отсекут невыгодные окна. BTC/ETH фандинг обычно мизерный.
    FUNDING_ARB_SYMBOLS: str = "BTC/USDT,ETH/USDT,SOL/USDT,XRP/USDT,DOGE/USDT,SUI/USDT"

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
    MARKET_CONNECTIVITY_MAX_LATENCY_MS: int = 15000
    MARKET_CONNECTIVITY_MAX_SPREAD_PCT: float = 0.75
    EXCHANGE_RECONCILIATION_ENABLED: bool = False


    @property
    def execution_market_type(self) -> str:
        """Рынок исполнения. ENABLE_FUTURES_EXECUTION → swap (шорты), иначе MARKET_TYPE."""
        return "swap" if self.ENABLE_FUTURES_EXECUTION else self.MARKET_TYPE

    @property
    def execution_leverage(self) -> int:
        """Плечо исполнения. Сейчас всегда 1 — smart leverage не подключён к сайзингу
        (Фаза 4 активация). Догма: плечо только на доказанном edge."""
        if self.ENABLE_FUTURES_EXECUTION:
            return max(int(self.FUTURES_LEVERAGE), 1)
        return 1

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