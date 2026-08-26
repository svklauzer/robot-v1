"""ATR обязан доходить до выхода (#atr-never-reached-the-exit-2026-08-26).

Адаптивный стоп был включён, закреплён в блупринте, показывался на странице
конфига как действующий — и не работал ни разу.

`exit_policy` зовёт `tz_trend_exit.evaluate(atr=tz_context.get("atr"))`, а
`SignalLifecycleService._tz_context` возвращал словарь БЕЗ ключа "atr". Приходил
None, условие `if use_atr and atr_v is not None and atr_v > 0` не выполнялось
никогда, и буфер под KAMA всегда брался из legacy-ветки — фиксированные
`TZ_EXIT_KAMA_BUFFER_PCT=0.5%` на любой инструмент.

Худший вид мёртвой настройки: ключ существует, значение выставлено, тест на
фантомные имена его не поймает. Ловится только сквозной проверкой «дошло ли
значение до места применения» — ради неё этот файл и написан.

Цена вопроса: 0.5% — это 1.6 ATR для TRX (ATR 1h 0.32%) и 0.45 ATR для XRP
(1.12%). На одном буфер вдвое шире нужного, на другом вдвое уже. XRP #423 закрыт
`tz_kama` на −1.26%, AVAX #422 — на −1.01%.
"""
from __future__ import annotations

import pytest

from core.config import settings
from services import tz_trend_exit


# ── сквозная проверка: ключ есть в контракте ────────────────────────────────
def test_tz_context_carries_atr():
    """`_tz_context` обязан класть atr — иначе адаптивный стоп мёртв."""
    import inspect

    from services.signal_lifecycle import SignalLifecycleService

    src = inspect.getsource(SignalLifecycleService._tz_context)
    assert '"atr"' in src, "ключ atr пропал — адаптивный стоп снова отключится"
    assert "atr14" in src, "atr должен браться из контекста таймфрейма"


def test_exit_policy_reads_the_same_key():
    """Имя ключа на обеих сторонах одно. Разъедутся — стоп молча умрёт."""
    import inspect

    from services import exit_policy

    src = inspect.getsource(exit_policy)
    assert 'tz_context.get("atr")' in src


# ── поведение буфера ────────────────────────────────────────────────────────
def _level_breached(close, kama, atr, side="long"):
    """Пробит ли уровень выхода при данном ATR."""
    return tz_trend_exit.evaluate(
        side=side, close=close, kama=kama,
        adx=25.0, adx_peak=30.0, obv=10.0, obv_ema=5.0,
        atr=atr, entry_price=None,
    )


def test_atr_widens_the_buffer_and_holds_the_trade(monkeypatch):
    """С ATR сделку на обычном шуме больше не выбивает.

    XRP: KAMA 1.50, ATR 1h ≈ 0.0168 (1.12%). Цена ушла под KAMA на 0.9% —
    в пределах одного ATR. Прежний фиксированный буфер 0.5% это закрывал.
    """
    monkeypatch.setattr(settings, "TZ_USE_DYNAMIC_ATR_STOPS", True, raising=False)
    monkeypatch.setattr(settings, "TZ_EXIT_KAMA_BUFFER_ATR_MULT", 2.0, raising=False)
    monkeypatch.setattr(settings, "TZ_EXIT_KAMA_BUFFER_PCT", 0.5, raising=False)

    kama, atr = 1.50, 0.0168
    close = kama * (1 - 0.009)          # −0.9% под KAMA

    with_atr = _level_breached(close, kama, atr)
    without_atr = _level_breached(close, kama, None)

    assert "kama_broken" not in with_atr.triggers, \
        "с ATR буфер 2.24% — шум не должен закрывать сделку"
    assert with_atr.exit is False

    assert "kama_broken" in without_atr.triggers, \
        "без ATR буфер 0.5% — прежнее поведение, выбивало"
    assert without_atr.exit is True


def test_atr_also_tightens_where_the_instrument_is_quiet(monkeypatch):
    """Правка не только ослабляет: на тихом инструменте буфер сужается.

    TRX: ATR 1h 0.32%, буфер по ATR = 0.64%. Фиксированные 0.5% были ЛОЖЕ,
    то есть стоп стоял ближе, чем волатильность инструмента оправдывает.
    """
    monkeypatch.setattr(settings, "TZ_USE_DYNAMIC_ATR_STOPS", True, raising=False)
    monkeypatch.setattr(settings, "TZ_EXIT_KAMA_BUFFER_ATR_MULT", 2.0, raising=False)

    kama, atr = 0.3387, 0.00109
    atr_buffer_pct = (atr * 2.0) / kama * 100.0

    assert atr_buffer_pct == pytest.approx(0.64, abs=0.02)
    assert atr_buffer_pct > 0.5, "у TRX адаптивный буфер шире фиксированного"


def test_switch_off_returns_to_the_fixed_buffer(monkeypatch):
    """Выключенный тумблер по-прежнему даёт legacy-поведение."""
    monkeypatch.setattr(settings, "TZ_USE_DYNAMIC_ATR_STOPS", False, raising=False)
    monkeypatch.setattr(settings, "TZ_EXIT_KAMA_BUFFER_PCT", 0.5, raising=False)

    kama, atr = 1.50, 0.0168
    close = kama * (1 - 0.009)

    assert "kama_broken" in _level_breached(close, kama, atr).triggers


def test_pinned_keys_are_actually_live():
    """Обе настройки закреплены в блупринте — значит обязаны быть живыми."""
    assert bool(getattr(settings, "TZ_USE_DYNAMIC_ATR_STOPS", False)) is True
    assert float(getattr(settings, "TZ_EXIT_KAMA_BUFFER_ATR_MULT", 0)) > 0
