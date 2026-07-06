import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from core.config import settings
from core.db import SessionLocal
from core.logging import get_logger, log_event
from core.security import require_owner_action
from services.affiliate_trial import AffiliateTrialService
from services.billing_service import BillingService
from services.customer_notifications import CustomerNotificationService
from services.htx_affiliate import HTXAffiliateService
from services.signal_broadcaster import SignalBroadcaster
from services.telegram_bot_menu import TelegramBotMenuService
from services.telegram_delivery_log import TelegramDeliveryLog
from services.telegram_payments import TelegramPaymentsService
from services.telegram_router import TelegramRouter

logger = get_logger(__name__)

router = APIRouter(prefix="/telegram", tags=["telegram"])


class TelegramWebhookRequest(BaseModel):
    update_id: int | None = None
    message: dict | None = None
    callback_query: dict | None = None
    pre_checkout_query: dict | None = None


def _payment_id_from_payload(invoice_payload: str) -> int | None:
    # Формат payload: "vip:<payment_id>"
    try:
        return int(str(invoice_payload).split(":", 1)[1])
    except (IndexError, ValueError):
        return None


@router.post("/webhook")
async def telegram_webhook(payload: TelegramWebhookRequest):
    # 1) pre_checkout_query — обязателен ответ в течение 10 секунд.
    if payload.pre_checkout_query:
        pcq = payload.pre_checkout_query
        ok, err = _validate_pre_checkout(pcq)
        try:
            await TelegramPaymentsService().answer_pre_checkout_query(
                pre_checkout_query_id=str(pcq.get("id")), ok=ok, error_message=err,
            )
        except Exception as e:  # noqa: BLE001
            log_event(logger, logging.WARNING, "pre_checkout_answer_failed", error=str(e))
        return {"status": "ok", "stage": "pre_checkout", "approved": ok}

    # 2) successful_payment — деньги получены, выдаём VIP.
    if payload.message and payload.message.get("successful_payment"):
        return await _handle_successful_payment(payload.message)

    # 3) Обычное меню / команды.
    db = SessionLocal()
    try:
        response = TelegramBotMenuService().handle(
            db=db,
            message=payload.message,
            callback_query=payload.callback_query,
        )
        db.commit()

        if response.htx_verify:
            # Сначала «проверяю...», затем результат верификации.
            if response.chat_id and response.text:
                await SignalBroadcaster().send_message(
                    response.chat_id, response.text, message_type="htx_verify_pending")
            await _handle_htx_verify(response.htx_verify)
        elif response.invoice:
            await TelegramPaymentsService().send_invoice(**response.invoice)
        elif response.chat_id:
            text = response.text
            # Affiliate-триал активирован → выдаём одноразовый VIP-invite.
            if response.vip_invite_request:
                invite = None
                try:
                    invite = await TelegramPaymentsService().create_single_use_invite(
                        chat_id=str(settings.TELEGRAM_VIP_SIGNALS_CHAT_ID),
                        name=f"trial-{response.telegram_user_id}",
                    )
                except Exception as e:  # noqa: BLE001
                    log_event(logger, logging.WARNING, "trial_invite_create_failed", error=str(e))
                invite = invite or settings.VIP_INVITE_LINK
                if invite:
                    text = f"{text}\n\n🔑 Ваша персональная ссылка в VIP:\n{invite}"
            await SignalBroadcaster().send_message(
                chat_id=response.chat_id,
                text=text,
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


def _validate_pre_checkout(pcq: dict) -> tuple[bool, str | None]:
    payment_id = _payment_id_from_payload(pcq.get("invoice_payload", ""))
    if payment_id is None:
        return False, "Некорректный платёж. Начните заново через /plans."
    db = SessionLocal()
    try:
        from models.payment import Payment
        payment = db.query(Payment).filter(Payment.id == payment_id).first()
        if not payment:
            return False, "Платёж не найден. Начните заново через /plans."
        if payment.status == "paid":
            return False, "Этот счёт уже оплачен."
        return True, None
    finally:
        db.close()


async def _handle_successful_payment(message: dict) -> dict:
    sp = message.get("successful_payment") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    payment_id = _payment_id_from_payload(sp.get("invoice_payload", ""))
    charge_id = sp.get("telegram_payment_charge_id")

    if payment_id is None:
        log_event(logger, logging.WARNING, "successful_payment_bad_payload", payload=sp.get("invoice_payload"))
        return {"status": "error", "error": "bad_invoice_payload"}

    db = SessionLocal()
    try:
        payment, subscriber, activated = BillingService().confirm_payment(
            db=db, payment_id=payment_id,
            provider_event_id=charge_id, raw_payload=str(sp)[:1000],
        )
        db.commit()

        # Одноразовая ссылка в приватный VIP-канал.
        invite_link = None
        try:
            invite_link = await TelegramPaymentsService().create_single_use_invite(
                chat_id=str(settings.TELEGRAM_VIP_SIGNALS_CHAT_ID),
                name=f"vip-{subscriber.telegram_user_id}",
            )
        except Exception as e:  # noqa: BLE001
            log_event(logger, logging.WARNING, "vip_invite_create_failed", error=str(e))

        text = CustomerNotificationService().payment_success_text(
            payment, subscriber, activated=activated, invite_link=invite_link,
        )
        if chat_id:
            await SignalBroadcaster().send_message(chat_id, text, message_type="payment_success")

        log_event(logger, logging.INFO, "stars_payment_confirmed",
                  payment_id=payment_id, activated=activated, has_invite=bool(invite_link))
        return {"status": "ok", "stage": "successful_payment", "payment_id": payment_id, "activated": activated}
    except Exception as e:  # noqa: BLE001
        db.rollback()
        log_event(logger, logging.ERROR, "successful_payment_failed", payment_id=payment_id, error=str(e))
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    finally:
        db.close()


def _htx_reject_text(reason: str) -> str:
    if reason == "invalid_uid":
        return "❌ HTX UID должен быть числом. Пришлите числовой UID из профиля HTX."
    if reason == "uid_not_in_referrals":
        return (
            "❌ Не нашли этот UID среди регистраций по нашей ссылке.\n\n"
            "Убедитесь, что зарегистрировались по партнёрской ссылке (/htx), "
            "и пришлите корректный HTX UID."
        )
    if reason == "verification_unconfigured":
        return "⚙️ Авто-проверка временно недоступна. Напишите /support."
    return "⚠️ Не удалось связаться с HTX. Попробуйте позже или напишите /support."


async def _handle_htx_verify(data: dict) -> None:
    uid = data.get("uid")
    chat_id = str(data.get("chat_id") or "")
    tid = str(data.get("telegram_user_id") or "")

    ok, reason = await HTXAffiliateService().verify_referral(uid)
    if not ok:
        log_event(logger, logging.INFO, "htx_verify_rejected", uid=uid, reason=reason)
        if chat_id:
            await SignalBroadcaster().send_message(
                chat_id, _htx_reject_text(reason), message_type="htx_verify_failed",
                reply_markup={"inline_keyboard": [[{"text": "🔁 Попробовать снова", "callback_data": "htx_affiliate"}]]},
            )
        return

    # UID подтверждён → активируем бесплатный триал.
    db = SessionLocal()
    try:
        subscriber, activated, areason = AffiliateTrialService().activate_htx_trial(
            db=db, telegram_user_id=tid,
            username=data.get("username"), full_name=data.get("full_name"),
        )
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        log_event(logger, logging.ERROR, "htx_trial_activate_failed", uid=uid, error=str(e))
        if chat_id:
            await SignalBroadcaster().send_message(chat_id, "⚠️ Ошибка активации. Напишите /support.", message_type="htx_verify_error")
        return
    finally:
        db.close()

    if not activated:
        note = {
            "paid_subscription_already_active": "✅ У вас уже активна платная VIP-подписка. /status",
            "affiliate_trial_already_claimed": "ℹ️ Бесплатный триал уже был активирован ранее. /status",
            "affiliate_link_not_configured": "⚠️ Партнёрская программа временно недоступна. Напишите /support.",
        }.get(areason, f"ℹ️ Триал не выдан: {areason}. /support")
        if chat_id:
            await SignalBroadcaster().send_message(chat_id, note, message_type="htx_trial_not_granted")
        return

    invite = None
    try:
        invite = await TelegramPaymentsService().create_single_use_invite(
            chat_id=str(settings.TELEGRAM_VIP_SIGNALS_CHAT_ID), name=f"trial-{tid}")
    except Exception as e:  # noqa: BLE001
        log_event(logger, logging.WARNING, "trial_invite_create_failed", error=str(e))
    invite = invite or settings.VIP_INVITE_LINK

    text = (
        "✅ Регистрация по нашей HTX-ссылке подтверждена!\n\n"
        f"🎁 VIP активирован на {settings.AFFILIATE_FREE_VIP_DAYS} дней.\n"
        f"Доступ до: {subscriber.expires_at}\n"
    )
    if invite:
        text += f"\n🔑 Ваша персональная ссылка в VIP:\n{invite}\n"
    text += "\nКогда период закончится — продлите доступ командой /plans."
    if chat_id:
        await SignalBroadcaster().send_message(chat_id, text, message_type="htx_trial_activated")
    log_event(logger, logging.INFO, "htx_trial_activated", uid=uid, telegram_user_id=tid)


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
