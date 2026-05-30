from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4
from sqlalchemy.orm import Session

from core.config import settings
from models.payment import BillingPlan, Payment, PaymentEvent
from models.subscriber import Subscriber


DEFAULT_BILLING_PLANS = [
    {
        "code": "vip_30",
        "title": "VIP 30 дней",
        "amount_usdt": 49.0,
        "currency": "USDT",
        "duration_days": 30,
        "description": "Полные VIP сигналы, уровни, сопровождение и отчёты на 30 дней.",
    },
    {
        "code": "vip_90",
        "title": "VIP 90 дней",
        "amount_usdt": 129.0,
        "currency": "USDT",
        "duration_days": 90,
        "description": "Полные VIP сигналы и сопровождение на 90 дней.",
    },
]


class BillingService:
    def ensure_default_plans(self, db: Session) -> list[BillingPlan]:
        plans: list[BillingPlan] = []

        for item in DEFAULT_BILLING_PLANS:
            plan = db.query(BillingPlan).filter(BillingPlan.code == item["code"]).first()
            if not plan:
                plan = BillingPlan(**item, is_active=True)
                db.add(plan)
                db.flush()
            plans.append(plan)

        return plans

    def list_plans(self, db: Session, active_only: bool = True) -> list[BillingPlan]:
        self.ensure_default_plans(db)
        query = db.query(BillingPlan).order_by(BillingPlan.amount_usdt.asc())
        if active_only:
            query = query.filter(BillingPlan.is_active.is_(True))
        return query.all()

    def get_plan(self, db: Session, plan_code: str) -> BillingPlan | None:
        self.ensure_default_plans(db)
        return (
            db.query(BillingPlan)
            .filter(BillingPlan.code == plan_code, BillingPlan.is_active.is_(True))
            .first()
        )

    def create_checkout(
        self,
        db: Session,
        telegram_user_id: str,
        plan_code: str = "vip_30",
        username: str | None = None,
        full_name: str | None = None,
        provider: str = "manual",
        notes: str | None = None,
    ) -> Payment:
        plan = self.get_plan(db, plan_code)
        if not plan:
            raise ValueError("billing_plan_not_found")

        provider_payment_id = f"manual-{telegram_user_id}-{plan.code}-{uuid4().hex[:12]}"
        invite = settings.VIP_INVITE_LINK or "VIP invite будет выдан после подтверждения оплаты"
        checkout_url = invite if provider == "manual" else None

        payment = Payment(
            telegram_user_id=str(telegram_user_id),
            username=username,
            full_name=full_name,
            plan_code=plan.code,
            amount=float(plan.amount_usdt),
            currency=plan.currency,
            duration_days=int(plan.duration_days),
            provider=provider,
            provider_payment_id=provider_payment_id,
            status="pending",
            checkout_url=checkout_url,
            notes=notes,
        )
        db.add(payment)
        db.flush()
        return payment

    def confirm_payment(
        self,
        db: Session,
        payment_id: int,
        provider_event_id: str | None = None,
        raw_payload: str | None = None,
    ) -> tuple[Payment, Subscriber, bool]:
        payment = db.query(Payment).filter(Payment.id == payment_id).first()
        if not payment:
            raise ValueError("payment_not_found")

        existing_subscriber = None
        if payment.subscriber_id:
            existing_subscriber = db.query(Subscriber).filter(Subscriber.id == payment.subscriber_id).first()

        if payment.status == "paid" and existing_subscriber:
            return payment, existing_subscriber, False

        now = datetime.now(timezone.utc)
        subscriber = existing_subscriber or (
            db.query(Subscriber)
            .filter(Subscriber.telegram_user_id == payment.telegram_user_id)
            .first()
        )

        if subscriber:
            subscriber.username = payment.username or subscriber.username
            subscriber.full_name = payment.full_name or subscriber.full_name
            subscriber.plan = payment.plan_code
            subscriber.status = "active"
            subscriber.is_trial = False
            base = subscriber.expires_at if subscriber.expires_at and subscriber.expires_at > now else now
            subscriber.expires_at = base + timedelta(days=payment.duration_days)
        else:
            subscriber = Subscriber(
                telegram_user_id=payment.telegram_user_id,
                username=payment.username,
                full_name=payment.full_name,
                plan=payment.plan_code,
                status="active",
                starts_at=now,
                expires_at=now + timedelta(days=payment.duration_days),
                is_trial=False,
                notes="activated_by_payment",
            )
            db.add(subscriber)
            db.flush()

        payment.status = "paid"
        payment.subscriber_id = subscriber.id
        payment.paid_at = payment.paid_at or now
        payment.raw_payload = raw_payload or payment.raw_payload
        if provider_event_id:
            payment.provider_payment_id = payment.provider_payment_id or provider_event_id

        db.flush()
        return payment, subscriber, True

    def process_payment_event(
        self,
        db: Session,
        payment_id: int,
        provider: str,
        provider_event_id: str,
        status: str = "paid",
        raw_payload: str | None = None,
    ) -> tuple[Payment, Subscriber | None, bool, PaymentEvent]:
        existing_event = (
            db.query(PaymentEvent)
            .filter(PaymentEvent.provider == provider, PaymentEvent.provider_event_id == provider_event_id)
            .first()
        )
        if existing_event:
            payment = db.query(Payment).filter(Payment.id == existing_event.payment_id).first()
            subscriber = None
            if existing_event.subscriber_id:
                subscriber = db.query(Subscriber).filter(Subscriber.id == existing_event.subscriber_id).first()
            if not payment:
                raise ValueError("payment_event_without_payment")
            return payment, subscriber, False, existing_event

        payment = db.query(Payment).filter(Payment.id == payment_id).first()
        if not payment:
            raise ValueError("payment_not_found")

        now = datetime.now(timezone.utc)
        event = PaymentEvent(
            payment_id=payment.id,
            provider=provider,
            provider_event_id=provider_event_id,
            event_type="payment_status",
            status=status,
            amount=payment.amount,
            currency=payment.currency,
            raw_payload=raw_payload,
            processed_at=now,
        )
        db.add(event)
        db.flush()

        subscriber = None
        activated = False
        if status == "paid":
            payment, subscriber, activated = self.confirm_payment(
                db=db,
                payment_id=payment.id,
                provider_event_id=None,
                raw_payload=raw_payload,
            )
            event.subscriber_id = subscriber.id
        elif status in ["failed", "canceled", "refunded"]:
            payment.status = status
            payment.raw_payload = raw_payload or payment.raw_payload

        db.flush()
        return payment, subscriber, activated, event

    def serialize_plan(self, plan: BillingPlan) -> dict:
        return {
            "id": plan.id,
            "code": plan.code,
            "title": plan.title,
            "amount_usdt": plan.amount_usdt,
            "currency": plan.currency,
            "duration_days": plan.duration_days,
            "is_active": plan.is_active,
            "description": plan.description,
        }

    def serialize_payment(self, payment: Payment) -> dict:
        return {
            "id": payment.id,
            "subscriber_id": payment.subscriber_id,
            "telegram_user_id": payment.telegram_user_id,
            "username": payment.username,
            "full_name": payment.full_name,
            "plan_code": payment.plan_code,
            "amount": payment.amount,
            "currency": payment.currency,
            "duration_days": payment.duration_days,
            "provider": payment.provider,
            "provider_payment_id": payment.provider_payment_id,
            "status": payment.status,
            "checkout_url": payment.checkout_url,
            "notes": payment.notes,
            "created_at": str(payment.created_at),
            "updated_at": str(payment.updated_at),
            "paid_at": str(payment.paid_at) if payment.paid_at else None,
        }

    def serialize_payment_event(self, event: PaymentEvent) -> dict:
        return {
            "id": event.id,
            "payment_id": event.payment_id,
            "subscriber_id": event.subscriber_id,
            "provider": event.provider,
            "provider_event_id": event.provider_event_id,
            "event_type": event.event_type,
            "status": event.status,
            "amount": event.amount,
            "currency": event.currency,
            "processed_at": str(event.processed_at) if event.processed_at else None,
            "created_at": str(event.created_at),
        }

    def summary(self, db: Session) -> dict:
        payments = db.query(Payment).all()
        paid = [p for p in payments if p.status == "paid"]
        pending = [p for p in payments if p.status == "pending"]
        failed = [p for p in payments if p.status in ["failed", "canceled", "refunded", "expired"]]

        events = db.query(PaymentEvent).all()

        return {
            "total": len(payments),
            "paid": len(paid),
            "pending": len(pending),
            "failed": len(failed),
            "cash_collected": round(sum(float(p.amount or 0) for p in paid), 6),
            "currency": "USDT",
            "events_total": len(events),
            "events_processed": sum(1 for e in events if e.processed_at is not None),
        }
