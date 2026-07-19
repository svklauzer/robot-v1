"""Тесты кросс-биржевой телеметрии HTX↔Kraken (#kraken-p1-2026-07-18).

Чистые функции + сервис с фейковыми клиентами — без ccxt/сети.
"""

import pytest

from services.kraken_client import map_to_kraken_symbol
from services.venue_compare import (
    VenueCompareService,
    VenueSpreadHistory,
    _prediction_pct,
    funding_spread,
    kraken_rate_from_entry,
    normalize_funding,
)


def test_map_to_kraken_symbol():
    assert map_to_kraken_symbol("BTC/USDT") == "BTC/USD:USD"
    assert map_to_kraken_symbol("trx/usdt") == "TRX/USD:USD"
    # Уже swap-формат HTX — берём только базу.
    assert map_to_kraken_symbol("ETH/USDT:USDT") == "ETH/USD:USD"


def test_normalize_funding_htx_8h_vs_kraken_1h():
    # 0.08% за 8ч на HTX == 0.01%/час == 0.01% за 1ч на Kraken.
    htx = normalize_funding(0.0008, 8.0)
    kraken = normalize_funding(0.0001, 1.0)
    assert htx["per_hour_pct"] == pytest.approx(0.01)
    assert kraken["per_hour_pct"] == pytest.approx(0.01)
    assert htx["annualized_pct"] == pytest.approx(kraken["annualized_pct"])
    # Годовая: 0.01%/час × 24 × 365 = 87.6%.
    assert htx["annualized_pct"] == pytest.approx(87.6)


def test_funding_spread_direction_and_break_even():
    # HTX 0.08%/8ч (0.01%/ч) vs Kraken 0.002%/ч → спред +0.008%/ч →
    # шорт HTX + лонг Kraken; 0.192%/день; комиссии 0.2% → окупаемость ~1 день.
    s = funding_spread(0.0008, 0.00002, 8.0, 1.0, round_trip_fee_pct=0.2)
    assert s["direction"] == "short_htx_long_kraken"
    assert s["spread_hourly_pct"] == pytest.approx(0.008)
    assert s["spread_daily_pct"] == pytest.approx(0.192)
    assert s["break_even_days"] == pytest.approx(1.0, abs=0.05)

    inverse = funding_spread(0.0, 0.0001, 8.0, 1.0, round_trip_fee_pct=0.2)
    assert inverse["direction"] == "short_kraken_long_htx"

    flat = funding_spread(0.0, 0.0, 8.0, 1.0, round_trip_fee_pct=0.2)
    assert flat["break_even_days"] is None


def test_kraken_rate_prefers_relative():
    # Известная странность ccxt/krakenfutures: fundingRate может быть абсолютным
    # (USD на контракт) — относительная ставка лежит в info.relativeFundingRate.
    entry = {"fundingRate": 12.34, "info": {"relativeFundingRate": "0.000045"}}
    assert kraken_rate_from_entry(entry) == pytest.approx(0.000045)
    assert kraken_rate_from_entry({"fundingRate": 0.0001, "info": {}}) == pytest.approx(0.0001)
    assert kraken_rate_from_entry(None) is None
    assert kraken_rate_from_entry({"info": {}}) is None


class _FakeHTX:
    def fetch_funding_rate(self, symbol):
        return {"fundingRate": 0.0008}

    def fetch_mark_price(self, symbol):
        return 100.0


class _FakeKraken:
    def __init__(self, fail=False):
        self.fail = fail

    def fetch_funding_rates(self, symbols=None):
        if self.fail:
            return {}
        return {
            "BTC/USD:USD": {
                "markPrice": 99.9,
                "info": {"relativeFundingRate": "0.0001", "relativeFundingRatePrediction": "0.0002"},
            }
        }

    def fetch_funding_rate(self, symbol):
        if self.fail:
            raise RuntimeError("kraken_down")
        return {"markPrice": 99.9, "info": {"relativeFundingRate": "0.0001"}}

    def fetch_mark_price(self, symbol):
        return 99.9

    def ping(self):
        return {"ok": not self.fail, "latency_ms": 5.0}


def test_compare_happy_path():
    service = VenueCompareService(htx_client=_FakeHTX(), kraken_client=_FakeKraken())
    result = service.compare(["BTC/USDT"], use_cache=False)
    assert result["status"] == "ok"
    item = result["items"][0]
    assert item["kraken_symbol"] == "BTC/USD:USD"
    assert "error" not in item
    # HTX 0.0001/ч vs Kraken 0.0001/ч × 100 → HTX 0.01%/ч, Kraken 0.01%/ч → спред 0.
    assert item["spread"]["spread_hourly_pct"] == pytest.approx(0.0)
    assert item["price_diff_pct"] == pytest.approx((100.0 - 99.9) / 99.9 * 100, abs=1e-3)
    assert item["kraken_next_funding_prediction_pct"] == pytest.approx(0.02)
    assert result["best_spread"]["symbol"] == "BTC/USDT"


def test_compare_fail_open_per_symbol():
    service = VenueCompareService(htx_client=_FakeHTX(), kraken_client=_FakeKraken(fail=True))
    result = service.compare(["BTC/USDT"], use_cache=False)
    assert result["errors"] == 1
    assert "error" in result["items"][0]
    assert result["status"] == "error"  # все символы упали
    assert result["best_spread"] is None


def test_prediction_pct_fallback_to_absolute():
    # bulk /tickers: предикт абсолютный (USD на контракт) → делим на mark.
    entry = {"info": {"fundingRatePrediction": "0.06395"}}
    assert _prediction_pct(entry, mark=63950.0) == pytest.approx(0.0001, abs=1e-6)
    # Относительный ключ приоритетнее абсолютного.
    both = {"info": {"relativeFundingRatePrediction": "0.000045", "fundingRatePrediction": "999"}}
    assert _prediction_pct(both, mark=100.0) == pytest.approx(0.0045)
    # Нет mark → абсолютный не интерпретируем.
    assert _prediction_pct(entry, mark=None) is None


def _fake_payload(spread_ann: float, direction: str = "short_htx_long_kraken", break_even: float = 5.0):
    return {
        "status": "ok",
        "items": [
            {
                "symbol": "ARB/USDT",
                "price_diff_pct": 0.01,
                "spread": {
                    "spread_annualized_pct": spread_ann,
                    "direction": direction,
                    "break_even_days": break_even,
                    "htx": {"annualized_pct": 10.95},
                    "kraken": {"annualized_pct": 10.95 - spread_ann},
                },
            }
        ],
    }


def test_spread_history_append_and_aggregate(tmp_path):
    import time as _time

    path = str(tmp_path / "spread.jsonl")
    history = VenueSpreadHistory(path=path)
    now = _time.time()
    # Старый снапшот (за окном 7д) + три свежих с доминирующим направлением 2/3.
    assert history.append(_fake_payload(30.0), ts=now - 10 * 86400)
    assert history.append(_fake_payload(18.0), ts=now - 3600 * 3)
    assert history.append(_fake_payload(12.0), ts=now - 3600 * 2)
    assert history.append(_fake_payload(-4.0, direction="short_kraken_long_htx"), ts=now - 3600)

    result = history.history(days=7)
    assert result["snapshots"] == 3  # старый отфильтрован
    row = result["by_symbol"][0]
    assert row["symbol"] == "ARB/USDT"
    assert row["snapshots"] == 3
    assert row["avg_spread_ann_pct"] == pytest.approx((18.0 + 12.0 - 4.0) / 3, abs=0.01)
    assert row["dominant_direction"] == "short_htx_long_kraken"
    assert row["direction_stability_pct"] == pytest.approx(66.7, abs=0.1)
    assert row["last_direction"] == "short_kraken_long_htx"
    assert result["daily"], "должна быть дневная серия"


def test_spread_history_empty_and_broken_lines(tmp_path):
    path = str(tmp_path / "spread.jsonl")
    history = VenueSpreadHistory(path=path)
    # Пустой файл/нет файла → пустая история без ошибок.
    empty = history.history(days=7)
    assert empty["snapshots"] == 0 and empty["by_symbol"] == []
    # Битая строка не валит чтение.
    history.append(_fake_payload(10.0))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("{broken json\n")
    ok = history.history(days=7)
    assert ok["snapshots"] == 1
    # Payload без items → append честно возвращает False.
    assert history.append({"status": "ok", "items": []}) is False


def test_fetch_funding_rates_filters_symbols_before_ccxt(monkeypatch):
    # (#kraken-tickers-fix-2026-07-19) ccxt должен получать СПИСОК существующих
    # символов — иначе парсит все тикеры, включая истёкшие FI_* без рынка, и падает.
    from services.kraken_client import KrakenClient

    client = KrakenClient.__new__(KrakenClient)  # без ccxt-инициализации

    class _FakeExchange:
        def __init__(self):
            self.called_with = "NOT_CALLED"

        def fetch_funding_rates(self, symbols):
            self.called_with = symbols
            return {s: {"fundingRate": 0.0001} for s in symbols}

    client.exchange = _FakeExchange()
    markets = {"BTC/USD:USD": {"swap": True}, "ETH/USD:USD": {"swap": True}, "FI_LEGACY": {"swap": False}}
    monkeypatch.setattr(client, "load_markets", lambda: markets)

    # Несуществующий символ отрезан до ccxt.
    result = client.fetch_funding_rates(["BTC/USD:USD", "NOPE/USD:USD"])
    assert client.exchange.called_with == ["BTC/USD:USD"]
    assert list(result) == ["BTC/USD:USD"]

    # Без аргумента — только перпы из карты рынков, никогда None.
    client.fetch_funding_rates()
    assert sorted(client.exchange.called_with) == ["BTC/USD:USD", "ETH/USD:USD"]

    # Ни одного валидного символа → {} без похода в ccxt.
    client.exchange.called_with = "NOT_CALLED"
    assert client.fetch_funding_rates(["NOPE/USD:USD"]) == {}
    assert client.exchange.called_with == "NOT_CALLED"


def test_kraken_cached_markets_rebuild_markets_by_id():
    # (#kraken-markets-by-id-2026-07-19) Инжект кэша обязан строить markets_by_id,
    # иначе safe_market внутри ccxt падает «NoneType is not iterable» у второго
    # и последующих инстансов клиента.
    from services.kraken_client import KrakenClient

    saved = KrakenClient._cached_markets
    try:
        # Структура как у реального ccxt-рынка: set_markets требует spot/type и пр.
        KrakenClient._cached_markets = {
            "BTC/USD:USD": {
                "id": "PF_XBTUSD",
                "symbol": "BTC/USD:USD",
                "base": "BTC",
                "quote": "USD",
                "settle": "USD",
                "type": "swap",
                "spot": False,
                "swap": True,
                "future": False,
                "option": False,
                "contract": True,
                "active": True,
                "precision": {},
                "limits": {},
                "info": {},
            },
        }
        client = KrakenClient()
        client.load_markets()
        assert getattr(client.exchange, "markets_by_id", None), "markets_by_id должен быть построен"
        assert "PF_XBTUSD" in client.exchange.markets_by_id
    finally:
        KrakenClient._cached_markets = saved


def test_grid_net_realized_roundtrip_adapter():
    # (#grid-roundtrip-fix-2026-07-19) Адаптер к _net_realized: tp/sl-путь с
    # готовым gross и hedged-путь (gross сам, выход по обеим ногам).
    from services.grid_engine import GridEngine

    eng = GridEngine.__new__(GridEngine)  # без тяжёлой инициализации
    fee = 0.001  # GRID_FEE_ROUND_PCT=0.1 по умолчанию

    filled = [{"volume": 100.0, "price": 0.32, "side": "buy"}]
    r = eng._net_realized_roundtrip(filled, 0.33, "long", 100.0, 1.0)
    expected = 1.0 - 100.0 * 0.32 * fee - 100.0 * 0.33 * fee
    assert r == pytest.approx(expected)

    hedged = [
        {"volume": 100.0, "price": 0.32, "side": "buy"},
        {"volume": 100.0, "price": 0.34, "side": "sell"},
    ]
    r2 = eng._net_realized_roundtrip(hedged, 0.33)
    gross = (0.33 - 0.32) * 100.0 + (0.34 - 0.33) * 100.0  # 2.0 — локированный PnL
    expected2 = gross - (100.0 * 0.32 + 100.0 * 0.34) * fee - 200.0 * 0.33 * fee
    assert r2 == pytest.approx(expected2)


def test_log_snapshot_uses_history(tmp_path):
    path = str(tmp_path / "spread.jsonl")
    service = VenueCompareService(htx_client=_FakeHTX(), kraken_client=_FakeKraken())
    result = service.log_snapshot(history=VenueSpreadHistory(path=path))
    assert result["logged"] is True
    assert VenueSpreadHistory(path=path).history(days=1)["snapshots"] == 1
