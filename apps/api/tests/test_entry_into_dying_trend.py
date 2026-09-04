"""Не входить в тренд, который на входе УЖЕ затухает
(#tz-enforce-adx-rising-2026-09-03, #anti-chop-young-trend-2026-09-03).

Владелец: «мы постоянно упускаем крупные движения». Разбор боевой выборки
01–03.09.2026 (15 трендовых сделок, HTX+OKX) показал два независимых механизма,
работающих в одну сторону:

1. Вход РАЗРЕШЁН в затухающий тренд. Условие ТЗ «ADX имеет восходящую
   траекторию» считалось, писалось в plan_json и не блокировало ничего: оно
   делило семейство `adx` с некалиброванным абсолютным порогом TZ_ADX_MIN, а
   всё семейство держали в тени именно из-за порога.
       провалившие adx_not_rising: 11 сделок, 2 прибыльных, −9.74 USDT
       не провалившие:              4 сделки, 4 прибыльных, +8.90 USDT
   Все крупные стопы — из первой группы.

2. Вход ЗАПРЕЩЁН в молодой тренд. Веер EMA20↔EMA200 на 1h после V-разворота
   ещё несколько суток держит закончившийся рынок, и гейт anti_chop закрывает
   вход на весь первый отрезок нового движения. Замер 03.09 в один момент:
   ETH fan −0.61, XRP −0.12, SOL +0.22, AVAX +0.22 при пороге 0.80 — при ADX
   24–29 РАСТУЩЕМ и разведённых DI на всех четырёх.

Вместе: систематически берём поздний вход в умирающий тренд и пропускаем
ранний вход в рождающийся.

Выборка маленькая — это основание для правки, а не доказательство эффекта.
Оба механизма снимаются флагом без правки кода.
"""
from __future__ import annotations

import pytest

from core.config import settings
from services import tz_entry_shadow as tz
from services.market_intelligence import MarketIntelligenceEngine


# ── 1. adx_rising отделён от некалиброванного порога ────────────────────────

def _shadow(*, failed=()):
    return tz.TZShadow(
        regime="trend_down_candidate", side="short", evaluated=True,
        would_pass=not failed, failed=tuple(failed),
        adx=26.7, di_spread=7.1, stoch_k=80.3, stoch_d=89.1, obv_vs_ema=-2649944.0,
    )


@pytest.fixture
def enforce(monkeypatch):
    monkeypatch.setattr(settings, "TZ_MODE", "enforce", raising=False)
    monkeypatch.setattr(settings, "TZ_ENFORCE_MIN_SAMPLE", 0, raising=False)
    monkeypatch.setattr(settings, "TZ_ENFORCE_CONDITIONS", "kama,di,obv,adx_rising",
                        raising=False)


def test_adx_rising_is_its_own_family():
    """Склеенное с `adx` семейство было невключаемым: включить производную можно
    было только вместе с некалиброванным абсолютным порогом."""
    assert tz.CONDITION_FAMILY["adx_not_rising"] == "adx_rising"
    assert tz.CONDITION_FAMILY["adx_below_min"] == "adx"


def test_dying_trend_is_now_blocked(enforce):
    """Регресс на ADA #468 (−1.89), SOL #465 (−2.14), AVAX #472 (−2.00) и ещё
    восемь: ADX на входе шёл ВНИЗ, вход всё равно открывался."""
    blocked, reason = tz.should_block(
        _shadow(failed=["adx_not_rising:27.5->26.7"]), sample_size=0,
    )

    assert blocked is True
    assert reason == "blocked_by:adx_rising"


def test_uncalibrated_absolute_adx_threshold_stays_in_shadow(enforce):
    """Порог TZ_ADX_MIN остаётся в тени — иначе правка заодно отрезала бы
    XRP #458 (ADX 12.2, результат +3.65 USDT). Отделение семейств ровно для
    того и сделано."""
    blocked, reason = tz.should_block(
        _shadow(failed=["adx_below_min:12.2"]), sample_size=0,
    )

    assert blocked is False
    assert reason == "enabled_conditions_passed"


def test_rising_trend_still_passes(enforce):
    """Победитель выборки — XRP #461 (+4.09, tp2_reached) — прошёл ВСЕ условия
    ТЗ. Правка не имеет права его отсечь."""
    blocked, reason = tz.should_block(_shadow(failed=[]), sample_size=0)

    assert blocked is False
    assert reason == "enabled_conditions_passed"


def test_enforced_families_parse_the_new_name(monkeypatch):
    monkeypatch.setattr(settings, "TZ_ENFORCE_CONDITIONS", "kama,di,obv,adx_rising",
                        raising=False)
    assert "adx_rising" in tz.enforced_families()
    assert "adx" not in tz.enforced_families()


def test_shipped_default_enforces_adx_rising():
    """Дефолт в коде обязан включать условие. Боевое значение приходит из
    Render env и перекрывает дефолт — но код без env не должен молча
    возвращаться к прежнему поведению."""
    from core.config import Settings

    assert "adx_rising" in Settings().TZ_ENFORCE_CONDITIONS


# ── 2. молодой тренд проходит узкий веер EMA ────────────────────────────────

def _svc():
    return MarketIntelligenceEngine.__new__(MarketIntelligenceEngine)


def _anchor(adx=26.07, adx_prev=23.92, plus_di=40.13, minus_di=11.97):
    return {
        "adx14": adx, "adx14_prev": adx_prev,
        "plus_di": plus_di, "minus_di": minus_di,
    }


@pytest.fixture
def young(monkeypatch):
    monkeypatch.setattr(settings, "ANTI_CHOP_YOUNG_TREND_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "ANTI_CHOP_YOUNG_ADX_MIN", 20.0, raising=False)
    monkeypatch.setattr(settings, "ANTI_CHOP_YOUNG_ADX_RISE_MIN", 0.5, raising=False)
    monkeypatch.setattr(settings, "ANTI_CHOP_YOUNG_DI_SPREAD_MIN", 15.0, raising=False)


def test_real_v_reversal_passes(young):
    """Боевой срез ETH 1h 03.09.2026: веер −0.61 (лонг закрыт), но ADX 26.1
    растёт с 23.9, DI разведены на 28.2. Это и есть пропущенное движение."""
    ok, note = _svc()._young_trend_override(_anchor(), "long")

    assert ok is True
    assert "adx=26.1" in note


@pytest.mark.parametrize("symbol,ctx", [
    ("XRP", _anchor(29.20, 26.19, 41.23, 7.78)),
    ("SOL", _anchor(25.20, 22.49, 33.59, 8.27)),
    ("AVAX", _anchor(24.50, 22.13, 32.34, 9.34)),
    ("BTC", _anchor(25.46, 22.42, 44.23, 9.39)),
])
def test_all_symbols_blocked_by_the_fan_that_day_now_pass(young, symbol, ctx):
    ok, _ = _svc()._young_trend_override(ctx, "long")
    assert ok is True, f"{symbol}: движение снова было бы пропущено"


def test_chop_is_still_rejected(young):
    """Гейт не ослаблен: в чопе ADX низкий, плоский, DI перепутаны — не
    выполняется ни одно из трёх условий."""
    ok, note = _svc()._young_trend_override(
        _anchor(adx=14.0, adx_prev=14.2, plus_di=18.0, minus_di=17.0), "long",
    )

    assert ok is False
    assert "adx=14.0<20" in note
    assert "adx_falling" in note or "adx_rise_too_small" in note
    assert "di_spread=1.0<15" in note


def test_dying_trend_does_not_get_the_young_pass(young):
    """Симметрия с фиксом №1: сильный, но ЗАТУХАЮЩИЙ ADX не считается молодым
    трендом. Иначе альтернативный путь стал бы дырой ровно под те входы,
    которые мы только что закрыли в tz_entry_shadow."""
    ok, note = _svc()._young_trend_override(
        _anchor(adx=40.9, adx_prev=42.1, plus_di=30.0, minus_di=10.0), "long",
    )

    assert ok is False
    assert "adx_falling=42.1->40.9" in note


def test_direction_is_respected(young):
    """Тот же срез, но для шорта: DI разведены ПРОТИВ — пропуска нет."""
    ok, note = _svc()._young_trend_override(_anchor(), "short")

    assert ok is False
    assert "di_spread=-28.2" in note


def test_flag_off_restores_previous_behaviour(monkeypatch, young):
    monkeypatch.setattr(settings, "ANTI_CHOP_YOUNG_TREND_ENABLED", False, raising=False)

    ok, note = _svc()._young_trend_override(_anchor(), "long")

    assert ok is False
    assert note == ""


def test_missing_indicators_do_not_look_like_a_trend(young):
    """Отсутствие приборов не имеет права выглядеть как подтверждённый тренд —
    fail-closed именно здесь, потому что это ослабление гейта, а не выход."""
    ok, note = _svc()._young_trend_override({"adx14": None}, "long")

    assert ok is False
    assert note == "young_trend_no_data"


def test_gate_wires_the_override_in(young):
    """Связь: альтернативный путь должен быть вызван из самого гейта, иначе он
    останется мёртвым кодом — ровно та ошибка, что была с position_notional."""
    import inspect

    src = inspect.getsource(MarketIntelligenceEngine)
    block = src.split("ANTI_CHOP_GATE_ENABLED", 1)[1][:2000]

    assert "_young_trend_override" in block


def test_a_rising_adx_is_not_called_falling(young):
    """(#adx-label-2026-09-05) 05.09 в ленте стояло «adx_not_rising=34.0->34.1»
    — надпись, противоречащая собственным числам: ADX ВЫРОС. Читалась она как
    «тренд затухает», хотя он усиливался, просто медленнее порога.

    Разница не косметическая: на этом основании решают, поздний вход или
    ранний, и именно такие сообщения вводят в заблуждение тогда, когда в них
    вглядываются.
    """
    ok, note = _svc()._young_trend_override(
        _anchor(adx=34.1, adx_prev=34.0, plus_di=30.0, minus_di=10.0), "long",
    )

    assert ok is False
    assert "adx_falling" not in note, "рост назван падением"
    assert "adx_rise_too_small=34.0->34.1" in note


def test_the_threshold_is_printed_next_to_the_miss(young):
    """«Растёт ли ADX» проверяется в трёх местах с разными ответами: строго
    больше нуля в условиях ТЗ, ANTI_CHOP_YOUNG_ADX_RISE_MIN здесь, своя
    настройка у защёлки импульса. Пока порог не виден в сообщении, расхождение
    приходится искать по коду — а искать его начинают уже после того, как оно
    что-нибудь испортило.
    """
    ok, note = _svc()._young_trend_override(
        _anchor(adx=34.1, adx_prev=34.0, plus_di=30.0, minus_di=10.0), "long",
    )

    assert "+0.10<0.5" in note
