"""Решения гейта стакана на двух книгах (#okx-gate-compare-2026-09-12).

Медианы 12.09: стенки HTX 0.49/0.43 против OKX 0.30/0.22 при пороге 0.30.
Гейт пропускает лонг по OBI ≥ 0.15 или бид-стенке ≥ 0.30 — на OKX он
блокировал бы чаще. Здесь меряется доля пропусков и подбираются пороги OKX с
той же избирательностью.
"""
from __future__ import annotations

import inspect
import pytest

from services import book_gate_compare as bgc
from services import orderbook_feed as feed
from services.orderbook_feed import OrderBookStore


@pytest.fixture(autouse=True)
def clean():
    bgc.SAMPLES.clear()
    yield
    bgc.SAMPLES.clear()


def _book(bid_top, ask_top, rest=1.0, n=10):
    """Книга: крупный первый уровень + ровные остальные — стенка = top/(top+rest·(n−1))."""
    bids = [[100.0 - i * 0.1, bid_top if i == 0 else rest] for i in range(n)]
    # Спред 0.01% — ниже скальпового лимита 0.08%: сравниваем стенки, а не спред.
    asks = [[100.01 + i * 0.1, ask_top if i == 0 else rest] for i in range(n)]
    return bids, asks


def test_a_book_with_walls_passes_where_a_flat_one_does_not():
    walls, flat = OrderBookStore(), OrderBookStore()
    walls.update_book("BTC/USDT", *_book(9.0, 9.0))      # стенка 9/18 = 0.5
    flat.update_book("BTC/USDT", *_book(1.0, 1.0))       # стенка 0.1, OBI 0

    bgc.sample("primary", walls, now=1.0)
    bgc.sample("shadow", flat, now=1.0)
    out = bgc.summary()

    for prof in ("position", "scalp"):
        assert out["books"]["primary"]["pass_rate"][prof]["long"] == 1.0, prof
        assert out["books"]["shadow"]["pass_rate"][prof]["long"] == 0.0, prof


def test_the_matched_threshold_keeps_the_pass_rate():
    """На рабочей книге стенка ≥ 0.30 в половине выборок. На тени стенки
    вдвое ниже — порог с той же долей тоже вдвое ниже."""
    ref = [0.2, 0.25, 0.35, 0.4]
    tgt = [0.1, 0.125, 0.175, 0.2]
    assert bgc._matched_threshold(ref, 0.30, tgt) == 0.175


def test_trend_and_scalp_are_judged_by_their_own_thresholds():
    """Тренд (position): OBI ≥ 0.05 или стенка ≥ 0.20; скальп: 0.15 / 0.30."""
    th = bgc.summary()["thresholds"]
    assert th["position"]["wall_confirm"] == 0.20 and th["position"]["obi_confirm"] == 0.05
    assert th["scalp"]["wall_confirm"] == 0.30 and th["scalp"]["obi_confirm"] == 0.15


def test_stale_books_are_not_sampled():
    store = OrderBookStore()
    store.update_book("BTC/USDT", *_book(9.0, 9.0))
    store._book_ts["BTC/USDT"] = 0.0          # книга старше порога свежести
    assert bgc.sample("primary", store) == 0


def test_the_sampler_runs_beside_the_shadow():
    src = inspect.getsource(feed.run_orderbook_feed)
    assert "_sample_books_loop(" in src
    loop = inspect.getsource(feed._sample_books_loop)
    assert 'book_gate_compare.sample("primary"' in loop and 'book_gate_compare.sample("shadow"' in loop


def test_cvd_is_left_out_because_the_shadow_has_no_trades():
    params = bgc.gate_params()
    assert params["cvd_block_ratio"] == 0.0 and params["cvd_thin_ratio"] == 0.0
