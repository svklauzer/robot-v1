from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends
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


@router.get("", dependencies=[Depends(require_owner_action)])
def list_subscribers():
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        subs = db.query(Subscriber).order_by(Subscriber.id.desc()).all()
        return [
            {
                "id": s.id,
                "telegram_user_id": s.telegram_user_id,
                "username": s.username,
                "full_name": s.full_name,
                "plan": s.plan,
                "status": s.status,
                "is_trial": s.is_trial,
                "starts_at": str(s.starts_at),
                "expires_at": str(s.expires_at),
                "days_left": max((s.expires_at - now).days, 0) if s.expires_at else 0,
                "notes": s.notes,
                "created_at": str(s.created_at),
            }
            for s in subs
        ]
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
