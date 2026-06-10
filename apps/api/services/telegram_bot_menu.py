from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from models.payment import Payment
from models.subscriber import Subscriber
from models.telegram_profile import TelegramProfile
from services.billing_service import BillingService
from services.affiliate_trial import AffiliateTrialService
from core.config import settings


@dataclass
class TelegramBotResponse:
    chat_id: str | None
    telegram_user_id: str | None
    command: str
    text: str
    reply_markup: dict | None = None
    message_type: str = "bot_menu"
    # Когда задано — роутер вместо обычного sendMessage выставляет счёт Stars.
    # Формат: {chat_id, title, description, payload, stars_amount}
    invoice: dict | None = None
    # Когда True — роутер генерирует одноразовый VIP-invite и дописывает в текст.
    vip_invite_request: bool = False
    # Когда задано — роутер проверяет HTX UID через affiliate-API и при успехе
    # выдаёт триал. Формат: {uid, telegram_user_id, username, full_name, chat_id}
    htx_verify: dict | None = None


class TelegramBotMenuService:
    def __init__(self, billing: BillingService | None = None, affiliate: AffiliateTrialService | None = None):
        self.billing = billing or BillingService()
        self.affiliate = affiliate or AffiliateTrialService()

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
        prev_stage = profile.funnel_stage
        subscriber = self._find_subscriber(db, telegram_user_id)
        command, args = self._parse_command(raw_text)

        # Пользователь прислал HTX UID после запроса (HTX_AFFILIATE_VERIFY_ENABLED).
        if prev_stage == "awaiting_htx_uid" and raw_text.strip().isdigit():
            profile.funnel_stage = "htx_uid_submitted"
            resp = self._response(
                chat_id, telegram_user_id, "/htx-verify",
                "⏳ Проверяю вашу регистрацию в HTX по партнёрской ссылке...", None,
            )
            resp.htx_verify = {
                "uid": raw_text.strip(),
                "telegram_user_id": telegram_user_id,
                "username": user.get("username"),
                "full_name": self._full_name(user),
                "chat_id": chat_id,
            }
            return resp

        if command in ["/start", "/menu"]:
            profile.funnel_stage = "started"
            # Deep-link из FREE-тизера: /start vip → сразу показываем тарифы.
            if command == "/start" and args and args[0].lower() in ("vip", "plans", "buy"):
                profile.funnel_stage = "viewed_plans"
                return self._response(chat_id, telegram_user_id, "/plans", self._plans_text(db), self._plans_keyboard(db))
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
                    "💳 Выберите тариф — оплата звёздами Telegram.",
                    self._plans_keyboard(db),
                )

            if not telegram_user_id:
                return self._response(chat_id, telegram_user_id, command, "Не удалось определить Telegram ID. Нажмите /start.", self._main_keyboard())

            try:
                stars = settings.stars_price_for_plan(plan_code)
                payment = self.billing.create_checkout(
                    db=db,
                    telegram_user_id=telegram_user_id,
                    plan_code=plan_code,
                    username=user.get("username"),
                    full_name=self._full_name(user),
                    provider="telegram_stars" if stars > 0 else "manual",
                    notes="telegram_stars_checkout" if stars > 0 else "telegram_menu_checkout",
                )
                profile.funnel_stage = "checkout_pending"

                if stars > 0:
                    plan = self.billing.get_plan(db, plan_code)
                    resp = self._response(
                        chat_id, telegram_user_id, command,
                        f"💫 Счёт на {stars} ⭐ отправлен — оплатите прямо в Telegram.",
                        None,
                    )
                    resp.message_type = "stars_invoice"
                    resp.invoice = {
                        "chat_id": chat_id,
                        "title": (plan.title if plan else plan_code),
                        "description": f"VIP-доступ Finmt на {plan.duration_days if plan else ''} дней. Полные сигналы: входы, стопы, тейки.",
                        "payload": f"vip:{payment.id}",
                        "stars_amount": stars,
                    }
                    return resp

                # Stars-цена не задана для тарифа → fallback на ручной checkout
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

        if command == "/htx":
            profile.funnel_stage = "htx_affiliate_clicked"
            return self._response(chat_id, telegram_user_id, command, self._htx_affiliate_text(), self._htx_affiliate_keyboard())

        if command == "/affiliate-registered":
            # Если включена авто-верификация — просим HTX UID, активируем после проверки.
            if settings.HTX_AFFILIATE_VERIFY_ENABLED:
                profile.funnel_stage = "awaiting_htx_uid"
                return self._response(
                    chat_id, telegram_user_id, command,
                    "🔎 Для активации бесплатного VIP пришлите ваш HTX UID "
                    "(числовой ID из профиля HTX). Мы проверим, что регистрация "
                    "сделана по нашей партнёрской ссылке.",
                    None,
                )
            subscriber, activated, reason = self.affiliate.activate_htx_trial(
                db=db,
                telegram_user_id=telegram_user_id or "",
                username=user.get("username"),
                full_name=self._full_name(user),
            )
            profile.funnel_stage = "affiliate_trial_active" if activated else "affiliate_trial_blocked"
            resp = self._response(
                chat_id,
                telegram_user_id,
                command,
                self._affiliate_trial_text(subscriber, activated, reason),
                self._main_keyboard(),
            )
            resp.vip_invite_request = bool(activated)
            return resp

        if command in ["/status", "/subscription_status"]:
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
            "subscription_status": "/subscription_status",
            "help": "/help",
            "support": "/support",
            "pay": "/pay",
            "buy_vip_30d": "/pay vip_30",
            "buy_vip_90d": "/pay vip_90",
            "renew": "/pay vip_30",
            "faq_risks": "/help",
            "contact_support": "/support",
            "htx_affiliate": "/htx",
            "affiliate_registered": "/affiliate-registered",
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
            "Выберите действие ниже или используйте команды /plans /pay /status /subscription_status /htx /help /support."
            f"{status_line}"
        )

    def _plan_price_label(self, plan) -> str:
        stars = settings.stars_price_for_plan(plan.code)
        if stars > 0:
            return f"⭐ {stars:,}".replace(",", " ")
        return f"{plan.amount_usdt:g} {plan.currency}"

    def _plans_text(self, db: Session) -> str:
        plans = self.billing.list_plans(db)
        rows = ["💎 VIP планы\n"]
        for plan in plans:
            rows.append(f"• {plan.title}: {self._plan_price_label(plan)} · {plan.duration_days} дней")
        rows.append("\nНажмите кнопку тарифа, чтобы оплатить звёздами Telegram. Или получите бесплатный VIP через HTX партнёрскую регистрацию: /htx.")
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
        expires_at = self._as_aware_datetime(subscriber.expires_at)
        active = subscriber.status == "active" and expires_at and expires_at > now
        return (
            "📌 Статус подписки\n\n"
            f"Plan: {subscriber.plan}\n"
            f"Status: {subscriber.status}\n"
            f"Active now: {'yes' if active else 'no'}\n"
            f"Expires: {expires_at}"
        )

    def _as_aware_datetime(self, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _htx_affiliate_text(self) -> str:
        days = max(int(settings.AFFILIATE_FREE_VIP_DAYS or 30), 1)
        link = settings.HTX_AFFILIATE_LINK or "HTX_AFFILIATE_LINK не настроен"
        return (
            "🎁 Бесплатный VIP через HTX\n\n"
            f"1) Зарегистрируйтесь в HTX по партнёрской ссылке:\n{link}\n\n"
            f"2) После регистрации нажмите «Я зарегистрировался» — бот активирует VIP на {days} дней.\n\n"
            "Важно: доступ выдаётся как trial, без гарантии прибыли и с обязательным риск-менеджментом."
        )

    def _affiliate_trial_text(self, subscriber: Subscriber | None, activated: bool, reason: str) -> str:
        invite = settings.VIP_INVITE_LINK or "VIP invite будет выдан owner/admin."
        if activated and subscriber:
            # Сама invite-ссылка дописывается роутером (одноразовая, см. vip_invite_request).
            return (
                "✅ HTX affiliate VIP активирован\n\n"
                f"Период: {settings.AFFILIATE_FREE_VIP_DAYS} дней\n"
                f"Доступ до: {subscriber.expires_at}\n\n"
                "Проверить статус можно через /status."
            )
        if reason == "paid_subscription_already_active":
            return "✅ У вас уже активна платная VIP подписка. Проверить статус: /status."
        if reason == "affiliate_trial_already_claimed":
            return "ℹ️ HTX affiliate trial уже был активирован ранее. Проверить статус: /status."
        return f"Не удалось активировать HTX affiliate VIP: {reason}. Напишите /support."

    def _help_text(self) -> str:
        return (
            "ℹ️ FAQ и риски\n\n"
            "Сигналы не являются финансовой рекомендацией. Нет гарантированной прибыли. "
            "Используйте риск-менеджмент, ограничивайте плечо и не торгуйте средствами, которые не готовы потерять. "
            "Перед live-режимом система проходит paper/live-shadow gates. "
            "Маркетинговый бонус: /htx выдаёт пробный VIP после регистрации по партнёрской ссылке HTX."
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
                [{"text": "🎁 HTX 30d VIP", "callback_data": "htx_affiliate"}, {"text": "📌 Статус", "callback_data": "status"}],
                [{"text": "ℹ️ Риски", "callback_data": "faq_risks"}],
                [{"text": "🛟 Поддержка", "callback_data": "contact_support"}],
            ]
        }

    def _htx_affiliate_keyboard(self) -> dict:
        rows = []
        if settings.HTX_AFFILIATE_LINK:
            rows.append([{"text": "🔗 Открыть HTX", "url": settings.HTX_AFFILIATE_LINK}])
        rows.append([{"text": "✅ Я зарегистрировался", "callback_data": "affiliate_registered"}])
        rows.append([{"text": "⬅️ Меню", "callback_data": "menu"}, {"text": "🛟 Поддержка", "callback_data": "support"}])
        return {"inline_keyboard": rows}

    def _plans_keyboard(self, db: Session) -> dict:
        plans = self.billing.list_plans(db)
        rows = [[{"text": f"{plan.title} · {self._plan_price_label(plan)}", "callback_data": f"pay:{plan.code}"}] for plan in plans]
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
