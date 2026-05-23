from sqlalchemy import String, DateTime, JSON, func, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from core.db import Base

class Bot(Base):
    __tablename__ = "bots"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30), default="stopped")
    mode: Mapped[str] = mapped_column(String(20), default="paper")
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())