"""Предохранители перед выходом в live (#live-prep-2026-07-25)."""
from datetime import datetime, timedelta, timezone

from core.config import Settings, settings
from services.live_safety import LiveSafetyService


# ── Ловушка кэпа нотионала ────────────────────────────────────────────────────

def _live_settings(**over):
    base = dict(
        APP_ENV="development",
        ENABLE_LIVE_ORDERS=True,
        TRADING_MODE="live_limited",
        ROBOT_MODE="live",
        TELEGRAM_BOT_TOKEN="token",
        RISK_EQUITY_USDT=950.0,
        MAX_POSITION_MARGIN_PCT=0.13,
        LIVE_MAX_LEVERAGE=1.0,
    )
    base.update(over)
    return Settings(**base)


def test_notional_cap_below_position_size_blocks_live():
    """Дефолтный кэп 25 USDT против типового нотионала ~124 — в первый же день
    live КАЖДЫЙ ордер был бы отклонён. Ловим до старта, а не по логам."""
    blockers = _live_settings(LIVE_MAX_ORDER_NOTIONAL_USDT=25.0).production_blockers()

    assert any("LIVE_MAX_ORDER_NOTIONAL_USDT" in b for b in blockers), blockers


def test_notional_cap_above_position_size_passes():
    blockers = _live_settings(LIVE_MAX_ORDER_NOTIONAL_USDT=150.0).production_blockers()

    assert not any("LIVE_MAX_ORDER_NOTIONAL_USDT" in b for b in blockers), blockers


def test_small_position_sizing_also_satisfies_a_small_cap():
    """Второй легальный путь на этап ramp-up — снизить размер позиции."""
    blockers = _live_settings(
        LIVE_MAX_ORDER_NOTIONAL_USDT=25.0, MAX_POSITION_MARGIN_PCT=0.02
    ).production_blockers()

    assert not any("LIVE_MAX_ORDER_NOTIONAL_USDT" in b for b in blockers), blockers


def test_leverage_requires_futures():
    blockers = _live_settings(
        LIVE_MAX_ORDER_NOTIONAL_USDT=150.0, LIVE_MAX_LEVERAGE=5.0, ENABLE_FUTURES=False
    ).production_blockers()

    assert any("LIVE_MAX_LEVERAGE" in b for b in blockers), blockers


# ── Дневной лимит числа сделок ────────────────────────────────────────────────

class _FakeSignal:
    pass


class _CountQuery:
    def __init__(self, n):
        self._n = n

    def filter(self, *a, **k):
        return self

    def count(self):
        return self._n


class _CountDB:
    def __init__(self, n):
        self._n = n

    def query(self, *a, **k):
        return _CountQuery(self._n)


class _Bot:
    def __init__(self):
        self.id = 1
        self.status = "running"
        self.config_json = {}


def _snapshot_with_trades(n, limit):
    old = settings.MAX_TRADES_PER_DAY
    try:
        settings.MAX_TRADES_PER_DAY = limit
        svc = LiveSafetyService()
        svc.daily_net_pnl_usdt = lambda db, hours=24: 0.0   # изолируем счётчик
        return svc.snapshot(db=_CountDB(n), bot=_Bot(), equity_usdt=950.0)
    finally:
        settings.MAX_TRADES_PER_DAY = old


def test_trade_count_breaker_blocks_at_limit():
    state = _snapshot_with_trades(12, 12)

    assert state["trade_count_blocked"] is True
    assert state["blocked"] is True
    assert any("trade-count" in b for b in state["blockers"])


def test_trade_count_breaker_silent_below_limit():
    state = _snapshot_with_trades(5, 12)

    assert state["trade_count_blocked"] is False
    assert state["blocked"] is False


def test_trade_count_breaker_disabled_by_zero():
    state = _snapshot_with_trades(999, 0)

    assert state["trade_count_blocked"] is False
    assert state["max_trades_per_day"] == 0
