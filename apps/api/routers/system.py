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


@router.get("/exchange-diagnostics", dependencies=[Depends(require_owner_action)])
def system_exchange_diagnostics(timeout: float = 8.0):
    """(#htx-outage-2026-07-26) Почему биржа недоступна: DNS / TCP / TLS / HTTP.

    Запускать С САМОГО инстанса — проверяет сеть именно из ДЦ, где живёт сервис.
    Логи ccxt дают только `htx GET <url>` без причины; здесь причина названа явно,
    включая гео-блокировку (403/451) и подсказку, какой хост живой.
    """
    from services.exchange_diagnostics import diagnose

    return diagnose(timeout=timeout)


@router.get("/exchange-diagnostics-all", dependencies=[Depends(require_owner_action)])
def system_exchange_diagnostics_all(timeout: float = 8.0):
    """(#okx-satellite-2026-09-02) Тот же разбор DNS/TCP/TLS/HTTP, но для ОБЕИХ
    бирж сразу, плюс active_exchange — независимо от того, какая сейчас
    торгует. Отдельный эндпоинт от /exchange-diagnostics (HTX-only, старая
    форма ответа) — существующие потребители того ответа не задеты."""
    from services.exchange_diagnostics import diagnose_all

    return diagnose_all(timeout=timeout)


@router.get("/capital-envelopes", dependencies=[Depends(require_owner_action)])
def system_capital_envelopes():
    """(#capital-envelopes-2026-08-21) Кто на какую долю депозита претендует.

    До этого три контура делили счёт вслепую: направленные 70% + арбитраж ~42%
    (2 хеджа × 10.5% × 2 ноги) + сетка 5% ≈ 117% при капитале 950. Связи не было
    — `used_margin()` перебирает только Signal. В live отказ по марже получил бы
    не «лишний» контур, а тот, кто открылся последним.

    Показывает заданные доли, фактические (с учётом переданных), занято и
    свободно по каждому контуру.
    """
    from services import capital_envelopes as envelopes
    from services.arb_capital import available_equity

    db = SessionLocal()
    try:
        equity = available_equity()
        shares = envelopes.effective_shares(db=db)
        configured = envelopes.configured_shares()

        contours = []
        total_used = 0.0
        for key, label in (
            (envelopes.DIRECTIONAL, "Направленные (тренд/скальп/range/CRT)"),
            (envelopes.ARB, "Funding arb"),
            (envelopes.GRID, "Grid"),
        ):
            pct = float(shares.get(key, 0.0))
            envelope = round(equity * pct / 100.0, 2)
            used = envelopes.used_usdt(key, db=db)
            if used is not None:
                total_used += used
            contours.append({
                "contour": key,
                "label": label,
                "configured_pct": configured.get(key, 0.0),
                "effective_pct": pct,
                "envelope_usdt": envelope,
                "used_usdt": round(used, 2) if used is not None else None,
                # Превышение конверта фактом — сигнал, что контур занял больше,
                # чем ему отведено (возможен, если позиции открыты до правки долей).
                "over_envelope": bool(used is not None and used > envelope + 0.01),
                "note": (shares.get("_detail") or {}).get(key),
            })

        return {
            "equity_usdt": round(equity, 2),
            "used_total_usdt": round(total_used, 2),
            "used_total_pct": round(total_used / equity * 100.0, 2) if equity > 0 else 0.0,
            "configured_total_pct": round(sum(configured.values()), 2),
            "effective_total_pct": round(
                sum(float(shares.get(k, 0.0))
                    for k in (envelopes.DIRECTIONAL, envelopes.ARB, envelopes.GRID)), 2
            ),
            "released_pct": round(float(shares.get("_released_pct", 0.0)), 2),
            # Остаток до 100% — намеренный запас на просадку и комиссии.
            "reserve_pct": round(100.0 - sum(configured.values()), 2),
            "contours": contours,
            "arb_leg_notional_usdt": envelopes.arb_leg_notional(equity=equity, db=db),
        }
    finally:
        db.close()


@router.get("/config-effective", dependencies=[Depends(require_owner_action)])
def system_config_effective():
    """(#config-visibility-2026-08-21) Что реально действует и откуда взято.

    Настройка живёт в двух местах — дефолт в config.py и перекрытие в
    render.yaml → env, — и по коду не видно, какое из двух работает. Здесь по
    каждому параметру: действующее значение, дефолт кода и победитель.

    ТОЛЬКО ЧТЕНИЕ. Пороги торговли правятся коммитом: так у каждой правки
    остаются ревью, тесты и записанная причина. Секреты отдаются как факт
    «задан/не задан», без значений.
    """
    from services.config_inspector import effective_config

    return effective_config()


@router.get("/egress-history", dependencies=[Depends(require_owner_action)])
def system_egress_history(hours: float = 24.0):
    """(#egress-monitor-2026-07-26) Доказательная база для тикета в поддержку.

    Временной ряд доступности внешних хостов, замеренный С САМОГО инстанса, с
    контрольной группой (Cloudflare/Google/Telegram). `outage_windows` — готовый
    список окон с временными метками: именно то, что просит поддержка и чего
    нет на глобальной статус-странице платформы.
    """
    from services.egress_monitor import history

    return history(hours=hours)


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
