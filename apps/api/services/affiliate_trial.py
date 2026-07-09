from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from core.config import settings
from models.subscriber import Subscriber


class AffiliateTrialService:
    """HTX affiliate → free VIP trial activation flow.

    MVP assumption: the Telegram bot lets a user claim the trial after following
    the HTX affiliate link. Fraud/registration verification can be moved to an
    HTX/provider webhook later, but the subscriber record is tagged so the trial
    is not granted repeatedly.
    """

    marker = "affiliate_htx_trial"

    def _as_aware(self, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    def activate_htx_trial(
        self,
        db: Session,
        telegram_user_id: str,
        username: str | None = None,
        full_name: str | None = None,
    ) -> tuple[Subscriber | None, bool, str]:
        if not telegram_user_id:
            return None, False, "telegram_user_id_missing"

        days = max(int(settings.AFFILIATE_FREE_VIP_DAYS or 30), 1)
        now = datetime.now(timezone.utc)
        subscriber = db.query(Subscriber).filter(Subscriber.telegram_user_id == str(telegram_user_id)).first()

        current_expiry = self._as_aware(subscriber.expires_at) if subscriber else None

        if subscriber and subscriber.status == "active" and not subscriber.is_trial and current_expiry and current_expiry > now:
            return subscriber, False, "paid_subscription_already_active"

        if subscriber and self.marker in (subscriber.notes or ""):
            return subscriber, False, "affiliate_trial_already_claimed"

        expires_at = now + timedelta(days=days)
        note = (
            f"{self.marker}; source=htx_affiliate; days={days}; "
            f"activated_at={now.isoformat()}; link={settings.HTX_AFFILIATE_LINK or '-'}"
        )

        if subscriber:
            subscriber.username = username or subscriber.username
            subscriber.full_name = full_name or subscriber.full_name
            subscriber.plan = "affiliate_htx_vip"
            subscriber.status = "active"
            subscriber.starts_at = now
            subscriber.expires_at = expires_at
            subscriber.is_trial = True
            subscriber.notes = f"{subscriber.notes}; {note}" if subscriber.notes else note
        else:
            subscriber = Subscriber(
                telegram_user_id=str(telegram_user_id),
                username=username,
                full_name=full_name,
                plan="affiliate_htx_vip",
                status="active",
                starts_at=now,
                expires_at=expires_at,
                is_trial=True,
                notes=note,
            )
            db.add(subscriber)
            db.flush()

        return subscriber, True, "affiliate_trial_activated"
