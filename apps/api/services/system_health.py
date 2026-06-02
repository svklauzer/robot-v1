from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from core.config import settings
from models.bot import Bot
from models.signal import Signal
from models.subscriber import Subscriber
from services.billing_service import BillingService
from services.exchange_reconciliation import ExchangeReconciliationService
from services.funding_arbitrage import FundingArbEngine
from services.live_safety import LiveSafetyService
from services.market_connectivity import MarketConnectivityService
from services.ml_outcome_stats import MLOutcomeStatsService
from services.revenue_metrics import RevenueMetricsService
from services.telegram_delivery_log import TelegramDeliveryLog


class SystemHealthService:
    """Build the owner-facing system health payload outside the FastAPI route."""

    def summary(self, db: Session, *, loops: dict[str, Any] | None = None, market_symbol: str = "BTC/USDT") -> dict[str, Any]:
        bot = db.query(Bot).filter(Bot.name == "Main Robot").first()

        signal_counts = {
            "total": db.query(Signal).count(),
            "published": db.query(Signal).filter(Signal.status == "published").count(),
            "opened": db.query(Signal).filter(Signal.status == "opened").count(),
            "tp1": db.query(Signal).filter(Signal.status == "tp1").count(),
            "closed": db.query(Signal).filter(Signal.status == "closed").count(),
            "expired": db.query(Signal).filter(Signal.status == "expired").count(),
        }
        subscriber_counts = {
            "active": db.query(Subscriber).filter(Subscriber.status == "active").count(),
            "expired": db.query(Subscriber).filter(Subscriber.status == "expired").count(),
            "blocked": db.query(Subscriber).filter(Subscriber.status == "blocked").count(),
        }
        production_blockers = settings.production_blockers()

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
            "loops": loops or {},
            "market": MarketConnectivityService().check(market_symbol),
            "exchange_reconciliation": ExchangeReconciliationService().check(db),
            "signals": signal_counts,
            "subscribers": subscriber_counts,
            "telegram_delivery": TelegramDeliveryLog().summary(db, hours=24),
            "payments": BillingService().summary(db),
            "revenue": RevenueMetricsService().summary(db),
            "funding_arb": FundingArbEngine().summary(db),
            "live_safety": LiveSafetyService().snapshot(db=db, bot=bot),
            "ml_outcomes": MLOutcomeStatsService().safe_summary(),
            "production_readiness": {
                "ready": len(production_blockers) == 0,
                "blockers": production_blockers,
                "live_enabled": settings.is_live_enabled,
            },
        }
