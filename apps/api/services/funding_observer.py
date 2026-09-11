"""Наблюдение ставок фандинга по биржам — только чтение (#okx-funding-2026-09-12).

Оба арбитражных контура выключены 04.09 по экономике, и с тех пор ставки не
копятся вовсе: журнал пишет только скан внутрибиржевого арбитража, а он ходит
лишь при `ENABLE_FUNDING_ARB=true`. Пересчитать экономику, из-за которой
контуры выключали, не по чему — и тем более её нечем посчитать для OKX.

Этот цикл снимает ставку и базис по каждой бирже из `FUNDING_OBSERVE_VENUES`
и пишет в тот же журнал с меткой биржи. Никаких позиций, ордеров и строк в
базе: торговлю он не трогает при любом значении выключателей арбитража.
"""
from __future__ import annotations

from core.config import settings


def observe_venues() -> list[str]:
    raw = str(getattr(settings, "FUNDING_OBSERVE_VENUES", "htx,okx") or "")
    return [v.strip().lower() for v in raw.split(",") if v.strip()]


def observe_once(venues: list[str] | None = None,
                 symbols: list[str] | None = None) -> dict:
    """Один проход: снимок ставки и базиса по каждой паре (биржа, символ).

    Снимок сам пишет наблюдение в журнал с меткой биржи. Сбой одной биржи или
    символа не останавливает остальные.
    """
    from services.funding_arbitrage import FundingMonitorService

    venues = venues if venues is not None else observe_venues()
    symbols = symbols if symbols is not None else list(settings.funding_arb_symbols)
    recorded: dict[str, int] = {}
    errors: list[dict] = []
    for venue in venues:
        try:
            monitor = FundingMonitorService(venue=venue)
        except Exception as exc:  # noqa: BLE001 — одна биржа не валит проход
            errors.append({"venue": venue, "symbol": None,
                           "error": f"{type(exc).__name__}: {exc}"})
            continue
        recorded[venue] = 0
        for symbol in symbols:
            try:
                monitor.snapshot(symbol)
                recorded[venue] += 1
            except Exception as exc:  # noqa: BLE001
                errors.append({"venue": venue, "symbol": symbol,
                               "error": f"{type(exc).__name__}: {exc}"})
    return {"recorded": recorded, "errors": errors}
