"""Новая настройка не должна молча падать в «Прочее»
(#config-grouping-ratchet-2026-09-04).

Страница Конфигурации — инструмент ответа на вопрос «что реально действует».
Ключ, не попавший ни в одну группу, уезжает в «Прочее» и вдобавок теряет
закрепление: страница начинает советовать вычистить его из блупринта, потому
что «ни выключатель, ни лимит».

За один день 04.09 правило отстало ТРИЖДЫ: утром так потерялись ANTI_CHOP_*,
POST_TP1_*, OKX_ и ACTIVE_EXCHANGE (среди них — порог, вокруг которого шла вся
работа, и переключатель биржи), вечером — LOOP_SKIP_HEARTBEAT_SEC. Правило
ведётся руками и само не пополняется, поэтому здесь стоит храповик.

Список ниже — это ДОЛГ, а не норма. Он обязан только уменьшаться: 158 из 641
полей сейчас не отнесены ни к одной группе. Добавлять в него новые имена
означает расписаться в том, что настройку негде показать.
"""
from __future__ import annotations

from core.config import Settings
from services.config_inspector import _group_of

OTHER = "Прочее"  # "Прочее"

# Уже существующий долг. Только сокращать.
_KNOWN_UNGROUPED: frozenset[str] = frozenset({
    "ADAPTIVE_TRAIL_DRAWDOWN_PCT", "AFFILIATE_FREE_VIP_DAYS", "ALLOW_MARKET_MOCK",
    "ALLOW_SHORTS", "ALLOW_WEAK_VOLUME_TREND_ENTRIES", "ATR_CAPTURE_START_MULT",
    "ATR_FAILED_SETUP_DEEP_MULT", "ATR_FAILED_SETUP_MID_MULT",
    "ATR_FAILED_SETUP_SOFT_MULT", "ATR_PROTECT_START_MULT", "ATR_TRAIL_START_MULT",
    "CAPITAL_ENVELOPE_ARB_PCT", "CAPITAL_ENVELOPE_DIRECTIONAL_PCT",
    "CAPITAL_ENVELOPE_GRID_PCT", "DAILY_REPORT_MIN_SAMPLE", "EGRESS_CACHE_TTL_SEC",
    "EGRESS_DNS_TIMEOUT_SEC", "EGRESS_GUARD_ENABLED", "EGRESS_MONITOR_ENABLED",
    "EGRESS_MONITOR_INTERVAL_SEC", "EGRESS_MONITOR_PATH", "EGRESS_MONITOR_TIMEOUT_SEC",
    "ENTRY_TIMING_VETO_ENABLED", "ENTRY_ZONE_ADVERSE_CVD_RATIO",
    "ENTRY_ZONE_CVD_MIN_TRADES", "ENTRY_ZONE_DEPTH_AWARE",
    "ENTRY_ZONE_LIMIT_WIDTH_PCT", "ENTRY_ZONE_MAX_DRIFT_PCT",
    "ENTRY_ZONE_MAX_MARKET_SPREAD_PCT", "ENTRY_ZONE_MIN_NEAR_DEPTH_SHARE",
    "ENTRY_ZONE_TTL_SEC", "ENTRY_ZONE_WIDTH_PCT", "EXCHANGE_RECONCILIATION_ENABLED",
    "EXCHANGE_SWITCH_GUARD_ENABLED", "EXHAUSTION_LEVEL_DIST_PCT",
    "EXHAUSTION_RSI_OVERBOUGHT", "EXHAUSTION_RSI_OVERSOLD", "EXPECTANCY_GATE_ENABLED",
    "EXPECTANCY_MIN_HISTORY", "EXPECTANCY_PTN_MAX_PCT", "EXPECTANCY_TOLERANCE_USDT",
    "EXPECTANCY_WINDOW_HOURS", 
    "INTEL_EVENT_DEDUP_MINUTES", "LEARNING_SETUP_MIN_SCORE",
    "LEARNING_SETUP_MIN_TREND_ALIGNMENT", "LEARNING_SETUP_MIN_VOLUME_CONFIRMATION",
    "LEARNING_SETUP_STRONG_SCORE", "LEARNING_TREND_CONTINUATION_MIN_FINAL_SCORE",
    "LEARNING_TREND_CONTINUATION_MIN_STRUCTURE_QUALITY",
    "LEARNING_TREND_CONTINUATION_MIN_TREND_ALIGNMENT",
    "LEARNING_TREND_CONTINUATION_MIN_VOLUME_CONFIRMATION", "LEVELS_CONTEXT_TF",
    "LEVELS_ENTRY_TF", "LEVELS_MAX_STOP_PCT", "LEVELS_MIN_STOP_PCT",
    "LEVELS_SIGNAL_TF", "LEVELS_STOP_ATR_MULT", "LEVELS_STRUCT_STOP_BUFFER_PCT",
    "LEVELS_STRUCT_STOP_ENABLED", "LEVELS_VP_BINS", "LEVELS_VP_ENABLED",
    "LEVELS_VP_STOP_BUFFER_PCT", "LEVELS_VP_STOP_MAX_EXTRA_PCT", "LEVELS_VP_TF",
    "LEVELS_VP_TP_BUFFER_PCT", "LEVELS_VP_TP_MIN_DIST_PCT", "LEVELS_VP_TTL_SEC",
    "LIQ_BLOCK_ENTRY", "LIQ_EXIT_MAX_AGE_SEC", "LIQ_EXIT_SPREAD_MULT",
    "LIQ_PROTECT_EXIT", "LIQ_SPREAD_ABS_MAX_BPS", "LIQ_SPREAD_BASELINE_ALPHA",
    "LIQ_SPREAD_BASELINE_MULT", "LIQ_SPREAD_MIN_BASELINE_BPS", "MANAGE_INTERVAL_SEC",
    "MAX_ACTIVE_SIGNALS", "MAX_ACTIVE_SIGNALS_PER_SYMBOL", "MAX_DAILY_LOSS_PCT",
    "MAX_DRAWDOWN_PCT", "MAX_TRADES_PER_DAY", "MIN_NET_PNL_RELAX_MARGIN_PCT",
    "MIN_NET_PNL_TP1_USDT", "MIN_NET_PNL_TP2_USDT", "MIN_NET_RR_BLENDED",
    "MIN_POST_TP1_EXIT_PCT", "MIN_PROTECTIVE_EXIT_PCT", "MIN_PROTECTIVE_NET_USDT",
    "MTF_SNAPSHOT_TTL_SEC", "NET_SAFE_FLOOR_SPOT_PCT", "NET_SAFE_FLOOR_SWAP_PCT",
    "NEWS_ENABLED", "NEXT_PUBLIC_API_URL", "OVERHEAT_ENTRY_PENALTY_M1",
    "OVERHEAT_ENTRY_PENALTY_M5", "PAPER_STOP_ADVERSE_SLIPPAGE_PCT", "POSTGRES_DB",
    "POSTGRES_HOST", "POSTGRES_PASSWORD", "POSTGRES_PORT", "POSTGRES_USER",
    "PUBLIC_API_URL", "RADAR_WATCH_MIN_TOTAL_SCORE", "REGIME_EXP_CACHE_TTL_SEC",
    "REGIME_EXP_FLOOR_AT_R", "REGIME_EXP_MAX_ROWS", "REGIME_EXP_MIN_HISTORY",
    "REGIME_EXP_MIN_MULT", "REGIME_EXP_PRIOR_N", "REGIME_EXP_SIZING_ENABLED",
    "REGIME_EXP_WINDOW_HOURS", "REJECT_EVENT_THROTTLE_MINUTES", "RESEARCH_COST_ATR",
    "RESEARCH_WF_FOLDS", "RSI_DYNAMIC_ENABLED", "RSI_DYN_FAN_FULL_ATR",
    "RSI_DYN_FAN_WEIGHT", "RSI_DYN_HARD_BLOCK_GAP", "RSI_DYN_HTF_WEIGHT",
    "RSI_DYN_MAX_LIFT", "RSI_DYN_VOLUME_STRONG_RATIO", "RSI_DYN_VOLUME_WEIGHT",
    "RSI_LATE_ENTRY_RISK_MULTIPLIER", "SCAN_INTERVAL_SEC", "SETUP_MIN_TOTAL_SCORE",
    "SETUP_REACH_CACHE_TTL_SEC", "SETUP_REACH_ENABLED", "SETUP_REACH_MAX_ROWS",
    "SETUP_REACH_MIN_HISTORY", "SETUP_REACH_MIN_TARGET_PCT", "SETUP_REACH_SL_QUANTILE",
    "SETUP_REACH_TP_QUANTILE", "SETUP_REACH_WINDOW_HOURS", "SETUP_TREND_SCORE_MARGIN",
    "SETUP_VOTING_MIN_TOTAL_SCORE", "SHORT_ALERT_THROTTLE_MINUTES", "SIGNAL_PROFILE",
    "STRICT_SETUP_MIN_SCORE", "TRADEABLE_REGIMES", "TRADE_OUTCOMES_PATH",
    "TRAJ_MAX_POINTS", "TRAJ_MIN_STEP_PCT", "TRAJ_RECORD_ENABLED",
    "UNIFIED_MARGIN_ACCOUNTING", "VALIDATION_FAILED_SETUP_MAX_PCT",
    "VALIDATION_MIN_CLOSED_SIGNALS", "VALIDATION_POSITIVE_THEN_NEGATIVE_MAX_PCT",
    "VIP_INVITE_EXPIRE_HOURS", "VIP_INVITE_LINK", "VIP_STARS_PRICE_30",
    "VIP_STARS_PRICE_90", "WALKFORWARD_ENABLED", "WALKFORWARD_FOLDS",
    "WALKFORWARD_INTERVAL_SEC", "WALKFORWARD_LOG_PATH",
})


def _fields() -> list[str]:
    return list(getattr(Settings, "model_fields", {}) or {})


def test_no_new_settings_land_in_the_other_bucket():
    ungrouped = {f for f in _fields() if _group_of(f) == OTHER}
    new = sorted(ungrouped - _KNOWN_UNGROUPED)

    assert not new, (
        "настройка не отнесена ни к одной группе Конфигурации: она уедет в "
        "«Прочее» и потеряет закрепление в блупринте — добавь префикс в "
        f"_GROUPS и _PINNED_PREFIXES (config_inspector): {new}"
    )


def test_the_debt_list_does_not_rot():
    """Имя, которое уже сгруппировали или удалили, обязано уйти из долга —
    иначе список превращается в свалку и перестаёт что-либо значить."""
    ungrouped = {f for f in _fields() if _group_of(f) == OTHER}
    stale = sorted(_KNOWN_UNGROUPED - ungrouped)

    assert not stale, f"эти ключи больше не в «Прочее», убери их из списка: {stale}"


def test_loop_knobs_are_grouped_and_pinned():
    """Регресс на промах 04.09: LOOP_SKIP_HEARTBEAT_SEC — параметр поведения
    цикла, а страница предлагала вычистить его из блупринта."""
    from services.config_inspector import is_pinned_on_purpose

    assert _group_of("LOOP_SKIP_HEARTBEAT_SEC") != OTHER
    assert is_pinned_on_purpose("LOOP_SKIP_HEARTBEAT_SEC")
