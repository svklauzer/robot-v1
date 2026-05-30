import httpx
from datetime import datetime, timedelta, timezone

from core.config import settings
from services.telegram_delivery_log import TelegramDeliveryLog
from services.telegram_errors import is_retryable_telegram_error, sanitize_telegram_error


class SignalBroadcaster:
    def __init__(self):
        self.delivery_log = TelegramDeliveryLog()

    async def _send_telegram_http(self, chat_id: str, text: str, reply_markup: dict | None = None) -> dict:
        url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"

        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                url,
                json=payload
            )
            response.raise_for_status()

        return {"ok": True, "chat_id": chat_id}

    async def send_message(self, chat_id: str, text: str, message_type: str = "message", reply_markup: dict | None = None):
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
            print(
                f"[TELEGRAM SEND ERROR] chat_id={chat_id}: "
                f"{sanitized_error}"
            )

            self.delivery_log.record(
                chat_id=chat_id,
                text=text,
                status="failed_retryable" if retryable else "failed_final",
                message_type=message_type,
                error=sanitized_error,
                attempts=1,
                max_attempts=3,
                next_retry_at=(datetime.now(timezone.utc) + timedelta(seconds=60)) if retryable else None,
                reply_markup=reply_markup,
            )

            self.delivery_log.record(
                chat_id=chat_id,
                text=text,
                status="failed_retryable",
                message_type=message_type,
                error=f"{type(e).__name__}: {repr(e)}",
                attempts=1,
                max_attempts=3,
                next_retry_at=datetime.now(timezone.utc) + timedelta(seconds=60),
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
        Старый метод оставляем для совместимости.
        По умолчанию отправляет в VIP.
        Новая логика идёт через TelegramRouter.
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

        await self.send_message(settings.TELEGRAM_VIP_SIGNALS_CHAT_ID, text, message_type="legacy_vip_signal")

    async def send_signal_update(self, symbol: str, text_status: str, extra: str = ""):
        """
        Старый метод для совместимости.
        По умолчанию отправляет update в VIP.
        """

        text = (
            f"📌 {symbol}\n"
            f"{text_status}\n"
        )

        if extra:
            text += f"\n{extra}"

        await self.send_message(settings.TELEGRAM_VIP_SIGNALS_CHAT_ID, text, message_type="legacy_vip_update")