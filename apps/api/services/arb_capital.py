"""Нотионал арбитража от ФАКТИЧЕСКОГО капитала (#arb-capital-2026-08-03).

Что было
--------
Оба арбитражных движка брали размер из абсолютной константы:

    funding_arbitrage:  FUNDING_ARB_DEFAULT_NOTIONAL_USDT = 100
    cross_funding_arb:  CROSS_FARB_NOTIONAL_USDT          = 100

Депозит в расчёте не участвовал вообще. При балансе 200 арбитраж открыл бы
позицию на половину счёта, при 20 000 — те же 100 и простаивал бы. Направленные
движки устроены иначе: `MAX_POSITION_MARGIN_PCT` это доля эквити, а эквити
берётся из `LIVE_EXECUTOR.effective_equity_usdt()`, то есть в live — реальный
свободный баланс. Арбитражи из этой схемы просто выпали.

Как теперь
----------
Нотионал = доля капитала на момент открытия. Один и тот же процент работает и
на бумажных 950, и на реальном балансе в live без правок конфига.

Сколько КАПИТАЛА занимает позиция — зависит от движка, и это не одно и то же:

    внутрибиржевой   спот-лонг + своп-шорт  → ~2 × нотионал
                     (спотовая нога фондируется целиком, плеча на ней нет)
    межбиржевой      своп на HTX + своп на Kraken → ~2 × нотионал
                     (маржа на каждой площадке отдельно)

Поэтому доля 10% при двух одновременных позициях означает примерно 40% капитала,
а не 20%. Это учтено в дефолтах: они подобраны так, чтобы при текущих 950 размер
совпал с прежней константой 100 — механизм меняется, поведение нет.

Ограничения по краям: не меньше биржевого минимума, не больше явного потолка.
"""
from __future__ import annotations

from core.config import settings


def available_equity() -> float:
    """Капитал здесь и сейчас. В live — свободные USDT со счетов, в бумаге —
    RISK_EQUITY_USDT. Любой сбой чтения → бумажный дефолт, а не ноль: нулевой
    капитал остановил бы движок молча."""
    fallback = float(getattr(settings, "RISK_EQUITY_USDT", 950.0))
    try:
        from services.live_executor import LIVE_EXECUTOR

        equity = float(LIVE_EXECUTOR.effective_equity_usdt())
    except Exception:  # noqa: BLE001 — размер позиции не на крит-пути импорта
        return fallback
    return equity if equity > 0 else fallback


def notional_from_share(
    *,
    share_key: str,
    share_default: float,
    min_key: str,
    min_default: float,
    max_key: str,
    max_default: float,
    equity: float | None = None,
) -> float:
    """Доля капитала, зажатая между минимумом и потолком."""
    equity = float(equity if equity is not None else available_equity())
    share = float(getattr(settings, share_key, share_default))
    floor = float(getattr(settings, min_key, min_default))
    ceiling = float(getattr(settings, max_key, max_default))
    value = equity * max(0.0, share)
    return round(max(floor, min(value, ceiling)), 2)


def funding_arb_notional(equity: float | None = None, db=None) -> float:
    """Нотионал одной ноги внутрибиржевого хеджа (спот-лонг = своп-шорт).

    (#capital-envelopes-2026-08-21) Размер выводится из КОНВЕРТА контура, а не
    из собственной доли эквити. Раньше `FUNDING_ARB_NOTIONAL_PCT=10.5%` жил
    отдельно от всего, и два хеджа занимали ~42% депозита (каждый ≈2 нотионала:
    спотовая нога без плеча). Конверт и доля были двумя числами про одно и то
    же — ровно та конструкция, которая уже расходилась в
    `_assumed_hold_periods`. Второго числа больше нет.

    Пол и потолок биржи по-прежнему обязательны: доля может дать сумму ниже
    минимального лота.
    """
    from services.capital_envelopes import arb_leg_notional

    value = arb_leg_notional(equity=equity, db=db)
    floor = float(getattr(settings, "FUNDING_ARB_MIN_NOTIONAL_USDT", 20.0))
    ceiling = float(getattr(settings, "FUNDING_ARB_MAX_NOTIONAL_USDT", 500.0))
    return round(max(floor, min(value, ceiling)), 2)


def cross_farb_notional(equity: float | None = None) -> float:
    """Нотионал одной ноги межбиржевой пары (шорт HTX = лонг Kraken)."""
    return notional_from_share(
        share_key="CROSS_FARB_NOTIONAL_PCT", share_default=0.105,
        min_key="CROSS_FARB_MIN_NOTIONAL_USDT", min_default=20.0,
        max_key="CROSS_FARB_MAX_NOTIONAL_USDT", max_default=500.0,
        equity=equity,
    )
