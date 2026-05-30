from __future__ import annotations

from datetime import datetime, timedelta, timezone
from collections import defaultdict

from sqlalchemy.orm import Session

from models.payment import Payment
from models.subscriber import Subscriber


class RevenueMetricsService:
    """Revenue/funnel metrics for the owner dashboard.

    This is deliberately read-only: it derives MVP revenue signals from paid
    payments and subscriber state without changing checkout/subscription flows.
    """

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _as_aware(self, value) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except Exception:
                return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    def summary(self, db: Session, window_days: int = 30) -> dict:
        now = self._now()
        window_start = now - timedelta(days=window_days)

        payments = db.query(Payment).all()
        subscribers = db.query(Subscriber).all()
        paid = [payment for payment in payments if payment.status == "paid"]
        pending = [payment for payment in payments if payment.status == "pending"]
        failed = [payment for payment in payments if payment.status in ["failed", "canceled", "refunded"]]

        paid_in_window = [
            payment
            for payment in paid
            if (self._as_aware(payment.paid_at) or self._as_aware(payment.created_at) or now) >= window_start
        ]
        active_paid_subscribers = [
            subscriber
            for subscriber in subscribers
            if subscriber.status == "active" and not subscriber.is_trial and self._as_aware(subscriber.expires_at) and self._as_aware(subscriber.expires_at) > now
        ]
        active_trials = [
            subscriber
            for subscriber in subscribers
            if subscriber.status in ["active", "trial"] and subscriber.is_trial and self._as_aware(subscriber.expires_at) and self._as_aware(subscriber.expires_at) > now
        ]
        expired_or_blocked = [subscriber for subscriber in subscribers if subscriber.status in ["expired", "blocked"]]

        paid_users = {str(payment.telegram_user_id) for payment in paid}
        known_users = {str(subscriber.telegram_user_id) for subscriber in subscribers} | {str(payment.telegram_user_id) for payment in payments}
        trial_users = {str(subscriber.telegram_user_id) for subscriber in subscribers if subscriber.is_trial}
        trial_to_paid_users = trial_users & paid_users

        plan_breakdown = defaultdict(lambda: {"payments": 0, "cash_collected": 0.0, "active_subscribers": 0})
        for payment in paid:
            bucket = plan_breakdown[payment.plan_code]
            bucket["payments"] += 1
            bucket["cash_collected"] += float(payment.amount or 0.0)
        for subscriber in active_paid_subscribers:
            plan_breakdown[subscriber.plan]["active_subscribers"] += 1

        mrr_estimate = sum(
            (float(payment.amount or 0.0) / max(int(payment.duration_days or 30), 1)) * 30
            for payment in paid_in_window
        )
        cash_collected = sum(float(payment.amount or 0.0) for payment in paid)
        cash_collected_window = sum(float(payment.amount or 0.0) for payment in paid_in_window)
        pending_amount = sum(float(payment.amount or 0.0) for payment in pending)
        failed_amount = sum(float(payment.amount or 0.0) for payment in failed)

        return {
            "status": "ok",
            "window_days": window_days,
            "currency": "USDT",
            "cash_collected_total": round(cash_collected, 6),
            "cash_collected_window": round(cash_collected_window, 6),
            "mrr_estimate": round(mrr_estimate, 6),
            "pending_amount": round(pending_amount, 6),
            "failed_amount": round(failed_amount, 6),
            "payments_total": len(payments),
            "payments_paid": len(paid),
            "payments_pending": len(pending),
            "payments_failed": len(failed),
            "active_paid_subscribers": len(active_paid_subscribers),
            "active_trials": len(active_trials),
            "expired_or_blocked_subscribers": len(expired_or_blocked),
            "known_users": len(known_users),
            "paid_users": len(paid_users),
            "trial_users": len(trial_users),
            "trial_to_paid_users": len(trial_to_paid_users),
            "trial_to_paid_conversion_pct": round((len(trial_to_paid_users) / len(trial_users) * 100), 2) if trial_users else 0.0,
            "paid_conversion_pct": round((len(paid_users) / len(known_users) * 100), 2) if known_users else 0.0,
            "churn_proxy_pct": round((len(expired_or_blocked) / len(subscribers) * 100), 2) if subscribers else 0.0,
            "plan_breakdown": [
                {
                    "plan_code": plan_code,
                    "payments": data["payments"],
                    "cash_collected": round(data["cash_collected"], 6),
                    "active_subscribers": data["active_subscribers"],
                }
                for plan_code, data in sorted(plan_breakdown.items())
            ],
        }
