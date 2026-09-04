"""Гейт входа не должен учиться на решениях контура выхода
(#tp-reach-censoring-2026-09-04).

Боевой замок 04.09.2026. ETH: `tp2_reached_too_rarely: 0% < 27%`, выборка 21,
`tp2_hit_rate = 0.0` — ни одна сделка не дошла до TP2. Но и не могла: их
закрывали НАШИ защитные выходы раньше цели, и каждое такое закрытие уходило в
статистику как «цель недостижима».

Контур замкнут: жёсткий TP2 срезает правый хвост → измеренная частота падает →
гейт не пускает → новых сделок нет → частота не обновляется. Сам он не
разомкнётся: вчерашняя правка TP2 меняет будущие сделки, а гейт смотрит на
прошлый 21.

Докстринг модуля предупреждал об этом с самого начала: «MFE ограничен нашими же
выходами… именно он делает замок возможным». Здесь этот замок вскрывается.

Устройство фикса — то же, что уже применено в модуле для выборки символа:
вторая оценка, которая может ТОЛЬКО снять отказ, никогда его не создать.
"""
from __future__ import annotations

import pytest

from core.config import settings
from services import tp_reachability as tr


@pytest.fixture(autouse=True)
def _enforce(monkeypatch):
    monkeypatch.setattr(settings, "TP_REACH_MODE", "enforce", raising=False)
    monkeypatch.setattr(settings, "TP_REACH_EV_MARGIN", 1.0, raising=False)
    monkeypatch.setattr(settings, "TP_REACH_MIN_SAMPLE", 20, raising=False)
    monkeypatch.setattr(settings, "TP_REACH_CENSOR_ADJUST_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "TP_REACH_MIN_UNCENSORED_SAMPLE", 12, raising=False)
    tr._CACHE.update({"ts": 0.0, "by_key": {}, "by_regime": {}})
    yield
    tr._CACHE.update({"ts": 0.0, "by_key": {}, "by_regime": {}})


def _load(monkeypatch, rows):
    """rows: список (mfe, closed_reason) для пары (SYM, regime)."""
    monkeypatch.setattr(tr, "_load_mfe", lambda *a, **k: ({("SYM", "reg"): rows}, {}))


def _evaluate():
    # RR 3.0 → требуется 1/(1+3) = 25%.
    return tr.evaluate(symbol="SYM", regime="reg", tp1_dist_pct=1.0,
                       tp2_dist_pct=2.0, net_rr_tp2=3.0)


# ── регресс на боевой замок ─────────────────────────────────────────────────

def test_our_own_early_exits_no_longer_prove_the_target_unreachable(monkeypatch):
    """Сценарий 04.09: цель берётся редко, но больше половины сделок мы закрыли
    САМИ, пока они были живы.

    30 сделок: 6 дошли до цели, 8 умерли от рынка, 16 закрыты нашими защитными
    выходами. Сырая частота 6/30 = 20% и отказывает при требуемых 25%.

    Но 16 закрытий — это не наблюдения рынка, это наши решения: они говорят
    «MFE был не меньше записанного», а не «цель недостижима». По полным
    наблюдениям 6/14 = 43%, и отказ снимается.
    """
    rows = (
        [(2.5, "tp2_reached")] * 6                       # дошли до цели
        + [(0.3, "stop_loss")] * 8                       # умерли от рынка
        + [(0.9, "tz_mfe_giveback_backstop")] * 8        # вышли сами
        + [(1.1, "post_tp1_giveback_trail")] * 8         # вышли сами
    )
    _load(monkeypatch, rows)

    result = _evaluate()

    assert result.tp2_hit_rate == pytest.approx(6 / 30, abs=1e-4)
    assert result.tp2_hit_rate_uncensored == pytest.approx(6 / 14, abs=1e-4)
    assert result.censored_out == 16
    assert result.uncensored_sample == 14
    assert result.allowed is True
    assert result.reason == "reward_reached_uncensored"


def test_zero_reached_is_not_rescued_by_censoring(monkeypatch):
    """Честная граница фикса: если до цели не дошла НИ ОДНА сделка, отбрасывание
    цензуры не помогает — числитель остаётся нулём.

    Это ровно случай ETH 04.09 (`tp2_hit_rate = 0.0` на 21 сделке): правка
    убирает смещение, но не выдумывает попаданий, которых не было. Там разбор
    другой — цель стоит слишком далеко для того, как инструмент реально ходил.
    """
    rows = [(0.3, "stop_loss")] * 14 + [(0.9, "tz_mfe_giveback_backstop")] * 10
    _load(monkeypatch, rows)

    result = _evaluate()

    assert result.tp2_hit_rate == 0.0
    assert result.tp2_hit_rate_uncensored == 0.0
    assert result.allowed is False


def test_genuine_misses_still_block(monkeypatch):
    """Обратная сторона: если цель не берётся, а сделки умирают ОТ РЫНКА,
    отказ обязан остаться. Иначе правка превратилась бы в отключение гейта."""
    rows = [(2.5, "tp2_reached")] * 2 + [(0.2, "stop_loss")] * 22
    _load(monkeypatch, rows)

    result = _evaluate()

    assert result.tp2_hit_rate_uncensored == pytest.approx(2 / 24, abs=1e-4)
    assert result.allowed is False
    assert result.reason.startswith("tp2_reached_too_rarely")


def test_unknown_reason_counts_as_a_genuine_miss(monkeypatch):
    """Отсутствие причины — не доказательство нашей вины.

    Первая версия правки считала неопознанное цензурой, и на строках журнала
    без closed_reason оценка вырождалась в 1.0, пропуская вообще всё. Право
    снять отказ даёт только ЯВНО опознанный свой выход.
    """
    rows = [(2.5, "tp2_reached")] * 2 + [(0.2, "")] * 22
    _load(monkeypatch, rows)

    result = _evaluate()

    assert result.tp2_hit_rate_uncensored == pytest.approx(2 / 24, abs=1e-4)
    assert result.allowed is False


def test_estimate_never_tightens_the_gate(monkeypatch):
    """Вторая оценка смещена ВВЕРХ (выходим мы, когда импульс гаснет), поэтому
    ей позволено только снимать отказ. Если сырая частота проходит, до второй
    оценки дело не доходит вовсе — и она остаётся None."""
    rows = [(2.5, "tp2_reached")] * 12 + [(0.2, "tz_mfe_giveback_backstop")] * 12
    _load(monkeypatch, rows)

    result = _evaluate()

    assert result.tp2_hit_rate == pytest.approx(0.5, abs=1e-4)
    assert result.allowed is True
    assert result.reason == "reward_reached_often_enough"
    assert result.tp2_hit_rate_uncensored is None, (
        "оценка посчиталась там, где отказа не было — значит она способна "
        "влиять на решение в обе стороны, а это не её роль"
    )


def test_too_few_complete_observations_do_not_unblock(monkeypatch):
    """Отбрасывая цензурированные, мы уменьшаем выборку. На горстке полных
    наблюдений частота — это не частота, и отказ должен устоять."""
    rows = [(2.5, "tp2_reached")] * 2 + [(0.2, "stop_loss")] * 2 + \
           [(0.9, "tz_mfe_giveback_backstop")] * 20
    _load(monkeypatch, rows)

    result = _evaluate()

    assert result.tp2_hit_rate_uncensored is None
    assert result.uncensored_sample == 4
    assert result.allowed is False


def test_flag_restores_previous_behaviour(monkeypatch):
    monkeypatch.setattr(settings, "TP_REACH_CENSOR_ADJUST_ENABLED", False, raising=False)
    rows = (
        [(2.5, "tp2_reached")] * 4
        + [(0.3, "stop_loss")] * 4
        + [(0.9, "tz_mfe_giveback_backstop")] * 16
    )
    _load(monkeypatch, rows)

    result = _evaluate()

    assert result.allowed is False
    assert result.tp2_hit_rate_uncensored is None


# ── сторож полноты: новый выход обязан быть классифицирован ─────────────────

def test_every_exit_reason_is_classified():
    """Причина, не попавшая ни в один список, считается настоящим промахом —
    то есть новый защитный выход молча вернул бы замок 04.09.

    Ровно та же ловушка, что была с CONDITION_FAMILY в tz_entry_shadow:
    условие считается, пишется в план и не влияет ни на что. Здесь цена выше —
    гейт перестаёт пускать сделки, и выглядит это как «рынок не даёт».
    """
    import re
    from pathlib import Path

    api = Path(__file__).resolve().parents[1]
    reasons: set[str] = set()
    for name in ("services/exit_policy.py", "services/signal_lifecycle.py"):
        src = (api / name).read_text(encoding="utf-8")
        reasons |= set(re.findall(r'reason="([a-z0-9_]+)"', src))
        for pair in re.findall(r'reason = "([a-z0-9_]+)" if .* else "([a-z0-9_]+)"', src):
            reasons |= set(pair)

    # Достижение цели классифицировать не нужно: там MFE и так выше дистанции.
    reasons -= {"tp2_reached", "tp1_reached", "tp1_partial", "tp2_partial"}

    known = tr._TERMINAL_REASONS | tr._VOLUNTARY_REASONS
    missing = sorted(reasons - known)

    assert not missing, (
        "выход контура не отнесён ни к терминальным, ни к добровольным — "
        f"он будет считаться настоящим промахом и ужмёт гейт входа: {missing}"
    )


def test_terminal_and_voluntary_do_not_overlap():
    assert not (tr._TERMINAL_REASONS & tr._VOLUNTARY_REASONS)
