import asyncio
import logging
from core.decision_codes import (
    DECISION_WAIT_BETTER_ENTRY_RR,
    DECISION_ACTIVE_SIGNAL_ALREADY_EXISTS,
    DECISION_MAX_ACTIVE_SIGNALS_REACHED,
    DECISION_REQUIRED_MARGIN_EXCEEDS_FREE_MARGIN,
)
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from datetime import datetime, timezone, timedelta
from threading import Lock

from contextlib import asynccontextmanager

from core.db import Base, engine, SessionLocal
from core.config import settings
from core.security import hash_password, require_owner_action
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
from models.audit_event import AuditEvent
from models.payment import BillingPlan, Payment, PaymentEvent
from models.funding_arbitrage import FundingArbOpportunity, FundingArbPosition

from workers.robot_loop import RobotLoop
from services.signal_broadcaster import SignalBroadcaster
from services.signal_lifecycle import SignalLifecycleManager
from services.signal_quality import SignalQualityService
from services.market_data import MarketDataService
from services.market_connectivity import MarketConnectivityService
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
from services.ml_outcome_stats import MLOutcomeStatsService
from services.candidate_priority import CandidatePriorityService
from services.reentry_cooldown import ReEntryCooldownGuard
from services.production_entry_gate import ProductionEntryGate
from services.signal_replacement import SignalReplacementPolicy
from services.candidate_funnel import CandidateFunnelService
from services.outcome_diagnostics import OutcomeDiagnosticsService
from services.telegram_bot_menu import TelegramBotMenuService
from services.audit_log import AuditLogService
from services.live_safety import LiveSafetyService
from services.funding_arbitrage import FundingMonitorService, FundingArbEngine

from pydantic import BaseModel

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

logger = get_logger(__name__)


async def background_subscription_loop():
    global subscription_loop_enabled

    await asyncio.sleep(10)

    while subscription_loop_enabled:
        db = SessionLocal()

        try:
            service = SubscriptionWatchdog()
            await service.check_subscriptions(db)
            db.commit()

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
            result = service.reconcile_pending(db)
            db.commit()

            if result.get("expired", 0) > 0:
                log_event(logger, logging.INFO, "payment_reconciliation", **result)

        except Exception as e:
            db.rollback()
            log_event(logger, logging.ERROR, "payment_reconciliation_loop_error", error_type=type(e).__name__, error=str(e))

        finally:
            db.close()

        await asyncio.sleep(60 * 60)


class CloseSignalRequest(BaseModel):
    result_pct: float
    reason: str = "manual_close"

class TestLifecyclePriceRequest(BaseModel):
    signal_id: int
    price: float

class CreateSubscriberRequest(BaseModel):
    telegram_user_id: str
    username: str | None = None
    full_name: str | None = None
    plan: str = "vip"
    days: int = 30
    is_trial: bool = False
    notes: str | None = None

class CostPreviewRequest(BaseModel):
    symbol: str = "BTC/USDT"
    market_type: str = "spot"
    side: str = "long"
    entry_price: float
    exit_price: float
    qty: float
    liquidity: str = "taker"
    holding_funding_periods: int = 1
    leverage: int | None = None

class TradePlanRequest(BaseModel):
    symbol: str
    side: str = "long"
    entry: float
    stop: float
    tp1: float
    tp2: float
    balance_usdt: float = 1000

class ExtendSubscriberRequest(BaseModel):
    days: int = 30


class UpdateSubscriberStatusRequest(BaseModel):
    status: str

class ExposureDebugRequest(BaseModel):
    symbol: str = "BTC/USDT"
    required_margin: float = 333.0


class TelegramWebhookRequest(BaseModel):
    update_id: int | None = None
    message: dict | None = None
    callback_query: dict | None = None


class CreateCheckoutRequest(BaseModel):
    telegram_user_id: str
    plan_code: str = "vip_30"
    username: str | None = None
    full_name: str | None = None
    provider: str = "manual"
    notes: str | None = None


class ManualConfirmPaymentRequest(BaseModel):
    provider_event_id: str | None = None
    raw_payload: str | None = None


class PaymentEventRequest(BaseModel):
    payment_id: int
    provider: str = "manual"
    provider_event_id: str
    status: str = "paid"
    raw_payload: str | None = None


class PaymentReconcileRequest(BaseModel):
    older_than_hours: int | None = None


class KillSwitchRequest(BaseModel):
    enabled: bool = True
    reason: str | None = "owner_request"


class FundingArbScanRequest(BaseModel):
    symbols: list[str] | None = None


class FundingArbOpenRequest(BaseModel):
    opportunity_id: int
    notional_usdt: float | None = None
    mode: str = "paper"


class FundingArbCloseRequest(BaseModel):
    spot_exit_price: float
    swap_exit_price: float
    funding_periods: int = 1
    exit_funding_rate: float | None = None

async def background_robot_loop():
    global robot_loop_enabled

    await asyncio.sleep(5)

    loop = RobotLoop()

    while robot_loop_enabled:
        db = SessionLocal()

        try:
            bot = db.query(Bot).filter(Bot.name == "Main Robot").first()

            if bot and bot.status == "running":
                safety = LiveSafetyService().enforce(db=db, bot=bot, equity_usdt=1000)

                if safety.get("blocked"):
                    db.commit()
                    log_event(logger, logging.WARNING, "robot_loop_safety_skip", **safety)
                else:
                    await loop.step(
                        db=db,
                        bot=bot,
                        headlines=[],
                        balance_usdt=1000,
                        daily_loss_pct=safety.get("daily_loss_pct", 0),
                        drawdown_pct=0,
                    )
                    db.commit()

        except Exception as e:
            db.rollback()
            log_event(logger, logging.ERROR, "robot_loop_error", error_type=type(e).__name__, error=str(e))

        finally:
            db.close()

        await asyncio.sleep(60)



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
    global robot_task, robot_loop_enabled, subscription_task, subscription_loop_enabled, telegram_delivery_task, telegram_delivery_loop_enabled, payment_reconciliation_task, payment_reconciliation_loop_enabled

    initialize_database_schema()
    bootstrap_owner_and_bot()
    bootstrap_billing_plans()

    robot_loop_enabled = True
    robot_task = asyncio.create_task(background_robot_loop())

    subscription_loop_enabled = True
    subscription_task = asyncio.create_task(background_subscription_loop())

    telegram_delivery_loop_enabled = True
    telegram_delivery_task = asyncio.create_task(background_telegram_delivery_loop())

    payment_reconciliation_loop_enabled = True

    payment_reconciliation_task = asyncio.create_task(background_payment_reconciliation_loop())

    yield

    robot_loop_enabled = False
    if robot_task:
        robot_task.cancel()

    subscription_loop_enabled = False
    if subscription_task:
        subscription_task.cancel()

    telegram_delivery_loop_enabled = False
    if telegram_delivery_task:
        telegram_delivery_task.cancel()

    payment_reconciliation_loop_enabled = False
    if payment_reconciliation_task:
        payment_reconciliation_task.cancel()


app = FastAPI(title="Robot V1 API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    }


@app.get("/bot/state")
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
        return {"status": "running", "live_safety": live_safety}

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


@app.get("/signals")
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


@app.get("/positions")
def list_positions():
    db = SessionLocal()

    try:
        positions = db.query(Position).order_by(Position.id.desc()).limit(50).all()

        return [
            {
                "id": p.id,
                "symbol": p.symbol,
                "side": p.side,
                "qty": p.qty,
                "entry_price": p.entry_price,
                "mark_price": p.mark_price,
                "unrealized_pnl": p.unrealized_pnl,
                "status": p.status,
                "signal_id": p.signal_id,
            }
            for p in positions
        ]

    finally:
        db.close()


@app.get("/orders")
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

@app.get("/robot/loop-state")
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


@app.post("/telegram/test-owner", dependencies=[Depends(require_owner_action)])
async def telegram_test_owner():
    broadcaster = SignalBroadcaster()
    await broadcaster.send_owner_alert(
        "ROBOT V1 ONLINE",
        "Owner alert работает. Telegram подключен."
    )
    return {"status": "sent"}

@app.get("/robot/debug-signals", dependencies=[Depends(require_owner_action)])
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

@app.post("/robot/force-paper-signal", dependencies=[Depends(require_owner_action)])
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

@app.get("/analytics/summary")
def analytics_summary():
    db = SessionLocal()

    try:
        total = db.query(Signal).count()

        closed_signals = (
            db.query(Signal)
            .filter(Signal.status == "closed")
            .all()
        )

        active_statuses = ["published", "opened", "tp1", "breakeven"]

        active_signals = (
            db.query(Signal)
            .filter(Signal.status.in_(active_statuses))
            .all()
        )

        expired = db.query(Signal).filter(Signal.status == "expired").count()
        rejected = db.query(Signal).filter(Signal.status == "rejected").count()
        telegram_failed = db.query(Signal).filter(Signal.status == "telegram_failed").count()
        queued = db.query(Signal).filter(Signal.status == "queued").count()

        wins = 0
        losses = 0

        total_result_pct = 0.0
        total_net_pnl = 0.0
        total_costs = 0.0

        closed_with_money = 0

        for s in closed_signals:
            result_pct = float(s.result_pct or 0)
            total_result_pct += result_pct

            net_pnl = s.closed_net_pnl

            if net_pnl is not None:
                net_pnl = float(net_pnl)
                total_net_pnl += net_pnl
                closed_with_money += 1

                if net_pnl > 0:
                    wins += 1
                else:
                    losses += 1
            else:
                if result_pct > 0:
                    wins += 1
                else:
                    losses += 1

            if s.closed_total_cost is not None:
                total_costs += float(s.closed_total_cost)

        closed_count = len(closed_signals)
        winrate = round((wins / closed_count * 100), 2) if closed_count else 0.0
        avg_net_pnl = round((total_net_pnl / closed_with_money), 6) if closed_with_money else 0.0

        guard = ExposureGuard()
        bot = db.query(Bot).filter(Bot.name == "Main Robot").first()

        used_margin = 0.0
        max_allowed_margin = 0.0
        free_margin = 0.0

        if bot:
            equity_usdt = float(getattr(settings, "RISK_EQUITY_USDT", 950.0))
            max_used_margin_pct = float(getattr(settings, "MAX_USED_MARGIN_PCT", 0.85))

            used_margin = guard.used_margin(db, bot.id)
            max_allowed_margin = round(equity_usdt * max_used_margin_pct, 6)
            free_margin = round(max_allowed_margin - used_margin, 6)

        return {
            "total_signals": total,
            "active_signals": len(active_signals),
            "closed_signals": closed_count,
            "expired_signals": expired,
            "rejected_signals": rejected,
            "telegram_failed_signals": telegram_failed,
            "queued_signals": queued,

            "wins": wins,
            "losses": losses,
            "winrate": winrate,

            "total_result_pct": round(total_result_pct, 4),
            "total_net_pnl_usdt": round(total_net_pnl, 6),
            "avg_net_pnl_usdt": avg_net_pnl,
            "total_costs_usdt": round(total_costs, 6),

            "exposure": {
                "used_margin": used_margin,
                "max_allowed_margin": max_allowed_margin,
                "free_margin": free_margin,
                "active_signals_count": len(active_signals),
            },
        }

    finally:
        db.close()


@app.get("/analytics/reason-breakdown")
def analytics_reason_breakdown(limit: int = 500):
    """
    Причины закрытия сделок с деньгами/метриками.
    Нужен для быстрого контроля утечек PnL (например failed_setup_exit).
    """
    db = SessionLocal()

    try:
        limit = min(max(limit, 50), 5000)

        signals = (
            db.query(Signal)
            .filter(Signal.status == "closed")
            .order_by(Signal.id.desc())
            .limit(limit)
            .all()
        )

        rows = {}
        total_net = 0.0
        total_count = len(signals)

        for s in signals:
            reason = str(s.closed_reason or "unknown")
            result_pct = float(s.result_pct or 0.0)
            net = float(s.closed_net_pnl or 0.0)
            cost = float(s.closed_total_cost or 0.0)

            total_net += net

            if reason not in rows:
                rows[reason] = {
                    "reason": reason,
                    "count": 0,
                    "wins": 0,
                    "losses": 0,
                    "sum_result_pct": 0.0,
                    "sum_net_pnl_usdt": 0.0,
                    "sum_costs_usdt": 0.0,
                }

            row = rows[reason]
            row["count"] += 1
            row["sum_result_pct"] += result_pct
            row["sum_net_pnl_usdt"] += net
            row["sum_costs_usdt"] += cost

            if net > 0:
                row["wins"] += 1
            else:
                row["losses"] += 1

        items = []
        for reason, row in rows.items():
            count = row["count"] or 1
            share = round((row["count"] / total_count) * 100, 2) if total_count else 0.0
            avg_net = row["sum_net_pnl_usdt"] / count
            avg_result = row["sum_result_pct"] / count
            pnl_share = round((row["sum_net_pnl_usdt"] / total_net) * 100, 2) if total_net else 0.0

            items.append({
                "reason": reason,
                "count": row["count"],
                "share_pct": share,
                "wins": row["wins"],
                "losses": row["losses"],
                "avg_result_pct": round(avg_result, 4),
                "sum_net_pnl_usdt": round(row["sum_net_pnl_usdt"], 6),
                "avg_net_pnl_usdt": round(avg_net, 6),
                "sum_costs_usdt": round(row["sum_costs_usdt"], 6),
                "pnl_share_pct": pnl_share,
            })

        # худшие по net-pnl причины — в начале.
        items.sort(key=lambda x: (x["sum_net_pnl_usdt"], -x["count"]))

        return {
            "status": "ok",
            "sample_closed_signals": total_count,
            "total_net_pnl_usdt": round(total_net, 6),
            "items": items,
        }
    finally:
        db.close()

@app.get("/analytics/outcome-root-cause")
def analytics_outcome_root_cause(reason: str = "failed_setup_exit", limit: int = 500):
    """Root-cause report for repeated losing exit reasons (roadmap Phase 1)."""
    db = SessionLocal()

    try:
        return OutcomeDiagnosticsService().root_cause(db, reason=reason, limit=limit)
    finally:
        db.close()


@app.get("/analytics/symbol-performance")
def analytics_symbol_performance(lookback: int = 12):
    """Per-symbol profitability guard report for roadmap P1 operations."""
    db = SessionLocal()

    try:
        bot = db.query(Bot).filter(Bot.name == "Main Robot").first()
        return SymbolPerformanceSummaryService().summarize(db, bot=bot, lookback=lookback)
    finally:
        db.close()


@app.post("/signals/{signal_id}/close")
async def close_signal(signal_id: int, payload: CloseSignalRequest):
    db = SessionLocal()

    try:
        sig = db.query(Signal).filter(Signal.id == signal_id).first()

        if not sig:
            return {"status": "error", "error": "signal_not_found"}

        sig.status = "closed"
        sig.result_pct = payload.result_pct

        db.commit()

        broadcaster = SignalBroadcaster()

        emoji = "✅" if payload.result_pct > 0 else "🛑"

        await broadcaster.send_signal_update(
            symbol=sig.symbol,
            text_status=f"{emoji} Позиция закрыта",
            extra=f"Результат: {payload.result_pct}%\nПричина: {payload.reason}"
        )

        return {
            "status": "closed",
            "signal_id": sig.id,
            "result_pct": sig.result_pct,
        }

    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}

    finally:
        db.close()

@app.post("/robot/force-live-near-signal", dependencies=[Depends(require_owner_action)])
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

@app.post("/signals/maintenance/queued-to-published")
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

@app.post("/robot/force-scalp-signal", dependencies=[Depends(require_owner_action)])
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

@app.post("/robot/test-lifecycle-price", dependencies=[Depends(require_owner_action)])
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

@app.get("/reports/summary")
def report_summary(hours: int = 24):
    db = SessionLocal()

    try:
        service = ReportService()
        return service.collect_stats(db, hours)

    finally:
        db.close()


@app.post("/reports/send-owner")
async def send_owner_report(hours: int = 24):
    db = SessionLocal()

    try:
        service = ReportService()
        stats = await service.send_owner_report(db, hours)
        return {"status": "sent", "stats": stats}

    finally:
        db.close()


@app.post("/reports/send-free")
async def send_free_report(hours: int = 24):
    db = SessionLocal()

    try:
        service = ReportService()
        stats = await service.send_free_report(db, hours)
        return {"status": "sent", "stats": stats}

    finally:
        db.close()


@app.post("/reports/send-vip")
async def send_vip_report(hours: int = 24):
    db = SessionLocal()

    try:
        service = ReportService()
        stats = await service.send_vip_report(db, hours)
        return {"status": "sent", "stats": stats}

    finally:
        db.close()


@app.post("/reports/send-all")
async def send_all_reports(hours: int = 24):
    db = SessionLocal()

    try:
        service = ReportService()
        stats = await service.send_all_reports(db, hours)
        return {"status": "sent", "stats": stats}

    finally:
        db.close()

@app.get("/subscribers")
def list_subscribers():
    db = SessionLocal()

    try:
        now = datetime.now(timezone.utc)
        subs = db.query(Subscriber).order_by(Subscriber.id.desc()).all()

        return [
            {
                "id": s.id,
                "telegram_user_id": s.telegram_user_id,
                "username": s.username,
                "full_name": s.full_name,
                "plan": s.plan,
                "status": s.status,
                "is_trial": s.is_trial,
                "starts_at": str(s.starts_at),
                "expires_at": str(s.expires_at),
                "days_left": max((s.expires_at - now).days, 0) if s.expires_at else 0,
                "notes": s.notes,
                "created_at": str(s.created_at),
            }
            for s in subs
        ]

    finally:
        db.close()


@app.post("/subscribers", dependencies=[Depends(require_owner_action)])
async def create_subscriber(payload: CreateSubscriberRequest):
    db = SessionLocal()

    try:
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=payload.days)

        existing = (
            db.query(Subscriber)
            .filter(Subscriber.telegram_user_id == payload.telegram_user_id)
            .first()
        )

        if existing:
            existing.username = payload.username or existing.username
            existing.full_name = payload.full_name or existing.full_name
            existing.plan = payload.plan
            existing.status = "active"
            existing.expires_at = expires_at
            existing.is_trial = payload.is_trial
            existing.notes = payload.notes
            sub = existing
        else:
            sub = Subscriber(
                telegram_user_id=payload.telegram_user_id,
                username=payload.username,
                full_name=payload.full_name,
                plan=payload.plan,
                status="active",
                starts_at=now,
                expires_at=expires_at,
                is_trial=payload.is_trial,
                notes=payload.notes,
            )
            db.add(sub)

        db.commit()
        db.refresh(sub)

        router = TelegramRouter()
        await router.owner_alert(
            "SUBSCRIBER ACTIVE",
            f"{sub.full_name or sub.username or sub.telegram_user_id}\n"
            f"Plan: {sub.plan}\n"
            f"Expires: {sub.expires_at}"
        )

        return {
            "status": "ok",
            "subscriber_id": sub.id,
            "expires_at": str(sub.expires_at),
        }

    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}

    finally:
        db.close()


@app.post("/subscribers/{subscriber_id}/extend", dependencies=[Depends(require_owner_action)])
async def extend_subscriber(subscriber_id: int, payload: ExtendSubscriberRequest):
    db = SessionLocal()

    try:
        sub = db.query(Subscriber).filter(Subscriber.id == subscriber_id).first()

        if not sub:
            return {"status": "error", "error": "subscriber_not_found"}

        now = datetime.now(timezone.utc)
        base = sub.expires_at if sub.expires_at and sub.expires_at > now else now
        sub.expires_at = base + timedelta(days=payload.days)
        sub.status = "active"

        db.commit()

        router = TelegramRouter()
        await router.owner_alert(
            "SUBSCRIBER EXTENDED",
            f"{sub.full_name or sub.username or sub.telegram_user_id}\n"
            f"+{payload.days} days\n"
            f"New expiry: {sub.expires_at}"
        )

        return {
            "status": "ok",
            "subscriber_id": sub.id,
            "expires_at": str(sub.expires_at),
        }

    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}

    finally:
        db.close()


@app.post("/subscribers/{subscriber_id}/status", dependencies=[Depends(require_owner_action)])
def update_subscriber_status(subscriber_id: int, payload: UpdateSubscriberStatusRequest):
    db = SessionLocal()

    try:
        sub = db.query(Subscriber).filter(Subscriber.id == subscriber_id).first()

        if not sub:
            return {"status": "error", "error": "subscriber_not_found"}

        sub.status = payload.status
        db.commit()

        return {
            "status": "ok",
            "subscriber_id": sub.id,
            "new_status": sub.status,
        }

    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}

    finally:
        db.close()

@app.post("/subscribers/check-expirations", dependencies=[Depends(require_owner_action)])
async def check_subscriber_expirations():
    db = SessionLocal()

    try:
        service = SubscriptionWatchdog()
        result = await service.check_subscriptions(db)
        db.commit()
        return {"status": "ok", "result": result}

    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}

    finally:
        db.close()

@app.get("/system/health")
def system_health():
    db = SessionLocal()

    try:
        bot = db.query(Bot).filter(Bot.name == "Main Robot").first()

        total_signals = db.query(Signal).count()
        published_signals = db.query(Signal).filter(Signal.status == "published").count()
        opened_signals = db.query(Signal).filter(Signal.status == "opened").count()
        tp1_signals = db.query(Signal).filter(Signal.status == "tp1").count()
        closed_signals = db.query(Signal).filter(Signal.status == "closed").count()
        expired_signals = db.query(Signal).filter(Signal.status == "expired").count()

        active_subscribers = db.query(Subscriber).filter(Subscriber.status == "active").count()
        expired_subscribers = db.query(Subscriber).filter(Subscriber.status == "expired").count()
        blocked_subscribers = db.query(Subscriber).filter(Subscriber.status == "blocked").count()
        telegram_delivery = TelegramDeliveryLog().summary(db, hours=24)
        payments_summary = BillingService().summary(db)
        revenue = RevenueMetricsService().summary(db)
        funding_arb = FundingArbEngine().summary(db)
        live_safety = LiveSafetyService().snapshot(db=db, bot=bot)
        ml_outcomes = MLOutcomeStatsService().safe_summary()
        production_blockers = settings.production_blockers()

        market_status = MarketConnectivityService().check("BTC/USDT")

        return {
            "api": {
                "ok": True,
                "env": settings.APP_ENV,
                "mode": settings.ROBOT_MODE,
            },
            "bot": {
                "id": bot.id if bot else None,
                "name": bot.name if bot else None,
                "status": bot.status if bot else None,
                "mode": bot.mode if bot else None,
            },
            "loops": {
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
            },
            "market": market_status,
            "signals": {
                "total": total_signals,
                "published": published_signals,
                "opened": opened_signals,
                "tp1": tp1_signals,
                "closed": closed_signals,
                "expired": expired_signals,
            },
            "subscribers": {
                "active": active_subscribers,
                "expired": expired_subscribers,
                "blocked": blocked_subscribers,
            },
            "telegram_delivery": telegram_delivery,
            "payments": payments_summary,
            "revenue": revenue,
            "funding_arb": funding_arb,
            "live_safety": live_safety,
            "ml_outcomes": ml_outcomes,
            "production_readiness": {
                "ready": len(production_blockers) == 0,
                "blockers": production_blockers,
                "live_enabled": settings.is_live_enabled,
            },
        }

    finally:
        db.close()


@app.get("/audit/events")
def list_audit_events(limit: int = 100, action: str | None = None):
    db = SessionLocal()
    try:
        limit = min(max(limit, 1), 500)
        query = db.query(AuditEvent)
        if action:
            query = query.filter(AuditEvent.action == action)
        events = query.order_by(AuditEvent.id.desc()).limit(limit).all()
        service = AuditLogService()
        return {"items": [service.serialize(event) for event in events]}
    finally:
        db.close()

@app.get("/payments/plans")
def list_payment_plans():
    db = SessionLocal()
    try:
        service = BillingService()
        plans = service.list_plans(db)
        db.commit()
        return [service.serialize_plan(plan) for plan in plans]
    finally:
        db.close()


@app.post("/payments/checkout", dependencies=[Depends(require_owner_action)])
def create_payment_checkout(payload: CreateCheckoutRequest):
    db = SessionLocal()
    try:
        service = BillingService()
        payment = service.create_checkout(
            db=db,
            telegram_user_id=payload.telegram_user_id,
            plan_code=payload.plan_code,
            username=payload.username,
            full_name=payload.full_name,
            provider=payload.provider,
            notes=payload.notes,
        )
        db.commit()
        db.refresh(payment)
        return {"status": "ok", "payment": service.serialize_payment(payment)}
    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        db.close()


@app.get("/payments")
def list_payments(limit: int = 100, status: str | None = None):
    db = SessionLocal()
    try:
        limit = min(max(limit, 1), 500)
        query = db.query(Payment)
        if status:
            query = query.filter(Payment.status == status)
        payments = query.order_by(Payment.id.desc()).limit(limit).all()
        service = BillingService()
        return {
            "summary": service.summary(db),
            "items": [service.serialize_payment(payment) for payment in payments],
        }
    finally:
        db.close()


@app.post("/payments/{payment_id}/manual-confirm", dependencies=[Depends(require_owner_action)])
async def manual_confirm_payment(payment_id: int, payload: ManualConfirmPaymentRequest | None = None):
    db = SessionLocal()
    try:
        service = BillingService()
        event = None
        if payload and payload.provider_event_id:
            payment, subscriber, activated, event = service.process_payment_event(
                db=db,
                payment_id=payment_id,
                provider="manual",
                provider_event_id=payload.provider_event_id,
                status="paid",
                raw_payload=payload.raw_payload,
            )
        else:
            payment, subscriber, activated = service.confirm_payment(
                db=db,
                payment_id=payment_id,
                raw_payload=payload.raw_payload if payload else None,
            )
        notification = CustomerNotificationService().queue_payment_success(db, payment, subscriber, activated)
        AuditLogService().record(
            db,
            action="payment_manual_confirm",
            resource_type="payment",
            resource_id=payment.id,
            details={"activated": activated, "subscriber_id": subscriber.id, "customer_notification": notification},
        )
        db.commit()
        await TelegramRouter().owner_alert(
            "PAYMENT CONFIRMED",
            (
                f"Payment #{payment.id} {payment.amount} {payment.currency}\n"
                f"User: {subscriber.telegram_user_id}\n"
                f"Plan: {subscriber.plan}\n"
                f"Expires: {subscriber.expires_at}\n"
                f"Activated now: {activated}"
            ),
        )
        return {
            "status": "ok",
            "activated": activated,
            "payment": service.serialize_payment(payment),
            "subscriber_id": subscriber.id,
            "expires_at": str(subscriber.expires_at),
            "customer_notification": notification,
            "payment_event": service.serialize_payment_event(event) if event else None,
        }
    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        db.close()


@app.get("/payments/events")
def list_payment_events(limit: int = 100):
    db = SessionLocal()
    try:
        limit = min(max(limit, 1), 500)
        service = BillingService()
        events = db.query(PaymentEvent).order_by(PaymentEvent.id.desc()).limit(limit).all()
        return {
            "items": [service.serialize_payment_event(event) for event in events],
            "summary": service.summary(db),
        }
    finally:
        db.close()


@app.post("/payments/events", dependencies=[Depends(require_owner_action)])
def process_payment_event(payload: PaymentEventRequest):
    db = SessionLocal()
    try:
        service = BillingService()
        payment, subscriber, activated, event = service.process_payment_event(
            db=db,
            payment_id=payload.payment_id,
            provider=payload.provider,
            provider_event_id=payload.provider_event_id,
            status=payload.status,
            raw_payload=payload.raw_payload,
        )
        notification = CustomerNotificationService().queue_payment_success(db, payment, subscriber, activated)
        AuditLogService().record(
            db,
            action="payment_event_processed",
            resource_type="payment",
            resource_id=payment.id,
            details={"event_id": event.id, "status": event.status, "activated": activated, "customer_notification": notification},
        )
        db.commit()
        db.refresh(payment)
        db.refresh(event)
        return {
            "status": "ok",
            "activated": activated,
            "payment": service.serialize_payment(payment),
            "subscriber_id": subscriber.id if subscriber else None,
            "customer_notification": notification,
            "payment_event": service.serialize_payment_event(event),
        }
    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        db.close()


@app.get("/payments/summary")
def payments_summary():
    db = SessionLocal()
    try:
        return BillingService().summary(db)
    finally:
        db.close()


@app.post("/payments/reconcile", dependencies=[Depends(require_owner_action)])
def reconcile_payments(payload: PaymentReconcileRequest | None = None):
    db = SessionLocal()
    try:
        result = PaymentReconciliationService().reconcile_pending(
            db,
            older_than_hours=payload.older_than_hours if payload else None,
        )
        if result.get("expired", 0) > 0:
            AuditLogService().record(
                db,
                action="payment_reconciliation",
                resource_type="payment",
                details=result,
            )
        db.commit()
        return result
    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        db.close()


@app.get("/payments/revenue")
def payments_revenue(window_days: int = 30):
    db = SessionLocal()
    try:
        window_days = min(max(window_days, 1), 365)
        return RevenueMetricsService().summary(db, window_days=window_days)
    finally:
        db.close()

@app.get("/funding-arb/summary")
def funding_arb_summary():
    db = SessionLocal()
    try:
        return FundingArbEngine().summary(db)
    finally:
        db.close()


@app.post("/funding-arb/scan", dependencies=[Depends(require_owner_action)])
def funding_arb_scan(payload: FundingArbScanRequest | None = None):
    db = SessionLocal()
    try:
        result = FundingMonitorService().scan(db, symbols=payload.symbols if payload else None)
        db.commit()
        return result
    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        db.close()


@app.get("/funding-arb/opportunities")
def funding_arb_opportunities(limit: int = 50, status: str | None = None):
    db = SessionLocal()
    try:
        limit = min(max(limit, 1), 200)
        query = db.query(FundingArbOpportunity)
        if status:
            query = query.filter(FundingArbOpportunity.status == status)
        items = query.order_by(FundingArbOpportunity.id.desc()).limit(limit).all()
        monitor = FundingMonitorService()
        return {"items": [monitor.serialize_opportunity(item) for item in items]}
    finally:
        db.close()


@app.get("/funding-arb/positions")
def funding_arb_positions(limit: int = 50, status: str | None = None):
    db = SessionLocal()
    try:
        limit = min(max(limit, 1), 200)
        query = db.query(FundingArbPosition)
        if status:
            query = query.filter(FundingArbPosition.status == status)
        items = query.order_by(FundingArbPosition.id.desc()).limit(limit).all()
        engine = FundingArbEngine()
        return {"items": [engine.serialize_position(item) for item in items]}
    finally:
        db.close()


@app.post("/funding-arb/open", dependencies=[Depends(require_owner_action)])
def funding_arb_open(payload: FundingArbOpenRequest):
    db = SessionLocal()
    try:
        position = FundingArbEngine().open_hedge(
            db,
            opportunity_id=payload.opportunity_id,
            notional_usdt=payload.notional_usdt,
            mode=payload.mode,
        )
        AuditLogService().record(
            db,
            action="funding_arb_opened",
            resource_type="funding_arb_position",
            resource_id=position.id,
            details={"symbol": position.symbol, "notional_usdt": position.notional_usdt, "mode": position.mode},
        )
        db.commit()
        db.refresh(position)
        return {"status": "ok", "position": FundingArbEngine().serialize_position(position)}
    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        db.close()


@app.post("/funding-arb/{position_id}/close", dependencies=[Depends(require_owner_action)])
def funding_arb_close(position_id: int, payload: FundingArbCloseRequest):
    db = SessionLocal()
    try:
        position = FundingArbEngine().close_paper(
            db,
            position_id=position_id,
            spot_exit_price=payload.spot_exit_price,
            swap_exit_price=payload.swap_exit_price,
            funding_periods=payload.funding_periods,
            exit_funding_rate=payload.exit_funding_rate,
        )
        AuditLogService().record(
            db,
            action="funding_arb_closed",
            resource_type="funding_arb_position",
            resource_id=position.id,
            details={"symbol": position.symbol, "realized_pnl": position.realized_pnl},
        )
        db.commit()
        db.refresh(position)
        return {"status": "ok", "position": FundingArbEngine().serialize_position(position)}
    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        db.close()


@app.get("/system/live-safety")
def system_live_safety():
    db = SessionLocal()

    try:
        bot = db.query(Bot).filter(Bot.name == "Main Robot").first()
        return LiveSafetyService().snapshot(db=db, bot=bot)

    finally:
        db.close()


@app.post("/system/kill-switch", dependencies=[Depends(require_owner_action)])
def system_kill_switch(payload: KillSwitchRequest):
    db = SessionLocal()

    try:
        bot = db.query(Bot).filter(Bot.name == "Main Robot").first()
        if not bot:
            return {"status": "error", "error": "bot_not_found"}

        state = LiveSafetyService().set_kill_switch(
            db=db,
            bot=bot,
            enabled=payload.enabled,
            reason=payload.reason,
        )
        db.commit()
        return {"status": "ok", "live_safety": state}

    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}

    finally:
        db.close()


@app.get("/system/readiness")
def system_readiness():
    db = SessionLocal()

    try:
        analytics = analytics_summary()
        telegram_delivery = TelegramDeliveryLog().summary(db, hours=24)
        payments_summary = BillingService().summary(db)
        revenue = RevenueMetricsService().summary(db)
        funding_arb = FundingArbEngine().summary(db)
        bot = db.query(Bot).filter(Bot.name == "Main Robot").first()
        live_safety = LiveSafetyService().snapshot(db=db, bot=bot)
        ml_outcomes = MLOutcomeStatsService().safe_summary()
        market_connectivity = MarketConnectivityService().check("BTC/USDT")
        blockers = list(settings.production_blockers())

        if analytics.get("total_net_pnl_usdt", 0) < 0:
            blockers.append("rolling net PnL is negative")
        if analytics.get("closed_signals", 0) < 200 and settings.is_live_enabled:
            blockers.append("live mode requires at least 200 closed validation signals")
        if telegram_delivery.get("failed", 0) > 0:
            blockers.append("telegram delivery has failures in the last 24h")
        blockers.extend(live_safety.get("blockers", []))
        if ml_outcomes.get("status") not in ["ok", "empty"]:
            blockers.append("ML outcomes summary is degraded")
        if market_connectivity.get("breaker_blocked"):
            blockers.extend(market_connectivity.get("blockers") or ["market connectivity breaker is blocked"])
        if settings.ENABLE_FUNDING_ARB and not settings.ENABLE_FUTURES:
            blockers.append("funding arbitrage requires ENABLE_FUTURES=true")

        return {
            "status": "ready" if not blockers else "blocked",
            "ready": not blockers,
            "blockers": blockers,
            "analytics": analytics,
            "telegram_delivery": telegram_delivery,
            "payments": payments_summary,
            "revenue": revenue,
            "funding_arb": funding_arb,
            "live_safety": live_safety,
            "ml_outcomes": ml_outcomes,
            "market_connectivity": market_connectivity,
            "required_gates": {
                "closed_validation_signals": 200,
                "failed_setup_exit_share_max_pct": 35.0,
                "positive_then_negative_max_pct": 25.0,
                "telegram_delivery_sla_min_pct": 99.0,
                "market_connectivity_max_latency_ms": getattr(settings, "MARKET_CONNECTIVITY_MAX_LATENCY_MS", 5000),
                "market_connectivity_max_spread_pct": getattr(settings, "MARKET_CONNECTIVITY_MAX_SPREAD_PCT", 0.75),
            },
        }

    finally:
        db.close()


def _telegram_menu_text(command: str, subscriber: Subscriber | None = None) -> str:
    command = command.lower().strip()

    if command in ["/start", "/menu"]:
        return (
            "🤖 Finmt Robot\n\n"
            "Меню:\n"
            "/plans — тарифы VIP\n"
            "/pay — как оплатить доступ\n"
            "/status — статус подписки\n"
            "/help — FAQ и риски\n"
            "/support — поддержка"
        )

    if command == "/plans":
        return (
            "💎 VIP планы\n\n"
            "VIP 30 дней — полный сигнал, уровни, сопровождение и отчёты.\n"
            "VIP 90 дней — тот же доступ с долгим периодом.\n\n"
            "Нажмите /pay для инструкции по оплате."
        )

    if command == "/pay":
        return (
            "💳 Оплата VIP\n\n"
            "Напишите /pay vip_30 или /pay vip_90, чтобы создать pending checkout. "
            "Owner сможет подтвердить оплату в разделе Payments."
        )

    if command == "/status":
        if not subscriber:
            return "Статус: подписка не найдена. Нажмите /plans или /pay."
        return (
            "📌 Статус подписки\n\n"
            f"Plan: {subscriber.plan}\n"
            f"Status: {subscriber.status}\n"
            f"Expires: {subscriber.expires_at}"
        )

    if command == "/help":
        return (
            "ℹ️ FAQ и риски\n\n"
            "Сигналы не являются финансовой рекомендацией. "
            "Используйте риск-менеджмент и не торгуйте средствами, которые не готовы потерять. "
            "Перед live-режимом система проходит paper/live-shadow gates."
        )

    if command == "/support":
        return "Поддержка: напишите owner/admin канала с вашим Telegram ID."

    return "Неизвестная команда. Нажмите /menu."


@app.post("/telegram/webhook")
async def telegram_webhook(payload: TelegramWebhookRequest):
    db = SessionLocal()
    try:
        response = TelegramBotMenuService().handle(
            db=db,
            message=payload.message,
            callback_query=payload.callback_query,
        )
        db.commit()

        if response.chat_id:
            await SignalBroadcaster().send_message(
                chat_id=response.chat_id,
                text=response.text,
                message_type=response.message_type,
                reply_markup=response.reply_markup,
            )

        return {
            "status": "ok",
            "command": response.command,
            "telegram_user_id": response.telegram_user_id,
            "message_type": response.message_type,
        }

    except Exception as e:
        db.rollback()
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}

    finally:
        db.close()


@app.get("/telegram/deliveries/summary")
def telegram_deliveries_summary(hours: int = 24):
    db = SessionLocal()
    try:
        return TelegramDeliveryLog().summary(db, hours=min(max(hours, 1), 720))
    finally:
        db.close()

@app.post("/system/test-telegram-owner", dependencies=[Depends(require_owner_action)])
async def test_telegram_owner():
    router = TelegramRouter()
    await router.owner_alert(
        "SYSTEM HEALTH TEST",
        "Owner Telegram alerts работают."
    )
    return {"status": "sent"}


@app.post("/system/test-telegram-free", dependencies=[Depends(require_owner_action)])
async def test_telegram_free():
    broadcaster = SignalBroadcaster()
    await broadcaster.send_message(
        settings.TELEGRAM_FREE_SIGNALS_CHAT_ID,
        "🧪 FREE channel test: система Finmt работает."
    )
    return {"status": "sent"}


@app.post("/system/test-telegram-vip", dependencies=[Depends(require_owner_action)])
async def test_telegram_vip():
    broadcaster = SignalBroadcaster()
    await broadcaster.send_message(
        settings.TELEGRAM_VIP_SIGNALS_CHAT_ID,
        "🧪 VIP channel test: система Finmt работает."
    )
    return {"status": "sent"}

@app.post("/trade/cost-preview")
def trade_cost_preview(payload: CostPreviewRequest):
    try:
        engine = CostEngine()

        preview = engine.estimate(
            symbol=payload.symbol,
            market_type=payload.market_type,
            side=payload.side,
            entry_price=payload.entry_price,
            exit_price=payload.exit_price,
            qty=payload.qty,
            liquidity=payload.liquidity,
            holding_funding_periods=payload.holding_funding_periods,
            leverage=payload.leverage,
        )

        return {
            "status": "ok",
            "cost": engine.to_dict(preview),
            "config": {
                "trading_mode": settings.TRADING_MODE,
                "market_type": settings.MARKET_TYPE,
                "enable_live_orders": settings.ENABLE_LIVE_ORDERS,
                "enable_futures": settings.ENABLE_FUTURES,
                "spot_taker_fee": settings.SPOT_TAKER_FEE,
                "spot_maker_fee": settings.SPOT_MAKER_FEE,
                "futures_taker_fee": settings.FUTURES_TAKER_FEE,
                "futures_maker_fee": settings.FUTURES_MAKER_FEE,
                "slippage_buffer_pct": settings.SLIPPAGE_BUFFER_PCT,
                "funding_buffer_pct": settings.FUNDING_BUFFER_PCT,
                "enable_futures": settings.ENABLE_FUTURES,
                "allow_shorts": settings.ALLOW_SHORTS,
                "execution_market": settings.EXECUTION_MARKET,
            },
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
        }

@app.post("/trade/build-plan")
def build_trade_plan(payload: TradePlanRequest):
    builder = TradePlanBuilder()
    plan = builder.build_plan(
        symbol=payload.symbol,
        side=payload.side,
        entry_price=payload.entry,
        stop_price=payload.stop,
        tp1=payload.tp1,
        tp2=payload.tp2,
        balance_usdt=payload.balance_usdt
    )
    return {"status": "ok", "trade_plan": plan.__dict__}

@app.post("/robot/force-valid-trade-signal", dependencies=[Depends(require_owner_action)])
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

@app.get("/intelligence/analyze")
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

@app.post("/robot/force-intelligence-signal", dependencies=[Depends(require_owner_action)])
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
                        f"{getattr(settings, 'EXECUTION_MARKET', 'spot')} / shorts_disabled.\n"
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

    confidence_hint — общий MTF score рынка.
    setup_quality.final_score — качество конкретного входа.

    Для trend continuation сетапов разрешаем approve от 62+,
    поэтому confidence тоже нужно поднимать от setup_score,
    иначе SignalQuality может отклонить хороший directional setup.
    """

    base = float(result.confidence_hint or 0)

    setup_quality = result.setup_quality if isinstance(result.setup_quality, dict) else {}
    setup_score = float(setup_quality.get("final_score") or 0)
    setup_decision = str(setup_quality.get("decision") or result.setup_decision or "")
    setup_comment = str(setup_quality.get("comment") or "")

    if setup_decision == "approve" and setup_score >= 70:
        calibrated = max(base, setup_score * 0.92)
        return round(min(calibrated, 88.0), 2)

    if (
        setup_decision == "approve"
        and setup_score >= 62
        and setup_comment == "trend_continuation_approved_weak_volume"
    ):
        calibrated = max(base, setup_score + 8)
        return round(min(calibrated, 82.0), 2)

    if setup_decision == "approve" and setup_score >= 62:
        calibrated = max(base, setup_score * 0.95)
        return round(min(calibrated, 80.0), 2)

    if setup_decision == "wait" and setup_score >= 55:
        calibrated = max(base, setup_score * 0.75)
        return round(min(calibrated, 72.0), 2)

    return round(base, 2)

@app.get("/intelligence/scan")
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

                if not quality.should_publish_to_clients(
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


@app.post("/intelligence/scan/run")
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
                        f"current_execution_mode_{getattr(settings, 'EXECUTION_MARKET', 'spot')}_shorts_disabled"
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
                sig.status = "telegram_failed"
                sig.closed_reason = "initial_telegram_publish_failed"

                item["status"] = "telegram_failed"
                item["decision"] = "initial_telegram_publish_failed"
                item["priority_publish_status"] = "telegram_failed_signal_not_activated"
                item["telegram_error"] = f"{type(telegram_error).__name__}: {repr(telegram_error)}"
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

@app.get("/intelligence/funnel")
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


@app.get("/intelligence/events")
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


@app.post("/debug/exposure", dependencies=[Depends(require_owner_action)])
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

@app.get("/analytics/signal-quality")
def analytics_signal_quality(limit: int = 200, only_lifecycle: bool = False):
    db = SessionLocal()

    try:
        limit = min(max(limit, 1), 1000)

        signals = (
            db.query(Signal)
            .filter(Signal.status == "closed")
            .order_by(Signal.id.desc())
            .limit(limit)
            .all()
        )

        total_closed = len(signals)

        lifecycle_count = 0
        legacy_count = 0

        went_positive = 0
        positive_then_negative = 0

        stop_loss_count = 0
        breakeven_count = 0
        trailing_count = 0
        post_tp1_stop_count = 0
        tp2_count = 0

        mfe_values = []
        mae_values = []
        missed_values = []
        result_values = []
        net_pnl_values = []
        costs_values = []

        by_reason = {}
        by_reason_money = {}

        items = []

        trailing_reasons = {
            "protective_trailing_stop",
            "adaptive_trailing_stop",
            "trend_trailing_stop",
        }

        post_tp1_reasons = {
            "adaptive_post_tp1_stop",
        }

        for s in signals:
            plan = s.plan_json or {}
            lifecycle = plan.get("lifecycle") or {}

            has_lifecycle = bool(lifecycle)
            if has_lifecycle:
                lifecycle_count += 1
            else:
                legacy_count += 1

            if only_lifecycle and not has_lifecycle:
                continue

            reason = s.closed_reason or lifecycle.get("close_reason") or "unknown"
            by_reason[reason] = by_reason.get(reason, 0) + 1

            net_pnl = float(s.closed_net_pnl) if s.closed_net_pnl is not None else None
            total_cost = float(s.closed_total_cost) if s.closed_total_cost is not None else None
            result_pct = float(s.result_pct) if s.result_pct is not None else None

            if reason not in by_reason_money:
                by_reason_money[reason] = {
                    "count": 0,
                    "net_pnl": 0.0,
                    "costs": 0.0,
                    "avg_result_pct": 0.0,
                    "_result_values": [],
                }

            by_reason_money[reason]["count"] += 1

            if net_pnl is not None:
                by_reason_money[reason]["net_pnl"] += net_pnl
                net_pnl_values.append(net_pnl)

            if total_cost is not None:
                by_reason_money[reason]["costs"] += total_cost
                costs_values.append(total_cost)

            if result_pct is not None:
                by_reason_money[reason]["_result_values"].append(result_pct)
                result_values.append(result_pct)

            if reason == "stop_loss":
                stop_loss_count += 1

            if reason == "breakeven_stop":
                breakeven_count += 1

            if reason in trailing_reasons:
                trailing_count += 1

            if reason in post_tp1_reasons:
                post_tp1_stop_count += 1

            if reason == "tp2_reached":
                tp2_count += 1

            if lifecycle.get("went_positive"):
                went_positive += 1

            if lifecycle.get("positive_then_negative"):
                positive_then_negative += 1

            if lifecycle.get("mfe_pct") is not None:
                mfe_values.append(float(lifecycle.get("mfe_pct")))

            if lifecycle.get("mae_pct") is not None:
                mae_values.append(float(lifecycle.get("mae_pct")))

            if lifecycle.get("missed_profit_pct") is not None:
                missed_values.append(float(lifecycle.get("missed_profit_pct")))

            items.append({
                "id": s.id,
                "symbol": s.symbol,
                "side": s.side,
                "grade": s.grade,
                "status": s.status,
                "result_pct": s.result_pct,
                "closed_reason": s.closed_reason,
                "closed_net_pnl": s.closed_net_pnl,
                "closed_total_cost": s.closed_total_cost,

                "has_lifecycle": has_lifecycle,
                "mfe_pct": lifecycle.get("mfe_pct"),
                "mae_pct": lifecycle.get("mae_pct"),
                "missed_profit_pct": lifecycle.get("missed_profit_pct"),
                "positive_then_negative": lifecycle.get("positive_then_negative"),

                "entry_price": lifecycle.get("entry_price"),
                "max_profit_price": lifecycle.get("max_profit_price"),
                "max_drawdown_price": lifecycle.get("max_drawdown_price"),
                "exit_price": lifecycle.get("exit_price"),
                "close_reason": lifecycle.get("close_reason") or s.closed_reason,
            })

        def avg(values):
            if not values:
                return 0.0
            return round(sum(values) / len(values), 4)

        for reason, row in by_reason_money.items():
            values = row.pop("_result_values", [])
            row["net_pnl"] = round(row["net_pnl"], 6)
            row["costs"] = round(row["costs"], 6)
            row["avg_result_pct"] = avg(values)

        lifecycle_total = lifecycle_count if lifecycle_count else 0

        return {
            "status": "ok",

            "total_closed": total_closed,
            "lifecycle_count": lifecycle_count,
            "legacy_count": legacy_count,
            "only_lifecycle": only_lifecycle,

            "went_positive": went_positive,
            "positive_then_negative": positive_then_negative,
            "positive_then_negative_rate": round(
                (positive_then_negative / lifecycle_total * 100), 2
            ) if lifecycle_total else 0.0,

            "stop_loss_count": stop_loss_count,
            "breakeven_count": breakeven_count,
            "trailing_count": trailing_count,
            "post_tp1_stop_count": post_tp1_stop_count,
            "tp2_count": tp2_count,

            "tp2_rate": round((tp2_count / lifecycle_total * 100), 2) if lifecycle_total else 0.0,
            "trailing_rate": round((trailing_count / lifecycle_total * 100), 2) if lifecycle_total else 0.0,
            "post_tp1_stop_rate": round((post_tp1_stop_count / lifecycle_total * 100), 2) if lifecycle_total else 0.0,

            "avg_mfe_pct": avg(mfe_values),
            "avg_mae_pct": avg(mae_values),
            "avg_missed_profit_pct": avg(missed_values),
            "avg_result_pct": avg(result_values),
            "avg_net_pnl_usdt": avg(net_pnl_values),
            "avg_costs_usdt": avg(costs_values),

            "total_net_pnl_usdt": round(sum(net_pnl_values), 6),
            "total_costs_usdt": round(sum(costs_values), 6),

            "by_reason": by_reason,
            "by_reason_money": by_reason_money,

            "items": items,
        }

    finally:
        db.close()

@app.get("/ml/outcomes/summary")
def ml_outcomes_summary():
    service = MLOutcomeStatsService()
    return service.safe_summary()


@app.get("/analytics/grade-c-audit")
def analytics_grade_c_audit(date_from: str | None = None):
    """Count new Grade C trades/signals after a date."""
    db = SessionLocal()
    try:
        q = db.query(Signal)
        if date_from:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(str(date_from).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            q = q.filter(Signal.created_at >= dt)

        total = q.count()
        grade_c = q.filter(Signal.grade == "C").count()
        opened_like = q.filter(Signal.status.in_(["published", "opened", "tp1", "breakeven", "closed"]))
        opened_total = opened_like.count()
        opened_c = opened_like.filter(Signal.grade == "C").count()

        return {
            "status": "ok",
            "date_from": date_from,
            "total_signals": total,
            "grade_c_signals": grade_c,
            "grade_c_share_pct": round((grade_c / total * 100), 2) if total else 0.0,
            "opened_family_total": opened_total,
            "opened_family_grade_c": opened_c,
            "opened_family_grade_c_share_pct": round((opened_c / opened_total * 100), 2) if opened_total else 0.0,
        }
    finally:
        db.close()
