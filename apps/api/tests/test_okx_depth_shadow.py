"""Полная книга OKX в тени и сравнение книг (#okx-depth-2026-09-12).

07.09 фид перевели на OKX `books5` без проверки потребителей глубины, и пороги
входа сломались молча. Теперь OKX идёт каналом `books`, книга ведётся локально,
и пока рабочий фид на HTX — пишется в тень. `/orderbook/compare` показывает
метрики всех потребителей по обеим книгам: переключаться — по ним.
"""
from __future__ import annotations

import inspect

from services import orderbook_feed as feed
from services.okx_book import OkxLocalBook, okx_checksum
from services.orderbook_feed import OrderBookStore, compare_books, handle_okx_message


def _deep(n, start_bid=1000, step=1, size="1"):
    bids = [[str(start_bid - i * step), size, "0", "1"] for i in range(n)]
    asks = [[str(start_bid + 1 + i * step), size, "0", "1"] for i in range(n)]
    return bids, asks


def _checksum_for(bids, asks):
    probe = OkxLocalBook()
    probe.apply("snapshot", {"bids": bids, "asks": asks})
    return okx_checksum(probe.sorted_bids(), probe.sorted_asks())


def _msg(action, bids, asks, *, seq, prev=-1, checksum=None, inst="BTC-USDT"):
    row = {"bids": bids, "asks": asks, "seqId": seq, "prevSeqId": prev}
    if checksum is not None:
        row["checksum"] = checksum
    return {"arg": {"channel": "books", "instId": inst}, "action": action, "data": [row]}


def test_a_snapshot_lands_in_the_store_cut_to_htx_depth():
    store, books = OrderBookStore(), {}
    bids, asks = _deep(400)
    out = handle_okx_message(_msg("snapshot", bids, asks, seq=1,
                                  checksum=_checksum_for(bids, asks)),
                             {"BTC-USDT": "BTC/USDT"}, books, store, 150)

    assert out == []
    snap = store.snapshot("BTC/USDT")
    assert len(snap["bids"]) == 150 and len(snap["asks"]) == 150


def test_a_broken_sequence_asks_for_a_rebuild_and_keeps_the_last_good_book():
    store, books = OrderBookStore(), {}
    bids, asks = _deep(20)
    handle_okx_message(_msg("snapshot", bids, asks, seq=1, checksum=_checksum_for(bids, asks)),
                       {"BTC-USDT": "BTC/USDT"}, books, store, 150)

    out = handle_okx_message(_msg("update", [["1000", "9", "0", "1"]], [], seq=5, prev=3),
                             {"BTC-USDT": "BTC/USDT"}, books, store, 150)

    assert out == ["BTC-USDT"]
    assert store.snapshot("BTC/USDT")["bids"][0] == [1000.0, 1.0], "в хранилище попала неверная книга"
    assert books["BTC-USDT"].ready is False


def test_books5_still_works_as_before():
    store = OrderBookStore()
    msg = {"arg": {"channel": "books5", "instId": "BTC-USDT"},
           "data": [{"bids": [["100", "1"]], "asks": [["101", "1"]]}]}
    handle_okx_message(msg, {"BTC-USDT": "BTC/USDT"}, {}, store, 150)
    assert store.snapshot("BTC/USDT")["bids"] == [["100", "1"]]


def test_the_shadow_feed_takes_only_the_book():
    """Тень нужна для сравнения глубины; лента сделок ей не нужна."""
    src = inspect.getsource(feed.run_okx_orderbook_feed)
    assert "if not shadow:" in src
    assert "ob_feed_book_resync" in src


def test_comparison_puts_every_consumer_side_by_side():
    """Пять уровней против полной книги — ровно то, что сломалось 07.09:
    thinness на пяти уровнях тождественно 1.0, на полной книге — нет."""
    primary, shadow = OrderBookStore(), OrderBookStore()
    b5, a5 = _deep(5)
    primary.update_book("BTC/USDT", b5, a5)
    b, a = _deep(150)
    shadow.update_book("BTC/USDT", b, a)

    out = compare_books(primary, shadow, levels=10)
    row = out["symbols"]["BTC/USDT"]

    assert row["primary"]["thinness_long"] == 1.0
    assert row["shadow"]["thinness_long"] < 0.1
    for key in ("spread_pct", "obi", "bid_wall", "ask_wall", "thinness_short", "levels_bid"):
        assert key in row["shadow"]
    assert out["median"]["shadow"]["levels_bid"] == 150


def test_a_symbol_missing_on_one_side_is_shown_as_missing():
    primary, shadow = OrderBookStore(), OrderBookStore()
    b, a = _deep(20)
    primary.update_book("ETH/USDT", b, a)

    row = compare_books(primary, shadow)["symbols"]["ETH/USDT"]
    assert row["shadow"] is None and row["primary"] is not None



def test_the_orderbook_page_shows_the_comparison():
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    page = (root / "apps/web/app/orderbook/page.tsx").read_text(encoding="utf-8")
    main_src = (root / "apps/api/main.py").read_text(encoding="utf-8")

    assert "/orderbook/compare" in page and "function BookCompare" in page
    assert '@app.get("/orderbook/compare"' in main_src
