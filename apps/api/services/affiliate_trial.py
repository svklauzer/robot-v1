from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from core.config import settings
from models.subscriber import Subscriber

# (#okx-affiliate-2026-09-08) Площадки партнёрки. Маркер пишется в notes и
# служит признаком «этот триал уже брали»; для HTX он обязан остаться прежним —
# по нему опознаются записи, созданные до появления второй площадки.
VENUES: dict[str, dict[str, str]] = {
    "htx": {"marker": "affiliate_htx_trial", "plan": "affiliate_htx_vip",
            "label": "HTX", "link_setting": "HTX_AFFILIATE_LINK"},
    "okx": {"marker": "affiliate_okx_trial", "plan": "affiliate_okx_vip",
            "label": "OKX", "link_setting": "OKX_AFFILIATE_LINK"},
}

_ANY_MARKER = tuple(v["marker"] for v in VENUES.values())


def venue_link(venue: str) -> str:
    meta = VENUES.get(venue) or {}
    return str(getattr(settings, meta.get("link_setting", ""), "") or "")


def configured_venues() -> list[str]:
    """Площадки, у которых задана ссылка. Кнопку без ссылки не показываем:
    предложение «зарегистрируйтесь по ссылке» без ссылки — тупик."""
    return [name for name in VENUES if venue_link(name)]


class AffiliateTrialService:
    """Партнёрская регистрация → бесплатный VIP.

    Допущение MVP осталось прежним: бот верит нажатию «я зарегистрировался».
    Проверка регистрации есть только у HTX (affiliate-API) и включается флагом;
    у OKX её нет. Запись подписчика помечается маркером, чтобы триал не
    выдавался повторно.

    (#okx-affiliate-2026-09-08) Площадок стало две, и по умолчанию льготный
    период — ОДИН на человека, а не по одному на площадку: без автопроверки
    «я зарегистрировался» это просто нажатие кнопки, и два месяца подряд
    достаются бесплатно любому желающему.
    """

    # Совместимость: код и тесты ссылались на marker до появления площадок.
    marker = VENUES["htx"]["marker"]

    def _as_aware(self, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    def _already_claimed(self, subscriber: Subscriber | None, venue: str) -> bool:
        if subscriber is None:
            return False
        notes = subscriber.notes or ""
        if bool(getattr(settings, "AFFILIATE_TRIAL_ONE_PER_USER", True)):
            return any(m in notes for m in _ANY_MARKER)
        return VENUES[venue]["marker"] in notes

    def activate_trial(
        self,
        db: Session,
        venue: str,
        telegram_user_id: str,
        username: str | None = None,
        full_name: str | None = None,
    ) -> tuple[Subscriber | None, bool, str]:
        venue = str(venue or "").lower().strip()
        if venue not in VENUES:
            return None, False, "unknown_affiliate_venue"
        if not telegram_user_id:
            return None, False, "telegram_user_id_missing"

        meta = VENUES[venue]
        days = max(int(settings.AFFILIATE_FREE_VIP_DAYS or 30), 1)
        now = datetime.now(timezone.utc)
        subscriber = (db.query(Subscriber)
                      .filter(Subscriber.telegram_user_id == str(telegram_user_id))
                      .first())

        current_expiry = self._as_aware(subscriber.expires_at) if subscriber else None

        # Платная подписка не подменяется триалом: это было бы понижением срока.
        if (subscriber and subscriber.status == "active" and not subscriber.is_trial
                and current_expiry and current_expiry > now):
            return subscriber, False, "paid_subscription_already_active"

        if self._already_claimed(subscriber, venue):
            return subscriber, False, "affiliate_trial_already_claimed"

        expires_at = now + timedelta(days=days)
        note = (
            f"{meta['marker']}; source={venue}_affiliate; days={days}; "
            f"activated_at={now.isoformat()}; link={venue_link(venue) or '-'}"
        )

        if subscriber:
            subscriber.username = username or subscriber.username
            subscriber.full_name = full_name or subscriber.full_name
            subscriber.plan = meta["plan"]
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
                plan=meta["plan"],
                status="active",
                starts_at=now,
                expires_at=expires_at,
                is_trial=True,
                notes=note,
            )
            db.add(subscriber)
            db.flush()

        return subscriber, True, "affiliate_trial_activated"

    def activate_htx_trial(
        self,
        db: Session,
        telegram_user_id: str,
        username: str | None = None,
        full_name: str | None = None,
    ) -> tuple[Subscriber | None, bool, str]:
        """Прежняя точка входа. Оставлена: на неё ссылается вебхук проверки UID,
        и её поведение не изменилось."""
        return self.activate_trial(
            db=db, venue="htx", telegram_user_id=telegram_user_id,
            username=username, full_name=full_name,
        )
