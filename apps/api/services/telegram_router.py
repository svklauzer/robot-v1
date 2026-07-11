from core.config import settings
from services.signal_broadcaster import SignalBroadcaster


class TelegramRouter:
    def __init__(self):
        self.sender = SignalBroadcaster()

    async def _send_required(self, chat_id, text: str, label: str):
        result = await self.sender.send_message(chat_id, text, message_type=label)

        if not result or not result.get("ok"):
            raise RuntimeError(
                f"telegram_required_send_failed:{label}:"
                f"chat_id={chat_id}:error={result.get('error') if result else 'no_result'}"
            )

        return result

    async def _send_optional(self, chat_id, text: str, label: str):
        result = await self.sender.send_message(chat_id, text, message_type=label)

        if not result or not result.get("ok"):
            await self.sender.send_owner_alert(
                "TELEGRAM OPTIONAL SEND FAILED",
                (
                    f"Label: {label}\n"
                    f"Chat ID: {chat_id}\n"
                    f"Error: {result.get('error') if result else 'no_result'}"
                ),
            )

        return result

    async def publish_new_signal(
        self,
        signal: dict,
        confidence: float,
        grade: str,
        signal_id: int | None = None,
        is_public: bool = True,
    ):
        """
        Маршрутизация нового сигнала.

        ВАЖНО:
        Если сигнал создаётся как published и потом будет сопровождаться в VIP,
        то полный VIP SIGNAL должен уйти обязательно.
        Иначе main.py должен пометить сигнал telegram_failed.
        """

        if not is_public:
            await self.sender.send_owner_alert(
                "SIGNAL NOT PUBLIC",
                (
                    f"{signal.get('symbol')} {signal.get('action')} "
                    f"grade={grade}, confidence={confidence}"
                ),
            )
            return {
                "ok": True,
                "route": "owner_only_not_public",
                "vip_sent": False,
                "free_sent": False,
            }

        # C-сигналы не должны попадать клиентам.
        if grade == "C":
            await self.sender.send_owner_alert(
                "GRADE C SIGNAL BLOCKED FROM CLIENTS",
                (
                    f"{signal.get('symbol')} {signal.get('action')} "
                    f"grade={grade}, confidence={confidence}"
                ),
            )
            return {
                "ok": True,
                "route": "owner_only_grade_c",
                "vip_sent": False,
                "free_sent": False,
            }

        vip_text = self._format_vip_full_signal(signal, confidence, grade, signal_id)

        # VIP full — обязательный.
        await self._send_required(
            settings.TELEGRAM_VIP_SIGNALS_CHAT_ID,
            vip_text,
            "vip_full_signal",
        )

        free_sent = False

        # FREE teaser отправляем для любого публичного сигнала.
        # Это синхронизирует FREE и VIP по факту появления нового сигнала
        # (даже если полный сетап остаётся только в VIP).
        free_text = self._format_free_teaser(signal, confidence, grade, signal_id)

        await self._send_optional(
            settings.TELEGRAM_FREE_SIGNALS_CHAT_ID,
            free_text,
            "free_teaser",
        )

        free_sent = True

        return {
            "ok": True,
            "route": "client_signal",
            "vip_sent": True,
            "free_sent": free_sent,
        }

    async def publish_signal_update(
        self,
        symbol: str,
        text_status: str,
        extra: str = "",
        grade: str | None = None,
    ):
        """
        Updates по активным сделкам.
        VIP получает полное сопровождение.
        FREE получает важные updates по A+/A.
        """

        vip_text = self._format_update(symbol, text_status, extra)

        vip_result = await self.sender.send_message(
            settings.TELEGRAM_VIP_SIGNALS_CHAT_ID,
            vip_text,
            message_type="vip_signal_update",
        )

        free_result = None
        if grade in ["A+", "A"] and (
            "закрыта" in text_status.lower()
            or "tp1" in text_status.lower()
        ):
            free_text = self._format_free_update(symbol, text_status)

            free_result = await self._send_optional(
                settings.TELEGRAM_FREE_SIGNALS_CHAT_ID,
                free_text,
                "free_update",
            )

        return {
            "ok": bool(vip_result and vip_result.get("ok")),
            "route": "signal_update",
            "vip_sent": bool(vip_result and vip_result.get("ok")),
            "vip_error": vip_result.get("error") if vip_result else "no_result",
            "free_sent": bool(free_result and free_result.get("ok")) if free_result is not None else False,
            "free_error": free_result.get("error") if free_result and not free_result.get("ok") else None,
        }

    async def owner_alert(self, title: str, body: str):
        await self.sender.send_owner_alert(title, body)

    def _format_vip_full_signal(
        self,
        signal: dict,
        confidence: float,
        grade: str,
        signal_id: int | None,
    ) -> str:
        side = signal["action"].upper()
        emoji = "🟢" if side == "LONG" else "🔴"
        signal_ref = f"#{signal_id}" if signal_id else ""

        return (
            f"{emoji} VIP SIGNAL {signal_ref}\n"
            f"{signal['symbol']} {side}\n\n"
            f"🎯 Вход: {signal['entry_zone'][0]} - {signal['entry_zone'][1]}\n"
            f"🛑 Стоп: {signal['stop_price']}\n"
            f"✅ TP1: {signal['tp']['tp1']}\n"
            f"✅ TP2: {signal['tp']['tp2']}\n\n"
            f"🏆 Класс сигнала: {grade}\n"
            f"📊 Уверенность: {round(confidence, 1)}%\n"
            f"🧠 Логика: {signal['reason']}\n\n"
            f"⚠️ Не финансовая рекомендация. Соблюдайте риск-менеджмент."
        )

    def _vip_cta(self, prefix: str) -> str:
        """(#free-cta-2026-07-11) Единый CTA для FREE-канала: deep-link в бота
        (start=vip), как в тизере. Раньше апдейты слали захардкоженный @finmt_vip
        (приватный канал — обращение по юзернейму не работает для не-участников,
        и это не воронка бота). Фолбэк без юзернейма — команда /plans."""
        bot_username = (settings.TELEGRAM_BOT_USERNAME or "").lstrip("@")
        if bot_username:
            return f"{prefix}: https://t.me/{bot_username}?start=vip"
        return f"{prefix} — напишите боту команду /plans"

    def _format_free_teaser(
        self,
        signal: dict,
        confidence: float,
        grade: str,
        signal_id: int | None,
    ) -> str:
        side = signal["action"].upper()
        emoji = "🟢" if side == "LONG" else "🔴"
        signal_ref = f"#{signal_id}" if signal_id else ""

        cta = self._vip_cta("👉 Полный сигнал и VIP-доступ")

        return (
            f"{emoji} FREE SIGNAL TEASER {signal_ref}\n"
            f"{signal['symbol']} {side}\n\n"
            f"🏆 Класс: {grade}\n"
            f"📊 Уверенность: {round(confidence, 1)}%\n\n"
            f"Полные уровни входа, стопа и тейков доступны в VIP.\n"
            f"{cta}"
        )

    async def _send_vip_full_signal(
        self,
        signal: dict,
        confidence: float,
        grade: str,
        signal_id: int | None,
    ):
        text = self._format_vip_full_signal(signal, confidence, grade, signal_id)
        return await self._send_required(
            settings.TELEGRAM_VIP_SIGNALS_CHAT_ID,
            text,
            "vip_full_signal",
        )

    async def _send_free_teaser(
        self,
        signal: dict,
        confidence: float,
        grade: str,
        signal_id: int | None,
    ):
        text = self._format_free_teaser(signal, confidence, grade, signal_id)
        return await self._send_optional(
            settings.TELEGRAM_FREE_SIGNALS_CHAT_ID,
            text,
            "free_teaser",
        )

    def _format_update(self, symbol: str, text_status: str, extra: str = "") -> str:
        text = (
            f"📌 VIP UPDATE\n"
            f"{symbol}\n"
            f"{text_status}\n"
        )

        if extra:
            text += f"\n{extra}"

        return text

    def _format_free_update(self, symbol: str, text_status: str) -> str:
        return (
            f"📌 FREE UPDATE\n"
            f"{symbol}\n"
            f"{text_status}\n\n"
            f"{self._vip_cta('👉 Полное сопровождение и VIP-доступ')}"
        )