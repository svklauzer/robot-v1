"""Патч: свечи и тикер со СВОПА (ключ MARKET_DATA_TYPE).

Зачем: в ccxt символ BTC/USDT — это СПОТ. MarketDataService запрашивал свечи и
тикер по такому символу, а исполнение, стакан и стопы идут по своп-контрактам.
Теперь при MARKET_DATA_TYPE=swap (и бирже OKX) свечи/тикер/paper-цены берутся с
BTC/USDT:USDT. Символ-ключ в системе остаётся прежним.

Что меняет
  config.py          : MARKET_DATA_TYPE: str = "spot"  (по умолчанию поведение прежнее)
  render.yaml        : MARKET_DATA_TYPE = "swap"
  market_data.py     : подмена символа в одном месте (snapshot / ticker_snapshot / ohlcv);
                       только OKX; символ без свопа -> откат на спот с предупреждением;
                       сетевые ошибки НЕ маскируются спотом; в ответ добавлен "data_market".
  tests/test_market_data_swap.py : новые тесты.
Откат: MARKET_DATA_TYPE=spot (одна строка в render.yaml/дашборде), без правки кода.

Запуск из корня репозитория: python scripts/patch_market_data_swap.py
Идемпотентен; .bak-before-market-swap; кодировка и переводы строк сохраняются.
"""
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
CFG = ROOT / "apps/api/core/config.py"
MD = ROOT / "apps/api/services/market_data.py"
RENDER = ROOT / "render.yaml"
TEST = ROOT / "apps/api/tests/test_market_data_swap.py"
TEST_SRC = '"""MarketDataService.MARKET_DATA_TYPE: свечи и тикер берутся с того же рынка, на\nкотором торгуем (#market-data-swap-2026-10-25).\n\nВ ccxt символ BTC/USDT — это СПОТ, своп — BTC/USDT:USDT. Без подмены индикаторы и\npaper-цены считались по споту, а исполнялось и стоп-контролировалось на свопе.\n"""\nfrom __future__ import annotations\n\nfrom unittest.mock import MagicMock\n\nfrom core.config import settings\nfrom services.market_data import MarketDataService\n\n\ndef _client(last=100.0):\n    stub = MagicMock()\n    stub.fetch_ticker.return_value = {"last": last, "bid": last - 0.1, "ask": last + 0.1}\n    stub.fetch_ohlcv.return_value = [[i, 1, 1, 1, 1, 1] for i in range(200)]\n    return stub\n\n\ndef _svc(monkeypatch, *, exchange="okx", market="swap"):\n    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", exchange, raising=False)\n    monkeypatch.setattr(settings, "MARKET_DATA_TYPE", market, raising=False)\n    s = MarketDataService(exchange=exchange)\n    s.client = _client()\n    return s\n\n\ndef test_default_spot_keeps_symbol(monkeypatch):\n    s = _svc(monkeypatch, market="spot")\n    s.ticker_snapshot("BTC/USDT")\n    s.client.fetch_ticker.assert_called_with("BTC/USDT")\n\n\ndef test_swap_okx_requests_contract_symbol_everywhere(monkeypatch):\n    s = _svc(monkeypatch)\n    snap = s.snapshot("BTC/USDT")\n    s.client.fetch_ticker.assert_called_with("BTC/USDT:USDT")\n    s.client.fetch_ohlcv.assert_called_with("BTC/USDT:USDT", "5m", 200)\n    assert snap["symbol"] == "BTC/USDT"          # ключ системы не меняется\n    assert snap["data_market"] == "swap"\n    assert s.ticker_snapshot("ETH/USDT")["data_market"] == "swap"\n    s.client.fetch_ticker.assert_called_with("ETH/USDT:USDT")\n    s.ohlcv("SOL/USDT", timeframe="15m", limit=250)\n    s.client.fetch_ohlcv.assert_called_with("SOL/USDT:USDT", timeframe="15m", limit=250)\n\n\ndef test_swap_is_okx_only(monkeypatch):\n    s = _svc(monkeypatch, exchange="htx")\n    s.ticker_snapshot("BTC/USDT")\n    s.client.fetch_ticker.assert_called_with("BTC/USDT")\n\n\ndef test_contract_symbol_not_doubled(monkeypatch):\n    s = _svc(monkeypatch)\n    s.ticker_snapshot("BTC/USDT:USDT")\n    s.client.fetch_ticker.assert_called_with("BTC/USDT:USDT")\n\n\ndef test_unknown_value_means_spot(monkeypatch):\n    s = _svc(monkeypatch, market="swapp")\n    s.ticker_snapshot("BTC/USDT")\n    s.client.fetch_ticker.assert_called_with("BTC/USDT")\n\n\ndef test_symbol_without_swap_falls_back_to_spot(monkeypatch):\n    class BadSymbol(Exception):\n        pass\n\n    s = _svc(monkeypatch)\n    calls = []\n\n    def fetch(sym):\n        calls.append(sym)\n        if sym.endswith(":USDT"):\n            raise BadSymbol("okx does not have market symbol NEW/USDT:USDT")\n        return {"last": 1.0, "bid": 0.9, "ask": 1.1}\n\n    s.client.fetch_ticker.side_effect = fetch\n    assert s.ticker_snapshot("NEW/USDT")["last"] == 1.0\n    assert calls == ["NEW/USDT:USDT", "NEW/USDT"]\n\n\ndef test_network_error_is_not_masked_by_spot(monkeypatch):\n    import pytest\n\n    s = _svc(monkeypatch)\n    s.client.fetch_ticker.side_effect = TimeoutError("net")\n    with pytest.raises(TimeoutError):\n        s.ticker_snapshot("BTC/USDT")\n    assert s.client.fetch_ticker.call_count == 1\n'


def load(p):
    raw = p.read_bytes().decode("utf-8")
    return raw.replace("\r\n", "\n"), "\r\n" in raw, raw


def save(p, text, crlf, raw):
    p.with_name(p.name + ".bak-before-market-swap").write_bytes(raw.encode("utf-8"))
    p.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))


def once(text, old, new, label):
    n = text.count(old)
    if n != 1:
        sys.exit(f"[{label}] якорь найден {n} раз(а), ожидался 1 - файлы не менялись (проверка до записи)")
    return text.replace(old, new)


# ── подготовка всех правок в памяти ДО записи (всё или ничего) ────────────────
plans = []

t, crlf, raw = load(CFG)
if "MARKET_DATA_TYPE" in t:
    print("config.py: уже применено")
else:
    t = once(t, '    OB_MARKET_TYPE: str = "spot"\n',
        '    OB_MARKET_TYPE: str = "spot"\n'
        '    # (#market-data-swap-2026-10-25) Рынок, с которого берутся СВЕЧИ и ТИКЕР\n'
        '    # (MarketDataService): индикаторы, цена входа и paper-выходы. Исполнение,\n'
        '    # стакан и стопы идут по своп-контрактам, а символ BTC/USDT в ccxt — это СПОТ,\n'
        '    # поэтому сигналы считались по одному инструменту, а торговались по другому.\n'
        '    # spot — прежнее поведение (дефолт, ничего не меняет); swap — BTC/USDT →\n'
        '    # BTC/USDT:USDT. Только для OKX; символ без свопа откатывается на спот с\n'
        '    # предупреждением в логе. Любое другое значение читается как spot.\n'
        '    MARKET_DATA_TYPE: str = "spot"\n', "config")
    plans.append((CFG, t, crlf, raw))

t, crlf, raw = load(RENDER)
if "MARKET_DATA_TYPE" in t:
    print("render.yaml: уже применено")
else:
    t = once(t, '      - key: OB_MARKET_TYPE\n        value: "swap"\n',
        '      - key: OB_MARKET_TYPE\n        value: "swap"\n'
        '      # (#market-data-swap-2026-10-25) Свечи и тикер — со СВОПА, как исполнение и\n'
        '      # стакан: BTC/USDT в ccxt это спот. Откат — значение "spot", без деплоя кода.\n'
        '      - key: MARKET_DATA_TYPE\n        value: "swap"\n', "render")
    plans.append((RENDER, t, crlf, raw))

t, crlf, raw = load(MD)
if "_data_symbol" in t:
    print("market_data.py: уже применено")
else:
    t = once(t, "from core.config import settings\n",
        "from core.config import settings\n"
        "from core.logging import get_logger, log_event\n\n"
        "logger = get_logger(__name__)\n"
        "_SWAP_MISSING: set[str] = set()   # символы без свопа: предупреждаем один раз\n"
        "_SOURCE_LOGGED = False\n", "md-import")
    t = once(t, "        self.exchange_name = resolve_exchange_name(exchange)\n",
        "        self.exchange_name = resolve_exchange_name(exchange)\n\n"
        "    # (#market-data-swap-2026-10-25) Рынок данных. ccxt: BTC/USDT — спот,\n"
        "    # BTC/USDT:USDT — своп. Торгуем и стоп-контролируем на свопе, значит и\n"
        "    # свечи/цена должны быть оттуда. Только OKX; прочее — спот как раньше.\n"
        "    def _data_market(self) -> str:\n"
        "        if self.exchange_name != \"okx\":\n"
        "            return \"spot\"\n"
        "        value = str(getattr(settings, \"MARKET_DATA_TYPE\", \"spot\") or \"spot\").lower().strip()\n"
        "        return \"swap\" if value == \"swap\" else \"spot\"\n\n"
        "    def _data_symbol(self, symbol: str) -> str:\n"
        "        if self._data_market() != \"swap\" or \":\" in symbol:\n"
        "            return symbol\n"
        "        from services.market_routing import _swap_symbol\n\n"
        "        return _swap_symbol(symbol)\n\n"
        "    def _call(self, method: str, symbol: str, *args, **kwargs):\n"
        "        global _SOURCE_LOGGED\n"
        "        if not _SOURCE_LOGGED:\n"
        "            _SOURCE_LOGGED = True\n"
        "            log_event(logger, 20, \"market_data_source\", exchange=self.exchange_name,\n"
        "                      data_market=self._data_market())\n"
        "        fn = getattr(self.client, method)\n"
        "        data_symbol = self._data_symbol(symbol)\n"
        "        if data_symbol == symbol:\n"
        "            return fn(symbol, *args, **kwargs)\n"
        "        try:\n"
        "            return fn(data_symbol, *args, **kwargs)\n"
        "        except Exception as exc:  # noqa: BLE001\n"
        "            # Откат на спот ТОЛЬКО если у символа нет свопа. Сетевые и прочие\n"
        "            # ошибки не маскируем спотом: это снова смешало бы два инструмента.\n"
        "            if type(exc).__name__ != \"BadSymbol\" and \"does not have market symbol\" not in str(exc):\n"
        "                raise\n"
        "            if symbol not in _SWAP_MISSING:\n"
        "                _SWAP_MISSING.add(symbol)\n"
        "                log_event(logger, 30, \"market_data_swap_missing\", symbol=symbol,\n"
        "                          data_symbol=data_symbol, fallback=\"spot\")\n"
        "            return fn(symbol, *args, **kwargs)\n", "md-helpers")
    t = once(t, "            ticker = self.client.fetch_ticker(symbol)\n            ohlcv = self.client.fetch_ohlcv(symbol, \"5m\", 200)\n",
        "            ticker = self._call(\"fetch_ticker\", symbol)\n            ohlcv = self._call(\"fetch_ohlcv\", symbol, \"5m\", 200)\n", "md-snapshot")
    t = once(t, "                \"ohlcv\": df,\n                \"source\": self.exchange_name,\n",
        "                \"ohlcv\": df,\n                \"source\": self.exchange_name,\n                \"data_market\": self._data_market(),\n", "md-snapshot-ret")
    t = once(t, "        ticker = self.client.fetch_ticker(symbol)\n        return {\n            \"symbol\": symbol,\n            \"last\": ticker.get(\"last\"),\n            \"bid\": ticker.get(\"bid\"),\n            \"ask\": ticker.get(\"ask\"),\n            \"source\": self.exchange_name,\n",
        "        ticker = self._call(\"fetch_ticker\", symbol)\n        return {\n            \"symbol\": symbol,\n            \"last\": ticker.get(\"last\"),\n            \"bid\": ticker.get(\"bid\"),\n            \"ask\": ticker.get(\"ask\"),\n            \"source\": self.exchange_name,\n            \"data_market\": self._data_market(),\n", "md-ticker")
    t = once(t, "            data = self.client.fetch_ohlcv(\n                symbol,\n                timeframe=timeframe,\n                limit=limit,\n            )\n",
        "            data = self._call(\"fetch_ohlcv\", symbol, timeframe=timeframe, limit=limit)\n", "md-ohlcv")
    plans.append((MD, t, crlf, raw))

for p, t, crlf, raw in plans:
    save(p, t, crlf, raw)
    print(f"{p.relative_to(ROOT)}: ok")

if TEST.exists():
    print("tests/test_market_data_swap.py: уже есть")
else:
    TEST.parent.mkdir(parents=True, exist_ok=True)
    TEST.write_bytes(TEST_SRC.encode("utf-8"))
    print("tests/test_market_data_swap.py: создан")
