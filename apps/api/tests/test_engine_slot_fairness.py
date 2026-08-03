"""Движки не отнимают слоты друг у друга (#engine-slots-2026-08-03).

Замер 03.08 по ленте решений: 44 кандидата CRT, все заблокированы, из них 24 —
`cluster_direction_cap`. На медвежьем рынке trend_down давал 60% событий и
занимал оба шортовых слота первым. Ожидание при этом: CRT +0.134R,
trend_down −0.012R — дефицитный ресурс доставался худшему режиму по очереди.
"""
from __future__ import annotations

import pytest

from services.exposure_guard import ExposureGuard


class _Sig:
    def __init__(self, symbol, side, regime, margin=50.0, status="opened"):
        self.symbol = symbol
        self.side = side
        self.status = status
        self.required_margin = margin
        self.plan_json = {"regime": regime}


class _DB:
    def __init__(self, rows):
        self.rows = rows


def _guard(rows):
    guard = ExposureGuard()
    guard.active_signals = lambda db, bot_id: rows            # type: ignore
    guard.active_signals_for_symbol = lambda db, bot_id, sym: [  # type: ignore
        r for r in rows if r.symbol == sym
    ]
    return guard


def _check(guard, *, side, engine, per_engine=2, portfolio=5, symbol="NEW/USDT"):
    return guard.check_before_publish(
        db=None, bot_id=1, symbol=symbol, required_margin=50.0,
        equity_usdt=950.0, max_used_margin_pct=0.85,
        max_active_signals=100, max_active_per_symbol=1,
        side=side, max_same_direction_cluster=per_engine,
        cluster_symbols=None, engine=engine,
        portfolio_max_same_direction=portfolio,
    )


# ── метка движка ────────────────────────────────────────────────────────────
def test_trend_up_and_trend_down_are_one_engine():
    """Направление уже выражено в side; делить слоты движка ещё и по нему нельзя."""
    assert ExposureGuard.engine_of_regime("trend_up_candidate") == "trend"
    assert ExposureGuard.engine_of_regime("trend_down_candidate") == "trend"


def test_engines_are_distinct():
    assert ExposureGuard.engine_of_regime("crt") == "crt"
    assert ExposureGuard.engine_of_regime("scalp") == "scalp"
    assert ExposureGuard.engine_of_regime("range") == "range"
    assert ExposureGuard.engine_of_regime("reversal_long_candidate") == "reversal"
    assert ExposureGuard.engine_of_regime("watch_short_escalated_candidate") == "reversal"


# ── главный сценарий ────────────────────────────────────────────────────────
def test_crt_opens_while_trend_holds_two_shorts():
    """Исходная поломка: два шорта trend закрывали вход CRT."""
    rows = [
        _Sig("BTC/USDT", "short", "trend_down_candidate"),
        _Sig("ETH/USDT", "short", "trend_down_candidate"),
    ]
    guard = _guard(rows)

    blocked = _check(guard, side="short", engine="trend")
    assert not blocked.allowed
    assert blocked.reason == "cluster_direction_cap"

    allowed = _check(guard, side="short", engine="crt")
    assert allowed.allowed, "CRT обязан входить: слоты trend его не касаются"
    assert allowed.cluster_same_dir_count == 0
    assert allowed.portfolio_same_dir_count == 2


def test_engine_still_limits_itself():
    """Своё направление внутри движка по-прежнему ограничено."""
    rows = [
        _Sig("BTC/USDT", "short", "crt"),
        _Sig("ETH/USDT", "short", "crt"),
    ]
    result = _check(_guard(rows), side="short", engine="crt")
    assert not result.allowed
    assert result.reason == "cluster_direction_cap"


def test_opposite_direction_is_never_blocked():
    rows = [_Sig("BTC/USDT", "short", "crt"), _Sig("ETH/USDT", "short", "crt")]
    assert _check(_guard(rows), side="long", engine="crt").allowed


# ── портфельный предохранитель ──────────────────────────────────────────────
def test_portfolio_cap_still_bounds_total_exposure():
    """Пять однонаправленных стопов = 2% эквити при RISK_PER_TRADE_PCT=0.4,
    против MAX_DAILY_LOSS_PCT=3. Дальше пускать нельзя даже разным движкам."""
    rows = [
        _Sig("BTC/USDT", "short", "trend_down_candidate"),
        _Sig("ETH/USDT", "short", "trend_down_candidate"),
        _Sig("SOL/USDT", "short", "crt"),
        _Sig("XRP/USDT", "short", "crt"),
        _Sig("AVAX/USDT", "short", "scalp"),
    ]
    result = _check(_guard(rows), side="short", engine="range")
    assert not result.allowed
    assert result.reason == "portfolio_direction_cap"
    assert result.portfolio_same_dir_count == 5


def test_portfolio_cap_can_be_disabled():
    rows = [_Sig(f"S{i}/USDT", "short", "scalp") for i in range(9)]
    result = _check(_guard(rows), side="short", engine="crt", portfolio=0)
    assert result.allowed


# ── совместимость ───────────────────────────────────────────────────────────
def test_engine_none_restores_portfolio_wide_behaviour():
    """CORR_CLUSTER_PER_ENGINE=false → прежняя логика общего слота."""
    rows = [
        _Sig("BTC/USDT", "short", "trend_down_candidate"),
        _Sig("ETH/USDT", "short", "trend_down_candidate"),
    ]
    result = _check(_guard(rows), side="short", engine=None)
    assert not result.allowed
    assert result.reason == "cluster_direction_cap"


def test_legacy_signals_without_regime_do_not_crash():
    sig = _Sig("BTC/USDT", "short", None)
    sig.plan_json = {}
    assert ExposureGuard.engine_of(sig) == "unknown"
    assert _check(_guard([sig]), side="short", engine="crt").allowed
