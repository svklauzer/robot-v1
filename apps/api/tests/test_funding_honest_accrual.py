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
"""
import pytest

from core.config import settings


def _funding(notional, entry_rate, exit_rate, periods, honest):
    """Формула начисления из close_paper."""
    if honest and exit_rate is not None:
        rate = (entry_rate + exit_rate) / 2.0
    else:
        rate = entry_rate
    return notional * rate * periods


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


def test_close_paper_uses_the_honest_rate():
    """Регресс на само место правки: формула обязана читать exit_funding_rate."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "services" / "funding_arbitrage.py"
    text = src.read_text(encoding="utf-8")
    block = text[text.index("def close_paper"):text.index("def close_hedge")]

    assert "FUNDING_ARB_HONEST_ACCRUAL" in block
    assert "exit_funding_rate" in block and "effective_rate" in block
    assert "entry_funding_rate) * int(funding_periods)" not in block, (
        "начисление снова считается только по ставке входа"
    )
