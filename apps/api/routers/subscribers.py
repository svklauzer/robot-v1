from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends
from sqlalchemy import func, or_
from pydantic import BaseModel
from core.db import SessionLocal
from core.security import require_owner_action
from models.subscriber import Subscriber
from services.subscription_watchdog import SubscriptionWatchdog
from services.telegram_router import TelegramRouter

router = APIRouter(prefix="/subscribers", tags=["subscribers"])


class CreateSubscriberRequest(BaseModel):
    telegram_user_id: str
    username: str | None = None
    full_name: str | None = None
    plan: str = "vip"
    days: int = 30
    is_trial: bool = False
    notes: str | None = None


class ExtendSubscriberRequest(BaseModel):
    days: int = 30


class UpdateSubscriberStatusRequest(BaseModel):
    status: str


def _aware(value: datetime | None) -> datetime | None:
    """Приводит к UTC-aware. Колонки объявлены `DateTime(timezone=True)`, но флаг
    соблюдает Postgres, а SQLite отдаёт наивные значения — вычитание падало бы
    на «can't subtract offset-naive and offset-aware datetimes».
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _serialize(sub: Subscriber, now: datetime) -> dict:
    expires_at = _aware(sub.expires_at)
    return {
        "id": sub.id,
        "telegram_user_id": sub.telegram_user_id,
        "username": sub.username,
        "full_name": sub.full_name,
        "plan": sub.plan,
        "status": sub.status,
        "is_trial": sub.is_trial,
        "starts_at": str(sub.starts_at),
        "expires_at": str(expires_at) if expires_at else None,
        # Колонка `expires_at` объявлена NOT NULL, то есть подписки без срока
        # сейчас не бывает. Ветка на None оставлена как страховка на случай
        # смены схемы — но не как утверждение, что такие записи есть.
        "days_left": max((expires_at - now).days, 0) if expires_at else None,
        "notes": sub.notes,
        "created_at": str(sub.created_at),
    }


def _overview(db, now: datetime) -> dict:
    """Сводка по ВСЕЙ базе, не по странице и не по фильтру.

    Считается в SQL намеренно. Раньше карточки складывались на клиенте из
    полного списка — с постраничной выдачей тот же код молча начал бы показывать
    «Всего 50» при тысяче подписчиков, и заметить это было бы нечем.
    """
    def _count(*where):
        q = db.query(func.count(Subscriber.id))
        for clause in where:
            q = q.filter(clause)
        return int(q.scalar() or 0)

    soon_edge = now + timedelta(days=3)
    return {
        "total": _count(),
        "active": _count(Subscriber.status == "active"),
        "expired": _count(Subscriber.status == "expired"),
        "blocked": _count(Subscriber.status == "blocked"),
        "trial": _count(Subscriber.is_trial.is_(True)),
        "vip": _count(Subscriber.plan == "vip"),
        "expiring_soon": _count(
            Subscriber.status == "active",
            Subscriber.expires_at.isnot(None),
            Subscriber.expires_at <= soon_edge,
            Subscriber.expires_at >= now,
        ),
    }


@router.get("", dependencies=[Depends(require_owner_action)])
def list_subscribers(limit: int = 100, offset: int = 0,
                     status: str | None = None, plan: str | None = None,
                     trial: str | None = None, q: str | None = None):
    """Страница подписчиков + сводка по всей базе.

    (#subscribers-paging-2026-09-07) Раньше отдавался весь список одним куском,
    а фильтры и сводка считались на клиенте. Постраничная выдача сама по себе
    сломала бы и то, и другое незаметно: фильтр отбирал бы внутри страницы, а
    карточки показывали бы размер страницы вместо размера базы. Поэтому фильтры
    и счётчики переехали в SQL вместе с пагинацией, а не после неё.
    """
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))

        query = db.query(Subscriber)
        if status and status != "all":
            query = query.filter(Subscriber.status == status)
        if plan and plan != "all":
            query = query.filter(Subscriber.plan == plan)
        if trial == "trial":
            query = query.filter(Subscriber.is_trial.is_(True))
        elif trial == "paid":
            query = query.filter(Subscriber.is_trial.is_(False))
        if q and q.strip():
            like = f"%{q.strip()}%"
            query = query.filter(or_(
                Subscriber.telegram_user_id.ilike(like),
                Subscriber.username.ilike(like),
                Subscriber.full_name.ilike(like),
                Subscriber.notes.ilike(like),
            ))

        total = int(query.with_entities(func.count(Subscriber.id)).scalar() or 0)
        rows = (query.order_by(Subscriber.id.desc())
                .limit(limit).offset(offset).all())

        return {
            "total": total,          # сколько подходит под ФИЛЬТР
            "limit": limit,
            "offset": offset,
            "items": [_serialize(row, now) for row in rows],
            "overview": _overview(db, now),   # сколько всего в базе
        }
    finally:
        db.close()


@router.post("", dependencies=[Depends(require_owner_action)])
async def create_subscriber(payload: CreateSubscriberRequest):
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=payload.days)

        existing = (
            db.query(Subscriber)
            .filter(Subscriber.telegram_user_id == payload.telegram_user_id)
            .first()
        )

        if existing:
            existing.username = payload.username or existing.username
            existing.full_name = payload.full_name or existing.full_name
            existing.plan = payload.plan
            existing.status = "active"
            existing.expires_at = expires_at
            existing.is_trial = payload.is_trial
            existing.notes = payload.notes
            sub = existing
        else:
            sub = Subscriber(
                telegram_user_id=payload.telegram_user_id,
                username=payload.username,
                full_name=payload.full_name,
                plan=payload.plan,
                status="active",
                starts_at=now,
                expires_at=expires_at,
                is_trial=payload.is_trial,
                notes=payload.notes,
            )
            db.add(sub)

        db.commit()
        db.refresh(sub)

        telegram = TelegramRouter()
        await telegram.owner_alert(
            "SUBSCRIBER ACTIVE",
            f"{sub.full_name or sub.username or sub.telegram_user_id}\n"
            f"Plan: {sub.plan}\n"
            f"Expires: {sub.expires_at}",
        )
        return {"status": "ok", "subscriber_id": sub.id, "expires_at": str(sub.expires_at)}

    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        db.close()


@router.post("/{subscriber_id}/extend", dependencies=[Depends(require_owner_action)])
async def extend_subscriber(subscriber_id: int, payload: ExtendSubscriberRequest):
    db = SessionLocal()
    try:
        sub = db.query(Subscriber).filter(Subscriber.id == subscriber_id).first()
        if not sub:
            return {"status": "error", "error": "subscriber_not_found"}

        now = datetime.now(timezone.utc)
        base = sub.expires_at if sub.expires_at and sub.expires_at > now else now
        sub.expires_at = base + timedelta(days=payload.days)
        sub.status = "active"
        db.commit()

        telegram = TelegramRouter()
        await telegram.owner_alert(
            "SUBSCRIBER EXTENDED",
            f"{sub.full_name or sub.username or sub.telegram_user_id}\n"
            f"+{payload.days} days\n"
            f"New expiry: {sub.expires_at}",
        )
        return {"status": "ok", "subscriber_id": sub.id, "expires_at": str(sub.expires_at)}

    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        db.close()


@router.post("/{subscriber_id}/status", dependencies=[Depends(require_owner_action)])
def update_subscriber_status(subscriber_id: int, payload: UpdateSubscriberStatusRequest):
    db = SessionLocal()
    try:
        sub = db.query(Subscriber).filter(Subscriber.id == subscriber_id).first()
        if not sub:
            return {"status": "error", "error": "subscriber_not_found"}
        sub.status = payload.status
        db.commit()
        return {"status": "ok", "subscriber_id": sub.id, "new_status": sub.status}
    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        db.close()


@router.post("/check-expirations", dependencies=[Depends(require_owner_action)])
async def check_subscriber_expirations():
    db = SessionLocal()
    try:
        service = SubscriptionWatchdog()
        result = await service.check_subscriptions(db)
        db.commit()
        return {"status": "ok", "result": result}
    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        db.close()
