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


def _age_sec(created_at) -> float:
    """Возраст цикла в секундах по created_at (ISO). Ошибка → «очень старый»."""
    if not created_at:
        return 1e9
    try:
        dt = datetime.fromisoformat(str(created_at))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return 1e9


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
            ema_band_pct=float(getattr(settings, "GRID_REGIME_EMA_BAND_PCT", 0.25)),
        )
        return regime, atr, ind

    # ── публичный тик ─────────────────────────────────────────────────────────
    def tick_all(self) -> dict:
        if not self.store.is_enabled():
            return {"enabled": False, "ticked": 0}
        # (#grid-drain) Тикаем ОБЪЕДИНЕНИЕ: настроенные символы + символы с активными
        # циклами. Иначе при сужении GRID_SYMBOLS де-листнутые циклы осиротеют (не
        # тикаются → нет TP/SL/флипа/выхода, маржа заперта). Тик их обслужит, а
        # новые циклы по ним не откроются (см. tick()).
        symbols = list(dict.fromkeys(
            [s.upper() for s in settings.grid_symbols]
            + [str(s).upper() for s in self.store.cycles.keys()]
        ))
        n = 0
        for sym in symbols:
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

        # (#grid-drain) Символ в активном универсуме? Де-листнутый обслуживаем
        # (доводим цикл до выхода), но НОВЫХ циклов по нему не открываем.
        configured = symbol in {s.upper() for s in settings.grid_symbols}

        cyc = self.store.get_cycle(symbol)
        if cyc is None:
            if configured:
                self._maybe_open(symbol, price, bid=bid, ask=ask)
            return

        # Адаптация к живому рынку ДО филлов: пере-раскладка пустых уровней под
        # текущий ATR/дрейф и разворот направления при смене регайма.
        if bool(getattr(settings, "GRID_ADAPT_ENABLED", True)):
            if self._adapt(cyc, symbol, price) == "flipped":
                if configured:
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

        # (#grid-flip-cooldown) тихое окно после открытия/флипа: не переворачиваемся,
        # пока не прошло GRID_FLIP_COOLDOWN_SEC. Лечит почасовую пилу на монетах у EMA
        # (ETH/AAVE). streak держим на 0 → после окна нужны свежие confirm-тики.
        cooldown = float(getattr(settings, "GRID_FLIP_COOLDOWN_SEC", 0) or 0)
        in_cooldown = cooldown > 0 and _age_sec(cyc.get("created_at")) < cooldown
        cyc["flip_cooldown"] = bool(in_cooldown)

        # гистерезис разворота
        # (#audit-grid) + ATR-дистанция: streak растёт только если цена реально
        # оторвалась от EMA (|price−EMA| ≥ k×ATR). Иначе почасовая пила вокруг
        # EMA закрывала корзины мелкими flip-минусами (BTC: 4 флипа подряд).
        ema_now = float(ind.get("ema") or 0.0)
        min_dist = float(getattr(settings, "GRID_FLIP_MIN_ATR_DIST", 0.5))
        dist_ok = ema_now <= 0 or atr <= 0 or abs(price - ema_now) >= min_dist * atr
        if opposite and dist_ok and bool(getattr(settings, "GRID_FLIP_ON_REGIME", True)) and not in_cooldown:
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
            realized = self._net_realized(filled, unreal)
            self.store.close_cycle(symbol, realized=realized, reason="grid_regime_flip", price=price)
            print(f"[GRID FLIP] {symbol} {side}->{regime_now} streak={cyc['flip_streak']} pnl={realized:.4f}")
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

    # ── издержки корзины ──────────────────────────────────────────────────────
    def _net_realized(self, filled: list, gross: float) -> float:
        """(#audit-grid) Realized за вычетом round-trip комиссий по исполненному
        нотионалу. gross-учёт завышал результат сетки."""
        if not bool(getattr(settings, "GRID_FEES_IN_REALIZED", True)) or not filled:
            return float(gross)
        fee_pct = float(getattr(settings, "GRID_FEE_ROUND_PCT", 0.1)) / 100.0
        notional = sum(
            float(lv.get("volume") or 0.0) * float(lv.get("fill_price") or lv.get("price") or 0.0)
            for lv in filled
        )
        return float(gross) - notional * fee_pct

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

        # (#audit-grid) Экономический фильтр: шаг ближайшего уровня должен
        # превышать спред×mult + round-trip fee, иначе цикл платит спред каждым
        # кругом (AAVE neutral при спреде 0.1–0.5% сливал на самой механике).
        try:
            if bid and ask and float(bid) > 0 and float(ask) > float(bid):
                mid = (float(bid) + float(ask)) / 2.0
                spread_pct = (float(ask) - float(bid)) / mid * 100.0
                fee_round = float(getattr(settings, "GRID_FEE_ROUND_PCT", 0.1))
                mult = float(getattr(settings, "GRID_OPEN_MIN_EDGE_SPREAD_MULT", 1.0))
                min_dist = min(float(lv.get("distance_pct") or 1e9) for lv in levels)
                if min_dist < spread_pct * mult + fee_round:
                    print(f"[GRID ECON-SKIP] {symbol} step {min_dist:.3f}% < spread {spread_pct:.3f}%×{mult}+fee {fee_round}%")
                    return
        except Exception:
            pass

        for lv in levels:
            lv["price"] = self._round_price(symbol, lv["price"])
            lv["volume"] = self._round_qty(symbol, lv["volume"])
            lv["filled"] = False
            lv["fill_price"] = None

        cyc = {
            "symbol": symbol, "regime": regime, "anchor": price, "atr": atr,
            "timeframe": str(getattr(settings, "GRID_TIMEFRAME", "1h")),
            "leverage": lev, "margin_mode": settings.grid_effective_margin_mode,
            "status": "active", "created_at": _now(),
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

        # (#audit-grid) Анти-мартингейл: не доливаем против импульса/раннего
        # разворота. TRX-кейс: 5 доливок в шорт при растущем RSI → корзина у SL.
        if bool(getattr(settings, "GRID_ANTI_MARTINGALE_ENABLED", True)):
            side = str(cyc.get("regime") or "")
            regime_now = str(cyc.get("regime_now") or side)
            opposite = (side == "long" and regime_now == "short") or \
                       (side == "short" and regime_now == "long")
            rsi = float((cyc.get("ind") or {}).get("rsi") or 50.0)
            rsi_against = (
                (side == "short" and rsi >= float(getattr(settings, "GRID_SHORT_FILL_RSI_MAX", 65.0)))
                or (side == "long" and rsi <= float(getattr(settings, "GRID_LONG_FILL_RSI_MIN", 35.0)))
            )
            if opposite or rsi_against:
                cyc["fills_paused"] = "regime_opposite" if opposite else f"rsi_against:{rsi:.1f}"
                self.store.put_cycle(symbol, cyc)
                return
            if cyc.pop("fills_paused", None) is not None:
                self.store.put_cycle(symbol, cyc)

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
            realized = self._net_realized(filled, unreal)
            self.store.close_cycle(symbol, realized=realized, reason="grid_take_profit", price=price)
            print(f"[GRID TP] {symbol} closed basket pnl={realized:.4f} @ {price}")
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
            realized = self._net_realized(filled, unreal)
            self.store.close_cycle(symbol, realized=realized, reason="grid_stop_loss", price=price)
            print(f"[GRID SL] {symbol} closed basket pnl={realized:.4f} @ {price}")
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
        realized = self._net_realized(filled, unreal)
        self.store.close_cycle(symbol, realized=realized, reason=reason, price=price)
        return {"status": "closed", "symbol": symbol, "realized": round(realized, 6), "price": price}

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
                "flip_cooldown": bool(cyc.get("flip_cooldown")),
                "adapted_at": cyc.get("adapted_at"),
                "levels": cyc.get("levels", []),
            })
        return {**summary, "cycles": cycles, "history": self.store.history[-20:]}
