from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from models.payment import Payment, PaymentEvent
from models.subscriber import Subscriber
from models.telegram_delivery import TelegramDelivery
from models.telegram_profile import TelegramProfile
from services.billing_service import BillingService
from services.customer_notifications import CustomerNotificationService
from services.telegram_bot_menu import TelegramBotMenuService


class ProductE2ESmokeService:
    """Dry-run product funnel smoke for Telegram -> payment -> VIP access.

    The service intentionally reuses the existing production services instead of
    duplicating menu/payment/subscription logic. Routes should rollback by
    default after calling this service so operators can validate the contour
    without creating real customers.
    """

    def __init__(
        self,
        menu: TelegramBotMenuService | None = None,
        billing: BillingService | None = None,
        notifications: CustomerNotificationService | None = None,
    ):
        self.menu = menu or TelegramBotMenuService()
        self.billing = billing or BillingService()
        self.notifications = notifications or CustomerNotificationService()

    def run(
        self,
        db: Session,
        *,
        telegram_user_id: str | None = None,
        plan_code: str = "vip_30",
    ) -> dict[str, Any]:
        telegram_user_id = str(telegram_user_id or self._default_smoke_user_id())
        message = self._message(telegram_user_id, "/start")

        before = self._counts(db)

        start = self.menu.handle(db, message=message, callback_query=None)
        checkout = self.menu.handle(
            db,
            message=None,
            callback_query=self._callback(telegram_user_id, f"pay:{plan_code}"),
        )
        db.flush()

        payment = (
            db.query(Payment)
            .filter(Payment.telegram_user_id == telegram_user_id)
            .order_by(Payment.id.desc())
            .first()
        )
        if not payment:
            return self._result(
                ok=False,
                before=before,
                after=self._counts(db),
                checks={"checkout_created": False},
                details={"error": "payment_not_created", "checkout_text": checkout.text},
            )

        provider_event_id = f"smoke-{payment.id}-{telegram_user_id}"
        paid, subscriber, activated, event = self.billing.process_payment_event(
            db=db,
            payment_id=payment.id,
            provider="smoke",
            provider_event_id=provider_event_id,
            status="paid",
            raw_payload='{"source":"product_e2e_smoke"}',
        )
        notification = self.notifications.queue_payment_success(db, paid, subscriber, activated)
        db.flush()

        first_expiry = subscriber.expires_at if subscriber else None
        _, subscriber_again, activated_again, event_again = self.billing.process_payment_event(
            db=db,
            payment_id=payment.id,
            provider="smoke",
            provider_event_id=provider_event_id,
            status="paid",
            raw_payload='{"source":"product_e2e_smoke","retry":true}',
        )
        db.flush()

        status = self.menu.handle(db, message=self._message(telegram_user_id, "/subscription_status"), callback_query=None)
        profile = db.query(TelegramProfile).filter(TelegramProfile.telegram_user_id == telegram_user_id).first()

        checks = {
            "start_menu_ok": start.command == "/start" and start.reply_markup is not None,
            "profile_created": profile is not None,
            "checkout_created": payment.status in {"pending", "paid"} and checkout.command == "/pay",
            "payment_event_created": event.id is not None and event.status == "paid",
            "payment_paid": paid.status == "paid",
            "subscriber_active": subscriber is not None and subscriber.status == "active",
            "vip_access_granted": subscriber is not None and subscriber.plan == plan_code and subscriber.expires_at is not None,
            "idempotent_event": event_again.id == event.id and activated_again is False,
            "idempotent_expiry_unchanged": bool(subscriber_again and subscriber_again.expires_at == first_expiry),
            "customer_notification_queued": bool(notification.get("queued")),
            "subscription_status_ok": status.command == "/subscription_status" and "Статус подписки" in status.text,
        }

        return self._result(
            ok=all(checks.values()),
            before=before,
            after=self._counts(db),
            checks=checks,
            details={
                "telegram_user_id": telegram_user_id,
                "plan_code": plan_code,
                "payment_id": payment.id,
                "payment_event_id": event.id,
                "subscriber_id": subscriber.id if subscriber else None,
                "notification": notification,
                "funnel_stage": profile.funnel_stage if profile else None,
                "status_text_preview": status.text[:160],
            },
        )

    def _result(self, *, ok: bool, before: dict, after: dict, checks: dict, details: dict) -> dict[str, Any]:
        return {
            "status": "ok" if ok else "failed",
            "ok": ok,
            "checks": checks,
            "counts_before": before,
            "counts_after": after,
            "details": details,
        }

    def _counts(self, db: Session) -> dict[str, int]:
        return {
            "profiles": db.query(TelegramProfile).count(),
            "payments": db.query(Payment).count(),
            "payment_events": db.query(PaymentEvent).count(),
            "subscribers": db.query(Subscriber).count(),
            "telegram_deliveries": db.query(TelegramDelivery).count(),
        }

    def _default_smoke_user_id(self) -> str:
        return f"900{int(datetime.now(timezone.utc).timestamp())}"

    def _message(self, telegram_user_id: str, text: str) -> dict:
        return {
            "text": text,
            "chat": {"id": int(telegram_user_id)},
            "from": {
                "id": int(telegram_user_id),
                "username": "smoke_user",
                "first_name": "Smoke",
                "last_name": "Tester",
            },
        }

    def _callback(self, telegram_user_id: str, data: str) -> dict:
        return {
            "data": data,
            "message": {"chat": {"id": int(telegram_user_id)}},
            "from": {
                "id": int(telegram_user_id),
                "username": "smoke_user",
                "first_name": "Smoke",
                "last_name": "Tester",
            },
        }
