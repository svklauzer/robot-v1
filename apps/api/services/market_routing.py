"""Куда идёт конкретная сделка: рынок, биржевой символ, плечо.

Рынок — свойство СДЕЛКИ, а не глобальная настройка. Лонг исполняется на споте
(купил монету — держишь монету), шорт возможен только на деривативе. Поэтому
одна пара живёт на двух рынках одновременно, и у них разные:

    комиссия      spot taker 0.2%   vs   swap taker 0.05%
    символ ccxt   BTC/USDT          vs   BTC/USDT:USDT
    единица qty   базовая монета    vs   КОНТРАКТЫ (contractSize из метаданных)
    фандинг       нет                    есть, каждые 8 ч

Пока рынок брался из одного `settings.execution_market_type`, эти два набора
смешивались: при ENABLE_FUTURES_EXECUTION=true экономика считалась по свопу
(0.05%), а ордер по символу `BTC/USDT` уходил на спот и стоил 0.2% — вчетверо
дороже. Разрыв не виден ни в одном отчёте: расходятся не числа, а смысл чисел.

Резолвер вызывается один раз при построении плана, результат кладётся в
`Signal.plan_json.routing` и дальше читается всеми — сопровождением, выходом,
закрытием. Сделка обязана закрываться на том же рынке, где открылась, даже
если настройки за это время поменялись.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from core.config import settings


@dataclass(frozen=True)
class TradeRoute:
    market_type: str        # spot | swap
    exchange_symbol: str    # символ для ccxt на этом рынке
    base_symbol: str        # унифицированная пара, как её знает система
    side: str               # long | short
    leverage: int
    margin_mode: str | None
    reason: str

    def as_dict(self) -> dict:
        return asdict(self)


def _swap_symbol(symbol: str) -> str:
    """BTC/USDT → BTC/USDT:USDT. Уже контрактный символ отдаём как есть."""
    if ":" in symbol:
        return symbol
    base, _, quote = symbol.partition("/")
    if not quote:
        return symbol
    return f"{base}/{quote}:{quote}"


def _spot_symbol(symbol: str) -> str:
    return symbol.split(":", 1)[0]


def resolve(symbol: str, side: str) -> TradeRoute:
    """Рынок для конкретного входа.

    long  → спот, если он разрешён; иначе своп (лонг перпом тоже допустим).
    short → только дериватив: на споте продать то, чего нет, нельзя.
    """
    side_value = str(side or "").lower()
    is_short = side_value in ("short", "sell")

    futures_available = bool(getattr(settings, "ENABLE_FUTURES", False))
    prefer_futures = bool(getattr(settings, "ENABLE_FUTURES_EXECUTION", False))

    if is_short:
        if not futures_available:
            raise ValueError("short_requires_futures:ENABLE_FUTURES=false")
        return TradeRoute(
            market_type="swap",
            exchange_symbol=_swap_symbol(symbol),
            base_symbol=_spot_symbol(symbol),
            side="short",
            leverage=max(int(getattr(settings, "FUTURES_LEVERAGE", 1)), 1),
            margin_mode=str(getattr(settings, "TREND_MARGIN_MODE", "isolated")),
            reason="short_only_on_derivative",
        )

    if prefer_futures and futures_available:
        return TradeRoute(
            market_type="swap",
            exchange_symbol=_swap_symbol(symbol),
            base_symbol=_spot_symbol(symbol),
            side="long",
            leverage=max(int(getattr(settings, "FUTURES_LEVERAGE", 1)), 1),
            margin_mode=str(getattr(settings, "TREND_MARGIN_MODE", "isolated")),
            reason="long_on_derivative_by_config",
        )

    return TradeRoute(
        market_type="spot",
        exchange_symbol=_spot_symbol(symbol),
        base_symbol=_spot_symbol(symbol),
        side="long",
        leverage=1,
        margin_mode=None,
        reason="long_on_spot",
    )


def from_payload(payload: dict | None, symbol: str, side: str) -> TradeRoute:
    """Маршрут ранее открытой сделки из `plan_json.routing`.

    Сделка обязана сопровождаться и закрываться по тому рынку, где открылась:
    иначе смена настроек на лету переоценит открытую позицию по чужой комиссии,
    а выход уйдёт не на ту биржевую площадку. Нет записи (старые сигналы) —
    восстанавливаем по стороне сделки.
    """
    routing = (payload or {}).get("routing") if isinstance(payload, dict) else None
    if isinstance(routing, dict) and routing.get("market_type"):
        return TradeRoute(
            market_type=str(routing["market_type"]),
            exchange_symbol=str(routing.get("exchange_symbol") or symbol),
            base_symbol=str(routing.get("base_symbol") or _spot_symbol(symbol)),
            side=str(routing.get("side") or side),
            leverage=int(routing.get("leverage") or 1),
            margin_mode=routing.get("margin_mode"),
            reason=str(routing.get("reason") or "restored_from_signal"),
        )
    return resolve(symbol, side)
