from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session

from models.subscriber import Subscriber
from services.telegram_router import TelegramRouter
from services.customer_notifications import CustomerNotificationService


class SubscriptionWatchdog:
    def __init__(self):
        self.telegram = TelegramRouter()
        self.customer = CustomerNotificationService()

    async def check_subscriptions(self, db: Session) -> dict:
        now = datetime.now(timezone.utc)

        subscribers = (
            db.query(Subscriber)
            .filter(Subscriber.status.in_(["active", "trial"]))
            .all()
        )

        expired = []
        warning_1d = []
        warning_3d = []

        for sub in subscribers:
            if not sub.expires_at:
                continue

            expires_at = self._as_aware(sub.expires_at)
            seconds_left = (expires_at - now).total_seconds()
            days_left = int(seconds_left // 86400)

            if seconds_left <= 0:
                sub.status = "expired"
                marker = f"subscription_watchdog_expired_at={now.isoformat()}"
                sub.notes = f"{sub.notes}; {marker}" if sub.notes else marker
                expired.append(sub)
                await self.telegram.owner_alert(
                    "SUBSCRIPTION EXPIRED",
                    self._sub_text(sub)
                )
                # Зовём клиента к оплате (триал или платная — текст адаптируется).
                self.customer.queue_expiry(db, sub)
                continue

            if days_left == 1:
                warning_1d.append(sub)

            if days_left == 3:
                warning_3d.append(sub)

        if warning_3d:
            await self.telegram.owner_alert(
                "SUBSCRIPTIONS EXPIRE IN 3 DAYS",
                "\n\n".join(self._sub_text(s) for s in warning_3d)
            )

        if warning_1d:
            await self.telegram.owner_alert(
                "SUBSCRIPTIONS EXPIRE TOMORROW",
                "\n\n".join(self._sub_text(s) for s in warning_1d)
            )

        db.flush()

        return {
            "checked": len(subscribers),
            "expired": len(expired),
            "expired_ids": [sub.id for sub in expired],
            "warning_3d": len(warning_3d),
            "warning_1d": len(warning_1d),
        }

    def _as_aware(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _sub_text(self, sub: Subscriber) -> str:
        return (
            f"ID: {sub.id}\n"
            f"User: @{sub.username or '-'}\n"
            f"Telegram ID: {sub.telegram_user_id}\n"
            f"Name: {sub.full_name or '-'}\n"
            f"Plan: {sub.plan}\n"
            f"Expires: {sub.expires_at}"
        )