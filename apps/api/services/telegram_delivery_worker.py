from __future__ import annotations

import json
import logging

import httpx
from sqlalchemy.orm import Session

from core.config import settings
from core.logging import get_logger, log_event
from services.telegram_delivery_log import TelegramDeliveryLog
from services.telegram_errors import is_retryable_telegram_error, sanitize_telegram_error

logger = get_logger(__name__)

# Owner alert when hard-failure rate exceeds this percent of processed deliveries.
_FAILURE_ALERT_THRESHOLD_PCT: float = 5.0


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

            if delivery.error and not is_retryable_telegram_error(delivery.error):
                delivery.status = "failed_final"
                delivery.error = sanitize_telegram_error(delivery.error)
                delivery.next_retry_at = None
                failed_final += 1
                processed += 1
                continue

            try:
                reply_markup = (
                    json.loads(delivery.reply_markup_json)
                    if delivery.reply_markup_json
                    else None
                )
                await self._send_telegram_http(
                    str(delivery.chat_id), str(delivery.text), reply_markup=reply_markup
                )
                self.log.mark_sent(db, delivery)
                sent += 1
            except Exception as exc:
                self.log.mark_failed(
                    db,
                    delivery,
                    f"{type(exc).__name__}: {repr(exc)}",
                    retryable=is_retryable_telegram_error(exc),
                )
                if delivery.status == "failed_final":
                    failed_final += 1
                else:
                    failed_retryable += 1
            finally:
                processed += 1

        result = {
            "processed": processed,
            "sent": sent,
            "failed_retryable": failed_retryable,
            "failed_final": failed_final,
        }

        if processed > 0:
            failure_pct = failed_final / processed * 100
            result["failure_pct"] = round(failure_pct, 1)
            if failure_pct > _FAILURE_ALERT_THRESHOLD_PCT:
                log_event(
                    logger, logging.WARNING, "telegram_delivery_sla_breach",
                    failure_pct=round(failure_pct, 1),
                    failed_final=failed_final,
                    processed=processed,
                    threshold_pct=_FAILURE_ALERT_THRESHOLD_PCT,
                )
                await self._send_owner_sla_alert(failure_pct, failed_final, processed)

        return result

    async def _send_telegram_http(
        self,
        chat_id: str,
        text: str,
        reply_markup: dict | None = None,
    ) -> None:
        """Send one Telegram message, forcing IPv4 to avoid Happy Eyeballs timeout."""
        url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
        payload: dict = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        # Force IPv4 — same fix as SignalBroadcaster; Docker on Windows drops IPv6.
        transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
        timeout = httpx.Timeout(
            connect=float(getattr(settings, "TELEGRAM_CONNECT_TIMEOUT", 15.0)),
            read=float(getattr(settings, "TELEGRAM_READ_TIMEOUT", 30.0)),
            write=10.0,
            pool=5.0,
        )
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()

    async def _send_owner_sla_alert(
        self, failure_pct: float, failed_final: int, processed: int
    ) -> None:
        """Fire a Telegram owner alert when delivery SLA is breached."""
        try:
            owner_chat_id = str(getattr(settings, "TELEGRAM_OWNER_CHAT_ID", "") or "")
            if not owner_chat_id:
                return
            text = (
                f"⚠️ DELIVERY SLA BREACH\n\n"
                f"Hard failures: {failed_final}/{processed} ({failure_pct:.1f}%)\n"
                f"Threshold: {_FAILURE_ALERT_THRESHOLD_PCT:.0f}%\n\n"
                f"Check /telegram/deliveries/summary for details."
            )
            await self._send_telegram_http(owner_chat_id, text)
        except Exception as e:
            log_event(
                logger, logging.WARNING, "telegram_delivery_owner_alert_failed",
                error=str(e),
            )
