"""Пересмотр ML: метка, датасет, контракт фич (#ml-rework-2026-07-28).

Модель показывала val AUC 0.4615 — хуже монетки. Это не «слабая модель», это
следствие трёх вещей сразу: метка воспроизводила ошибку win-rate, в датасете
лежали фантомные филлы и 154 записи из режимов, которые больше не торгуются.
"""
from __future__ import annotations

from core.config import settings
from services.ml_features import (
    FEATURE_NAMES,
    FEATURE_VERSION,
    is_phantom_row,
    row_to_features,
    row_to_label,
)


def _trade(pnl: float, *, risk: float = 1.0, result_pct: float = 0.0,
           mfe: float = 1.0) -> dict:
    return {
        "closed_net_pnl": pnl,
        "net_pnl_stop": -abs(risk),
        "result_pct": result_pct,
        "lifecycle": {"mfe_pct": mfe},
    }


# ── Метка ────────────────────────────────────────────────────────────────────

def test_is_win_would_teach_the_model_that_a_losing_system_is_good():
    """Ключевая причина пересмотра, на нашем же профиле результатов.

    Средняя честная победа +0.091, средний убыток −0.822. Система убыточна,
    но по метке `is_win` две трети сделок — «успех».
    """
    rows = [_trade(0.091) for _ in range(10)] + [_trade(-0.822) for _ in range(5)]

    positives = sum(1 for r in rows if row_to_label(r, "is_win") == 1)

    assert positives / len(rows) > 0.6, "воспроизводим исходную ловушку"
    assert sum(r["closed_net_pnl"] for r in rows) < 0, "при этом сумма отрицательная"


def test_beats_costs_calls_the_same_sample_what_it_is():
    """Та же выборка под правильной меткой — почти ни одной положительной."""
    rows = [_trade(0.091) for _ in range(10)] + [_trade(-0.822) for _ in range(5)]

    positives = sum(1 for r in rows if row_to_label(r, "beats_costs") == 1)

    assert positives == 0, (
        "сделка на +0.09 при риске 1.0 не окупает риск — метка обязана это видеть"
    )


def test_beats_costs_accepts_a_trade_that_paid_for_its_risk():
    assert row_to_label(_trade(0.5), "beats_costs", min_r=0.3) == 1
    assert row_to_label(_trade(0.2), "beats_costs", min_r=0.3) == 0


def test_row_without_planned_risk_is_excluded_not_guessed():
    """Без плана риска судить не о чем.

    Подставить сюда «плюс/минус по знаку» — значит тихо перекосить выборку в
    сторону строк, у которых плана не было.
    """
    row = {"closed_net_pnl": 1.0, "net_pnl_stop": None}
    assert row_to_label(row, "beats_costs") is None


def test_default_label_is_the_expectancy_one():
    assert settings.ML_LABEL_KIND == "beats_costs"
    assert settings.ML_LABEL_MIN_R > 0


# ── Датасет ──────────────────────────────────────────────────────────────────

def test_phantom_rows_are_detectable():
    """13 строк из 287 записаны с результатом выше достигнутого пика.

    Метка у них положительная там, где рынок дал минус.
    """
    phantom = _trade(8.18, result_pct=1.80, mfe=0.97)
    honest = _trade(0.5, result_pct=0.55, mfe=0.70)

    assert is_phantom_row(phantom) is True
    assert is_phantom_row(honest) is False
    assert row_to_label(phantom, "is_win") == 1, (
        "именно так фантом и попадал в обучение как успешный сетап"
    )


def test_training_window_is_bounded():
    """Exit-политика менялась несколько раз за месяц.

    Метка — это исход ПОД ТОГДАШНЮЮ логику ведения; старая строка отвечает на
    другой вопрос.
    """
    assert settings.ML_TRAIN_WINDOW_DAYS > 0
    assert settings.ML_TRAIN_WINDOW_DAYS <= 90


# ── Контракт фич ─────────────────────────────────────────────────────────────

def test_dead_regime_features_are_gone():
    """is_trend_up / is_trend_down после отключения режимов — константные нули.

    Константа не несёт информации, но участвует в регуляризации и размывает
    веса живых признаков.
    """
    assert "is_trend_up" not in FEATURE_NAMES
    assert "is_trend_down" not in FEATURE_NAMES


def test_decisive_features_are_present():
    """То, вокруг чего крутился весь разбор дня, в векторе не было вовсе."""
    for name in ("stop_distance_pct", "notional_usdt", "rr_asymmetry", "hour_of_day"):
        assert name in FEATURE_NAMES, f"фича {name} решает исход, но не собиралась"


def test_feature_vector_matches_the_contract_length():
    """Порядок и длина фиксированы — по ним модель обучается и предсказывает."""
    row = {
        "confidence": 70, "grade": "A", "side": "long",
        "net_rr_tp1": 0.8, "net_rr_tp2": 2.6,
        "stop_price": 99.0, "required_margin": 250.0,
        "entry_zone": {"from": 99.7, "to": 100.3},
        "regime": "crt", "opened_at": "2026-07-28T14:00:00+00:00",
        "entry_depth": {"spread_pct": 0.02, "obi": 0.3, "cvd_trades": 20, "cvd_ratio": 0.4},
    }
    vec = row_to_features(row)

    assert len(vec) == len(FEATURE_NAMES)
    assert all(isinstance(v, float) for v in vec)

    idx = {n: i for i, n in enumerate(FEATURE_NAMES)}
    assert vec[idx["stop_distance_pct"]] > 0, "дистанция стопа не посчиталась"
    assert vec[idx["hour_of_day"]] == 14.0
    assert vec[idx["is_crt"]] == 1.0
    assert vec[idx["rr_asymmetry"]] > 1.0, "TP2 дальше TP1 — асимметрия > 1"


def test_cvd_is_zeroed_on_a_thin_window_identically_in_train_and_serve():
    """Расхождение train/serve — самый тихий класс ML-багов."""
    thin = {"entry_depth": {"cvd_ratio": 1.0, "cvd_trades": 2}}
    thick = {"entry_depth": {"cvd_ratio": 1.0, "cvd_trades": 40}}
    idx = {n: i for i, n in enumerate(FEATURE_NAMES)}

    assert row_to_features(thin)[idx["cvd_ratio"]] == 0.0
    assert row_to_features(thick)[idx["cvd_ratio"]] == 1.0


def test_feature_version_is_declared():
    """Модель с прежним набором фич несовместима и по длине, и по смыслу."""
    assert isinstance(FEATURE_VERSION, int) and FEATURE_VERSION >= 2
