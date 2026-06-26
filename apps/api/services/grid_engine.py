"""GridEngine — движок умной сетки (paper). Считает регайм/уровни, симулирует
филлы лимитов по живой цене, ведёт безубыток/TP/SL по ВСЕЙ корзине.

Принципы:
  • Изоляция: работает на СВОЙ карман маржи (GRID_MAX_USED_MARGIN_PCT), Position/
    Signal тренда НЕ трогает. Открытые тренд-ордера в безопасности.
  • «Знает свои ордера»: всё состояние — в GridStore (циклы/уровни/филлы).
  • Округление tick/lot — через htx.price_to_precision/amount_to_precision (best-effort).
  • Закрытие — по агрегату корзины (а не по расчётному числу ордеров) → нет «пыли».
  • Цены — из существующего фида (WS-снэпшот рынка), ордера — только на отправку (REST).
  • fail-open: любая ошибка тика логируется, движок не падает.
"""
from __future__ import annotations

from datetime import datetime, timezone

from core.config import settings
from services.market_data import MarketDataService
from services.grid_store import GridStore
from services import grid_calculator as gc


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GridEngine:
    def __init__(self):
        self.market = MarketDataService()
        self.store = GridStore()
        try:
            from services.htx_client import HTXClient
            self.htx = HTXClient()
        except Exception:
            self.htx = None

    # ── вспомогательные ───────────────────────────────────────────────────────
    def _price(self, symbol: str) -> float:
        try:
            snap = self.market.snapshot(symbol)
            return float(snap["last"])
        except Exception:
            return 0.0

    def _quote(self, symbol: str):
        """(last, bid, ask) одним снимком + кормит LiquidityGuard спредом."""
        try:
            snap = self.market.snapshot(symbol)
            last = float(snap.get("last") or 0.0)
            bid = snap.get("bid")
            ask = snap.get("ask")
            try:
                from services.liquidity_guard import LIQUIDITY_GUARD
                LIQUIDITY_GUARD.observe(symbol, bid, ask)
            except Exception:
                pass
            return last, bid, ask
        except Exception:
            return 0.0, None, None

    def _round_price(self, symbol: str, price: float) -> float:
        try:
            return float(self.htx.price_to_precision(symbol, price)) if self.htx else round(price, 8)
        except Exception:
            return round(price, 8)

    def _round_qty(self, symbol: str, qty: float) -> float:
        try:
            return float(self.htx.amount_to_precision(symbol, qty)) if self.htx else round(qty, 8)
        except Exception:
            return round(qty, 8)

    def _envelope(self) -> float:
        # В LIVE — реальный свободный USDT счёта сетки (swap для USDT-M / spot),
        # в paper — RISK_EQUITY_USDT. Карман = доля свободного баланса.
        try:
            from services.live_executor import LIVE_EXECUTOR
            equity = LIVE_EXECUTOR.effective_equity_usdt(getattr(settings, "EXECUTION_MARKET", "swap"))
        except Exception:
            equity = float(getattr(settings, "RISK_EQUITY_USDT", 950.0))
        return equity * float(getattr(settings, "GRID_MAX_USED_MARGIN_PCT", 20.0)) / 100.0

    def _fresh_market(self, symbol: str):
        """Живые индикаторы + регайм + ATR по grid-ТФ. None при нехватке данных."""
        try:
            ema_p = int(getattr(settings, "GRID_EMA_PERIOD", 200))
            limit = max(ema_p + 60, 260)
            df = self.market.ohlcv(symbol, timeframe=str(getattr(settings, "GRID_TIMEFRAME", "1h")), limit=limit)
            if df is None or len(df) < ema_p:
                return None
            ind = gc.compute_indicators(
                df, ema_period=ema_p,
                rsi_period=int(getattr(settings, "GRID_RSI_PERIOD", 14)),
                atr_period=int(getattr(settings, "GRID_ATR_PERIOD", 14)),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[GRID INDICATORS] {symbol}: {exc}")
            return None
        atr = float(ind.get("atr") or 0.0)
        if atr <= 0:
            return None
        regime = gc.detect_regime(
            ind, float(getattr(settings, "GRID_RSI_HIGH", 70.0)),
            float(getattr(settings, "GRID_RSI_LOW", 30.0)),
        )
        return regime, atr, ind

    # ── публичный тик ─────────────────────────────────────────────────────────
    def tick_all(self) -> dict:
        if not self.store.is_enabled():
            return {"enabled": False, "ticked": 0}
        n = 0
        for sym in settings.grid_symbols:
            try:
                self.tick(sym)
                n += 1
            except Exception as exc:  # noqa: BLE001 — fail-open на символ
                print(f"[GRID ERROR] {sym}: {type(exc).__name__}: {exc}")
        return {"enabled": True, "ticked": n}

    def tick(self, symbol: str):
        symbol = symbol.upper()
        last, bid, ask = self._quote(symbol)  # греет LiquidityGuard спредом символа
        price = last
        if price <= 0:
            return

        cyc = self.store.get_cycle(symbol)
        if cyc is None:
            self._maybe_open(symbol, price, bid=bid, ask=ask)
            return

        # Адаптация к живому рынку ДО филлов: пере-раскладка пустых уровней под
        # текущий ATR/дрейф и разворот направления при смене регайма.
        if bool(getattr(settings, "GRID_ADAPT_ENABLED", True)):
            if self._adapt(cyc, symbol, price) == "flipped":
                self._maybe_open(symbol, price, bid=bid, ask=ask)  # сразу в новом направлении
                return
            cyc = self.store.get_cycle(symbol)   # перечитать после возможной записи
            if cyc is None:
                return

        self._process_fills(cyc, symbol, price)
        self._check_exits(cyc, symbol, price, bid=bid, ask=ask)

    # ── адаптация открытого цикла к живому рынку ──────────────────────────────
    def _adapt(self, cyc: dict, symbol: str, price: float) -> str:
        """Переоценка цикла по живым EMA200/RSI/ATR.

        Возвращает "flipped" если корзина закрыта под разворот (вызывающий
        откроет новый цикл), иначе "kept". Исполненные уровни не трогаются.
        """
        fm = self._fresh_market(symbol)
        if not fm:
            return "kept"
        regime_now, atr, ind = fm
        cyc["atr"] = atr
        cyc["ind"] = {"ema": round(ind["ema"], 6), "rsi": round(ind["rsi"], 2)}
        cyc["regime_now"] = regime_now
        cyc["adapted_at"] = _now()

        side = cyc.get("regime")  # исходное направление сетки
        opposite = (side == "long" and regime_now == "short") or \
                   (side == "short" and regime_now == "long")

        # гистерезис разворота
        if opposite and bool(getattr(settings, "GRID_FLIP_ON_REGIME", True)):
            cyc["flip_streak"] = int(cyc.get("flip_streak", 0)) + 1
        else:
            cyc["flip_streak"] = 0

        # заморозка добора на боковике (выходы продолжают работать)
        if bool(getattr(settings, "GRID_FREEZE_ON_NEUTRAL", True)):
            cyc["frozen"] = (regime_now == "neutral")

        confirm = int(getattr(settings, "GRID_FLIP_CONFIRM_TICKS", 3))
        if cyc["flip_streak"] >= confirm:
            filled = [lv for lv in cyc["levels"] if lv.get("filled")]
            unreal = gc.unrealized_pnl(filled, price) if filled else 0.0
            self.store.close_cycle(symbol, realized=unreal, reason="grid_regime_flip", price=price)
            print(f"[GRID FLIP] {symbol} {side}->{regime_now} streak={cyc['flip_streak']} pnl={unreal:.4f}")
            return "flipped"

        # пере-раскладка пустых уровней под текущий ATR/дрейф
        if bool(getattr(settings, "GRID_RESPACE_ENABLED", True)):
            if self._respace(cyc, symbol, atr):
                self._recompute(cyc)

        self.store.put_cycle(symbol, cyc)
        return "kept"

    def _respace(self, cyc: dict, symbol: str, atr: float) -> bool:
        """Пере-разложить НЕисполненные уровни под текущий ATR. Якорь стороны —
        самый глубокий исполненный уровень (реальная позиция). Исполненные ордера
        и их объёмы неизменны. Возвращает True, если цены изменились."""
        levels = cyc.get("levels", [])
        k_vol = float(getattr(settings, "GRID_VOL_COEFF", 0.5))
        m_step = float(getattr(settings, "GRID_STEP_MULTIPLIER", 1.1))
        last_price = float(cyc.get("last_price") or cyc.get("anchor") or 0.0)
        changed = False

        for side in ("buy", "sell"):
            unfilled = [lv for lv in levels if lv["side"] == side and not lv.get("filled")]
            if not unfilled:
                continue
            filled_prices = [float(lv.get("fill_price") or lv["price"])
                             for lv in levels if lv["side"] == side and lv.get("filled")]
            if filled_prices:
                base = min(filled_prices) if side == "buy" else max(filled_prices)
            else:
                base = last_price
            if base <= 0:
                continue
            respaced = gc.respace_levels(unfilled, base, atr, side, k_vol, m_step)
            by_n = {lv["n"]: lv for lv in respaced}
            for lv in levels:
                if lv["side"] != side or lv.get("filled") or lv["n"] not in by_n:
                    continue
                new_price = self._round_price(symbol, by_n[lv["n"]]["price"])
                if new_price != lv["price"]:
                    lv["price"] = new_price
                    lv["distance_pct"] = by_n[lv["n"]]["distance_pct"]
                    changed = True
        return changed

    # ── открытие нового цикла ─────────────────────────────────────────────────
    def _maybe_open(self, symbol: str, price: float, *, bid=None, ask=None):
        # карман маржи: есть ли место хотя бы под базовый ордер
        lev = max(float(getattr(settings, "GRID_LEVERAGE", 1.0)), 1e-9)
        base_usdt = float(getattr(settings, "GRID_BASE_ORDER_USDT", 20.0))
        free = self._envelope() - self.store.grid_used_margin()
        if free < base_usdt / lev:
            return  # нет свободной маржи в кармане сетки

        # LiquidityGuard: не открываем новый цикл при широком спреде (свип-риск).
        try:
            from services.liquidity_guard import LIQUIDITY_GUARD
            blocked, reason, sp = LIQUIDITY_GUARD.entry_blocked(symbol, bid, ask)
            if blocked:
                print(f"[GRID LIQ-BLOCK] {symbol} open skipped: {reason}")
                return
        except Exception:
            pass

        fm = self._fresh_market(symbol)
        if not fm:
            return
        regime, atr, ind = fm

        v_base_qty = base_usdt / price  # базовый объём в базовой монете
        levels = gc.compute_grid(
            anchor=price, atr=atr, regime=regime,
            lines=int(getattr(settings, "GRID_LINES", 6)),
            k_vol=float(getattr(settings, "GRID_VOL_COEFF", 0.5)),
            m_step=float(getattr(settings, "GRID_STEP_MULTIPLIER", 1.1)),
            v_base=v_base_qty,
            m_vol=float(getattr(settings, "GRID_VOL_MULTIPLIER", 1.2)),
        )
        if not levels:
            return
        for lv in levels:
            lv["price"] = self._round_price(symbol, lv["price"])
            lv["volume"] = self._round_qty(symbol, lv["volume"])
            lv["filled"] = False
            lv["fill_price"] = None

        cyc = {
            "symbol": symbol, "regime": regime, "anchor": price, "atr": atr,
            "timeframe": str(getattr(settings, "GRID_TIMEFRAME", "1h")),
            "leverage": lev, "status": "active", "created_at": _now(),
            "levels": levels, "breakeven": None, "tp_price": None, "sl_price": None,
            "ind": {"ema": round(ind["ema"], 6), "rsi": round(ind["rsi"], 2)},
            "regime_now": regime, "flip_streak": 0, "frozen": False,
        }
        self.store.put_cycle(symbol, cyc)
        print(f"[GRID OPEN] {symbol} regime={regime} anchor={price} atr={atr:.6f} levels={len(levels)}")

    # ── симуляция филлов (paper) ──────────────────────────────────────────────
    def _process_fills(self, cyc: dict, symbol: str, price: float):
        if cyc.get("frozen"):
            return  # боковик: новые уровни не добираем, ждём выхода по TP/SL
        lev = max(float(getattr(settings, "GRID_LEVERAGE", 1.0)), 1e-9)
        max_orders = int(getattr(settings, "GRID_MAX_SAFETY_ORDERS", 6))
        envelope = self._envelope()
        filled_now = sum(1 for lv_ in cyc["levels"] if lv_.get("filled"))
        changed = False

        for lv_ in cyc["levels"]:
            if lv_.get("filled"):
                continue
            if filled_now >= max_orders:
                break  # лимит страховочных ордеров достигнут
            crossed = (lv_["side"] == "buy" and price <= lv_["price"]) or \
                      (lv_["side"] == "sell" and price >= lv_["price"])
            if not crossed:
                continue
            # проверка кармана маржи перед филлом
            add_margin = float(lv_["volume"]) * float(lv_["price"]) / lev
            if self.store.grid_used_margin() + add_margin > envelope:
                break  # карман сетки заполнен
            lv_["filled"] = True
            lv_["fill_price"] = lv_["price"]  # paper: лимит исполняется по своей цене
            filled_now += 1
            changed = True

        if changed:
            self._recompute(cyc)
            self.store.put_cycle(symbol, cyc)

    def _recompute(self, cyc: dict):
        filled = [lv for lv in cyc["levels"] if lv.get("filled")]
        if not filled:
            cyc["breakeven"] = cyc["tp_price"] = cyc["sl_price"] = None
            return
        fee = float(getattr(settings, "GRID_FEE_ROUND_PCT", 0.1))
        ps = gc.position_state(filled, fee)
        side = ps["dominant_side"]
        if side == "flat":
            cyc["breakeven"] = cyc["tp_price"] = cyc["sl_price"] = None
            cyc["position"] = ps
            return
        be = gc.breakeven_price(ps["avg_price"], side, fee)
        cyc["breakeven"] = round(be, 8)
        cyc["tp_price"] = round(gc.take_profit_price(be, side, float(getattr(settings, "GRID_TP_PCT", 0.5))), 8)
        cyc["sl_price"] = round(gc.stop_loss_price(filled, cyc["atr"], side,
                                                   float(getattr(settings, "GRID_SL_ATR_MULT", 1.5))), 8)
        cyc["position"] = ps

    # ── проверка выходов (TP / SL по всей корзине) ────────────────────────────
    def _check_exits(self, cyc: dict, symbol: str, price: float, *, bid=None, ask=None):
        filled = [lv for lv in cyc["levels"] if lv.get("filled")]
        if not filled:
            return
        self._recompute(cyc)
        side = (cyc.get("position") or {}).get("dominant_side")
        tp, sl = cyc.get("tp_price"), cyc.get("sl_price")
        if side not in ("long", "short") or not tp or not sl:
            return

        unreal = gc.unrealized_pnl(filled, price)

        tp_hit = (side == "long" and price >= tp) or (side == "short" and price <= tp)
        sl_hit = (side == "long" and price <= sl) or (side == "short" and price >= sl)

        if tp_hit:
            self.store.close_cycle(symbol, realized=unreal, reason="grid_take_profit", price=price)
            print(f"[GRID TP] {symbol} closed basket pnl={unreal:.4f} @ {price}")
        elif sl_hit:
            # LiquidityGuard: на спайке спреда стоп может быть «сносом» — не закрываем
            # корзину по раздутому стакану, ждём нормализации (хард-риск ATR-стопа
            # остаётся: при реальном ходе цена удержится за SL и закроемся следующим
            # тиком без спайка).
            try:
                from services.liquidity_guard import LIQUIDITY_GUARD
                if LIQUIDITY_GUARD.exit_suppressed(symbol, bid, ask):
                    cyc["unrealized_pnl"] = unreal
                    cyc["last_price"] = price
                    cyc["sl_suppressed_spread"] = True
                    self.store.put_cycle(symbol, cyc)
                    print(f"[GRID SL-HOLD] {symbol} SL отложен: спайк спреда @ {price}")
                    return
            except Exception:
                pass
            cyc.pop("sl_suppressed_spread", None)
            self.store.close_cycle(symbol, realized=unreal, reason="grid_stop_loss", price=price)
            print(f"[GRID SL] {symbol} closed basket pnl={unreal:.4f} @ {price}")
        else:
            # просто обновим текущий нереализованный для фронта
            cyc["unrealized_pnl"] = unreal
            cyc["last_price"] = price
            self.store.put_cycle(symbol, cyc)

    def close_now(self, symbol: str, reason: str = "manual_close") -> dict:
        """Ручное закрытие цикла по символу (по агрегату корзины)."""
        symbol = symbol.upper()
        cyc = self.store.get_cycle(symbol)
        if not cyc:
            return {"status": "no_cycle", "symbol": symbol}
        price = self._price(symbol)
        filled = [lv for lv in cyc["levels"] if lv.get("filled")]
        unreal = gc.unrealized_pnl(filled, price) if filled else 0.0
        self.store.close_cycle(symbol, realized=unreal, reason=reason, price=price)
        return {"status": "closed", "symbol": symbol, "realized": round(unreal, 6), "price": price}

    # ── снимок для фронта ─────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        summary = self.store.summary()
        cycles = []
        for cyc in self.store.cycles.values():
            filled = [lv for lv in cyc.get("levels", []) if lv.get("filled")]
            cycles.append({
                "symbol": cyc["symbol"], "regime": cyc["regime"], "anchor": cyc["anchor"],
                "atr": cyc["atr"], "status": cyc["status"], "created_at": cyc["created_at"],
                "levels_total": len(cyc.get("levels", [])), "levels_filled": len(filled),
                "breakeven": cyc.get("breakeven"), "tp_price": cyc.get("tp_price"),
                "sl_price": cyc.get("sl_price"), "unrealized_pnl": cyc.get("unrealized_pnl"),
                "last_price": cyc.get("last_price"),
                "position": cyc.get("position"),
                "ind": cyc.get("ind"),
                "regime_now": cyc.get("regime_now"),
                "flip_streak": cyc.get("flip_streak", 0),
                "frozen": bool(cyc.get("frozen")),
                "adapted_at": cyc.get("adapted_at"),
                "levels": cyc.get("levels", []),
            })
        return {**summary, "cycles": cycles, "history": self.store.history[-20:]}
