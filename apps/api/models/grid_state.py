"""GridState — состояние умной сетки в Postgres (singleton-строка id=1).

Переживает redeploy/restart так же, как trade-сделки (signals/positions): данные
живут в БД, а не на эфемерном диске контейнера. Обнуляется только при сбросе тома
БД (docker compose down -v). Цикл/история хранятся JSON-блобами.
"""
from sqlalchemy import Boolean, DateTime, Float, Integer, JSON, func
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


class GridState(Base):
    __tablename__ = "grid_state"

    id: Mapped[int] = mapped_column(primary_key=True)  # singleton: всегда 1
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    closed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cycles: Mapped[dict | None] = mapped_column(JSON, nullable=True)    # {symbol: cycle}
    history: Mapped[list | None] = mapped_column(JSON, nullable=True)   # [closed cycle, ...]
    updated_at: Mapped[str] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=True
    )
