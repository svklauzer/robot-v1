from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session

from core.db import SessionLocal
from models.telegram_delivery import TelegramDelivery


class TelegramDeliveryLog:
    def record(
        self,
        chat_id: str | int,
        text: str,
        status: str,
        message_type: str = "message",
        error: str | None = None,
        attempts: int = 1,
    ) -> None:
        db = SessionLocal()
        try:
            delivery = TelegramDelivery(
                chat_id=str(chat_id),
                message_type=message_type,
                status=status,
                text_preview=(text or "")[:500],
                attempts=attempts,
                error=error,
            )
            db.add(delivery)
            db.commit()
        except Exception as exc:
            db.rollback()
            print(f"[TELEGRAM DELIVERY LOG ERROR] {type(exc).__name__}: {exc}")
        finally:
            db.close()

    def summary(self, db: Session, hours: int = 24) -> dict:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        rows = (
            db.query(TelegramDelivery)
            .filter(TelegramDelivery.created_at >= since)
            .all()
        )

        total = len(rows)
        sent = sum(1 for row in rows if row.status == "sent")
        failed = sum(1 for row in rows if row.status == "failed")
        by_type: dict[str, dict[str, int]] = {}
        last_error = None

        for row in rows:
            bucket = by_type.setdefault(row.message_type, {"total": 0, "sent": 0, "failed": 0})
            bucket["total"] += 1
            if row.status == "sent":
                bucket["sent"] += 1
            if row.status == "failed":
                bucket["failed"] += 1
                last_error = row.error or last_error

        return {
            "hours": hours,
            "total": total,
            "sent": sent,
            "failed": failed,
            "sla_pct": round((sent / total * 100), 2) if total else 100.0,
            "by_type": by_type,
            "last_error": last_error,
        }
