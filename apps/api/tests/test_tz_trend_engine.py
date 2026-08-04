"""Трендовый движок по ТЗ: вход и выход по приборам (#tz-trend-engine-2026-08-03).

Что заменяем и почему
---------------------
Замер по 342 закрытым:

    trend_up    70 сделок  edge_ratio 0.943  capture −20.57%  −56.01 USDT
    trend_down 109 сделок  edge_ratio 1.248  capture  10.96%  −52.77 USDT
    вместе — 96% всего убытка системы (−108.79 из −113.54)

edge_ratio 0.943 означает: ход ПРОТИВ сделки больше хода за неё. Так выглядит
вход в случайный момент, и он случайный и был — зона входа задавалась как
`last × 0.997…1.003`, то есть «цена в тот миг, когда до символа дошёл сканер»,
при условии тренда, истинном сутками.

Стопы: 107 срабатываний, −223.76 USDT, НИ ОДНОЙ прибыльной. Средний MAE при
этом −0.618%, а стопы стоят на 1–3% — типичная сделка до стопа не доходит.
Стоп не защищал, он фиксировал уже случившееся.
"""
from __future__ import annotations

import pytest

from core.config import settings
from services import kama as kama_mod
from services import tz_entry_shadow as tz
from services import tz_trend_exit as tze


# ── KAMA ────────────────────────────────────────────────────────────────────
def test_kama_speeds_up_on_a_straight_move():
    """Направленный ход — KAMA почти догоняет цену."""
    closes = [100.0 + i for i in range(60)]
    value = kama_mod.kama_last(closes)
    assert value is not None
    # На идеальной прямой ER = 1, сглаживание минимально.
    assert value == pytest.approx(closes[-1], rel=0.02)


def test_kama_slows_down_in_chop():
    """Пила той же амплитуды — KAMA почти стоит.

    Это и есть причина брать её вместо EMA: во флэте DEX/крипты EMA генерит
    ложные пересечения, KAMA замирает.
    """
    closes = [100.0 + (2.0 if i % 2 else 0.0) for i in range(60)]
    value = kama_mod.kama_last(closes)
    assert value is not None
    assert 100.0 <= value <= 102.0
    # И заметно дальше от последней цены, чем на прямой.
    assert abs(value - closes[-1]) > 0.2


def test_efficiency_ratio_separates_trend_from_chop():
    trend = [100.0 + i for i in range(30)]
    chop = [100.0 + (1.0 if i % 2 else 0.0) for i in range(30)]
    assert kama_mod.efficiency_ratio(trend) == pytest.approx(1.0, abs=1e-6)
    assert kama_mod.efficiency_ratio(chop) < 0.2


def test_kama_needs_enough_history():
    assert kama_mod.kama_last([100.0, 101.0]) is None


# ── вход по ТЗ ──────────────────────────────────────────────────────────────
def _tfs(**over):
    trend = {
        "last_close": 105.0, "kama": 100.0,
        "adx14": 30.0, "adx14_prev": 26.0,
        "plus_di": 28.0, "minus_di": 12.0,
        "obv": 1000.0, "obv_ema20": 800.0,
    }
    entry = {
        "stoch_rsi_k": 25.0, "stoch_rsi_d": 20.0,
        "stoch_rsi_k_prev": 18.0, "stoch_rsi_d_prev": 22.0,
    }
    trend.update(over.pop("trend", {}))
    entry.update(over.pop("entry", {}))
    return {"1h": trend, "15m": entry}


def test_clean_tz_long_passes():
    """Все пять условий раздела 3.1 выполнены."""
    out = tz.evaluate(_tfs(), regime="trend_up_candidate", side="long")
    assert out.evaluated is True
    assert out.would_pass is True, out.failed


def test_price_below_kama_blocks_long():
    """Пункт 1 ТЗ. Раньше KAMA не проверялась вовсе."""
    out = tz.evaluate(_tfs(trend={"last_close": 95.0}),
                      regime="trend_up_candidate", side="long")
    assert "price_below_kama" in out.failed


def test_falling_adx_blocks_entry():
    """«ADX выше 23 И имеет ВОСХОДЯЩУЮ траекторию» — прямая цитата ТЗ.

    Без этой проверки движок входит в тренд, который уже иссяк: ADX 30 по
    дороге вниз выглядит так же, как ADX 30 по дороге вверх.
    """
    out = tz.evaluate(_tfs(trend={"adx14": 30.0, "adx14_prev": 35.0}),
                      regime="trend_up_candidate", side="long")
    assert any(f.startswith("adx_not_rising") for f in out.failed)


def test_requires_a_real_cross_not_just_position():
    """ТЗ требует ПЕРЕСЕЧЕНИЯ %K через %D, а не «%K выше %D».

    Разница принципиальная: «выше» истинно всю дорогу вверх и пропускает вход
    в любой точке отката — то есть возвращает ту самую случайность входа,
    ради устранения которой всё и делается.
    """
    out = tz.evaluate(
        _tfs(entry={"stoch_rsi_k": 25.0, "stoch_rsi_d": 20.0,
                    "stoch_rsi_k_prev": 24.0, "stoch_rsi_d_prev": 19.0}),
        regime="trend_up_candidate", side="long",
    )
    assert "stoch_no_bullish_cross" in out.failed


def test_entry_outside_pullback_zone_blocked():
    out = tz.evaluate(_tfs(entry={"stoch_rsi_k": 65.0}),
                      regime="trend_up_candidate", side="long")
    assert any(f.startswith("stoch_not_in_pullback") for f in out.failed)


def test_obv_against_the_move_blocks_long():
    """«Цена растёт, а OBV падает» — ложный памп по ТЗ."""
    out = tz.evaluate(_tfs(trend={"obv": 700.0, "obv_ema20": 800.0}),
                      regime="trend_up_candidate", side="long")
    assert "obv_below_ema" in out.failed


def test_short_is_symmetric():
    """Требование распространяется и на шорты — trend_down даёт −52.77."""
    out = tz.evaluate(
        _tfs(
            trend={"last_close": 95.0, "kama": 100.0,
                   "plus_di": 12.0, "minus_di": 28.0,
                   "obv": 700.0, "obv_ema20": 800.0},
            entry={"stoch_rsi_k": 75.0, "stoch_rsi_d": 80.0,
                   "stoch_rsi_k_prev": 82.0, "stoch_rsi_d_prev": 78.0},
        ),
        regime="trend_down_candidate", side="short",
    )
    assert out.would_pass is True, out.failed


# ── выход по ТЗ ─────────────────────────────────────────────────────────────
@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setattr(settings, "TZ_EXIT_CONDITIONS", "kama,adx,obv", raising=False)
    monkeypatch.setattr(settings, "TZ_EXIT_ADX_PEAK_MIN", 50.0, raising=False)
    monkeypatch.setattr(settings, "TZ_EXIT_ADX_FADE", 3.0, raising=False)


def test_close_below_kama_exits_long(armed):
    """Пункт 1 раздела 3.2 — экстренный выход."""
    out = tze.evaluate(side="long", close=98.0, kama=100.0, adx=30.0,
                       adx_peak=35.0, obv=1000.0, obv_ema=800.0)
    assert out.exit is True
    assert out.reason == "tz_kama"


def test_trend_intact_holds(armed):
    out = tze.evaluate(side="long", close=105.0, kama=100.0, adx=30.0,
                       adx_peak=35.0, obv=1000.0, obv_ema=800.0)
    assert out.exit is False
    assert out.reason == "trend_intact"


def test_adx_fade_requires_memory_of_the_peak(armed):
    """«Разворот вниз из зоны выше 50» невозможно проверить одним значением.

    Без памяти о пике условие вырождается в «ADX < 50» и закрывало бы каждую
    сделку, которая до 50 просто не дошла.
    """
    # Пик не достигал 50 — не выходим, хотя ADX низкий.
    held = tze.evaluate(side="long", close=105.0, kama=100.0, adx=25.0,
                        adx_peak=40.0, obv=1000.0, obv_ema=800.0)
    assert held.exit is False

    # Пик был 60, ADX откатился — выходим.
    out = tze.evaluate(side="long", close=105.0, kama=100.0, adx=55.0,
                       adx_peak=60.0, obv=1000.0, obv_ema=800.0)
    assert out.exit is True
    assert out.reason == "tz_adx"


def test_obv_reversal_exits(armed):
    out = tze.evaluate(side="long", close=105.0, kama=100.0, adx=30.0,
                       adx_peak=35.0, obv=700.0, obv_ema=800.0)
    assert out.exit is True
    assert out.reason == "tz_obv"


def test_short_exit_is_symmetric(armed):
    out = tze.evaluate(side="short", close=102.0, kama=100.0, adx=30.0,
                       adx_peak=35.0, obv=700.0, obv_ema=800.0)
    assert out.exit is True
    assert out.reason == "tz_kama"


def test_disarmed_condition_records_but_does_not_close(monkeypatch):
    """Условие можно наблюдать, не давая ему закрывать."""
    monkeypatch.setattr(settings, "TZ_EXIT_CONDITIONS", "kama", raising=False)
    out = tze.evaluate(side="long", close=105.0, kama=100.0, adx=30.0,
                       adx_peak=35.0, obv=700.0, obv_ema=800.0)
    assert out.exit is False
    assert "obv_reversed" in out.triggers


def test_default_exit_does_not_arm_obv():
    """Дефолт не вооружает OBV на выход: вход требует obv>ema, и тот же порог на
    выходе churn-ил позицию у линии (TRX #362 — открыл/закрыл за 10с в ноль)."""
    armed = {x.strip() for x in settings.TZ_EXIT_CONDITIONS.split(",") if x.strip()}
    assert "obv" not in armed


def test_obv_reversal_alone_does_not_churn_on_default_exit(monkeypatch):
    """С дефолтными условиями (kama,adx) разворот OBV НЕ закрывает свежий вход,
    а слом KAMA — закрывает."""
    monkeypatch.setattr(settings, "TZ_EXIT_CONDITIONS", "kama,adx", raising=False)
    # тренд цел (close>kama), ADX не падает от пика, но OBV ушёл под EMA
    held = tze.evaluate(side="long", close=105.0, kama=100.0, adx=30.0,
                        adx_peak=35.0, obv=700.0, obv_ema=800.0)
    assert held.exit is False
    assert "obv_reversed" in held.triggers  # наблюдается, но не закрывает
    # а вот пробой KAMA закрывает
    broke = tze.evaluate(side="long", close=99.0, kama=100.0, adx=30.0,
                         adx_peak=35.0, obv=1000.0, obv_ema=800.0)
    assert broke.exit is True
    assert broke.reason == "tz_kama"


# ── сайзинг без стопа ───────────────────────────────────────────────────────
class _Levels:
    """Минимальный носитель _tz_stop без рынка и pandas."""

    from services.market_intelligence import MarketIntelligenceEngine as _E

    _tz_stop = _E._tz_stop
    _tf = _E._tf
    _ctx_value = _E._ctx_value


def _ctx(kama):
    return {"1h": {"kama": kama}}


@pytest.fixture
def sizing(monkeypatch):
    monkeypatch.setattr(settings, "TZ_TREND_EXIT_ONLY", True, raising=False)
    monkeypatch.setattr(settings, "TZ_TREND_TF", "1h", raising=False)
    monkeypatch.setattr(settings, "TZ_STOP_KAMA_BUFFER_PCT", 0.15, raising=False)
    monkeypatch.setattr(settings, "TZ_STOP_MIN_DIST_PCT", 0.30, raising=False)
    monkeypatch.setattr(settings, "TZ_DISASTER_STOP_PCT", 5.0, raising=False)
    return _Levels()


def test_sizing_anchor_uses_kama_distance(sizing):
    """Дистанция до KAMA заменила ATR как мера риска сетапа."""
    level, meta = sizing._tz_stop(_ctx(100.0), side="long", last=105.0)
    assert meta["source"] == "kama"
    # KAMA 100 минус буфер 0.15% = 99.85. От цены 105 это 4.9048%.
    assert meta["dist_pct"] == pytest.approx(4.9048, abs=1e-3)
    # Уровень остаётся РОВНО на линии слома тренда: ограничитель не сработал,
    # значит гонять его через проценты и обратно незачем.
    assert level == pytest.approx(99.85, abs=1e-8)
    assert meta["clamped"] is None


def test_price_glued_to_kama_hits_the_floor(sizing):
    """Главный риск отказа от стопа: дистанция 0.05% раздула бы позицию в разы.

    qty = risk / дистанция, поэтому пол здесь — не косметика, а защита от
    позиции на весь депозит.
    """
    level, meta = sizing._tz_stop(_ctx(100.0), side="long", last=100.10)
    assert meta["clamped"] == "floor"
    assert meta["dist_pct"] == pytest.approx(0.30)
    assert level < 100.10


def test_stretched_price_hits_the_cap(sizing):
    """Растянутая цена дала бы «риск» в 20% и съела бюджет одной сделкой."""
    level, meta = sizing._tz_stop(_ctx(100.0), side="long", last=130.0)
    assert meta["clamped"] == "cap"
    assert meta["dist_pct"] == pytest.approx(5.0)


def test_short_anchor_is_symmetric(sizing):
    level, meta = sizing._tz_stop(_ctx(100.0), side="short", last=95.0)
    assert meta["source"] == "kama"
    assert level > 95.0


def test_price_on_the_wrong_side_falls_back(sizing):
    """Лонг ниже KAMA по ТЗ вообще не должен был открыться.

    Не выдумываем уровень — отдаём решение прежней логике, иначе «стоп» окажется
    выше цены входа.
    """
    level, meta = sizing._tz_stop(_ctx(100.0), side="long", last=95.0)
    assert level is None
    assert meta["source"] == "price_wrong_side_of_kama"


def test_no_kama_falls_back(sizing):
    level, meta = sizing._tz_stop(_ctx(0.0), side="long", last=105.0)
    assert level is None
    assert meta["source"] == "no_kama"


def test_disabled_flag_keeps_old_behaviour(sizing, monkeypatch):
    monkeypatch.setattr(settings, "TZ_TREND_EXIT_ONLY", False, raising=False)
    level, meta = sizing._tz_stop(_ctx(100.0), side="long", last=105.0)
    assert level is None
    assert meta["source"] == "disabled"


def test_kama_distance_anchors_position_size():
    """Убрав стоп, мы убрали якорь сайзинга (qty = risk / дистанция).

    Дистанция до KAMA — естественная замена: она мала при входе от линии
    (хороший вход по ТЗ) и велика при растянутой цене (плохой вход получит
    меньший размер).
    """
    near = tze.stop_from_kama(side="long", kama=100.0, buffer_pct=0.15)
    assert near == pytest.approx(99.85)
    far = tze.stop_from_kama(side="short", kama=100.0, buffer_pct=0.15)
    assert far == pytest.approx(100.15)
    assert tze.stop_from_kama(side="long", kama=None) is None
