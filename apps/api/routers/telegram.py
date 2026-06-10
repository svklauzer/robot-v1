from fastapi import APIRouter, Depends
from pydantic import BaseModel
from core.config import settings
from core.db import SessionLocal
from core.security import require_owner_action
from services.signal_broadcaster import SignalBroadcaster
from services.telegram_bot_menu import TelegramBotMenuService
from services.telegram_delivery_log import TelegramDeliveryLog
from services.telegram_router import TelegramRouter

router = APIRouter(prefix="/telegram", tags=["telegram"])


class TelegramWebhookRequest(BaseModel):
    update_id: int | None = None
    message: dict | None = None
    callback_query: dict | None = None


@router.post("/webhook")
async def telegram_webhook(payload: TelegramWebhookRequest):
    db = SessionLocal()
    try:
        response = TelegramBotMenuService().handle(
            db=db,
            message=payload.message,
            callback_query=payload.callback_query,
        )
        db.commit()
        if response.chat_id:
            await SignalBroadcaster().send_message(
                chat_id=response.chat_id,
                text=response.text,
                message_type=response.message_type,
                reply_markup=response.reply_markup,
            )
        return {
            "status": "ok",
            "command": response.command,
            "telegram_user_id": response.telegram_user_id,
            "message_type": response.message_type,
        }
    except Exception as e:
        db.rollback()
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    finally:
        db.close()


@router.get("/deliveries/summary", dependencies=[Depends(require_owner_action)])
def telegram_deliveries_summary(hours: int = 24):
    db = SessionLocal()
    try:
        return TelegramDeliveryLog().summary(db, hours=min(max(hours, 1), 720))
    finally:
        db.close()


@router.post("/test-owner", dependencies=[Depends(require_owner_action)])
async def test_telegram_owner():
    await TelegramRouter().owner_alert("SYSTEM HEALTH TEST", "Owner Telegram alerts работают.")
    return {"status": "sent"}


@router.post("/test-free", dependencies=[Depends(require_owner_action)])
async def test_telegram_free():
    await SignalBroadcaster().send_message(
        settings.TELEGRAM_FREE_SIGNALS_CHAT_ID,
        "🧪 FREE channel test: система Finmt работает.",
    )
    return {"status": "sent"}


@router.post("/test-vip", dependencies=[Depends(require_owner_action)])
async def test_telegram_vip():
    await SignalBroadcaster().send_message(
        settings.TELEGRAM_VIP_SIGNALS_CHAT_ID,
        "🧪 VIP channel test: система Finmt работает.",
    )
    return {"status": "sent"}
