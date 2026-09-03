"""Снимок настроек, при которых принято решение по сделке.

Зачем. Сделка записывает СВОЙ результат, но не записывает правила, по которым
её открыли и закрыли. Из-за этого вопрос «при каких настройках система
зарабатывает» неразрешим в принципе: метка есть, признака нет. Историю с
12.06 по 28.07 разобрать по настройкам нельзя — config.py правился почти
ежедневно, а в сделках от него не осталось ничего.

Здесь снимаются ровно те величины, которые ГЕЙТЯТ вход, задают РАЗМЕР и
определяют ВЫХОД. Не весь Settings: сотня посторонних полей превратит снимок в
шум и раздует plan_json. Критерий включения простой — параметр должен уметь
изменить исход сделки.

`fingerprint` — короткий хэш набора. По нему сделки группируются в «поколения
конфига» одним сравнением, без разбора вложенных словарей.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from core.config import settings


def _g(name: str, default: Any = None) -> Any:
    return getattr(settings, name, default)


def snapshot(
    *,
    market_type: str,
    fee_rate: float | None = None,
    leverage: int | float | None = None,
    is_scalp: bool = False,
    gate_thresholds: dict | None = None,
) -> dict:
    """Действующие параметры решения.

    `fee_rate` — ставка тейкера рынка ЭТОЙ сделки; от неё считаются пороги
    выхода, поэтому она часть конфига решения, а не рыночный факт.
    `gate_thresholds` — фактические пороги production_gate для грейда сделки
    (они зависят от грейда и режима, восстановить их постфактум нельзя).
    """
    fee = float(fee_rate) if fee_rate is not None else None
    slip = float(_g("SLIPPAGE_BUFFER_PCT", 0.0002))
    round_trip_pct = (fee * 2 + slip) * 100 if fee is not None else None

    cfg: dict[str, Any] = {
        # ── рынок и его цена ────────────────────────────────────────────────
        "market": {
            "market_type": market_type,
            "taker_fee": fee,
            "slippage_buffer_pct": slip,
            "round_trip_pct": round(round_trip_pct, 4) if round_trip_pct is not None else None,
            "leverage": float(leverage) if leverage is not None else None,
        },
        # ── гейты входа ─────────────────────────────────────────────────────
        "entry_gate": {
            "thresholds": gate_thresholds or {},
            "tradeable_regimes": str(_g("TRADEABLE_REGIMES", "")),
            "allow_shorts": bool(_g("ALLOW_SHORTS", True)),
            "max_trades_per_day": int(_g("MAX_TRADES_PER_DAY", 0) or 0),
            "max_active_signals": int(_g("MAX_ACTIVE_SIGNALS", 0) or 0),
            "max_active_per_symbol": int(_g("MAX_ACTIVE_SIGNALS_PER_SYMBOL", 0) or 0),
        },
        "anti_drain": {
            "enabled": bool(_g("ANTI_DRAIN_ENABLED", True)),
            "min_confidence": float(_g("ANTI_DRAIN_MIN_CONFIDENCE", 0)),
            "min_net_rr_tp1": float(
                _g("SCALP_ANTI_DRAIN_MIN_NET_RR_TP1", 0) if is_scalp else _g("ANTI_DRAIN_MIN_NET_RR_TP1", 0)
            ),
            "min_net_rr_tp2": float(
                _g("SCALP_ANTI_DRAIN_MIN_NET_RR_TP2", 0) if is_scalp else _g("ANTI_DRAIN_MIN_NET_RR_TP2", 0)
            ),
            "min_edge_after_costs_usdt": float(
                _g("SCALP_ANTI_DRAIN_MIN_EDGE_AFTER_COSTS_USDT", 0) if is_scalp
                else _g("ANTI_DRAIN_MIN_EDGE_AFTER_COSTS_USDT", 0)
            ),
            "max_open_positions": int(_g("ANTI_DRAIN_MAX_OPEN_POSITIONS", 0) or 0),
            "max_used_margin_pct": float(_g("ANTI_DRAIN_POSITION_MAX_USED_MARGIN_PCT", 0)),
        },
        # ── размер ──────────────────────────────────────────────────────────
        "sizing": {
            "risk_per_trade_pct": float(_g("RISK_PER_TRADE_PCT", 0)),
            "max_position_margin_pct": float(
                _g("SCALP_MAX_POSITION_MARGIN_PCT", 0) if is_scalp else _g("MAX_POSITION_MARGIN_PCT", 0)
            ),
            "max_used_margin_pct": float(_g("MAX_USED_MARGIN_PCT", 0)),
            "dynamic_alloc": bool(_g("ENABLE_DYNAMIC_MARGIN_ALLOC", True)),
            "dynamic_fair_share": bool(_g("DYNAMIC_MARGIN_FAIR_SHARE", True)),
            "grade_b_cap_pct_of_free": float(_g("DYNAMIC_MARGIN_B_CAP_PCT_OF_FREE", 0)),
        },
        # ── выход: то, что закрывает сделку ─────────────────────────────────
        "exit": {
            "net_safe_floor_pct": float(
                _g("NET_SAFE_FLOOR_SWAP_PCT", 0.30) if (fee is not None and fee <= 0.001)
                else _g("NET_SAFE_FLOOR_SPOT_PCT", 0.60)
            ),
            "breakeven_lock_enabled": bool(_g("BREAKEVEN_LOCK_ENABLED", False)),
            "breakeven_lock_arm_pct": float(_g("BREAKEVEN_LOCK_ARM_PCT", 0)),
            "breakeven_lock_floor_pct": float(_g("BREAKEVEN_LOCK_FLOOR_PCT", 0)),
            "breakeven_lock_cost_buffer_pct": float(_g("BREAKEVEN_LOCK_COST_BUFFER_PCT", 0)),
            "breakeven_lock_hard_floor_pct": float(_g("BREAKEVEN_LOCK_HARD_FLOOR_PCT", 0)),
            # Эффективный пол замка: именно он решает, может ли «безубыток»
            # закрыться в плюс. Считается из ставки рынка сделки.
            "breakeven_lock_effective_floor_pct": (
                round(max(
                    float(_g("BREAKEVEN_LOCK_FLOOR_PCT", 0)),
                    round_trip_pct + float(_g("BREAKEVEN_LOCK_COST_BUFFER_PCT", 0)),
                ), 4) if round_trip_pct is not None else None
            ),
            "mfe_capture_enabled": bool(_g("MFE_CAPTURE_ENABLED", True)),
            "mfe_capture_start_pct": float(_g("MFE_CAPTURE_START_PCT", 0)),
            "mfe_capture_drawdown_pct": float(_g("MFE_CAPTURE_DRAWDOWN_PCT", 0)),
            "protective_mfe_start_pct": float(_g("PROTECTIVE_MFE_START_PCT", 0)),
            "min_protective_exit_pct": float(_g("MIN_PROTECTIVE_EXIT_PCT", 0)),
            "min_protective_net_usdt": float(_g("MIN_PROTECTIVE_NET_USDT", 0)),
            "min_post_tp1_exit_pct": float(_g("MIN_POST_TP1_EXIT_PCT", 0)),
            "failed_setup_min_age_sec": float(_g("FAILED_SETUP_MIN_AGE_SEC", 0)),
            "failed_setup_mfe_absolute_min_pct": float(_g("FAILED_SETUP_MFE_ABSOLUTE_MIN_PCT", 0)),
            "scalp_time_stop_min": float(_g("SCALP_TIME_STOP_MIN", 0)),
            "range_time_stop_min": float(_g("RANGE_TIME_STOP_MIN", 0)),
            "tp1_partial_enabled": bool(_g("TP1_PARTIAL_ENABLED", True)),
            "tp1_partial_share": float(_g("TP1_PARTIAL_CLOSE_SHARE", 0)),
            "exit_require_flow_confirm": bool(_g("EXIT_REQUIRE_FLOW_CONFIRM", True)),
        },
        # ── защиты и фильтры ────────────────────────────────────────────────
        "guards": {
            "symbol_perf_block_max_winrate": float(_g("SYMBOL_PERF_BLOCK_MAX_WINRATE", 0)),
            "symbol_perf_reduce_max_winrate": float(_g("SYMBOL_PERF_REDUCE_MAX_WINRATE", 0)),
            "symbol_perf_lookback": int(_g("SYMBOL_PERF_LOOKBACK", 0) or 0),
            "reentry_cooldown_enabled": bool(_g("REENTRY_COOLDOWN_ENABLED", True)),
            "post_loss_cooldown_min": float(_g("POST_LOSS_COOLDOWN_MIN", 0)),
            "orderbook_engine": bool(_g("ENABLE_ORDERBOOK_ENGINE", False)),
            "ob_gate_entries": bool(_g("OB_GATE_ENTRIES", True)),
            "ob_cvd_min_trades": int(_g("OB_CVD_MIN_TRADES", 0) or 0),
            "corr_cluster_max_same_dir": int(_g("CORR_CLUSTER_MAX_SAME_DIR", 0) or 0),
            # (#engine-slots-2026-08-03) Считается ли лимит направления внутри
            # движка. Без этого поля сделки до и после правки неразличимы в
            # снимке, хотя принимались по разным правилам.
            "corr_cluster_per_engine": bool(_g("CORR_CLUSTER_PER_ENGINE", False)),
            "corr_cluster_portfolio_max_same_dir": int(
                _g("CORR_CLUSTER_PORTFOLIO_MAX_SAME_DIR", 0) or 0),
            "reentry_cooldown_per_engine": bool(_g("REENTRY_COOLDOWN_PER_ENGINE", False)),
            # Наблюдающие оси: на решение не влияют, но их пороги определяют,
            # что записано в plan_json.trend_trigger и plan_json.tz_shadow.
            "trend_trigger_mode": str(_g("TREND_TRIGGER_MODE", "shadow")).lower(),
            "trend_max_extension_atr": float(_g("TREND_MAX_EXTENSION_ATR", 0)),
            "tz_adx_min": float(_g("TZ_ADX_MIN", 0)),
            "tz_stoch_zone": float(_g("TZ_STOCH_ZONE", 0)),
            # (#tz-enforce-2026-08-03) Условия ТЗ перестали быть только
            # наблюдающими — режим и список активных условий меняют ВЫБОРКУ
            # входов, поэтому обязаны попасть в отпечаток: иначе сделки до и
            # после включения смешаются в одной статистике незаметно.
            "tz_mode": str(_g("TZ_MODE", "shadow")).lower(),
            "tz_enforce_conditions": str(_g("TZ_ENFORCE_CONDITIONS", "")),
            "tz_enforce_min_sample": int(_g("TZ_ENFORCE_MIN_SAMPLE", 0)),
            # (#anti-chop-young-trend-2026-09-03) Альтернативный путь anti-chop
            # меняет ВЫБОРКУ входов (пускает молодой тренд с узким веером EMA) —
            # значит это ось конфига, а не наблюдение, и обязана попасть в
            # отпечаток: иначе сделки до и после включения смешаются в одной
            # статистике незаметно, как это уже было с tz_mode.
            "anti_chop_young_trend_enabled": bool(_g("ANTI_CHOP_YOUNG_TREND_ENABLED", False)),
            "anti_chop_young_adx_min": float(_g("ANTI_CHOP_YOUNG_ADX_MIN", 0)),
            "anti_chop_young_adx_rise_min": float(_g("ANTI_CHOP_YOUNG_ADX_RISE_MIN", 0)),
            "anti_chop_young_di_spread_min": float(_g("ANTI_CHOP_YOUNG_DI_SPREAD_MIN", 0)),
            # (#tp-reachability-2026-08-03) Порог достижимости цели — тоже ось,
            # меняющая выборку.
            "tp_reach_mode": str(_g("TP_REACH_MODE", "shadow")).lower(),
            # 24.08.2026: `tp_reach_max_ratio` снят вместе с самим отношением.
            # Порог частоты выводится из RR сделки и осью конфига не является —
            # осью остаётся запас к нему.
            "tp_reach_ev_margin": float(_g("TP_REACH_EV_MARGIN", 1.0)),
            "setup_reach_enabled": bool(_g("SETUP_REACH_ENABLED", False)),
            "regime_exp_sizing_enabled": bool(_g("REGIME_EXP_SIZING_ENABLED", False)),
        },
        "ml": {
            "mode": str(_g("ML_MODE", "off")).lower(),
            "min_score_to_trade": float(_g("ML_MIN_SCORE_TO_TRADE", 0)),
            "size_alloc_enabled": bool(_g("ML_SIZE_ALLOC_ENABLED", True)),
        },
    }

    cfg["fingerprint"] = fingerprint(cfg)
    return cfg


def fingerprint(cfg: dict) -> str:
    """Короткий хэш набора настроек — метка поколения конфига.

    Считается по всему снимку, кроме самого отпечатка. Сделки с одинаковым
    отпечатком принимались по одним правилам, и только их можно сравнивать
    между собой.
    """
    payload = {k: v for k, v in cfg.items() if k != "fingerprint"}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]
