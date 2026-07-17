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

    # (#leak-correlation) Кластерный лимит нетто-направления. Наша вселенная —
    # коррелированные мажоры (BTC/ETH/SOL/AVAX/XRP, corr~0.85+): шорт по всем сразу
    # = одна ставка с плечом, на общем движении проигрывают разом (аудит: −15.5 за
    # одно up-движение по 4 шортам). Лимит одновременных однонаправленных позиций
    # в кластере. CORR_CLUSTER_SYMBOLS пусто → весь портфель = один кластер.
    CORR_CLUSTER_ENABLED: bool = True
    CORR_CLUSTER_MAX_SAME_DIR: int = 2
    CORR_CLUSTER_SYMBOLS: str = ""  # "" → вся вселенная один кластер

    # (#leak-cost-bleed) Минимальный модельный TP1-нетто после издержек (USDT).
    # 0.0 → TP1 хотя бы не под водой (мягко). Поднять, если мелкие минусы у входа
    # продолжатся (издержки ~0.3-0.45% round-trip съедают флэт-сделки).
    ANTI_DRAIN_MIN_NET_PNL_TP1_USDT: float = 0.0

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

    # ── LiveExecutor: безопасное ядро исполнения (готовность к Live) ──────────
    # Режим живого пути: off | dry_run | live. dry_run по умолчанию — живая логика
    # проходит ПОЛНОСТЬЮ, но реальный ордер НЕ отправляется (валидация на бумаге).
    # live разрешён ТОЛЬКО при ENABLE_LIVE_ORDERS=true (иначе понижается до dry_run).
    LIVE_EXECUTION_MODE: str = "dry_run"
    LIVE_SET_LEVERAGE: bool = True            # выставлять плечо/режим маржи для swap
    LIVE_MARGIN_MODE: str = "cross"           # cross | isolated (swap)
    LIVE_FILL_POLL_TIMEOUT_SEC: float = 10.0  # ждать подтверждения филла
    LIVE_FILL_POLL_INTERVAL_SEC: float = 1.0
    # Предохранитель live_limited: макс. нотионал ОДНОГО ордера (USDT). 0 → выкл.
    # Для старта живой торговли держим крошечным (напр. 25), потом поднимаем.
    LIVE_MAX_ORDER_NOTIONAL_USDT: float = 25.0
    # Сайзинг от РЕАЛЬНОГО баланса биржи (fetch_balance), а не от RISK_EQUITY_USDT.
    # В live эквити = свободный USDT соответствующего счёта в моменте (SPOT и
    # USDT-M фьючерсы — РАЗНЫЕ счета HTX). Растёт с пополнениями владельца и
    # прибылью. RISK_EQUITY_USDT остаётся дефолтом для paper/dry_run и fallback.
    LIVE_SIZE_FROM_BALANCE: bool = True
    LIVE_BALANCE_CACHE_SEC: float = 30.0      # TTL кэша баланса (не дёргать API на каждый сайзинг)

    # ── Плечо ПО ДВИЖКУ (разный риск-профиль → разное плечо) ──────────────────
    # Жёсткий потолок: ни один движок не выставит плечо выше (предохранитель).
    LIVE_MAX_LEVERAGE: float = 5.0
    # FUNDING: дельта-нейтральный хедж (лонг spot + шорт swap равного размера) —
    # ценовой риск захеджирован, поэтому swap-ногу можно вести с бОльшим плечом
    # ради капиталоэффективности. НО: spot и swap в HTX — РАЗНЫЕ счета, маржа НЕ
    # взаимозачитывается, и swap-нога может быть ликвидирована на резком ходе,
    # даже когда spot-нога в плюсе. Поэтому умеренно (2x), а не «сколько дают».
    FUNDING_LEVERAGE: int = 2
    # TREND — направленная ставка: плечо ПРЯМО множит риск → консервативно
    # (execution_leverage = FUTURES_LEVERAGE, по умолчанию 1, поднимать на edge).
    # GRID — мартингейл (добор в просадку): плечо + мартингейл = ликвидация,
    # держим минимальным (GRID_LEVERAGE, по умолчанию 1).

    # ── Режим маржи ПО ДВИЖКУ (cross/isolated) ───────────────────────────────
    # TREND isolated: локализуем риск каждой направленной ставки (одна сделка не
    #   каскадит на другие; макс. убыток предсказуем; стоп срабатывает раньше).
    # GRID isolated: при плече 1x ликвидация ~100% от входа → ATR-стоп корзины
    #   всегда раньше; изоляция не даёт просадке сетки выесть маржу других движков.
    # FUNDING cross: дельта-нейтральной swap-ноге нужен буфер ВСЕГО swap-счёта,
    #   иначе isolated-ликвидация одной ноги РАЗОРВЁТ хедж (spot и swap — разные
    #   счета HTX, маржа не взаимозачитывается). Cross держит хедж живым.
    TREND_MARGIN_MODE: str = "isolated"
    GRID_MARGIN_MODE: str = "isolated"   # базовый режим сетки (при плече 1x)
    FUNDING_MARGIN_MODE: str = "cross"
    # Порог плеча сетки, ВЫШЕ которого isolated небезопасен (ликвиднёт корзину до
    # mean-reversion) → авто-переключение на cross (см. grid_effective_margin_mode).
    GRID_MARGIN_ISOLATED_MAX_LEV: float = 1.0

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
    FAILED_SETUP_LOSS_MID_PCT: float = -0.8 # было -0.65 (изм 17.07.2026 )
    FAILED_SETUP_LOSS_DEEP_PCT: float = -1.2 # было -0.90 (изм 17.07.2026 )
    FAILED_SETUP_MIN_AGE_SEC: int = 600
    # (#2 консолидация) failed_setup_exit в ТРЕНДЕ выключен — он рубил в шумовой
    # полосе до структурного smart-стопа, часто на вике. В тренде бэкстоп =
    # smart-stop + breakeven_lock + ride-трейл. True вернёт прежнее поведение.
    FAILED_SETUP_EXIT_TREND_ENABLED: bool = True # (изм 17.07.2026)

    # =========================
    # БЕЗУБЫТОК-ЗАМОК (#1/#2)
    # Корневая проблема телеметрии: positive_then_negative 50-64%. Сделки
    # доходили до +1% MFE и закрывались в минус через failed_setup_exit.
    # Как только сделка показала значимый MFE (ARM), запрещаем закрываться
    # глубоко в минус: фиксируем у безубытка (+комиссии), не отдаём ход.
    # =========================
    BREAKEVEN_LOCK_ENABLED: bool = True
    # MFE (%), после которого вооружается безубыток-замок.
    # (#leak-give-back) Был 0.80 — но проигравшие шли в плюс лишь на 0.3–0.66%
    # (НИЖЕ порога), безубыток не вооружался, в тренде ride тоже молчал (нужен
    # MFE≥1.2) → сделка держалась до полного хард-стопа −4.5 вместо ~безубытка.
    # 0.45 ловит откатчиков в диапазоне 0.45–1.2%. Над-килла нет: выход всё равно
    # требует flow_against ИЛИ ухода за hard_floor (−0.35%), т.е. не по вику.
    BREAKEVEN_LOCK_ARM_PCT: float = 0.35 # было 0.45 (изм 17.06.2026)
    # Уровень результата (%), на котором фиксируемся после вооружения:
    # как только текущий профит откатил к этому полу — выходим тут, а не
    # ждём failed_setup_exit на -0.6/-0.9%.
    # (#leak-be-lock-2026-07-09) 0.10→0.15: пол должен покрывать round-trip издержки
    # swap (~0.12%) — фиксация на +0.10% давала около-нулевой/минусовой нетто.
    BREAKEVEN_LOCK_FLOOR_PCT: float = 0.10 # было 0.15 (изм 17.06.2026)
    # (#wick) Вик-фильтр для мягких выходов. В тренде откат вверх — обычно тонкий
    # вик-пулбэк, а не разворот: выходить по нему = выбиться перед продолжением.
    # Мягкие выходы (breakeven_lock, failed_setup soft/mid) срабатывают ТОЛЬКО
    # если поток подтвердил разворот (flow_against по CVD) ИЛИ цена ушла за
    # hard_floor. Иначе держим — бэкстопом остаётся smart-stop и deep-порог.
    EXIT_REQUIRE_FLOW_CONFIRM: bool = False # (изм 17.07.2026)
    # Глубина минуса, при которой breakeven_lock выходит БЕЗ подтверждения потоком
    # (реальный неблагоприятный ход, а не вик).
    # (#leak-be-lock-2026-07-09) −0.35→−0.05: «безубыток-замок» закрывал вооружённые
    # сделки на −0.33% нетто (#201) — замок, теряющий деньги. После MFE≥arm сделка
    # не имеет права уходить глубже символических −0.05%.
    BREAKEVEN_LOCK_HARD_FLOOR_PCT: float = -0.05
    # (#churn) Re-entry cooldown в авто-цикле: не открывать ту же сторону символа
    # сразу после закрытия (особенно стопа). Машинка ReEntryCooldownGuard уже
    # есть, флаг включает её проверку в robot_loop.
    REENTRY_COOLDOWN_ENABLED: bool = True

    # ── LiquidityGuard: защита от расширения спреда (тонкая ликвидность) ───────
    # Единый адаптивный спред-гард для ВСЕХ движков (trend/grid/funding/ML).
    # «Широко» = текущий спред > mult×(EWMA-база символа) ИЛИ > абс. потолка.
    # Это не часы по UTC (данные опровергли ночную гипотезу), а живое состояние
    # ликвидности — реагирует на реальный спайк спреда когда бы он ни случился.
    LIQUIDITY_GUARD_ENABLED: bool = True
    LIQ_BLOCK_ENTRY: bool = True            # не открываться при широком спреде
    LIQ_PROTECT_EXIT: bool = True           # не давать спайку выбивать софт-стопы
    LIQ_SPREAD_ABS_MAX_BPS: float = 25.0    # абс. потолок входа, bps (0.25%)
    LIQ_SPREAD_BASELINE_MULT: float = 3.0   # вход: широко если >3× базы символа
    LIQ_EXIT_SPREAD_MULT: float = 4.0       # выход подавляем только на бОльшем спайке
    LIQ_SPREAD_BASELINE_ALPHA: float = 0.05 # EWMA-сглаживание базы (≈20 семплов)
    LIQ_SPREAD_MIN_BASELINE_BPS: float = 1.0  # пол базы (ранний/нулевой не перетриггерит)
    LIQ_EXIT_MAX_AGE_SEC: float = 30.0      # свежесть кэша для подавления выхода

    # (#leak-bad-entry) Анти-чоп gate на входе. Аудит: ETH #104 (MFE 0.0 → −4.45)
    # и BTC #106 (MFE 0.30 → −4.68) — шорты в чопповый/разворачивающийся рынок,
    # выходной логикой не спасти. Меряем силу тренда ADX-подобно через веер EMA:
    # направленный спред EMA20↔EMA200, нормированный на ATR якорного ТФ.
    #   long:  spread = ema20 - ema200   short: spread = ema200 - ema20
    #   fan_atr = spread / atr14
    # В реальном тренде веер раскрыт ПО направлению (fan_atr велик); в чопе EMA
    # свёрнуты (fan_atr→0), при входе против тренда спред отрицателен. Если
    # fan_atr < порога — рынок не трендовый для этой стороны, ждём.
    ANTI_CHOP_GATE_ENABLED: bool = True
    # Якорный ТФ для оценки силы тренда (структурный, не исполнительный).
    ANTI_CHOP_ANCHOR_TF: str = "1h"
    # Мин. раскрытие веера EMA в ATR. Поднято 0.5→0.8 (#expectancy-cleanup):
    # данные показали 52% входов с MFE<0.35% (сразу мимо) и E/сделку trend-long
    # −1.60 / «strong confirmed» −1.68 — это входы БЕЗ реального тренда по
    # направлению. 0.8 требует явный веер EMA в сторону сделки → стоим в стороне
    # в боковике/против тренда. Регайм-симметрично (лонги и шорты на равных).
    ANTI_CHOP_MIN_EMA_FAN_ATR: float = 0.8

    # (#htf-align) Выравнивание со старшим ТФ: не лонгуем против 4h-даунтренда,
    # не шортим против 4h-аптренда (price vs ema200 на HTF). Бьёт контртренд-входы.
    HTF_ALIGN_ENABLED: bool = True
    HTF_ALIGN_TF: str = "4h"

    # (#range-pos) Не входить в невыгодный край диапазона: шорт — только в верхней
    # части (range_pos>=SHORT_MIN), лонг — только в нижней (range_pos<=LONG_MAX).
    # Бьёт 0%-WR паттерн «шорт у поддержки / лонг у сопротивления».
    RANGE_POS_GATE_ENABLED: bool = True
    RANGE_POS_ANCHOR_TF: str = "1h"
    RANGE_POS_SHORT_MIN: float = 0.40   # шорт нельзя в нижних 40% диапазона
    RANGE_POS_LONG_MAX: float = 0.60    # лонг нельзя в верхних 40% диапазона

    # MFE-протекция и частичная фиксация в процентах.
    PROTECTIVE_MFE_START_PCT: float = 0.80
    PROTECTIVE_DRAWDOWN_SHARE: float = 0.35
    # Поднято 0.90→1.30 (#expectancy-cleanup): только 4/101 сделки дошли до MFE≥2%,
    # но дали ВСЮ прибыль (+20). Победителей резали на ~60% MFE (capture ~40%).
    # Старт трейла/фиксации позже → раннеры доезжают к TP2; мелкие плюсы (0.45–1.3)
    # держит безубыток-замок, не давая откатиться в минус.
    ADAPTIVE_TRAIL_MFE_START_PCT: float = 0.9 # было 1.3 (изм 17.07.2026)
    ADAPTIVE_TRAIL_DRAWDOWN_PCT: float = 0.35

    # Adaptive MFE capture experiment: earlier before-TP1 profit lock when
    # fresh paper data shows positive->negative giveback.
    MFE_CAPTURE_ENABLED: bool = True
    MFE_CAPTURE_START_PCT: float = 0.9   # 1.30→0.90 (изм. 17.07.2026)
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
    # (#crt-part13-2026-07-10) 5m→15m: по канонической таблице HTF↔LTF пара для
    # 4h — это 15m (5m — пара для HTF 1h). Вызов в market_intelligence и так
    # ожидал 15m (fallback), config переопределял на 5m. MSS/FVG на 15m чище.
    CRT_LTF_TF: str = "15m"                # младший ТФ для входа/MSS/FVG
    CRT_MIN_RANGE_PCT: float = 1.5         # мин. ширина C1-диапазона (%)
    CRT_LTF_CONFIRM: str = "either"        # "either" | "both" | "off" (MSS/FVG)
    # (#crt-part13-2026-07-10) CISD-чек из LTF Sequence (CRT→CISD→OTE→MSS→IDM):
    # манипуляционная свеча C2 должна ЗАКРЫТЬСЯ против свипа (свип CRH →
    # медвежье закрытие ниже открытия; свип CRL → бычье выше открытия) — это и
    # есть «close below OHP / above OLP» из инструкции. False → прежнее поведение.
    CRT_REQUIRE_CISD: bool = True
    # (#crt-part13-2026-07-10) Цели по инструкции: Target1 = 50% диапазона
    # (частичная фиксация — теперь РЕАЛЬНО исполняется TP1-partial'ом),
    # Target2 = 100% (противоположный край CRH/CRL). Прежние цели агрессивнее
    # (TP1 = сразу край, TP2 — за диапазоном): телеметрия CRT — missed_profit
    # avg 1.12%, capture −76% — до целей доезжали редко, пик отдавали.
    # "range" = инструкция (с RR-полом CRT_MIN_RR_TP1), "extended" = старое.
    CRT_TARGETS_MODE: str = "range"
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
    # (#leak-B) Фейдить вход не только по ярлыку тренда (часто "mixed"/"flat"),
    # но и по моментуму HTF/MTF: long не берём при bearish/oversold, short — при
    # bullish/overheated. Лечит контртрендовые crt_bull_sweep лонги в медвежьей
    # ленте. False → старое поведение (только trend-align).
    CRT_REQUIRE_MOMENTUM_ALIGN: bool = True

    # --- Scalp ENGINE (micro-flow вход: 5m микроструктура + стакан OBI/CVD) ---
    # ВОССТАНОВЛЕНО: движок (services/micro_scalp.py) и весь downstream (anti-drain/
    # breakeven/time-stop/trade_plan) были на месте и подключены в каскад
    # market_intelligence, но поле ENABLE_SCALP_STRATEGY НЕ было объявлено → при
    # extra="ignore" env молча игнорировался и getattr всегда давал False (движок
    # тёмный). Параметры движка тоже объявляем — теперь тюнятся из env (дефолты =
    # прежним getattr-фолбэкам, поведение не меняется). enabled включается из env.
    ENABLE_SCALP_STRATEGY: bool = False
    SCALP_EDGE_ZONE: float = 0.25              # вход в пределах этой доли от микро-края
    SCALP_MIN_MICRO_WIDTH_PCT: float = 1.2     # мин. ширина 5m-диапазона
    SCALP_TARGET_PCT: float = 0.8              # TP1 (net target, %)
    SCALP_TP2_MULT: float = 1.6               # TP2 = target * mult
    SCALP_STOP_BUFFER_ATR: float = 0.5         # стоп за микро-экстремумом (в ATR)
    SCALP_MIN_OBI: float = 0.15               # подтверждение потоком (OBI)
    SCALP_ENG_MIN_TP1_NET_PCT: float = 0.3     # мин. net TP1 после комиссий, %
    SCALP_ENG_ALLOW_SHORT: bool = True
    SCALP_MIN_SETUP_SCORE: float = 50.0
    SCALP_MAX_SPREAD_PCT: float = 0.06         # дороже — скальп не входит
    SCALP_REQUIRE_DEPTH: bool = True           # без живого стакана не торгует
    # (#scalp-htf-veto-2026-07-10) Микро-скальп по принципу живёт на 5m и HTF не
    # читает — но телеметрия 10 июля: ВСЕ убытки дня (−0.86: ETH #224, BTC #221,
    # ETH #220) — шорты «от сопротивления» против 1h RSI 76–78, т.е. фейд
    # разогнавшегося паровоза. Вето ТОЛЬКО на экстремумы старшего моментума:
    # не шортим при 1h RSI ≥ overheat, не лонгуем при 1h RSI ≤ oversold.
    # Обычные микро-края в нормальном 1h движок торгует как раньше.
    SCALP_HTF_EXTREME_VETO: bool = True
    SCALP_HTF_RSI_OVERHEAT: float = 70.0
    SCALP_HTF_RSI_OVERSOLD: float = 30.0

    # --- Scalp risk profile (trade_mode="scalp" / regime="range") ---
    # Скальп — маленькая позиция, мелкое движение, мелкие абсолютные суммы.
    # Глобальные пороги риска заточены под крупные трендовые сделки и душат
    # скальп. Эти параметры применяются ТОЛЬКО к range/scalp-входам; тренд
    # продолжает жить на строгих глобальных порогах.
    # (#scalp-size-2026-07-16) 0.10 → 0.20: скальп после правок exit-логики —
    # единственный режим с положительным capture (+23%), 13 сделок июля ≈ +0.24
    # при MAE ~0. Малый размер был защитой сливавшего скальпа; edge подтверждён →
    # удваиваем до потолка anti-drain (SCALP_ANTI_DRAIN_MAX_POSITION_MARGIN_PCT=20%,
    # выше — бессмысленно, срежет гейт). Экономика линейна по qty: стоп ≈ −0.8…−1.1
    # (vs трендовые −3…−4), TP1 ≈ $1.35, TP2 ≈ $2.3.
    SCALP_MAX_POSITION_MARGIN_PCT: float = 0.20        # доля эквити на одну скальп-позицию
    # Экономика скальпа: $ умеренные BY DESIGN (цель 0.4%; при размере 200 →
    # TP1 ≈$1.35, TP2 ≈$2.3). Прежние $-флоры (TP1 0.5 / TP2 1.0) рубили КАЖДЫЙ скальп
    # (tp1_net_pnl_below_min_usdt). Софт: TP1 0.20, TP2 0.55 — реальный гейт это
    # net_rr_tp2 (runner платит >1.10× стопа). Как у тренда: судим по TP2, не по TP1.
    SCALP_MIN_NET_PNL_TP1_USDT: float = 0.20           # санити, не гейт
    SCALP_MIN_NET_PNL_TP2_USDT: float = 0.55           # санити, не гейт
    SCALP_MIN_NET_RR_TP2: float = 1.10                 # РЕАЛЬНЫЙ гейт экономики скальпа
    SCALP_ANTI_DRAIN_MIN_EDGE_AFTER_COSTS_USDT: float = 0.0  # абсолютный edge-флор anti-drain
    SCALP_ANTI_DRAIN_MAX_POSITION_MARGIN_PCT: float = 20.0   # маржевый лимит anti-drain для скальпа
    SCALP_ANTI_DRAIN_MIN_NET_RR_TP1: float = 0.40
    SCALP_ANTI_DRAIN_MIN_NET_RR_TP2: float = 0.85
    # Scalp exit: безубыток-замок (трейл от MFE). Тренд-пороги capture (~0.95%)
    # и protective (1.2% / 1.5 USDT) под маленький скальп не вооружаются — и
    # зелёная сделка переворачивается в убыток (кейс LINK: +0.72% → −1.18%).
    # Замок трейлит от пика и фиксирует остаток в плюсе.
    SCALP_BREAKEVEN_ENABLED: bool = True
    # (#exit-replay-2026-07-09) Откалибровано по /ml/exit-replay на живых траекториях:
    # arm 0.3 / giveback 0.4 / time_stop 45 → +0.64% total против −0.74% факта
    # (все 0.5/0.6-варианты в минусе). Плюс телеметрия: positive_then_negative 57–62%
    # — замок вооружался слишком поздно и отдавал слишком много пика.
    SCALP_BREAKEVEN_ARM_PCT: float = 0.3         # MFE %, с которого включается замок
    SCALP_BREAKEVEN_GIVEBACK_SHARE: float = 0.4  # выходим, отдав эту долю пика MFE
    # (#geometry-arm-2026-07-09) Замок масштабируется ГЕОМЕТРИЕЙ сделки: эффективный
    # arm = max(SCALP_BREAKEVEN_ARM_PCT, TP1_dist × эта доля). Микро-скальп (TP1
    # ~0.8-1%) живёт на абсолютных 0.3%; range-вход (TP1 ~2%) вооружается от ~0.6%.
    # Лечит слив: #216/#217 (range, цель 2%) резались замком на +0.02/+0.03 нетто —
    # издержки ~0.15% съедали фиксацию, «выигрыши» приносили ноль при стопах −0.5%.
    # Гарантия giveback 0.4: замок фиксирует ≥0.6×arm_eff ≈ 0.37%+ для range — выше костов.
    SCALP_BE_ARM_TP1_SHARE: float = 0.30
    # Скальп тайм-стоп (профиль ведения SCALP): сделка должна разрешиться быстро.
    # Если за N минут скальп не вооружился (mfe < arm) — закрываем по текущей цене,
    # чтобы «мёртвая» сделка не дрейфовала в свинг-убыток и освободила слот.
    SCALP_TIME_STOP_ENABLED: bool = True
    SCALP_TIME_STOP_MIN: float = 45.0            # минут до тайм-стопа невооружённого скальпа
    # (#range-time-stop-2026-07-09) Range-сделки (стоп ~2.4%, TP1 ~2%) — другая
    # геометрия, чем микро-скальп: 45 минут не хватает диапазону разрешиться
    # (SOL #214 убит тайм-стопом на пути к TP1, ADA #215 не успела доехать).
    # Отдельный таймер для regime=range; остальная скальп-механика (замок,
    # giveback) остаётся общей.
    RANGE_TIME_STOP_MIN: float = 90.0
    # (#audit-time-stop) Cost-aware grace: не в значимом минусе и поток не против →
    # держим до жёсткого стопа (MIN × MULT). Реальный минус/CVD-разворот — закрытие сразу.
    # (#exit-replay-2026-07-09) 2.0→1.5: grace-окно продлевало «мёртвые» сделки до
    # 90 мин, и они дрейфовали в минус (SOL #214 −0.51%, #213 −0.08% на 90-й мин).
    # Условие grace тоже ужесточено в exit_policy: нужно И not_losing, И showed_life.
    SCALP_TIME_STOP_HARD_MULT: float = 1.5

    # (#audit-cost-model) Полы net-safe по типу рынка. Единый пол 0.60% (производный
    # от спот-комиссии 0.2%) завышал защитные пороги swap-сделок вдвое.
    NET_SAFE_FLOOR_SPOT_PCT: float = 0.60
    NET_SAFE_FLOOR_SWAP_PCT: float = 0.30

    # (#audit-event-spam) Дедуп повторяющихся blocked-событий intelligence_events:
    # одно и то же (symbol, decision) не пишем чаще, чем раз в N минут.
    INTEL_EVENT_DEDUP_MINUTES: float = 10.0

    # (#audit-traj) Компактная траектория сделки [age_sec, current_pct] в
    # lifecycle — сырьё для offline A/B exit-параметров (/ml/exit-replay).
    # Только запись данных, на торговые решения не влияет.
    TRAJ_RECORD_ENABLED: bool = True
    TRAJ_MIN_STEP_PCT: float = 0.05     # новая точка при изменении result_pct на этот шаг
    TRAJ_MAX_POINTS: int = 400          # при переполнении прореживаем ×2 и удваиваем шаг

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
    # (#leak-A) Порог снижен 0.75→0.45: зона OBI -0.5…-0.68 — уже сильное
    # давление против входа (в live #198 XRP obi -0.68 / #194 ADA -0.67 / #197
    # AVAX -0.50 — все лонги в стоп), её больше не пропускаем. 0 → выкл.
    OB_OBI_HARD_VETO: float = 0.45
    # (#leak-A) Встречная стенка засчитывается как опора входа, только пока OBI
    # не глубже этого порога. Раньше стоячая бид-стенка >= wall_confirm пропускала
    # long при любом отрицательном OBI (её «съедали»). 0 → стенка спасает всегда.
    OB_WALL_RESCUE_MAX_ADVERSE_OBI: float = 0.35
    # (#leak-A) CVD на ТОНКОЙ выборке (cvd_trades < OB_CVD_MIN_TRADES): если поток
    # ~полностью против входа (|cvd_ratio| >= OB_CVD_THIN_RATIO) — блок. На неликвиде
    # окно даёт 1–5 сделок и обычный CVD-фильтр не включался. 0 → выкл.
    OB_CVD_THIN_RATIO: float = 0.9
    OB_CVD_THIN_MIN_TRADES: int = 1
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
    # «Смотрим на сейчас, не живём прошлым»: окно guard по ВРЕМЕНИ (часы) для
    # ЖИВОГО решения публикации. Исходы старше выпадают из оценки сами → блок
    # снимается без ручного сброса.
    SYMBOL_PERF_WINDOW_HOURS: float = 24.0
    # Окно для OWNER-ВИТРИНЫ (/analytics/symbol-performance). Шире живого, чтобы
    # оператор видел историю по символам, а не пустые no_history после падения
    # частоты сделок. На решения НЕ влияет — только отображение.
    SYMBOL_PERF_SUMMARY_WINDOW_HOURS: float = 720.0  # 30 дней
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
    # Софт 0.20→0.10 заодно с production_gate: anti-drain не должен возвращать
    # гейт на TP1 после того, как мы перенесли экономику на TP2.
    ANTI_DRAIN_MIN_NET_RR_TP1: float = 0.10
    ANTI_DRAIN_MIN_NET_RR_TP2: float = 0.90       # реальный гейт награды — на TP2
    ANTI_DRAIN_MIN_EDGE_AFTER_COSTS_USDT: float = 1.20
    ANTI_DRAIN_MAX_POSITION_MARGIN_PCT: float = 12.0
    ANTI_DRAIN_MAX_USED_MARGIN_PCT: float = 50.0
    # POSITION (trend) профиль anti-drain: согласован с trade_plan
    # (MAX_POSITION_MARGIN_PCT=0.13). Снижено 35→15 вместе с размером сделки —
    # держим буфер над планом (план 13% < блок 15%), чтобы пограничную позицию не
    # резало целиком. 5 позиций × ~13% = 65% < общий потолок 70% → диверсификация.
    # weak_structure/overheated/economics-по-TP1 для тренда отключаются в robot_loop
    # (тренд растянут и перегрет by design; награда позиции — на TP2).
    ANTI_DRAIN_POSITION_MAX_MARGIN_PCT: float = 15.0
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
    # TP1 = точка частичной фиксации/перевода в безубыток, BY DESIGN близкая →
    # net_rr_tp1 мал. Пол 0.50 резал сетапы с отличным TP2 (RR 2.5+), но «крошечным»
    # TP1 (массовые b_rr_tp1_too_low по AVAX/BTC/ETH/XRP при net_rr_tp2 2.5–2.7).
    # Софт-пол 0.10 (только санити против вырожденного TP1≈entry). ЭКОНОМИКУ судит
    # TP2: production_gate min_rr_tp2 (1.30–1.45, «живой» и в paper) + anti-drain
    # economics_use_tp2 (net_pnl_tp2 ≥ |стоп|+edge). Инверсия RR прикрыта именно TP2.
    PROD_GATE_A_PLUS_MIN_RR_TP1_PAPER: float = 0.10   # санити, не гейт
    PROD_GATE_A_PLUS_MIN_RR_TP2: float = 1.45     # live
    PROD_GATE_A_PLUS_MIN_RR_TP2_PAPER: float = 1.15   # spot 0.2% paper

    # Grade A: setup_score >= 62, confidence >= 58
    PROD_GATE_A_MIN_SETUP: float = 65.0
    PROD_GATE_A_MIN_CONFIDENCE: float = 62.0
    PROD_GATE_A_MIN_RR_TP1: float = 0.90          # live
    PROD_GATE_A_MIN_RR_TP1_PAPER: float = 0.10    # санити, не гейт (см. A+ выше)
    PROD_GATE_A_MIN_RR_TP2: float = 1.35          # live
    PROD_GATE_A_MIN_RR_TP2_PAPER: float = 1.05    # spot 0.2% paper

    # Grade B: setup_score >= 58, confidence >= 60
    PROD_GATE_B_MIN_SETUP: float = 58.0
    PROD_GATE_B_MIN_CONFIDENCE: float = 60.0
    PROD_GATE_B_MIN_RR_TP1: float = 0.85          # live
    PROD_GATE_B_MIN_RR_TP1_PAPER: float = 0.10    # санити, не гейт (см. A+ выше)
    PROD_GATE_B_MIN_RR_TP2: float = 1.30          # live
    PROD_GATE_B_MIN_RR_TP2_PAPER: float = 0.85    # spot 0.2% paper
    PROD_GATE_B_MIN_PRIORITY: float = 85.0

    # (#grade-fix-2026-07-06) Пороги грейда (по composite score = effective_confidence
    # + setup-бонус + regime). Были захардкожены 88/78/62 → ладдер не разлипался,
    # ~все сигналы B, grade_ord как ML-фича = константа. Рекалибровано под реальное
    # распределение: сильный сетап → A, исключительный → A+, средний → B, слабый → C.
    # БЕЗОПАСНО: размер позиции больше НЕ зависит от грейда (перенесён на ml_score, §9),
    # поэтому релейбл не раздаёт эквити. Тюнится без релиза. NB: после накопления
    # сигналов на новой шкале — переобучить мета-лейблер (grade_ord меняет распределение).
    GRADE_A_PLUS_MIN_SCORE: float = 82.0
    GRADE_A_MIN_SCORE: float = 73.0
    GRADE_B_MIN_SCORE: float = 62.0

    # =========================
    # RISK MANAGEMENT
    # =========================
    MAX_DAILY_LOSS_PCT: float = 3
    MAX_DRAWDOWN_PCT: float = 15
    # MAX_OPEN_POSITIONS удалён: его читал только RiskEngine.allow(), который в
    # боевом цикле не вызывался (мёртвый код). Реальный потолок числа позиций —
    # ANTI_DRAIN_MAX_OPEN_POSITIONS (anti_drain_guard). RiskEngine тоже удалён.
    RISK_PER_TRADE_PCT: float = 0.5
    # (#диверсификация) Снижено 0.30→0.13 ради БОЛЬШЕГО ЧИСЛА параллельных
    # позиций. Раньше сделка занимала ~30% экв (~285 USDT), и 3 трендовых
    # раннера уже выбирали 70%-потолок маржи (665) → CRT/A+ душились
    # blocked_total_margin_limit (см. аудит, течь #5). При 13% сделка ≈123 USDT,
    # и 5×123=615 < 665 — влезает 5 параллельных (= ANTI_DRAIN_MAX_OPEN_POSITIONS).
    # Риск $ на сделку падает (меньше qty), что и есть диверсификация. Буфер под
    # anti-drain-кап (15%) сохранён: план 13% < блок 15%.
    MAX_POSITION_MARGIN_PCT: float = 0.13
    # === ДИНАМИЧЕСКОЕ РАСПРЕДЕЛЕНИЕ МАРЖИ ПО КАНДИДАТАМ ЦИКЛА ===
    # Когда сетап прошёл ВСЕ гейты, система считает сколько ещё кандидатов прошло
    # гейты в этом же цикле и делит СВОБОДНУЮ маржу (потолок − открытые) поровну.
    # Кандидат один → берёт всю свободную маржу (статический MAX_POSITION_MARGIN_PCT
    # тогда не ограничивает — динамический бюджет его замещает). Флаг ON → активно.
    ENABLE_DYNAMIC_MARGIN_ALLOC: bool = True
    # Верхний предохранитель на ОДНУ сделку как доля свободной маржи (1.0 = 100%,
    # как просил Капитан; поставь 0.4–0.5, если захочешь подушку под добор/2-й вход).
    DYNAMIC_MARGIN_CAP_PCT_OF_FREE: float = 1.0
    # Регулируемое плечо для размера по динамическому бюджету (нотионал = маржа×плечо).
    # 1.0 = без плеча (как сейчас, smart leverage off). Поднимай для live-фьючерсов.
    DYNAMIC_MARGIN_LEVERAGE: float = 1.0
    # Грейд-кап: «одинокий берёт всю свободную маржу» — только для A/A+. Слабый B
    # не должен получать весь free (TRX #101 B → 430 маржи → -5.7). Для B бюджет
    # капается этой долей свободной маржи. A/A+ — без этого капа.
    DYNAMIC_MARGIN_B_CAP_PCT_OF_FREE: float = 0.5
    # (#grade-ml-sync-2026-07-09) Размер = min(грейд-ось, ML-ось). Грейд — публичная
    # уверенность (виден в Telegram-канале): одинокий A/A+ забирает весь free,
    # несколько — поровну, B капается DYNAMIC_MARGIN_B_CAP_PCT_OF_FREE. ML — приватный
    # модулятор: score < ML_SIZE_FULL_MIN_SCORE ужимает размер до ML_SIZE_LOW_MULT
    # (в full_auto такой вход вообще блокируется гейтом ML_MIN_SCORE_TO_TRADE).
    # Слабейшая ось решает: опубликованный A не несёт полный размер против мнения ML,
    # B не получает полный бюджет только за высокий score — канал и эквити согласованы.
    # ML off/нет score → чистый грейд (fail-open). История: #grade-ml-2026-07-06
    # игнорировал грейд при живом score (тогда грейды слипались в B); после
    # рекалибровки порогов (#grade-fix-2026-07-06) ось снова информативна.
    ML_SIZE_ALLOC_ENABLED: bool = True
    ML_SIZE_FULL_MIN_SCORE: float = 0.45   # >= → ML-ось не режет (мультипликатор 1.0)
    ML_SIZE_LOW_MULT: float = 0.5          # < → ML-ось ужимает до этой доли

    # (#ml-explore-2026-07-09) Exploration-квота full_auto НА PAPER: каждый N-й
    # кандидат, заблокированный ML-гейтом, всё же открывается микро-размером —
    # чисто ради РАЗМЕТКИ. Иначе full_auto создаёт селекционное смещение: датасет
    # пополняется только сделками score>=порога, модель перестаёт учиться на том,
    # что сама режет, и retrain деградирует (особенно критично сейчас: exit-логика
    # изменена, старые метки устаревают). На live (is_live_enabled) exploration
    # АВТО-ВЫКЛЮЧЕН — гейт режет без исключений, реальные деньги не платят за
    # обучение. Размер пробы: ML_EXPLORE_SIZE_MULT поверх ml-оси 0.5 → ~25% бюджета.
    ML_EXPLORE_ENABLED: bool = True
    ML_EXPLORE_EVERY_N: int = 3
    ML_EXPLORE_SIZE_MULT: float = 0.5

    # =========================
    # УМНАЯ СЕТКА (GRID) — отдельный движок на swap, работает ПАРАЛЛЕЛЬНО тренду
    # на СВОЙ карман маржи. Тренд-позиции/ордера НЕ трогает. Toggle из API/фронта
    # (рантайм-флаг в grid_store; GRID_ENABLED — лишь дефолт при старте).
    # =========================
    GRID_ENABLED: bool = False                  # дефолт выкл; вкл/выкл из /grid
    # Путь состояния сетки. Локально — относительный. На Render файловая система
    # эфемерна (деплой стирает диск) → состояние ДОЛЖНО лежать на persistent-диске.
    # В render.yaml выставляем /app/storage/ml/grid_state.json (тот же ml-disk, что
    # переживает деплой) — иначе сетка сбрасывается и выключается на каждом деплое.
    GRID_STATE_PATH: str = "storage/grid/grid_state.json"
    GRID_SYMBOLS: str = "BTC/USDT,ETH/USDT,SOL/USDT"  # ликвидные swap-пары
    GRID_TIMEFRAME: str = "1h"                  # ТФ для ATR/EMA/RSI (1h меньше шума)
    GRID_LINES: int = 6                         # число уровней (на сторону для neutral)
    GRID_BASE_ORDER_USDT: float = 20.0          # базовый объём ордера (нотионал, USDT)
    GRID_VOL_MULTIPLIER: float = 1.2            # m_vol: мартингейл объёма (1.1–1.5)
    GRID_STEP_MULTIPLIER: float = 1.1           # m_step: расширение шага (1.05–1.2)
    GRID_VOL_COEFF: float = 0.5                 # k_vol: шаг = ATR·k_vol
    # (#audit-grid) Анти-пила флипа: разворот засчитывается только когда цена
    # реально ушла от EMA на k×ATR — почасовое обнимание EMA больше не пилит
    # корзину (12 из 20 последних закрытий были flip-минусами, −1.73 USDT).
    # (#grid-flip-2026-07-09) 0.5→1.0: телеметрия 8–9 июля — BTC три flip-минуса
    # подряд (−0.58/−0.23/−0.09) при цене в ±0.5×ATR от EMA200. Полный ATR
    # отделяет реальный уход от обнимания EMA. Сетка остаётся mean-reversion:
    # это НЕ трендовый фильтр входа, а лишь запрет пилить корзину в чопе.
    GRID_FLIP_MIN_ATR_DIST: float = 1.0
    # (#audit-grid / #grid-mean-revert) Анти-мартингейл: не доливаем ТОЛЬКО против
    # экстремального импульса. Усреднение на откате — само ядро сетки, поэтому
    # пороги 65/35 (резали любой дип-добор → сетка вела себя как тренд) расширены
    # до экстремумов 80/20: обычное усреднение работает, но блоу-офф (кейс TRX:
    # доборы в шорт при RSI 87) по-прежнему блокируется. Плюс сохраняются жёсткие
    # анти-блоу-ап лимиты: GRID_MAX_SAFETY_ORDERS, карман маржи, ATR-SL, `opposite`.
    GRID_ANTI_MARTINGALE_ENABLED: bool = True
    GRID_SHORT_FILL_RSI_MAX: float = 80.0
    GRID_LONG_FILL_RSI_MIN: float = 20.0
    # (#audit-grid) Экономика открытия: шаг 1-го уровня должен превышать
    # spread×mult + fee_round, иначе цикл платит спред каждым кругом (кейс
    # AAVE neutral-грид при спреде 0.1–0.5%).
    GRID_OPEN_MIN_EDGE_SPREAD_MULT: float = 1.0
    # (#audit-grid) Вычитать round-trip комиссии из realized при закрытии корзины
    # (раньше realized был gross → +1.80 за 79 циклов ещё и завышен).
    GRID_FEES_IN_REALIZED: bool = True
    GRID_ATR_PERIOD: int = 14
    GRID_EMA_PERIOD: int = 200
    GRID_RSI_PERIOD: int = 14
    GRID_RSI_HIGH: float = 70.0                 # RSI выше → не лонг-сетка (перегрев)
    GRID_RSI_LOW: float = 30.0                  # RSI ниже → не шорт-сетка (перепрод.)
    # Мёртвая зона ±% вокруг EMA200: цена в зоне → регайм NEUTRAL (не флипаем).
    # Гасит grid_regime_flip-пилу, когда цена висит на EMA200 в боковике.
    # (#grid-flip-2026-07-09) 0.25→0.60: BTC-чоп 8–9 июля ходил ±0.5% вокруг
    # EMA200 и band 0.25% не удерживал NEUTRAL → пила long/short. Шире зона =
    # ДОЛЬШЕ двусторонний грид (родная среда сетки), направленная лестница
    # только при реальном уходе цены. Это усиливает mean-reversion, не тренд.
    GRID_REGIME_EMA_BAND_PCT: float = 0.60
    GRID_TP_PCT: float = 0.8 # было 0.5 (изм 17.06.2026) тейк = безубыток + этот % (вся сетка)
    GRID_SL_ATR_MULT: float = 2 # было 1.5 (изм 17.06.2026) стоп = крайний уровень ± k·ATR
    GRID_MAX_SAFETY_ORDERS: int = 4 # было 6 (изм 17.06.2026) макс. исполненных уровней; дальше стоп выставлять
    GRID_MAX_USED_MARGIN_PCT: float = 20.0     # СВОЙ карман маржи (% экв), отдельно от тренда (70%)
    GRID_LEVERAGE: float = 1.0                 # плечо для нотионала/маржи (swap)
    GRID_FEE_ROUND_PCT: float = 0.1            # round-trip комиссия для безубытка, %
    GRID_REARM: bool = True                    # после TP/SL переоткрывать новый цикл
    GRID_SLIPPAGE_PCT: float = 0.05            # допуск проскальзывания при paper-филле, %
    GRID_TICK_INTERVAL_SEC: float = 20.0       # период фонового тика сетки

    # ── Адаптивность к живому рынку ───────────────────────────────────────────
    # Рынок движется: ATR меняется, регайм (EMA200/RSI) разворачивается, а цикл
    # висит на старых линиях. На каждом тике переоцениваем цикл по ЖИВЫМ данным.
    GRID_ADAPT_ENABLED: bool = True
    # Пере-раскладка НЕисполненных уровней под текущий ATR и дрейф цены. Якорь —
    # самый глубокий ИСПОЛНЕННЫЙ уровень (реальная позиция), от него лестница вниз
    # (buy)/вверх (sell) с текущим шагом ATR·k_vol·m_step^(i-1). Исполненные ордера
    # (реальные позиции) и их объёмы НЕ трогаются — двигаются только живые лимиты.
    GRID_RESPACE_ENABLED: bool = True
    # Разворот направления сетки при подтверждённой смене регайма (EMA200/RSI).
    # long-цикл + регайм short (или наоборот) — закрываем корзину по агрегату
    # (grid_regime_flip), следующий тик открывает цикл в НОВОМ направлении.
    GRID_FLIP_ON_REGIME: bool = True
    # Гистерезис: сколько подряд тиков регайм должен быть противоположным, прежде
    # чем разворачиваемся (защита от пилы на одном-двух шумных тиках).
    # (#grid-flip-2026-07-09) 3→6: при тике 20с confirm=3 — это всего минута
    # после истечения cooldown; 6 тиков (~2 мин) + band 0.60 + ATR-дистанция 1.0
    # вместе требуют устойчивого ухода, а не свечного выброса.
    GRID_FLIP_CONFIRM_TICKS: int = 6
    # (#grid-flip-cooldown) Тихое окно ПОСЛЕ открытия/флипа: пока не прошло столько
    # секунд, цикл НЕ переворачивается вообще. confirm ловит дрожь за 3 тика, но не
    # почасовую пилу на монетах у EMA (ETH/AAVE флипали ~раз в час по -0.05..-0.6,
    # съедая заработок BTC/XRP/AVAX). Регайм сетки считается на 1h ТФ — суб-часовые
    # флипы это шум by design, поэтому дефолт = 1 час. 0 = выключено.
    GRID_FLIP_COOLDOWN_SEC: int = 3600
    # (#grid-mean-revert) NEUTRAL (боковик) — РОДНАЯ среда сетки: двусторонний грид
    # (buy ниже + sell выше) зарабатывает именно на осцилляции в диапазоне. Заморозка
    # (True) гасила добор ровно в neutral → настоящий двусторонний грид открывался и
    # никогда не наполнялся, активной оставалась лишь односторонняя тренд-лестница.
    # Дефолт False: даём рейндж-гриду работать. Спред-риск неликвида уже отсекает
    # эконом-фильтр открытия (GRID_OPEN_MIN_EDGE_SPREAD_MULT). True вернёт заморозку.
    GRID_FREEZE_ON_NEUTRAL: bool = False

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
    # (#tp1-partial-2026-07-09) РЕАЛЬНАЯ частичная фиксация на TP1. Раньше «TP1 =
    # точка частичной фиксации» была фикцией: на TP1 двигался только стоп в
    # безубыток, прибыль НЕ реализовывалась → модальный исход «дошли до TP1 и
    # откатились к безубытку» приносил ~0 вместо половины TP1-профита. Это главный
    # источник positive_then_negative (57–62%) и отрицательного роллинг-PnL.
    # Теперь на TP1 закрывается TP1_PARTIAL_CLOSE_SHARE позиции по цене TP1,
    # остаток едет к TP2 под защитой breakeven-стопа.
    TP1_PARTIAL_ENABLED: bool = True
    TP1_PARTIAL_CLOSE_SHARE: float = 0.5

    # (#tp1-partial-2026-07-09) Гейт ОЖИДАЕМОЙ экономики: раз на TP1 реализуется
    # половина, «награда» сделки = share·netTP1 + (1−share)·netTP2. Требуем, чтобы
    # эта смесь платила минимум MIN_NET_RR_BLENDED × |стоп|. Заменяет фиктивную
    # оценку «вся награда на TP2» (TP2 достигается ~5% сделок).
    MIN_NET_RR_BLENDED: float = 1.10

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
    # Не трогаем позицию protective-логикой, пока MFE не дошёл до этого порога (%) было 1.2 (изм 17.07.2026).
    TREND_RIDE_MIN_MFE_TO_PROTECT_PCT: float = 0.8
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
    # (#paper-slippage-2026-07-09) Адверс-слиппедж СТОП-филлов в paper: стоп —
    # маркет-ордер по триггеру, на live он исполняется ХУЖЕ уровня. Раньше paper
    # закрывал ровно по стопу → PnL бумаги систематически завышен, а validation
    # gates судят live-готовность по этим числам. 0.05% — консервативно для
    # ликвидных пар HTX swap. TP не трогаем (лимитная семантика).
    PAPER_STOP_ADVERSE_SLIPPAGE_PCT: float = 0.05

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
    # Live: доля МЕНЬШЕГО свободного остатка (spot/swap), которую можно занять под
    # один хедж (буфер на проскальзывание/комиссии). Хедж занимает оба счёта.
    FUNDING_ARB_FREE_BUFFER_PCT: float = 95.0

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

    @property
    def grid_effective_margin_mode(self) -> str:
        """Режим маржи сетки С УЧЁТОМ плеча.

        При плече 1x isolated безопасен: ликвидация ~100% от входа, ATR-стоп
        корзины всегда срабатывает раньше. Но при плече >1x дистанция до
        ликвидации ≈1/плечо сжимается и становится МЕНЬШЕ длины лестницы сетки —
        isolated ликвиднёт корзину на дне просадки, ДО mean-reversion. Поэтому
        выше порога авто-переключаемся на cross (буфер всего счёта держит корзину).
        Рекомендация при cross: выделенный субсчёт под сетку, чтобы её просадка не
        затрагивала маржу других движков (риск тогда локализован счётом сетки).
        """
        lev = float(getattr(self, "GRID_LEVERAGE", 1.0) or 1.0)
        if lev > float(getattr(self, "GRID_MARGIN_ISOLATED_MAX_LEV", 1.0)):
            return "cross"
        return str(getattr(self, "GRID_MARGIN_MODE", "isolated")).lower()

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

    @property
    def grid_symbols(self) -> List[str]:
        return [s.strip().upper() for s in self.GRID_SYMBOLS.split(",") if s.strip()]

settings = Settings()