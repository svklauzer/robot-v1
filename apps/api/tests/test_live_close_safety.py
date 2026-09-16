"""Закрытие в live: сначала биржа, потом учёт (#live-close-safety-2026-09-16).

Аудит 16.09 перед live. Позиция в учёте помечалась закрытой ДО ордера, а отказ
биржи уходил только в лог `live_order_not_filled`: на бирже оставалась позиция
без стопа и без сопровождения — для системы сделка закрыта. Частичное закрытие
округлялось по точности спота, а биржа закрывала целыми лотами — финальное
закрытие оставляло хвост. Здесь проверяется новое поведение, а бумажный поток
(off/dry_run) — что он не изменился.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from models.bot import Bot
from models.position import Position
from services.execution_engine import ExecutionEngine

ROUTING = {"market_type": "swap", "exchange_symbol": "XRP/USDT:USDT", "base_symbol": "XRP/USDT",
           "side": "short", "leverage": 1, "margin_mode": "isolated"}


class _Query:
    def __init__(self, db, model):
        self.db, self.model = db, model

    def filter(self, *_a, **_k):
        return self

    def first(self):
        if self.model is Position:
            p = self.db.position
            return p if p is not None and p.status == "open" else None
        if self.model is Bot:
            return self.db.bot
        return None


class _DB:
    def __init__(self, position):
        self.position = position
        self.bot = SimpleNamespace(id=1)
        self.added = []

    def query(self, model, *_a, **_k):
        return _Query(self, model)

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        pass


class _Costs:
    def estimate(self, *, exit_price, qty, entry_price, side, **_k):
        gross = (entry_price - exit_price) * qty if side == "short" else (exit_price - entry_price) * qty
        return SimpleNamespace(net_pnl=round(gross, 6), net_pnl_pct=0.0, total_cost=0.1)


@pytest.fixture
def world(monkeypatch):
    position = SimpleNamespace(
        signal_id=7, bot_id=1, symbol="XRP/USDT", side="short", qty=176.0,
        entry_price=1.42, mark_price=1.42, unrealized_pnl=0.0, status="open",
        opened_at=datetime.now(timezone.utc), closed_at=None,
    )
    signal = SimpleNamespace(id=7, bot_id=1, symbol="XRP/USDT", side="short",
                             plan_json={"routing": dict(ROUTING), "execution": {"mode": "live"}})
    alerts, kills, sent = [], [], []

    async def owner_alert(title, body):
        alerts.append(title)

    engine = ExecutionEngine.__new__(ExecutionEngine)
    engine.db = _DB(position)
    engine.telegram = SimpleNamespace(owner_alert=owner_alert)
    engine.cost_engine = _Costs()
    engine.client = SimpleNamespace()

    state = {"live": True, "exchange_qty": None, "reply": {"mode": "live", "ok": True, "status": "closed",
                                                          "filled_qty": 176.0, "avg_price": 1.40}}

    def submit(self, side, symbol, qty, ref_price, reduce_only, purpose, route=None):
        sent.append({"side": side, "qty": qty, "reduce_only": reduce_only, "purpose": purpose,
                     "market": route.market_type if route else None})
        reply = state["reply"]
        return dict(reply) if isinstance(reply, dict) else reply

    monkeypatch.setattr(ExecutionEngine, "_live_mode", staticmethod(lambda: state["live"]))
    monkeypatch.setattr(ExecutionEngine, "_exchange_position_base",
                        staticmethod(lambda route, pos: state["exchange_qty"]))
    monkeypatch.setattr(ExecutionEngine, "_submit_live", submit)
    # Лот XRP-своп на OKX — 0.01 контракта = 1 XRP.
    monkeypatch.setattr(ExecutionEngine, "_quantize_qty",
                        lambda self, route, qty: (float(int(qty)), {}))

    from services import live_safety

    monkeypatch.setattr(live_safety.LiveSafetyService, "set_kill_switch",
                        lambda self, db, bot, enabled, reason: kills.append(reason))
    return SimpleNamespace(engine=engine, position=position, signal=signal, state=state,
                           alerts=alerts, kills=kills, sent=sent)


# ── полное закрытие ──────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_a_refused_close_keeps_the_trade_open(world):
    world.state["reply"] = {"mode": "live", "ok": False, "status": "error", "error": "51008 insufficient"}
    out = await world.engine.close_paper_position(world.signal, exit_price=1.40, reason="stop_loss")

    assert out["status"] == "live_close_failed"
    assert world.position.status == "open", "позиция закрыта в учёте вопреки отказу биржи"
    assert world.engine.db.added == [], "ордер закрытия записан, хотя биржа ничего не закрыла"
    assert world.signal.plan_json["live_close_failures"]["count"] == 1
    assert len(world.kills) == 1 and len(world.alerts) == 1


@pytest.mark.anyio
async def test_repeated_refusals_do_not_spam(world):
    world.state["reply"] = {"mode": "live", "ok": False, "status": "error", "error": "timeout"}
    for _ in range(9):
        await world.engine.close_paper_position(world.signal, exit_price=1.40, reason="stop_loss")
    assert world.signal.plan_json["live_close_failures"]["count"] == 9
    assert len(world.kills) == 1, "kill switch дёргается на каждую попытку"
    assert len(world.alerts) == 1
    await world.engine.close_paper_position(world.signal, exit_price=1.40, reason="stop_loss")
    assert len(world.alerts) == 2, "десятая попытка обязана напомнить владельцу"


@pytest.mark.anyio
async def test_submit_exception_in_live_is_a_refusal(world):
    world.state["reply"] = None
    out = await world.engine.close_paper_position(world.signal, exit_price=1.40, reason="tz_kama")
    assert out["status"] == "live_close_failed"
    assert world.position.status == "open"


@pytest.mark.anyio
async def test_a_partial_fill_shrinks_the_book_by_what_was_closed(world):
    world.state["reply"] = {"mode": "live", "ok": False, "status": "canceled", "filled_qty": 100.0}
    out = await world.engine.close_paper_position(world.signal, exit_price=1.40, reason="stop_loss")
    assert out["status"] == "live_close_failed"
    assert world.position.qty == pytest.approx(76.0)
    assert world.position.status == "open"


@pytest.mark.anyio
async def test_the_close_never_takes_more_than_the_robot_holds(world):
    """(#manual-orders-2026-09-16) Учёт 176, на бирже 180: лишние 4 — ручная
    позиция владельца в том же режиме маржи. Закрывается доля робота."""
    world.state["exchange_qty"] = 180.0
    world.state["reply"] = {"mode": "live", "ok": True, "status": "closed", "filled_qty": 176.0, "avg_price": 1.395}
    out = await world.engine.close_paper_position(world.signal, exit_price=1.40, reason="tz_kama")

    assert world.sent[-1]["qty"] == pytest.approx(176.0) and world.sent[-1]["reduce_only"] is True
    assert out["status"] == "closed" and world.position.status == "closed"
    assert out["exit_price"] == pytest.approx(1.395), "цена закрытия — филл биржи, а не расчётная"


@pytest.mark.anyio
async def test_the_close_sends_what_is_left_when_the_exchange_holds_less(world):
    """Учёт 176, на бирже 150 (часть закрыли вне робота) — закрыть 150, не больше."""
    world.state["exchange_qty"] = 150.0
    world.state["reply"] = {"mode": "live", "ok": True, "status": "closed", "filled_qty": 150.0, "avg_price": 1.395}
    await world.engine.close_paper_position(world.signal, exit_price=1.40, reason="tz_kama")

    assert world.sent[-1]["qty"] == pytest.approx(150.0)


@pytest.mark.anyio
@pytest.mark.parametrize("opened_as", ["paper", "dry_run", None])
async def test_a_position_not_opened_live_is_closed_in_the_book_only(world, opened_as):
    """Позиция из paper/dry_run, дожившая до включения live: на бирже её нет, а
    reduce-only закрытие по её объёму съело бы ручную позицию на том же символе."""
    if opened_as is None:
        world.signal.plan_json.pop("execution")
    else:
        world.signal.plan_json["execution"] = {"mode": opened_as}
    world.state["exchange_qty"] = 500.0                   # ручная позиция владельца

    out = await world.engine.close_paper_position(world.signal, exit_price=1.40, reason="stop_loss")
    await world.engine.close_paper_position(world.signal, exit_price=1.40, reason="stop_loss")

    assert out["status"] == "closed" and world.position.status == "closed"
    assert world.sent == [], "ордер на биржу по позиции, которой робот там не открывал"
    assert world.alerts == ["LIVE: ПОЗИЦИЯ НЕ С БИРЖИ — ЗАКРЫТА ТОЛЬКО В УЧЁТЕ"]


@pytest.mark.anyio
async def test_partial_close_of_a_position_not_opened_live_sends_nothing(world):
    world.signal.plan_json["execution"] = {"mode": "dry_run"}
    out = await world.engine.partial_close_paper_position(world.signal, exit_price=1.40, share=0.5)

    assert out["status"] == "partial_closed" and world.sent == []
    assert world.position.qty == pytest.approx(88.0)


@pytest.mark.anyio
async def test_unknown_exchange_size_falls_back_to_the_book(world):
    world.state["exchange_qty"] = None
    await world.engine.close_paper_position(world.signal, exit_price=1.40, reason="tz_kama")
    assert world.sent[-1]["qty"] == pytest.approx(176.0)


@pytest.mark.anyio
async def test_an_already_flat_exchange_closes_the_book_without_an_order(world):
    world.state["exchange_qty"] = 0.0
    out = await world.engine.close_paper_position(world.signal, exit_price=1.40, reason="stop_loss")
    assert out["status"] == "closed"
    assert world.sent == [], "на пустую биржу ушёл ордер"
    assert world.alerts == ["LIVE: ПОЗИЦИИ НА БИРЖЕ УЖЕ НЕТ"]


@pytest.mark.anyio
async def test_paper_and_dry_run_flow_is_unchanged(world):
    """off/dry_run: учёт закрывается, ядро только логирует — даже если «отказ»."""
    world.state["live"] = False
    world.state["reply"] = {"mode": "dry_run", "ok": False, "status": "dry_run"}
    out = await world.engine.close_paper_position(world.signal, exit_price=1.40, reason="stop_loss")
    assert out["status"] == "closed" and world.position.status == "closed"
    assert world.sent and world.kills == [] and world.alerts == []


# ── частичное закрытие ───────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_a_refused_partial_leaves_the_position_whole(world):
    world.state["reply"] = {"mode": "live", "ok": False, "status": "error", "error": "timeout"}
    out = await world.engine.partial_close_paper_position(world.signal, exit_price=1.38, share=0.5,
                                                          reason="tp2_partial")
    assert out["status"] == "live_close_failed"
    assert world.position.qty == pytest.approx(176.0)
    assert world.engine.db.added == []


@pytest.mark.anyio
async def test_a_partially_filled_partial_books_only_the_filled_part(world):
    world.state["reply"] = {"mode": "live", "ok": False, "status": "canceled", "filled_qty": 50.0, "avg_price": 1.381}
    out = await world.engine.partial_close_paper_position(world.signal, exit_price=1.38, share=0.5,
                                                          reason="tp2_partial")
    assert out["status"] == "partial_closed"
    assert out["closed_qty"] == pytest.approx(50.0)
    assert world.position.qty == pytest.approx(126.0)
    assert out["exit_price"] == pytest.approx(1.381)


@pytest.mark.anyio
async def test_the_partial_share_is_rounded_to_the_exchange_lot(world):
    """176 × 0.5 = 88 — ровно; 175 × 0.5 = 87.5 → лот 1 XRP → 87, и бирже, и учёту."""
    world.position.qty = 175.0
    world.state["reply"] = {"mode": "live", "ok": True, "status": "closed", "filled_qty": 87.0, "avg_price": 1.38}
    out = await world.engine.partial_close_paper_position(world.signal, exit_price=1.38, share=0.5,
                                                          reason="tp2_partial")
    assert world.sent[-1]["qty"] == pytest.approx(87.0)
    assert out["closed_qty"] == pytest.approx(87.0) and world.position.qty == pytest.approx(88.0)


# ── открытие ─────────────────────────────────────────────────────────────────

def _open_signal():
    return SimpleNamespace(
        id=9, bot_id=1, symbol="XRP/USDT", side="short", stop_price=1.44,
        tp_json={"tp1": 1.40, "tp2": 1.36}, plan_json={"routing": dict(ROUTING)},
        qty=176.0, required_margin=250.0, net_pnl_tp1=3.0, net_pnl_tp2=10.0,
        net_pnl_stop=-4.0, net_rr_tp1=0.7, net_rr_tp2=2.5,
    )


@pytest.mark.anyio
async def test_a_live_exception_on_open_does_not_book_a_phantom_position(world):
    world.engine.db.position = None
    world.state["reply"] = None
    out = await world.engine.open_paper_position(bot=SimpleNamespace(id=1), signal=_open_signal(),
                                                 entry_price=1.42)
    assert out["status"] == "live_rejected"
    assert world.engine.db.added == []


@pytest.mark.anyio
async def test_a_partially_filled_open_is_booked_not_abandoned(world):
    world.engine.db.position = None
    world.state["reply"] = {"mode": "live", "ok": False, "status": "canceled", "filled_qty": 120.0, "avg_price": 1.419}
    out = await world.engine.open_paper_position(bot=SimpleNamespace(id=1), signal=_open_signal(),
                                                 entry_price=1.42)
    assert out["status"] == "opened"
    assert out["position"].qty == pytest.approx(120.0)
    assert out["position"].entry_price == pytest.approx(1.419)


@pytest.mark.anyio
@pytest.mark.parametrize("reply, opened_as", [
    ({"mode": "live", "ok": True, "status": "closed", "filled_qty": 176.0, "avg_price": 1.42,
      "exchange_order_id": "ex1"}, "live"),
    ({"mode": "dry_run", "ok": True, "status": "dry_run", "filled_qty": 176.0, "avg_price": 1.42}, "dry_run"),
    (None, "paper"),
])
async def test_the_open_records_where_the_position_lives(world, reply, opened_as):
    """(#manual-orders-2026-09-16) По этой отметке live закрывает и страхует на
    бирже только то, что робот сам там открыл."""
    world.engine.db.position = None
    world.state["live"] = opened_as == "live"
    world.state["reply"] = reply
    signal = _open_signal()

    out = await world.engine.open_paper_position(bot=SimpleNamespace(id=1), signal=signal, entry_price=1.42)

    assert out["status"] == "opened"
    assert signal.plan_json["execution"]["mode"] == opened_as
    assert signal.plan_json["routing"] == ROUTING, "отметка затёрла маршрут сделки"


# ── жизненный цикл: сделка не закрывается при отказе биржи ───────────────────

@pytest.mark.anyio
async def test_lifecycle_keeps_the_signal_open_when_the_exchange_refuses(monkeypatch):
    from services import signal_lifecycle

    class _Engine:
        def __init__(self, *_a, **_k):
            pass

        async def close_paper_position(self, **_k):
            return {"status": "live_close_failed"}

    monkeypatch.setattr(signal_lifecycle, "ExecutionEngine", _Engine)
    manager = signal_lifecycle.SignalLifecycleManager.__new__(signal_lifecycle.SignalLifecycleManager)
    signal = SimpleNamespace(id=1, status="opened", exchange="okx", stop_price=1.44, side="short",
                             plan_json={}, closed_at=None)
    db = SimpleNamespace(flush=lambda: None)

    await manager._close_signal(db, signal, exit_price=1.40, fallback_result_pct=0.5, reason="tz_kama")
    assert signal.status == "opened"
    assert signal.closed_at is None


# ── размер позиции на бирже ──────────────────────────────────────────────────

def _executor(positions=None, raises=False):
    from services.live_executor import LiveExecutor

    def fetch_positions():
        if raises:
            raise RuntimeError("exchange down")
        return positions

    ex = LiveExecutor.__new__(LiveExecutor)
    ex.client = SimpleNamespace(fetch_positions=fetch_positions, contract_size=lambda s: 100.0)
    return ex


def test_exchange_position_is_read_in_coins_for_the_right_side():
    ex = _executor([
        {"symbol": "XRP/USDT:USDT", "side": "short", "contracts": 1.8, "contractSize": 100.0},
        {"symbol": "XRP/USDT:USDT", "side": "long", "contracts": 5.0, "contractSize": 100.0},
        {"symbol": "BTC/USDT:USDT", "side": "short", "contracts": 0.3, "contractSize": 0.01},
    ])
    assert ex.exchange_position_base("XRP/USDT:USDT", "short", "swap") == pytest.approx(180.0)


def test_a_failed_fetch_is_unknown_not_flat():
    assert _executor(raises=True).exchange_position_base("XRP/USDT:USDT", "short", "swap") is None


def test_a_successful_empty_fetch_is_flat():
    assert _executor([]).exchange_position_base("XRP/USDT:USDT", "short", "swap") == 0.0


def test_spot_has_no_exchange_position():
    assert _executor([]).exchange_position_base("XRP/USDT", "long", "spot") is None
