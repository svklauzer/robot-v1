"""services/ml_features.py — единый источник фич и меток мета-лейблера.

v6 (#ml-audit-2026-11-23). Аудит живого shadow-report (870 закрытых):
live_auc=0.456, live_auc_vs_train_label=0.498, verdict=no_edge_yet при
val_auc=0.848 / wf_auc=0.747. Причины:
  1. набор фич без драйверов PnL (regime/DI/ADX/momentum/extension), зато
     с антипредсказательным confidence (грейд A хуже B);
  2. train на фичах филла закрытой сделки vs live на фичах кандидата
     (midpoint зоны) -> рассинхрон train/serve;
  3. метка beats_costs 0.3R награждает частоту мелких плюсов, а платит хвост.

v6: +regime one-hot, di_spread, adx14, adx_delta, momentum_ord, extension_atr,
is_long; -confidence; мёртвый _grade_ord удалён. row_to_features ПО-ПРЕЖНЕМУ
возвращает list[float] в порядке FEATURE_NAMES (контракт с ml_meta_labeler).

Совместимость: is_phantom_row / total_net_pnl / tp1_partial_net_pnl / row_to_label
сохранены (импортируются ml_meta_labeler и ml_shadow_report). total_net_pnl =
только closed_net_pnl (плоские total_net_pnl/tp1_partial — двойной счёт, игнор).
"""
from __future__ import annotations

import math
from typing import Any

from core.config import settings
from services.close_reasons import reached_tp2

CVD_MIN_TRADES: int = 10

FEATURE_VERSION: int = 6  # сброс старых несовместимых моделей (v5 confidence/2-dummy)
FEATURE_NAMES: list[str] = [
    "stop_distance_pct",
    "log_stop_distance",
    "net_rr_tp1",
    "rr_asymmetry",
    "spread_pct",
    "cvd_ratio",
    "hour_sin",
    "hour_cos",
    # regime one-hot (было только is_scalp/is_crt):
    "is_trend_up",
    "is_trend_down",
    "is_range",
    "is_reversal",
    "is_scalp",
    "is_crt",
    # направленные приборы разворота (доступны на входе, без lookahead):
    "di_spread",
    "adx14",
    "adx_delta",
    "momentum_ord",
    "extension_atr",
    "is_long",
]

_MOM_ORD = {"bullish": 1.0, "overheated": 1.5, "neutral": 0.0,
            "bearish": -1.0, "oversold": -1.5}


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _gate(row: dict, name: str) -> dict:
    """Гейт лежит в plan/plan_json (живой кандидат), в gates (jsonl) или плоско."""
    plan = row.get("plan") or row.get("plan_json") or {}
    for src in (row, plan if isinstance(plan, dict) else {}, row.get("gates") or {}):
        g = src.get(name)
        if isinstance(g, dict):
            return g
    return {}


def _depth(row: dict) -> dict:
    """Данные стакана: из лога (entry_depth), из plan/plan_json кандидата или плоско."""
    d = row.get("entry_depth")
    if isinstance(d, dict):
        return d
    plan = row.get("plan") or row.get("plan_json") or {}
    if isinstance(plan, dict) and isinstance(plan.get("entry_depth"), dict):
        return plan["entry_depth"]
    return row


def _entry_price(row: dict) -> float:
    # (#ml-audit) База цены входа едина для train и live. zone_mid (дефолт) —
    # середина зоны, как на входе (филла ещё нет); fill — фактический филл
    # закрытой сделки (прежнее поведение, рассинхрон train/serve).
    basis = str(getattr(settings, "ML_ENTRY_PRICE_BASIS", "zone_mid"))
    zone = row.get("entry_zone")
    if not isinstance(zone, dict):
        plan = row.get("plan") or row.get("plan_json") or {}
        zone = plan.get("entry_zone") if isinstance(plan, dict) else None
    mid = 0.0
    if isinstance(zone, dict):
        lo, hi = _f(zone.get("from")), _f(zone.get("to"))
        if lo > 0 and hi > 0:
            mid = (lo + hi) / 2.0
    if basis == "fill":
        lc = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
        px = _f(lc.get("entry_price"))
        if px > 0:
            return px
    return mid


def _hour_of_day(row: dict) -> float:
    raw = row.get("opened_at") or row.get("created_at")
    if raw:
        s = str(raw)
        for sep in ("T", " "):
            if sep in s:
                try:
                    parts = s.split(sep, 1)
                    if len(parts) > 1:
                        return float(int(parts[1][:2]))
                except (ValueError, IndexError):
                    break
    from datetime import datetime, timezone as _tz
    return float(datetime.now(_tz.utc).hour)


def _momentum(row: dict, mg: dict) -> str:
    mom = str(mg.get("momentum") or "")
    if mom:
        return mom
    # fallback: импульс зашит в причину входа (mtf_trend_<dir>_<momentum>_...),
    # работает и когда momentum_gate выключен (гейт не пишет вердикт).
    rat = str(row.get("rationale") or row.get("entry_reason") or "")
    toks = rat.split("_")
    if "trend" in toks:
        i = toks.index("trend")
        if i + 2 < len(toks):
            return toks[i + 2]
    return ""


def features_dict(row: dict) -> dict:
    """Фичи в виде dict (для логирования на входе / объяснимости)."""
    d = _depth(row)
    cvd_reliable = _f(d.get("cvd_trades")) >= float(CVD_MIN_TRADES)
    rr1 = _f(row.get("net_rr_tp1"))
    rr2 = _f(row.get("net_rr_tp2"))
    entry = _entry_price(row)
    stop = _f(row.get("stop_price"))
    stop_dist_pct = abs(entry - stop) / entry * 100.0 if entry > 1e-9 and stop > 0 else 0.0
    log_stop_dist = math.log(stop_dist_pct + 1e-5) if stop_dist_pct > 0 else 0.0
    hour = _hour_of_day(row)

    plan = row.get("plan") or row.get("plan_json") or {}
    regime = str(row.get("regime") or (plan.get("regime") if isinstance(plan, dict) else "") or "")
    trade_mode = str(row.get("trade_mode") or (plan.get("trade_mode") if isinstance(plan, dict) else "") or "").lower()
    rationale = str(row.get("rationale") or "").lower()
    side = str(row.get("side") or "").lower()

    tz = _gate(row, "tz_shadow")
    tt = _gate(row, "trend_trigger")
    mg = _gate(row, "momentum_gate")

    return {
        "stop_distance_pct": stop_dist_pct,
        "log_stop_distance": log_stop_dist,
        "net_rr_tp1": rr1,
        "rr_asymmetry": rr2 / rr1 if rr1 > 1e-9 else 0.0,
        "spread_pct": _f(d.get("spread_pct")),
        "cvd_ratio": _f(d.get("cvd_ratio")) if cvd_reliable else 0.0,
        "hour_sin": math.sin(2 * math.pi * hour / 24.0),
        "hour_cos": math.cos(2 * math.pi * hour / 24.0),
        "is_trend_up": 1.0 if regime == "trend_up_candidate" else 0.0,
        "is_trend_down": 1.0 if regime == "trend_down_candidate" else 0.0,
        "is_range": 1.0 if regime == "range" else 0.0,
        "is_reversal": 1.0 if "reversal" in regime else 0.0,
        "is_scalp": 1.0 if (regime == "scalp" or trade_mode == "scalp" or "scalp" in rationale) else 0.0,
        "is_crt": 1.0 if (regime == "crt" or trade_mode == "crt" or "crt" in rationale) else 0.0,
        "di_spread": _f(tz.get("di_spread")),
        "adx14": _f(tz.get("adx")),
        "adx_delta": _f(tz.get("adx_delta")),
        "momentum_ord": _MOM_ORD.get(_momentum(row, mg), 0.0),
        "extension_atr": _f(tt.get("extension_atr")),
        "is_long": 1.0 if side in ("long", "buy") else 0.0,
    }


def row_to_features(row: dict) -> list[float]:
    """Контракт с ml_meta_labeler: list[float] в порядке FEATURE_NAMES."""
    fd = features_dict(row)
    return [float(fd[k]) for k in FEATURE_NAMES]


def logged_features(row: dict) -> list[float] | None:
    """Вектор, сохранённый live-скором на входе (plan.ml.features). None если нет/старый.
    Используется ml_meta_labeler для обучения на ТОМ ЖЕ векторе, что видел скор."""
    if not bool(getattr(settings, "ML_TRAIN_ON_LOGGED_FEATURES", True)):
        return None
    plan = row.get("plan") or row.get("plan_json") or {}
    ml = (plan.get("ml") if isinstance(plan, dict) else None) or row.get("ml") or {}
    if not isinstance(ml, dict):
        return None
    if int(ml.get("feature_version") or 0) != FEATURE_VERSION:
        return None
    vec = ml.get("features")
    if isinstance(vec, dict):
        return [float(vec.get(k, 0.0)) for k in FEATURE_NAMES]
    if isinstance(vec, list) and len(vec) == len(FEATURE_NAMES):
        return [float(x) for x in vec]
    return None


def feature_psi(baseline: dict, feat: dict | list) -> float:
    """PSI live-вектора против train-baseline (q10/q90). >0.25 — дрейф."""
    q10 = baseline.get("q10") or []
    q90 = baseline.get("q90") or []
    vec = feat if isinstance(feat, list) else [float(feat.get(k, 0.0)) for k in FEATURE_NAMES]
    if len(q10) != len(vec) or len(q90) != len(vec):
        return 0.0
    exp = [0.10, 0.80, 0.10]
    obs = [0, 0, 0]
    for v, lo, hi in zip(vec, q10, q90):
        obs[0 if v < lo else (2 if v > hi else 1)] += 1
    n = max(len(vec), 1)
    psi = 0.0
    for o, e in zip(obs, exp):
        p = max(o / n, 1e-4)
        psi += (p - e) * math.log(p / e)
    return psi


# ─────────────────────────────────────────────────────────────────────────────
# Метки и P&L — контракт сохранён (импортируют ml_meta_labeler / ml_shadow_report).
# ─────────────────────────────────────────────────────────────────────────────
def is_phantom_row(row: dict) -> bool:
    """Выход лучше, чем цена когда-либо была (фантомный филл).

    (#phantom-units-2026-10-16) С сигнала #621 (20.09.2026) `result_pct` пишется в
    процентах от маржи (≈ ×10 плечо), а `mfe_pct` остался в процентах цены. Старое
    сравнение result_pct > mfe_pct с этого момента помечало фантомами ВСЕ победы
    (~250 строк, 45–54% свежих сделок), и после фильтра в обучении почти не
    оставалось позитивов. Поэтому сравниваем в единицах цены: выход против
    max_profit_price. Старое сравнение — только если цен нет.
    """
    lc = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
    try:
        side = str(row.get("side") or "").lower()
        x = lc.get("exit_price")
        x = row.get("closed_exit_price") if x is None else x
        m = lc.get("max_profit_price")
        if side in ("long", "short") and x is not None and m is not None:
            x, m = float(x), float(m)
            if x > 0 and m > 0:
                return x > m * (1 + 1e-6) if side == "long" else x < m * (1 - 1e-6)
    except (TypeError, ValueError):
        pass
    try:
        return float(row["result_pct"]) > float(lc["mfe_pct"]) + 1e-9
    except (KeyError, TypeError, ValueError):
        return False


def tp1_partial_net_pnl(row: dict) -> float:
    """Чистый P&L первой ноги (частичное закрытие на TP1) — ТОЛЬКО для информации.

    (#two-legs-revert-2026-10-17) `closed_net_pnl` УЖЕ содержит все ноги сделки:
    с #tp1-partial-2026-07-09 `_close_signal` прибавляет `plan.tp1_partial` и
    `tp2_partial` к net и издержкам перед записью. Сверка на 68 сделках с TP1:
    gross(обеих ног) − closed_total_cost == closed_net_pnl до цента, а остаток
    без ноги ему НЕ равен. Прибавлять эту величину к closed_net_pnl нельзя —
    это двойной счёт (прежняя правка #two-legs-2026-10-10 была ошибочной).
    """
    v = row.get("tp1_partial_net_pnl")
    if v is None:
        plan = row.get("plan")
        part = plan.get("tp1_partial") if isinstance(plan, dict) else None
        v = part.get("net_pnl") if isinstance(part, dict) else None
    out = _f(v, 0.0)
    return out if math.isfinite(out) else 0.0


def total_net_pnl(row: dict) -> float | None:
    """Полный чистый результат сделки = `closed_net_pnl` (он уже включает все ноги).
    Плоские поля `total_net_pnl` / `tp1_partial_net_pnl` из jsonl игнорируются:
    строки, дописанные прежним backfill'ом, содержат двойной счёт.
    """
    pnl = row.get("closed_net_pnl")
    if pnl is None:
        return None
    try:
        pnl = float(pnl)
    except (TypeError, ValueError):
        return None
    return pnl if math.isfinite(pnl) else None


def row_to_label(row: dict, label_kind: str = "beats_costs", min_r: float = 0.3) -> int | None:
    labels = row.get("labels") if isinstance(row.get("labels"), dict) else {}
    if label_kind == "hit_tp2":
        if "hit_tp2" in labels:
            return 1 if labels.get("hit_tp2") else 0
        return 1 if reached_tp2(row.get("closed_reason"), row.get("plan") or {}) else 0

    # Метка считается по ПОЛНОМУ результату (обе ноги уже в closed_net_pnl).
    pnl = total_net_pnl(row)
    if pnl is None:
        return None
    if label_kind == "is_win":
        return 1 if pnl > 0 else 0

    risk = abs(_f(row.get("net_pnl_stop")))
    if risk <= 1e-9:
        return None

    # (#ml-audit) Экономическая метка: позитив = чистый ход >= ML_LABEL_NET_R_MIN
    # риска. В отличие от beats_costs (частота мелких плюсов) ранжирует по величине,
    # что согласовано с тем, что платит хвост (tp2_trail), а не модальный мелкий плюс.
    if label_kind == "net_r":
        thr = float(getattr(settings, "ML_LABEL_NET_R_MIN", 1.0))
        return 1 if (pnl / risk) >= thr else 0

    # beats_costs (legacy default)
    return 1 if (pnl / risk) >= float(min_r) else 0
