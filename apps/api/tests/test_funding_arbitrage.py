import pytest
from datetime import datetime, timedelta, timezone

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


class FakeClosingHTXFundingClient:
    def fetch_funding_rate(self, symbol: str):
        return {"fundingRate": 0.00001}

    def fetch_mark_price(self, symbol: str) -> float:
        return {
            "BTC/USDT": 101.0,
            "BTC/USDT:USDT": 99.0,
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
    # (#funding-confirm-2026-07-27) Тест проверяет ЭКОНОМИКУ кандидата. Гейт
    # подтверждения ставки — отдельный слой со своими тестами
    # (test_funding_rate_confirmation.py); здесь он выключен, иначе тест
    # проверял бы наличие истории наблюдений, а не расчёт доходности.
    old_confirm = settings.FUNDING_ARB_CONFIRM_ENABLED
    try:
        settings.ENABLE_FUNDING_ARB = True
        settings.ENABLE_FUTURES = True
        settings.FUNDING_ARB_CONFIRM_ENABLED = False
        result = FundingMonitorService(client=FakeHTXFundingClient()).scan(db, symbols=["BTC/USDT"])

        assert result["errors"] == []
        assert result["items"][0]["status"] == "candidate"
        assert result["items"][0]["funding_rate_pct"] == 0.08
        assert db.query(FundingArbOpportunity).count() == 1
    finally:
        settings.ENABLE_FUNDING_ARB = old_enabled
        settings.ENABLE_FUTURES = old_futures
        settings.FUNDING_ARB_CONFIRM_ENABLED = old_confirm
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
        # (#funding-honest-accrual-2026-07-27) Ставка упала 0.001 → 0.0001, и это
        # теперь учитывается: начисление идёт по средней (вход+выход)/2, а не по
        # ставке входа за весь срок. Прежнее значение 0.2 завышало результат на
        # 82% — ровно тот перекос, из-за которого движок выглядел прибыльным.
        # entry 0.001, exit 0.0001, 2 периода, notional 100:
        #   было:  0.001  × 100 × 2 = 0.20
        #   стало: 0.00055 × 100 × 2 = 0.11
        assert closed.funding_collected == pytest.approx(0.11)
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
    old_exec_mode = settings.LIVE_EXECUTION_MODE
    old_cap = settings.LIVE_MAX_ORDER_NOTIONAL_USDT
    try:
        # Тест про маршрутизацию ног хеджа, а не про предохранитель размера:
        # дефолтный кэп 25 USDT отклонил бы обе ноги на 100 и оставил список
        # ордеров пустым.
        settings.LIVE_MAX_ORDER_NOTIONAL_USDT = 1000.0
        settings.ENABLE_FUNDING_ARB = True
        settings.ENABLE_FUTURES = True
        settings.ENABLE_LIVE_ORDERS = True
        # Одного ENABLE_LIVE_ORDERS мало: LIVE_EXECUTION_MODE по умолчанию
        # dry_run, и ядро исполнения возвращает синтетический ack, не трогая
        # клиента. Без этой строки тест проверял пустой список ордеров.
        settings.LIVE_EXECUTION_MODE = "live"
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

        # Обе ноги уходят через общее ядро исполнения (LIVE_EXECUTOR), а не
        # через клиента, переданного в движок: клиент остался только для чтения
        # рыночных данных. Перехватываем ядро — это и есть реальный путь ордера.
        from services.live_executor import LIVE_EXECUTOR, OrderResult

        sent: list[dict] = []

        def _capture(symbol, side, amount, **kwargs):
            sent.append({"symbol": symbol, "side": side, "amount": amount,
                         "market_type": kwargs.get("market_type")})
            return OrderResult(
                ok=True, mode="live", sent=True, status="filled", symbol=symbol,
                side=side, requested_qty=amount, market_type=kwargs.get("market_type", ""),
                reduce_only=bool(kwargs.get("reduce_only")), filled_qty=amount,
                avg_price=kwargs.get("reference_price"),
            )

        original = LIVE_EXECUTOR.place_market
        LIVE_EXECUTOR.place_market = _capture
        try:
            position = FundingArbEngine(client=FakeLiveHTXFundingClient()).open_hedge(
                db, opportunity.id, notional_usdt=100, mode="live"
            )
        finally:
            LIVE_EXECUTOR.place_market = original

        assert position.mode == "live"
        # Спотовая нога — спотовым символом, своп-нога — контрактным (`:USDT`).
        # Перепутанный символ увёл бы своп-ордер на спот, где нет ни шорта, ни
        # плеча, а хедж превратился бы в двойную покупку.
        assert [order["symbol"] for order in sent] == ["BTC/USDT", "BTC/USDT:USDT"]
        assert [order["side"] for order in sent] == ["buy", "sell"]
        assert [order["market_type"] for order in sent] == ["spot", "swap"]
    finally:
        settings.ENABLE_FUNDING_ARB = old_enabled
        settings.ENABLE_FUTURES = old_futures
        settings.ENABLE_LIVE_ORDERS = old_live
        settings.LIVE_EXECUTION_MODE = old_exec_mode
        settings.LIVE_MAX_ORDER_NOTIONAL_USDT = old_cap
        db.close()


def test_monitor_scan_interval_defaults_to_eight_hours():
    old_interval = settings.FUNDING_ARB_SCAN_INTERVAL_HOURS
    try:
        settings.FUNDING_ARB_SCAN_INTERVAL_HOURS = 8
        assert FundingMonitorService(client=FakeHTXFundingClient()).scan_interval_seconds() == 8 * 60 * 60
    finally:
        settings.FUNDING_ARB_SCAN_INTERVAL_HOURS = old_interval


def test_evaluate_exits_auto_closes_paper_when_funding_compresses():
    db = _db_session()
    old_min_hold = settings.FUNDING_ARB_MIN_HOLD_PERIODS
    try:
        settings.FUNDING_ARB_MIN_HOLD_PERIODS = 2   # lower for test

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
        position = FundingArbEngine().open_paper(db, opportunity.id, notional_usdt=100)
        # 16h = 2 funding periods — meets the min_hold=2 threshold
        position.opened_at = datetime.now(timezone.utc) - timedelta(hours=16)

        result = FundingArbEngine(client=FakeClosingHTXFundingClient()).evaluate_exits(db)

        assert result["evaluated"] == 1
        assert len(result["closed"]) == 1
        assert result["close_required"] == []
        assert result["closed"][0]["decision"]["reason"] == "funding_rate_compressed"
        assert result["closed"][0]["position"]["funding_periods"] == 2
        assert db.query(FundingArbPosition).filter(FundingArbPosition.status == "closed").count() == 1
    finally:
        settings.FUNDING_ARB_MIN_HOLD_PERIODS = old_min_hold
        db.close()


def test_evaluate_exits_respects_min_hold_periods():
    """Position younger than FUNDING_ARB_MIN_HOLD_PERIODS must not be closed."""
    db = _db_session()
    old_min_hold = settings.FUNDING_ARB_MIN_HOLD_PERIODS
    try:
        settings.FUNDING_ARB_MIN_HOLD_PERIODS = 3

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
        position = FundingArbEngine().open_paper(db, opportunity.id, notional_usdt=100)
        # Only 10 hours held = 1.25 periods < min_hold=3
        position.opened_at = datetime.now(timezone.utc) - timedelta(hours=10)

        result = FundingArbEngine(client=FakeClosingHTXFundingClient()).evaluate_exits(db)

        assert result["evaluated"] == 1
        assert len(result["closed"]) == 0   # must NOT close — min hold not reached
        assert result["held"][0]["reason"] == "min_hold_not_reached"
    finally:
        settings.FUNDING_ARB_MIN_HOLD_PERIODS = old_min_hold
        db.close()


def test_auto_open_candidates_opens_paper_for_candidate():
    db = _db_session()
    old_enabled = settings.ENABLE_FUNDING_ARB
    old_auto = settings.FUNDING_ARB_AUTO_OPEN_PAPER
    old_max = settings.FUNDING_ARB_MAX_OPEN_HEDGES
    try:
        settings.ENABLE_FUNDING_ARB = True
        settings.FUNDING_ARB_AUTO_OPEN_PAPER = True
        settings.FUNDING_ARB_MAX_OPEN_HEDGES = 2

        # Create a candidate opportunity
        opportunity = FundingArbOpportunity(
            symbol="BTC/USDT",
            spot_symbol="BTC/USDT",
            swap_symbol="BTC/USDT:USDT",
            funding_rate=0.001,
            annualized_rate_pct=109.5,
            spot_price=100.0,
            swap_price=100.0,
            basis_pct=0.0,
            estimated_edge_pct=0.05,
            status="candidate",
        )
        db.add(opportunity)
        db.flush()

        result = FundingArbEngine().auto_open_candidates(db)

        assert result["auto_open"] is True
        assert len(result["opened"]) == 1
        assert result["opened"][0]["symbol"] == "BTC/USDT"
        assert db.query(FundingArbPosition).filter(FundingArbPosition.status == "open").count() == 1
    finally:
        settings.ENABLE_FUNDING_ARB = old_enabled
        settings.FUNDING_ARB_AUTO_OPEN_PAPER = old_auto
        settings.FUNDING_ARB_MAX_OPEN_HEDGES = old_max
        db.close()


def test_auto_open_candidates_respects_max_hedges():
    db = _db_session()
    old_enabled = settings.ENABLE_FUNDING_ARB
    old_auto = settings.FUNDING_ARB_AUTO_OPEN_PAPER
    old_max = settings.FUNDING_ARB_MAX_OPEN_HEDGES
    try:
        settings.ENABLE_FUNDING_ARB = True
        settings.FUNDING_ARB_AUTO_OPEN_PAPER = True
        settings.FUNDING_ARB_MAX_OPEN_HEDGES = 1

        for i in range(3):
            opp = FundingArbOpportunity(
                symbol=f"SYM{i}/USDT",
                spot_symbol=f"SYM{i}/USDT",
                swap_symbol=f"SYM{i}/USDT:USDT",
                funding_rate=0.001,
                annualized_rate_pct=109.5,
                spot_price=100.0,
                swap_price=100.0,
                basis_pct=0.0,
                estimated_edge_pct=0.05,
                status="candidate",
            )
            db.add(opp)
        db.flush()

        result = FundingArbEngine().auto_open_candidates(db)

        # Should only open 1 position (max_open_hedges = 1)
        assert len(result["opened"]) == 1
        assert db.query(FundingArbPosition).filter(FundingArbPosition.status == "open").count() == 1
    finally:
        settings.ENABLE_FUNDING_ARB = old_enabled
        settings.FUNDING_ARB_AUTO_OPEN_PAPER = old_auto
        settings.FUNDING_ARB_MAX_OPEN_HEDGES = old_max
        db.close()


def test_snapshot_computes_fee_aware_net_yield():
    """Net yield per period must account for round-trip fees."""
    old_enabled = settings.ENABLE_FUNDING_ARB
    old_assumed = settings.FUNDING_ARB_ASSUMED_HOLD_PERIODS
    # См. комментарий выше: здесь проверяется расчёт доходности, а не наличие
    # подтверждённой истории ставки.
    old_confirm = settings.FUNDING_ARB_CONFIRM_ENABLED
    try:
        settings.ENABLE_FUNDING_ARB = True
        settings.FUNDING_ARB_ASSUMED_HOLD_PERIODS = 10
        settings.FUNDING_ARB_CONFIRM_ENABLED = False

        snapshot = FundingMonitorService(client=FakeHTXFundingClient()).snapshot("BTC/USDT")

        # funding_rate_pct = 0.08, fee_round_trip_pct = (0.002 + 0.0005) * 2 * 100 = 0.5
        # fee_per_period = 0.5 / 10 = 0.05
        # basis is positive (0.02%) → contribution = 0.006
        # net_yield = 0.08 + 0.006 - 0.05 = 0.036
        assert snapshot.funding_rate_pct == 0.08
        assert snapshot.fee_round_trip_pct == 0.5
        assert snapshot.net_yield_per_period_pct > 0  # profitable after fees
        assert snapshot.break_even_periods is not None
        assert snapshot.break_even_periods > 0
        assert snapshot.status == "candidate"
    finally:
        settings.ENABLE_FUNDING_ARB = old_enabled
        settings.FUNDING_ARB_ASSUMED_HOLD_PERIODS = old_assumed
        settings.FUNDING_ARB_CONFIRM_ENABLED = old_confirm


def test_funding_arb_paper_cycle_smoke_logs_closed_pnl():
    db = _db_session()
    try:
        result = FundingArbEngine().paper_cycle_smoke(db, notional_usdt=100, funding_periods=1)

        assert result["status"] == "ok"
        assert result["checks"]["scan_candidate_created"] is True
        assert result["checks"]["paper_hedge_opened"] is True
        assert result["checks"]["funding_periods_logged"] is True
        assert result["checks"]["pnl_logged"] is True
        assert result["checks"]["closed_on_compression"] is True
        assert result["position"]["status"] == "closed"
        assert result["position"]["mode"] == "paper"
        assert result["position"]["funding_periods"] == 1
        assert result["position"]["realized_pnl"] is not None
        assert db.query(FundingArbOpportunity).count() == 1
        assert db.query(FundingArbPosition).filter(FundingArbPosition.status == "closed").count() == 1
    finally:
        db.close()
