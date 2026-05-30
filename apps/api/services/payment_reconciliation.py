from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from core.config import settings
from models.payment import Payment, PaymentEvent


class PaymentReconciliationService:
    """Payment hygiene worker for the manual checkout MVP.

    Pending checkouts must not stay pending forever: they pollute revenue metrics,
    confuse owner UI, and can be accidentally confirmed long after a customer has
    abandoned the funnel. This service expires stale pending payments and records
    a provider-like reconciliation event for auditability.
    """

    marker = "auto_expired_pending_checkout"

    def reconcile_pending(self, db: Session, older_than_hours: int | None = None) -> dict[str, Any]:
        ttl_hours = int(older_than_hours or getattr(settings, "PAYMENT_PENDING_EXPIRE_HOURS", 48) or 48)
        ttl_hours = max(ttl_hours, 1)
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=ttl_hours)

        pending = db.query(Payment).filter(Payment.status == "pending").all()
        expired: list[Payment] = []

        for payment in pending:
            created_at = self._as_aware(payment.created_at)
            if created_at and created_at > cutoff:
                continue

            payment.status = "expired"
            payment.raw_payload = payment.raw_payload or "payment_reconciliation_expired_pending"
            note = f"{self.marker}; expired_at={now.isoformat()}; ttl_hours={ttl_hours}"
            payment.notes = f"{payment.notes}; {note}" if payment.notes else note
            expired.append(payment)

            db.add(
                PaymentEvent(
                    payment_id=payment.id,
                    subscriber_id=payment.subscriber_id,
                    provider=payment.provider or "manual",
                    provider_event_id=f"reconcile-expired-{payment.id}-{int(now.timestamp())}",
                    event_type="payment_reconciliation",
                    status="expired",
                    amount=payment.amount,
                    currency=payment.currency,
                    raw_payload=note,
                    processed_at=now,
                )
            )

        return {
            "status": "ok",
            "ttl_hours": ttl_hours,
            "pending_scanned": len(pending),
            "expired": len(expired),
            "expired_payment_ids": [payment.id for payment in expired],
        }

    def _as_aware(self, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
