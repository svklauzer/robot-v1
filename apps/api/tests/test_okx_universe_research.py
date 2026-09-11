"""Расчёты скрипта кандидатов во вселенную OKX (#okx-universe-2026-09-12)."""
from __future__ import annotations

import pytest

from research.okx_universe import atr_pct, depth_within, turnover_usdt


def test_turnover_is_base_volume_times_price():
    """ccxt для свопов OKX quoteVolume не даёт: объём в монетах × цена."""
    assert turnover_usdt({"volCcy24h": "2779856.971"}, 2466.74) == pytest.approx(2779856.971 * 2466.74)
    assert turnover_usdt({}, 100.0) is None


def test_depth_counts_contracts_times_contract_size_inside_the_band():
    levels = [[100.0, 10], [100.4, 5], [101.5, 50]]      # последний за полосой 0.5%
    assert depth_within(levels, 100.0, 0.5, 100.0) == pytest.approx(100.0 * 10 * 100 + 100.4 * 5 * 100)


def test_atr_is_a_percent_of_price():
    bars = [[i, 100, 101, 99, 100, 0] for i in range(20)]   # размах 2 при цене 100
    assert atr_pct(bars) == pytest.approx(2.0)
