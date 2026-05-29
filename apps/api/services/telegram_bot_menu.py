from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from models.payment import Payment
from models.subscriber import Subscriber
from models.telegram_profile import TelegramProfile
from services.billing_service import BillingService


@dataclass
class TelegramBotResponse:
    chat_id: str | None
    telegram_user_id: str | None
    command: str
    text: str
    reply_markup: dict | None = None
    message_type: str = "bot_menu"


class TelegramBotMenuService:
    def __init__(self, billing: BillingService | None = None):
        self.billing = billing or BillingService()

    def handle(self, db: Session, message: dict | None, callback_query: dict | None) -> TelegramBotResponse:
        message = message or {}
        callback_query = callback_query or {}

        if callback_query:
            message = callback_query.get("message") or {}
            raw_text = str(callback_query.get("data") or "menu")
            user = callback_query.get("from") or {}
        else:
            raw_text = str(message.get("text") or "/menu")
            user = message.get("from") or {}

        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or user.get("id") or "") or None
        telegram_user_id = str(user.get("id") or chat_id or "") or None
        profile = self._upsert_profile(db, user=user, chat_id=chat_id, raw_text=raw_text)
        subscriber = self._find_subscriber(db, telegram_user_id)
        command, args = self._parse_command(raw_text)

        if command in ["/start", "/menu"]:
            profile.funnel_stage = "started"
            return self._response(chat_id, telegram_user_id, command, self._menu_text(subscriber), self._main_keyboard())

        if command == "/plans":
            profile.funnel_stage = "viewed_plans"
            return self._response(chat_id, telegram_user_id, command, self._plans_text(db), self._plans_keyboard(db))

        if command == "/pay":
            plan_code = args[0] if args else None
            if not plan_code:
                profile.funnel_stage = "pay_plan_select"
                return self._response(
                    chat_id,
                    telegram_user_id,
                    command,
                    "💳 Выберите тариф для создания pending checkout.",
                    self._plans_keyboard(db),
                )

            if not telegram_user_id:
                return self._response(chat_id, telegram_user_id, command, "Не удалось определить Telegram ID. Нажмите /start.", self._main_keyboard())

            try:
                payment = self.billing.create_checkout(
                    db=db,
                    telegram_user_id=telegram_user_id,
                    plan_code=plan_code,
                    username=user.get("username"),
                    full_name=self._full_name(user),
                    provider="manual",
                    notes="telegram_menu_checkout",
                )
                profile.funnel_stage = "checkout_pending"
                return self._response(chat_id, telegram_user_id, command, self._checkout_text(payment), self._after_checkout_keyboard())
            except Exception as exc:
                db.rollback()
                profile = self._upsert_profile(db, user=user, chat_id=chat_id, raw_text=raw_text)
                profile.funnel_stage = "checkout_error"
                return self._response(
                    chat_id,
                    telegram_user_id,
                    command,
                    f"Не удалось создать checkout: {type(exc).__name__}: {exc}",
                    self._plans_keyboard(db),
                )

        if command == "/status":
            profile.funnel_stage = "active" if subscriber and subscriber.status == "active" else "status_checked"
            return self._response(chat_id, telegram_user_id, command, self._status_text(subscriber), self._main_keyboard())

        if command == "/help":
            profile.funnel_stage = "help"
            return self._response(chat_id, telegram_user_id, command, self._help_text(), self._main_keyboard())

        if command == "/support":
            profile.funnel_stage = "support"
            return self._response(chat_id, telegram_user_id, command, self._support_text(telegram_user_id), self._main_keyboard())

        return self._response(chat_id, telegram_user_id, command, "Неизвестная команда. Нажмите /menu.", self._main_keyboard())

    def _upsert_profile(self, db: Session, user: dict, chat_id: str | None, raw_text: str) -> TelegramProfile:
        telegram_user_id = str(user.get("id") or chat_id or "")
        profile = db.query(TelegramProfile).filter(TelegramProfile.telegram_user_id == telegram_user_id).first()
        if not profile:
            profile = TelegramProfile(
                telegram_user_id=telegram_user_id,
                username=user.get("username"),
                full_name=self._full_name(user),
                chat_id=chat_id,
                funnel_stage="started",
                source="telegram_bot",
            )
            db.add(profile)
            db.flush()
        else:
            profile.username = user.get("username") or profile.username
            profile.full_name = self._full_name(user) or profile.full_name
            profile.chat_id = chat_id or profile.chat_id

        profile.last_command = raw_text[:100]
        return profile

    def _find_subscriber(self, db: Session, telegram_user_id: str | None) -> Subscriber | None:
        if not telegram_user_id:
            return None
        return db.query(Subscriber).filter(Subscriber.telegram_user_id == telegram_user_id).first()

    def _parse_command(self, text: str) -> tuple[str, list[str]]:
        text = text.strip()
        callback_map = {
            "menu": "/menu",
            "plans": "/plans",
            "status": "/status",
            "help": "/help",
            "support": "/support",
            "pay": "/pay",
            "buy_vip_30d": "/pay vip_30",
            "buy_vip_90d": "/pay vip_90",
            "renew": "/pay vip_30",
            "faq_risks": "/help",
            "contact_support": "/support",
        }
        if text in callback_map:
            text = callback_map[text]
        elif text.startswith("pay:"):
            text = f"/pay {text.split(':', 1)[1]}"

        parts = text.split()
        command = parts[0].lower() if parts else "/menu"
        if not command.startswith("/"):
            command = f"/{command}"
        return command, parts[1:]

    def _response(self, chat_id: str | None, telegram_user_id: str | None, command: str, text: str, keyboard: dict | None) -> TelegramBotResponse:
        return TelegramBotResponse(
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            command=command,
            text=text,
            reply_markup=keyboard,
        )

    def _menu_text(self, subscriber: Subscriber | None) -> str:
        status_line = f"\n\nВаш статус: {subscriber.status} до {subscriber.expires_at}" if subscriber else "\n\nVIP пока не активирован. Можно посмотреть тарифы или создать checkout."
        return (
            "🤖 Finmt Robot\n\n"
            "Сигналы, уровни входа/стопа/TP, сопровождение и отчеты.\n"
            "Выберите действие ниже или используйте команды /plans /pay /status /help /support."
            f"{status_line}"
        )

    def _plans_text(self, db: Session) -> str:
        plans = self.billing.list_plans(db)
        rows = ["💎 VIP планы\n"]
        for plan in plans:
            rows.append(f"• {plan.title}: {plan.amount_usdt:g} {plan.currency}, {plan.duration_days} дней")
        rows.append("\nНажмите кнопку тарифа, чтобы создать pending checkout.")
        return "\n".join(rows)

    def _checkout_text(self, payment: Payment) -> str:
        return (
            "💳 Checkout создан\n\n"
            f"Payment ID: #{payment.id}\n"
            f"Plan: {payment.plan_code}\n"
            f"Amount: {payment.amount:g} {payment.currency}\n"
            f"Status: {payment.status}\n\n"
            "После оплаты owner подтвердит платеж, и VIP будет активирован автоматически. "
            "Проверить доступ можно через /status."
        )

    def _status_text(self, subscriber: Subscriber | None) -> str:
        if not subscriber:
            return "📌 Статус: подписка не найдена. Нажмите /plans или /pay."

        now = datetime.now(timezone.utc)
        active = subscriber.status == "active" and subscriber.expires_at and subscriber.expires_at > now
        return (
            "📌 Статус подписки\n\n"
            f"Plan: {subscriber.plan}\n"
            f"Status: {subscriber.status}\n"
            f"Active now: {'yes' if active else 'no'}\n"
            f"Expires: {subscriber.expires_at}"
        )

    def _help_text(self) -> str:
        return (
            "ℹ️ FAQ и риски\n\n"
            "Сигналы не являются финансовой рекомендацией. Нет гарантированной прибыли. "
            "Используйте риск-менеджмент, ограничивайте плечо и не торгуйте средствами, которые не готовы потерять. "
            "Перед live-режимом система проходит paper/live-shadow gates."
        )

    def _support_text(self, telegram_user_id: str | None) -> str:
        return (
            "🛟 Поддержка\n\n"
            "Напишите owner/admin канала и приложите ваш Telegram ID: "
            f"{telegram_user_id or 'не определен'}."
        )

    def _main_keyboard(self) -> dict:
        return {
            "inline_keyboard": [
                [{"text": "💎 Тарифы", "callback_data": "plans"}, {"text": "💳 Оплатить", "callback_data": "pay"}],
                [{"text": "📌 Статус", "callback_data": "status"}, {"text": "ℹ️ Риски", "callback_data": "faq_risks"}],
                [{"text": "🛟 Поддержка", "callback_data": "contact_support"}],
            ]
        }

    def _plans_keyboard(self, db: Session) -> dict:
        plans = self.billing.list_plans(db)
        rows = [[{"text": f"{plan.title} · {plan.amount_usdt:g} {plan.currency}", "callback_data": f"pay:{plan.code}"}] for plan in plans]
        rows.append([{"text": "⬅️ Меню", "callback_data": "menu"}, {"text": "🛟 Поддержка", "callback_data": "support"}])
        return {"inline_keyboard": rows}

    def _after_checkout_keyboard(self) -> dict:
        return {
            "inline_keyboard": [
                [{"text": "📌 Проверить статус", "callback_data": "status"}, {"text": "🔁 Продлить", "callback_data": "renew"}],
                [{"text": "🛟 Поддержка", "callback_data": "contact_support"}],
            ]
        }

    def _full_name(self, user: dict) -> str | None:
        return " ".join(part for part in [user.get("first_name"), user.get("last_name")] if part) or None
