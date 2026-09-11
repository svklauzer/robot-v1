"""OKX в фандинг-арбитраже, шаг 1 — наблюдение (#okx-funding-2026-09-12).

Контуры выключены 04.09 по экономике, и с тех пор ставки не копились: журнал
писал только скан арбитража, а он ходит лишь при включённом арбитраже. Шаг 1 —
наблюдать HTX и OKX раз в час, только на чтение, с меткой биржи в журнале, и
считать по этим наблюдениям экономику: внутрибиржевой хедж на каждой бирже и
межбиржевой HTX↔OKX.
"""
from __future__ import annotations

import inspect

import pytest

from core.config import settings
from services import funding_observer, funding_rate_history
from services.funding_venue_economics import build


@pytest.fixture(autouse=True)
def journal(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "FUNDING_RATE_LOG_PATH", str(tmp_path / "rates.jsonl"),
                        raising=False)


NOW = 1_800_000_000.0


def _rec(venue, rate, hour, symbol="BTC/USDT", basis=0.02):
    funding_rate_history.record(symbol, rate_pct=rate, basis_pct=basis,
                                ts=NOW - hour * 3600, venue=venue)


# ── журнал: биржи не смешиваются ────────────────────────────────────────────

def test_venues_do_not_mix_in_the_journal(monkeypatch):
    monkeypatch.setattr("time.time", lambda: NOW)
    for h in range(3):
        _rec("htx", 0.01, h)
        _rec("okx", 0.03, h)

    assert funding_rate_history.stability("BTC/USDT", 24, venue="okx")["mean_rate_pct"] == 0.03
    assert funding_rate_history.stability("BTC/USDT", 24, venue="htx")["mean_rate_pct"] == 0.01


def test_old_rows_without_a_venue_are_htx(tmp_path, monkeypatch):
    monkeypatch.setattr("time.time", lambda: NOW)
    path = tmp_path / "rates.jsonl"
    path.write_text('{"ts": %s, "s": "BTC/USDT", "r": 0.02, "b": 0.0}\n' % (NOW - 60),
                    encoding="utf-8")

    assert funding_rate_history.stability("BTC/USDT", 24)["observations"] == 1
    assert funding_rate_history.stability("BTC/USDT", 24, venue="okx")["observations"] == 0


# ── наблюдение ──────────────────────────────────────────────────────────────

class _Client:
    def __init__(self, rate, fail=False):
        self.rate, self.fail = rate, fail

    def fetch_funding_rate(self, symbol):
        if self.fail:
            raise RuntimeError("venue down")
        return {"fundingRate": self.rate}

    def fetch_mark_price(self, symbol):
        return 100.0


def test_one_pass_records_every_venue_and_symbol(monkeypatch):
    clients = {"htx": _Client(0.0001), "okx": _Client(0.0003)}
    monkeypatch.setattr("services.exchange_factory.get_exchange_client",
                        lambda venue=None: clients[venue])
    monkeypatch.setattr("services.funding_arbitrage.HTXClient", lambda: clients["htx"])

    out = funding_observer.observe_once(venues=["htx", "okx"], symbols=["BTC/USDT", "ETH/USDT"])

    assert out["recorded"] == {"htx": 2, "okx": 2}
    assert funding_rate_history.stability("ETH/USDT", 24, venue="okx")["mean_rate_pct"] == 0.03


def test_a_venue_that_fails_does_not_stop_the_other(monkeypatch):
    clients = {"htx": _Client(0.0001), "okx": _Client(0.0003, fail=True)}
    monkeypatch.setattr("services.exchange_factory.get_exchange_client",
                        lambda venue=None: clients[venue])
    monkeypatch.setattr("services.funding_arbitrage.HTXClient", lambda: clients["htx"])

    out = funding_observer.observe_once(venues=["htx", "okx"], symbols=["BTC/USDT"])

    assert out["recorded"] == {"htx": 1, "okx": 0}
    assert out["errors"][0]["venue"] == "okx"


def test_observation_is_on_while_the_arb_is_off():
    assert settings.FUNDING_OBSERVE_ENABLED is True
    assert funding_observer.observe_venues() == ["htx", "okx"]
    assert settings.ENABLE_FUNDING_ARB is False


def test_the_loop_is_started_with_the_app():
    import main

    src = inspect.getsource(main)
    assert "asyncio.create_task(background_funding_observe_loop())" in src
    loop = inspect.getsource(main.background_funding_observe_loop)
    assert "asyncio.to_thread(observe_once)" in loop


# ── экономика ───────────────────────────────────────────────────────────────

_FEES = {("htx", "spot"): 0.002, ("htx", "swap"): 0.0005,
         ("okx", "spot"): 0.001, ("okx", "swap"): 0.0005}


def _fee(venue, symbol, market_type):
    return _FEES[(venue, market_type)], "test"


def test_intra_breakeven_is_shorter_where_spot_is_cheaper(monkeypatch):
    """Ставка 0.01%/период на обеих. Круг HTX (0.2+0.05)×2 = 0.5% — 50
    периодов; OKX (0.1+0.05)×2 = 0.3% — 30 периодов."""
    monkeypatch.setattr("time.time", lambda: NOW)
    for h in range(8):
        _rec("htx", 0.01, h)
        _rec("okx", 0.01, h)

    out = build(window_hours=24, hold_periods=10, venues=["htx", "okx"],
                symbols=["BTC/USDT"], fee=_fee)
    intra = {r["venue"]: r for r in out["intra"]}

    assert intra["htx"]["round_trip_pct"] == pytest.approx(0.5)
    assert intra["htx"]["break_even_periods"] == pytest.approx(50.0)
    assert intra["okx"]["break_even_periods"] == pytest.approx(30.0)
    assert intra["okx"]["net_over_hold_pct"] == pytest.approx(0.1 - 0.3)


def test_cross_spread_is_taken_hour_by_hour(monkeypatch):
    """OKX платит на 0.02 больше в каждом общем часу: шорт OKX / лонг HTX.
    Круг четырёх своп-ног (0.05+0.05)×2 = 0.2% — 10 периодов."""
    monkeypatch.setattr("time.time", lambda: NOW)
    for h in range(6):
        _rec("htx", 0.01, h)
        _rec("okx", 0.03, h)
    _rec("okx", 0.50, 10)          # час, где HTX не наблюдалась, — не в счёт

    cross = build(window_hours=24, hold_periods=10, venues=["htx", "okx"],
                  symbols=["BTC/USDT"], fee=_fee)["cross"][0]

    assert cross["hours_both_observed"] == 6
    assert cross["mean_spread_pct"] == pytest.approx(-0.02)
    assert cross["direction"] == "short okx / long htx"
    assert cross["break_even_periods"] == pytest.approx(10.0)


def test_no_common_hours_says_so_instead_of_guessing(monkeypatch):
    monkeypatch.setattr("time.time", lambda: NOW)
    _rec("htx", 0.01, 1)
    _rec("okx", 0.03, 5)

    cross = build(window_hours=24, venues=["htx", "okx"], symbols=["BTC/USDT"],
                  fee=_fee)["cross"][0]

    assert cross["mean_spread_pct"] is None
