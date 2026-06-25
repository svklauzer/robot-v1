"""GridStore — состояние умной сетки. «Сетка знает свои ордера».

Хранит активные циклы (по символу), историю закрытых, рантайм-флаг вкл/выкл и
агрегаты. Полностью ИЗОЛИРОВАН от Position/Signal (тренд-движок не трогается).
Персист в storage/grid/grid_state.json (best-effort, fail-open) — переживает рестарт.

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

_PATH = Path("storage/grid/grid_state.json")
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

    # ── персист ────────────────────────────────────────────────────────────
    def _load(self):
        try:
            if _PATH.exists():
                data = json.loads(_PATH.read_text(encoding="utf-8"))
                self.enabled = bool(data.get("enabled", self.enabled))
                self.cycles = data.get("cycles", {}) or {}
                self.history = (data.get("history", []) or [])[-200:]
                self.realized_pnl = float(data.get("realized_pnl", 0.0) or 0.0)
                self.closed_count = int(data.get("closed_count", 0) or 0)
        except Exception:
            pass  # битый файл → начинаем с пустого, не валимся

    def _save(self):
        try:
            _PATH.parent.mkdir(parents=True, exist_ok=True)
            _PATH.write_text(json.dumps({
                "enabled": self.enabled,
                "cycles": self.cycles,
                "history": self.history[-200:],
                "realized_pnl": round(self.realized_pnl, 8),
                "closed_count": self.closed_count,
                "saved_at": _now(),
            }, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

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
