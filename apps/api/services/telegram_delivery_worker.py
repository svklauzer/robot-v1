import httpx
from sqlalchemy.orm import Session

from core.config import settings
from services.telegram_delivery_log import TelegramDeliveryLog


class TelegramDeliveryWorker:
    def __init__(self, log: TelegramDeliveryLog | None = None):
        self.log = log or TelegramDeliveryLog()

    async def process_once(self, db: Session, limit: int = 25) -> dict:
        deliveries = self.log.due_for_retry(db, limit=limit)
        processed = 0
        sent = 0
        failed_retryable = 0
        failed_final = 0

        for delivery in deliveries:
            if not delivery.text:
                delivery.status = "failed_final"
                delivery.error = "missing_full_text_for_retry"
                failed_final += 1
                processed += 1
                continue

            try:
                await self._send_telegram_http(str(delivery.chat_id), str(delivery.text))
                self.log.mark_sent(db, delivery)
                sent += 1
            except Exception as exc:
                self.log.mark_failed(
                    db,
                    delivery,
                    f"{type(exc).__name__}: {repr(exc)}",
                )
                if delivery.status == "failed_final":
                    failed_final += 1
                else:
                    failed_retryable += 1
            finally:
                processed += 1

        return {
            "processed": processed,
            "sent": sent,
            "failed_retryable": failed_retryable,
            "failed_final": failed_final,
        }

    async def _send_telegram_http(self, chat_id: str, text: str) -> None:
        url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
            )
            response.raise_for_status()
