"""Честное начисление funding (#funding-honest-accrual-2026-07-27).

Внутрибиржевой funding-arb начислял доход по ставке ВХОДА за весь срок
удержания, игнорируя фактическую динамику. `exit_funding_rate` при этом
записывался в базу, но в формуле PnL не участвовал.

Смещение систематическое и в одну сторону: движок входит только на аномально
высокой ставке (эффективный порог ≈60% годовых), а funding rate mean-reverts —
значит фактическая средняя за срок почти всегда НИЖЕ входной.

Косвенное подтверждение: cross-arb (P2) начисляет carry pro-rata по ТЕКУЩЕЙ
ставке и показывает −1.20 USDT; этот движок по ставке входа — +4.02.
Один принцип, разный знак, разница ровно в методике учёта.

ОБНОВЛЕНО 03.08.2026 (#funding-periodic-accrual-2026-08-03)
-----------------------------------------------------------
Трапеция была промежуточным решением: среднее двух крайних точек равно
интегралу только на линейном участке. Теперь carry копится пер-периодно по
фактической ставке (services/funding_accrual.py), а трапеция осталась запасным
путём для позиций, открытых до правки, — у них ledger'а нет и взяться ему
неоткуда.

Проверки ниже сохранены: запасной путь тоже обязан считать правильно. Но
формула больше не дублируется в тесте — вызывается рабочая функция, иначе тест
проверял бы собственную копию логики, а не код.
"""
import pytest

from core.config import settings
from services.funding_accrual import collected_usdt


def _funding(notional, entry_rate, exit_rate, periods, honest):
    """Запасной путь начисления — тот самый, что сработает на старой позиции."""
    value, _method = collected_usdt(
        None,
        notional=notional,
        entry_rate=entry_rate,
        exit_rate=exit_rate,
        periods=periods,
        honest_trapezoid=honest,
    )
    return value


def test_falling_rate_is_no_longer_overstated():
    """Типовой сценарий: вошли на пике 0.055%, ставка вернулась к базовой 0.01%."""
    old = _funding(100.0, 0.055, 0.010, 10, honest=False)
    new = _funding(100.0, 0.055, 0.010, 10, honest=True)

    assert old == pytest.approx(55.0 / 100 * 100 * 0.01 * 0 + 0.055 * 100 * 10)
    assert new < old, "падение ставки обязано уменьшать начисление"
    assert new == pytest.approx((0.055 + 0.010) / 2 * 100 * 10)


def test_stable_rate_changes_nothing():
    """Если ставка не менялась — расчёт прежний, регресса нет."""
    old = _funding(100.0, 0.05, 0.05, 8, honest=False)
    new = _funding(100.0, 0.05, 0.05, 8, honest=True)

    assert new == pytest.approx(old)


def test_rising_rate_is_credited_too():
    """Симметрия: рост ставки должен увеличивать начисление, а не только падение
    уменьшать — иначе это не честность, а односторонний штраф."""
    old = _funding(100.0, 0.03, 0.03, 5, honest=False)
    new = _funding(100.0, 0.03, 0.07, 5, honest=True)

    assert new > old


def test_missing_exit_rate_falls_back_safely():
    """Нет ставки выхода — считаем как раньше (fail-safe, не роняем закрытие)."""
    assert _funding(100.0, 0.04, None, 6, honest=True) == pytest.approx(
        _funding(100.0, 0.04, None, 6, honest=False)
    )


def test_flag_can_restore_previous_behaviour():
    assert isinstance(settings.FUNDING_ARB_HONEST_ACCRUAL, bool)
    assert _funding(100.0, 0.06, 0.01, 10, honest=False) == pytest.approx(0.06 * 100 * 10)


def test_measured_carry_wins_over_any_estimate():
    """Главное правило: если carry измерен — оценки не применяются.

    Трапеция ближе к правде, чем ставка входа, но обе остаются приближениями.
    Ledger — факт, и он обязан иметь приоритет.
    """
    from services import funding_accrual as fa

    ledger = fa.accrue(fa.empty_ledger(0.0, 0.055), notional=100.0,
                       current_rate=0.010, now_ts=10 * 8 * 3600.0)
    measured, method = collected_usdt({"accrual": ledger}, notional=100.0,
                                      entry_rate=0.055, exit_rate=0.010, periods=10)

    assert method == "per_period"
    trapezoid = _funding(100.0, 0.055, 0.010, 10, honest=True)
    assert measured != pytest.approx(trapezoid)


def test_close_paper_delegates_instead_of_computing_carry_itself():
    """Регресс на место правки.

    Проверяется не название переменной, а то, что расчёт carry вынесен из
    close_paper: пока формула жила прямо здесь, она дважды разъезжалась с
    реальностью незаметно. Плюс прямой запрет на возврат к ставке входа.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "services" / "funding_arbitrage.py"
    text = src.read_text(encoding="utf-8")
    block = text[text.index("def close_paper"):text.index("def close_hedge")]

    assert "collected_usdt" in block, "close_paper обязан делегировать расчёт carry"
    assert "accrual_method" in block, "способ учёта обязан записываться в сделку"
    assert "entry_funding_rate) * int(funding_periods)" not in block, (
        "начисление снова считается только по ставке входа"
    )
    assert "(entry_rate + exit_rate) / 2" not in block, (
        "трапеция вернулась в close_paper вместо запасного пути в funding_accrual"
    )
