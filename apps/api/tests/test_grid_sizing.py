"""Размер уровня сетки выводится из конверта (#grid-envelope-sizing-2026-09-07).

`GRID_BASE_ORDER_USDT = 20` не знал о конверте капитала. На боевых настройках
(equity 900 → конверт 5% = 45 USDT, GRID_LINES=6, m_vol=1.2) лестница одной
стороны стоит 198.6 USDT, а база плюс два страховочных — 72.8. Цикл при этом
открывался, если хватало на БАЗОВЫЙ ордер, и обрывался на втором уровне.

Для нейтральной корзины обрыв филлов — худший исход: она задумана двусторонней
и примерно дельта-нейтральной, а недобранная сторона оставляет случайную
направленную позицию.
"""
from __future__ import annotations

import pytest

from services.grid_sizing import LadderPlan, plan_ladder, symbol_budget, unit_ladder_cost


def _unit_ladder(price: float = 2500.0, per_side: int = 3, m_vol: float = 1.2,
                 sides: tuple[str, ...] = ("buy", "sell")) -> list[dict]:
    """Раскладка при базовом объёме в одну монету — как её отдаёт compute_grid."""
    levels: list[dict] = []
    for side in sides:
        volume = 1.0
        for i in range(1, per_side + 1):
            offset = 0.01 * i
            levels.append({
                "side": side,
                "price": price * (1 - offset) if side == "buy" else price * (1 + offset),
                "volume": volume,
            })
            volume *= m_vol
    return levels


# ── бюджет расходуется целиком и ровно ──────────────────────────────────────

def test_the_ladder_spends_the_budget_exactly():
    """Суть правки. Раньше стоимость лестницы задавалась константой и с
    конвертом не сходилась ни при каком его размере."""
    plan = plan_ladder(_unit_ladder(), budget_usdt=22.5)

    assert plan.ok
    assert plan.ladder_cost_usdt == pytest.approx(22.5)


def test_scaling_is_linear_in_both_directions():
    """Ради этого расчёт и заводится: доля конверта сжалась — уровень уменьшился,
    расширилась — вырос, без правки настроек."""
    small = plan_ladder(_unit_ladder(), budget_usdt=22.5)
    large = plan_ladder(_unit_ladder(), budget_usdt=225.0)

    assert large.v_base_qty == pytest.approx(small.v_base_qty * 10.0)
    assert large.smallest_level_usdt == pytest.approx(small.smallest_level_usdt * 10.0)


def test_cost_is_read_from_the_levels_not_from_a_formula():
    """У нейтральной корзины мартингейл идёт по КАЖДОЙ стороне отдельно.
    Переписанная от руки сумма прогрессии разошлась бы с раскладкой при первой
    же правке `compute_grid` — и разошлась бы молча.
    """
    one_side = _unit_ladder(sides=("buy",))
    both_sides = _unit_ladder()

    assert unit_ladder_cost(both_sides) > unit_ladder_cost(one_side)
    # Цены сторон разные (ниже и выше якоря), поэтому ровно вдвое не выйдет —
    # что и показывает, почему считать надо по уровням.
    assert unit_ladder_cost(both_sides) != pytest.approx(unit_ladder_cost(one_side) * 2)


# ── пороги площадки ─────────────────────────────────────────────────────────

def test_a_swap_contract_minimum_blocks_the_whole_ladder():
    """Не теория: на OKX-свопе один контракт ETH — 0.1 ETH, это ~250 USDT на
    уровень. При конверте 45 USDT своп-сетка недостижима в принципе, и узнать
    об этом надо ДО открытия цикла, а не отказом ордера."""
    plan = plan_ladder(_unit_ladder(), budget_usdt=22.5, min_cost=250.0)

    assert not plan.ok
    assert plan.blocked.startswith("below_venue_min_cost")


def test_a_spot_minimum_lets_the_same_ladder_through():
    """Та же лестница на споте проходит — и именно так устроено у OKX: спотовая
    сетка мелкая и лонговая, фьючерсная крупная и двусторонняя."""
    plan = plan_ladder(_unit_ladder(), budget_usdt=22.5, min_cost=1.0)

    assert plan.ok, plan.blocked


def test_minimum_amount_is_checked_too():
    """Минимум бывает задан не деньгами, а количеством базовой монеты."""
    plan = plan_ladder(_unit_ladder(), budget_usdt=22.5, min_amount=1.0)

    assert not plan.ok
    assert plan.blocked.startswith("below_venue_min_amount")


def test_unknown_limits_are_not_permission():
    """Биржа не отдала лимиты — это не «минимума нет». Работает собственный пол,
    и причина прямо говорит, что лимиты неизвестны: иначе отказ выглядел бы
    измеренным."""
    blind = plan_ladder(_unit_ladder(), budget_usdt=22.5,
                        min_level_usdt=5.0, limits_available=False)

    assert not blind.ok
    assert blind.blocked.endswith(":limits_unknown")

    known = plan_ladder(_unit_ladder(), budget_usdt=22.5,
                        min_level_usdt=5.0, min_cost=1.0, limits_available=True)
    assert known.ok, "с известным лимитом площадки собственный пол не применяется"


def test_the_reason_is_a_sentence_not_a_flag():
    """Причина попадает в состояние сетки и на экран. «Не открылась» без числа —
    ровно тот молчаливый отказ, из-за которого гейты становятся необъяснимыми.
    """
    plan = plan_ladder(_unit_ladder(), budget_usdt=22.5, min_cost=250.0)

    assert "<" in plan.blocked and "250" in plan.blocked
    assert plan.as_dict()["blocked"] == plan.blocked


# ── вырожденные случаи ──────────────────────────────────────────────────────

def test_an_empty_envelope_is_refused_not_divided_by():
    assert plan_ladder(_unit_ladder(), budget_usdt=0.0).blocked == "envelope_empty"


def test_an_empty_ladder_does_not_explode():
    assert plan_ladder([], budget_usdt=45.0).blocked == "ladder_empty"


def test_broken_levels_are_skipped_not_counted_as_free():
    """Уровень без цены не должен обнулять стоимость лестницы и делать бюджет
    бесконечным."""
    levels = _unit_ladder() + [{"side": "buy", "volume": 1.0}]

    assert unit_ladder_cost(levels) == pytest.approx(unit_ladder_cost(_unit_ladder()))


# ── деление конверта между символами ────────────────────────────────────────

def test_the_envelope_is_split_across_the_whole_universe():
    """Делится на всю вселенную, а не на свободный остаток: иначе первый
    открывшийся символ забирает конверт целиком, а второй не откроется никогда.
    """
    assert symbol_budget(45.0, ["BTC/USDT", "ETH/USDT"]) == pytest.approx(22.5)


def test_a_symbol_cannot_claim_more_than_is_free():
    """Доля посчитана от полного конверта, но занять больше свободного нельзя."""
    assert symbol_budget(45.0, ["BTC/USDT", "ETH/USDT"], used_usdt=35.0) == pytest.approx(10.0)


def test_an_empty_universe_does_not_divide_by_zero():
    assert symbol_budget(45.0, []) == pytest.approx(45.0)
    assert symbol_budget(45.0, ["", "  "]) == pytest.approx(45.0)


def test_plan_reports_the_numbers_the_screen_needs():
    plan = plan_ladder(_unit_ladder(), budget_usdt=22.5, min_cost=1.0)
    shown = plan.as_dict()

    for field in ("budget_usdt", "v_base_qty", "ladder_cost_usdt", "levels",
                  "smallest_level_usdt", "largest_level_usdt", "blocked"):
        assert field in shown, f"нечего показать на экране: нет {field}"
    assert isinstance(plan, LadderPlan)
