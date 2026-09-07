"""Размер уровня сетки выводится из конверта, а не задаётся числом
(#grid-envelope-sizing-2026-09-07).

Что было
--------
`GRID_BASE_ORDER_USDT = 20` — константа, ничего не знавшая о конверте капитала.
Лестница строилась от неё, а конверт проверялся дважды и оба раза не за то:

  * при открытии — «влезает ли БАЗОВЫЙ ордер» (`free < base_usdt/lev`);
  * при филле каждого уровня — «не переполнен ли карман», и если переполнен,
    цикл просто перестаёт добирать.

Арифметика на боевых настройках (equity 900 → конверт 5% = 45 USDT,
`GRID_LINES=6`, `GRID_VOL_MULTIPLIER=1.2`, `GRID_MAX_SAFETY_ORDERS=2`):

    уровни одной стороны   20.0 · 24.0 · 28.8 · 34.6 · 41.5 · 49.8 = 198.6 USDT
    база + 2 страховочных  20.0 + 24.0 + 28.8                      =  72.8 USDT
    конверт                                                          45.0 USDT

То есть цикл открывался заведомо недофинансированным и обрывался на втором
уровне. Для НЕЙТРАЛЬНОЙ корзины это худший из возможных исходов: она задумана
двусторонней и примерно дельта-нейтральной, а обрыв филлов оставляет случайную
направленную позицию — ровно то, от чего сетку защищают все остальные правила.

Дефект не в размере конверта: при любом конверте лестница, посчитанная от
константы, либо не влезет, либо оставит деньги незанятыми.

Как теперь
----------
Как это устроено у OKX: задаётся ВЛОЖЕНИЕ, а размер уровня выводится из него.
Здесь то же самое — бюджет символа делится на «стоимость лестницы при единичном
базовом объёме», и получается базовый объём, при котором лестница расходует
бюджет целиком и ровно.

Стоимость берётся из самих уровней, а не из формулы мартингейла: у нейтральной
корзины прогрессия идёт по каждой стороне отдельно, и переписанная от руки сумма
разошлась бы с `compute_grid` при первой же правке раскладки.

Порог биржи
-----------
Масштабирование вниз упирается в минимальный ордер, и это не теория: на
OKX-своп один контракт BTC — 0.01 BTC, ETH — 0.1 ETH, то есть сотни USDT на
уровень. При конверте 45 USDT своп-сетка недостижима в принципе, а спотовая
проходит — и именно так у OKX и устроено: спотовая сетка лонговая и мелкая,
фьючерсная двусторонняя и крупная.

Поэтому лестница проверяется на минимумы площадки ДО открытия цикла, а не
обнаруживает их отказом ордера. Когда биржа лимитов не отдала, работает
консервативный пол `GRID_MIN_LEVEL_USDT`: неизвестный минимум — не то же самое,
что отсутствующий.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LadderPlan:
    """Во что обходится лестница и можно ли её вообще выставить."""

    budget_usdt: float
    # Объём базовой монеты для первого уровня. Все прочие получаются из него
    # мартингейлом внутри compute_grid.
    v_base_qty: float
    ladder_cost_usdt: float
    levels: int
    smallest_level_usdt: float
    smallest_level_qty: float
    largest_level_usdt: float
    # Причина отказа или None. Строкой, а не флагом: она попадает в состояние
    # сетки и на экран, и «не открылась» без причины — это ровно тот молчаливый
    # отказ, из-за которого гейты и становятся необъяснимыми.
    blocked: str | None = None

    @property
    def ok(self) -> bool:
        return self.blocked is None and self.v_base_qty > 0.0

    def as_dict(self) -> dict:
        return {
            "budget_usdt": round(self.budget_usdt, 6),
            "v_base_qty": round(self.v_base_qty, 10),
            "ladder_cost_usdt": round(self.ladder_cost_usdt, 6),
            "levels": self.levels,
            "smallest_level_usdt": round(self.smallest_level_usdt, 6),
            "smallest_level_qty": round(self.smallest_level_qty, 10),
            "largest_level_usdt": round(self.largest_level_usdt, 6),
            "blocked": self.blocked,
        }


def unit_ladder_cost(levels: list[dict]) -> float:
    """Стоимость лестницы в USDT при базовом объёме в одну монету.

    Объёмы уровней линейны по `v_base`, поэтому масштабирование бюджета —
    простое деление. Считаем по самим уровням: у нейтральной корзины прогрессия
    идёт по каждой стороне отдельно, и переписанная от руки формула разошлась бы
    с раскладкой при первой же правке.
    """
    total = 0.0
    for lv in levels or []:
        try:
            total += float(lv["volume"]) * float(lv["price"])
        except (KeyError, TypeError, ValueError):
            continue
    return total


def plan_ladder(unit_levels: list[dict], *, budget_usdt: float,
                min_level_usdt: float = 5.0,
                min_cost: float | None = None,
                min_amount: float | None = None,
                limits_available: bool = True) -> LadderPlan:
    """Базовый объём, при котором лестница ровно расходует бюджет.

    `unit_levels` — раскладка, посчитанная при `v_base=1.0`.
    """
    unit_cost = unit_ladder_cost(unit_levels)
    count = len(unit_levels or [])

    if count == 0 or unit_cost <= 0.0:
        return LadderPlan(budget_usdt=budget_usdt, v_base_qty=0.0, ladder_cost_usdt=0.0,
                          levels=count, smallest_level_usdt=0.0, smallest_level_qty=0.0,
                          largest_level_usdt=0.0, blocked="ladder_empty")

    if budget_usdt <= 0.0:
        return LadderPlan(budget_usdt=budget_usdt, v_base_qty=0.0, ladder_cost_usdt=0.0,
                          levels=count, smallest_level_usdt=0.0, smallest_level_qty=0.0,
                          largest_level_usdt=0.0, blocked="envelope_empty")

    v_base = budget_usdt / unit_cost
    costs = []
    qtys = []
    for lv in unit_levels:
        try:
            qty = float(lv["volume"]) * v_base
            costs.append(qty * float(lv["price"]))
            qtys.append(qty)
        except (KeyError, TypeError, ValueError):
            continue

    smallest_usdt = min(costs) if costs else 0.0
    smallest_qty = min(qtys) if qtys else 0.0
    largest_usdt = max(costs) if costs else 0.0

    blocked = None
    # Порядок проверок — от самого достоверного к запасному. Лимит площадки
    # знает точную правду; собственный пол лишь страхует, когда её нет.
    if min_cost is not None and smallest_usdt < float(min_cost):
        blocked = f"below_venue_min_cost:{smallest_usdt:.4f}<{float(min_cost):.4f}"
    elif min_amount is not None and smallest_qty < float(min_amount):
        blocked = f"below_venue_min_amount:{smallest_qty:.8f}<{float(min_amount):.8f}"
    elif not limits_available and smallest_usdt < float(min_level_usdt):
        blocked = (f"below_min_level_usdt:{smallest_usdt:.4f}<{float(min_level_usdt):.4f}"
                   ":limits_unknown")

    return LadderPlan(
        budget_usdt=float(budget_usdt),
        v_base_qty=v_base,
        ladder_cost_usdt=float(budget_usdt) if blocked is None else unit_cost * v_base,
        levels=count,
        smallest_level_usdt=smallest_usdt,
        smallest_level_qty=smallest_qty,
        largest_level_usdt=largest_usdt,
        blocked=blocked,
    )


def symbol_budget(envelope_usdt: float, symbols: list[str], used_usdt: float = 0.0) -> float:
    """Доля конверта на один символ.

    Делится на ВСЮ вселенную сетки, а не на свободный остаток: иначе первый
    открывшийся символ забирает конверт целиком, а второй не открывается уже
    никогда. У OKX то же — вложение задаётся на бота, а не на очередь.
    """
    count = max(1, len([s for s in symbols if str(s).strip()]))
    free = max(0.0, float(envelope_usdt) - float(used_usdt))
    per_symbol = float(envelope_usdt) / count
    return min(per_symbol, free)
