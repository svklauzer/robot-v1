import httpx
from core.config import settings


class SignalBroadcaster:
    async def send_message(self, chat_id: str, text: str):
        url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    url,
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "disable_web_page_preview": True,
                    }
                )
                response.raise_for_status()

            return {
                "ok": True,
                "chat_id": chat_id,
            }

        except Exception as e:
            print(
                f"[TELEGRAM SEND ERROR] chat_id={chat_id}: "
                f"{type(e).__name__}: {repr(e)}"
            )

            return {
                "ok": False,
                "chat_id": chat_id,
                "error": str(e),
            }

    async def send_owner_alert(self, title: str, body: str):
        text = f"🧭 {title}\n\n{body}"
        await self.send_message(settings.TELEGRAM_OWNER_CHAT_ID, text)

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

        await self.send_message(settings.TELEGRAM_VIP_SIGNALS_CHAT_ID, text)

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

        await self.send_message(settings.TELEGRAM_VIP_SIGNALS_CHAT_ID, text)