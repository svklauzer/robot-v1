"""GridStore — состояние умной сетки. «Сетка знает свои ордера».

Хранит активные циклы (по символу), историю закрытых, рантайм-флаг вкл/выкл и
агрегаты. Полностью ИЗОЛИРОВАН от Position/Signal (тренд-движок не трогается).
Персист в Postgres (таблица grid_state, singleton-строка id=1) — переживает
redeploy/restart так же, как trade-сделки. Старый JSON-файл импортируется
одноразово при первом запуске и больше не используется.

Цикл сетки (dict, JSON-сериализуемый):
  symbol, regime(long/short/neutral), anchor, atr, timeframe, leverage,
  status(active/closed), created_at, closed_at, close_reason,
  levels:[{n, side, price, volume, filled, fill_price}],
  breakeven, tp_price, sl_price, realized_pnl
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.config import settings

_PATH = Path(str(getattr(settings, "GRID_STATE_PATH", "storage/grid/grid_state.json")))
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GridStore:
    """Синглтон-стор состояния сетки."""

    _instance: "GridStore | None" = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_state()
        return cls._instance

    def _init_state(self):
        self.enabled: bool = bool(getattr(settings, "GRID_ENABLED", False))
        self.cycles: dict[str, dict] = {}      # активные циклы по символу
        self.history: list[dict] = []          # закрытые циклы (хвост)
        self.realized_pnl: float = 0.0
        self.closed_count: int = 0
        self._load()

    # ── персист (Postgres — переживает redeploy, как trade-сделки) ───────────
    def _load(self):
        # Источник правды — БД. Эфемерный диск контейнера больше не используется;
        # состояние обнулится только при сбросе тома БД (docker compose down -v).
        try:
            from core.db import SessionLocal
            from models.grid_state import GridState
            db = SessionLocal()
            try:
                row = db.get(GridState, 1)
                if row is not None:
                    self.enabled = bool(row.enabled)
                    self.cycles = dict(row.cycles or {})
                    self.history = list(row.history or [])[-200:]
                    self.realized_pnl = float(row.realized_pnl or 0.0)
                    self.closed_count = int(row.closed_count or 0)
                    return
            finally:
                db.close()
        except Exception:
            pass  # БД ещё не готова → пробуем одноразовый импорт старого файла
        self._import_legacy_file()

    def _import_legacy_file(self):
        """Одноразовый перенос состояния со старого JSON-файла в БД (если был)."""
        try:
            if _PATH.exists():
                data = json.loads(_PATH.read_text(encoding="utf-8"))
                self.enabled = bool(data.get("enabled", self.enabled))
                self.cycles = data.get("cycles", {}) or {}
                self.history = (data.get("history", []) or [])[-200:]
                self.realized_pnl = float(data.get("realized_pnl", 0.0) or 0.0)
                self.closed_count = int(data.get("closed_count", 0) or 0)
                self._save()  # переносим в БД
        except Exception:
            pass

    def _save(self):
        try:
            from core.db import SessionLocal
            from models.grid_state import GridState
            db = SessionLocal()
            try:
                row = db.get(GridState, 1)
                if row is None:
                    row = GridState(id=1)
                    db.add(row)
                row.enabled = bool(self.enabled)
                row.cycles = dict(self.cycles)            # копия → SQLAlchemy видит изменение JSON
                row.history = list(self.history[-200:])
                row.realized_pnl = round(float(self.realized_pnl), 8)
                row.closed_count = int(self.closed_count)
                db.commit()
            finally:
                db.close()
        except Exception:
            pass  # сбой записи не должен ронять тик сетки

    # ── управление ───────────────────────────────────────────────────────────
    def is_enabled(self) -> bool:
        with _LOCK:
            return bool(self.enabled)

    def set_enabled(self, value: bool) -> dict:
        with _LOCK:
            self.enabled = bool(value)
            self._save()
            return {"enabled": self.enabled}

    def get_cycle(self, symbol: str) -> dict | None:
        with _LOCK:
            return self.cycles.get(symbol.upper())

    def put_cycle(self, symbol: str, cycle: dict):
        with _LOCK:
            self.cycles[symbol.upper()] = cycle
            self._save()

    def close_cycle(self, symbol: str, realized: float, reason: str, price: float):
        with _LOCK:
            sym = symbol.upper()
            cyc = self.cycles.pop(sym, None)
            if cyc is None:
                return
            cyc["status"] = "closed"
            cyc["closed_at"] = _now()
            cyc["close_reason"] = reason
            cyc["close_price"] = price
            cyc["realized_pnl"] = round(float(realized), 8)
            self.realized_pnl += float(realized)
            self.closed_count += 1
            self.history.append(cyc)
            self.history = self.history[-200:]
            self._save()

    # ── агрегаты для фронта ───────────────────────────────────────────────────
    def grid_used_margin(self) -> float:
        """Маржа, занятая ИСПОЛНЕННЫМИ уровнями активных циклов (свой карман)."""
        lev = max(float(getattr(settings, "GRID_LEVERAGE", 1.0)), 1e-9)
        used = 0.0
        with _LOCK:
            for cyc in self.cycles.values():
                for lv in cyc.get("levels", []):
                    if lv.get("filled"):
                        used += float(lv["volume"]) * float(lv.get("fill_price") or lv["price"]) / lev
        return round(used, 6)

    def summary(self) -> dict:
        with _LOCK:
            active = list(self.cycles.values())
            equity = float(getattr(settings, "RISK_EQUITY_USDT", 950.0))
            envelope = round(equity * float(getattr(settings, "GRID_MAX_USED_MARGIN_PCT", 20.0)) / 100.0, 2)
            used = self.grid_used_margin()
            return {
                "enabled": self.enabled,
                "active_cycles": len(active),
                "symbols": list(self.cycles.keys()),
                "margin_envelope_usdt": envelope,
                "grid_used_margin_usdt": used,
                "grid_free_margin_usdt": round(max(0.0, envelope - used), 6),
                "realized_pnl_usdt": round(self.realized_pnl, 6),
                "closed_cycles": self.closed_count,
            }
