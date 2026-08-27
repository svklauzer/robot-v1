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


def test_robot_loop_forwards_stop_and_size_to_ml_controller():
    """(#audit-2026-08-27) Train/serve skew: robot_loop.py передавал в
    ml_controller.evaluate_candidate() confidence/grade/side/regime/net_rr_*,
    но НЕ stop_price/required_margin/entry — хотя plan (тот же объект, из
    которого net_rr_tp1/tp2 уже читались) их уже содержал. Результат:
    row_to_features() тихо ставил stop_distance_pct=0.0 и notional_usdt=0.0
    на КАЖДОМ живом предсказании, хотя модель обучена на реальных значениях
    (см. test_decisive_features_are_present выше — обе фичи "решающие").
    Регрессия по исходнику: дорогой end-to-end тест здесь избыточен, конкретные
    ключи в буквальном dict-литерале — именно то, что сломалось."""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1] / "workers" / "robot_loop.py"
    ).read_text(encoding="utf-8")

    call_start = src.index("self.ml_controller.evaluate_candidate({")
    call_end = src.index("})", call_start)
    call_src = src[call_start:call_end]

    for token in ('"stop_price": plan.stop_price',
                  '"required_margin": plan.required_margin',
                  '"lifecycle": {"entry_price": plan.entry_price}'):
        assert token in call_src, f"{token!r} must be forwarded to ml_controller"


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


# ── Market Intelligence: паритет paper/live ──────────────────────────────────

def test_setup_classification_does_not_depend_on_execution_mode():
    """(#paper-live-parity) Режим ИСПОЛНЕНИЯ не может менять пороги сетапа.

    Было продублировано в четырёх местах:

        learning_mode = trading_mode in ("paper_signal", "paper_trade")
                        or signal_profile in ("learning", "aggressive", "dev")

    Два независимых тумблера управляли одним поведением, причём TRADING_MODE —
    про то, ГДЕ исполняем. Переход paper → live менял бы пороги рождения
    сетапов: learning требует согласия четырёх ТФ при score ≥ 55/50, ветка
    голосования — трёх голосов при score ≥ 58 / ≤ 50.

    Тогда вся статистика, на которой мы принимаем решения, оказалась бы про
    другую систему. Требование «paper == live» этого не допускает.
    """
    from services.market_intelligence import MarketIntelligenceEngine

    eng = MarketIntelligenceEngine.__new__(MarketIntelligenceEngine)

    old_mode = settings.TRADING_MODE
    old_profile = settings.SIGNAL_PROFILE
    try:
        settings.SIGNAL_PROFILE = "learning"
        settings.TRADING_MODE = "paper_signal"
        in_paper = eng._learning_mode()
        settings.TRADING_MODE = "live"
        in_live = eng._learning_mode()

        assert in_paper == in_live, "смена режима исполнения меняет классификацию"
        assert in_paper is True
    finally:
        settings.TRADING_MODE = old_mode
        settings.SIGNAL_PROFILE = old_profile


def test_profile_still_controls_strictness():
    """Профиль — единственный тумблер строгости, и он обязан работать."""
    from services.market_intelligence import MarketIntelligenceEngine

    eng = MarketIntelligenceEngine.__new__(MarketIntelligenceEngine)
    old = settings.SIGNAL_PROFILE
    try:
        settings.SIGNAL_PROFILE = "learning"
        assert eng._learning_mode() is True
        settings.SIGNAL_PROFILE = "strict"
        assert eng._learning_mode() is False
    finally:
        settings.SIGNAL_PROFILE = old


def test_learning_mode_is_defined_once():
    """Четыре копии одного условия — четыре места, где оно разойдётся."""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1] / "services" / "market_intelligence.py"
    ).read_text(encoding="utf-8")

    assert src.count("def _learning_mode") == 1
    assert src.count("self._learning_mode()") >= 4
    assert 'trading_mode in ["paper_signal"' not in src, (
        "режим исполнения снова просочился в классификацию сетапов"
    )


# ── Паритет сторон и отсутствие тупиковой зоны ───────────────────────────────

def _code_only(path_parts: tuple[str, ...]) -> str:
    """Исходник без комментариев.

    Первая версия этих тестов искала подстроки по всему файлу и падала на
    собственных комментариях, где старые пороги процитированы как объяснение.
    Проверять надо код, а не текст рядом с ним.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[1].joinpath(*path_parts).read_text(encoding="utf-8")
    lines = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
    return "\n".join(lines)


def test_quality_threshold_is_the_same_for_both_sides():
    """(#side-parity) Шорт пускался по более низкому общему качеству.

    Было: лонг total_score ≥ 55, шорт ≥ 50 — без обоснования в коде.
    В данных: шортов 170 против лонгов 117 при почти одинаковом убытке на
    сделку (−0.36 против −0.34). Перекос дал не худшие сделки, а больше
    сделок той же убыточности.
    """
    src = _code_only(("services", "market_intelligence.py"))

    assert src.count("total_score >= _min_total") == 2, (
        "порог качества должен быть один и тот же объект для обеих сторон"
    )
    assert "total_score >= 50" not in src
    assert "total_score <= 50" not in src, (
        "для шорта total_score использовался как индикатор НАПРАВЛЕНИЯ: "
        "шорт с отличным качеством отвергался за то, что он хороший"
    )


def test_direction_threshold_is_symmetric_around_neutral():
    """Было `>= 55` для лонга против `<= 55` для шорта.

    Наблюдаемый trend_score по вселенной лежит в 25–43: условие лонга не
    выполнялось почти никогда, условие шорта — почти всегда. Направленный
    фильтр работал в одну сторону.
    """
    margin = float(settings.SETUP_TREND_SCORE_MARGIN)
    long_bar = 50.0 + margin
    short_bar = 50.0 - margin

    assert margin > 0
    assert long_bar - 50.0 == 50.0 - short_bar, "отступы от нейтрали не равны"


def test_voting_branch_judges_quality_not_direction_for_shorts():
    src = _code_only(("services", "market_intelligence.py"))

    assert src.count("total_score >= _vote_min") == 2
    assert "trend_down_votes >= 3 and total_score <= 50" not in src


def test_no_limbo_state_between_approve_and_reject():
    """(#no-limbo) `wait` ничего не ждал.

    Состояние между сканами не сохраняется — каждый цикл считает заново. То
    есть это был отказ, названный ожиданием: сигнал не дозревал, он
    выбрасывался. ETH с final_score 73.16 получал
    `learning_wait_more_confirmation` из-за trend_alignment 30.0 против порога
    32.0 — разница в два пункта.
    """
    src = _code_only(("services", "market_intelligence.py"))

    assert "learning_wait_more_confirmation" not in src
    assert "candidate_but_wait_confirmation" not in src
    assert "learning_setup_strong_score" in src


def test_strong_score_rescues_a_setup_that_missed_one_criterion():
    """Порог strong обязан лежать выше обычного одобрения, иначе он его подменяет."""
    assert settings.LEARNING_SETUP_STRONG_SCORE > settings.LEARNING_SETUP_MIN_SCORE
    # ETH из скана: 73.16 — сетап такого качества больше не выбрасывается.
    assert settings.LEARNING_SETUP_STRONG_SCORE <= 73.0


def test_radar_watch_threshold_is_shared():
    """Третья копия перекоса: watch_long ≥ 50 против watch_short ≥ 47.

    Наблюдение не ведёт к сделке напрямую, но кормит витрину и ленту решений —
    смещённая картина мира тоже вводит в заблуждение.
    """
    src = _code_only(("services", "market_intelligence.py"))

    assert src.count("total_score >= _watch_min") == 2
    assert "total_score >= 47" not in src
