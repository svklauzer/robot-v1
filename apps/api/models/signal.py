from sqlalchemy import String, DateTime, JSON, Float, ForeignKey, func, Boolean, Column, Integer
from sqlalchemy.orm import Mapped, mapped_column
from core.db import Base

class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"))
    symbol: Mapped[str] = mapped_column(String(50), index=True)
    side: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    entry_zone_json: Mapped[dict] = mapped_column(JSON)
    stop_price: Mapped[float] = mapped_column(Float)
    tp_json: Mapped[dict] = mapped_column(JSON)
    confidence: Mapped[float] = mapped_column(Float, default=0)
    rationale: Mapped[str] = mapped_column(String(1000), default="")
    result_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    opened_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())
    grade: Mapped[str | None] = mapped_column(String(10), nullable=True)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
    qty = Column(Float, nullable=True)
    required_margin = Column(Float, nullable=True)
    net_rr_tp1 = Column(Float, nullable=True)
    net_rr_tp2 = Column(Float, nullable=True)
    net_pnl_tp1 = Column(Float, nullable=True)
    net_pnl_tp2 = Column(Float, nullable=True)
    net_pnl_stop = Column(Float, nullable=True)
    plan_json = Column(JSON, nullable=True)
    closed_exit_price = Column(Float, nullable=True)
    closed_net_pnl = Column(Float, nullable=True)
    closed_total_cost = Column(Float, nullable=True)
    closed_reason = Column(String(100), nullable=True)