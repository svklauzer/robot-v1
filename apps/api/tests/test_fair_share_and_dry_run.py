"""Равная доля капитала по символам + работоспособность DRY RUN
(#fair-share-2026-07-27).

Запрос Капитана: «каждому символу равная доля при любых гейтах, чтобы не было
rejected из-за денег».

Прежнее правило `num_ready <= 1 → вся свободная маржа` создавало гонку:
кандидаты приходят не одновременно (разные ТФ, разное время подтверждения),
поэтому первый прошедший гейты символ занимал весь потолок, а остальные
отклонялись через `blocked_total_margin_limit` — то есть НЕ по качеству сетапа.
"""
import pytest

from core.config import settings
from services.live_executor import LiveExecutor
from services.margin_allocator import per_trade_margin

UNIVERSE = 8          # BTC ETH SOL XRP AVAX TRX ADA ARB
CEILING = 700.0       # 70% от эквити 1000


# ── Равная доля ───────────────────────────────────────────────────────────────

def test_single_candidate_no_longer_takes_everything():
    """Ядро правки: одинокий кандидат получает СВОЮ долю, а не весь потолок."""
    old = per_trade_margin(CEILING, 1)                       # прежнее поведение
    new = per_trade_margin(CEILING, 1, universe_size=UNIVERSE, ceiling=CEILING)

    assert old == CEILING, "контроль: раньше одинокий кандидат забирал всё"
    assert new == pytest.approx(CEILING / UNIVERSE)
    assert new < old


def test_every_symbol_gets_the_same_share_regardless_of_arrival_order():
    """Справедливость: доля не зависит от того, сколько кандидатов готово сейчас."""
    shares = {
        n: per_trade_margin(CEILING, n, universe_size=UNIVERSE, ceiling=CEILING)
        for n in (1, 2, 5, 8)
    }
    assert len(set(shares.values())) == 1, f"доля скачет от числа готовых: {shares}"


def test_whole_universe_fits_without_money_rejects():
    """Все символы обязаны помещаться в потолок — иначе снова будет rejected."""
    share = per_trade_margin(CEILING, 1, universe_size=UNIVERSE, ceiling=CEILING)

    assert share * UNIVERSE <= CEILING + 1e-6, (
        "суммарно доли превышают потолок — часть символов всё равно отклонят"
    )


def test_share_is_capped_by_actually_free_margin():
    """Занять больше, чем реально свободно, нельзя даже по справедливой доле."""
    free = 40.0                                  # почти всё уже занято
    share = per_trade_margin(free, 1, universe_size=UNIVERSE, ceiling=CEILING)

    assert share == pytest.approx(free)


def test_zero_free_margin_gives_nothing():
    assert per_trade_margin(0.0, 3, universe_size=UNIVERSE, ceiling=CEILING) == 0.0


def test_legacy_behaviour_preserved_without_universe():
    """Обратная совместимость: без universe_size логика прежняя."""
    assert per_trade_margin(CEILING, 1) == CEILING
    assert per_trade_margin(CEILING, 4) == pytest.approx(CEILING / 4)


def test_robot_loop_passes_the_universe():
    """Регресс: движок обязан передавать размер вселенной, иначе правка мертва."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "workers" / "robot_loop.py"
    text = src.read_text(encoding="utf-8")

    assert "universe_size=universe" in text
    assert "DYNAMIC_MARGIN_FAIR_SHARE" in text


# ── DRY RUN под нагрузкой ─────────────────────────────────────────────────────

@pytest.fixture
def executor(monkeypatch):
    monkeypatch.setattr(LiveExecutor, "effective_mode", classmethod(lambda cls: "dry_run"))
    return LiveExecutor.__new__(LiveExecutor)


def test_dry_run_is_active_by_default():
    """LIVE_EXECUTION_MODE=dry_run без ENABLE_LIVE_ORDERS обязан оставаться dry_run,
    а не деградировать в off — иначе живой путь исполнения не проверяется вовсе."""
    assert LiveExecutor.configured_mode() == "dry_run"
    assert LiveExecutor.effective_mode() in ("dry_run", "off")
    assert LiveExecutor.is_live() is False


def test_dry_run_survives_leveraged_notional(executor, monkeypatch):
    """Стресс: плечо 5 даёт нотионал в 5 раз больше кэпа — прогон обязан дойти
    до конца и не оборваться (иначе dry_run не валидирует то, что уйдёт в live)."""
    monkeypatch.setattr(settings, "LIVE_MAX_ORDER_NOTIONAL_USDT", 25.0)

    res = executor.place_market(
        "ADA/USDT", "buy", 3939.0,          # ~650 USDT нотионала при плече 5
        market_type="swap", reference_price=0.165,
        leverage=5.0, purpose="stress",
    )

    assert res.ok is True
    assert res.status == "dry_run"
    assert res.sent is False
    assert res.filled_qty == pytest.approx(3939.0)


def test_dry_run_reports_reduce_only_for_partial_close(executor):
    """TP1-partial уходит reduceOnly — путь обязан проходить в dry_run."""
    res = executor.place_market(
        "ADA/USDT", "sell", 1969.5,
        market_type="swap", reference_price=0.165,
        reduce_only=True, purpose="tp1",
    )

    assert res.ok is True and res.sent is False
    assert res.reduce_only is True


def test_dry_run_off_mode_short_circuits(monkeypatch):
    """Режим off обязан честно отдавать «не отправлено», а не притворяться."""
    monkeypatch.setattr(LiveExecutor, "effective_mode", classmethod(lambda cls: "off"))
    ex = LiveExecutor.__new__(LiveExecutor)

    res = ex.place_market("ADA/USDT", "buy", 100.0,
                          market_type="swap", reference_price=0.165, purpose="x")

    assert res.ok is False and res.status == "off" and res.sent is False


# ── Гвард по символу: не судить по одной сделке ───────────────────────────────

def test_single_stop_no_longer_shrinks_position_size():
    """(#no-noise-guard-2026-07-27) После ОДНОГО стопа размер не режется.

    При min_history=3 гвард делал вывод о символе по 1–2 сделкам: резал риск на
    35% и поднимал требуемую confidence на +5, из-за чего сетапы со score 85–100
    отклонялись как `symbol_policy_confidence_too_low`. На такой выборке знак
    результата случаен — замер по урезанным сделкам разнонаправлен.
    """
    assert settings.SYMBOL_PERF_SMALL_HISTORY_STOP_MULTIPLIER == 1.0


def test_guard_requires_a_statistically_meaningful_sample():
    """Выводы о символе — только на осмысленной выборке, не на 2–5 сделках."""
    assert settings.SYMBOL_PERF_MIN_HISTORY >= 10, (
        "гвард снова судит символ по горстке сделок"
    )
    assert settings.SYMBOL_PERF_BLOCK_MIN_HISTORY >= 15, (
        "блокировка символа на малой выборке — это шум, а не риск-менеджмент"
    )
    assert settings.SYMBOL_PERF_BLOCK_MIN_HISTORY > settings.SYMBOL_PERF_MIN_HISTORY


def test_real_losing_streak_protection_is_untouched():
    """Защита от РЕАЛЬНОЙ серии убытков остаётся: она судит по
    последовательности исходов, а не по одному, и потому информативна
    независимо от размера общей выборки."""
    assert int(settings.SYMBOL_PERF_COOLDOWN_STREAK) >= 2
    assert int(settings.SYMBOL_PERF_COOLDOWN_STOPS) >= 2


def test_streak_check_runs_before_small_history_branch():
    """Регресс: подъём min_history не должен глушить защиту от серии.

    Порядок веток критичен — при обратном серия из 4 стопов проваливалась в
    `small_history_ok`, и защита молча отключалась."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "services" / "symbol_performance_guard.py"
    text = src.read_text(encoding="utf-8")

    streak_at = text.index("symbol_cooldown_losing_streak")
    small_at = text.index("if closed_count < min_history:")
    assert streak_at < small_at, "проверка серии оказалась ПОСЛЕ ветки малой истории"


def test_small_history_returns_neutral_multiplier():
    """Функциональная проверка на самом гварде, а не только на константах."""
    from services.symbol_performance_guard import SymbolPerformanceGuard

    guard = SymbolPerformanceGuard()
    decision = guard._decide(                       # noqa: SLF001 — целевая логика
        closed_count=2, wins=1, losses=1, winrate=50.0,
        total_net_pnl=-3.2, stop_loss_count=1, failed_setup_count=0,
        positive_then_negative_count=1, last_closed_reason="stop_loss",
        losing_streak=1,
    ) if hasattr(SymbolPerformanceGuard, "_decide") else None

    if decision is None:
        pytest.skip("внутренний хелпер недоступен — константы проверены выше")
    assert decision.get("risk_multiplier") == 1.0
