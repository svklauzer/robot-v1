from fastapi import APIRouter, Depends
from pydantic import BaseModel
from core.config import settings
from core.db import SessionLocal
from core.security import require_owner_action
from models.bot import Bot
from services.billing_service import BillingService
from services.revenue_metrics import RevenueMetricsService
from services.market_connectivity import MarketConnectivityService
from services.exchange_reconciliation import ExchangeReconciliationService
from services.validation_gates import ValidationGateService
from services.live_safety import LiveSafetyService
from services.live_shadow import LiveShadowDriftService
from services.ml_outcome_stats import MLOutcomeStatsService
from services.product_e2e_smoke import ProductE2ESmokeService
from services.funding_arbitrage import FundingArbEngine
from services.telegram_delivery_log import TelegramDeliveryLog
from services.signal_broadcaster import SignalBroadcaster
from services.telegram_router import TelegramRouter

# Analytics summary callable — no circular import (analytics has no system deps)
from routers.analytics import _analytics_summary_data

router = APIRouter(prefix="/system", tags=["system"])


class KillSwitchRequest(BaseModel):
    enabled: bool = True
    reason: str | None = "owner_request"


class KillSwitchSmokeRequest(BaseModel):
    reason: str | None = "owner_smoke"


class ProductE2ESmokeRequest(BaseModel):
    telegram_user_id: str | None = None
    plan_code: str = "vip_30"
    persist: bool = False


@router.get("/live-safety", dependencies=[Depends(require_owner_action)])
def system_live_safety():
    db = SessionLocal()
    try:
        bot = db.query(Bot).filter(Bot.name == "Main Robot").first()
        return LiveSafetyService().snapshot(db=db, bot=bot)
    finally:
        db.close()


@router.post("/kill-switch", dependencies=[Depends(require_owner_action)])
def system_kill_switch(payload: KillSwitchRequest):
    db = SessionLocal()
    try:
        bot = db.query(Bot).filter(Bot.name == "Main Robot").first()
        if not bot:
            return {"status": "error", "error": "bot_not_found"}
        state = LiveSafetyService().set_kill_switch(
            db=db, bot=bot, enabled=payload.enabled, reason=payload.reason
        )
        db.commit()
        return {"status": "ok", "live_safety": state}
    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        db.close()


@router.post("/kill-switch-smoke", dependencies=[Depends(require_owner_action)])
def system_kill_switch_smoke(payload: KillSwitchSmokeRequest | None = None):
    db = SessionLocal()
    try:
        bot = db.query(Bot).filter(Bot.name == "Main Robot").first()
        if not bot:
            return {"status": "error", "error": "bot_not_found"}
        request = payload or KillSwitchSmokeRequest()
        result = LiveSafetyService().kill_switch_smoke(db=db, bot=bot, reason=request.reason)
        db.rollback()
        return result
    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        db.close()


@router.get("/live-shadow/drift", dependencies=[Depends(require_owner_action)])
def system_live_shadow_drift(limit: int = 20):
    db = SessionLocal()
    try:
        return LiveShadowDriftService().report(db, limit=limit)
    finally:
        db.close()


@router.get("/exchange-reconciliation", dependencies=[Depends(require_owner_action)])
def exchange_reconciliation_status(symbol: str | None = None, force: bool = False):
    db = SessionLocal()
    try:
        return ExchangeReconciliationService().check(db, symbol=symbol, force=force)
    finally:
        db.close()


@router.post("/product-e2e-smoke", dependencies=[Depends(require_owner_action)])
def system_product_e2e_smoke(payload: ProductE2ESmokeRequest | None = None):
    db = SessionLocal()
    request = payload or ProductE2ESmokeRequest()
    try:
        result = ProductE2ESmokeService().run(
            db, telegram_user_id=request.telegram_user_id, plan_code=request.plan_code
        )
        result["persisted"] = bool(request.persist)
        if request.persist:
            db.commit()
        else:
            db.rollback()
        return result
    except Exception as e:
        db.rollback()
        return {"status": "error", "ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        db.close()


@router.get("/readiness", dependencies=[Depends(require_owner_action)])
def system_readiness():
    db = SessionLocal()
    try:
        analytics = _analytics_summary_data()
        telegram_delivery = TelegramDeliveryLog().summary(db, hours=24)
        payments_data = BillingService().summary(db)
        revenue = RevenueMetricsService().summary(db)
        funding_arb = FundingArbEngine().summary(db)
        bot = db.query(Bot).filter(Bot.name == "Main Robot").first()
        live_safety = LiveSafetyService().snapshot(db=db, bot=bot)
        ml_outcomes = MLOutcomeStatsService().safe_summary()
        market_connectivity = MarketConnectivityService().check("BTC/USDT")
        exchange_reconciliation = ExchangeReconciliationService().check(db)
        validation_gates = ValidationGateService().evaluate(db)

        hard_blockers = list(settings.production_blockers())
        hard_blockers.extend(live_safety.get("blockers", []))
        if settings.ENABLE_FUNDING_ARB and not settings.ENABLE_FUTURES:
            hard_blockers.append("funding arbitrage requires ENABLE_FUTURES=true")

        soft_warnings: list[str] = []
        soft_warnings.extend(validation_gates.get("blockers") or [])
        if telegram_delivery.get("failed", 0) > 0:
            soft_warnings.append("telegram delivery has failures in the last 24h")
        if ml_outcomes.get("stale"):
            soft_warnings.append(
                f"ML outcomes log is stale: latest_logged_at={ml_outcomes.get('latest_logged_at')} "
                f"age_hours={ml_outcomes.get('latest_age_hours')}"
            )
        elif ml_outcomes.get("status") not in ["ok", "empty"]:
            soft_warnings.append("ML outcomes summary is degraded")
        if market_connectivity.get("breaker_blocked"):
            soft_warnings.extend(
                market_connectivity.get("blockers") or ["market connectivity breaker is blocked"]
            )
        if exchange_reconciliation.get("blockers"):
            soft_warnings.extend(exchange_reconciliation.get("blockers") or [])

        if settings.is_live_enabled:
            hard_blockers.extend(soft_warnings)
            soft_warnings = []

        is_paper_mode = not settings.is_live_enabled
        effective_status = (
            "ready" if not hard_blockers and not soft_warnings
            else ("paper_ready" if is_paper_mode and not hard_blockers else "blocked")
        )

        return {
            "status": effective_status,
            "ready": not hard_blockers,
            "paper_mode": is_paper_mode,
            "hard_blockers": hard_blockers,
            "warnings": soft_warnings,
            "blockers": hard_blockers,
            "analytics": analytics,
            "telegram_delivery": telegram_delivery,
            "payments": payments_data,
            "revenue": revenue,
            "funding_arb": funding_arb,
            "live_safety": live_safety,
            "ml_outcomes": ml_outcomes,
            "market_connectivity": market_connectivity,
            "exchange_reconciliation": exchange_reconciliation,
            "validation_gates": validation_gates,
            "required_gates": {
                "closed_validation_signals": validation_gates.get("min_closed", 200),
                "failed_setup_exit_share_max_pct": validation_gates.get("failed_setup_max_pct", 35.0),
                "positive_then_negative_max_pct": validation_gates.get("positive_then_negative_max_pct", 25.0),
                "telegram_delivery_sla_min_pct": 99.0,
                "adaptive_mfe_capture_enabled": bool(getattr(settings, "MFE_CAPTURE_ENABLED", True)),
                "adaptive_mfe_capture_start_pct": getattr(settings, "MFE_CAPTURE_START_PCT", 0.65),
                "adaptive_mfe_capture_drawdown_pct": getattr(settings, "MFE_CAPTURE_DRAWDOWN_PCT", 0.30),
                "adaptive_mfe_capture_protect_share": getattr(settings, "MFE_CAPTURE_PROTECT_SHARE", 0.35),
                "market_connectivity_max_latency_ms": getattr(
                    settings, "MARKET_CONNECTIVITY_MAX_LATENCY_MS", 5000
                ),
                "market_connectivity_max_spread_pct": getattr(
                    settings, "MARKET_CONNECTIVITY_MAX_SPREAD_PCT", 0.75
                ),
            },
        }
    finally:
        db.close()


# ── Telegram test helpers (/system/test-telegram-* used by health page) ──────

@router.post("/test-telegram-owner", dependencies=[Depends(require_owner_action)])
async def test_telegram_owner():
    await TelegramRouter().owner_alert("SYSTEM HEALTH TEST", "Owner Telegram alerts работают.")
    return {"status": "sent"}


@router.post("/test-telegram-free", dependencies=[Depends(require_owner_action)])
async def test_telegram_free():
    await SignalBroadcaster().send_message(
        settings.TELEGRAM_FREE_SIGNALS_CHAT_ID,
        "🧪 FREE channel test: система Finmt работает.",
    )
    return {"status": "sent"}


@router.post("/test-telegram-vip", dependencies=[Depends(require_owner_action)])
async def test_telegram_vip():
    await SignalBroadcaster().send_message(
        settings.TELEGRAM_VIP_SIGNALS_CHAT_ID,
        "🧪 VIP channel test: система Finmt работает.",
    )
    return {"status": "sent"}
