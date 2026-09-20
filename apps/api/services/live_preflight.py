"""Проверка счёта перед включением live (#live-preflight-2026-09-16).

Цель владельца: пуск live = выбрать биржу и переключить рубильники. Здесь по
кнопке, только чтением, проверяется всё, что этому мешает или что стоит знать
до первой реальной сделки: версия ccxt, ключи, доступ, режим счёта, капитал и
размер сделки, спецификации рынков, ручная торговля владельца на символах
робота, остатки прошлого live, позиции из paper, kill switch, гейты допуска —
и какие рубильники осталось переключить.

Ничего не меняет ни в системе, ни на бирже. Приватные запросы идут только по
вызову владельца, не в фоне.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.config import settings
from services.market_routing import _swap_symbol, from_payload as route_from_payload

# Формы запросов OKX/HTX проверены на этой версии (tests/test_live_margin_mode.py,
# tests/test_exchange_stop.py собирают запросы настоящим ccxt без сети).
# Версия закреплена в requirements.txt, и тест держит эти два места вместе:
# 19.09 прод уехал на 4.5.78 при незакреплённой зависимости, а константа
# осталась на 4.5.77 — preflight увидел расхождение уже на проде.
TESTED_CCXT_VERSION = "4.5.78"

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"


class LivePreflight:
    def __init__(self, executor=None):
        if executor is None:
            from services.live_executor import LIVE_EXECUTOR

            executor = LIVE_EXECUTOR
        self.executor = executor
        self.checks: list[dict[str, Any]] = []

    def _add(self, check_id: str, status: str, title: str, detail: str | None = None, **data) -> None:
        self.checks.append({"id": check_id, "status": status, "title": title, "detail": detail, **data})

    # ── рубильники ─────────────────────────────────────────────────────────────
    @staticmethod
    def switches() -> list[dict[str, Any]]:
        def row(key, required, ok):
            return {"key": key, "current": getattr(settings, key, None), "required": required, "ok": bool(ok)}

        return [
            row("ACTIVE_EXCHANGE", "okx или htx", str(settings.active_exchange) in ("okx", "htx")),
            row("ROBOT_MODE", "live", str(settings.ROBOT_MODE) != "paper"),
            row("TRADING_MODE", "live_limited или live", settings.TRADING_MODE in ("live", "live_limited")),
            row("ENABLE_LIVE_ORDERS", True, bool(settings.ENABLE_LIVE_ORDERS)),
            row("LIVE_EXECUTION_MODE", "live", str(getattr(settings, "LIVE_EXECUTION_MODE", "")).lower() == "live"),
            row("ENABLE_FUTURES", True, bool(getattr(settings, "ENABLE_FUTURES", False))),
            row("ENABLE_FUTURES_EXECUTION", True, bool(getattr(settings, "ENABLE_FUTURES_EXECUTION", False))),
            row("LIVE_EXCHANGE_STOP_ENABLED", True, bool(getattr(settings, "LIVE_EXCHANGE_STOP_ENABLED", True))),
            row("EXCHANGE_RECONCILIATION_ENABLED", True,
                bool(getattr(settings, "EXCHANGE_RECONCILIATION_ENABLED", True))),
        ]

    # ── проверка ───────────────────────────────────────────────────────────────
    def run(self, db, bot=None) -> dict[str, Any]:
        self.checks = []
        exchange = settings.active_exchange
        client = self.executor.client

        self._check_ccxt()
        self._check_keys(exchange)

        free = self._check_balance()
        account = self._check_account_mode(client)
        robot_trades, paper_trades = self._book(db, bot)
        self._check_capital(db, bot, free)
        universe = sorted({_swap_symbol(s) for s in settings.symbols_for(exchange)})
        self._check_markets(client, universe)
        self._check_exchange_activity(client, universe, robot_trades)
        self._check_paper_positions(paper_trades)
        self._check_absolute_thresholds(db, bot, free)
        self._check_risk_fits_the_daily_limit(db, bot, free)
        self._check_kill_switch(bot)
        self._check_gates(db)

        switches = self.switches()
        to_flip = [s for s in switches if not s["ok"]]
        failed = [c for c in self.checks if c["status"] == FAIL]
        return {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "exchange": exchange,
            # Готовность счёта и системы; рубильники — отдельным списком.
            "ready": not failed,
            "live_now": self.executor.effective_mode() == "live",
            "account": account,
            "checks": self.checks,
            "switches": switches,
            "switches_to_flip": [s["key"] for s in to_flip],
            "summary": {status: sum(1 for c in self.checks if c["status"] == status)
                        for status in (OK, INFO, WARN, FAIL)},
        }

    def _check_ccxt(self) -> None:
        try:
            import ccxt

            version = str(ccxt.__version__)
        except Exception as exc:  # noqa: BLE001
            self._add("ccxt_version", FAIL, "ccxt", f"не импортируется: {exc}")
            return
        if version == TESTED_CCXT_VERSION:
            self._add("ccxt_version", OK, "ccxt", f"{version} — на ней проверены формы запросов", value=version)
        else:
            self._add("ccxt_version", WARN, "ccxt",
                      f"стоит {version}, формы запросов OKX/HTX проверены на {TESTED_CCXT_VERSION}; "
                      f"ccxt в requirements.txt не закреплён", value=version)

    def _check_keys(self, exchange: str) -> None:
        if exchange == "okx":
            present = all(str(getattr(settings, k, "") or "") for k in
                          ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE"))
            names = "OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE"
        else:
            present = all(str(getattr(settings, k, "") or "") for k in ("HTX_API_KEY", "HTX_API_SECRET"))
            names = "HTX_API_KEY, HTX_API_SECRET"
        self._add("api_keys", OK if present else FAIL, "Ключи API",
                  "заданы" if present else f"не заданы: {names}")

    def _check_balance(self) -> float | None:
        total = 0.0
        seen = False
        details = {}
        for account in self.executor.execution_accounts():
            try:
                bal = self.executor.client.fetch_balance(params={"type": account}) or {}
                usdt = bal.get("USDT") or {}
                free = usdt.get("free") if isinstance(usdt, dict) else None
                details[account] = {"free": free, "total": usdt.get("total") if isinstance(usdt, dict) else None}
                if free is not None:
                    total += float(free)
                    seen = True
            except Exception as exc:  # noqa: BLE001
                self._add("balance", FAIL, "Доступ к счёту",
                          f"{account}: {type(exc).__name__}: {exc} — ключ, права (чтение+торговля) или IP")
                return None
        if not seen:
            self._add("balance", FAIL, "Доступ к счёту", "USDT на счёте исполнения не найден", accounts=details)
            return None
        self._add("balance", OK, "Доступ к счёту", f"свободно {round(total, 2)} USDT", accounts=details)
        return total

    def _check_account_mode(self, client) -> dict:
        fetch = getattr(client, "fetch_derivatives_account", None)
        if not callable(fetch):
            self._add("account_mode", WARN, "Режим счёта", "биржа не отдаёт режим счёта")
            return {}
        try:
            state = fetch() or {}
        except Exception as exc:  # noqa: BLE001
            self._add("account_mode", FAIL, "Режим счёта", f"{type(exc).__name__}: {exc}")
            return {}
        if state.get("blocker"):
            self._add("account_mode", FAIL, "Режим счёта", str(state["blocker"]), info=state.get("info"))
        elif state.get("hedged") is None:
            self._add("account_mode", WARN, "Режим счёта",
                      "режим позиций не определён — ордера пойдут как для One-way", info=state.get("info"))
        else:
            mode = "Long/Short (hedge)" if state["hedged"] else "One-way"
            self._add("account_mode", OK, "Режим счёта", f"свопы торгуются, режим позиций {mode}",
                      info=state.get("info"))
        return {"hedged": state.get("hedged"), "blocker": state.get("blocker"), "info": state.get("info")}

    def _book(self, db, bot) -> tuple[list, list]:
        from models.signal import Signal

        query = db.query(Signal).filter(Signal.status.in_(("opened", "tp1", "breakeven")))
        if bot is not None:
            query = query.filter(Signal.bot_id == bot.id)
        live, paper = [], []
        for signal in query.all():
            mode = ((signal.plan_json or {}).get("execution") or {}).get("mode")
            (live if mode == "live" else paper).append(signal)
        return live, paper

    def _check_capital(self, db, bot, free: float | None) -> None:
        if free is None:
            self._add("capital", FAIL, "Капитал робота", "свободный баланс не прочитан")
            return
        robot_margin = 0.0
        if bot is not None:
            from services.exposure_guard import ExposureGuard

            robot_margin = ExposureGuard().live_position_margin(db, bot.id)
        capital = free + robot_margin
        leverage = self.executor._leverage_value(getattr(settings, "FUTURES_LEVERAGE", 1))
        max_margin = capital * max(0.0, min(float(getattr(settings, "MAX_POSITION_MARGIN_PCT", 0.2)), 1.0))
        # (#sizing-scales-with-equity-2026-09-19) Потолок ордера спрашиваем у
        # конфига с капиталом и плечом: когда он задан долей экспозиции, цифра
        # в preflight обязана быть той же, что получит сайзинг.
        cap = float(settings.max_order_notional(capital, leverage) or 0.0)
        max_notional = max_margin * float(leverage)
        if cap > 0:
            max_notional = min(max_notional, cap)
        data = {
            "free_usdt": round(free, 2), "robot_live_margin_usdt": round(robot_margin, 2),
            "capital_usdt": round(capital, 2), "leverage": leverage,
            "risk_per_trade_usdt": round(capital * float(getattr(settings, "RISK_PER_TRADE_PCT", 0.4)) / 100, 2),
            "max_position_margin_usdt": round(max_margin, 2), "max_order_notional_usdt": round(max_notional, 2),
            "order_cap_usdt": round(cap, 2),
            "order_cap_scales_with_equity": float(
                getattr(settings, "LIVE_MAX_ORDER_NOTIONAL_PCT", 0.0) or 0.0) > 0,
            "accounts": self.executor.execution_accounts(),
        }
        detail = (f"капитал {data['capital_usdt']} USDT = свободно {data['free_usdt']} + маржа позиций "
                  f"робота {data['robot_live_margin_usdt']}; плечо {leverage}×, риск на сделку "
                  f"{data['risk_per_trade_usdt']}, позиция до {data['max_order_notional_usdt']} USDT. "
                  f"Маржа ручных позиций и ордеров уже вычтена биржей")
        if capital <= 0:
            self._add("capital", FAIL, "Капитал робота", detail, **data)
        elif cap > 0 and max_notional < cap * 0.2:
            self._add("capital", WARN, "Капитал робота",
                      detail + " — позиции будут заметно меньше, чем в paper", **data)
        else:
            self._add("capital", OK, "Капитал робота", detail, **data)

    # Пороги, заданные суммой в USDT, и БАЗА, с которой каждый сравним.
    # Первая версия этой проверки мерила все шесть долей от одного номинала и
    # врала трижды: потолок размера сделки сравнивался сам с собой (всегда
    # 100%), адаптивные пороги TP1/TP2 попадали в «великоваты», хотя гейт
    # берёт min(сумма, 1% маржи) и смягчает их сам, а толеранс символа — это
    # сумма за окно наблюдения, а не прибыль одной сделки.
    #   notional  — порог на прибыль ОДНОЙ сделки: сравним с её номиналом;
    #   exposure  — потолок размера: сравним с капиталом × плечо;
    #   adaptive  — сумма, которую гейт смягчает сам; показываем, не ругаем.
    ABSOLUTE_THRESHOLDS: tuple[tuple[str, str, str, str | None], ...] = (
        ("ANTI_DRAIN_MIN_EDGE_AFTER_COSTS_USDT", "запас экономики сделки",
         "notional", "ANTI_DRAIN_MIN_EDGE_AFTER_COSTS_PCT"),
        ("MIN_PROTECTIVE_NET_USDT", "порог защитного выхода", "notional", None),
        ("LIVE_MAX_ORDER_NOTIONAL_USDT", "потолок размера сделки",
         "exposure", "LIVE_MAX_ORDER_NOTIONAL_PCT"),
        ("MIN_NET_PNL_TP2_USDT", "минимальная прибыль на TP2", "adaptive", None),
        ("MIN_NET_PNL_TP1_USDT", "минимальная прибыль на TP1", "adaptive", None),
        ("SYMBOL_PERF_WEAK_PNL_TOLERANCE_USDT", "толеранс символа около нуля",
         "window_pnl", None),
    )
    # Выше этой доли номинала порог на прибыль сделки перестаёт быть страховкой
    # и начинает управлять отбором.
    THRESHOLD_WARN_SHARE_PCT = 0.5
    # Потолок размера, съедающий больше этой доли экспозиции, означает, что
    # система торгует малой частью счёта, и об этом стоит знать.
    ORDER_CAP_WARN_SHARE_PCT = 50.0

    def _check_absolute_thresholds(self, db, bot, free: float | None) -> None:
        """Насколько пороги-суммы соответствуют размеру сделки на этом счёте.

        (#sizing-scales-with-equity-2026-09-19) Живёт в preflight намеренно:
        именно при смене капитала эти суммы и расходятся с реальностью, а
        заметно это становится уже по пустой ленте решений.
        """
        capital = self._capital_usdt(db, bot, free)
        if not capital:
            self._add("absolute_thresholds", INFO, "Пороги-суммы",
                      "капитал неизвестен — сравнить не с чем")
            return

        leverage = self.executor._leverage_value(getattr(settings, "FUTURES_LEVERAGE", 1))
        exposure = capital * float(leverage)
        margin_share = max(0.0, min(float(getattr(settings, "MAX_POSITION_MARGIN_PCT", 0.2)), 1.0))
        order_cap = float(settings.max_order_notional(capital, leverage) or 0.0)
        # Размер сделки — это МИНИМУМ из потолков, а не сам потолок ордера.
        # Риск-сайзинг сюда не входит: он зависит от дистанции стопа конкретной
        # сделки, и без неё его не посчитать.
        notional = min(x for x in (exposure, capital * margin_share * float(leverage),
                                   order_cap or exposure) if x > 0)

        if notional < 1.0:
            self._add("absolute_thresholds", WARN, "Пороги-суммы",
                      f"капитал {round(capital, 2)} USDT — сделка такого размера "
                      f"({round(notional, 4)} USDT) не состоится ни при каких порогах; "
                      f"сравнивать их с ней бессмысленно",
                      thresholds=[], heavy=[], notional_usdt=round(notional, 4))
            return

        rows, heavy = [], []
        for key, title, base, replacement in self.ABSOLUTE_THRESHOLDS:
            value = float(getattr(settings, key, 0.0) or 0.0)
            if value <= 0:
                continue
            reference = {"notional": notional, "exposure": exposure,
                         "adaptive": notional, "window_pnl": notional}[base]
            share = round(value / reference * 100, 4) if reference else None
            replaced = bool(replacement and float(getattr(settings, replacement, 0.0) or 0.0) > 0)
            row = {"key": key, "title": title, "usdt": value, "base": base,
                   "share_pct": share, "scaled_by": replacement, "scaling_on": replaced}
            if base == "adaptive":
                # Гейт берёт min(сумма, 1% маржи) — на малом счёте порог
                # опускается сам, и претензии к сумме нет.
                row["effective_usdt"] = round(min(value, notional / float(leverage) * 0.01), 4)
            rows.append(row)
            if replaced or base == "adaptive":
                continue
            if base == "notional" and share is not None and share > self.THRESHOLD_WARN_SHARE_PCT:
                heavy.append(row)
            elif base == "exposure" and share is not None and share < self.ORDER_CAP_WARN_SHARE_PCT:
                heavy.append(row)

        detail = (f"капитал {round(capital, 2)}, плечо {leverage}×, экспозиция "
                  f"{round(exposure, 2)}; сделка до {round(notional, 2)} USDT")
        if heavy:
            names = ", ".join(f"{r['key']} ({r['share_pct']:.2f}% {'экспозиции' if r['base'] == 'exposure' else 'номинала'})"
                              for r in heavy)
            self._add("absolute_thresholds", WARN, "Пороги-суммы",
                      f"{detail}. Не по размеру этого счёта: {names} — "
                      f"калибровались под другой; у части есть замена долей",
                      thresholds=rows, heavy=[r["key"] for r in heavy],
                      notional_usdt=round(notional, 2), exposure_usdt=round(exposure, 2))
        else:
            self._add("absolute_thresholds", OK, "Пороги-суммы", detail,
                      thresholds=rows, heavy=[],
                      notional_usdt=round(notional, 2), exposure_usdt=round(exposure, 2))

    # Меньше трёх стопов до дневного лимита — робот останавливается раньше,
    # чем серия успевает отработать, и настройки просто не работают вместе.
    MIN_STOPS_BEFORE_DAILY_LIMIT = 3.0

    def _check_risk_fits_the_daily_limit(self, db, bot, free: float | None) -> None:
        """Сходятся ли риск на сделку и дневной лимит убытка.

        (#any-deposit-any-leverage-2026-09-20) Обе настройки — доли капитала, и
        по отдельности каждая выглядит разумной. Вместе они задают, сколько
        стопов подряд робот переживёт за день, и это число нигде не
        показывалось: при риске 1.5% и лимите 3% их ровно два.
        """
        capital = self._capital_usdt(db, bot, free)
        risk_pct = float(getattr(settings, "RISK_PER_TRADE_PCT", 0.0) or 0.0)
        daily_pct = float(getattr(settings, "MAX_DAILY_LOSS_PCT", 0.0) or 0.0)
        if not capital or risk_pct <= 0 or daily_pct <= 0:
            self._add("risk_vs_daily_limit", INFO, "Риск против дневного лимита",
                      "одна из настроек выключена — сравнивать нечего")
            return

        stops = daily_pct / risk_pct
        leverage = self.executor._leverage_value(getattr(settings, "FUTURES_LEVERAGE", 1))
        # Сколько экспозиции робот реально займёт: размер сделки от риска,
        # умноженный на предел одновременных позиций.
        positions = int(getattr(settings, "ANTI_DRAIN_MAX_OPEN_POSITIONS", 5) or 5)
        typical_stop_pct = 1.5
        by_risk = capital * risk_pct / 100.0 / (typical_stop_pct / 100.0)
        size = min(by_risk,
                   capital * float(getattr(settings, "MAX_POSITION_MARGIN_PCT", 0.13)) * leverage,
                   float(settings.max_order_notional(capital, leverage) or by_risk) or by_risk)
        exposure = capital * leverage
        used_pct = round(size * positions / exposure * 100, 1) if exposure else None

        data = {"stops_before_daily_limit": round(stops, 1),
                "risk_per_trade_pct": risk_pct, "max_daily_loss_pct": daily_pct,
                "typical_trade_usdt": round(size, 2),
                "exposure_usdt": round(exposure, 2),
                "exposure_used_pct": used_pct,
                # Плечо, которое система использует ПО ФАКТУ: заданное 10× при
                # 13% занятой экспозиции — это 1.3×, и ощущение масштаба ложное.
                "effective_leverage": round(size * positions / capital, 2) if capital else None}
        detail = (f"риск {risk_pct}% против лимита {daily_pct}% — {stops:.1f} стопов подряд; "
                  f"сделка ~{size:.0f} USDT, {positions} позиций занимают {used_pct}% "
                  f"экспозиции {exposure:.0f} (фактическое плечо {data['effective_leverage']}×)")
        if stops < self.MIN_STOPS_BEFORE_DAILY_LIMIT:
            self._add("risk_vs_daily_limit", WARN, "Риск против дневного лимита",
                      f"{detail}. Робот остановится раньше, чем серия отработает: "
                      f"поднимать риск без дневного лимита бессмысленно", **data)
        else:
            self._add("risk_vs_daily_limit", OK, "Риск против дневного лимита", detail, **data)

    def _capital_usdt(self, db, bot, free: float | None) -> float:
        if free is None:
            return 0.0
        robot_margin = 0.0
        if bot is not None:
            from services.exposure_guard import ExposureGuard

            robot_margin = ExposureGuard().live_position_margin(db, bot.id)
        return float(free) + float(robot_margin)

    def _check_markets(self, client, universe: list[str]) -> None:
        getter = getattr(client, "contract_size", None)
        unknown = []
        for symbol in universe:
            try:
                size = getter(symbol) if callable(getter) else None
            except Exception:  # noqa: BLE001
                size = None
            if not size:
                unknown.append(symbol)
        if unknown:
            self._add("markets", FAIL, "Рынки свопов",
                      f"нет размера контракта: {', '.join(unknown)} — ордера по ним не уйдут", symbols=unknown)
        else:
            self._add("markets", OK, "Рынки свопов", f"{len(universe)} символов, размер контракта известен")

    def _check_exchange_activity(self, client, universe: list[str], robot_trades: list) -> None:
        from services.robot_orders import is_robot_order

        robot_margin = None
        try:
            robot_margin = self.executor.resolve_margin_mode(getattr(settings, "TREND_MARGIN_MODE", None))
        except ValueError:
            pass
        robot_symbols = set()
        for signal in robot_trades:
            route = route_from_payload(signal.plan_json, signal.symbol, signal.side)
            robot_symbols.add(route.exchange_symbol)

        manual_orders, robot_leftovers, mixing, separate = [], [], [], []
        try:
            positions = client.fetch_positions() or []
        except Exception as exc:  # noqa: BLE001
            self._add("exchange_activity", WARN, "Ручная торговля на символах робота",
                      f"позиции не прочитаны: {type(exc).__name__}: {exc}")
            positions = []
        for p in positions:
            if not isinstance(p, dict) or p.get("symbol") not in universe:
                continue
            try:
                if abs(float(p.get("contracts") or 0.0)) <= 0:
                    continue
            except (TypeError, ValueError):
                continue
            row = {"symbol": p.get("symbol"), "side": p.get("side"), "contracts": p.get("contracts"),
                   "margin_mode": p.get("marginMode")}
            if p.get("symbol") in robot_symbols:
                continue
            if robot_margin and str(p.get("marginMode") or "").lower() == robot_margin:
                mixing.append(row)
            else:
                separate.append(row)

        for symbol in universe:
            for fetch_name in ("fetch_open_orders", "fetch_open_stop_orders"):
                fetch = getattr(client, fetch_name, None)
                if not callable(fetch):
                    continue
                try:
                    orders = fetch(symbol) or []
                except Exception:  # noqa: BLE001
                    continue
                for order in orders:
                    row = {"symbol": symbol, "id": order.get("id"), "side": order.get("side"),
                           "kind": "stop" if fetch_name == "fetch_open_stop_orders" else "order"}
                    if is_robot_order(order):
                        if symbol not in robot_symbols:
                            robot_leftovers.append(row)
                    else:
                        manual_orders.append(row)

        if mixing:
            self._add("manual_positions_mixing", WARN, "Ручные позиции в режиме маржи робота",
                      f"{len(mixing)} позиций в режиме {robot_margin} на символах робота: при входе робота "
                      f"они сольются с его позицией на бирже. Робот закроет только свою долю, но ручные "
                      f"удобнее вести в другом режиме маржи", positions=mixing)
        if separate or manual_orders:
            self._add("manual_activity", INFO, "Ручная торговля владельца",
                      f"позиций в другом режиме маржи: {len(separate)}, ручных ордеров и стопов: "
                      f"{len(manual_orders)} — робот их не трогает, их маржа роботу недоступна",
                      positions=separate, orders=manual_orders[:50])
        if robot_leftovers:
            self._add("robot_leftovers", WARN, "Остатки прошлого live",
                      f"{len(robot_leftovers)} ордеров/стопов робота без открытой сделки робота — снять "
                      f"вручную: стоп закрыл бы следующую сделку по старой цене", orders=robot_leftovers)
        if not (mixing or separate or manual_orders or robot_leftovers):
            self._add("exchange_activity", OK, "Символы робота на бирже", "чужих позиций и ордеров нет")

    def _check_paper_positions(self, paper_trades: list) -> None:
        if not paper_trades:
            self._add("paper_positions", OK, "Позиции из paper", "открытых нет")
            return
        self._add("paper_positions", WARN, "Позиции из paper",
                  f"{len(paper_trades)} открыто до live — после включения они закроются только в учёте, "
                  f"без ордеров на бирже (#{', #'.join(str(s.id) for s in paper_trades[:20])}); чище "
                  f"включать live, когда их нет", signal_ids=[s.id for s in paper_trades])

    def _check_kill_switch(self, bot) -> None:
        config = dict(getattr(bot, "config_json", None) or {}) if bot is not None else {}
        if config.get("kill_switch_enabled"):
            self._add("kill_switch", WARN, "Kill switch", f"включён: {config.get('kill_switch_reason')}")
        else:
            self._add("kill_switch", OK, "Kill switch", "выключен")

    def _check_gates(self, db) -> None:
        try:
            from services.validation_gates import ValidationGateService

            state = ValidationGateService().evaluate(db)
            blockers = list(state.get("blockers") or [])
        except Exception as exc:  # noqa: BLE001
            self._add("validation_gates", WARN, "Гейты допуска", f"не посчитаны: {type(exc).__name__}: {exc}")
            blockers = []
        else:
            if blockers:
                self._add("validation_gates", FAIL, "Гейты допуска",
                          "в live цикл не откроет ни одной сделки, пока не пройдены: " + "; ".join(blockers),
                          blockers=blockers)
            else:
                self._add("validation_gates", OK, "Гейты допуска", "пройдены")

        production = list(settings.production_blockers()) if hasattr(settings, "production_blockers") else []
        if production:
            self._add("production_blockers", FAIL, "Блокеры конфигурации", "; ".join(production),
                      blockers=production)
        else:
            self._add("production_blockers", OK, "Блокеры конфигурации", "нет")
