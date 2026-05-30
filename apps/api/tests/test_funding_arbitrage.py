from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import settings
from core.db import Base
from models.funding_arbitrage import FundingArbOpportunity, FundingArbPosition
from services.funding_arbitrage import FundingArbEngine, FundingMonitorService, FundingSymbolMapper, HedgeBuilder


class FakeHTXFundingClient:
    def fetch_funding_rate(self, symbol: str):
        assert symbol == "BTC/USDT:USDT"
        return {"fundingRate": 0.0008, "nextFundingTimestamp": 1_800_000_000_000}

    def fetch_mark_price(self, symbol: str) -> float:
        return {
            "BTC/USDT": 100.0,
            "BTC/USDT:USDT": 100.02,
        }[symbol]


class FakeLiveHTXFundingClient(FakeHTXFundingClient):
    def __init__(self):
        self.orders = []

    def create_market_order(self, symbol: str, side: str, amount: float, params: dict | None = None):
        order = {"id": f"order-{len(self.orders) + 1}", "symbol": symbol, "side": side, "amount": amount, "params": params or {}}
        self.orders.append(order)
        return order


def _db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[FundingArbOpportunity.__table__, FundingArbPosition.__table__])
    return sessionmaker(bind=engine)()


def test_symbol_mapper_uses_single_htx_spot_and_swap_symbol_family():
    assert FundingSymbolMapper.spot_symbol("BTC/USDT") == "BTC/USDT"
    assert FundingSymbolMapper.swap_symbol("BTC/USDT") == "BTC/USDT:USDT"
    assert FundingSymbolMapper.swap_symbol("ETH/USDT:USDT") == "ETH/USDT:USDT"


def test_monitor_scan_persists_candidate_when_funding_edge_is_positive():
    db = _db_session()
    old_enabled = settings.ENABLE_FUNDING_ARB
    old_futures = settings.ENABLE_FUTURES
    try:
        settings.ENABLE_FUNDING_ARB = True
        settings.ENABLE_FUTURES = True
        result = FundingMonitorService(client=FakeHTXFundingClient()).scan(db, symbols=["BTC/USDT"])

        assert result["errors"] == []
        assert result["items"][0]["status"] == "candidate"
        assert result["items"][0]["funding_rate_pct"] == 0.08
        assert db.query(FundingArbOpportunity).count() == 1
    finally:
        settings.ENABLE_FUNDING_ARB = old_enabled
        settings.ENABLE_FUTURES = old_futures
        db.close()


def test_hedge_builder_and_paper_close_log_pnl():
    db = _db_session()
    try:
        opportunity = FundingArbOpportunity(
            symbol="BTC/USDT",
            spot_symbol="BTC/USDT",
            swap_symbol="BTC/USDT:USDT",
            funding_rate=0.001,
            annualized_rate_pct=109.5,
            spot_price=100.0,
            swap_price=100.0,
            basis_pct=0.0,
            estimated_edge_pct=0.1,
            status="candidate",
        )
        db.add(opportunity)
        db.flush()

        hedge = HedgeBuilder().build(opportunity, notional_usdt=100)
        assert hedge["hedge_side"] == "spot_long_perp_short"
        assert hedge["spot_qty"] == 1.0
        assert hedge["break_even_periods"] is not None

        engine = FundingArbEngine()
        position = engine.open_paper(db, opportunity.id, notional_usdt=100)
        assert position.status == "open"
        assert position.entry_funding_rate == 0.001

        closed = engine.close_paper(
            db,
            position.id,
            spot_exit_price=101.0,
            swap_exit_price=99.0,
            funding_periods=2,
            exit_funding_rate=0.0001,
        )

        assert closed.status == "closed"
        assert closed.funding_collected == 0.2
        assert closed.realized_pnl is not None
        assert closed.realized_pnl > 0
    finally:
        db.close()


def test_funding_arb_requires_futures_when_enabled():
    cfg = settings.__class__(ENABLE_FUNDING_ARB=True, ENABLE_FUTURES=False)

    assert "ENABLE_FUNDING_ARB requires ENABLE_FUTURES=true for HTX swap hedge" in cfg.production_blockers()


def test_live_open_uses_one_htx_client_for_spot_and_swap_orders():
    db = _db_session()
    old_enabled = settings.ENABLE_FUNDING_ARB
    old_futures = settings.ENABLE_FUTURES
    old_live = settings.ENABLE_LIVE_ORDERS
    try:
        settings.ENABLE_FUNDING_ARB = True
        settings.ENABLE_FUTURES = True
        settings.ENABLE_LIVE_ORDERS = True
        opportunity = FundingArbOpportunity(
            symbol="BTC/USDT",
            spot_symbol="BTC/USDT",
            swap_symbol="BTC/USDT:USDT",
            funding_rate=0.001,
            annualized_rate_pct=109.5,
            spot_price=100.0,
            swap_price=100.0,
            basis_pct=0.0,
            estimated_edge_pct=0.1,
            status="candidate",
        )
        db.add(opportunity)
        db.flush()
        fake_client = FakeLiveHTXFundingClient()

        position = FundingArbEngine(client=fake_client).open_hedge(db, opportunity.id, notional_usdt=100, mode="live")

        assert position.mode == "live"
        assert [order["symbol"] for order in fake_client.orders] == ["BTC/USDT", "BTC/USDT:USDT"]
        assert [order["side"] for order in fake_client.orders] == ["buy", "sell"]
    finally:
        settings.ENABLE_FUNDING_ARB = old_enabled
        settings.ENABLE_FUTURES = old_futures
        settings.ENABLE_LIVE_ORDERS = old_live
        db.close()
