import asyncio

from services.telegram_router import TelegramRouter


class FailingUpdateSender:
    async def send_message(self, chat_id, text, message_type="message", reply_markup=None):
        return {"ok": False, "chat_id": chat_id, "error": "ConnectTimeout: ConnectTimeout('')"}

    async def send_owner_alert(self, title, body):
        return {"ok": True}


def test_signal_update_telegram_timeout_does_not_raise_robot_loop_error():
    router = TelegramRouter()
    router.sender = FailingUpdateSender()

    result = asyncio.run(
        router.publish_signal_update(
            symbol="XRP/USDT",
            text_status="📥 Позиция активирована | Signal #42",
            extra="Цена входа: 1.0",
            grade="A+",
        )
    )

    assert result["ok"] is False
    assert result["vip_sent"] is False
    assert "ConnectTimeout" in result["vip_error"]
