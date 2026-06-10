"""Telegram payments (Stars / XTR) + VIP channel invite generation.

Использует тот же httpx-клиент, что и SignalBroadcaster (IPv4-forced,
поддержка прокси), но вызывает другие методы Bot API:
- sendInvoice            — выставить счёт в Telegram Stars
- answerPreCheckoutQuery — подтвердить pre-checkout (обязательно за 10 сек)
- createChatInviteLink   — одноразовая ссылка в приватный VIP-канал
"""
import logging
from datetime import datetime, timedelta, timezone

from core.config import settings
from core.logging import get_logger, log_event
from services.signal_broadcaster import _build_telegram_client

logger = get_logger(__name__)


def _api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/{method}"


class TelegramPaymentsService:
    async def _call(self, method: str, payload: dict) -> dict:
        url = _api_url(method)
        async with _build_telegram_client() as client:
            resp = await client.post(url, json=payload)
        data = resp.json() if resp.content else {}
        if resp.status_code >= 400 or not data.get("ok", False):
            body = str(data)[:300] or resp.text[:300]
            log_event(
                logger, logging.WARNING, "telegram_payments_api_error",
                method=method, status=resp.status_code, body=body,
            )
            raise RuntimeError(f"telegram_{method}_failed:{resp.status_code}:{body}")
        return data.get("result", {})

    async def send_invoice(
        self,
        chat_id: str,
        title: str,
        description: str,
        payload: str,
        stars_amount: int,
    ) -> dict:
        """Выставить счёт в Telegram Stars. provider_token пуст, currency=XTR."""
        if stars_amount <= 0:
            raise ValueError("stars_amount_must_be_positive")
        return await self._call("sendInvoice", {
            "chat_id": chat_id,
            "title": title[:32],
            "description": description[:255],
            "payload": payload,
            "provider_token": "",          # Stars: всегда пусто
            "currency": "XTR",
            "prices": [{"label": title[:32], "amount": int(stars_amount)}],
        })

    async def answer_pre_checkout_query(
        self,
        pre_checkout_query_id: str,
        ok: bool = True,
        error_message: str | None = None,
    ) -> dict:
        body: dict = {"pre_checkout_query_id": pre_checkout_query_id, "ok": ok}
        if not ok and error_message:
            body["error_message"] = error_message
        return await self._call("answerPreCheckoutQuery", body)

    async def create_single_use_invite(
        self,
        chat_id: str,
        name: str | None = None,
    ) -> str:
        """Одноразовая (member_limit=1) ссылка-приглашение с TTL. Бот должен
        быть админом канала с правом приглашать."""
        expire_at = int(
            (datetime.now(timezone.utc)
             + timedelta(hours=settings.VIP_INVITE_EXPIRE_HOURS)).timestamp()
        )
        body: dict = {
            "chat_id": chat_id,
            "member_limit": 1,
            "expire_date": expire_at,
        }
        if name:
            body["name"] = name[:32]
        result = await self._call("createChatInviteLink", body)
        return result.get("invite_link", "")
