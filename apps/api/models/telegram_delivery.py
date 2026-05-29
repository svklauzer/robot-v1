from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


class TelegramDelivery(Base):
    __tablename__ = "telegram_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(100), index=True)
    message_type: Mapped[str] = mapped_column(String(80), default="message", index=True)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_preview: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reply_markup_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_retry_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_attempt_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[str] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
