import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from core.db import SessionLocal, engine
from models.telegram_delivery import TelegramDelivery
from services.telegram_errors import is_retryable_telegram_error, sanitize_telegram_error

RETRYABLE_STATUSES = {"queued", "failed_retryable"}
FAILED_STATUSES = {"failed", "failed_retryable", "failed_final"}


class TelegramDeliveryLog:
    def record(
        self,
        chat_id: str | int,
        text: str,
        status: str,
        message_type: str = "message",
        error: str | None = None,
        attempts: int = 1,
        max_attempts: int = 3,
        next_retry_at: datetime | None = None,
        reply_markup: dict | None = None,
    ) -> None:
        db = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            sanitized_error = sanitize_telegram_error(error)
            effective_status = status
            effective_next_retry_at = next_retry_at
            if status in {"failed", "failed_retryable"} and not is_retryable_telegram_error(error):
                effective_status = "failed_final"
                effective_next_retry_at = None

            delivery = TelegramDelivery(
                chat_id=str(chat_id),
                message_type=message_type,
                status=effective_status,
                text=text,
                text_preview=(text or "")[:500],
                reply_markup_json=json.dumps(reply_markup) if reply_markup else None,
                attempts=attempts,
                max_attempts=max_attempts,
                error=sanitized_error,
                next_retry_at=effective_next_retry_at,
                last_attempt_at=now if attempts else None,
                sent_at=now if status == "sent" else None,
            )
            db.add(delivery)
            db.commit()
        except Exception as exc:
            db.rollback()
            print(f"[TELEGRAM DELIVERY LOG ERROR] {type(exc).__name__}: {exc}")
        finally:
            db.close()

    def queue(
        self,
        db: Session,
        chat_id: str | int,
        text_value: str,
        message_type: str = "message",
        max_attempts: int = 3,
        reply_markup: dict | None = None,
    ) -> TelegramDelivery:
        delivery = TelegramDelivery(
            chat_id=str(chat_id),
            message_type=message_type,
            status="queued",
            text=text_value,
            text_preview=(text_value or "")[:500],
            reply_markup_json=json.dumps(reply_markup) if reply_markup else None,
            attempts=0,
            max_attempts=max_attempts,
            next_retry_at=datetime.now(timezone.utc),
        )
        db.add(delivery)
        return delivery

    def mark_sent(self, db: Session, delivery: TelegramDelivery) -> None:
        now = datetime.now(timezone.utc)
        delivery.status = "sent"
        delivery.error = None
        delivery.next_retry_at = None
        delivery.last_attempt_at = now
        delivery.sent_at = now

    def mark_failed(self, db: Session, delivery: TelegramDelivery, error: str, base_delay_seconds: int = 60, retryable: bool | None = None) -> None:
        now = datetime.now(timezone.utc)
        delivery.attempts = int(delivery.attempts or 0) + 1
        delivery.error = sanitize_telegram_error(error)
        delivery.last_attempt_at = now

        should_retry = is_retryable_telegram_error(error) if retryable is None else retryable

        if not should_retry or delivery.attempts >= int(delivery.max_attempts or 3):
            delivery.status = "failed_final"
            delivery.next_retry_at = None
            return

        delivery.status = "failed_retryable"
        delay_seconds = base_delay_seconds * (2 ** max(delivery.attempts - 1, 0))
        delivery.next_retry_at = now + timedelta(seconds=delay_seconds)

    def due_for_retry(self, db: Session, limit: int = 25) -> list[TelegramDelivery]:
        now = datetime.now(timezone.utc)
        return (
            db.query(TelegramDelivery)
            .filter(TelegramDelivery.status.in_(RETRYABLE_STATUSES))
            .filter(TelegramDelivery.next_retry_at <= now)
            .order_by(TelegramDelivery.next_retry_at.asc(), TelegramDelivery.id.asc())
            .limit(limit)
            .all()
        )

    def summary(self, db: Session, hours: int = 24) -> dict:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        rows = (
            db.query(TelegramDelivery)
            .filter(TelegramDelivery.created_at >= since)
            .all()
        )

        total = len(rows)
        sent = sum(1 for row in rows if row.status == "sent")
        failed = sum(1 for row in rows if row.status in FAILED_STATUSES)
        queued = sum(1 for row in rows if row.status == "queued")
        retryable = sum(1 for row in rows if row.status == "failed_retryable")
        failed_final = sum(1 for row in rows if row.status == "failed_final")
        by_type: dict[str, dict[str, int]] = {}
        by_status: dict[str, int] = {}
        last_error = None

        for row in rows:
            by_status[row.status] = by_status.get(row.status, 0) + 1
            bucket = by_type.setdefault(
                row.message_type,
                {"total": 0, "sent": 0, "failed": 0, "queued": 0, "retryable": 0},
            )
            bucket["total"] += 1
            if row.status == "sent":
                bucket["sent"] += 1
            if row.status in FAILED_STATUSES:
                bucket["failed"] += 1
                last_error = sanitize_telegram_error(row.error) or last_error
            if row.status == "queued":
                bucket["queued"] += 1
            if row.status == "failed_retryable":
                bucket["retryable"] += 1

        delivered_or_failed = sent + failed

        return {
            "hours": hours,
            "total": total,
            "sent": sent,
            "failed": failed,
            "queued": queued,
            "retryable": retryable,
            "failed_final": failed_final,
            "sla_pct": round((sent / delivered_or_failed * 100), 2) if delivered_or_failed else 100.0,
            "by_status": by_status,
            "by_type": by_type,
            "last_error": last_error,
        }


def ensure_telegram_delivery_schema() -> None:
    """
    Lightweight compatibility shim until Alembic is introduced.
    create_all() does not add columns to existing dev/paper databases, so runtime
    startup must add nullable retry columns before health/readiness queries use them.
    """

    inspector = inspect(engine)
    if "telegram_deliveries" not in inspector.get_table_names():
        return

    existing = {col["name"] for col in inspector.get_columns("telegram_deliveries")}
    additions = {
        "text": "TEXT",
        "max_attempts": "INTEGER DEFAULT 3",
        "next_retry_at": "TIMESTAMP NULL",
        "last_attempt_at": "TIMESTAMP NULL",
        "sent_at": "TIMESTAMP NULL",
        "reply_markup_json": "TEXT",
    }

    with engine.begin() as conn:
        for column, ddl_type in additions.items():
            if column not in existing:
                conn.execute(text(f"ALTER TABLE telegram_deliveries ADD COLUMN {column} {ddl_type}"))
