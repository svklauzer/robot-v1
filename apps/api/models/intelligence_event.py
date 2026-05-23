from sqlalchemy import String, Float, DateTime, JSON, func
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


class IntelligenceEvent(Base):
    __tablename__ = "intelligence_events"

    id: Mapped[int] = mapped_column(primary_key=True)

    symbol: Mapped[str] = mapped_column(String(50), index=True)

    status: Mapped[str] = mapped_column(String(50), index=True)
    # hold / watch / wait / candidate / rejected / blocked / error

    decision: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # watch_long / watch_short / ready_to_publish / quality_grade_too_low / etc

    action: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # hold / long / short

    regime: Mapped[str | None] = mapped_column(String(100), nullable=True)
    radar_state: Mapped[str | None] = mapped_column(String(100), nullable=True)

    confidence_hint: Mapped[float | None] = mapped_column(Float, nullable=True)
    setup_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())