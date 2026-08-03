"""Ввод условий ТЗ в бой (#tz-enforce-2026-08-03).

Первый замер по боевым сетапам: ADX 16.1 / 18.0 / 19.4 при пороге ТЗ 23.
Ни один не достиг порога. Enforce на 23 — это не «стать разборчивее», это
остановить трендовый контур настройкой. Отсюда предохранители, которые здесь
и проверяются: без выборки enforce не включается, а условия с некалиброванным
порогом можно держать в тени отдельно от беспороговых.
"""
from __future__ import annotations

import pytest

from core.config import settings
from services import tz_entry_shadow as tz


def _shadow(*, failed=(), evaluated=True):
    return tz.TZShadow(
        regime="trend_down_candidate", side="short", evaluated=evaluated,
        would_pass=not failed, failed=tuple(failed),
        adx=19.4, di_spread=11.1, stoch_k=15.5, stoch_d=39.7, obv_vs_ema=-11188.0,
    )


@pytest.fixture
def enforce(monkeypatch):
    monkeypatch.setattr(settings, "TZ_MODE", "enforce", raising=False)
    monkeypatch.setattr(settings, "TZ_ENFORCE_MIN_SAMPLE", 40, raising=False)
    monkeypatch.setattr(settings, "TZ_ENFORCE_CONDITIONS", "di,obv", raising=False)


# ── предохранитель по выборке ───────────────────────────────────────────────
def test_small_sample_never_blocks(enforce):
    """Три наблюдения — не выборка. Порог по ним = подгонка под другим именем."""
    blocked, reason = tz.should_block(_shadow(failed=["di_against_side:-2.0"]),
                                      sample_size=3)
    assert blocked is False
    assert reason == "sample_too_small:3<40"


def test_blocks_once_the_sample_is_there(enforce):
    blocked, reason = tz.should_block(_shadow(failed=["di_against_side:-2.0"]),
                                      sample_size=40)
    assert blocked is True
    assert reason == "blocked_by:di"


# ── частичный enforce ───────────────────────────────────────────────────────
def test_uncalibrated_adx_stays_in_shadow_by_default(enforce):
    """ADX не в списке включённых — сам по себе вход не режет.

    Это и есть ответ на замер: порог 23 не пройден ни разу, поэтому условие
    считается и записывается, но решение не принимает.
    """
    blocked, reason = tz.should_block(_shadow(failed=["adx_below_min:19.4"]),
                                      sample_size=100)
    assert blocked is False
    assert reason == "enabled_conditions_passed"


def test_threshold_free_conditions_may_block(enforce):
    """У di и obv порога нет вовсе — они сравнивают величины между собой."""
    for code, family in (("di_against_side:-3.0", "di"), ("obv_above_ema", "obv")):
        blocked, reason = tz.should_block(_shadow(failed=[code]), sample_size=100)
        assert blocked is True, code
        assert reason == f"blocked_by:{family}"


def test_adx_alongside_enabled_condition_does_not_mask_it(enforce):
    blocked, reason = tz.should_block(
        _shadow(failed=["adx_below_min:19.4", "obv_above_ema"]), sample_size=100
    )
    assert blocked is True
    assert reason == "blocked_by:obv"


def test_stoch_can_be_switched_on_explicitly(enforce, monkeypatch):
    monkeypatch.setattr(settings, "TZ_ENFORCE_CONDITIONS", "stoch", raising=False)
    blocked, _ = tz.should_block(_shadow(failed=["stoch_not_in_pullback:15.5"]),
                                 sample_size=100)
    assert blocked is True


# ── fail-open ───────────────────────────────────────────────────────────────
def test_shadow_mode_blocks_nothing(monkeypatch):
    """Режим выставляем ЯВНО.

    Раньше тест полагался на дефолт конфига, и когда 03.08 дефолт сменился на
    enforce, тест начал падать — правильно начал. Тест про поведение в shadow
    обязан задавать shadow сам, иначе он проверяет значение настройки, а не
    логику.
    """
    monkeypatch.setattr(settings, "TZ_MODE", "shadow", raising=False)
    blocked, reason = tz.should_block(_shadow(failed=["di_against_side:-2.0"]),
                                      sample_size=1000)
    assert blocked is False
    assert reason == "mode_shadow"


def test_enforce_is_the_default_now(monkeypatch):
    """Дефолт — enforce: трендовые движки дают 96% убытка, полумеры кончились."""
    monkeypatch.setattr(settings, "TZ_ENFORCE_MIN_SAMPLE", 0, raising=False)
    monkeypatch.setattr(settings, "TZ_ENFORCE_CONDITIONS", "kama,adx,di,stoch,obv",
                        raising=False)
    blocked, _ = tz.should_block(_shadow(failed=["price_below_kama"]), sample_size=0)
    assert blocked is True


def test_missing_indicators_do_not_block(enforce):
    """Нет данных — не повод не пускать: иначе сбой расчёта тихо гасит движок."""
    blocked, reason = tz.should_block(_shadow(evaluated=False), sample_size=1000)
    assert blocked is False
    assert reason == "not_evaluated"


def test_empty_condition_list_blocks_nothing(enforce, monkeypatch):
    monkeypatch.setattr(settings, "TZ_ENFORCE_CONDITIONS", "", raising=False)
    blocked, reason = tz.should_block(_shadow(failed=["di_against_side:-2.0"]),
                                      sample_size=1000)
    assert blocked is False
    assert reason == "no_conditions_enabled"


def test_clean_setup_passes(enforce):
    blocked, reason = tz.should_block(_shadow(failed=[]), sample_size=1000)
    assert blocked is False
    assert reason == "enabled_conditions_passed"


def test_every_condition_has_a_family():
    """Условие вне таблицы семейств не блокирует НИЧЕГО — молча.

    _family() вернёт "unknown", оно не совпадёт ни с одним включённым
    семейством, и вход пройдёт. Условие при этом будет исправно считаться и
    писаться в план, то есть выглядеть работающим. Этот тест — единственное,
    что не даст добавить новое условие и забыть про таблицу.
    """
    known = set(tz.CONDITION_FAMILY.values())
    assert known == {"kama", "adx", "di", "stoch", "obv"}
    for code, family in tz.CONDITION_FAMILY.items():
        assert tz._family(code) == family, code
        assert tz._family(f"{code}:12.3") == family, code
    assert tz._family("something_new") == "unknown"
