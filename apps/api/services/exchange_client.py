"""(#okx-satellite-2026-09-02) Формальный контракт "биржевой клиент".

HTXClient и OKXClient — независимые, вручную написанные реализации (тот же
идиом, что уже использует KrakenClient относительно HTXClient), НЕ подклассы
общего базового класса. Этот Protocol не меняет их наследование и не имеет
рантайм-эффекта — это только структурная типизация: единое место, где виден
весь торговый контракт, чтобы отсутствие/несовпадение сигнатуры метода в
OKXClient ловилось при чтении/тайпчеке, а не посреди боевой сделки.

KrakenClient сюда НЕ подходит: он read-only (нет create_order/set_leverage/
fetch_balance) — намеренно другой, меньший контракт для чисто наблюдательной
роли (#okx-satellite plan, раздел 1).
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ExchangeClient(Protocol):
    def load_markets(self) -> dict: ...

    def fetch_balance(self, params: dict | None = None): ...

    def fetch_ticker(self, symbol: str): ...

    def fetch_ohlcv(self, symbol: str, timeframe: str = "5m", limit: int = 200): ...

    def fetch_open_orders(self, symbol: str | None = None): ...

    def fetch_positions(self): ...

    def fetch_funding_rate(self, symbol: str): ...

    def fetch_mark_price(self, symbol: str) -> float: ...

    def create_market_order(
        self, symbol: str, side: str, amount: float, params: dict | None = None
    ): ...

    def create_order_once(
        self,
        symbol: str,
        type_: str,
        side: str,
        amount: float,
        price: float | None = None,
        params: dict | None = None,
    ): ...

    def set_leverage(self, leverage: float, symbol: str, params: dict | None = None): ...

    def set_margin_mode(self, margin_mode: str, symbol: str, params: dict | None = None): ...

    def fetch_closed_orders(self, symbol: str | None = None, limit: int = 20): ...

    def cancel_order(self, order_id: str, symbol: str): ...

    def fetch_order(self, order_id: str, symbol: str): ...

    def price_to_precision(self, symbol: str, price: float) -> float: ...

    def amount_to_precision(self, symbol: str, amount: float) -> float: ...

    def contract_size(self, symbol: str) -> float | None: ...

    def market_limits(self, symbol: str) -> dict: ...

    def fetch_trading_fee(self, symbol: str) -> dict: ...

    def trading_fee_rates(self, symbol: str, market_type: str | None = None) -> dict: ...
