from services.exit_policy import ExitPolicyService
from core.config import settings


def test_before_tp1_failed_setup_does_not_fire_before_absolute_mfe_and_age():
    svc = ExitPolicyService()

    # (#tp1-partial-2026-07-09) Тест проверяет ГЕЙТЫ failed_setup — отключаем
    # breakeven-lock, который теперь (намеренно) срабатывает раньше на armed-MFE.
    old_lock = settings.BREAKEVEN_LOCK_ENABLED
    try:
        settings.BREAKEVEN_LOCK_ENABLED = False

        # No age — guard must not fire regardless of loss
        no_age = svc.before_tp1_decision(
            side="long",
            entry_price=100.0,
            current_price=99.6,
            stop_price=98.0,
            mfe_pct=0.7,
            signal_age_sec=None,
            symbol=None,
        )
        # MFE too low (below absolute min 0.50) — guard must not fire
        low_mfe = svc.before_tp1_decision(
            side="long",
            entry_price=100.0,
            current_price=99.6,
            stop_price=98.0,
            mfe_pct=0.1,
            signal_age_sec=600,
            symbol=None,
        )
        # Age below new threshold (599 < 600) — guard must not fire
        young_trade = svc.before_tp1_decision(
            side="long",
            entry_price=100.0,
            current_price=99.6,
            stop_price=98.0,
            mfe_pct=0.55,
            signal_age_sec=599,
            symbol=None,
        )
    finally:
        settings.BREAKEVEN_LOCK_ENABLED = old_lock

    assert no_age.exit is False
    assert low_mfe.exit is False
    assert young_trade.exit is False, "Trade younger than 600s must not trigger failed_setup_exit"


def test_before_tp1_armed_breakeven_lock_fires_on_real_loss():
    """(#leak-be-lock-2026-07-09) Вооружённая сделка (MFE>=arm) в реальном минусе
    (глубже hard floor) обязана закрыться breakeven_lock, не дожидаясь flow.

    (#kill-losers-2026-07-28) Ветка ВЫКЛЮЧЕНА по умолчанию — на 287 закрытых она
    дала 45 сделок, 2 победы, −30.24 USDT. Тест сохранён и включает флаг явно:
    механика ветки не сломана, отключено её применение. Поведение при
    выключенном флаге проверяется отдельно ниже.
    """
    svc = ExitPolicyService()

    old = settings.BREAKEVEN_LOCK_ENABLED
    try:
        settings.BREAKEVEN_LOCK_ENABLED = True
        decision = svc.before_tp1_decision(
            side="long",
            entry_price=100.0,
            current_price=99.6,
            stop_price=98.0,
            mfe_pct=0.7,
            signal_age_sec=None,
            symbol=None,
        )
    finally:
        settings.BREAKEVEN_LOCK_ENABLED = old

    assert decision.exit is True
    assert decision.reason == "breakeven_lock"


def test_breakeven_lock_is_off_by_default_after_the_measurement():
    """(#kill-losers-2026-07-28) Замер решил судьбу ветки — фиксируем решение.

        breakeven_lock:  45 сделок,  побед 2,  net −30.24,  avg −0.67

    Замок взводился на MFE 0.35%, ставил стоп в безубыток+0.10% и закрывал
    сделку до того, как до неё добирались ветки, которые реально зарабатывают
    (tp2 +66.39, ride-трейл +53.18, post-TP1 +21.26 — суммарно +152.5 на 57
    сделках). Он не защищал прибыль, он отбирал сделки у прибыльных веток.

    13.08: ВКЛЮЧЕНО ОБРАТНО для non-trend режимов (scalp/range), где нет TZ exit.
    Конфигурация обновлена: ARM=0.45%, FLOOR=0.18%, COST_BUFFER=0.07%.
    На 16 траекториях сумма замков +0.02 USDT (было −30.24).

    Тест проверяет, что замок ТЕПЕРЬ РАБОТАЕТ: при MFE=0.7% и просадке -0.4%
    срабатывает breakeven_lock с выходом по floor=0.18%.
    """
    assert settings.BREAKEVEN_LOCK_ENABLED is True

    svc = ExitPolicyService()
    decision = svc.before_tp1_decision(
        side="long",
        entry_price=100.0,
        current_price=99.6,
        stop_price=98.0,
        mfe_pct=0.7,
        signal_age_sec=None,
        symbol=None,
    )

    # Замок включен и должен сработать: MFE=0.7 > arm=0.45, cur=-0.4 < floor=0.18
    assert decision.reason == "breakeven_lock", (
        f"включенный замок должен сработать: {decision}"
    )
    assert decision.exit is True
    assert abs(decision.exit_price - 99.6) < 0.01  # выход по текущей цене ~99.6


def test_before_tp1_failed_setup_exit_triggers_after_strict_age_and_real_mfe():
    svc = ExitPolicyService()

    old_lock = settings.BREAKEVEN_LOCK_ENABLED
    try:
        settings.BREAKEVEN_LOCK_ENABLED = False
        decision = svc.before_tp1_decision(
            side="long",
            entry_price=100.0,
            current_price=99.55,
            stop_price=98.0,
            mfe_pct=0.55,
            signal_age_sec=600,
            symbol=None,
            # soft/mid failed_setup — под вик-фильтром: нужен подтверждённый
            # разворот потока (EXIT_REQUIRE_FLOW_CONFIRM=True по умолчанию).
            flow_against=True,
        )
    finally:
        settings.BREAKEVEN_LOCK_ENABLED = old_lock

    assert decision.exit is True
    assert decision.reason == "failed_setup_exit"


def test_before_tp1_protective_breakeven_uses_v4_profit_floor():
    svc = ExitPolicyService()

    decision = svc.before_tp1_decision(
        side="long",
        entry_price=100.0,
        current_price=100.55,
        stop_price=98.5,
        mfe_pct=0.9,
        symbol=None,
    )

    assert decision.exit is True
    assert decision.reason == "protective_breakeven_profit_guard"
    assert decision.exit_price is not None
    # (#phantom-fill-2026-07-25) Раньше здесь стояло `>= 101.8`: тест ЗАКРЕПЛЯЛ баг —
    # MIN_PROTECTIVE_EXIT_PCT (экономический гейт) попадал прямо в цену филла, и
    # сделка «закрывалась» по 101.8 при рынке 100.55 и максимуме ~100.9. В бою это
    # дало TRX #281 (+8.18) и #272 (+10.97) по цене entry×1.018, которой рынок не
    # видел. Защитный выход не может исполниться ЛУЧШЕ рынка.
    assert decision.exit_price <= 100.55 + 1e-9
    assert "protected" in (decision.note or "")


def test_protective_exit_price_never_exceeds_market():
    """(#phantom-fill-2026-07-25) Ни одна защитная/трейл-ветка не имеет права
    вернуть цену выхода лучше текущего рынка — ни в лонг, ни в шорт."""
    svc = ExitPolicyService()

    long_dec = svc.before_tp1_decision(
        side="long",
        entry_price=0.328645,
        current_price=0.330223,   # реальный рынок TRX #281 в момент выхода
        stop_price=0.326,
        mfe_pct=0.9737,
        trade_mode="trend",
        position_notional_usdt=496.0,
        symbol=None,
    )
    if long_dec.exit:
        assert long_dec.exit_price <= 0.330223 + 1e-9, "лонг: филл лучше рынка"

    short_dec = svc.before_tp1_decision(
        side="short",
        entry_price=100.0,
        current_price=99.5,       # шорт в плюсе на 0.5%
        stop_price=101.5,
        mfe_pct=1.0,
        trade_mode="trend",
        position_notional_usdt=500.0,
        symbol=None,
    )
    if short_dec.exit:
        # для шорта «лучше рынка» = ниже текущей цены
        assert short_dec.exit_price >= 99.5 - 1e-9, "шорт: филл лучше рынка"


def test_breakeven_lock_cannot_close_below_round_trip_cost():
    """(#be-floor-cost-2026-07-25) Пол замка обязан покрывать round-trip издержки.
    Конфигурационный 0.10% ниже фактических 0.15% swap-издержек — при таком поле
    breakeven_lock математически не может закрыться в плюс (7 из 20 последних
    сделок закрылись ровно на gross − 0.15%)."""
    svc = ExitPolicyService()
    _net_safe, _src, fee_rate = svc._net_safe_profit_pct(symbol=None, market_type="swap")
    round_trip_pct = (fee_rate * 2 + float(settings.SLIPPAGE_BUFFER_PCT)) * 100

    effective_floor = max(
        float(settings.BREAKEVEN_LOCK_FLOOR_PCT),
        round_trip_pct + float(settings.BREAKEVEN_LOCK_COST_BUFFER_PCT),
    )
    assert effective_floor > round_trip_pct, "пол замка не покрывает издержки"

    # Сделка на уровне старого пола (0.10% gross) больше НЕ считается безубытком:
    # при round-trip 0.12% это чистый минус, замок обязан был закрыться раньше.
    assert effective_floor > 0.10


def test_before_tp1_no_exit_on_healthy_pullback():
    svc = ExitPolicyService()

    decision = svc.before_tp1_decision(
        side="long",
        entry_price=100.0,
        current_price=100.9,
        stop_price=98.5,
        mfe_pct=1.1,
        symbol=None,
    )

    assert decision.exit is False
    assert decision.reason is None


def test_before_tp1_adaptive_mfe_capture_triggers_before_deep_giveback():
    svc = ExitPolicyService()

    old_enabled = settings.MFE_CAPTURE_ENABLED
    old_drawdown = settings.MFE_CAPTURE_DRAWDOWN_PCT
    old_share = settings.MFE_CAPTURE_PROTECT_SHARE
    old_start = settings.MFE_CAPTURE_START_PCT
    try:
        settings.MFE_CAPTURE_ENABLED = True
        settings.MFE_CAPTURE_DRAWDOWN_PCT = 0.30
        settings.MFE_CAPTURE_PROTECT_SHARE = 0.40
        # Дефолт START_PCT поднят до 1.30 (#expectancy-cleanup) — тест проверяет
        # сам механизм capture, поэтому фиксируем старый порог явно.
        settings.MFE_CAPTURE_START_PCT = 0.90

        decision = svc.before_tp1_decision(
            side="long",
            entry_price=100.0,
            current_price=100.7,
            stop_price=99.0,
            tp1_price=100.7,
            mfe_pct=1.2,
            symbol=None,
        )

        assert decision.exit is True
        assert decision.reason == "adaptive_mfe_capture"
        assert decision.exit_price is not None
        assert "protected" in (decision.note or "")
    finally:
        settings.MFE_CAPTURE_ENABLED = old_enabled
        settings.MFE_CAPTURE_DRAWDOWN_PCT = old_drawdown
        settings.MFE_CAPTURE_PROTECT_SHARE = old_share
        settings.MFE_CAPTURE_START_PCT = old_start


def test_before_tp1_adaptive_mfe_capture_can_be_disabled():
    svc = ExitPolicyService()

    old_enabled = settings.MFE_CAPTURE_ENABLED
    try:
        settings.MFE_CAPTURE_ENABLED = False

        decision = svc.before_tp1_decision(
            side="long",
            entry_price=100.0,
            current_price=100.7,
            stop_price=99.0,
            mfe_pct=1.2,
            symbol=None,
        )

        assert decision.reason != "adaptive_mfe_capture"
    finally:
        settings.MFE_CAPTURE_ENABLED = old_enabled


def test_exit_policy_has_no_stale_exit_pct_reference():
    import re
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "services" / "exit_policy.py"

    assert re.search(r"(?<!protective_)\bexit_pct\b", source.read_text(encoding="utf-8")) is None


def test_exit_policy_runtime_guard_reports_protected_pct_runtime():
    guard = ExitPolicyService.runtime_guard()

    assert guard["ok"] is True
    assert guard["runtime"] == "protected_pct_v4"
    assert guard["stale_exit_pct_reference"] is False


# ──────────────────────────────────────────────────────────────────────────────
# Ярус 2: фиксация в «мёртвой зоне» MFE (#trend-capture-band-2026-07-25)
# ──────────────────────────────────────────────────────────────────────────────

def _trend_band_call(svc, *, mfe, current_pct, entry=100.0, notional=500.0):
    """Трендовая сделка с заданными MFE и текущим результатом."""
    return svc.before_tp1_decision(
        side="long",
        entry_price=entry,
        current_price=entry * (1 + current_pct / 100),
        stop_price=entry * 0.99,
        tp1_price=entry * 1.02,
        mfe_pct=mfe,
        trade_mode="trend",
        position_notional_usdt=notional,
        market_type="swap",
        symbol=None,
    )


def test_trend_capture_band_fires_in_dead_zone():
    """Модальный случай тренда: MFE 0.70% (ниже ride_min 0.8) и отдали 25% пика.
    До правки ЕДИНСТВЕННЫМ механизмом был безубыток-замок — ветка ride делает
    ранний return, из-за чего adaptive_mfe_capture в тренде недостижим."""
    svc = ExitPolicyService()
    old = settings.BREAKEVEN_LOCK_ENABLED
    try:
        settings.BREAKEVEN_LOCK_ENABLED = False   # изолируем ярус 2
        d = _trend_band_call(svc, mfe=0.70, current_pct=0.50)
        assert d.exit is True
        assert d.reason == "trend_capture_band"
        # филл не лучше рынка и не хуже уровня трейла
        assert d.exit_price <= 100.0 * (1 + 0.50 / 100) + 1e-9
    finally:
        settings.BREAKEVEN_LOCK_ENABLED = old


def test_trend_capture_band_does_not_cut_runners():
    """Как только MFE дошёл до ride_min, ярус 2 отключается — управление
    передаётся широкому ride-трейлу, раннера не режем тугим give=0.25."""
    svc = ExitPolicyService()
    old = settings.BREAKEVEN_LOCK_ENABLED
    try:
        settings.BREAKEVEN_LOCK_ENABLED = False
        ride_min = float(settings.TREND_RIDE_MIN_MFE_TO_PROTECT_PCT)
        # MFE выше ride_min, отдали 25% — для яруса 2 это был бы выход,
        # для широкого ride (give 0.50) — ещё нет.
        d = _trend_band_call(svc, mfe=ride_min + 0.4, current_pct=(ride_min + 0.4) * 0.75)
        assert d.reason != "trend_capture_band"
    finally:
        settings.BREAKEVEN_LOCK_ENABLED = old


def test_trend_capture_band_respects_cost_floor():
    """Ниже пола (net_safe / MIN_PROTECTIVE_EXIT_PCT) фиксировать нечего —
    издержки съедят фиксацию, держим дальше."""
    svc = ExitPolicyService()
    old_lock = settings.BREAKEVEN_LOCK_ENABLED
    # (#band-floor-2026-07-27) У яруса 2 теперь СВОЙ пол — проверяем именно его.
    old_floor = settings.TREND_CAPTURE_FLOOR_PCT
    try:
        settings.BREAKEVEN_LOCK_ENABLED = False
        settings.TREND_CAPTURE_FLOOR_PCT = 5.0   # заведомо недостижимый пол
        d = _trend_band_call(svc, mfe=0.70, current_pct=0.50)
        assert d.exit is False
    finally:
        settings.BREAKEVEN_LOCK_ENABLED = old_lock
        settings.TREND_CAPTURE_FLOOR_PCT = old_floor


def test_protective_gates_are_reachable_for_real_mfe():
    """(#gate-recalib-2026-07-25) Гейты должны быть достижимы на реальном
    распределении MFE: максимум в выборке 264–282 = 1.535%, медиана 0.64%.
    Порог 1.80 не достигался НИ РАЗУ — защитный выход не срабатывал."""
    assert settings.MIN_PROTECTIVE_EXIT_PCT < 1.535, "гейт выше любого MFE в истории"
    assert settings.MIN_PROTECTIVE_EXIT_PCT >= settings.NET_SAFE_FLOOR_SWAP_PCT, (
        "гейт не должен опускаться ниже издержек"
    )
    # $-гейт должен пропускать типовую позицию (маржа 130 USDT, фиксация 0.40%)
    typical_net = 130.0 * (settings.MIN_PROTECTIVE_EXIT_PCT - 0.15) / 100
    assert settings.MIN_PROTECTIVE_NET_USDT <= typical_net, (
        "$-гейт блокирует защитный выход на типовой позиции"
    )


def test_breakeven_lock_covers_the_band_below_trend_capture_arm():
    """(#flow-confirm-decision-2026-07-25) Полоса MFE между вооружением замка и
    вооружением trend_capture_band обслуживается ТОЛЬКО замком.

    band не может фиксировать ниже пола (mfe*(1-give) >= MIN_PROTECTIVE_EXIT_PCT),
    поэтому при MFE ~0.40–0.50% единственный механизм — фиксированный замок.
    EXIT_REQUIRE_FLOW_CONFIRM=True глушит его в тонком стакане (поток не
    подтверждает) и открывает мини-версию мёртвой зоны: замер на реальных
    траекториях дал −0.54 USDT на трёх сделках (#275/#268/#267).
    """
    band_arm_effective = float(settings.MIN_PROTECTIVE_EXIT_PCT) / (
        1.0 - float(settings.TREND_CAPTURE_GIVEBACK_SHARE)
    )
    assert band_arm_effective > float(settings.BREAKEVEN_LOCK_ARM_PCT), (
        "полосы между замком и band нет — тест потерял смысл, пересчитать пороги"
    )

    svc = ExitPolicyService()
    # MFE внутри полосы: band вооружиться не может, замок обязан отработать.
    mfe = (float(settings.BREAKEVEN_LOCK_ARM_PCT) + band_arm_effective) / 2

    # (#kill-losers-2026-07-28) Замок выключен по итогам замера, поэтому здесь
    # он включается явно: проверяется механика полосы, а не то, применяем ли мы
    # её в бою. Цена отключения известна и принята — сделки в этой полосе теперь
    # доходят до стопа или до трейла.
    old = settings.BREAKEVEN_LOCK_ENABLED
    try:
        settings.BREAKEVEN_LOCK_ENABLED = True
        d = svc.before_tp1_decision(
            side="long",
            entry_price=100.0,
            current_price=100.05,          # откат к полу замка
            stop_price=99.0,
            tp1_price=102.0,
            mfe_pct=mfe,
            trade_mode="trend",
            position_notional_usdt=500.0,
            market_type="swap",
            symbol=None,
            flow_against=False,            # тонкий стакан: поток НЕ подтверждает
        )
    finally:
        settings.BREAKEVEN_LOCK_ENABLED = old

    assert d.exit is True, "в полосе между замком и band не осталось механизма фиксации"
    assert d.reason == "breakeven_lock"


def test_flow_confirm_stays_off_so_the_lock_works_in_thin_books():
    """Настройка выбрана по замеру, а не по умолчанию — фиксируем решение.
    Переворот в True должен быть осознанным и пересчитанным (так уже случилось
    в коммите 4df5092, который молча откатил решения аудита)."""
    assert settings.EXIT_REQUIRE_FLOW_CONFIRM is False
