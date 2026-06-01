from sqlalchemy import DateTime, Float, ForeignKey, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


class FundingArbOpportunity(Base):
    __tablename__ = "funding_arb_opportunities"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(50), index=True)
    spot_symbol: Mapped[str] = mapped_column(String(50))
    swap_symbol: Mapped[str] = mapped_column(String(80))
    funding_rate: Mapped[float] = mapped_column(Float)
    annualized_rate_pct: Mapped[float] = mapped_column(Float)
    spot_price: Mapped[float] = mapped_column(Float)
    swap_price: Mapped[float] = mapped_column(Float)
    basis_pct: Mapped[float] = mapped_column(Float, default=0.0)
    estimated_edge_pct: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(30), default="candidate", index=True)
    next_funding_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class FundingArbPosition(Base):
    __tablename__ = "funding_arb_positions"

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("funding_arb_opportunities.id"), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(50), index=True)
    spot_symbol: Mapped[str] = mapped_column(String(50))
    swap_symbol: Mapped[str] = mapped_column(String(80))
    mode: Mapped[str] = mapped_column(String(20), default="paper", index=True)
    status: Mapped[str] = mapped_column(String(30), default="open", index=True)
    hedge_side: Mapped[str] = mapped_column(String(40), default="spot_long_perp_short")
    notional_usdt: Mapped[float] = mapped_column(Float)
    spot_qty: Mapped[float] = mapped_column(Float)
    swap_qty: Mapped[float] = mapped_column(Float)
    spot_entry_price: Mapped[float] = mapped_column(Float)
    swap_entry_price: Mapped[float] = mapped_column(Float)
    spot_exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    swap_exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_funding_rate: Mapped[float] = mapped_column(Float, default=0.0)
    exit_funding_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    funding_periods: Mapped[int] = mapped_column(default=0)
    funding_collected: Mapped[float] = mapped_column(Float, default=0.0)
    fees_paid: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    opened_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    closed_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
