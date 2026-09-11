"""Локальная книга OKX по каналу `books` (#okx-depth-2026-09-12).

Пять уровней `books5` сломали метрики глубины 07.09, и стакан закрепили на HTX.
Полная книга OKX ведётся из снимка и изменений, с проверкой последовательности
и контрольной суммы — ошибка в любой из них даёт неверную книгу молча.
"""
from __future__ import annotations

import zlib

from services.okx_book import OkxLocalBook, okx_checksum


def _signed(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - (1 << 32) if value >= (1 << 31) else value


def _snap(bids, asks, seq=1, checksum=True):
    book = OkxLocalBook()
    data = {"bids": bids, "asks": asks, "seqId": seq, "prevSeqId": -1}
    if checksum:
        probe = OkxLocalBook()
        probe.apply("snapshot", {"bids": bids, "asks": asks})
        data["checksum"] = okx_checksum(probe.sorted_bids(), probe.sorted_asks())
    assert book.apply("snapshot", data)
    return book


def test_checksum_interleaves_bid_and_ask_as_received_strings():
    """Строка «бид1:размер:аск1:размер:бид2…» — ровно в том виде, как пришло."""
    bids = [("3366.1", "7"), ("3366", "6")]
    asks = [("3366.8", "9"), ("3368", "8")]
    expected = _signed(zlib.crc32(b"3366.1:7:3366.8:9:3366:6:3368:8"))
    assert okx_checksum(bids, asks) == expected


def test_checksum_appends_the_longer_side_when_the_other_runs_out():
    bids = [("10", "1")]
    asks = [("11", "2"), ("12", "3")]
    assert okx_checksum(bids, asks) == _signed(zlib.crc32(b"10:1:11:2:12:3"))


def test_a_snapshot_is_sorted_best_first():
    book = _snap([["99", "1", "0", "1"], ["100", "2", "0", "1"]],
                 [["102", "1", "0", "1"], ["101", "3", "0", "1"]])
    bids, asks = book.top(10)
    assert [b[0] for b in bids] == [100.0, 99.0]
    assert [a[0] for a in asks] == [101.0, 102.0]


def test_updates_change_and_delete_levels():
    book = _snap([["100", "2", "0", "1"], ["99", "1", "0", "1"]], [["101", "3", "0", "1"]])
    probe = OkxLocalBook()
    probe.apply("snapshot", {"bids": [["100", "5"]], "asks": [["101", "3"]]})
    ok = book.apply("update", {
        "bids": [["100", "5", "0", "1"], ["99", "0", "0", "0"]],   # 99 удалён
        "asks": [],
        "seqId": 2, "prevSeqId": 1,
        "checksum": okx_checksum(probe.sorted_bids(), probe.sorted_asks()),
    })
    assert ok
    bids, _ = book.top(10)
    assert bids == [[100.0, 5.0]]


def test_a_missed_message_forces_a_rebuild():
    """prevSeqId не совпал с последним seqId — пропущено изменение."""
    book = _snap([["100", "2", "0", "1"]], [["101", "3", "0", "1"]], seq=5)
    assert book.apply("update", {"bids": [], "asks": [], "seqId": 9, "prevSeqId": 7}) is False


def test_a_wrong_checksum_forces_a_rebuild():
    book = _snap([["100", "2", "0", "1"]], [["101", "3", "0", "1"]])
    assert book.apply("update", {"bids": [["100", "4", "0", "1"]], "asks": [],
                                 "seqId": 2, "prevSeqId": 1, "checksum": 12345}) is False


def test_an_update_before_any_snapshot_is_refused():
    assert OkxLocalBook().apply("update", {"bids": [["1", "1"]], "asks": []}) is False


def test_the_book_is_cut_to_the_same_depth_as_htx():
    bids = [[str(1000 - i), "1", "0", "1"] for i in range(300)]
    asks = [[str(1001 + i), "1", "0", "1"] for i in range(300)]
    book = _snap(bids, asks, checksum=False)
    top_b, top_a = book.top(150)
    assert len(top_b) == 150 and len(top_a) == 150
    assert top_b[0][0] == 1000.0 and top_a[0][0] == 1001.0
