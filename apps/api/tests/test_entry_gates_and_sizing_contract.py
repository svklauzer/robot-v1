"""Гейты входа и сайзинг: механизмы обязаны срабатывать, а не просто быть.

Три дефекта, которые тихо жили в цикле и по которым здесь стоят сторожа:

1. `self.decisions` в RobotLoop не существовал. Обращение к нему стояло внутри
   `try/except Exception`, поэтому AttributeError глотался вместе с `continue`,
   и кандидат, отвергнутый зоной входа, всё равно уходил в публикацию.
2. Режим ML `shadow` заявлен как «на сделки не влияет», но ml_score читался
   сайзингом без проверки режима и резал позицию вдвое.
3. Сайзинг и дневной стоп-лосс считались от захардкоженной 1000 USDT, а
   экспозиция — от реального баланса.
"""
from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

from core.config import settings
from services.ml_controller import MLController
from workers.robot_loop import RobotLoop

API = Path(__file__).resolve().parents[1]


# ── 1. Гейт зоны входа ───────────────────────────────────────────────────────

def test_robot_loop_can_record_decisions():
    """Без этого поля любое обращение к ленте решений — AttributeError."""
    loop = RobotLoop.__new__(RobotLoop)
    RobotLoop.__init__(loop)

    assert hasattr(loop, "decisions"), "RobotLoop не умеет писать решения"
    assert callable(getattr(loop.decisions, "record", None))


def test_entry_zone_veto_is_not_swallowed_by_except():
    """`continue` вето не должен стоять внутри перехвата исключений.

    Пока он стоял там, ЛЮБАЯ ошибка в ветке отмены превращала «вход запрещён»
    в «вход разрешён» — самый дорогой вид тихого сбоя: гейт присутствует в
    коде, виден в ревью, но не отрабатывает ни разу.
    """
    source = (API / "workers" / "robot_loop.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    step = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "step"
    )

    for handler_owner in ast.walk(step):
        if not isinstance(handler_owner, ast.Try):
            continue
        body_src = ast.get_source_segment(source, handler_owner) or ""
        if "entry_zone" not in body_src:
            continue
        assert not any(
            isinstance(node, ast.Continue) for node in ast.walk(handler_owner)
        ), "решение по вето зоны входа снова внутри try — исключение отменит отказ"


# ── 2. ML shadow не трогает размер ───────────────────────────────────────────

def _sizing_multiplier_for(mode: str, ml_score: float, grade: str = "A") -> float:
    """Повторяет ветку conviction-сайзинга из robot_loop.

    Грейд по умолчанию A: его множитель 1.0, поэтому итог задаёт ML-ось — иначе
    `min()` упирается в кап грейда B и разница от ml_score не видна вовсе.
    """
    ml_may_size = str(mode or "off").lower() in ("advisory", "full_auto")
    grade_mult = 1.0 if grade.upper() in ("A", "A+") else float(settings.DYNAMIC_MARGIN_B_CAP_PCT_OF_FREE)
    ml_mult = 1.0
    if ml_may_size and settings.ML_SIZE_ALLOC_ENABLED and ml_score is not None:
        ml_mult = 1.0 if ml_score >= settings.ML_SIZE_FULL_MIN_SCORE else float(settings.ML_SIZE_LOW_MULT)
    return min(grade_mult, ml_mult)


def test_shadow_mode_returns_a_score():
    """Иначе тест ниже проверял бы отсутствие score, а не отсутствие влияния."""
    controller = MLController()
    controller._get_labeler = lambda: type("L", (), {"predict": staticmethod(lambda _c: 0.30)})()
    controller._mode = lambda: "shadow"

    result = controller.evaluate_candidate({"confidence": 70, "grade": "B"})

    assert result["mode"] == "shadow"
    assert result["ml_score"] == 0.3


def test_shadow_score_does_not_change_position_size():
    weak = 0.30   # ниже ML_SIZE_FULL_MIN_SCORE
    assert _sizing_multiplier_for("shadow", weak) == _sizing_multiplier_for("shadow", 0.90), (
        "в shadow размер зависит от ml_score — режим влияет на деньги вопреки контракту"
    )


def test_full_auto_score_does_change_position_size():
    """Обратная сторона: там, где ML дали полномочия, он обязан работать."""
    assert _sizing_multiplier_for("full_auto", 0.30) < _sizing_multiplier_for("full_auto", 0.90)


def test_sizing_branch_checks_the_ml_mode():
    source = (API / "workers" / "robot_loop.py").read_text(encoding="utf-8")
    assert "_ml_may_size" in source, "ветка сайзинга снова не смотрит на режим ML"


# ── 3. Единый источник эквити ────────────────────────────────────────────────

def test_no_hardcoded_equity_in_trading_loops():
    """1000 в сайзинге и в риск-лимите — это чужой счёт.

    Размер позиции и порог дневного убытка обязаны считаться от того же
    капитала, что и экспозиция, иначе на счёте, отличном от 1000, план и
    предохранитель описывают разные системы.
    """
    source = (API / "main.py").read_text(encoding="utf-8")

    for func in ("background_robot_loop", "run_robot_once"):
        body = source.split(f"def {func}(", 1)[1].split("\n@app", 1)[0].split("\nasync def ", 1)[0]
        assert "balance_usdt=1000" not in body, f"{func}: сайзинг от захардкоженного капитала"
        assert "equity_usdt=1000" not in body, f"{func}: риск-лимит от захардкоженного капитала"
        assert "effective_equity_usdt" in body, f"{func}: эквити не из единого источника"


def test_equity_helper_falls_back_to_configured_capital(monkeypatch):
    import main

    monkeypatch.setattr(
        main, "effective_equity_usdt", main.effective_equity_usdt, raising=False
    )
    value = main.effective_equity_usdt()

    assert value > 0
    # В paper источник — RISK_EQUITY_USDT; сетевой сбой в live тоже падает сюда.
    assert value == float(settings.RISK_EQUITY_USDT)


def test_lifecycle_opens_position_on_the_same_equity():
    source = (API / "services" / "signal_lifecycle.py").read_text(encoding="utf-8")
    open_call = source.split("execution.open_paper_position(", 1)[1][:300]

    assert "self._equity_usdt()" in open_call, (
        "открытие позиции считает план от своего капитала, а не от общего"
    )


# ── 4. Ведение позиций не блокирует event loop ───────────────────────────────

def test_position_management_does_not_block_the_event_loop():
    """Тик ведения идёт каждые MANAGE_INTERVAL_SEC; синхронный HTTP в нём
    останавливает весь веб-процесс на время сетевых таймаутов."""
    from services.signal_lifecycle import SignalLifecycleManager

    source = inspect.getsource(SignalLifecycleManager.process_signal)

    assert "asyncio.to_thread" in source, "сетевой вызов остался в event loop"
    assert "ticker_snapshot" in source, (
        "ведению нужна только last-цена — 200 свечей на каждый тик здесь лишние"
    )
    assert not re.search(r"self\.market\.snapshot\(", source)


def test_impulse_is_observed_before_the_gates_that_it_bypasses():
    """(#entry-impulse-2026-09-04) Смысл защёлки — поймать событие, пока
    состояние ещё не сложилось. Наблюдение, поставленное ПОСЛЕ отсева по
    setup_decision и режиму, увидело бы ровно тот поздний момент, ради обхода
    которого защёлка и существует, — и молча превратилось бы в пустышку,
    которая при этом исправно пишется в события и выглядит работающей.
    """
    source = (API / "workers" / "robot_loop.py").read_text(encoding="utf-8")

    observe_at = source.index("self.impulse_latch.observe(")
    setup_gate_at = source.index('if result.setup_decision != "approve":')
    regime_gate_at = source.index("_allowed_regimes and _regime")
    tz_at = source.index("tz_entry_shadow.evaluate(")

    assert observe_at < setup_gate_at, "защёлка наблюдается после отсева по сетапу"
    assert observe_at < regime_gate_at, "защёлка наблюдается после отсева по режиму"
    assert observe_at < tz_at, "защёлка наблюдается после условий ТЗ"


def test_latch_lifts_the_block_only_when_adx_is_the_sole_blocker():
    """Защёлка утверждает «импульс был недавно», а не «направление, сторона
    KAMA и объём тоже в порядке». Снятие блока при других сработавших
    семействах разоружило бы три фильтра одной правкой.
    """
    source = (API / "workers" / "robot_loop.py").read_text(encoding="utf-8")

    assert 'tz_entry_shadow.blocking_families(_tz) == ["adx_rising"]' in source
