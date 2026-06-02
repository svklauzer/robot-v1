from services.market_connectivity import MarketConnectivityService


class FakeMarket:
    def __init__(self, snap=None, error=None):
        self.snap = snap
        self.error = error

    def snapshot(self, symbol: str):
        if self.error:
            raise self.error
        return self.snap


def test_market_connectivity_ok_with_spread_and_latency(monkeypatch):
    monkeypatch.setattr("services.market_connectivity.settings.MARKET_CONNECTIVITY_MAX_LATENCY_MS", 5000)
    monkeypatch.setattr("services.market_connectivity.settings.MARKET_CONNECTIVITY_MAX_SPREAD_PCT", 1.0)
    monkeypatch.setattr("services.market_connectivity.settings.ENABLE_LIVE_ORDERS", False)
    monkeypatch.setattr("services.market_connectivity.settings.TRADING_MODE", "paper_signal")

    result = MarketConnectivityService(
        FakeMarket({"last": 100, "bid": 99.9, "ask": 100.1, "source": "htx"})
    ).check("BTC/USDT")

    assert result["ok"] is True
    assert result["breaker_blocked"] is False
    assert result["spread_pct"] == 0.2
    assert result["source"] == "htx"


def test_market_connectivity_blocks_mock_source_in_live(monkeypatch):
    monkeypatch.setattr("services.market_connectivity.settings.ENABLE_LIVE_ORDERS", True)
    monkeypatch.setattr("services.market_connectivity.settings.TRADING_MODE", "live_limited")

    result = MarketConnectivityService(
        FakeMarket({"last": 100, "bid": 99.9, "ask": 100.1, "source": "mock"})
    ).check("BTC/USDT")

    assert result["ok"] is False
    assert result["breaker_blocked"] is True
    assert "live mode cannot use mock market data" in result["blockers"]


def test_market_connectivity_reports_snapshot_failure():
    result = MarketConnectivityService(FakeMarket(error=TimeoutError("htx timeout"))).check("BTC/USDT")

    assert result["ok"] is False
    assert result["breaker_blocked"] is True
    assert result["blockers"] == ["market data snapshot failed"]
    assert "TimeoutError" in result["error"]


def test_market_connectivity_blocks_high_spread(monkeypatch):
    monkeypatch.setattr("services.market_connectivity.settings.MARKET_CONNECTIVITY_MAX_SPREAD_PCT", 0.5)
    monkeypatch.setattr("services.market_connectivity.settings.ENABLE_LIVE_ORDERS", False)
    monkeypatch.setattr("services.market_connectivity.settings.TRADING_MODE", "paper_signal")

    result = MarketConnectivityService(
        FakeMarket({"last": 100, "bid": 99.0, "ask": 101.0, "source": "htx"})
    ).check("BTC/USDT")

    assert result["ok"] is False
    assert result["breaker_blocked"] is True
    assert "market spread is above threshold" in result["blockers"]
