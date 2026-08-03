"""Объём в контрактах: бумага и live должны совпадать (#contract-quantize-2026-08-03).

Из боевого лога:

    htx amount of BTC/USDT:USDT must be greater than minimum amount precision of 1
    amount = 0.002979083696572486

Округление звалось с базовым символом "BTC/USDT", но ccxt при defaultType=swap
резолвит его в контрактный рынок, где точность = 1 контракт. Базовый объём
округлялся в ноль → исключение → фолбэк возвращал объём нетронутым.

Шум в логах был меньшей из бед. Live отправлял 2 контракта (0.002 BTC), а
бумага книжила план (0.002979 BTC) — на 49% больше. При цене 63650 это
127.3 USDT против 189.6: расхождение не в издержках, а в размере позиции.
На ETH шаг мельче (0.01 ≈ 19 USDT), там расхождение 1.8% — потому баг и жил
незамеченным, он виден только на дорогих монетах.
"""
from __future__ import annotations

import pytest

from services.live_executor import LiveExecutor

# Размеры контрактов HTX linear-swap.
CONTRACT_SIZES = {
    "BTC/USDT:USDT": 0.001,
    "ETH/USDT:USDT": 0.01,
    "SOL/USDT:USDT": 1.0,
    "ADA/USDT:USDT": 10.0,
}


class _FakeClient:
    """Биржа, принимающая своп только целыми контрактами."""

    def contract_size(self, symbol):
        return CONTRACT_SIZES.get(symbol)

    def amount_to_precision(self, symbol, amount):
        if symbol in CONTRACT_SIZES:
            if float(amount) < 1:
                raise ValueError(
                    f"htx amount of {symbol} must be greater than "
                    f"minimum amount precision of 1"
                )
            return float(int(float(amount)))
        return round(float(amount), 6)


@pytest.fixture
def ex():
    executor = LiveExecutor()
    executor.client = _FakeClient()
    return executor


# ── воспроизведение боевого случая ──────────────────────────────────────────
def test_btc_from_the_live_log():
    """Ровно те 0.002979 BTC из ошибки."""
    executor = LiveExecutor()
    executor.client = _FakeClient()
    base, meta = executor.quantize_base("BTC/USDT:USDT", 0.002979083696572486, "swap")

    assert meta["contracts"] == 2.0
    assert base == pytest.approx(0.002)
    # Недобор трети позиции — это и есть цена грубого шага контракта.
    assert meta["shortfall_pct"] == pytest.approx(32.9, abs=0.2)


def test_eth_from_the_live_log(ex):
    """ETH из второй строки лога: шаг мельче, расхождение почти незаметно."""
    base, meta = ex.quantize_base("ETH/USDT:USDT", 0.10184609447029311, "swap")
    assert meta["contracts"] == 10.0
    assert base == pytest.approx(0.1)
    assert meta["shortfall_pct"] < 2.0


# ── свойства округления ─────────────────────────────────────────────────────
def test_quantized_amount_never_exceeds_request(ex):
    """Округление только вниз: превысить план нельзя, это вышло бы за риск."""
    for symbol, amount in (
        ("BTC/USDT:USDT", 0.00456), ("ETH/USDT:USDT", 0.1999),
        ("SOL/USDT:USDT", 2.7), ("ADA/USDT:USDT", 1029.0),
    ):
        base, _ = ex.quantize_base(symbol, amount, "swap")
        assert base <= amount + 1e-12, symbol


def test_below_one_contract_returns_zero(ex):
    """Позиция меньше шага биржи — открывать нечем, и это должно быть видно."""
    base, meta = ex.quantize_base("BTC/USDT:USDT", 0.0004, "swap")
    assert base == 0.0
    assert meta["contracts"] == 0.0


def test_failed_precision_never_leaves_fractional_contracts(ex):
    """Причина, по которой предыдущий тест падал.

    В `_to_exchange_amount` стояло `except: pass`, и при отказе точности
    наружу уходило дробное число контрактов — 0.4. Значение положительное,
    поэтому предохранитель `send_amount <= 0` его пропускал, и ордер уезжал
    на биржу только чтобы вернуться отказом. Дробных контрактов быть не может
    ни при каких обстоятельствах.
    """
    for amount in (0.0004, 0.0019, 0.00299):
        contracts, _ = ex._to_exchange_amount("BTC/USDT:USDT", amount, "swap")
        assert contracts == int(contracts), amount
        assert contracts >= 0, amount


def test_spot_is_left_alone(ex):
    """У спота контрактов нет — объём в монетах и остаётся."""
    base, meta = ex.quantize_base("BTC/USDT", 0.002979, "spot")
    assert meta["submitted_unit"] == "base"
    assert base == pytest.approx(0.002979, abs=1e-6)


def test_exact_multiples_lose_nothing(ex):
    base, meta = ex.quantize_base("SOL/USDT:USDT", 2.0, "swap")
    assert base == pytest.approx(2.0)
    assert meta["shortfall_pct"] == pytest.approx(0.0)


def test_unknown_contract_size_is_reported_not_guessed(ex):
    """Угадывать размер контракта нельзя: ошибка кратна позиции, а не мала."""
    base, meta = ex.quantize_base("DOGE/USDT:USDT", 100.0, "swap")
    assert "error" in meta
    assert meta["error"].startswith("contract_size_unknown")


# ── что именно чинилось ─────────────────────────────────────────────────────
def test_paper_and_live_now_agree_on_size(ex):
    """Суть правки: обе ветки берут ОДИН объём.

    Раньше бумага книжила план, а биржа принимала округлённое — и бумажный
    результат по BTC был несопоставим с live не из-за издержек, а из-за того,
    что позиции были разного размера.
    """
    planned = 0.002979083696572486
    booked_by_paper, _ = ex.quantize_base("BTC/USDT:USDT", planned, "swap")
    sent_to_exchange, meta = ex._to_exchange_amount("BTC/USDT:USDT", planned, "swap")
    assert booked_by_paper == pytest.approx(sent_to_exchange * meta["contract_size"])
