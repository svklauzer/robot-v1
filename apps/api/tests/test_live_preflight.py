"""Проверка счёта перед включением live (#live-preflight-2026-09-16)."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import settings
from core.db import Base
from models.bot import Bot
from models.order import Order
from models.position import Position
from models.signal import Signal
from models.user import User
from services import live_preflight as lp
from services.live_executor import LiveExecutor
from services.live_preflight import LivePreflight

XRP = "XRP/USDT:USDT"
BTC = "BTC/USDT:USDT"


class _Exchange:
    UNIFIED_TRADING_ACCOUNT = True

    def __init__(self, *, free=600.0, balance_error=None, account=None, positions=None,
                 orders=None, stops=None, sizes=None):
        self.free = free
        self.balance_error = balance_error
        self.account = account if account is not None else {"hedged": False, "blocker": None, "info": {}}
        self.positions = positions or []
        self.orders = orders or {}
        self.stops = stops or {}
        self.sizes = sizes if sizes is not None else {XRP: 100.0, BTC: 0.01}
        self.writes: list = []

    def fetch_balance(self, params=None):
        if self.balance_error:
            raise self.balance_error
        return {"USDT": {"free": self.free, "total": self.free + 100}}

    def fetch_derivatives_account(self):
        return self.account

    def contract_size(self, symbol):
        return self.sizes.get(symbol)

    def fetch_positions(self):
        return self.positions

    def fetch_open_orders(self, symbol=None):
        return self.orders.get(symbol, [])

    def fetch_open_stop_orders(self, symbol):
        return self.stops.get(symbol, [])

    def __getattr__(self, name):
        if name.startswith(("create", "cancel", "set_")):
            raise AssertionError(f"preflight вызвал изменяющий метод {name}")
        raise AttributeError(name)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "okx")
    monkeypatch.setattr(settings, "OKX_SYMBOLS", "XRP/USDT,BTC/USDT")
    for key in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE"):
        monkeypatch.setattr(settings, key, "set")
    monkeypatch.setattr(settings, "TREND_MARGIN_MODE", "isolated")
    monkeypatch.setattr(settings, "ENABLE_FUTURES", True)
    monkeypatch.setattr(settings, "ENABLE_FUTURES_EXECUTION", True)
    monkeypatch.setattr(settings, "FUTURES_LEVERAGE", 1)
    monkeypatch.setattr(settings, "MAX_POSITION_MARGIN_PCT", 0.2)
    monkeypatch.setattr(settings, "LIVE_MAX_ORDER_NOTIONAL_USDT", 250.0)
    monkeypatch.setattr(settings, "RISK_PER_TRADE_PCT", 0.4)
    monkeypatch.setattr(lp, "TESTED_CCXT_VERSION", __import__("ccxt").__version__)
    from services import validation_gates

    monkeypatch.setattr(validation_gates.ValidationGateService, "evaluate", lambda self, db, limit=None: {"blockers": []})
    monkeypatch.setattr(type(settings), "production_blockers", lambda self: [])


def _db(*, signals=()):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[User.__table__, Bot.__table__, Signal.__table__,
                                                  Order.__table__, Position.__table__])
    db = sessionmaker(bind=engine)()
    db.add_all([User(email="owner@example.com", password_hash="x"),
                Bot(user_id=1, name="Main Robot", status="running", mode="paper")])
    db.flush()
    for execution, margin in signals:
        db.add(Signal(bot_id=1, symbol="XRP/USDT", side="short", status="opened", exchange="okx",
                      entry_zone_json={"from": 1.4, "to": 1.41}, stop_price=1.5, tp_json={"tp1": 1.3, "tp2": 1.2},
                      required_margin=margin,
                      plan_json={"routing": {"market_type": "swap", "exchange_symbol": XRP, "side": "short",
                                             "leverage": 1, "margin_mode": "isolated"},
                                 "execution": {"mode": execution}}))
    db.commit()
    return db


def _run(exchange, db=None):
    executor = LiveExecutor.__new__(LiveExecutor)
    executor.client = exchange
    db = db or _db()
    bot = db.query(Bot).first()
    return LivePreflight(executor).run(db, bot)


def _check(result, check_id):
    return next(c for c in result["checks"] if c["id"] == check_id)


def test_clean_account_is_ready_and_lists_the_switches():
    result = _run(_Exchange())

    assert result["ready"] is True, [c for c in result["checks"] if c["status"] == "fail"]
    assert {"ROBOT_MODE", "TRADING_MODE", "ENABLE_LIVE_ORDERS", "LIVE_EXECUTION_MODE"} <= set(result["switches_to_flip"])
    assert "ACTIVE_EXCHANGE" not in result["switches_to_flip"]
    assert _check(result, "exchange_activity")["status"] == "ok"


def test_capital_is_free_margin_plus_robot_positions_with_config_leverage():
    db = _db(signals=[("live", 200.0), ("paper", 300.0)])
    capital = _check(_run(_Exchange(free=600.0), db), "capital")

    assert capital["capital_usdt"] == 800.0 and capital["robot_live_margin_usdt"] == 200.0
    assert capital["leverage"] == 1 and capital["max_order_notional_usdt"] == 160.0
    assert capital["accounts"] == ["swap"]


def test_account_mode_blocker_fails():
    blocker = "okx_account_mode_spot_only: переключить Trading mode"
    result = _run(_Exchange(account={"hedged": False, "blocker": blocker}))

    assert result["ready"] is False
    assert _check(result, "account_mode")["status"] == "fail"


def test_rejected_key_fails_access_and_capital():
    result = _run(_Exchange(balance_error=PermissionError("50113 Invalid Sign")))

    assert result["ready"] is False
    assert "50113" in _check(result, "balance")["detail"]
    assert _check(result, "capital")["status"] == "fail"


def test_missing_keys_fail(monkeypatch):
    monkeypatch.setattr(settings, "OKX_API_PASSPHRASE", "")
    assert _check(_run(_Exchange()), "api_keys")["status"] == "fail"


def test_unknown_contract_size_fails():
    result = _run(_Exchange(sizes={XRP: 100.0}))
    assert _check(result, "markets")["symbols"] == [BTC]


def test_manual_trading_is_reported_not_blocking():
    exchange = _Exchange(
        positions=[
            {"symbol": BTC, "side": "long", "contracts": 3.0, "marginMode": "isolated"},
            {"symbol": XRP, "side": "long", "contracts": 5.0, "marginMode": "cross"},
        ],
        orders={XRP: [{"id": "m1", "clientOrderId": None, "side": "buy"}]},
        stops={BTC: [{"id": "m2", "clientOrderId": "", "side": "sell"},
                     {"id": "r1", "clientOrderId": "rbtslexch00aa", "side": "buy"}]},
    )
    result = _run(exchange)

    assert result["ready"] is True
    mixing = _check(result, "manual_positions_mixing")
    assert mixing["status"] == "warn" and [p["symbol"] for p in mixing["positions"]] == [BTC]
    manual = _check(result, "manual_activity")
    assert manual["status"] == "info" and len(manual["orders"]) == 2 and len(manual["positions"]) == 1
    leftovers = _check(result, "robot_leftovers")
    assert leftovers["status"] == "warn" and leftovers["orders"][0]["id"] == "r1"


def test_positions_opened_in_paper_are_named():
    db = _db(signals=[("paper", 100.0), ("dry_run", 100.0)])
    check = _check(_run(_Exchange(), db), "paper_positions")
    assert check["status"] == "warn" and len(check["signal_ids"]) == 2


def test_untested_ccxt_version_warns(monkeypatch):
    monkeypatch.setattr(lp, "TESTED_CCXT_VERSION", "0.0.1")
    assert _check(_run(_Exchange()), "ccxt_version")["status"] == "warn"


def test_validation_gates_block_live(monkeypatch):
    from services import validation_gates

    monkeypatch.setattr(validation_gates.ValidationGateService, "evaluate",
                        lambda self, db, limit=None: {"blockers": ["positive_to_negative > 25%"]})
    result = _run(_Exchange())
    assert result["ready"] is False and _check(result, "validation_gates")["status"] == "fail"


def test_preflight_runs_only_on_demand():
    main = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    assert main.count("LivePreflight()") == 1
    endpoint = main.split('@app.get("/live/preflight"', 1)[1].split("\n@app.", 1)[0]
    assert "require_owner_action" in endpoint and "asyncio.to_thread" in endpoint


def test_health_page_renders_the_preflight():
    page = (Path(__file__).resolve().parents[2] / "web" / "app" / "health" / "page.tsx").read_text(encoding="utf-8")
    assert 'apiGet("/live/preflight")' in page
    for field in ("switches_to_flip", "preflight.switches", "sw.ok", "sw.current", "sw.required",
                  "check.title", "check.detail", "check.status", "preflight.ready"):
        assert field in page, field
    for status in (lp.OK, lp.INFO, lp.WARN, lp.FAIL):
        assert f"{status}:" in page.split("const PREFLIGHT_MARK", 1)[1].split("\n", 1)[0]


def _pinned_ccxt() -> str:
    line = next(l for l in (Path(__file__).resolve().parents[1] / "requirements.txt")
                .read_text(encoding="utf-8").splitlines() if l.strip().startswith("ccxt"))
    assert "==" in line, f"ccxt в requirements.txt не закреплён: {line!r}"
    return line.split("==", 1)[1].strip()


def _declared_tested_ccxt() -> str:
    # Из исходника, а не из модуля: autouse-фикстура подменяет константу
    # установленной версией, и подмена скрыла бы именно то расхождение,
    # которое здесь проверяется.
    source = (Path(__file__).resolve().parents[1] / "services" / "live_preflight.py").read_text(encoding="utf-8")
    return source.split("TESTED_CCXT_VERSION = ", 1)[1].split("\n", 1)[0].strip().strip('"')


def test_tested_ccxt_version_matches_the_pin():
    """Версия, на которой проверены формы запросов, и версия в requirements.txt —
    одно и то же. 19.09 они разъехались молча: зависимость была не закреплена,
    прод собрался на 4.5.78, константа осталась на 4.5.77, и расхождение увидел
    только preflight на проде."""
    assert _declared_tested_ccxt() == _pinned_ccxt()


def test_request_shapes_run_on_the_pinned_ccxt():
    """Тесты форм проверяют ровно ту версию, что стоит в окружении. Здесь видно,
    та ли это версия: в CI зависимости ставятся из requirements.txt, значит
    формы проверяются на закреплённой; локальное окружение может отставать."""
    import ccxt

    if ccxt.__version__ != _pinned_ccxt():
        pytest.skip(f"окружение не по requirements.txt: стоит {ccxt.__version__}, "
                    f"закреплена {_pinned_ccxt()} — формы проверит CI")
    assert _declared_tested_ccxt() == ccxt.__version__


def test_live_execution_mode_is_pinned_in_the_blueprint():
    blueprint = (Path(__file__).resolve().parents[3] / "render.yaml").read_text(encoding="utf-8")
    block = blueprint.split("key: LIVE_EXECUTION_MODE", 1)[1].split("- key:", 1)[0]
    assert "value: dry_run" in block


# ── пороги, заданные суммой (#sizing-scales-with-equity-2026-09-19) ─────────
def _sized(monkeypatch, *, margin_pct=0.13, order_cap=250.0, leverage=1):
    """Размер сделки на счёте задают три потолка сразу, а не один."""
    monkeypatch.setattr(settings, "MAX_POSITION_MARGIN_PCT", margin_pct)
    monkeypatch.setattr(settings, "LIVE_MAX_ORDER_NOTIONAL_USDT", order_cap)
    monkeypatch.setattr(settings, "LIVE_MAX_ORDER_NOTIONAL_PCT", 0.0)
    monkeypatch.setattr(settings, "FUTURES_LEVERAGE", leverage)


def test_absolute_thresholds_are_measured_against_the_trade_size(monkeypatch):
    """Сумма, откалиброванная под один счёт, на другом означает не то же самое.
    Долю от номинала видно сразу, до первой пустой ленты решений."""
    _sized(monkeypatch, margin_pct=0.5, order_cap=250.0)
    monkeypatch.setattr(settings, "ANTI_DRAIN_MIN_EDGE_AFTER_COSTS_USDT", 1.20)
    monkeypatch.setattr(settings, "ANTI_DRAIN_MIN_EDGE_AFTER_COSTS_PCT", 0.0)

    check = _check(_run(_Exchange(free=500.0)), "absolute_thresholds")
    edge = next(r for r in check["thresholds"] if r["key"] == "ANTI_DRAIN_MIN_EDGE_AFTER_COSTS_USDT")

    # Сделка = min(экспозиция 500, маржевый потолок 250, потолок ордера 250) = 250.
    assert check["notional_usdt"] == 250.0
    assert edge["share_pct"] == pytest.approx(0.48, abs=1e-3)


def test_the_trade_size_is_the_smallest_ceiling_not_the_order_cap(monkeypatch):
    """Первая версия брала за номинал сам потолок ордера и завышала размер
    сделки втрое, когда сильнее режет маржевый потолок."""
    _sized(monkeypatch, margin_pct=0.13, order_cap=250.0)

    check = _check(_run(_Exchange(free=600.0)), "absolute_thresholds")

    # 600 × 0.13 × 1 = 78 — это меньше потолка ордера, значит оно и есть размер.
    assert check["notional_usdt"] == pytest.approx(78.0, abs=1e-2)


def test_the_order_cap_is_not_compared_against_itself(monkeypatch):
    """Дефект первой версии: потолок размера сделки мерился долей от размера
    сделки, то есть от себя — всегда ровно 100% и всегда «великоват»."""
    _sized(monkeypatch, margin_pct=1.0, order_cap=250.0, leverage=5)

    check = _check(_run(_Exchange(free=300.0)), "absolute_thresholds")
    cap = next(r for r in check["thresholds"] if r["key"] == "LIVE_MAX_ORDER_NOTIONAL_USDT")

    # База у потолка — экспозиция (300 × 5 = 1500), а не номинал: 250/1500 ≈ 16.7%.
    assert cap["base"] == "exposure"
    assert cap["share_pct"] == pytest.approx(16.6667, abs=1e-3)


def test_an_order_cap_throttling_the_account_is_flagged(monkeypatch):
    """250 при экспозиции 1500 — система торгует шестой частью доступного."""
    _sized(monkeypatch, margin_pct=1.0, order_cap=250.0, leverage=5)

    check = _check(_run(_Exchange(free=300.0)), "absolute_thresholds")

    assert check["status"] == "warn"
    assert "LIVE_MAX_ORDER_NOTIONAL_USDT" in check["heavy"]
    assert check["exposure_usdt"] == 1500.0


def test_an_adaptive_threshold_is_shown_but_not_blamed(monkeypatch):
    """Гейт берёт min(сумма, 1% маржи): на малом счёте порог опускается сам, и
    претензии к сумме нет. Первая версия ругалась и на него."""
    _sized(monkeypatch, margin_pct=0.5, order_cap=250.0)
    monkeypatch.setattr(settings, "MIN_NET_PNL_TP2_USDT", 1.5)

    check = _check(_run(_Exchange(free=500.0)), "absolute_thresholds")
    tp2 = next(r for r in check["thresholds"] if r["key"] == "MIN_NET_PNL_TP2_USDT")

    assert tp2["base"] == "adaptive"
    assert tp2["effective_usdt"] == pytest.approx(1.5, abs=1e-3)  # min(1.5, 250 × 1%)
    assert "MIN_NET_PNL_TP2_USDT" not in check["heavy"]


def test_a_threshold_too_heavy_for_the_account_is_flagged(monkeypatch):
    _sized(monkeypatch, margin_pct=0.3, order_cap=90.0)
    monkeypatch.setattr(settings, "ANTI_DRAIN_MIN_EDGE_AFTER_COSTS_USDT", 1.20)
    monkeypatch.setattr(settings, "ANTI_DRAIN_MIN_EDGE_AFTER_COSTS_PCT", 0.0)

    check = _check(_run(_Exchange(free=300.0)), "absolute_thresholds")

    # 1.20 на номинал 90 — это 1.33%, то есть порог управляет отбором.
    assert check["status"] == "warn"
    assert "ANTI_DRAIN_MIN_EDGE_AFTER_COSTS_USDT" in check["heavy"]


def test_a_threshold_already_replaced_by_a_share_is_not_a_complaint(monkeypatch):
    """Если доля включена, сумма просто не работает — претензии к ней нет."""
    _sized(monkeypatch, margin_pct=0.3, order_cap=90.0)
    monkeypatch.setattr(settings, "ANTI_DRAIN_MIN_EDGE_AFTER_COSTS_USDT", 1.20)
    monkeypatch.setattr(settings, "ANTI_DRAIN_MIN_EDGE_AFTER_COSTS_PCT", 0.48)

    check = _check(_run(_Exchange(free=300.0)), "absolute_thresholds")
    edge = next(r for r in check["thresholds"] if r["key"] == "ANTI_DRAIN_MIN_EDGE_AFTER_COSTS_USDT")

    assert edge["scaling_on"] is True
    assert "ANTI_DRAIN_MIN_EDGE_AFTER_COSTS_USDT" not in check["heavy"]


def test_an_empty_account_says_so_instead_of_reporting_percentages(monkeypatch):
    """На счёте 0.01 USDT сделка не состоится ни при каких порогах, и доли от
    неё — шум: на них владелец пошёл бы чинить исправное."""
    _sized(monkeypatch, margin_pct=0.13, order_cap=250.0)

    check = _check(_run(_Exchange(free=0.01)), "absolute_thresholds")

    assert check["status"] == "warn"
    assert "не состоится" in check["detail"]
    assert check["thresholds"] == []


def test_unknown_capital_says_so_instead_of_guessing():
    check = _check(_run(_Exchange(balance_error=PermissionError("50113"))), "absolute_thresholds")

    assert check["status"] == "info" and "капитал неизвестен" in check["detail"]


# ── риск против дневного лимита (#any-deposit-any-leverage-2026-09-20) ──────
def test_the_daily_limit_can_stop_the_robot_after_two_stops(monkeypatch):
    """Обе настройки — доли капитала, и по отдельности выглядят разумными.
    Вместе они задают, сколько стопов подряд робот переживёт, и это число
    нигде не показывалось."""
    monkeypatch.setattr(settings, "RISK_PER_TRADE_PCT", 1.5)
    monkeypatch.setattr(settings, "MAX_DAILY_LOSS_PCT", 3.0)

    check = _check(_run(_Exchange(free=300.0)), "risk_vs_daily_limit")

    assert check["status"] == "warn"
    assert check["stops_before_daily_limit"] == 2.0


def test_a_workable_pair_is_not_a_complaint(monkeypatch):
    monkeypatch.setattr(settings, "RISK_PER_TRADE_PCT", 0.4)
    monkeypatch.setattr(settings, "MAX_DAILY_LOSS_PCT", 3.0)

    check = _check(_run(_Exchange(free=300.0)), "risk_vs_daily_limit")

    assert check["status"] == "ok"
    assert check["stops_before_daily_limit"] == 7.5


def test_the_effective_leverage_is_reported_not_the_configured_one(monkeypatch):
    """Заданное плечо 10× при занятых 13% экспозиции — это 1.3×, и ощущение
    масштаба ложное: размер сделки режет риск, а не плечо."""
    monkeypatch.setattr(settings, "RISK_PER_TRADE_PCT", 0.4)
    monkeypatch.setattr(settings, "MAX_DAILY_LOSS_PCT", 3.0)
    monkeypatch.setattr(settings, "FUTURES_LEVERAGE", 10)
    monkeypatch.setattr(settings, "LIVE_MAX_LEVERAGE", 10.0)

    check = _check(_run(_Exchange(free=300.0)), "risk_vs_daily_limit")

    assert check["exposure_usdt"] == 3000.0
    assert check["effective_leverage"] < 2.0, check


def test_the_anti_drain_limit_never_silently_outranks_the_general_one():
    """(#risk-one-percent-2026-09-20) Дневных лимита ДВА, и anti-drain строже.
    Пока он был 2% против общих 3%, поднятие общего не меняло ничего: робот
    вставал по anti-drain. Настройка выглядит поднятой, поведение прежнее."""
    from core.config import Settings

    general = Settings.model_fields["MAX_DAILY_LOSS_PCT"].default
    anti_drain = Settings.model_fields["ANTI_DRAIN_MAX_DAILY_LOSS_PCT"].default

    assert anti_drain >= general, (
        f"anti-drain {anti_drain}% сработает раньше общего {general}% и заменит его собой")


def test_the_risk_pair_is_pinned_in_the_blueprint():
    """Обе настройки торговые — значит живут и в config.py, и в render.yaml."""
    blueprint = (Path(__file__).resolve().parents[3] / "render.yaml").read_text(encoding="utf-8")
    from core.config import Settings

    for key in ("RISK_PER_TRADE_PCT", "MAX_DAILY_LOSS_PCT", "ANTI_DRAIN_MAX_DAILY_LOSS_PCT"):
        block = blueprint.split(f"key: {key}", 1)[1].split("- key:", 1)[0]
        default = float(Settings.model_fields[key].default)
        written = float(block.split('value: "', 1)[1].split('"', 1)[0])
        assert written == default, f"{key}: блупринт {written}, код {default}"


def test_five_simultaneous_stops_do_not_exceed_the_daily_limit():
    """Портфельный потолок и дневной лимит должны сходиться: пять позиций по
    риску не могут стоить больше, чем робот готов потерять за день."""
    from core.config import Settings

    risk = float(Settings.model_fields["RISK_PER_TRADE_PCT"].default)
    positions = int(Settings.model_fields["ANTI_DRAIN_MAX_OPEN_POSITIONS"].default)
    daily = float(Settings.model_fields["MAX_DAILY_LOSS_PCT"].default)

    assert risk * positions <= daily, (
        f"{positions} стопов подряд = {risk * positions}% против лимита {daily}%")
