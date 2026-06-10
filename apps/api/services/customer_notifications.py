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

    def payment_success_text(self, payment: Payment, subscriber: Subscriber, activated: bool = True, invite_link: str | None = None) -> str:
        invite = invite_link or settings.VIP_INVITE_LINK or "VIP invite будет выдан owner/admin после подтверждения."
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

    def expiry_text(self, subscriber: Subscriber) -> str:
        was_trial = bool(getattr(subscriber, "is_trial", False))
        if was_trial:
            headline = "⏳ Ваш бесплатный VIP-период завершился"
            body = (
                "Бесплатные 30 дней VIP по HTX-партнёрству истекли. "
                "Чтобы не терять полные сигналы (входы, стопы, тейки) — оформите VIP."
            )
        else:
            headline = "⏳ Ваша VIP-подписка истекла"
            body = "Продлите доступ, чтобы продолжить получать полные сигналы."
        return (
            f"{headline}\n\n"
            f"{body}\n\n"
            "Выберите тариф командой /plans или кнопкой ниже. "
            "Оплата — в пару кликов через Telegram Stars."
        )

    def queue_expiry(self, db: Session, subscriber: Subscriber | None) -> dict:
        if not subscriber or not subscriber.telegram_user_id:
            return {"queued": False, "reason": "missing_subscriber"}
        delivery = self.delivery_log.queue(
            db=db,
            chat_id=subscriber.telegram_user_id,
            text_value=self.expiry_text(subscriber),
            message_type="customer_subscription_expired",
            max_attempts=5,
            reply_markup={
                "inline_keyboard": [
                    [{"text": "💎 Оформить VIP", "callback_data": "plans"}],
                    [{"text": "🔁 Продлить (30 дней)", "callback_data": "renew"}],
                ]
            },
        )
        return {"queued": True, "delivery_id": delivery.id, "message_type": "customer_subscription_expired"}
