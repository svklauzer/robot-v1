from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class AntiDrainConfig:
    # Defaults aligned with settings defaults (config.py).
    # robot_loop always constructs this from settings, so dataclass defaults
    # are used only in tests that instantiate AntiDrainConfig directly.
    min_confidence: float = 60.0
    allow_grade_c: bool = False
    allow_watch_escalated_candidates: bool = False
    min_net_rr_tp1: float = 0.55
    min_net_rr_tp2: float = 0.90
    min_expected_edge_after_costs_usdt: float = 1.20
    max_position_margin_pct: float = 12.0
    max_used_margin_pct: float = 30.0
    max_open_positions: int = 2
    max_active_signals_per_symbol: int = 1
    max_daily_loss_pct: float = 2.0
    max_drawdown_pct: float = 10.0
    block_weak_structure: bool = True
    block_long_overheated: bool = True
    block_short_oversold: bool = True
    # POSITION: награда на TP2 (TP1 частичный, RR_tp1<1 by design). Проверяем
    # экономику по TP2, иначе трендовая сделка никогда не пройдёт "TP1≥стоп".
    economics_use_tp2: bool = False
    # (#leak-cost-bleed) Минимальный модельный TP1-нетто (USDT) ПОСЛЕ издержек.
    # Аудит: издержки ~0.3-0.45% round-trip; сделки выходили у входа (failed_setup/
    # breakeven) и банкали комиссию убытком, хотя TP2-экономика проходила. TP1 —
    # точка де-риска, она НЕ должна быть модельно под водой. 0.0 → TP1 хотя бы
    # не отрицателен после costs (мягко, не режет TP2-логику).
    min_net_pnl_tp1_usdt: float = 0.0


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def should_open_signal(signal: Any, account_state: Any, cfg: AntiDrainConfig) -> tuple[bool, str]:
    symbol = str(_get(signal, "symbol", "")).upper()
    side = str(_get(signal, "side", "")).lower()
    grade = str(_get(signal, "grade", "")).upper()
    rationale = str(_get(signal, "rationale", "")).lower()
    confidence = float(_get(signal, "confidence", 0) or 0)

    equity = float(_get(account_state, "equity_usdt", 0) or 0)
    used_margin = float(_get(account_state, "used_margin_usdt", 0) or 0)
    daily_pnl = float(_get(account_state, "daily_pnl_usdt", 0) or 0)
    drawdown_pct = float(_get(account_state, "drawdown_pct", 0) or 0)
    open_positions_count = int(_get(account_state, "open_positions_count", 0) or 0)
    active_by_symbol = _get(account_state, "active_signals_by_symbol", {}) or {}

    required_margin = float(_get(signal, "required_margin", 0) or 0)
    net_rr_tp1 = float(_get(signal, "net_rr_tp1", 0) or 0)
    net_rr_tp2 = float(_get(signal, "net_rr_tp2", 0) or 0)
    net_pnl_tp1 = float(_get(signal, "net_pnl_tp1", 0) or 0)
    net_pnl_tp2 = float(_get(signal, "net_pnl_tp2", 0) or 0)
    net_pnl_stop = float(_get(signal, "net_pnl_stop", 0) or 0)

    if equity <= 0:
        return False, "blocked_no_equity"
    if open_positions_count >= cfg.max_open_positions:
        return False, "blocked_max_open_positions"
    if int(active_by_symbol.get(symbol, 0)) >= cfg.max_active_signals_per_symbol:
        return False, "blocked_active_signal_per_symbol"
    if daily_pnl <= -(equity * cfg.max_daily_loss_pct / 100):
        return False, "blocked_daily_loss_limit"
    if drawdown_pct >= cfg.max_drawdown_pct:
        return False, "blocked_max_drawdown"
    if (used_margin / equity * 100) >= cfg.max_used_margin_pct:
        return False, "blocked_total_margin_limit"
    if (required_margin / equity * 100) > cfg.max_position_margin_pct:
        return False, "blocked_position_margin_limit"
    if confidence < cfg.min_confidence:
        return False, "blocked_low_confidence"
    if grade == "C" and not cfg.allow_grade_c:
        return False, "blocked_grade_c"
    if "watch_" in rationale and not cfg.allow_watch_escalated_candidates:
        return False, "blocked_watch_escalated_candidate"
    if cfg.block_weak_structure and "weak_structure" in rationale:
        return False, "blocked_weak_structure"
    if cfg.block_long_overheated and side == "long" and "overheated" in rationale:
        return False, "blocked_long_overheated"
    if cfg.block_short_oversold and side == "short" and "oversold" in rationale:
        return False, "blocked_short_oversold"
    if net_rr_tp1 < cfg.min_net_rr_tp1:
        return False, "blocked_low_net_rr_tp1"
    # (#leak-cost-bleed) TP1-нетто после издержек не должен быть под водой: иначе
    # ранний/софт-выход у входа гарантированно банкает комиссию убытком.
    if net_pnl_tp1 < cfg.min_net_pnl_tp1_usdt:
        return False, "blocked_tp1_below_cost"
    if net_rr_tp2 < cfg.min_net_rr_tp2:
        return False, "blocked_low_net_rr_tp2"
    econ_ref = net_pnl_tp2 if cfg.economics_use_tp2 else net_pnl_tp1
    if econ_ref < abs(net_pnl_stop) + cfg.min_expected_edge_after_costs_usdt:
        return False, "blocked_bad_trade_economics"
    return True, "allowed_anti_drain_ok"
