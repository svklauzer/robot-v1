"""Конверты капитала: у каждого контура своя доля эквити, сумма ≤ 100%.

Что было (#capital-envelopes-2026-08-21)
---------------------------------------
Три контура претендовали на один и тот же депозит НЕЗАВИСИМО, ничего друг о
друге не зная:

    направленные   equity × 70%                        = 665
    funding arb    2 хеджа × (equity × 10.5%) × 2 ноги ≈ 400
    grid           equity × 5%                         = 47.5
                                                  итого ≈ 1112 при капитале 950

Связи между ними не существовало: `exposure_guard.used_margin()` перебирает
только `Signal` (направленные сделки) и не видит ни `FundingArbPosition`, ни
циклы сетки. В бумаге безвредно — `effective_equity_usdt()` возвращает
константу и не сжимается. В live отказ по марже получил бы не «лишний» контур,
а тот, кто открылся последним, то есть случайный.

Как теперь
----------
Доля задаётся явно на контур, а размеры позиций ВЫВОДЯТСЯ из доли, а не
подбираются отдельно. Инвариант «сумма ≤ 100%» становится структурным: его
нельзя нарушить, подкрутив один параметр, потому что второго параметра нет.

Освобождение долей
------------------
Выключенный контур отдаёт свою долю направленным — но ТОЛЬКО если он ничего не
держит. Разница принципиальная: «выключён» и «уже пуст» — разные состояния.
У сетки они расходятся штатно: при `GRID_KILL_SWITCH_ENABLED=true` новые
корзины не открываются, а существующие продолжают обслуживаться (TP/стоп),
чтобы маржа не заперлась в брошенных. Отдать её долю в этот момент — значит
дважды пообещать одни и те же деньги.

Без доступа к БД считаем контур «держащим» (консервативно): лучше недодать
направленным, чем пообещать больше, чем есть.
"""
from __future__ import annotations

from core.config import settings

DIRECTIONAL = "directional"
ARB = "arb"
GRID = "grid"


def _pct(name: str, default: float) -> float:
    try:
        return max(0.0, float(getattr(settings, name, default)))
    except (TypeError, ValueError):
        return default


def configured_shares() -> dict[str, float]:
    """Заданные доли (в процентах эквити), без учёта активности контуров."""
    return {
        DIRECTIONAL: _pct("CAPITAL_ENVELOPE_DIRECTIONAL_PCT", 70.0),
        ARB: _pct("CAPITAL_ENVELOPE_ARB_PCT", 20.0),
        GRID: _pct("CAPITAL_ENVELOPE_GRID_PCT", 5.0),
    }


def arb_enabled() -> bool:
    return bool(getattr(settings, "ENABLE_FUNDING_ARB", False))


def grid_enabled() -> bool:
    """Сетка может открывать НОВЫЕ корзины.

    Три независимых способа её погасить, и путать их нельзя:
      * `GRID_ENABLED` — конфиг;
      * `GRID_KILL_SWITCH_ENABLED` — стоп-кран: новые не открываем, старые
        доводим до конца;
      * рантайм-тумблер в сторе (кнопки Grid enable/disable на фронте) —
        здесь не проверяется, он живёт в grid_state и меняется на лету.
    """
    if not bool(getattr(settings, "GRID_ENABLED", False)):
        return False
    return not bool(getattr(settings, "GRID_KILL_SWITCH_ENABLED", False))


def _arb_holds(db) -> bool:
    if db is None:
        return True  # неизвестно → считаем занятым
    try:
        from models.funding_arbitrage import FundingArbPosition

        return db.query(FundingArbPosition).filter(
            FundingArbPosition.status == "open"
        ).count() > 0
    except Exception:  # noqa: BLE001 — учёт капитала не должен ронять цикл
        return True


def _grid_holds() -> bool:
    try:
        from services.grid_store import GridStore

        # Стор читает состояние из файла, синглтона у него нет — движок тоже
        # создаёт его на месте (`GridEngine.__init__`).
        return bool(getattr(GridStore(), "cycles", None))
    except Exception:  # noqa: BLE001
        return True


def effective_shares(db=None) -> dict[str, float]:
    """Доли с учётом того, кто выключен И ничего не держит.

    Освободившееся уходит направленным: это единственный контур, который умеет
    занять деньги немедленно и по качеству сетапа, а не по расписанию.
    """
    shares = configured_shares()
    released = 0.0
    detail: dict[str, str] = {}

    if not arb_enabled() and not _arb_holds(db):
        released += shares[ARB]
        detail[ARB] = "выключен и пуст — доля передана направленным"
        shares[ARB] = 0.0
    elif not arb_enabled():
        detail[ARB] = "выключен, но держит позиции — доля сохранена"

    if not grid_enabled() and not _grid_holds():
        released += shares[GRID]
        detail[GRID] = "выключен и пуст — доля передана направленным"
        shares[GRID] = 0.0
    elif not grid_enabled():
        detail[GRID] = "стоп-кран, но корзины живы — доля сохранена"

    shares[DIRECTIONAL] += released
    shares["_released_pct"] = released
    shares["_detail"] = detail  # type: ignore[assignment]
    return shares


def envelope_pct(contour: str, db=None) -> float:
    return float(effective_shares(db).get(contour, 0.0))


def envelope_usdt(contour: str, equity: float | None = None, db=None) -> float:
    """Сколько КАПИТАЛА отведено контуру."""
    if equity is None:
        from services.arb_capital import available_equity

        equity = available_equity()
    return round(float(equity) * envelope_pct(contour, db) / 100.0, 6)


def arb_leg_notional(equity: float | None = None, db=None) -> float:
    """Нотионал ОДНОЙ ноги внутрибиржевого хеджа — из конверта, а не отдельной долей.

    Хедж занимает примерно ДВА нотионала: спотовая нога фондируется целиком,
    плеча на ней нет. При лимите `FUNDING_ARB_MAX_OPEN_HEDGES` одновременных
    хеджей конверт делится так:

        нотионал ноги = конверт / (хеджей × 2)

    Раньше нотионал задавался своей долей (10.5%) независимо от конверта, и два
    хеджа съедали ~42% эквити при «выделенных» 20%. Теперь превысить конверт
    нельзя: второго числа, которое можно рассинхронизировать, просто нет.
    """
    hedges = max(1, int(getattr(settings, "FUNDING_ARB_MAX_OPEN_HEDGES", 2)))
    capital_per_notional = 2.0  # спот + своп
    envelope = envelope_usdt(ARB, equity=equity, db=db)
    return round(envelope / (hedges * capital_per_notional), 2)
