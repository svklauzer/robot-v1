import asyncio
import logging

import httpx
from datetime import datetime, timedelta, timezone

from core.config import settings
from core.logging import get_logger, log_event
from services.telegram_delivery_log import TelegramDeliveryLog
from services.telegram_errors import is_retryable_telegram_error, sanitize_telegram_error

logger = get_logger(__name__)

# ── HTTP client factory ──────────────────────────────────────────────────────

def _build_telegram_client() -> httpx.AsyncClient:
    """
    Build an httpx client for Telegram API.

    - Supports proxy via TELEGRAM_PROXY_URL (socks5:// or http://).
    - Forces IPv4 connections via local_address="0.0.0.0" on the transport.
      This defeats the Happy Eyeballs dual-stack race that causes ConnectTimeout
      inside Docker on Windows (where IPv6 routes don't work).
    - Separate connect / read timeouts so network blocks are detected quickly.
    """
    timeout = httpx.Timeout(
        connect=float(getattr(settings, "TELEGRAM_CONNECT_TIMEOUT", 15.0)),
        read=float(getattr(settings, "TELEGRAM_READ_TIMEOUT", 30.0)),
        write=10.0,
        pool=5.0,
    )

    proxy_url = str(getattr(settings, "TELEGRAM_PROXY_URL", "") or "").strip()

    if proxy_url:
        log_event(
            logger,
            logging.DEBUG,
            "telegram_using_proxy",
            proxy=proxy_url[:30] + "..." if len(proxy_url) > 30 else proxy_url,
        )
        return httpx.AsyncClient(timeout=timeout, proxy=proxy_url)

    # Bind transport to 0.0.0.0 (IPv4 wildcard) — prevents httpcore/asyncio from
    # attempting IPv6 connections that time out in Docker-on-Windows environments.
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    return httpx.AsyncClient(timeout=timeout, transport=transport)


class SignalBroadcaster:
    def __init__(self):
        self.delivery_log = TelegramDeliveryLog()

    async def _send_telegram_http(
        self,
        chat_id: str,
        text: str,
        reply_markup: dict | None = None,
        *,
        retries: int = 3,
        retry_delay: float = 2.0,
    ) -> dict:
        """
        POST sendMessage to Telegram API with retry on transient errors.

        Retry policy:
        - ConnectTimeout / NetworkError → retry up to `retries` times
        - HTTP 429 (rate limit) → retry after Retry-After header or 5s
        - HTTP 4xx (bad request) → no retry (permanent failure)
        - HTTP 5xx → retry
        """
        url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"

        payload: dict = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        last_error: Exception | None = None

        for attempt in range(1, retries + 1):
            try:
                async with _build_telegram_client() as client:
                    response = await client.post(url, json=payload)

                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 5))
                    log_event(
                        logger, logging.WARNING, "telegram_rate_limited",
                        chat_id=chat_id, retry_after=retry_after, attempt=attempt,
                    )
                    await asyncio.sleep(retry_after)
                    continue

                if response.status_code >= 400:
                    body = response.text[:300]
                    if response.status_code < 500:
                        # Permanent client error — don't retry
                        raise RuntimeError(
                            f"TelegramHTTPError:{response.status_code}:{body}"
                        )
                    # 5xx — retry
                    raise RuntimeError(f"TelegramServerError:{response.status_code}:{body}")

                return {"ok": True, "chat_id": chat_id}

            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.NetworkError) as e:
                last_error = e
                log_event(
                    logger, logging.WARNING, "telegram_network_error",
                    chat_id=chat_id, attempt=attempt, retries=retries,
                    error=f"{type(e).__name__}: {e}",
                )
                if attempt < retries:
                    await asyncio.sleep(retry_delay * attempt)

            except Exception as e:
                last_error = e
                if attempt < retries and "TelegramServerError" in str(e):
                    await asyncio.sleep(retry_delay * attempt)
                    continue
                break

        raise last_error or RuntimeError("telegram_send_failed_unknown")

    async def send_message(
        self,
        chat_id: str,
        text: str,
        message_type: str = "message",
        reply_markup: dict | None = None,
    ):
        try:
            result = await self._send_telegram_http(chat_id, text, reply_markup=reply_markup)

            self.delivery_log.record(
                chat_id=chat_id,
                text=text,
                status="sent",
                message_type=message_type,
                attempts=1,
                reply_markup=reply_markup,
            )

            return result

        except Exception as e:
            error_text = f"{type(e).__name__}: {repr(e)}"
            sanitized_error = sanitize_telegram_error(error_text)
            retryable = is_retryable_telegram_error(e)
            log_event(
                logger,
                logging.WARNING,
                "telegram_send_error",
                chat_id=chat_id,
                message_type=message_type,
                retryable=retryable,
                error=sanitized_error,
            )

            self.delivery_log.record(
                chat_id=chat_id,
                text=text,
                status="failed_retryable" if retryable else "failed_final",
                message_type=message_type,
                error=sanitized_error,
                attempts=3,
                max_attempts=3,
                next_retry_at=(
                    datetime.now(timezone.utc) + timedelta(seconds=300)
                ) if retryable else None,
                reply_markup=reply_markup,
            )

            return {
                "ok": False,
                "chat_id": chat_id,
                "error": sanitized_error,
            }

    async def send_owner_alert(self, title: str, body: str):
        text = f"🧭 {title}\n\n{body}"
        await self.send_message(settings.TELEGRAM_OWNER_CHAT_ID, text, message_type="owner_alert")

    async def send_signal_to_clients(self, signal: dict, confidence: float, grade: str | None = None):
        """
        Legacy method — kept for compatibility.
        New logic goes through TelegramRouter.
        """
        side = signal["action"].upper()
        emoji = "🟢" if side == "LONG" else "🔴"
        grade_text = grade or "N/A"

        text = (
            f"{emoji} {signal['symbol']} {side}\n\n"
            f"🎯 Вход: {signal['entry_zone'][0]} - {signal['entry_zone'][1]}\n"
            f"🛑 Стоп: {signal['stop_price']}\n"
            f"✅ TP1: {signal['tp']['tp1']}\n"
            f"✅ TP2: {signal['tp']['tp2']}\n\n"
            f"🏆 Класс сигнала: {grade_text}\n"
            f"📊 Уверенность: {round(confidence, 1)}%\n"
            f"🧠 Логика: {signal['reason']}\n\n"
            f"⚠️ Не финансовая рекомендация. Соблюдайте риск-менеджмент."
        )

        await self.send_message(
            settings.TELEGRAM_VIP_SIGNALS_CHAT_ID, text, message_type="legacy_vip_signal"
        )

    async def send_signal_update(self, symbol: str, text_status: str, extra: str = ""):
        """
        Legacy method — kept for compatibility.
        """
        text = f"📌 {symbol}\n{text_status}"
        if extra:
            text += f"\n{extra}"

        await self.send_message(
            settings.TELEGRAM_VIP_SIGNALS_CHAT_ID, text, message_type="legacy_vip_update"
        )
