from __future__ import annotations

from sqlalchemy.orm import Session

from core.config import settings
from models.payment import Payment
from models.subscriber import Subscriber
from services.telegram_delivery_log import TelegramDeliveryLog


class CustomerNotificationService:
    """Queues customer-facing Telegram messages for subscription/payment lifecycle events."""

    def __init__(self, delivery_log: TelegramDeliveryLog | None = None):
        self.delivery_log = delivery_log or TelegramDeliveryLog()

    def payment_success_text(self, payment: Payment, subscriber: Subscriber, activated: bool = True) -> str:
        invite = settings.VIP_INVITE_LINK or "VIP invite будет выдан owner/admin после подтверждения."
        headline = "✅ VIP активирован" if activated else "✅ Платеж уже обработан"
        return (
            f"{headline}\n\n"
            f"Plan: {subscriber.plan}\n"
            f"Payment: #{payment.id} · {payment.amount:g} {payment.currency}\n"
            f"Access until: {subscriber.expires_at}\n\n"
            f"VIP access: {invite}\n\n"
            "Проверить статус можно командой /status. "
            "Сигналы не являются финансовой рекомендацией — соблюдайте риск-менеджмент."
        )

    def queue_payment_success(
        self,
        db: Session,
        payment: Payment,
        subscriber: Subscriber | None,
        activated: bool,
    ) -> dict:
        if not activated or not subscriber or not subscriber.telegram_user_id:
            return {"queued": False, "reason": "not_activated_or_missing_subscriber"}

        message_type = "customer_payment_success"
        delivery = self.delivery_log.queue(
            db=db,
            chat_id=subscriber.telegram_user_id,
            text_value=self.payment_success_text(payment, subscriber, activated=activated),
            message_type=message_type,
            max_attempts=5,
            reply_markup={
                "inline_keyboard": [
                    [{"text": "📌 Проверить статус", "callback_data": "status"}],
                    [{"text": "🔁 Продлить", "callback_data": "renew"}],
                ]
            },
        )
        return {"queued": True, "delivery_id": delivery.id, "message_type": message_type}
