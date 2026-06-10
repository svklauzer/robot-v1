from fastapi import APIRouter, Depends
from pydantic import BaseModel
from core.db import SessionLocal
from core.security import require_owner_action
from models.payment import Payment, PaymentEvent
from services.billing_service import BillingService
from services.revenue_metrics import RevenueMetricsService
from services.payment_reconciliation import PaymentReconciliationService
from services.customer_notifications import CustomerNotificationService
from services.audit_log import AuditLogService
from services.telegram_router import TelegramRouter

router = APIRouter(prefix="/payments", tags=["payments"])


class CreateCheckoutRequest(BaseModel):
    telegram_user_id: str
    plan_code: str = "vip_30"
    username: str | None = None
    full_name: str | None = None
    provider: str = "manual"
    notes: str | None = None


class ManualConfirmPaymentRequest(BaseModel):
    provider_event_id: str | None = None
    raw_payload: str | None = None


class PaymentEventRequest(BaseModel):
    payment_id: int
    provider: str = "manual"
    provider_event_id: str
    status: str = "paid"
    raw_payload: str | None = None


class PaymentReconcileRequest(BaseModel):
    older_than_hours: int | None = None


@router.get("/plans")
def list_payment_plans():
    db = SessionLocal()
    try:
        service = BillingService()
        plans = service.list_plans(db)
        db.commit()
        return [service.serialize_plan(plan) for plan in plans]
    finally:
        db.close()


@router.post("/checkout", dependencies=[Depends(require_owner_action)])
def create_payment_checkout(payload: CreateCheckoutRequest):
    db = SessionLocal()
    try:
        service = BillingService()
        payment = service.create_checkout(
            db=db,
            telegram_user_id=payload.telegram_user_id,
            plan_code=payload.plan_code,
            username=payload.username,
            full_name=payload.full_name,
            provider=payload.provider,
            notes=payload.notes,
        )
        db.commit()
        db.refresh(payment)
        return {"status": "ok", "payment": service.serialize_payment(payment)}
    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        db.close()


@router.get("", dependencies=[Depends(require_owner_action)])
def list_payments(limit: int = 100, status: str | None = None):
    db = SessionLocal()
    try:
        limit = min(max(limit, 1), 500)
        query = db.query(Payment)
        if status:
            query = query.filter(Payment.status == status)
        payments = query.order_by(Payment.id.desc()).limit(limit).all()
        service = BillingService()
        return {
            "summary": service.summary(db),
            "items": [service.serialize_payment(p) for p in payments],
        }
    finally:
        db.close()


@router.post("/{payment_id}/manual-confirm", dependencies=[Depends(require_owner_action)])
async def manual_confirm_payment(payment_id: int, payload: ManualConfirmPaymentRequest | None = None):
    db = SessionLocal()
    try:
        service = BillingService()
        event = None
        if payload and payload.provider_event_id:
            payment, subscriber, activated, event = service.process_payment_event(
                db=db,
                payment_id=payment_id,
                provider="manual",
                provider_event_id=payload.provider_event_id,
                status="paid",
                raw_payload=payload.raw_payload,
            )
        else:
            payment, subscriber, activated = service.confirm_payment(
                db=db,
                payment_id=payment_id,
                raw_payload=payload.raw_payload if payload else None,
            )
        notification = CustomerNotificationService().queue_payment_success(db, payment, subscriber, activated)
        AuditLogService().record(
            db,
            action="payment_manual_confirm",
            resource_type="payment",
            resource_id=payment.id,
            details={"activated": activated, "subscriber_id": subscriber.id, "customer_notification": notification},
        )
        db.commit()
        await TelegramRouter().owner_alert(
            "PAYMENT CONFIRMED",
            (
                f"Payment #{payment.id} {payment.amount} {payment.currency}\n"
                f"User: {subscriber.telegram_user_id}\n"
                f"Plan: {subscriber.plan}\n"
                f"Expires: {subscriber.expires_at}\n"
                f"Activated now: {activated}"
            ),
        )
        return {
            "status": "ok",
            "activated": activated,
            "payment": service.serialize_payment(payment),
            "subscriber_id": subscriber.id,
            "expires_at": str(subscriber.expires_at),
            "customer_notification": notification,
            "payment_event": service.serialize_payment_event(event) if event else None,
        }
    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        db.close()


@router.get("/events", dependencies=[Depends(require_owner_action)])
def list_payment_events(limit: int = 100):
    db = SessionLocal()
    try:
        limit = min(max(limit, 1), 500)
        service = BillingService()
        events = db.query(PaymentEvent).order_by(PaymentEvent.id.desc()).limit(limit).all()
        return {
            "items": [service.serialize_payment_event(e) for e in events],
            "summary": service.summary(db),
        }
    finally:
        db.close()


@router.post("/events", dependencies=[Depends(require_owner_action)])
def process_payment_event(payload: PaymentEventRequest):
    db = SessionLocal()
    try:
        service = BillingService()
        payment, subscriber, activated, event = service.process_payment_event(
            db=db,
            payment_id=payload.payment_id,
            provider=payload.provider,
            provider_event_id=payload.provider_event_id,
            status=payload.status,
            raw_payload=payload.raw_payload,
        )
        notification = CustomerNotificationService().queue_payment_success(db, payment, subscriber, activated)
        AuditLogService().record(
            db,
            action="payment_event_processed",
            resource_type="payment",
            resource_id=payment.id,
            details={"event_id": event.id, "status": event.status, "activated": activated, "customer_notification": notification},
        )
        db.commit()
        db.refresh(payment)
        db.refresh(event)
        return {
            "status": "ok",
            "activated": activated,
            "payment": service.serialize_payment(payment),
            "subscriber_id": subscriber.id if subscriber else None,
            "customer_notification": notification,
            "payment_event": service.serialize_payment_event(event),
        }
    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        db.close()


@router.get("/summary", dependencies=[Depends(require_owner_action)])
def payments_summary():
    db = SessionLocal()
    try:
        return BillingService().summary(db)
    finally:
        db.close()


@router.post("/reconcile", dependencies=[Depends(require_owner_action)])
def reconcile_payments(payload: PaymentReconcileRequest | None = None):
    db = SessionLocal()
    try:
        result = PaymentReconciliationService().reconcile_pending(
            db,
            older_than_hours=payload.older_than_hours if payload else None,
            audit_log=AuditLogService(),
        )
        db.commit()
        return result
    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        db.close()


@router.get("/revenue", dependencies=[Depends(require_owner_action)])
def payments_revenue(window_days: int = 30):
    db = SessionLocal()
    try:
        window_days = min(max(window_days, 1), 365)
        return RevenueMetricsService().summary(db, window_days=window_days)
    finally:
        db.close()
