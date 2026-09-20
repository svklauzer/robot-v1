"""Объём ордеров OKX: бумага и live — один размер, не больше плана
(#okx-lot-step-2026-09-16, #okx-live-double-conversion-2026-09-16).

16.09, накануне выхода в live, реальный код live-ордера прогнан на реальных
спецификациях OKX. Две ошибки:

1. `LiveExecutor._to_exchange_amount` передавал клиенту КОНТРАКТЫ, а клиент
   ждёт МОНЕТЫ и делил на размер контракта ещё раз. План 100 XRP уходил как
   100 контрактов = 10 000 XRP (~14 000 USDT), 3000 DOGE — как 1000 контрактов
   = 1 000 000 DOGE (~81 000 USDT). Кэп нотионала проверяется до перевода.
2. `OKXClient.amount_to_precision` округлял до целого контракта и поднимал
   ниже-одного до одного. Шаг лота OKX — 0.01 контракта, 1 контракт BTC =
   0.01 BTC ≈ 760 USDT: BTC-сделка при кэпе 250 открывалась на 760.

Спецификации ниже сняты с OKX 16.09 (ctVal / lotSz / minSz).
"""
from __future__ import annotations

import pytest

from core.config import settings
from services.live_executor import LiveExecutor
from services.okx_client import OKXClient

#            символ              ctVal   lotSz  minSz  цена
OKX_SPECS = {
    "BTC/USDT:USDT":  (0.01,   0.01, 0.01, 76000.0),
    "ETH/USDT:USDT":  (0.1,    0.01, 0.01, 2393.0),
    "SOL/USDT:USDT":  (1.0,    0.01, 0.01, 97.0),
    "XRP/USDT:USDT":  (100.0,  0.01, 0.01, 1.418),
    "DOGE/USDT:USDT": (1000.0, 0.01, 0.01, 0.0813),
    "LINK/USDT:USDT": (1.0,    0.1,  0.1,  10.9),
    "LTC/USDT:USDT":  (1.0,    0.1,  0.1,  52.2),
    "HYPE/USDT:USDT": (0.1,    1.0,  1.0,  77.3),
    "CHIP/USDT:USDT": (100.0,  1.0,  1.0,  0.0379),
    "PI/USDT:USDT":   (1.0,    1.0,  1.0,  0.0956),
}


def _market(ct_val, lot, min_sz):
    # Форма ccxt.okx: сырые lotSz/minSz в info + унифицированные precision/limits.
    return {
        "contract": True, "contractSize": ct_val,
        "precision": {"amount": lot}, "limits": {"amount": {"min": min_sz}},
        "info": {"ctVal": str(ct_val), "lotSz": str(lot), "minSz": str(min_sz)},
    }


class _FakeExchange:
    def __init__(self):
        self.markets = {sym: _market(*spec[:3]) for sym, spec in OKX_SPECS.items()}

    def market(self, symbol):
        return self.markets[symbol]

    def amount_to_precision(self, symbol, amount):
        return round(float(amount), 8)

    def price_to_precision(self, symbol, price):
        return float(price)


@pytest.fixture
def okx():
    OKXClient._cached_markets = {}
    OKXClient._markets_loaded = False
    client = OKXClient.__new__(OKXClient)
    client.exchange = _FakeExchange()
    client.load_markets = lambda *a, **k: client.exchange.markets
    yield client
    OKXClient._cached_markets = {}
    OKXClient._markets_loaded = False


@pytest.fixture
def executor(okx):
    ex = LiveExecutor.__new__(LiveExecutor)
    ex.client = okx
    return ex


# ── клиент: шаг лота, минимум, только вниз ──────────────────────────────────

def test_btc_under_the_cap_is_not_inflated_to_a_whole_contract(okx):
    """250 USDT при 76 000 = 0.0033 BTC = 0.33 контракта — шаг 0.01 это позволяет."""
    assert okx.amount_to_precision("BTC/USDT:USDT", 250 / 76000) == pytest.approx(0.0032)


def test_the_real_lot_step_is_used(okx):
    assert okx.amount_to_precision("LTC/USDT:USDT", 4.37) == pytest.approx(4.3)
    assert okx.amount_to_precision("HYPE/USDT:USDT", 3.27) == pytest.approx(3.2)
    assert okx.amount_to_precision("CHIP/USDT:USDT", 6550) == pytest.approx(6500)
    assert okx.amount_to_precision("XRP/USDT:USDT", 176.3) == pytest.approx(176.0)


def test_below_the_minimum_lot_is_zero(okx):
    assert okx.amount_to_precision("BTC/USDT:USDT", 0.00005) == 0.0     # 0.005 контракта
    assert okx.amount_to_precision("CHIP/USDT:USDT", 99.0) == 0.0       # 0.99 контракта


@pytest.mark.parametrize("symbol", sorted(OKX_SPECS))
def test_rounding_never_exceeds_the_request(okx, symbol):
    ct_val, lot, _, price = OKX_SPECS[symbol]
    for notional in (7.3, 49.9, 131.7, 249.99, 777.7):
        amount = notional / price
        got = okx.amount_to_precision(symbol, amount)
        assert got <= amount + 1e-12
        assert amount - got < lot * ct_val + 1e-9, "недобор больше одного шага лота"


# ── live: один перевод в контракты ──────────────────────────────────────────

@pytest.mark.parametrize("symbol, planned_base, contracts", [
    ("BTC/USDT:USDT", 0.01, 1.0),
    ("ETH/USDT:USDT", 0.1, 1.0),
    ("SOL/USDT:USDT", 2.0, 2.0),
    ("XRP/USDT:USDT", 100.0, 1.0),        # было 100 контрактов = 10 000 XRP
    ("DOGE/USDT:USDT", 3000.0, 3.0),      # было 1000 контрактов = 1 000 000 DOGE
    ("LINK/USDT:USDT", 22.0, 22.0),
    ("LTC/USDT:USDT", 4.0, 4.0),
    ("HYPE/USDT:USDT", 3.2, 32.0),
    ("CHIP/USDT:USDT", 6500.0, 65.0),     # было 100 контрактов = 10 000 CHIP
    ("PI/USDT:USDT", 2613.0, 2613.0),
])
def test_live_sends_exactly_the_planned_size(executor, symbol, planned_base, contracts):
    """Позиции 14–16.09: что ушло бы на биржу, должно равняться плану."""
    sent, meta = executor._to_exchange_amount(symbol, planned_base, "swap")
    assert sent == pytest.approx(contracts)
    assert sent * meta["contract_size"] == pytest.approx(planned_base)


@pytest.mark.parametrize("symbol", sorted(OKX_SPECS))
def test_live_never_sends_more_than_planned(executor, symbol):
    ct_val, _, _, price = OKX_SPECS[symbol]
    for notional in (12.0, 99.9, 249.0, 1234.5):
        amount = notional / price
        sent, meta = executor._to_exchange_amount(symbol, amount, "swap")
        assert sent * ct_val <= amount + 1e-9, f"{symbol}: {sent} контрактов > плана {amount}"


def test_a_client_that_rounds_up_is_not_trusted(executor):
    """Предохранитель: если клиент вернул больше плана, в дело идёт округление
    вниз до целого контракта, а не его ответ."""
    executor.client.amount_to_precision = lambda _s, a: float(a) * 3
    sent, _ = executor._to_exchange_amount("XRP/USDT:USDT", 250.0, "swap")
    assert sent == 2.0


def test_the_cap_holds_on_what_is_actually_sent(executor, monkeypatch):
    """Кэп проверяется на плановом объёме — значит, отправленный обязан с ним
    совпадать. Раньше XRP проходил кэп как 142 USDT и уходил как 14 180."""
    monkeypatch.setattr(LiveExecutor, "effective_mode", classmethod(lambda cls: "live"))
    monkeypatch.setattr(settings, "LIVE_MAX_ORDER_NOTIONAL_PCT", 0.0)
    monkeypatch.setattr(settings, "LIVE_MAX_ORDER_NOTIONAL_USDT", 250.0)
    captured = {}

    def create_order_once(_symbol, _type, _side, amount, *_a, **_k):
        captured["contracts"] = amount
        raise RuntimeError("дошли до отправки — достаточно")

    executor.client.create_order_once = create_order_once
    executor._leverage_set = set()
    monkeypatch.setattr(executor, "_ensure_leverage", lambda *_a, **_k: None, raising=False)
    monkeypatch.setattr(executor, "_find_by_client_id", lambda *_a, **_k: None, raising=False)

    executor.place_market("XRP/USDT:USDT", "sell", 100.0, market_type="swap",
                          reference_price=1.418, purpose="test")
    assert captured["contracts"] * 100.0 * 1.418 <= 250.0


# ── план сделки: BTC больше не втрое крупнее ────────────────────────────────

class _CostEngine:
    def estimate(self, symbol, market_type, side, entry_price, exit_price, qty, liquidity, leverage):
        from types import SimpleNamespace
        gross = (entry_price - exit_price) * qty if str(side).lower() in ("short", "sell") \
            else (exit_price - entry_price) * qty
        return SimpleNamespace(net_pnl=gross)


def test_btc_swap_plan_respects_the_notional_cap(okx, monkeypatch):
    from services.trade_plan import TradePlanBuilder

    monkeypatch.setattr(settings, "ENABLE_FUTURES", True)
    monkeypatch.setattr(settings, "LIVE_MAX_ORDER_NOTIONAL_PCT", 0.0)
    monkeypatch.setattr(settings, "LIVE_MAX_ORDER_NOTIONAL_USDT", 250.0)
    builder = TradePlanBuilder.__new__(TradePlanBuilder)
    builder.htx = okx
    builder.cost_engine = _CostEngine()

    plan = builder.build_plan(
        symbol="BTC/USDT", side="short", entry_price=76000.0, stop_price=76760.0,
        tp1=75088.0, tp2=74480.0, balance_usdt=3000.0, risk_pct=0.4,
    )
    assert plan.is_valid, plan.reject_reason
    assert plan.qty * 76000.0 <= 250.0 + 1e-6, f"нотионал {plan.qty * 76000.0:.1f} > кэп 250"
    assert plan.qty == pytest.approx(0.0032)


def test_contract_limits_are_reported_in_coins(okx):
    """Минимум OKX для BTC — 0.01 контракта = 0.0001 BTC, а не «0.01 BTC»."""
    limits = okx.market_limits("BTC/USDT:USDT")
    assert limits["amount_unit"] == "base"
    assert limits["min_amount"] == pytest.approx(0.0001)
    assert okx.market_limits("CHIP/USDT:USDT")["min_amount"] == pytest.approx(100.0)
