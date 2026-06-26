"""LiquidityGuard — централизованная защита от расширения спреда (тонкая ликвидность).

Единый источник правды для ВСЕХ движков (trend / grid / funding / ML):
  • вход:  не открываемся, когда спред аномально широк (>порога / >mult×базы);
  • выход: не даём спайку bid/ask выбить СОФТ-стопы (свип спредом) — на спайке
           софт-выходы откладываются, реальный хард-стоп остаётся бэкстопом.

Адаптивность: текущий спред (bps) сравнивается со СКОЛЬЗЯЩЕЙ базой (EWMA) ПО
символу — нормально для BTC ~1bp, для алта ~5-10bp, и «широко» считается
относительно нормы самого символа, а не по жёсткому проценту. Дополнительно —
абсолютный потолок (никогда не торгуем сквозь него).

Fail-open: нет данных / мало семплов / устаревший кэш → НЕ блокируем и НЕ
подавляем (бот не замирает; риск-контроль не отключается).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from core.config import settings

_LOCK = threading.RLock()


def _bps(bid: float | None, ask: float | None) -> float | None:
    """Спред в базисных пунктах (1bp=0.01%). None при невалидных котировках."""
    if not bid or not ask or bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    return (ask - bid) / mid * 10000.0


@dataclass
class SpreadVerdict:
    spread_bps: float | None
    baseline_bps: float | None
    wide: bool
    reason: str


class LiquidityGuard:
    """Синглтон. Кэширует по символу последний спред и EWMA-базу; источник для
    входных гейтов и подавления софт-выходов во всех движках."""

    _instance: "LiquidityGuard | None" = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self):
        self._cache: dict[str, dict] = {}  # symbol -> {spread, base, ts}

    # ── обновление базы ───────────────────────────────────────────────────────
    def observe_bps(self, symbol: str, sp_bps: float | None) -> float | None:
        if sp_bps is None or sp_bps < 0:
            return None
        sym = symbol.upper()
        alpha = float(getattr(settings, "LIQ_SPREAD_BASELINE_ALPHA", 0.05))
        with _LOCK:
            rec = self._cache.get(sym)
            base = sp_bps if rec is None else (1 - alpha) * rec["base"] + alpha * sp_bps
            self._cache[sym] = {"spread": sp_bps, "base": base, "ts": time.time()}
        return sp_bps

    def observe(self, symbol: str, bid: float | None, ask: float | None) -> float | None:
        return self.observe_bps(symbol, _bps(bid, ask))

    def _threshold(self, base: float, mult: float) -> float:
        min_base = float(getattr(settings, "LIQ_SPREAD_MIN_BASELINE_BPS", 1.0))
        return mult * max(base, min_base)

    # ── ядро оценки ───────────────────────────────────────────────────────────
    def assess_bps(self, symbol: str, sp_bps: float | None = None, *,
                   mult: float | None = None, observe: bool = True) -> SpreadVerdict:
        if not bool(getattr(settings, "LIQUIDITY_GUARD_ENABLED", True)):
            return SpreadVerdict(None, None, False, "guard_disabled")
        sym = symbol.upper()
        with _LOCK:
            rec = self._cache.get(sym)
            if sp_bps is not None and sp_bps >= 0:
                if observe:
                    alpha = float(getattr(settings, "LIQ_SPREAD_BASELINE_ALPHA", 0.05))
                    base = sp_bps if rec is None else (1 - alpha) * rec["base"] + alpha * sp_bps
                    self._cache[sym] = {"spread": sp_bps, "base": base, "ts": time.time()}
                else:
                    base = sp_bps if rec is None else rec["base"]
                sp = sp_bps
            elif rec is not None:
                sp, base = rec["spread"], rec["base"]
            else:
                return SpreadVerdict(None, None, False, "no_data")
        abs_max = float(getattr(settings, "LIQ_SPREAD_ABS_MAX_BPS", 25.0))
        m = float(mult if mult is not None else getattr(settings, "LIQ_SPREAD_BASELINE_MULT", 3.0))
        thr = self._threshold(base, m)
        if sp > abs_max:
            return SpreadVerdict(sp, base, True, f"spread_abs:{sp:.1f}bps>{abs_max:.0f}")
        if sp > thr:
            return SpreadVerdict(sp, base, True, f"spread_x:{sp:.1f}bps>{m:.1f}x_base{base:.1f}")
        return SpreadVerdict(sp, base, False, "ok")

    def assess(self, symbol: str, bid: float | None = None, ask: float | None = None,
               *, mult: float | None = None) -> SpreadVerdict:
        return self.assess_bps(symbol, _bps(bid, ask), mult=mult)

    # ── вход ──────────────────────────────────────────────────────────────────
    def entry_blocked(self, symbol: str, bid: float | None = None, ask: float | None = None,
                      *, sp_bps: float | None = None) -> tuple[bool, str, float | None]:
        if not bool(getattr(settings, "LIQ_BLOCK_ENTRY", True)):
            return False, "entry_guard_off", None
        v = self.assess_bps(symbol, sp_bps if sp_bps is not None else _bps(bid, ask))
        return v.wide, v.reason, v.spread_bps

    # ── выход ─────────────────────────────────────────────────────────────────
    def exit_suppressed(self, symbol: str, bid: float | None = None, ask: float | None = None,
                        *, max_age_sec: float | None = None) -> bool:
        """True → подавить СОФТ-выход: спред спайкнул (свип). Только по свежим
        данным; устаревший/пустой кэш → False (fail-open)."""
        if not bool(getattr(settings, "LIQ_PROTECT_EXIT", True)):
            return False
        max_age = float(max_age_sec if max_age_sec is not None
                        else getattr(settings, "LIQ_EXIT_MAX_AGE_SEC", 30.0))
        sp = _bps(bid, ask)
        if sp is None:  # котировок не дали → работаем по кэшу, но только свежему
            with _LOCK:
                rec = self._cache.get(symbol.upper())
            if rec is None or (time.time() - rec["ts"]) > max_age:
                return False
        mult = float(getattr(settings, "LIQ_EXIT_SPREAD_MULT", 4.0))
        # observe=False: спайк НЕ должен раздувать базу, против которой его судим
        # (базу греют входные/lifecycle-наблюдения на нормальном спреде).
        return self.assess_bps(symbol, sp, mult=mult, observe=False).wide

    # ── снимок для фронта/health ──────────────────────────────────────────────
    def snapshot(self) -> dict:
        now = time.time()
        with _LOCK:
            return {
                s: {"spread_bps": round(r["spread"], 2), "base_bps": round(r["base"], 2),
                    "ratio": round(r["spread"] / r["base"], 2) if r["base"] else None,
                    "age_sec": round(now - r["ts"], 1)}
                for s, r in self._cache.items()
            }


LIQUIDITY_GUARD = LiquidityGuard()
