import asyncio
import logging
import time
from uuid import uuid4
from core.decision_codes import (
    DECISION_WAIT_BETTER_ENTRY_RR,
    DECISION_ACTIVE_SIGNAL_ALREADY_EXISTS,
    DECISION_MAX_ACTIVE_SIGNALS_REACHED,
    DECISION_REQUIRED_MARGIN_EXCEEDS_FREE_MARGIN,
)
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from datetime import datetime, timezone, timedelta
from threading import Lock

from contextlib import asynccontextmanager

from core.db import Base, engine, SessionLocal
from core.config import settings
from core.security import hash_password, require_owner_action, require_non_production_debug
from core.logging import get_logger, log_event

from models.user import User
from models.bot import Bot
from models.signal import Signal
from models.order import Order
from models.position import Position
from models.subscriber import Subscriber
from models.intelligence_event import IntelligenceEvent
from models.telegram_delivery import TelegramDelivery
from models.telegram_profile import TelegramProfile
from models.payment import BillingPlan, Payment, PaymentEvent
from models.funding_arbitrage import FundingArbOpportunity, FundingArbPosition

from workers.robot_loop import RobotLoop
from services.signal_broadcaster import SignalBroadcaster
from services.signal_lifecycle import SignalLifecycleManager
from services.signal_quality import SignalQualityService
from services.market_data import MarketDataService
from services.market_connectivity import MarketConnectivityService
from services.exchange_reconciliation import ExchangeReconciliationService
from services.strategy_engine import StrategyEngine
from services.ml_scorer import MLScorer
from services.telegram_router import TelegramRouter
from services.report_service import ReportService
from services.subscription_watchdog import SubscriptionWatchdog
from services.telegram_delivery_log import TelegramDeliveryLog, ensure_telegram_delivery_schema
from services.telegram_delivery_worker import TelegramDeliveryWorker
from services.billing_service import BillingService
from services.revenue_metrics import RevenueMetricsService
from services.customer_notifications import CustomerNotificationService
from services.payment_reconciliation import PaymentReconciliationService
from services.cost_engine import CostEngine
from services.trade_plan import TradePlanBuilder
from services.market_intelligence import MarketIntelligenceEngine
from services.intelligence_memory import IntelligenceMemory
from services.exposure_guard import ExposureGuard
from services.symbol_performance_guard import SymbolPerformanceGuard
from services.symbol_performance_summary import SymbolPerformanceSummaryService
from services.symbol_policy_replay import SymbolPolicyReplayService
from services.ml_outcome_stats import MLOutcomeStatsService
from services.ml_trade_logger import MLTradeLogger
from services.candidate_priority import CandidatePriorityService
from services.reentry_cooldown import ReEntryCooldownGuard
from services.production_entry_gate import ProductionEntryGate
from services.signal_replacement import SignalReplacementPolicy
from services.candidate_funnel import CandidateFunnelService
from services.outcome_diagnostics import OutcomeDiagnosticsService
from services.validation_gates import ValidationGateService
from services.system_health import SystemHealthService
from services.product_e2e_smoke import ProductE2ESmokeService
from services.telegram_bot_menu import TelegramBotMenuService
from services.audit_log import AuditLogService
from services.live_safety import LiveSafetyService
from services.live_shadow import LiveShadowDriftService
from services.funding_arbitrage import FundingMonitorService, FundingArbEngine
from services.exit_policy import ExitPolicyService
from services.daily_quality_report import DailyQualityReportService

from pydantic import BaseModel

# ── Domain routers ───────────────────────────────────────────────────────────
from routers.analytics import router as analytics_router
from routers.audit import router as audit_router
from routers.funding_arb import router as funding_arb_router
from routers.grid import router as grid_router
from routers.ml import router as ml_router
from routers.payments import router as payments_router
from routers.reports import router as reports_router
from routers.subscribers import router as subscribers_router
from routers.system import router as system_router
from routers.telegram import router as telegram_router
from routers.trade import router as trade_router

INTELLIGENCE_SCAN_LOCK = Lock()
INTELLIGENCE_PUBLISH_LOCK = Lock()

robot_task = None
robot_loop_enabled = True

subscription_task = None
subscription_loop_enabled = True

telegram_delivery_task = None
telegram_delivery_loop_enabled = True

payment_reconciliation_task = None
payment_reconciliation_loop_enabled = True

funding_arb_task = None
funding_arb_loop_enabled = True

grid_task = None
grid_loop_enabled = True

manage_task = None
manage_loop_enabled = True

orderbook_feed_task = None
orderbook_feed_enabled = True

digest_task = None
digest_loop_enabled = True
ml_retrain_task = None
ml_retrain_loop_enabled = True
# Сериализует ведение позиций между медленным SCAN и быстрым MANAGE циклами,
# чтобы одну позицию не обрабатывали два цикла одновременно.
position_manage_lock = asyncio.Lock()

logger = get_logger(__name__)


async def background_robot_loop():
    global robot_loop_enabled

    await asyncio.sleep(5)

    loop = RobotLoop()

    while robot_loop_enabled:
        db = SessionLocal()

        try:
            bot = db.query(Bot).filter(Bot.name == "Main Robot").first()

            if bot and bot.status == "running":
                validation_gates = ValidationGateService().live_blockers(db)

                if validation_gates.get("live_blockers"):
                    log_event(logger, logging.WARNING, "robot_loop_validation_skip", **validation_gates)
                else:
                    safety = LiveSafetyService().enforce(db=db, bot=bot, equity_usdt=1000)

                    if safety.get("blocked"):
                        db.commit()
                        log_event(logger, logging.WARNING, "robot_loop_safety_skip", **safety)
                    else:
                        async with position_manage_lock:
                            await loop.step(
                                db=db,
                                bot=bot,
                                headlines=[],
                                balance_usdt=1000,
                                daily_loss_pct=safety.get("daily_loss_pct", 0),
                                drawdown_pct=0,
                            )
                        ml_backfill = loop.ml_trade_logger.log_unlogged_closed_signals(db)
                        db.commit()
                        log_event(
                            logger,
                            logging.INFO,
                            "robot_loop_step_completed",
                            bot_id=bot.id,
                            mode=bot.mode,
                            daily_loss_pct=safety.get("daily_loss_pct", 0),
                            ml_outcomes_logged=ml_backfill.get("logged", 0),
                            ml_outcomes_path=ml_backfill.get("path"),
                        )

        except Exception as e:
            db.rollback()
            log_event(logger, logging.ERROR, "robot_loop_error", error_type=type(e).__name__, error=str(e))

        finally:
            db.close()

        await asyncio.sleep(int(getattr(settings, "SCAN_INTERVAL_SEC", 60)))


async def background_manage_loop():
    """Быстрый цикл ведения открытых позиций (выходы/скальп-замок/трейлы).

    Сканирование входов остаётся в background_robot_loop (медленно, по 4h-биасу),
    а этот цикл часто пересматривает уже открытые позиции по свежей цене, чтобы
    scalp_breakeven_lock и трейлы реагировали в секундах, а не раз в минуту.
    """
    global manage_loop_enabled

    await asyncio.sleep(8)

    loop = RobotLoop()

    while manage_loop_enabled:
        db = SessionLocal()
        try:
            bot = db.query(Bot).filter(Bot.name == "Main Robot").first()
            if bot and bot.status == "running":
                async with position_manage_lock:
                    await loop.lifecycle.process_open_signals(db, bot)
                    db.commit()
        except Exception as e:
            db.rollback()
            log_event(logger, logging.ERROR, "manage_loop_error", error_type=type(e).__name__, error=str(e))
        finally:
            db.close()

        await asyncio.sleep(int(getattr(settings, "MANAGE_INTERVAL_SEC", 10)))


async def background_digest_loop():
    """Периодическая короткая сводка состояния робота в Telegram (owner).

    Сеть Cowork не достаёт до Render API, поэтому дайджест собирается на стороне
    робота (БД + стакан в памяти) и шлётся через owner_alert. Первый прогон —
    вскоре после старта (подтверждение, что работает), далее раз в DIGEST_INTERVAL_SEC.
    """
    global digest_loop_enabled

    await asyncio.sleep(25)

    from services.digest_service import build_digest_text
    from services.telegram_router import TelegramRouter
    tg = TelegramRouter()

    while digest_loop_enabled:
        db = SessionLocal()
        try:
            if bool(getattr(settings, "ENABLE_DIGEST", True)):
                window_h = max(1, int(getattr(settings, "DIGEST_INTERVAL_SEC", 7200)) // 3600)
                text = build_digest_text(db, window_hours=window_h)
                await tg.owner_alert("ROBOT DIGEST", text)
        except Exception as e:
            log_event(logger, logging.ERROR, "digest_loop_error", error_type=type(e).__name__, error=str(e))
        finally:
            db.close()

        await asyncio.sleep(int(getattr(settings, "DIGEST_INTERVAL_SEC", 7200)))


async def background_ml_retrain_loop():
    """Ежесуточный авто-retrain мета-лейблера. Безопасно: ТОЛЬКО обучение на
    накопленных закрытых сделках, торговлю не трогает; при данных < min —
    honest skip. Держит модель свежей, чтобы при флипе ML_MODE она была готова.
    Telegram-алерт опционален (ML_TELEGRAM_ALERTS), чтобы не дублировать дайджест."""
    global ml_retrain_loop_enabled

    await asyncio.sleep(45)

    from services.ml_meta_labeler import MetaLabeler
    from services.telegram_router import TelegramRouter
    tg = TelegramRouter()

    while ml_retrain_loop_enabled:
        try:
            if bool(getattr(settings, "ML_AUTO_RETRAIN", True)):
                # sklearn-обучение — CPU-bound и СИНХРОННОЕ. В event loop оно бы
                # блокировало всё (вкл. pong WS-фида → разрыв 1003). Уносим в поток.
                res = await asyncio.to_thread(lambda: MetaLabeler().train())
                log_event(logger, logging.INFO, "ml_retrain",
                          status=res.get("status"), samples=res.get("samples"),
                          needed=res.get("needed"))
                if bool(getattr(settings, "ML_TELEGRAM_ALERTS", False)):
                    if res.get("status") == "trained":
                        m = res.get("metrics", {}) or {}
                        await tg.owner_alert(
                            "ML RETRAIN",
                            f"Обучено на {res.get('samples')} сделках · "
                            f"winrate {res.get('win_rate')}% · val_AUC {m.get('val_auc')}",
                        )
                    elif res.get("status") == "insufficient_data":
                        await tg.owner_alert(
                            "ML RETRAIN",
                            f"Пропуск: данных {res.get('samples')}/{res.get('needed')}",
                        )
        except Exception as e:
            log_event(logger, logging.ERROR, "ml_retrain_error",
                      error_type=type(e).__name__, error=str(e))

        await asyncio.sleep(int(getattr(settings, "ML_RETRAIN_INTERVAL_SEC", 86400)))


async def background_subscription_loop():
    global subscription_loop_enabled

    await asyncio.sleep(10)

    while subscription_loop_enabled:
        db = SessionLocal()

        try:
            service = SubscriptionWatchdog()
            result = await service.check_subscriptions(db)
            db.commit()
            log_event(logger, logging.INFO, "subscription_watchdog_check", **result)

        except Exception as e:
            db.rollback()
            log_event(logger, logging.ERROR, "subscription_loop_error", error_type=type(e).__name__, error=str(e))

        finally:
            db.close()

        await asyncio.sleep(60 * 60 * 6)


async def background_telegram_delivery_loop():
    global telegram_delivery_loop_enabled

    await asyncio.sleep(15)
    worker = TelegramDeliveryWorker()

    while telegram_delivery_loop_enabled:
        db = SessionLocal()

        try:
            result = await worker.process_once(db, limit=25)
            db.commit()

            if result.get("processed", 0) > 0:
                log_event(logger, logging.INFO, "telegram_delivery_retry", **result)

        except Exception as e:
            db.rollback()
            log_event(logger, logging.ERROR, "telegram_delivery_loop_error", error_type=type(e).__name__, error=str(e))

        finally:
            db.close()

        await asyncio.sleep(30)


async def background_payment_reconciliation_loop():
    global payment_reconciliation_loop_enabled

    await asyncio.sleep(20)
    service = PaymentReconciliationService()

    while payment_reconciliation_loop_enabled:
        db = SessionLocal()

        try:
            result = service.reconcile_pending(db, audit_log=AuditLogService())
            db.commit()

            if result.get("expired", 0) > 0:
                log_event(logger, logging.INFO, "payment_reconciliation", **result)

        except Exception as e:
            db.rollback()
            log_event(logger, logging.ERROR, "payment_reconciliation_loop_error", error_type=type(e).__name__, error=str(e))

        finally:
            db.close()

        await asyncio.sleep(60 * 60)


async def background_funding_arb_loop():
    global funding_arb_loop_enabled

    await asyncio.sleep(30)
    monitor = FundingMonitorService()
    arb_engine = FundingArbEngine(client=monitor.client)

    while funding_arb_loop_enabled:
        db = SessionLocal()

        try:
            if settings.ENABLE_FUNDING_ARB:
                # 1. Scan for new opportunities
                result = monitor.scan(db)
                candidates = [i for i in result.get("items", []) if i.get("status") == "candidate"]

                # 2. Auto-open paper positions for qualifying candidates
                auto_result = {"auto_open": False, "opened": [], "errors": []}
                if getattr(settings, "FUNDING_ARB_AUTO_OPEN_PAPER", True) and candidates:
                    auto_result = arb_engine.auto_open_candidates(db)

                # 3. Evaluate exits for open positions
                exit_result = arb_engine.evaluate_exits(db)
                db.commit()

                log_event(
                    logger,
                    logging.INFO,
                    "funding_arb_scan",
                    opportunities=len(result.get("items", [])),
                    candidates=len(candidates),
                    auto_opened=len(auto_result.get("opened", [])),
                    errors=len(result.get("errors", []))
                          + len(exit_result.get("errors", []))
                          + len(auto_result.get("errors", [])),
                    exits_closed=len(exit_result.get("closed", [])),
                    exits_required=len(exit_result.get("close_required", [])),
                )
            else:
                db.rollback()

        except Exception as e:
            db.rollback()
            log_event(logger, logging.ERROR, "funding_arb_loop_error", error_type=type(e).__name__, error=str(e))

        finally:
            db.close()

        await asyncio.sleep(monitor.scan_interval_seconds())


async def background_grid_loop():
    """Фоновый цикл умной сетки. Работает ПАРАЛЛЕЛЬНО тренду на свой карман маржи;
    тик только когда сетка включена (рантайм-флаг). Тренд-позиции/ордера не трогает.
    Тяжёлый расчёт (OHLCV/индикаторы) выносим в to_thread, чтобы не душить event loop."""
    global grid_loop_enabled

    await asyncio.sleep(35)
    try:
        from services.grid_engine import GridEngine
        engine = GridEngine()
    except Exception as e:  # noqa: BLE001
        log_event(logger, logging.ERROR, "grid_loop_init_error", error_type=type(e).__name__, error=str(e))
        return

    interval = float(getattr(settings, "GRID_TICK_INTERVAL_SEC", 20.0))
    while grid_loop_enabled:
        try:
            if engine.store.is_enabled():
                await asyncio.to_thread(engine.tick_all)
        except Exception as e:  # noqa: BLE001 — fail-open, цикл не падает
            log_event(logger, logging.ERROR, "grid_loop_error", error_type=type(e).__name__, error=str(e))
        await asyncio.sleep(interval)


class CloseSignalRequest(BaseModel):
    result_pct: float
    reason: str = "manual_close"

class TestLifecyclePriceRequest(BaseModel):
    signal_id: int
    price: float

class ExposureDebugRequest(BaseModel):
    symbol: str = "BTC/USDT"
    required_margin: float = 333.0


def initialize_database_schema():
    if settings.should_auto_create_schema:
        Base.metadata.create_all(bind=engine)
        ensure_telegram_delivery_schema()
        return

    log_event(
        logger,
        logging.INFO,
        "database_schema_auto_create_skipped",
        app_env=settings.APP_ENV,
        db_auto_create_schema=settings.DB_AUTO_CREATE_SCHEMA,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global robot_task, robot_loop_enabled, subscription_task, subscription_loop_enabled, telegram_delivery_task, telegram_delivery_loop_enabled, payment_reconciliation_task, payment_reconciliation_loop_enabled, funding_arb_task, funding_arb_loop_enabled, grid_task, grid_loop_enabled, manage_task, manage_loop_enabled, orderbook_feed_task, orderbook_feed_enabled, digest_task, digest_loop_enabled, ml_retrain_task, ml_retrain_loop_enabled

    initialize_database_schema()
    bootstrap_owner_and_bot()
    bootstrap_billing_plans()

    robot_loop_enabled = True
    robot_task = asyncio.create_task(background_robot_loop())

    manage_loop_enabled = True
    manage_task = asyncio.create_task(background_manage_loop())

    digest_loop_enabled = True
    digest_task = asyncio.create_task(background_digest_loop())

    ml_retrain_loop_enabled = True
    ml_retrain_task = asyncio.create_task(background_ml_retrain_loop())

    orderbook_feed_enabled = True
    if bool(getattr(settings, "ENABLE_ORDERBOOK_ENGINE", False)):
        try:
            from services.orderbook_feed import run_htx_orderbook_feed
            ob_db = SessionLocal()
            try:
                ob_bot = ob_db.query(Bot).filter(Bot.name == "Main Robot").first()
                ob_symbols = list((ob_bot.config_json or {}).get("symbols", [])) if ob_bot else []
            finally:
                ob_db.close()
            if not ob_symbols:
                ob_symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT", "LINK/USDT", "AVAX/USDT", "DOT/USDT"]
            orderbook_feed_task = asyncio.create_task(
                run_htx_orderbook_feed(ob_symbols, lambda: orderbook_feed_enabled)
            )
        except Exception as e:
            log_event(logger, logging.ERROR, "orderbook_feed_start_error", error_type=type(e).__name__, error=str(e))

    subscription_loop_enabled = True
    subscription_task = asyncio.create_task(background_subscription_loop())

    telegram_delivery_loop_enabled = True
    telegram_delivery_task = asyncio.create_task(background_telegram_delivery_loop())

    payment_reconciliation_loop_enabled = True

    payment_reconciliation_task = asyncio.create_task(background_payment_reconciliation_loop())

    funding_arb_loop_enabled = True
    funding_arb_task = asyncio.create_task(background_funding_arb_loop())

    grid_loop_enabled = True
    grid_task = asyncio.create_task(background_grid_loop())

    yield

    robot_loop_enabled = False
    if robot_task:
        robot_task.cancel()

    manage_loop_enabled = False
    if manage_task:
        manage_task.cancel()

    digest_loop_enabled = False
    if digest_task:
        digest_task.cancel()

    orderbook_feed_enabled = False
    if orderbook_feed_task:
        orderbook_feed_task.cancel()

    subscription_loop_enabled = False
    if subscription_task:
        subscription_task.cancel()

    telegram_delivery_loop_enabled = False
    if telegram_delivery_task:
        telegram_delivery_task.cancel()

    payment_reconciliation_loop_enabled = False
    if payment_reconciliation_task:
        payment_reconciliation_task.cancel()

    funding_arb_loop_enabled = False
    if funding_arb_task:
        funding_arb_task.cancel()

    grid_loop_enabled = False
    if grid_task:
        grid_task.cancel()


app = FastAPI(title="Robot V1 API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Domain routers ───────────────────────────────────────────────────────────
app.include_router(analytics_router)
app.include_router(audit_router)
app.include_router(funding_arb_router)
app.include_router(grid_router)
app.include_router(ml_router)
app.include_router(payments_router)
app.include_router(reports_router)
app.include_router(subscribers_router)
app.include_router(system_router)
app.include_router(telegram_router)
app.include_router(trade_router)



@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid4().hex
    started = time.perf_counter()

    try:
        response = await call_next(request)
    except Exception as exc:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        log_event(
            logger,
            logging.ERROR,
            "request_error",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            duration_ms=duration_ms,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise

    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    log_event(
        logger,
        logging.INFO,
        "request_completed",
        request_id=request_id,
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        duration_ms=duration_ms,
    )
    return response


def bootstrap_billing_plans():
    db: Session = SessionLocal()

    try:
        BillingService().ensure_default_plans(db)
        db.commit()
    finally:
        db.close()


def bootstrap_owner_and_bot():
    db: Session = SessionLocal()

    try:
        owner = db.query(User).filter(User.email == settings.OWNER_EMAIL).first()

        if not owner:
            owner = User(
                email=settings.OWNER_EMAIL,
                password_hash=hash_password(settings.OWNER_PASSWORD),
                is_active=True,
            )
            db.add(owner)
            db.flush()

        bot = db.query(Bot).filter(
            Bot.user_id == owner.id,
            Bot.name == "Main Robot"
        ).first()

        if not bot:
            bot = Bot(
                user_id=owner.id,
                name="Main Robot",
                status="stopped",
                mode=settings.ROBOT_MODE,
                config_json={
                    "symbols": settings.symbols,
                    "risk_profile": "balanced",
                    "telegram_signals": True,
                    "news_filter": True,
                },
            )
            db.add(bot)
        else:
            cfg = dict(bot.config_json or {})

            # .env теперь главный источник списка инструментов.
            cfg["symbols"] = settings.symbols

            # Сохраняем/восстанавливаем остальные настройки.
            cfg.setdefault("risk_profile", "balanced")
            cfg.setdefault("telegram_signals", True)
            cfg.setdefault("news_filter", True)

            bot.config_json = cfg
            bot.mode = settings.ROBOT_MODE

        db.commit()

    finally:
        db.close()

@app.get("/")
def root():
    return {
        "service": "Robot V1 API",
        "status": "ok",
        "mode": settings.ROBOT_MODE,
    }


@app.get("/health")
def health():
    return {
        "api": "ok",
        "env": settings.APP_ENV,
        "exit_policy": ExitPolicyService.runtime_guard(),
    }


@app.get("/bot/state", dependencies=[Depends(require_owner_action)])
def bot_state():
    db = SessionLocal()

    try:
        bot = db.query(Bot).filter(Bot.name == "Main Robot").first()
        signals_count = db.query(Signal).count()
        positions_count = db.query(Position).filter(Position.status == "open").count()
        orders_count = db.query(Order).count()

        return {
            "bot": {
                "id": bot.id,
                "name": bot.name,
                "status": bot.status,
                "mode": bot.mode,
                "config": bot.config_json,
            },
            "signals_count": signals_count,
            "open_positions": positions_count,
            "orders_count": orders_count,
        }

    finally:
        db.close()


@app.post("/bot/start", dependencies=[Depends(require_owner_action)])
def start_bot():
    db = SessionLocal()

    try:
        bot = db.query(Bot).filter(Bot.name == "Main Robot").first()
        if not bot:
            return {"status": "error", "error": "bot_not_found"}

        live_safety = LiveSafetyService().snapshot(db=db, bot=bot)
        validation_gates = ValidationGateService().live_blockers(db)
        if validation_gates.get("live_blockers"):
            AuditLogService().record(
                db,
                action="bot_start_blocked_by_validation_gates",
                resource_type="bot",
                resource_id=bot.id,
                status="blocked",
                details=validation_gates,
            )
            db.commit()
            return {"status": "blocked", "reason": "validation_gates_blocked", "validation_gates": validation_gates}

        if live_safety.get("blocked"):
            AuditLogService().record(
                db,
                action="bot_start_blocked_by_live_safety",
                resource_type="bot",
                resource_id=bot.id,
                status="blocked",
                details=live_safety,
            )
            db.commit()
            return {"status": "blocked", "reason": "live_safety_blocked", "live_safety": live_safety}

        bot.status = "running"
        AuditLogService().record(db, action="bot_start", resource_type="bot", resource_id=bot.id, details={"name": bot.name})
        db.commit()
        return {"status": "running", "live_safety": live_safety, "validation_gates": validation_gates}

    finally:
        db.close()


@app.post("/bot/stop", dependencies=[Depends(require_owner_action)])
def stop_bot():
    db = SessionLocal()

    try:
        bot = db.query(Bot).filter(Bot.name == "Main Robot").first()
        bot.status = "stopped"
        AuditLogService().record(db, action="bot_stop", resource_type="bot", resource_id=bot.id, details={"name": bot.name})
        db.commit()
        return {"status": "stopped"}

    finally:
        db.close()


@app.get("/orderbook/state", dependencies=[Depends(require_owner_action)])
def orderbook_state():
    """Живой снимок стакана по символам: spread / OBI / стенки / CVD / возраст
    данных. Для контроля, что WS-фид жив, и подбора порогов depth-движка."""
    from services.orderbook_feed import ORDERBOOK_STORE
    from services.orderbook_analyzer import OrderBookAnalyzer

    levels = int(getattr(settings, "OB_DEPTH_LEVELS", 10))
    max_age = float(getattr(settings, "OB_DATA_MAX_AGE_SEC", 15.0))
    stats = ORDERBOOK_STORE.stats()

    symbols = {}
    for sym in stats.get("symbols", []):
        snap = ORDERBOOK_STORE.snapshot(sym)
        sig = OrderBookAnalyzer.analyze(snap, levels=levels)
        row = sig.as_dict()
        age = snap.get("age_sec") if snap else None
        row["age_sec"] = round(age, 2) if age is not None else None
        row["stale"] = (snap is None) or (age is not None and age > max_age)
        symbols[sym] = row

    return {
        "enabled": bool(getattr(settings, "ENABLE_ORDERBOOK_ENGINE", False)),
        "gate_entries": bool(getattr(settings, "OB_GATE_ENTRIES", True)),
        "accelerate_exits": bool(getattr(settings, "OB_ACCELERATE_EXITS", True)),
        "thresholds": {
            "max_spread_pct": getattr(settings, "OB_MAX_SPREAD_PCT", 0.08),
            "obi_confirm": getattr(settings, "OB_OBI_CONFIRM", 0.15),
            "wall_confirm_share": getattr(settings, "OB_WALL_CONFIRM_SHARE", 0.30),
            "data_max_age_sec": max_age,
            "cvd_exit_ratio": getattr(settings, "OB_CVD_EXIT_RATIO", 0.6),
        },
        "stats": stats,
        "symbols": symbols,
    }


@app.get("/liquidity/state", dependencies=[Depends(require_owner_action)])
def liquidity_state():
    """LiquidityGuard: текущий спред vs скользящая база по символам (bps), и
    активные пороги. Единый адаптивный спред-гард для trend/grid/funding/ML."""
    from services.liquidity_guard import LIQUIDITY_GUARD
    return {
        "enabled": bool(getattr(settings, "LIQUIDITY_GUARD_ENABLED", True)),
        "block_entry": bool(getattr(settings, "LIQ_BLOCK_ENTRY", True)),
        "protect_exit": bool(getattr(settings, "LIQ_PROTECT_EXIT", True)),
        "thresholds": {
            "abs_max_bps": getattr(settings, "LIQ_SPREAD_ABS_MAX_BPS", 25.0),
            "entry_mult": getattr(settings, "LIQ_SPREAD_BASELINE_MULT", 3.0),
            "exit_mult": getattr(settings, "LIQ_EXIT_SPREAD_MULT", 4.0),
            "baseline_alpha": getattr(settings, "LIQ_SPREAD_BASELINE_ALPHA", 0.05),
            "min_baseline_bps": getattr(settings, "LIQ_SPREAD_MIN_BASELINE_BPS", 1.0),
        },
        "symbols": LIQUIDITY_GUARD.snapshot(),
    }


@app.get("/live/state", dependencies=[Depends(require_owner_action)])
def live_state():
    """Готовность к Live: режим исполнения, предохранители, баланс. Показывает,
    что реальные ордера уходят ТОЛЬКО при ENABLE_LIVE_ORDERS=true И mode=live."""
    from services.live_executor import LIVE_EXECUTOR
    eff = LIVE_EXECUTOR.effective_mode()
    out = {
        "configured_mode": LIVE_EXECUTOR.configured_mode(),
        "effective_mode": eff,
        "is_live": eff == "live",
        "enable_live_orders": bool(getattr(settings, "ENABLE_LIVE_ORDERS", False)),
        "robot_mode": getattr(settings, "ROBOT_MODE", "paper"),
        "trading_mode": getattr(settings, "TRADING_MODE", "paper_trade"),
        "execution_market": settings.execution_market_type,
        "by_engine": {
            "trend": {"leverage": settings.execution_leverage,
                      "margin_mode": getattr(settings, "TREND_MARGIN_MODE", "isolated")},
            "grid": {"leverage": getattr(settings, "GRID_LEVERAGE", 1.0),
                     "margin_mode": settings.grid_effective_margin_mode,
                     "margin_mode_base": getattr(settings, "GRID_MARGIN_MODE", "isolated"),
                     "auto_cross_on_leverage": settings.grid_effective_margin_mode != getattr(settings, "GRID_MARGIN_MODE", "isolated"),
                     "advisory": ("плечо>1x → cross; рекомендуется выделенный субсчёт под сетку"
                                  if float(getattr(settings, "GRID_LEVERAGE", 1.0)) > float(getattr(settings, "GRID_MARGIN_ISOLATED_MAX_LEV", 1.0))
                                  else None)},
            "funding_swap": {"leverage": getattr(settings, "FUNDING_LEVERAGE", 2),
                             "margin_mode": getattr(settings, "FUNDING_MARGIN_MODE", "cross")},
            "max_leverage_cap": getattr(settings, "LIVE_MAX_LEVERAGE", 5.0),
        },
        "safety": {
            "set_leverage": bool(getattr(settings, "LIVE_SET_LEVERAGE", True)),
            "margin_mode_default": getattr(settings, "LIVE_MARGIN_MODE", "cross"),
            "max_order_notional_usdt": getattr(settings, "LIVE_MAX_ORDER_NOTIONAL_USDT", 0.0),
            "size_from_balance": bool(getattr(settings, "LIVE_SIZE_FROM_BALANCE", True)),
            "fill_poll_timeout_sec": getattr(settings, "LIVE_FILL_POLL_TIMEOUT_SEC", 10.0),
        },
        "validation_blockers": settings.production_blockers() if hasattr(settings, "production_blockers") else None,
    }
    if eff == "live":
        out["free_balance_usdt"] = {
            "spot": LIVE_EXECUTOR.free_usdt("spot"),
            "swap": LIVE_EXECUTOR.free_usdt("swap"),
        }
        out["sizing_equity_usdt"] = LIVE_EXECUTOR.effective_equity_usdt(settings.execution_market_type)
    return out


@app.get("/orderbook/volume-profile", dependencies=[Depends(require_owner_action)])
def orderbook_volume_profile(symbol: str = "BTC/USDT", timeframe: str = "1h",
                             limit: int = 1000, bins: int = 50):
    """Volume Profile из OHLCV: VPOC, value area (VAH/VAL), HVN/LVN-уровни.
    Для выбора уровней TP/стоп (HVN=реакция, LVN=быстрая зона), не для прогноза."""
    from services.volume_profile import compute_volume_profile
    return compute_volume_profile(symbol=symbol, timeframe=timeframe,
                                  limit=int(limit), bins=int(bins))


@app.get("/ml/outcomes/stats", dependencies=[Depends(require_owner_action)])
def ml_outcomes_stats():
    """Статистика ML-датасета (trade_outcomes.jsonl на персистентном диске):
    сколько строк, последняя запись, баланс win/loss, есть ли depth-фичи."""
    import json as _json
    from services.ml_trade_logger import MLTradeLogger

    p = MLTradeLogger().path
    if not p.exists():
        return {"path": str(p), "exists": False, "count": 0, "last_logged_at": None}

    count = wins = losses = with_depth = with_regime = 0
    last_logged_at = last_symbol = last_reason = None
    for line in p.open("r", encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            d = _json.loads(line)
        except Exception:
            continue
        count += 1
        lab = d.get("labels") or {}
        if lab.get("is_win"):
            wins += 1
        elif lab.get("is_loss"):
            losses += 1
        if d.get("entry_depth"):
            with_depth += 1
        if d.get("regime"):
            with_regime += 1
        last_logged_at = d.get("logged_at") or last_logged_at
        last_symbol = d.get("symbol") or last_symbol
        last_reason = d.get("closed_reason") or last_reason

    return {
        "path": str(p),
        "exists": True,
        "count": count,
        "wins": wins,
        "losses": losses,
        "winrate_pct": round(wins / count * 100, 2) if count else 0.0,
        "with_entry_depth": with_depth,
        "with_regime": with_regime,
        "last_logged_at": last_logged_at,
        "last_symbol": last_symbol,
        "last_reason": last_reason,
        "size_bytes": p.stat().st_size,
        "target_for_training": 200,
    }


@app.get("/ml/status", dependencies=[Depends(require_owner_action)])
def ml_status():
    """Статус ML-слоя: режим (off/shadow/advisory/full_auto), готовность модели,
    метрики валидации. Для фронта-пульта и контроля."""
    from services.ml_meta_labeler import MetaLabeler
    status = MetaLabeler().status()
    return {
        "ml_mode": str(getattr(settings, "ML_MODE", "off")).lower(),
        "min_score_to_trade": float(getattr(settings, "ML_MIN_SCORE_TO_TRADE", 0.45)),
        "size_mult_range": [float(getattr(settings, "ML_SIZE_MULT_MIN", 0.7)),
                            float(getattr(settings, "ML_SIZE_MULT_MAX", 1.25))],
        "model": status,
    }


@app.get("/ml/shadow-report", dependencies=[Depends(require_owner_action)])
def ml_shadow_report():
    """Shadow-валидация: предсказанный ml_score vs РЕАЛЬНЫЙ исход на закрытых
    сделках. Калибровка по бакетам + live-AUC + эффект порога. На сделки НЕ влияет.
    Пусто, пока ML_MODE=shadow не накопит закрытий с ml_score."""
    from services.ml_shadow_report import build as _shadow_build
    db = SessionLocal()
    try:
        return _shadow_build(db)
    finally:
        db.close()


@app.get("/ml/features/analysis", dependencies=[Depends(require_owner_action)])
def ml_feature_analysis():
    """Дешёвый descriptive-тест: какие фичи (вкл. стакан OBI/CVD) разделяют
    win/loss на накопленных сделках. Работает до полного обучения мета-лейблера."""
    from services.ml_meta_labeler import MetaLabeler
    return MetaLabeler().feature_analysis()


@app.post("/ml/train", dependencies=[Depends(require_owner_action)])
def ml_train():
    """Переобучить мета-лейблер на trade_outcomes.jsonl (time-aware валидация).
    Безопасно: не трогает торговлю; при нехватке данных вернёт honest-статус."""
    from services.ml_meta_labeler import MetaLabeler
    return MetaLabeler().train()


@app.post("/ml/predict", dependencies=[Depends(require_owner_action)])
def ml_predict(candidate: dict):
    """Отладочный predict: P(win) для переданного кандидата (confidence/grade/
    side/regime/net_rr_tp1/net_rr_tp2/entry_depth)."""
    from services.ml_controller import MLController
    return MLController().evaluate_candidate(candidate or {})


@app.get("/ml/research/market", dependencies=[Depends(require_owner_action)])
def ml_research_market(symbol: str = "BTC/USDT", timeframe: str = "1h",
                       limit: int = 1500, horizon: int = 24, k_atr: float = 1.5):
    """ИССЛЕДОВАНИЕ (не торговля): предсказуемы ли движения по OHLC-фичам?
    Тянет историю, строит фичи + triple-barrier метку, делает walk-forward
    оценку и честный вердикт (edge_found / weak / no_edge_after_costs).
    Это ответ ДАННЫМИ на «насколько OHLC реально поможет»."""
    from services.ml_market_research import evaluate
    return evaluate(symbol=symbol, timeframe=timeframe, limit=int(limit),
                    horizon=int(horizon), k_atr=float(k_atr))


@app.get("/ml/research/scan", dependencies=[Depends(require_owner_action)])
def ml_research_scan(timeframe: str = "1h", limit: int = 1500,
                     horizon: int = 24, k_atr: float = 1.5):
    """Прогон research по ВСЕМ символам бота — одна сводка-таблица вместо 7 вызовов.
    Может занять 10–30с (тянет историю + обучает по 2 модели на символ)."""
    from services.ml_market_research import evaluate
    out = []
    for sym in settings.symbols:
        try:
            r = evaluate(symbol=sym, timeframe=timeframe, limit=int(limit),
                         horizon=int(horizon), k_atr=float(k_atr))
        except Exception as exc:
            r = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        models = r.get("models", {}) if isinstance(r, dict) else {}
        lg = models.get("logreg") or {}
        out.append({
            "symbol": sym,
            "status": r.get("status"),
            "verdict": r.get("verdict"),
            # logreg — основной (устойчив к переобучению); gbm для сравнения
            "logreg_mean_auc": lg.get("mean_auc"),
            "logreg_std_auc": lg.get("std_auc"),
            "logreg_mean_exp_atr": lg.get("mean_expectancy_atr"),
            "folds_positive": lg.get("folds_positive"),
            "folds": lg.get("folds"),
            "gbm_mean_auc": (models.get("gbm") or {}).get("mean_auc"),
            "labeled": r.get("labeled_samples"),
            "baseline_up_rate": r.get("baseline_up_rate"),
        })
    return {"timeframe": timeframe, "horizon": horizon, "k_atr": k_atr, "results": out}


@app.get("/signals", dependencies=[Depends(require_owner_action)])
def list_signals(limit: int = 50, offset: int = 0):
    db = SessionLocal()

    try:
        limit = min(max(limit, 1), 200)
        offset = max(offset, 0)

        query = db.query(Signal)
        total = query.count()

        signals = (
            query
            .order_by(Signal.id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [
                {
                    "id": s.id,
                    "symbol": s.symbol,
                    "side": s.side,
                    "status": s.status,
                    "entry_zone": s.entry_zone_json,
                    "stop_price": s.stop_price,
                    "tp": s.tp_json,
                    "confidence": s.confidence,
                    "grade": s.grade,
                    "is_public": s.is_public,
                    "expires_at": str(s.expires_at) if s.expires_at else None,
                    "rationale": s.rationale,
                    "result_pct": s.result_pct,
                    "created_at": str(s.created_at),

                    "qty": s.qty,
                    "required_margin": s.required_margin,
                    "net_rr_tp1": s.net_rr_tp1,
                    "net_rr_tp2": s.net_rr_tp2,
                    "net_pnl_tp1": s.net_pnl_tp1,
                    "net_pnl_tp2": s.net_pnl_tp2,
                    "net_pnl_stop": s.net_pnl_stop,
                    "plan": s.plan_json,

                    "closed_exit_price": s.closed_exit_price,
                    "closed_net_pnl": s.closed_net_pnl,
                    "closed_total_cost": s.closed_total_cost,
                    "closed_reason": s.closed_reason,
                }
                for s in signals
            ],
        }

    finally:
        db.close()


@app.get("/positions", dependencies=[Depends(require_owner_action)])
def list_positions(limit: int = 500):
    db = SessionLocal()

    try:
        # Лимит поднят 50→500: на 50 фронт-сводка по позициям недосчитывала PnL/Closed
        # и расходилась с analytics/summary. 500 покрывает всю историю paper.
        limit = max(1, min(int(limit), 2000))
        positions = db.query(Position).order_by(Position.id.desc()).limit(limit).all()

        # (#audit-positions) У закрытой позиции unrealized_pnl = 0; реализованный
        # результат отдаём отдельным полем realized_pnl из Signal.closed_net_pnl
        # (покрывает и старые строки, где unrealized хранил net закрытия).
        signal_ids = [p.signal_id for p in positions if p.signal_id is not None]
        realized_map: dict = {}
        if signal_ids:
            rows = (
                db.query(Signal.id, Signal.closed_net_pnl)
                .filter(Signal.id.in_(signal_ids))
                .all()
            )
            realized_map = {sid: pnl for sid, pnl in rows}

        return [
            {
                "id": p.id,
                "symbol": p.symbol,
                "side": p.side,
                "qty": p.qty,
                "entry_price": p.entry_price,
                "mark_price": p.mark_price,
                "unrealized_pnl": p.unrealized_pnl,
                "realized_pnl": realized_map.get(p.signal_id) if str(p.status or "").lower() == "closed" else None,
                "status": p.status,
                "signal_id": p.signal_id,
            }
            for p in positions
        ]

    finally:
        db.close()


@app.get("/orders", dependencies=[Depends(require_owner_action)])
def list_orders():
    db = SessionLocal()

    try:
        orders = db.query(Order).order_by(Order.id.desc()).limit(50).all()

        return [
            {
                "id": o.id,
                "symbol": o.symbol,
                "side": o.side,
                "type": o.order_type,
                "status": o.status,
                "qty": o.qty,
                "price": o.price,
                "filled_qty": o.filled_qty,
                "avg_fill_price": o.avg_fill_price,
                "client_order_id": o.client_order_id,
                "exchange_order_id": o.exchange_order_id,
            }
            for o in orders
        ]

    finally:
        db.close()


@app.post("/robot/run-once", dependencies=[Depends(require_owner_action)])
async def run_robot_once():
    db = SessionLocal()

    try:
        bot = db.query(Bot).filter(Bot.name == "Main Robot").first()

        if not bot:
            return {"status": "skipped", "reason": "bot_not_found"}
        if bot.status != "running":
            return {"status": "skipped", "reason": "bot_stopped"}

        validation_gates = ValidationGateService().live_blockers(db)
        if validation_gates.get("live_blockers"):
            return {"status": "skipped", "reason": "validation_gates_blocked", "validation_gates": validation_gates}

        safety = LiveSafetyService().enforce(db=db, bot=bot, equity_usdt=1000)
        if safety.get("blocked"):
            db.commit()
            return {"status": "skipped", "reason": "live_safety_blocked", "live_safety": safety}

        loop = RobotLoop()

        await loop.step(
            db=db,
            bot=bot,
            headlines=[],
            balance_usdt=1000,
            daily_loss_pct=safety.get("daily_loss_pct", 0),
            drawdown_pct=0,
        )

        db.commit()

        return {"status": "ok"}

    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}

    finally:
        db.close()

@app.get("/robot/loop-state", dependencies=[Depends(require_owner_action)])
def robot_loop_state():
    return {
        "robot_loop": {
            "enabled": robot_loop_enabled,
            "task_created": robot_task is not None,
            "task_done": robot_task.done() if robot_task else None,
        },
        "subscription_loop": {
            "enabled": subscription_loop_enabled,
            "task_created": subscription_task is not None,
            "task_done": subscription_task.done() if subscription_task else None,
        },
        "telegram_delivery_loop": {
            "enabled": telegram_delivery_loop_enabled,
            "task_created": telegram_delivery_task is not None,
            "task_done": telegram_delivery_task.done() if telegram_delivery_task else None,
        },
    }



@app.get("/robot/debug-signals", dependencies=[Depends(require_owner_action), Depends(require_non_production_debug)])
def debug_signals():
    db = SessionLocal()

    try:
        bot = db.query(Bot).filter(Bot.name == "Main Robot").first()

        market = MarketDataService()
        strategy = StrategyEngine()
        ml = MLScorer()

        result = []

        for symbol in bot.config_json.get("symbols", []):
            snap = market.snapshot(symbol)
            df = snap["ohlcv"]

            regime = strategy.detect_regime(df)
            features = strategy.build_features(df)
            signal = strategy.generate_signal(symbol, features, regime)
            ml_score = ml.score(features, regime)

            result.append({
                "symbol": symbol,
                "last": snap["last"],
                "source": snap.get("source"),
                "regime": regime,
                "features": features,
                "signal": signal,
                "ml": {
                    "probability": ml_score.probability,
                    "confidence": ml_score.confidence,
                    "multiplier": ml_score.multiplier,
                }
            })

        return result

    finally:
        db.close()

@app.post("/robot/force-paper-signal", dependencies=[Depends(require_owner_action), Depends(require_non_production_debug)])
async def force_paper_signal():
    db = SessionLocal()

    try:
        bot = db.query(Bot).filter(Bot.name == "Main Robot").first()

        test_signal = {
            "action": "long",
            "symbol": "BTC/USDT",
            "entry_zone": [64000, 64200],
            "stop_price": 63500,
            "tp": {
                "tp1": 64800,
                "tp2": 65500,
            },
            "reason": "forced_test_signal",
        }

        quality = SignalQualityService()
        grade = quality.grade(88.0, test_signal["reason"])
        expires_at = quality.expiry_time(grade)

        sig = Signal(
            bot_id=bot.id,
            symbol=test_signal["symbol"],
            side=test_signal["action"],
            status="published",
            entry_zone_json={"from": test_signal["entry_zone"][0], "to": test_signal["entry_zone"][1]},
            stop_price=test_signal["stop_price"],
            tp_json=test_signal["tp"],
            confidence=88.0,
            grade=grade,
            is_public=quality.should_publish_to_clients(grade),
            expires_at=expires_at,
            rationale=test_signal["reason"],
        )

        db.add(sig)
        db.commit()

        router = TelegramRouter()
        await router.publish_new_signal(
            signal=test_signal,
            confidence=88.0,
            grade=grade,
            signal_id=sig.id
        )

        return {"status": "created_and_sent", "signal_id": sig.id}

    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}

    finally:
        db.close()












ACTIVE_CLOSE_STATUSES = ["opened", "tp1", "breakeven"]


async def _manual_close_via_lifecycle(db, sig: Signal, exit_price: float, reason: str) -> dict:
    """(#manual-close-2026-07-09) ПОЛНОЕ ручное закрытие через lifecycle:
    закрывает Position (раньше легаси-эндпоинт лишь ставил status=closed, и
    позиция с маржой оставались висеть), считает net PnL через CostEngine,
    учитывает TP1-partial, пишет ML-метку и шлёт Telegram-уведомление."""
    manager = SignalLifecycleManager()
    entry_price = manager._get_signal_entry_price(db, sig) or float(
        (sig.entry_zone_json or {}).get("from") or 0.0
    )
    await manager._close_signal(
        db,
        sig,
        exit_price=float(exit_price),
        fallback_result_pct=manager._result_pct(sig.side.lower(), float(entry_price), float(exit_price)),
        reason=reason,
    )
    db.commit()
    return {
        "status": "closed",
        "signal_id": sig.id,
        "exit_price": float(exit_price),
        "result_pct": sig.result_pct,
        "net_pnl": sig.closed_net_pnl,
        "reason": sig.closed_reason,
    }


@app.post("/signals/{signal_id}/close", dependencies=[Depends(require_owner_action)])
async def close_signal(signal_id: int, payload: CloseSignalRequest):
    """(#manual-close-2026-07-09) Ручное закрытие с заданным result_pct.
    Раньше — заглушка: ставила status=closed, НЕ закрывая позицию (осиротевшая
    маржа) и не считая PnL. Теперь цена выводится из result_pct и закрытие идёт
    полным lifecycle-путём."""
    db = SessionLocal()
    try:
        sig = db.query(Signal).filter(Signal.id == signal_id).first()
        if not sig:
            return {"status": "error", "error": "signal_not_found"}
        if sig.status == "published":
            sig.status = "expired"
            sig.closed_reason = payload.reason or "manual_cancel"
            db.commit()
            return {"status": "expired", "signal_id": sig.id}
        if sig.status not in ACTIVE_CLOSE_STATUSES:
            return {"status": "error", "error": f"signal_not_active:{sig.status}"}

        manager = SignalLifecycleManager()
        entry_price = manager._get_signal_entry_price(db, sig) or float(
            (sig.entry_zone_json or {}).get("from") or 0.0
        )
        if entry_price <= 0:
            return {"status": "error", "error": "entry_price_unknown"}
        pct = float(payload.result_pct) / 100.0
        exit_price = entry_price * (1 + pct) if sig.side.lower() == "long" else entry_price * (1 - pct)
        return await _manual_close_via_lifecycle(db, sig, exit_price, payload.reason or "manual_close")
    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        db.close()


@app.post("/signals/{signal_id}/close-market", dependencies=[Depends(require_owner_action)])
async def close_signal_market(signal_id: int):
    """(#manual-close-2026-07-09) Ручное закрытие ПО РЫНКУ: живая цена символа,
    полный lifecycle-путь (позиция, PnL с издержками, TP1-partial, ML-метка,
    Telegram). Работает в production — в отличие от debug-кнопок test-lifecycle."""
    db = SessionLocal()
    try:
        sig = db.query(Signal).filter(Signal.id == signal_id).first()
        if not sig:
            return {"status": "error", "error": "signal_not_found"}
        if sig.status == "published":
            sig.status = "expired"
            sig.closed_reason = "manual_cancel"
            db.commit()
            return {"status": "expired", "signal_id": sig.id}
        if sig.status not in ACTIVE_CLOSE_STATUSES:
            return {"status": "error", "error": f"signal_not_active:{sig.status}"}

        snap = MarketDataService().snapshot(sig.symbol)
        price = float(snap.get("last") or 0.0)
        if price <= 0:
            return {"status": "error", "error": "market_price_unavailable"}
        return await _manual_close_via_lifecycle(db, sig, price, "manual_close")
    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        db.close()

@app.post("/robot/force-live-near-signal", dependencies=[Depends(require_owner_action), Depends(require_non_production_debug)])
async def force_live_near_signal():
    db = SessionLocal()

    try:
        bot = db.query(Bot).filter(Bot.name == "Main Robot").first()

        market = MarketDataService()
        snap = market.snapshot("BTC/USDT")
        price = float(snap["last"])

        entry_from = round(price * 0.9995, 2)
        entry_to = round(price * 1.0005, 2)

        stop = round(price * 0.995, 2)
        tp1 = round(price * 1.003, 2)
        tp2 = round(price * 1.006, 2)

        test_signal = {
            "action": "long",
            "symbol": "BTC/USDT",
            "entry_zone": [entry_from, entry_to],
            "stop_price": stop,
            "tp": {
                "tp1": tp1,
                "tp2": tp2,
            },
            "reason": "forced_live_near_signal",
        }

        quality = SignalQualityService()
        grade = quality.grade(90.0, test_signal["reason"])
        expires_at = quality.expiry_time(grade)

        sig = Signal(
            bot_id=bot.id,
            symbol=test_signal["symbol"],
            side=test_signal["action"],
            status="published",
            entry_zone_json={"from": entry_from, "to": entry_to},
            stop_price=stop,
            tp_json=test_signal["tp"],
            confidence=90.0,
            rationale=test_signal["reason"],
            grade=grade,
            is_public=quality.should_publish_to_clients(grade),
            expires_at=expires_at,
        )

        db.add(sig)
        db.commit()

        router = TelegramRouter()
        await router.publish_new_signal(
            signal=test_signal,
            confidence=90.0,
            grade=grade,
            signal_id=sig.id
        )

        return {
            "status": "created_and_sent",
            "signal_id": sig.id,
            "price": price,
            "entry": [entry_from, entry_to],
            "stop": stop,
            "tp": test_signal["tp"],
        }

    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}

    finally:
        db.close()

@app.post("/signals/maintenance/queued-to-published", dependencies=[Depends(require_owner_action)])
def queued_to_published():
    db = SessionLocal()

    try:
        updated = (
            db.query(Signal)
            .filter(Signal.status == "queued")
            .update({"status": "published"})
        )

        db.commit()

        return {
            "status": "ok",
            "updated": updated,
        }

    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}

    finally:
        db.close()

@app.post("/robot/run-lifecycle-once", dependencies=[Depends(require_owner_action)])
async def run_lifecycle_once():
    db = SessionLocal()

    try:
        bot = db.query(Bot).filter(Bot.name == "Main Robot").first()

        manager = SignalLifecycleManager()
        await manager.process_open_signals(db, bot)

        db.commit()

        return {"status": "ok"}

    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}

    finally:
        db.close()

@app.post("/robot/force-scalp-signal", dependencies=[Depends(require_owner_action), Depends(require_non_production_debug)])
async def force_scalp_signal():
    db = SessionLocal()

    try:
        bot = db.query(Bot).filter(Bot.name == "Main Robot").first()

        if not bot:
            return {"status": "error", "error": "bot_not_found"}

        market = MarketDataService()
        snap = market.snapshot("BTC/USDT")
        price = float(snap["last"])

        entry_from = round(price * 0.9995, 2)
        entry_to = round(price * 1.0005, 2)
        entry_price = round((entry_from + entry_to) / 2, 2)

        stop = round(price * 0.992, 2)
        tp1 = round(price * 1.006, 2)
        tp2 = round(price * 1.012, 2)

        test_signal = {
            "action": "long",
            "symbol": "BTC/USDT",
            "entry_zone": [entry_from, entry_to],
            "stop_price": stop,
            "tp": {
                "tp1": tp1,
                "tp2": tp2,
            },
            "reason": "forced_scalp_lifecycle_test_net_positive",
        }

        quality = SignalQualityService()
        grade = quality.grade(91.0, test_signal["reason"])
        expires_at = quality.expiry_time(grade)

        builder = TradePlanBuilder()
        plan = builder.build_plan(
            symbol=test_signal["symbol"],
            side=test_signal["action"],
            entry_price=entry_price,
            stop_price=stop,
            tp1=tp1,
            tp2=tp2,
            balance_usdt=1000.0,
        )

        if not plan.is_valid:
            await TelegramRouter().owner_alert(
                "SIGNAL REJECTED BEFORE PUBLISH",
                (
                    f"{test_signal['symbol']} {test_signal['action']}\n"
                    f"Reason: {plan.reject_reason}\n"
                    f"Entry: {entry_price}\n"
                    f"Stop: {stop}\n"
                    f"TP1: {tp1}\n"
                    f"TP2: {tp2}\n"
                    f"Net TP1: {plan.net_pnl_tp1} USDT\n"
                    f"Net TP2: {plan.net_pnl_tp2} USDT\n"
                    f"Net Stop: {plan.net_pnl_stop} USDT\n"
                    f"RR TP2: {plan.net_rr_tp2}"
                )
            )

            return {
                "status": "rejected_before_publish",
                "reason": plan.reject_reason,
                "price": price,
                "entry": [entry_from, entry_to],
                "stop": stop,
                "tp": test_signal["tp"],
                "grade": grade,
                "plan": {
                    "qty": plan.qty,
                    "required_margin": plan.required_margin,
                    "net_pnl_tp1": plan.net_pnl_tp1,
                    "net_pnl_tp2": plan.net_pnl_tp2,
                    "net_pnl_stop": plan.net_pnl_stop,
                    "net_rr_tp2": plan.net_rr_tp2,
                },
            }

        sig = Signal(
            bot_id=bot.id,
            symbol=test_signal["symbol"],
            side=test_signal["action"],
            status="published",
            entry_zone_json={"from": entry_from, "to": entry_to},
            stop_price=stop,
            tp_json=test_signal["tp"],
            confidence=91.0,
            grade=grade,
            is_public=quality.should_publish_to_clients(grade),
            expires_at=expires_at,
            rationale=test_signal["reason"],

            qty=plan.qty,
            required_margin=plan.required_margin,
            net_rr_tp1=plan.net_rr_tp1,
            net_rr_tp2=plan.net_rr_tp2,
            net_pnl_tp1=plan.net_pnl_tp1,
            net_pnl_tp2=plan.net_pnl_tp2,
            net_pnl_stop=plan.net_pnl_stop,
            plan_json={
                "qty": plan.qty,
                "required_margin": plan.required_margin,
                "net_pnl_tp1": plan.net_pnl_tp1,
                "net_pnl_tp2": plan.net_pnl_tp2,
                "net_pnl_stop": plan.net_pnl_stop,
                "net_rr_tp1": plan.net_rr_tp1,
                "net_rr_tp2": plan.net_rr_tp2,
                "is_valid": plan.is_valid,
                "reject_reason": plan.reject_reason,
            },
        )

        db.add(sig)
        db.commit()
        db.refresh(sig)

        router = TelegramRouter()
        await router.publish_new_signal(
            signal=test_signal,
            confidence=91.0,
            grade=grade,
            signal_id=sig.id,
        )

        return {
            "status": "created_and_sent",
            "signal_id": sig.id,
            "price": price,
            "entry": [entry_from, entry_to],
            "stop": stop,
            "tp": test_signal["tp"],
            "grade": grade,
            "is_public": sig.is_public,
            "expires_at": str(expires_at),
            "plan": {
                "qty": plan.qty,
                "required_margin": plan.required_margin,
                "net_pnl_tp1": plan.net_pnl_tp1,
                "net_pnl_tp2": plan.net_pnl_tp2,
                "net_pnl_stop": plan.net_pnl_stop,
                "net_rr_tp2": plan.net_rr_tp2,
            },
        }

    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}

    finally:
        db.close()

@app.post("/robot/test-lifecycle-price", dependencies=[Depends(require_owner_action), Depends(require_non_production_debug)])
async def test_lifecycle_price(payload: TestLifecyclePriceRequest):
    db = SessionLocal()

    try:
        sig = db.query(Signal).filter(Signal.id == payload.signal_id).first()

        if not sig:
            return {"status": "error", "error": "signal_not_found"}

        manager = SignalLifecycleManager()
        await manager.process_signal_with_price(db, sig, payload.price)

        db.commit()

        return {
            "status": "ok",
            "signal_id": sig.id,
            "new_status": sig.status,
            "result_pct": sig.result_pct,
            "test_price": payload.price,
        }

    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}

    finally:
        db.close()


















@app.get("/system/health", dependencies=[Depends(require_owner_action)])
def system_health():
    db = SessionLocal()

    try:
        loops = {
            "robot_loop": {
                "enabled": robot_loop_enabled,
                "task_created": robot_task is not None,
                "task_done": robot_task.done() if robot_task else None,
            },
            "subscription_loop": {
                "enabled": subscription_loop_enabled,
                "task_created": subscription_task is not None,
                "task_done": subscription_task.done() if subscription_task else None,
            },
            "telegram_delivery_loop": {
                "enabled": telegram_delivery_loop_enabled,
                "task_created": telegram_delivery_task is not None,
                "task_done": telegram_delivery_task.done() if telegram_delivery_task else None,
            },
            "payment_reconciliation_loop": {
                "enabled": payment_reconciliation_loop_enabled,
                "task_created": payment_reconciliation_task is not None,
                "task_done": payment_reconciliation_task.done() if payment_reconciliation_task else None,
            },
            "funding_arb_loop": {
                "enabled": funding_arb_loop_enabled and settings.ENABLE_FUNDING_ARB,
                "task_created": funding_arb_task is not None,
                "task_done": funding_arb_task.done() if funding_arb_task else None,
                "scan_interval_hours": settings.FUNDING_ARB_SCAN_INTERVAL_HOURS,
            },
        }
        return SystemHealthService().summary(db, loops=loops)

    finally:
        db.close()






























































@app.post("/robot/force-valid-trade-signal", dependencies=[Depends(require_owner_action), Depends(require_non_production_debug)])
async def force_valid_trade_signal():
    db = SessionLocal()

    try:
        bot = db.query(Bot).filter(Bot.name == "Main Robot").first()

        market = MarketDataService()
        snap = market.snapshot("BTC/USDT")
        price = float(snap["last"])

        entry_from = round(price * 0.999, 2)
        entry_to = round(price * 1.001, 2)
        entry_price = round((entry_from + entry_to) / 2, 2)

        stop = round(price * 0.985, 2)
        tp1 = round(price * 1.025, 2)
        tp2 = round(price * 1.08, 2)

        test_signal = {
            "action": "long",
            "symbol": "BTC/USDT",
            "entry_zone": [entry_from, entry_to],
            "stop_price": stop,
            "tp": {
                "tp1": tp1,
                "tp2": tp2,
            },
            "reason": "forced_valid_trade_signal",
        }

        quality = SignalQualityService()
        grade = quality.grade(91.0, test_signal["reason"])
        expires_at = quality.expiry_time(grade)

        builder = TradePlanBuilder()
        plan = builder.build_plan(
            symbol=test_signal["symbol"],
            side=test_signal["action"],
            entry_price=entry_price,
            stop_price=stop,
            tp1=tp1,
            tp2=tp2,
            balance_usdt=1000.0,
        )

        if not plan.is_valid:
            return {
                "status": "rejected_before_publish",
                "reason": plan.reject_reason,
                "price": price,
                "entry": [entry_from, entry_to],
                "stop": stop,
                "tp": test_signal["tp"],
                "grade": grade,
                "plan": {
                    "qty": plan.qty,
                    "required_margin": plan.required_margin,
                    "net_pnl_tp1": plan.net_pnl_tp1,
                    "net_pnl_tp2": plan.net_pnl_tp2,
                    "net_pnl_stop": plan.net_pnl_stop,
                    "net_rr_tp2": plan.net_rr_tp2,
                },
            }

        exposure = ExposureGuard()

        exposure_result = exposure.check_before_publish(
            db=db,
            bot_id=bot.id,
            symbol=test_signal["symbol"],
            required_margin=float(plan.required_margin or 0),
            equity_usdt=float(getattr(settings, "RISK_EQUITY_USDT", 950.0)),
            max_used_margin_pct=float(getattr(settings, "MAX_USED_MARGIN_PCT", 0.85)),
            max_active_signals=int(getattr(settings, "MAX_ACTIVE_SIGNALS", 2)),
            max_active_per_symbol=int(getattr(settings, "MAX_ACTIVE_SIGNALS_PER_SYMBOL", 1)),
        )

        if not exposure_result.allowed:
            await TelegramRouter().owner_alert(
                "FORCED SIGNAL EXPOSURE BLOCKED",
                (
                    f"{test_signal['symbol']} {test_signal['action']}\n"
                    f"Reason: {exposure_result.reason}\n"
                    f"Required margin: {exposure_result.required_margin} USDT\n"
                    f"Used margin: {exposure_result.used_margin} USDT\n"
                    f"Free margin: {exposure_result.free_margin} USDT\n"
                    f"Max allowed margin: {exposure_result.max_allowed_margin} USDT\n"
                    f"Active signals: {exposure_result.active_signals_count}\n"
                    f"Active symbol signals: {exposure_result.active_symbol_signals_count}"
                )
            )

            return {
                "status": "rejected_before_publish",
                "reason": exposure_result.reason,
                "price": price,
                "entry": [entry_from, entry_to],
                "stop": stop,
                "tp": test_signal["tp"],
                "grade": grade,
                "exposure": {
                    "allowed": exposure_result.allowed,
                    "reason": exposure_result.reason,
                    "active_signals_count": exposure_result.active_signals_count,
                    "active_symbol_signals_count": exposure_result.active_symbol_signals_count,
                    "used_margin": exposure_result.used_margin,
                    "max_allowed_margin": exposure_result.max_allowed_margin,
                    "free_margin": exposure_result.free_margin,
                    "required_margin": exposure_result.required_margin,
                },
                "plan": {
                    "qty": plan.qty,
                    "required_margin": plan.required_margin,
                    "net_pnl_tp1": plan.net_pnl_tp1,
                    "net_pnl_tp2": plan.net_pnl_tp2,
                    "net_pnl_stop": plan.net_pnl_stop,
                    "net_rr_tp2": plan.net_rr_tp2,
                },
            }

        sig = Signal(
            bot_id=bot.id,
            symbol=test_signal["symbol"],
            side=test_signal["action"],
            status="published",
            entry_zone_json={"from": entry_from, "to": entry_to},
            stop_price=stop,
            tp_json=test_signal["tp"],
            confidence=91.0,
            grade=grade,
            is_public=quality.should_publish_to_clients(grade),
            expires_at=expires_at,
            rationale=test_signal["reason"],

            qty=plan.qty,
            required_margin=plan.required_margin,
            net_rr_tp1=plan.net_rr_tp1,
            net_rr_tp2=plan.net_rr_tp2,
            net_pnl_tp1=plan.net_pnl_tp1,
            net_pnl_tp2=plan.net_pnl_tp2,
            net_pnl_stop=plan.net_pnl_stop,
            plan_json={
                "qty": plan.qty,
                "required_margin": plan.required_margin,
                "net_pnl_tp1": plan.net_pnl_tp1,
                "net_pnl_tp2": plan.net_pnl_tp2,
                "net_pnl_stop": plan.net_pnl_stop,
                "net_rr_tp1": plan.net_rr_tp1,
                "net_rr_tp2": plan.net_rr_tp2,
                "is_valid": plan.is_valid,
                "reject_reason": plan.reject_reason,
            },
        )


        db.add(sig)
        db.commit()

        router = TelegramRouter()
        await router.publish_new_signal(
            signal=test_signal,
            confidence=91.0,
            grade=grade,
            signal_id=sig.id
        )

        return {
            "status": "created_and_sent",
            "signal_id": sig.id,
            "price": price,
            "entry": [entry_from, entry_to],
            "stop": stop,
            "tp": test_signal["tp"],
            "grade": grade,
            "is_public": sig.is_public,
            "expires_at": str(expires_at),
            "plan": {
                "qty": plan.qty,
                "required_margin": plan.required_margin,
                "net_pnl_tp1": plan.net_pnl_tp1,
                "net_pnl_tp2": plan.net_pnl_tp2,
                "net_pnl_stop": plan.net_pnl_stop,
                "net_rr_tp2": plan.net_rr_tp2,
            },
        }

    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}

    finally:
        db.close()

@app.get("/intelligence/analyze", dependencies=[Depends(require_owner_action)])
def intelligence_analyze(symbol: str = "BTC/USDT"):
    try:
        engine = MarketIntelligenceEngine()
        result = engine.analyze_symbol(symbol)

        return {
            "status": "ok",
            "result": result.__dict__,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
        }

@app.post("/robot/force-intelligence-signal", dependencies=[Depends(require_owner_action), Depends(require_non_production_debug)])
async def force_intelligence_signal(symbol: str = "BTC/USDT"):
    db = SessionLocal()

    try:
        bot = db.query(Bot).filter(Bot.name == "Main Robot").first()

        if not bot:
            return {"status": "error", "error": "bot_not_found"}

        intelligence = MarketIntelligenceEngine()
        result = intelligence.analyze_symbol(symbol)

        if result.action == "hold":
            is_watch = result.radar_state in ["watch_long", "watch_short"]
            title = "INTELLIGENCE WATCH" if is_watch else "INTELLIGENCE HOLD"

            await TelegramRouter().owner_alert(
                title,
                (
                    f"{symbol}\n"
                    f"Regime: {result.regime}\n"
                    f"Confidence hint: {result.confidence_hint}\n"
                    f"Radar: {result.radar_state}\n"
                    f"Reason: {result.reason}\n"
                    f"Scores: {result.scores}\n"
                    f"Setup: {result.setup_quality}"
                )
            )

            return {
                "status": "watch" if is_watch else "hold",
                "symbol": symbol,
                "regime": result.regime,
                "confidence_hint": result.confidence_hint,
                "radar_state": result.radar_state,
                "reason": result.reason,
                "scores": result.scores,
                "setup_quality": result.setup_quality,
                "setup_decision": result.setup_decision,
            }

        if result.setup_decision != "approve":
            await TelegramRouter().owner_alert(
                "INTELLIGENCE WAIT",
                (
                    f"{symbol}\n"
                    f"Action: {result.action}\n"
                    f"Regime: {result.regime}\n"
                    f"Setup decision: {result.setup_decision}\n"
                    f"Setup quality: {result.setup_quality}\n"
                    f"Scores: {result.scores}"
                )
            )

            return {
                "status": "wait" if result.setup_decision == "wait" else "rejected_before_publish",
                "reason": result.setup_quality.get("comment", result.setup_decision) if result.setup_quality else result.setup_decision,
                "symbol": symbol,
                "action": result.action,
                "regime": result.regime,
                "confidence_hint": result.confidence_hint,
                "setup_quality": result.setup_quality,
                "scores": result.scores,
            }

        if result.action == "short" and not settings.ALLOW_SHORTS:
            throttle_minutes = getattr(settings, "SHORT_ALERT_THROTTLE_MINUTES", 60)

            if should_send_short_block_alert(db, symbol, throttle_minutes):
                await TelegramRouter().owner_alert(
                    "SHORT CANDIDATE OBSERVED",
                    (
                        f"{symbol}\n"
                        f"Short candidate detected, but current execution mode is "
                        f"{settings.execution_market_type} / shorts_disabled.\n"
                        f"Enable margin/futures short execution module before publishing short signals.\n"
                        f"Regime: {result.regime}\n"
                        f"Confidence: {result.confidence_hint}\n"
                        f"Scores: {result.scores}"
                    )
                )

            return {
                "status": "blocked",
                "reason": "short_candidate_but_shorts_disabled",
                "symbol": symbol,
                "action": result.action,
                "regime": result.regime,
                "confidence_hint": result.confidence_hint,
                "radar_state": result.radar_state,
                "scores": result.scores,
                "setup_quality": result.setup_quality,
                "setup_decision": result.setup_decision,
            }

        entry_from = float(result.entry_zone[0])
        entry_to = float(result.entry_zone[1])
        entry_price = round((entry_from + entry_to) / 2, 2)

        stop = float(result.stop_price)
        tp1 = float(result.tp["tp1"])
        tp2 = float(result.tp["tp2"])

        test_signal = {
            "action": result.action,
            "symbol": symbol,
            "entry_zone": [entry_from, entry_to],
            "stop_price": stop,
            "tp": {
                "tp1": tp1,
                "tp2": tp2,
            },
            "reason": f"intelligence_{result.reason}",
        }

        quality = SignalQualityService()

        effective_confidence = _intelligence_effective_confidence(result)

        grade = quality.grade(effective_confidence, test_signal["reason"], result.regime)
        expires_at = quality.expiry_time(grade)

        if not quality.should_publish_to_clients(grade):
            await TelegramRouter().owner_alert(
                "INTELLIGENCE LOW QUALITY",
                (
                    f"{symbol} {result.action}\n"
                    f"Grade: {grade}\n"
                    f"Confidence: {result.confidence_hint}\n"
                    f"Regime: {result.regime}\n"
                    f"Reason: {result.reason}\n"
                    f"Scores: {result.scores}"
                )
            )

            return {
                "status": "rejected_before_publish",
                "reason": "quality_grade_too_low",
                "symbol": symbol,
                "action": result.action,
                "grade": grade,
                "confidence_hint": result.confidence_hint,
                "scores": result.scores,
            }

        builder = TradePlanBuilder()
        plan = builder.build_plan(
            symbol=symbol,
            side=result.action,
            entry_price=entry_price,
            stop_price=stop,
            tp1=tp1,
            tp2=tp2,
            balance_usdt=1000.0,
        )

        if not plan.is_valid:
            await TelegramRouter().owner_alert(
                "INTELLIGENCE TRADE PLAN REJECTED",
                (
                    f"{symbol} {result.action}\n"
                    f"Reason: {plan.reject_reason}\n"
                    f"Regime: {result.regime}\n"
                    f"Confidence: {result.confidence_hint}\n"
                    f"Entry: {entry_price}\n"
                    f"Stop: {stop}\n"
                    f"TP1: {tp1}\n"
                    f"TP2: {tp2}\n"
                    f"Net TP1: {plan.net_pnl_tp1} USDT\n"
                    f"Net TP2: {plan.net_pnl_tp2} USDT\n"
                    f"Net Stop: {plan.net_pnl_stop} USDT\n"
                    f"RR TP2: {plan.net_rr_tp2}\n"
                    f"Scores: {result.scores}"
                )
            )

            return {
                "status": "rejected_before_publish",
                "reason": plan.reject_reason,
                "symbol": symbol,
                "action": result.action,
                "grade": grade,
                "confidence_hint": result.confidence_hint,
                "regime": result.regime,
                "entry": [entry_from, entry_to],
                "stop": stop,
                "tp": test_signal["tp"],
                "plan": {
                    "qty": plan.qty,
                    "required_margin": plan.required_margin,
                    "net_pnl_tp1": plan.net_pnl_tp1,
                    "net_pnl_tp2": plan.net_pnl_tp2,
                    "net_pnl_stop": plan.net_pnl_stop,
                    "net_rr_tp2": plan.net_rr_tp2,
                },
                "scores": result.scores,
            }

        exposure = ExposureGuard()

        exposure_result = exposure.check_before_publish(
            db=db,
            bot_id=bot.id,
            symbol=symbol,
            required_margin=float(plan.required_margin or 0),
            equity_usdt=float(getattr(settings, "RISK_EQUITY_USDT", 950.0)),
            max_used_margin_pct=float(getattr(settings, "MAX_USED_MARGIN_PCT", 0.85)),
            max_active_signals=int(getattr(settings, "MAX_ACTIVE_SIGNALS", 2)),
            max_active_per_symbol=int(getattr(settings, "MAX_ACTIVE_SIGNALS_PER_SYMBOL", 1)),
        )

        if not exposure_result.allowed:
            await TelegramRouter().owner_alert(
                "INTELLIGENCE EXPOSURE BLOCKED",
                (
                    f"{symbol} {result.action}\n"
                    f"Reason: {exposure_result.reason}\n"
                    f"Required margin: {exposure_result.required_margin} USDT\n"
                    f"Used margin: {exposure_result.used_margin} USDT\n"
                    f"Free margin: {exposure_result.free_margin} USDT\n"
                    f"Max allowed margin: {exposure_result.max_allowed_margin} USDT\n"
                    f"Active signals: {exposure_result.active_signals_count}\n"
                    f"Active symbol signals: {exposure_result.active_symbol_signals_count}"
                )
            )

            return {
                "status": "rejected_before_publish",
                "reason": exposure_result.reason,
                "symbol": symbol,
                "action": result.action,
                "grade": grade,
                "confidence_hint": result.confidence_hint,
                "effective_confidence": effective_confidence,
                "entry": [entry_from, entry_to],
                "stop": stop,
                "tp": test_signal["tp"],
                "exposure": {
                    "allowed": exposure_result.allowed,
                    "reason": exposure_result.reason,
                    "active_signals_count": exposure_result.active_signals_count,
                    "active_symbol_signals_count": exposure_result.active_symbol_signals_count,
                    "used_margin": exposure_result.used_margin,
                    "max_allowed_margin": exposure_result.max_allowed_margin,
                    "free_margin": exposure_result.free_margin,
                    "required_margin": exposure_result.required_margin,
                },
                "plan": {
                    "qty": plan.qty,
                    "required_margin": plan.required_margin,
                    "net_pnl_tp1": plan.net_pnl_tp1,
                    "net_pnl_tp2": plan.net_pnl_tp2,
                    "net_pnl_stop": plan.net_pnl_stop,
                    "net_rr_tp1": plan.net_rr_tp1,
                    "net_rr_tp2": plan.net_rr_tp2,
                    "is_valid": plan.is_valid,
                    "reject_reason": plan.reject_reason,
                },
            }

        sig = Signal(
            bot_id=bot.id,
            symbol=symbol,
            side=result.action,
            status="published",
            entry_zone_json={"from": entry_from, "to": entry_to},
            stop_price=stop,
            tp_json=test_signal["tp"],
            confidence=effective_confidence,
            grade=grade,
            is_public=quality.should_publish_to_clients(grade),
            expires_at=expires_at,
            rationale=test_signal["reason"],

            qty=plan.qty,
            required_margin=plan.required_margin,
            net_rr_tp1=plan.net_rr_tp1,
            net_rr_tp2=plan.net_rr_tp2,
            net_pnl_tp1=plan.net_pnl_tp1,
            net_pnl_tp2=plan.net_pnl_tp2,
            net_pnl_stop=plan.net_pnl_stop,
            plan_json={
                "qty": plan.qty,
                "required_margin": plan.required_margin,
                "net_pnl_tp1": plan.net_pnl_tp1,
                "net_pnl_tp2": plan.net_pnl_tp2,
                "net_pnl_stop": plan.net_pnl_stop,
                "net_rr_tp1": plan.net_rr_tp1,
                "net_rr_tp2": plan.net_rr_tp2,
                "is_valid": plan.is_valid,
                "reject_reason": plan.reject_reason,
            },
        )

        db.add(sig)
        db.commit()
        db.refresh(sig)

        router = TelegramRouter()
        await router.publish_new_signal(
            signal=test_signal,
            confidence=effective_confidence,
            grade=grade,
            signal_id=sig.id,
        )

        return {
            "status": "created_and_sent",
            "signal_id": sig.id,
            "symbol": symbol,
            "action": result.action,
            "regime": result.regime,
            "grade": grade,
            "confidence_hint": result.confidence_hint,
            "effective_confidence": effective_confidence,
            "entry": [entry_from, entry_to],
            "stop": stop,
            "tp": test_signal["tp"],
            "scores": result.scores,
            "plan": {
                "qty": plan.qty,
                "required_margin": plan.required_margin,
                "net_pnl_tp1": plan.net_pnl_tp1,
                "net_pnl_tp2": plan.net_pnl_tp2,
                "net_pnl_stop": plan.net_pnl_stop,
                "net_rr_tp1": plan.net_rr_tp1,
                "net_rr_tp2": plan.net_rr_tp2,
                "is_valid": plan.is_valid,
                "reject_reason": plan.reject_reason,
            },
        }

    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}

    finally:
        db.close()


def _utcnow():
    return datetime.now(timezone.utc)


def _to_aware_utc(dt):
    if dt is None:
        return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)

def _find_active_watch_event(db: Session, symbol: str, radar_state: str, max_age_minutes: int = 90):
    if not symbol or radar_state not in ["watch_long", "watch_short"]:
        return None

    since = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)

    return (
        db.query(IntelligenceEvent)
        .filter(
            IntelligenceEvent.symbol == symbol,
            IntelligenceEvent.status == "watch",
            IntelligenceEvent.decision == radar_state,
            IntelligenceEvent.created_at >= since,
        )
        .order_by(IntelligenceEvent.created_at.asc())
        .first()
    )

def _intelligence_effective_confidence(result) -> float:
    """
    Калибрует confidence для Intelligence-сигналов.

    Этапы:
    1. Base = direction-aware confidence_hint (inverting trend/momentum for shorts)
    2. Setup quality adjustment via setup_score
    3. Cap at 88 для strong setups, 80 для moderate

    До v2 confidence_hint для SHORT сигналов был ~44% даже в медвежьем рынке,
    потому что _score_context использует long-biased шкалу (trend_up=75, trend_down=25).
    После v2: direction-aware base для short = 100 - raw_trend.
    """
    raw_base = float(result.confidence_hint or 0)

    # Direction-aware base: mirror the formula in _build_multi_timeframe_candidate
    action = str(getattr(result, "action", "") or "").lower()
    scores = result.scores if isinstance(result.scores, dict) else {}

    if action in ("long", "short") and scores:
        raw_trend    = float(scores.get("trend", 50.0))
        raw_momentum = float(scores.get("momentum", 50.0))
        if action == "short":
            dir_trend    = 100.0 - raw_trend
            dir_momentum = 100.0 - raw_momentum
        else:
            dir_trend    = raw_trend
            dir_momentum = raw_momentum
        base = round(
            dir_trend    * 0.30
            + dir_momentum * 0.20
            + float(scores.get("volume", 50.0))    * 0.20
            + float(scores.get("structure", 50.0)) * 0.20
            + float(scores.get("volatility", 50.0)) * 0.10,
            2,
        )
    else:
        base = raw_base

    setup_quality = result.setup_quality if isinstance(result.setup_quality, dict) else {}
    setup_score   = float(setup_quality.get("final_score") or 0)
    setup_decision = str(setup_quality.get("decision") or result.setup_decision or "")

    if setup_decision == "approve" and setup_score >= 70:
        calibrated = max(base, setup_score * 0.92)
        return round(min(calibrated, 88.0), 2)

    if setup_decision == "approve" and setup_score >= 62:
        calibrated = max(base, setup_score * 0.95)
        return round(min(calibrated, 80.0), 2)

    if setup_decision == "wait" and setup_score >= 55:
        calibrated = max(base, setup_score * 0.75)
        return round(min(calibrated, 72.0), 2)

    return round(base, 2)

@app.get("/intelligence/scan", dependencies=[Depends(require_owner_action)])
def intelligence_scan_readonly():
    """
    READONLY live scan.

    Важно:
    - НЕ создаёт Signal
    - НЕ публикует в Telegram
    - НЕ пишет DecisionEvent
    - только показывает текущую рыночную картину для UI
    """
    db = SessionLocal()

    try:
        bot = db.query(Bot).filter(Bot.name == "Main Robot").first()

        if not bot:
            return {
                "status": "error",
                "error": "bot_not_found",
                "mode": "readonly_live",
                "symbols": [],
                "results": [],
            }

        intelligence = MarketIntelligenceEngine()
        builder = TradePlanBuilder()
        quality = SignalQualityService()
        exposure = ExposureGuard()

        results = []

        for symbol in bot.config_json.get("symbols", []):
            item = {
                "symbol": symbol,
                "status": "unknown",
                "decision": None,
                "action": None,
                "regime": None,
                "confidence_hint": None,
                "effective_confidence": None,
                "grade": None,
                "reason": None,
                "scores": None,
                "entry_zone": None,
                "stop_price": None,
                "tp": None,
                "plan": None,
                "exposure": None,
                "timeframes": None,
                "setup_quality": None,
                "setup_decision": None,
                "radar_state": None,
                "active_signal": None,
            }

            try:
                result = intelligence.analyze_symbol(symbol)

                item["action"] = result.action
                item["regime"] = result.regime
                item["confidence_hint"] = result.confidence_hint
                item["effective_confidence"] = _intelligence_effective_confidence(result)
                item["reason"] = result.reason
                item["radar_state"] = result.radar_state
                item["scores"] = result.scores
                item["setup_quality"] = result.setup_quality
                item["setup_decision"] = result.setup_decision
                item["timeframes"] = result.timeframes
                item["entry_zone"] = result.entry_zone
                item["stop_price"] = result.stop_price
                item["tp"] = result.tp

                if result.action == "hold":
                    if result.radar_state in ["watch_long", "watch_short"]:
                        item["status"] = "watch"
                        item["decision"] = result.radar_state
                    else:
                        item["status"] = "hold"
                        item["decision"] = "skip_no_trade_conditions"

                    results.append(item)
                    continue

                if result.setup_decision != "approve":
                    item["status"] = "wait" if result.setup_decision == "wait" else "rejected"
                    item["decision"] = (
                        result.setup_quality.get("comment", result.setup_decision)
                        if result.setup_quality
                        else result.setup_decision
                    )

                    results.append(item)
                    continue

                if result.action == "short" and not settings.ALLOW_SHORTS:
                    item["status"] = "blocked"
                    item["decision"] = "short_candidate_but_shorts_disabled"
                    item["grade"] = quality.grade(
                        item["effective_confidence"],
                        f"intelligence_{result.reason}",
                        result.regime,
                    )

                    results.append(item)
                    continue

                setup_score = None
                if result.setup_quality:
                    setup_score = result.setup_quality.get("final_score")

                grade = quality.grade(
                    result.confidence_hint,
                    f"intelligence_{result.reason}",
                    result.regime,
                    setup_score=setup_score,
                    effective_confidence=item["effective_confidence"],
                )

                item["grade"] = grade

                # RANGE-кандидат обходит трендовый quality-гейт (как в robot_loop).
                is_range = str(getattr(result, "regime", "")) in ("range", "crt")
                if not is_range and not quality.should_publish_to_clients(
                    grade,
                    setup_score=setup_score,
                    effective_confidence=item["effective_confidence"],
                    setup_decision=result.setup_decision,
                    setup_quality=result.setup_quality,
                ):
                    item["status"] = "rejected"
                    item["decision"] = "quality_grade_too_low"
                    results.append(item)
                    continue

                entry_from = float(result.entry_zone[0])
                entry_to = float(result.entry_zone[1])
                entry_price = round((entry_from + entry_to) / 2, 2)

                stop = float(result.stop_price)
                tp1 = float(result.tp["tp1"])
                tp2 = float(result.tp["tp2"])

                plan = builder.build_plan(
                    symbol=symbol,
                    side=result.action,
                    entry_price=entry_price,
                    stop_price=stop,
                    tp1=tp1,
                    tp2=tp2,
                    balance_usdt=float(getattr(settings, "RISK_EQUITY_USDT", 950.0)),
                )

                item["plan"] = {
                    "qty": plan.qty,
                    "required_margin": plan.required_margin,
                    "net_pnl_tp1": plan.net_pnl_tp1,
                    "net_pnl_tp2": plan.net_pnl_tp2,
                    "net_pnl_stop": plan.net_pnl_stop,
                    "net_rr_tp1": plan.net_rr_tp1,
                    "net_rr_tp2": plan.net_rr_tp2,
                    "is_valid": plan.is_valid,
                    "reject_reason": plan.reject_reason,
                }

                if not plan.is_valid:
                    if plan.reject_reason == "net_rr_too_low":
                        item["status"] = "wait"
                        item["decision"] = DECISION_WAIT_BETTER_ENTRY_RR
                        item["reason"] = "trend_ok_but_entry_rr_too_low_wait_pullback"
                        item["radar_state"] = "wait_better_entry_rr"
                        item["escalation_state"] = "waiting_better_entry"
                        item["escalation_reason"] = (
                            f"RR TP2 {plan.net_rr_tp2} too low; wait for better entry/pullback"
                        )
                    else:
                        item["status"] = "rejected"
                        item["decision"] = plan.reject_reason

                    results.append(item)
                    continue

                exposure_result = exposure.check_before_publish(
                    db=db,
                    bot_id=bot.id,
                    symbol=symbol,
                    required_margin=float(plan.required_margin or 0),
                    equity_usdt=float(getattr(settings, "RISK_EQUITY_USDT", 950.0)),
                    max_used_margin_pct=float(getattr(settings, "MAX_USED_MARGIN_PCT", 0.85)),
                    max_active_signals=int(getattr(settings, "MAX_ACTIVE_SIGNALS", 2)),
                    max_active_per_symbol=int(getattr(settings, "MAX_ACTIVE_SIGNALS_PER_SYMBOL", 1)),
                )

                item["exposure"] = {
                    "allowed": exposure_result.allowed,
                    "reason": exposure_result.reason,
                    "active_signals_count": exposure_result.active_signals_count,
                    "active_symbol_signals_count": exposure_result.active_symbol_signals_count,
                    "used_margin": exposure_result.used_margin,
                    "max_allowed_margin": exposure_result.max_allowed_margin,
                    "free_margin": exposure_result.free_margin,
                    "required_margin": exposure_result.required_margin,
                }

                if not exposure_result.allowed:
                    item["status"] = "blocked"
                    item["decision"] = exposure_result.reason

                    if exposure_result.reason == "active_signal_already_exists":
                        active_signals = exposure.active_signals_for_symbol(db, bot.id, symbol)
                        active_signal = active_signals[0] if active_signals else None

                        if active_signal:
                            item["active_signal"] = {
                                "id": active_signal.id,
                                "symbol": active_signal.symbol,
                                "side": active_signal.side,
                                "status": active_signal.status,
                                "grade": active_signal.grade,
                                "confidence": active_signal.confidence,
                                "entry_zone": active_signal.entry_zone_json,
                                "stop_price": active_signal.stop_price,
                                "tp": active_signal.tp_json,
                                "created_at": str(active_signal.created_at),
                                "expires_at": str(active_signal.expires_at) if active_signal.expires_at else None,
                            }

                    results.append(item)
                    continue

                item["status"] = "candidate"
                item["decision"] = "ready_to_publish"

                if grade == "C":
                    item["status"] = "wait"
                    item["decision"] = "grade_c_blocked_before_signal_create"
                    item["priority_publish_status"] = "skipped_grade_c_before_create"
                    continue

                results.append(item)

            except Exception as e:
                item["status"] = "error"
                item["decision"] = str(e)
                item["reason"] = str(e)
                results.append(item)

        return {
            "status": "ok",
            "mode": "readonly_live",
            "message": "Readonly live intelligence scan. No signals are published.",
            "symbols": bot.config_json.get("symbols", []),
            "results": results,
        }

    finally:
        db.close()


@app.post("/intelligence/scan/run", dependencies=[Depends(require_owner_action)])
async def intelligence_scan_run():
    if not INTELLIGENCE_PUBLISH_LOCK.acquire(blocking=False):
        return {
            "status": "busy",
            "message": "intelligence_publish_already_running",
            "symbols": [],
            "results": [],
        }

    db = SessionLocal()

    try:
        bot = db.query(Bot).filter(Bot.name == "Main Robot").first()

        if not bot:
            return {"status": "error", "error": "bot_not_found"}

        intelligence = MarketIntelligenceEngine()
        builder = TradePlanBuilder()
        quality = SignalQualityService()
        memory = IntelligenceMemory()
        performance_guard = SymbolPerformanceGuard()
        production_gate = ProductionEntryGate()
        reentry_guard = ReEntryCooldownGuard()
        priority = CandidatePriorityService()
        publish_queue = []
        published_count = 0
        max_publish_per_scan = int(getattr(settings, "MAX_PUBLISH_PER_SCAN", 2))
        replacement_policy = SignalReplacementPolicy()

        results = []

        def append_result(item: dict):
            item = memory.enrich_scan_item(db, item)

            now = _utcnow()

            if item.get("status") == "watch" and item.get("radar_state") in ["watch_long", "watch_short"]:
                symbol = item.get("symbol")
                radar_state = item.get("radar_state")

                active_watch = _find_active_watch_event(
                    db=db,
                    symbol=symbol,
                    radar_state=radar_state,
                )

                started_at = _to_aware_utc(active_watch.created_at) if active_watch else now
                age_minutes = round((now - started_at).total_seconds() / 60, 2)

                item["watch_started_at"] = started_at.isoformat()
                item["watch_age_minutes"] = age_minutes
                item["escalation_state"] = item.get("escalation_state") or "watching"
                item["escalation_reason"] = item.get("escalation_reason") or "waiting_for_confirmation"

            elif item.get("status") != "watch":
                item["watch_started_at"] = None
                item["watch_age_minutes"] = 0

            # 1. Если watch протух — снимаем с радара.
            if item.get("status") == "watch" and item.get("escalation_state") == "stale":
                expired_age = item.get("watch_age_minutes")
                expired_started_at = item.get("watch_started_at")

                item["status"] = "hold"
                item["decision"] = "watch_expired"
                item["radar_state"] = "none"
                item["reason"] = "watch_expired_no_confirmation"

                item["expired_watch_started_at"] = expired_started_at
                item["expired_watch_age_minutes"] = expired_age

                item["watch_started_at"] = None
                item["watch_age_minutes"] = 0
                item["escalation_state"] = None
                item["escalation_reason"] = "watch_expired_no_confirmation"

            # 2. Если монета хочет вернуться в watch после expiry — проверяем cooldown.
            if item.get("status") == "watch":
                cooldown_active, cooldown_reason = memory.is_watch_cooldown_active(
                    db=db,
                    symbol=item.get("symbol"),
                    radar_state=item.get("radar_state"),
                    cooldown_minutes=60,
                )

                if cooldown_active:
                    override_ok, override_reason = memory.has_strong_reentry_override(item)

                    if override_ok:
                        item["decision"] = item.get("radar_state")
                        item["reason"] = override_reason
                        item["reentry_override"] = True
                        item["reentry_override_reason"] = override_reason
                        item["escalation_reason"] = override_reason
                    else:
                        item["status"] = "hold"
                        item["decision"] = "watch_cooldown"
                        item["reason"] = cooldown_reason
                        item["radar_state"] = "none"

                        item["reentry_override"] = False
                        item["reentry_override_reason"] = None

                        item["watch_started_at"] = None
                        item["watch_age_minutes"] = 0
                        item["escalation_state"] = None
                        item["escalation_reason"] = cooldown_reason

            memory.record_scan_item(db, item)
            try:
                ranked_item = priority.rank_one(item)
                item["priority_score"] = ranked_item.priority_score
                item["priority_reason"] = ranked_item.reason
            except Exception as e:
                item["priority_score"] = 0.0
                item["priority_reason"] = f"priority_error:{type(e).__name__}: {e}"
            results.append(item)

        for symbol in bot.config_json.get("symbols", []):
            item = {
                "symbol": symbol,
                "status": "unknown",
                "action": None,
                "regime": None,
                "confidence_hint": None,
                "grade": None,
                "reason": None,
                "scores": None,
                "entry_zone": None,
                "stop_price": None,
                "tp": None,
                "plan": None,
                "timeframes": None,
                "setup_quality": None,
                "setup_decision": None,
                "radar_state": None,
            }

            try:
                result = intelligence.analyze_symbol(symbol)

                item["action"] = result.action
                item["regime"] = result.regime
                item["confidence_hint"] = result.confidence_hint
                item["reason"] = result.reason
                item["radar_state"] = result.radar_state
                item["scores"] = result.scores
                item["setup_quality"] = result.setup_quality
                item["setup_decision"] = result.setup_decision
                item["timeframes"] = result.timeframes
                item["entry_zone"] = result.entry_zone
                item["stop_price"] = result.stop_price
                item["tp"] = result.tp

                effective_confidence = _intelligence_effective_confidence(result)
                item["effective_confidence"] = effective_confidence

                if result.action == "hold":
                    if result.radar_state in ["watch_long", "watch_short"]:
                        item["status"] = "watch"
                        item["decision"] = result.radar_state
                    else:
                        item["status"] = "hold"
                        item["decision"] = "skip_no_trade_conditions"

                    append_result(item)
                    continue

                if result.setup_decision != "approve":
                    item["status"] = "wait" if result.setup_decision == "wait" else "rejected"
                    item["decision"] = (
                        result.setup_quality.get("comment", result.setup_decision)
                        if result.setup_quality
                        else result.setup_decision
                    )
                    append_result(item)
                    continue

                if result.action == "short" and not settings.ALLOW_SHORTS:
                    grade = quality.grade(
                        effective_confidence,
                        f"intelligence_{result.reason}",
                        result.regime,
                    )

                    item["grade"] = grade
                    item["status"] = "blocked"
                    item["decision"] = "short_candidate_but_shorts_disabled"
                    item["block_reason"] = (
                        f"current_execution_mode_{settings.execution_market_type}_shorts_disabled"
                    )

                    append_result(item)
                    continue

                effective_confidence = _intelligence_effective_confidence(result)

                item["effective_confidence"] = effective_confidence

                setup_score = None
                if result.setup_quality:
                    setup_score = result.setup_quality.get("final_score")

                effective_confidence = item.get("effective_confidence") or result.confidence_hint

                grade = quality.grade(
                    result.confidence_hint,
                    f"intelligence_{result.reason}",
                    result.regime,
                    setup_score=setup_score,
                    effective_confidence=effective_confidence,
                )

                item["grade"] = grade

                if not quality.should_publish_to_clients(
                    grade,
                    setup_score=setup_score,
                    effective_confidence=effective_confidence,
                    setup_decision=result.setup_decision,
                    setup_quality=result.setup_quality,
                ):
                    item["status"] = "rejected"
                    item["decision"] = "quality_grade_too_low"
                    append_result(item)
                    continue

                entry_from = float(result.entry_zone[0])
                entry_to = float(result.entry_zone[1])
                entry_price = round((entry_from + entry_to) / 2, 2)

                stop = float(result.stop_price)
                tp1 = float(result.tp["tp1"])
                tp2 = float(result.tp["tp2"])

                performance_decision = performance_guard.analyze(
                    db=db,
                    bot_id=bot.id,
                    symbol=symbol,
                    lookback=12,
                )

                item["performance_guard"] = performance_guard.to_dict(performance_decision)

                if not performance_decision.allowed:
                    item["status"] = "blocked"
                    item["decision"] = performance_decision.reason
                    item["reason"] = performance_decision.reason
                    append_result(item)
                    continue

                base_plan_balance_usdt = float(getattr(settings, "RISK_EQUITY_USDT", 950.0))
                plan_balance_usdt = round(
                    base_plan_balance_usdt * float(performance_decision.risk_multiplier),
                    6,
                )

                # Минимальный учебный размер, чтобы не превращать сделку в пыль.
                # Но если risk_multiplier = 0, до сюда мы уже не дошли.
                plan_balance_usdt = max(plan_balance_usdt, 150.0)

                plan = builder.build_plan(
                    symbol=symbol,
                    side=result.action,
                    entry_price=entry_price,
                    stop_price=stop,
                    tp1=tp1,
                    tp2=tp2,
                    balance_usdt=plan_balance_usdt,
                )

                item["plan"] = {
                    "qty": plan.qty,
                    "required_margin": plan.required_margin,
                    "net_pnl_tp1": plan.net_pnl_tp1,
                    "net_pnl_tp2": plan.net_pnl_tp2,
                    "net_pnl_stop": plan.net_pnl_stop,
                    "net_rr_tp1": plan.net_rr_tp1,
                    "net_rr_tp2": plan.net_rr_tp2,
                    "is_valid": plan.is_valid,
                    "reject_reason": plan.reject_reason,
                    "plan_balance_usdt": plan_balance_usdt,
                    "performance_risk_multiplier": performance_decision.risk_multiplier,
                }

                if not plan.is_valid:
                    if plan.reject_reason == "net_rr_too_low":
                        item["status"] = "wait"
                        item["decision"] = DECISION_WAIT_BETTER_ENTRY_RR
                        item["reason"] = "trend_ok_but_entry_rr_too_low_wait_pullback"
                        item["radar_state"] = "wait_better_entry_rr"
                        item["escalation_state"] = "waiting_better_entry"
                        item["escalation_reason"] = (
                            f"RR TP2 {plan.net_rr_tp2} too low; wait for better entry/pullback"
                        )
                    else:
                        item["status"] = "rejected"
                        item["decision"] = plan.reject_reason

                    append_result(item)
                    continue

                exposure = ExposureGuard()

                exposure_result = exposure.check_before_publish(
                    db=db,
                    bot_id=bot.id,
                    symbol=symbol,
                    required_margin=float(plan.required_margin or 0),
                    equity_usdt=float(getattr(settings, "RISK_EQUITY_USDT", 950.0)),
                    max_used_margin_pct=float(getattr(settings, "MAX_USED_MARGIN_PCT", 0.85)),
                    max_active_signals=int(getattr(settings, "MAX_ACTIVE_SIGNALS", 2)),
                    max_active_per_symbol=int(getattr(settings, "MAX_ACTIVE_SIGNALS_PER_SYMBOL", 1)),
                )

                item["exposure"] = {
                    "allowed": exposure_result.allowed,
                    "reason": exposure_result.reason,
                    "active_signals_count": exposure_result.active_signals_count,
                    "active_symbol_signals_count": exposure_result.active_symbol_signals_count,
                    "used_margin": exposure_result.used_margin,
                    "max_allowed_margin": exposure_result.max_allowed_margin,
                    "free_margin": exposure_result.free_margin,
                    "required_margin": exposure_result.required_margin,
                }

                if not exposure_result.allowed:
                    item["status"] = "blocked"
                    item["decision"] = exposure_result.reason

                    if exposure_result.reason == "active_signal_already_exists":
                        active_signals = exposure.active_signals_for_symbol(db, bot.id, symbol)
                        active_signal = active_signals[0] if active_signals else None

                        if active_signal:
                            item["active_signal"] = {
                                "id": active_signal.id,
                                "symbol": active_signal.symbol,
                                "side": active_signal.side,
                                "status": active_signal.status,
                                "grade": active_signal.grade,
                                "confidence": active_signal.confidence,
                                "entry_zone": active_signal.entry_zone_json,
                                "stop_price": active_signal.stop_price,
                                "tp": active_signal.tp_json,
                                "created_at": str(active_signal.created_at),
                                "expires_at": str(active_signal.expires_at) if active_signal.expires_at else None,
                            }

                    append_result(item)
                    continue

                expires_at = quality.expiry_time(grade)

                test_signal = {
                    "action": result.action,
                    "symbol": symbol,
                    "entry_zone": [entry_from, entry_to],
                    "stop_price": stop,
                    "tp": {
                        "tp1": tp1,
                        "tp2": tp2,
                    },
                    "reason": f"intelligence_{result.reason}",
                }

                item["status"] = "candidate"
                item["decision"] = "ready_to_publish"
                item["publish_payload"] = {
                    "test_signal": test_signal,
                    "expires_at": expires_at.isoformat() if expires_at else None,
                }

                if grade == "C":
                    item["status"] = "wait"
                    item["decision"] = "grade_c_learning_only_not_publishable"
                    item["priority_publish_status"] = "skipped_grade_c"
                    append_result(item)
                    continue

                publish_queue.append({
                    "item": item,
                    "result": result,
                    "test_signal": test_signal,
                    "grade": grade,
                    "effective_confidence": effective_confidence,
                    "expires_at": expires_at,
                    "plan": plan,
                    "entry_from": entry_from,
                    "entry_to": entry_to,
                    "stop": stop,
                    "tp1": tp1,
                    "tp2": tp2,
                })

                append_result(item)
                continue

            except Exception as e:
                item["status"] = "error"
                item["decision"] = str(e)
                append_result(item)

        db.commit()

        db.flush()

        ranked_publish_queue = sorted(
            publish_queue,
            key=lambda x: priority.rank_one(x["item"]).priority_score,
            reverse=True,
        )

        published = []

        for candidate in ranked_publish_queue:
            item = candidate["item"]

            if published_count >= max_publish_per_scan:
                item["status"] = "wait"
                item["decision"] = "priority_queue_wait_next_scan"
                item["priority_publish_status"] = "deferred_by_scan_limit"
                continue

            symbol = item["symbol"]
            side = item["action"]
            plan = candidate["plan"]
            test_signal = candidate["test_signal"]
            grade = candidate["grade"]
            effective_confidence = candidate["effective_confidence"]
            expires_at = candidate["expires_at"]

            # Повторная проверка дублей прямо перед публикацией.
            existing_signal = (
                db.query(Signal)
                .filter(
                    Signal.bot_id == bot.id,
                    Signal.symbol == symbol,
                    Signal.side == side,
                    Signal.status.in_(["published", "opened", "tp1", "breakeven"]),
                )
                .order_by(Signal.id.desc())
                .first()
            )

            if existing_signal:
                item["status"] = "blocked"
                item["decision"] = DECISION_ACTIVE_SIGNAL_ALREADY_EXISTS
                item["priority_publish_status"] = "skipped_duplicate_before_publish"
                item["active_signal"] = {
                    "id": existing_signal.id,
                    "symbol": existing_signal.symbol,
                    "side": existing_signal.side,
                    "status": existing_signal.status,
                    "grade": existing_signal.grade,
                    "confidence": existing_signal.confidence,
                    "entry_zone": existing_signal.entry_zone_json,
                    "stop_price": existing_signal.stop_price,
                    "tp": existing_signal.tp_json,
                    "created_at": str(existing_signal.created_at),
                    "expires_at": str(existing_signal.expires_at) if existing_signal.expires_at else None,
                }
                continue

            # Повторный ExposureGuard прямо перед публикацией.
            exposure = ExposureGuard()
            exposure_result = exposure.check_before_publish(
                db=db,
                bot_id=bot.id,
                symbol=symbol,
                required_margin=float(plan.required_margin or 0),
                equity_usdt=float(getattr(settings, "RISK_EQUITY_USDT", 950.0)),
                max_used_margin_pct=float(getattr(settings, "MAX_USED_MARGIN_PCT", 0.85)),
                max_active_signals=int(getattr(settings, "MAX_ACTIVE_SIGNALS", 2)),
                max_active_per_symbol=int(getattr(settings, "MAX_ACTIVE_SIGNALS_PER_SYMBOL", 1)),
            )

            if not exposure_result.allowed:
                item["exposure"] = {
                    "allowed": exposure_result.allowed,
                    "reason": exposure_result.reason,
                    "active_signals_count": exposure_result.active_signals_count,
                    "active_symbol_signals_count": exposure_result.active_symbol_signals_count,
                    "used_margin": exposure_result.used_margin,
                    "max_allowed_margin": exposure_result.max_allowed_margin,
                    "free_margin": exposure_result.free_margin,
                    "required_margin": exposure_result.required_margin,
                }

                replacement_result = None

                if exposure_result.reason in [
                    "max_active_signals_reached",
                    "required_margin_exceeds_free_margin",
                    "not_enough_free_margin",
                    "max_margin_exceeded",
                ]:
                    setup_score_for_replace = (item.get("setup_quality") or {}).get("final_score")
                    priority_score_for_replace = item.get("priority_score")

                    replacement_result = replacement_policy.check(
                        db=db,
                        bot_id=bot.id,
                        new_symbol=symbol,
                        new_side=side,
                        new_grade=grade,
                        new_priority_score=float(priority_score_for_replace or 0),
                        new_setup_score=float(setup_score_for_replace or 0),
                        new_rr_tp1=float(plan.net_rr_tp1 or 0),
                        new_rr_tp2=float(plan.net_rr_tp2 or 0),
                        new_required_margin=float(plan.required_margin or 0),
                    )

                    item["replacement"] = {
                        "allowed": replacement_result.allowed,
                        "reason": replacement_result.reason,
                        **replacement_result.payload,
                    }

                    if replacement_result.allowed and replacement_result.replace_signal_id:
                        old_signal = (
                            db.query(Signal)
                            .filter(
                                Signal.bot_id == bot.id,
                                Signal.id == replacement_result.replace_signal_id,
                                Signal.status == "published",
                            )
                            .first()
                        )

                        if old_signal:
                            old_signal.status = "replaced"
                            old_signal.closed_reason = "replaced_by_stronger_signal"
                            old_signal.closed_at = datetime.now(timezone.utc)

                            old_plan = old_signal.plan_json or {}
                            old_plan["replacement"] = {
                                "replaced_by_symbol": symbol,
                                "replaced_by_side": side,
                                "replaced_by_grade": grade,
                                "replaced_by_priority_score": priority_score_for_replace,
                                "reason": replacement_result.reason,
                            }
                            old_signal.plan_json = old_plan

                            db.flush()

                            # Повторяем exposure после освобождения published-слота.
                            exposure_result = exposure.check_before_publish(
                                db=db,
                                bot_id=bot.id,
                                symbol=symbol,
                                required_margin=float(plan.required_margin or 0),
                                equity_usdt=float(getattr(settings, "RISK_EQUITY_USDT", 950.0)),
                                max_used_margin_pct=float(getattr(settings, "MAX_USED_MARGIN_PCT", 0.85)),
                                max_active_signals=int(getattr(settings, "MAX_ACTIVE_SIGNALS", 2)),
                                max_active_per_symbol=int(getattr(settings, "MAX_ACTIVE_SIGNALS_PER_SYMBOL", 1)),
                            )

                            item["exposure_after_replacement"] = {
                                "allowed": exposure_result.allowed,
                                "reason": exposure_result.reason,
                                "active_signals_count": exposure_result.active_signals_count,
                                "active_symbol_signals_count": exposure_result.active_symbol_signals_count,
                                "used_margin": exposure_result.used_margin,
                                "max_allowed_margin": exposure_result.max_allowed_margin,
                                "free_margin": exposure_result.free_margin,
                                "required_margin": exposure_result.required_margin,
                            }

                if not exposure_result.allowed:
                    item["status"] = "blocked"
                    item["decision"] = exposure_result.reason
                    item["priority_publish_status"] = "skipped_exposure_before_publish"
                    continue

            if str(grade or "").upper() == "C":
                item["status"] = "wait"
                item["decision"] = "grade_c_blocked_before_signal_create"
                item["priority_publish_status"] = "skipped_grade_c_before_signal_create"
                continue

            setup_score = (item.get("setup_quality") or {}).get("final_score")
            priority_score = item.get("priority_score")

            # Финальный production gate.
            gate_result = production_gate.check(
                grade=grade,
                setup_score=setup_score,
                effective_confidence=effective_confidence,
                net_rr_tp1=plan.net_rr_tp1,
                net_rr_tp2=plan.net_rr_tp2,
                priority_score=priority_score,
            )

            if not gate_result.allowed:
                item["status"] = "wait"
                item["decision"] = gate_result.reason
                item["priority_publish_status"] = "skipped_by_production_entry_gate"
                item["production_gate"] = {
                    "allowed": gate_result.allowed,
                    "reason": gate_result.reason,
                    **gate_result.payload,
                }
                continue

            # Защита от повторного входа в ту же пару и сторону.
            cooldown_result = reentry_guard.check(
                db=db,
                bot_id=bot.id,
                symbol=symbol,
                side=side,
                current_priority_score=float(priority_score or 0),
                current_setup_score=float(setup_score or 0),
                current_rr_tp2=float(plan.net_rr_tp2 or 0),
            )

            if not cooldown_result.allowed:
                item["status"] = "wait"
                item["decision"] = cooldown_result.reason
                item["priority_publish_status"] = "skipped_by_reentry_cooldown"
                item["reentry_cooldown"] = {
                    "allowed": cooldown_result.allowed,
                    "reason": cooldown_result.reason,
                    **cooldown_result.payload,
                }
                continue

            sig = Signal(
                bot_id=bot.id,
                symbol=symbol,
                side=side,
                status="published",
                entry_zone_json={
                    "from": float(candidate["entry_from"]),
                    "to": float(candidate["entry_to"]),
                },
                stop_price=float(candidate["stop"]),
                tp_json={
                    "tp1": float(candidate["tp1"]),
                    "tp2": float(candidate["tp2"]),
                },
                confidence=effective_confidence,
                grade=grade,
                is_public=quality.should_publish_to_clients(
                    grade,
                    setup_score=(item.get("setup_quality") or {}).get("final_score"),
                    effective_confidence=effective_confidence,
                    setup_decision=item.get("setup_decision"),
                ),
                expires_at=expires_at,
                rationale=test_signal["reason"],

                qty=plan.qty,
                required_margin=plan.required_margin,
                net_rr_tp1=plan.net_rr_tp1,
                net_rr_tp2=plan.net_rr_tp2,
                net_pnl_tp1=plan.net_pnl_tp1,
                net_pnl_tp2=plan.net_pnl_tp2,
                net_pnl_stop=plan.net_pnl_stop,
                plan_json={
                    "qty": plan.qty,
                    "required_margin": plan.required_margin,
                    "net_pnl_tp1": plan.net_pnl_tp1,
                    "net_pnl_tp2": plan.net_pnl_tp2,
                    "net_pnl_stop": plan.net_pnl_stop,
                    "net_rr_tp1": plan.net_rr_tp1,
                    "net_rr_tp2": plan.net_rr_tp2,
                    "is_valid": plan.is_valid,
                    "reject_reason": plan.reject_reason,
                    "priority_score": item.get("priority_score"),
                    "priority_reason": item.get("priority_reason"),
                    "plan_balance_usdt": (item.get("plan") or {}).get("plan_balance_usdt"),
                    "performance_risk_multiplier": (item.get("plan") or {}).get("performance_risk_multiplier"),
                },
            )

            db.add(sig)
            db.flush()
            db.refresh(sig)

            try:
                await TelegramRouter().publish_new_signal(
                    signal=test_signal,
                    confidence=effective_confidence,
                    grade=grade,
                    signal_id=sig.id,
                    is_public=sig.is_public,
                )

            except Exception as telegram_error:
                error_text = f"{type(telegram_error).__name__}: {repr(telegram_error)}"
                live_delivery_required = bool(getattr(settings, "is_live_enabled", False)) or str(
                    getattr(settings, "TRADING_MODE", "paper_signal")
                ).lower().startswith("live")

                plan_json = sig.plan_json or {}
                plan_json["telegram_delivery"] = {
                    "ok": False,
                    "error": error_text,
                    "mode": "required_live" if live_delivery_required else "non_blocking_paper",
                    "vip_delivery_required": bool(sig.is_public),
                    "live_delivery_required": live_delivery_required,
                }
                sig.plan_json = plan_json

                if live_delivery_required:
                    sig.status = "telegram_failed"
                    sig.closed_reason = "initial_telegram_publish_failed"
                    item["status"] = "telegram_failed"
                    item["decision"] = "initial_telegram_publish_failed"
                    item["priority_publish_status"] = "telegram_failed_signal_not_activated"
                else:
                    item["status"] = "published"
                    item["decision"] = "published_signal_created"
                    item["priority_publish_status"] = "published_by_priority_queue_telegram_retry_pending"
                    published.append({
                        "signal_id": sig.id,
                        "symbol": symbol,
                        "side": side,
                        "grade": grade,
                        "priority_score": item.get("priority_score"),
                        "telegram_delivery": "retry_pending",
                    })
                    published_count += 1

                item["telegram_error"] = error_text
                item["signal_id"] = sig.id

                db.flush()
                continue

            item["status"] = "published"
            item["decision"] = "published_signal_created"
            item["priority_publish_status"] = "published_by_priority_queue"
            item["signal_id"] = sig.id

            published.append({
                "signal_id": sig.id,
                "symbol": symbol,
                "side": side,
                "grade": grade,
                "priority_score": item.get("priority_score"),
            })

            published_count += 1

        # Diagnostics: why candidates are not published in this scan.
        decision_counts = {}
        for r in results:
            d = str(r.get("decision") or "unknown")
            decision_counts[d] = decision_counts.get(d, 0) + 1

        rr_tp1_rejected_a = []
        for r in results:
            status = str(r.get("status") or "")
            if status not in {"wait", "rejected", "blocked"}:
                continue
            decision = str(r.get("decision") or "")
            if decision not in {
                "a_rr_tp1_too_low",
                "a_plus_rr_tp1_too_low",
                "quality_grade_too_low",
            }:
                continue
            gate = r.get("production_gate") or {}
            grade = str(gate.get("grade") or r.get("grade") or "")
            if grade in ["A", "A+"]:
                try:
                    rr_candidate = gate.get("net_rr_tp1")
                    if rr_candidate is None:
                        rr_candidate = (r.get("plan") or {}).get("net_rr_tp1")
                    if rr_candidate is not None:
                        rr_tp1_rejected_a.append(float(rr_candidate))
                except Exception:
                    pass

        rr_tp1_rejected_a.sort()
        median_rr_tp1_rejected_a = None
        if rr_tp1_rejected_a:
            n = len(rr_tp1_rejected_a)
            if n % 2 == 1:
                median_rr_tp1_rejected_a = rr_tp1_rejected_a[n // 2]
            else:
                median_rr_tp1_rejected_a = round((rr_tp1_rejected_a[n // 2 - 1] + rr_tp1_rejected_a[n // 2]) / 2.0, 6)

        ranked_summary = [
            {
                "symbol": item.get("symbol"),
                "action": item.get("action"),
                "status": item.get("status"),
                "decision": item.get("decision"),
                "grade": item.get("grade"),
                "effective_confidence": item.get("effective_confidence"),
                "setup_score": (item.get("setup_quality") or {}).get("final_score"),
                "net_rr_tp2": (item.get("plan") or {}).get("net_rr_tp2"),
                "priority_score": item.get("priority_score"),
                "priority_reason": item.get("priority_reason"),
                "priority_publish_status": item.get("priority_publish_status"),
                "signal_id": item.get("signal_id"),
                "production_gate": item.get("production_gate"),
                "reentry_cooldown": item.get("reentry_cooldown"),
                "replacement": item.get("replacement"),
                "exposure_after_replacement": item.get("exposure_after_replacement"),
            }
            for item in sorted(
                results,
                key=lambda x: float(x.get("priority_score") or 0),
                reverse=True,
            )
        ]

        db.commit()

        return {
            "status": "ok",
            "mode": "priority_publish",
            "symbols": bot.config_json.get("symbols", []),
            "published": published,
            "ranked": ranked_summary,
            "results": results,
            "diagnostics": {
                "decision_counts": decision_counts,
                "median_rr_tp1_rejected_a_a_plus": median_rr_tp1_rejected_a,
                "rejected_a_a_plus_rr_tp1_samples": rr_tp1_rejected_a,
            },
        }

    finally:
        db.close()
        INTELLIGENCE_PUBLISH_LOCK.release()

@app.get("/intelligence/funnel", dependencies=[Depends(require_owner_action)])
def intelligence_funnel(limit: int = 120):
    """
    Operator diagnostics for the candidate -> published -> open path.

    It explains why market intelligence keeps producing candidates/watch events
    while Signal rows do not become active published/open positions.
    """
    db = SessionLocal()

    try:
        return CandidateFunnelService().summarize(db, limit=limit)
    finally:
        db.close()


@app.get("/intelligence/events", dependencies=[Depends(require_owner_action)])
def intelligence_events(
    symbol: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    db = SessionLocal()

    try:
        limit = min(max(limit, 1), 200)
        offset = max(offset, 0)

        query = db.query(IntelligenceEvent)

        if symbol:
            query = query.filter(IntelligenceEvent.symbol == symbol)

        total = query.count()

        events = (
            query
            .order_by(IntelligenceEvent.id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [
                {
                    "id": e.id,
                    "symbol": e.symbol,
                    "status": e.status,
                    "decision": e.decision,
                    "action": e.action,
                    "regime": e.regime,
                    "radar_state": e.radar_state,
                    "confidence_hint": e.confidence_hint,
                    "setup_score": e.setup_score,
                    "created_at": str(e.created_at),
                    "payload": e.payload_json,
                }
                for e in events
            ],
        }

    finally:
        db.close()

def should_send_short_block_alert(db, symbol: str, minutes: int = 60) -> bool:
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)

    recent = (
        db.query(IntelligenceEvent)
        .filter(
            IntelligenceEvent.symbol == symbol,
            IntelligenceEvent.status == "blocked",
            IntelligenceEvent.decision.in_([
                "short_candidate_but_shorts_disabled",
                "robot_short_candidate_alert_sent",
            ]),
            IntelligenceEvent.created_at >= since,
        )
        .first()
    )

    return recent is None

def find_active_signal_for_symbol(db: Session, bot_id: int, symbol: str):
    if not symbol:
        return None

    return (
        db.query(Signal)
        .filter(
            Signal.bot_id == bot_id,
            Signal.symbol == symbol,
            Signal.status.in_(["published", "opened", "tp1", "breakeven"]),
        )
        .order_by(Signal.id.desc())
        .first()
    )


@app.post("/debug/exposure", dependencies=[Depends(require_owner_action), Depends(require_non_production_debug)])
def debug_exposure(payload: ExposureDebugRequest):
    db = SessionLocal()

    try:
        bot = db.query(Bot).filter(Bot.name == "Main Robot").first()

        if not bot:
            return {"status": "error", "error": "bot_not_found"}

        guard = ExposureGuard()

        result = guard.check_before_publish(
            db=db,
            bot_id=bot.id,
            symbol=payload.symbol,
            required_margin=float(payload.required_margin),
            equity_usdt=float(getattr(settings, "RISK_EQUITY_USDT", 950.0)),
            max_used_margin_pct=float(getattr(settings, "MAX_USED_MARGIN_PCT", 0.85)),
            max_active_signals=int(getattr(settings, "MAX_ACTIVE_SIGNALS", 2)),
            max_active_per_symbol=int(getattr(settings, "MAX_ACTIVE_SIGNALS_PER_SYMBOL", 1)),
        )

        return {
            "status": "ok",
            "symbol": payload.symbol,
            "exposure": {
                "allowed": result.allowed,
                "reason": result.reason,
                "active_signals_count": result.active_signals_count,
                "active_symbol_signals_count": result.active_symbol_signals_count,
                "used_margin": result.used_margin,
                "max_allowed_margin": result.max_allowed_margin,
                "free_margin": result.free_margin,
                "required_margin": result.required_margin,
            },
        }

    finally:
        db.close()
