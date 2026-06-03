from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


class TelegramProfile(Base):
    __tablename__ = "telegram_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_user_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    chat_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    funnel_stage: Mapped[str] = mapped_column(String(50), default="started", index=True)
    last_command: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[str] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
