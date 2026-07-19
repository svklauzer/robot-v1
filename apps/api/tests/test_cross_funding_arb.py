"""Тесты P2 paper cross-funding-arb (#cross-farb-2026-07-19).

Чистые функции + полный цикл движка (open → accrue → close) на фейковых
данных и tmp-state. Без сети и БД.
"""

import pytest

from services.cross_funding_arb import (
    CrossFundingArbEngine,
    CrossFundingArbStore,
    accrue_funding_usdt,
    basis_pnl_usdt,
    entry_allowed,
    exit_reason,
    round_trip_fees_usdt,
    signed_carry_pct,
)

HOUR = 3600.0


def _spread(ann=20.0, direction="short_htx_long_kraken"):
    return {
        "spread_hourly_pct": ann / (24 * 365),
        "spread_annualized_pct": ann,
        "direction": direction,
    }


def _item(symbol="AVAX/USDT", ann=20.0, direction="short_htx_long_kraken", basis=0.02):
    return {
        "symbol": symbol,
        "price_diff_pct": basis,
        "htx_mark": 6.6,
        "kraken_mark": 6.6,
        "spread": _spread(ann, direction),
    }


def _hist_row(symbol="AVAX/USDT", avg=19.0, stability=95.0, direction="short_htx_long_kraken"):
    return {
        "symbol": symbol,
        "avg_spread_ann_pct": avg,
        "direction_stability_pct": stability,
        "dominant_direction": direction,
    }


class _FakeHistory:
    def __init__(self, rows):
        self.rows = rows

    def history(self, days=1):
        return {"by_symbol": self.rows}


def test_signed_carry_and_accrual():
    hourly, ann = signed_carry_pct(_spread(20.0), "short_htx_long_kraken")
    assert ann == pytest.approx(20.0)
    # Направление позиции противоположно спреду → carry отрицательный.
    hourly2, ann2 = signed_carry_pct(_spread(20.0), "short_kraken_long_htx")
    assert ann2 == pytest.approx(-20.0) and hourly2 == pytest.approx(-hourly)
    # 100 USDT, 0.005%/час, 2 часа → 0.01 USDT.
    assert accrue_funding_usdt(100.0, 0.005, 2.0) == pytest.approx(0.01)


def test_basis_pnl_direction():
    # short_htx_long_kraken: premium HTX сжался 0.10% → 0.02% → +0.08 на 100.
    assert basis_pnl_usdt(100.0, 0.10, 0.02, "short_htx_long_kraken") == pytest.approx(0.08)
    assert basis_pnl_usdt(100.0, 0.10, 0.02, "short_kraken_long_htx") == pytest.approx(-0.08)


def test_entry_gates():
    ok, why = entry_allowed(_item(ann=20.0), _hist_row())
    assert ok, why
    assert not entry_allowed(_item(ann=5.0), _hist_row())[0]          # спред мал
    assert not entry_allowed(_item(ann=20.0), None)[0]                # нет истории
    assert not entry_allowed(_item(ann=20.0), _hist_row(stability=60))[0]  # шаткое направление
    assert not entry_allowed(
        _item(ann=20.0), _hist_row(direction="short_kraken_long_htx")
    )[0]  # текущее направление против доминирующего


def test_exit_reasons():
    pos = {"direction": "short_htx_long_kraken", "opened_ts": 0.0}
    assert exit_reason(pos, _item(ann=20.0), now_ts=HOUR) is None
    assert "spread_compressed" in exit_reason(pos, _item(ann=2.0), now_ts=HOUR)
    assert exit_reason(pos, _item(ann=-5.0), now_ts=HOUR) == "spread_flipped"
    # Возраст: 15 дней при лимите 14.
    assert exit_reason(pos, _item(ann=20.0), now_ts=15 * 86400) == "max_hold_reached"


def test_engine_full_cycle(tmp_path):
    store = CrossFundingArbStore(path=str(tmp_path / "farb.json"))
    engine = CrossFundingArbEngine(store=store, history=_FakeHistory([_hist_row()]))
    payload = {"status": "ok", "items": [_item(ann=25.0, basis=0.10)]}

    # Шаг 1: вход.
    r1 = engine.step(payload, now=1000.0)
    assert r1["open_count"] == 1
    assert r1["actions"][0]["action"] == "open"
    pos = store.load()["open"][0]
    assert pos["symbol"] == "AVAX/USDT"
    assert pos["entry_basis_pct"] == pytest.approx(0.10)
    assert pos["fees_round_trip_usdt"] == pytest.approx(round_trip_fees_usdt(100.0))

    # Шаг 2 (+2 часа): начисление carry, позиция жива.
    r2 = engine.step({"status": "ok", "items": [_item(ann=25.0, basis=0.06)]}, now=1000.0 + 2 * HOUR)
    assert r2["open_count"] == 1
    pos = store.load()["open"][0]
    expected_carry = accrue_funding_usdt(100.0, 25.0 / (24 * 365), 2.0)
    assert pos["funding_accrued_usdt"] == pytest.approx(expected_carry, abs=1e-6)
    # Базис сжался 0.10 → 0.06 → +0.04 в нашу пользу.
    assert pos["unrealized_basis_usdt"] == pytest.approx(0.04)

    # Шаг 3 (+1 час): спред сжался ниже порога → закрытие с реализацией.
    r3 = engine.step({"status": "ok", "items": [_item(ann=1.0, basis=0.02)]}, now=1000.0 + 3 * HOUR)
    assert r3["open_count"] == 0 and r3["closed_count"] == 1
    closed = store.load()["closed"][0]
    assert "spread_compressed" in closed["close_reason"]
    expected_realized = (
        expected_carry + accrue_funding_usdt(100.0, 1.0 / (24 * 365), 1.0)  # carry за 3-й час по новому спреду
        + basis_pnl_usdt(100.0, 0.10, 0.02, "short_htx_long_kraken")
        - round_trip_fees_usdt(100.0)
    )
    assert closed["realized_usdt"] == pytest.approx(expected_realized, abs=1e-6)
    assert store.load()["realized_total_usdt"] == pytest.approx(expected_realized, abs=1e-6)


def test_engine_respects_max_positions_and_symbol_list(tmp_path):
    store = CrossFundingArbStore(path=str(tmp_path / "farb.json"))
    rows = [_hist_row(s) for s in ("AVAX/USDT", "XRP/USDT", "TRX/USDT")]
    engine = CrossFundingArbEngine(store=store, history=_FakeHistory(rows))
    payload = {
        "status": "ok",
        "items": [
            _item("AVAX/USDT", ann=30.0),
            _item("XRP/USDT", ann=25.0),
            _item("TRX/USDT", ann=20.0),
            _item("ARB/USDT", ann=50.0),  # вне CROSS_FARB_SYMBOLS — игнор
        ],
    }
    r = engine.step(payload, now=1000.0)
    assert r["open_count"] == 2  # CROSS_FARB_MAX_POSITIONS
    opened = {p["symbol"] for p in store.load()["open"]}
    assert opened == {"AVAX/USDT", "XRP/USDT"}  # по величине спреда, ARB отрезан

    # Повторный шаг: дублей нет.
    r2 = engine.step(payload, now=1000.0 + HOUR)
    assert r2["open_count"] == 2 and not [a for a in r2["actions"] if a["action"] == "open"]


def test_engine_broken_state_file_fail_open(tmp_path):
    path = str(tmp_path / "farb.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{broken")
    store = CrossFundingArbStore(path=path)
    assert store.load()["open"] == []  # битый файл → чистое состояние, без исключений
