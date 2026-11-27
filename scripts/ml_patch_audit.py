#!/usr/bin/env python3
"""Аудит патчей ML-контура и движков. Запускать можно из любой папки.

    python scripts/ml_patch_audit.py
    python ml_patch_audit.py                 # из scripts/ — тоже работает
    python ml_patch_audit.py C:/path/to/repo # явный корень
"""
from __future__ import annotations

import pathlib
import re
import sys


def find_root() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve()
    for cand in (here.parent.parent, *here.parents):
        if (cand / "apps" / "api").is_dir():
            return cand
    cwd = pathlib.Path.cwd().resolve()
    for cand in (cwd, *cwd.parents):
        if (cand / "apps" / "api").is_dir():
            return cand
    return here.parent.parent


ROOT = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else find_root()
print(f"ROOT = {ROOT}")
if not (ROOT / "apps" / "api").is_dir():
    print("!! Не нашёл apps/api — передай корень репо первым аргументом.")
    sys.exit(2)

S = "apps/api/services/"
C = "apps/api/core/"

# (файл, регулярка, метка, инверсия: найдено = ПЛОХО)
CHECKS = [
    (S + "ml_features.py",         r"FEATURE_VERSION:\s*int\s*=\s*6",        "v6 фичи", False),
    (S + "ml_features.py",         r"def logged_features",                   "logged_features", False),
    (S + "ml_features.py",         r"def features_dict",                     "features_dict", False),
    (S + "ml_features.py",         r"ML_ENTRY_PRICE_BASIS",                  "единая база цены", False),
    (S + "ml_features.py",         r'==\s*"net_r"',                          "ветка net_r", False),
    (S + "ml_features.py",         r'"confidence"',                          "confidence в фичах (убрать)", True),

    (S + "ml_meta_labeler.py",     r"logged_features\(r\)",                  "обучение на logged ★", False),
    (S + "ml_meta_labeler.py",     r"_live_validation",                      "live-валидация", False),
    (S + "ml_meta_labeler.py",     r"drift_baseline",                        "baseline дрейфа", False),
    (S + "ml_meta_labeler.py",     r"ML_POSITIVE_SHIFT_Z_WARN",              "порог сдвига 2.0", False),
    (S + "ml_meta_labeler.py",     r"ML_LABEL_NET_R_MIN",                    "честный label_min_r", False),
    (S + "ml_meta_labeler.py",     r"min_samples_leaf=8",                    "регуляризация леса", False),

    (S + "ml_controller.py",       r'"features"',                            "лог вектора ★", False),
    (S + "ml_controller.py",       r"ML_PROMOTION_MIN_LIVE_AUC",             "live-гейт полномочий", False),
    (S + "ml_controller.py",       r"train_events_per_feature",              "гейт по train_epv", False),

    (S + "ml_intelligence_hub.py", r'outcome_stats"?\]?\s*[:=]\s*0\.7',      "ПОДМЕНА грейдом ★", True),
    (S + "ml_intelligence_hub.py", r"train_events_per_feature",              "связывающие метрики", False),
    (S + "ml_intelligence_hub.py", r'"implemented":\s*True',                 "DriftMonitor реализован", False),
    (S + "ml_intelligence_hub.py", r'"features"',                            "features в MLDecision", False),

    (S + "ml_shadow_report.py",    r"feature_version",                       "сегментация версий ★", False),

    (S + "tz_entry_shadow.py",     r'"di_weak"',                             "DI-пол: семья ★", False),
    (S + "tz_entry_shadow.py",     r"TZ_DI_SPREAD_MIN",                      "DI-пол: порог ★", False),
    (S + "trend_trigger.py",       r"TREND_MIN_EXTENSION_ENABLED",           "нижняя граница ext ★", False),
    (S + "momentum_gate.py",       r"_OPPOSED",                              "opposed-гейт ★", False),
    (S + "exit_policy.py",         r"tz_ride",                               "лестница внутри TZ: ride ★", False),
    (S + "exit_policy.py",         r"tz_band",                               "лестница внутри TZ: band ★", False),

    (C + "config.py",              r"TZ_DI_SPREAD_MIN",                      "ключ DI", False),
    (C + "config.py",              r"TREND_MIN_EXTENSION_ENABLED",           "ключ min-ext", False),
    (C + "config.py",              r"MOMENTUM_GATE_BLOCK_OPPOSED",           "ключ opposed", False),
    (C + "config.py",              r"ML_LABEL_NET_R_MIN",                    "ключ net_r", False),
    (C + "config.py",              r"ML_ENTRY_PRICE_BASIS",                  "ключ база цены", False),
    (C + "config.py",              r"ML_PROMOTION_MIN_LIVE_AUC",             "ключ live-гейт", False),
    (C + "config.py",              r"ML_SIZE_ALLOC_ENABLED:\s*bool\s*=\s*False", "тень не режет размер", False),
]

print("\n=== ПАТЧИ ===")
ok = bad = missing = 0
for path, pat, name, invert in CHECKS:
    p = ROOT / path
    if not p.exists():
        print(f"??  {name:34} {path} — файл не найден")
        missing += 1
        continue
    src = p.read_text(encoding="utf-8", errors="replace")
    hit = bool(re.search(pat, src))
    good = (not hit) if invert else hit
    print(f"{'OK ' if good else 'НЕТ'} {name:34} {path}")
    ok += good
    bad += (not good)

print("\n=== СИНТАКСИС (после IndentationError — обязательно) ===")
SYNTAX = [S + f for f in (
    "ml_features.py", "ml_meta_labeler.py", "ml_controller.py",
    "ml_intelligence_hub.py", "ml_shadow_report.py",
    "tz_entry_shadow.py", "trend_trigger.py", "momentum_gate.py",
    "exit_policy.py",
)] + [C + "config.py"]
for path in SYNTAX:
    p = ROOT / path
    if not p.exists():
        print(f"??  {path}")
        continue
    src = p.read_text(encoding="utf-8", errors="replace")
    try:
        compile(src, path, "exec")
        tabs = [i + 1 for i, l in enumerate(src.splitlines()) if "\t" in l]
        print(f"OK  {path}" + (f"  !! табы в строках {tabs[:5]}" if tabs else ""))
    except (IndentationError, SyntaxError) as e:
        bad += 1
        print(f"FAIL {path} -> {type(e).__name__}: {e.msg} (строка {e.lineno})")
        lines = src.splitlines()
        n = e.lineno or 1
        for i in range(max(0, n - 4), min(len(lines), n + 2)):
            raw = lines[i]
            ws = raw[:len(raw) - len(raw.lstrip())]
            print(f"     {i+1:4d} ws={ws!r} | {raw.strip()[:78]}")

print("\n=== RENDER / CONFIG SYNC ===")
rnd_p, cfg_p = ROOT / "render.yaml", ROOT / C / "config.py"
rnd = rnd_p.read_text(encoding="utf-8", errors="replace") if rnd_p.exists() else ""
cfg = cfg_p.read_text(encoding="utf-8", errors="replace") if cfg_p.exists() else ""
if not rnd:
    print("??  render.yaml не найден")
KEYS = [
    "TZ_DI_SPREAD_MIN", "TZ_ADX_MIN", "TZ_ENFORCE_CONDITIONS",
    "TREND_MIN_EXTENSION_ENABLED", "TREND_MIN_EXTENSION_ATR", "TREND_MAX_EXTENSION_ATR",
    "MOMENTUM_GATE_MODE", "MOMENTUM_GATE_BLOCK_OPPOSED", "MOMENTUM_GATE_BLOCK_ALIGNED",
    "ANTI_CHOP_YOUNG_ADX_MIN", "ANTI_CHOP_YOUNG_DI_SPREAD_MIN",
    "CRT_LTF_CONFIRM", "CRT_REQUIRE_CISD", "CRT_MIN_SETUP_SCORE",
    "SCALP_TARGET_PCT", "SCALP_STOP_BUFFER_ATR", "SCALP_TIME_STOP_ENABLED",
    "RANGE_STOP_ATR_MULT", "RANGE_MIN_WIDTH_PCT", "RANGE_MIN_TP1_NET_PCT",
    "MAX_DAILY_LOSS_PCT", "ANTI_DRAIN_MAX_DAILY_LOSS_PCT", "MAX_DRAWDOWN_PCT",
    "ANTI_DRAIN_MAX_OPEN_POSITIONS", "CORR_CLUSTER_PORTFOLIO_MAX_SAME_DIR",
    "SYMBOL_PERF_LOOKBACK", "TZ_HARD_STOP_LOSS_PCT",
    "ML_MODE", "ML_LABEL_KIND", "ML_LABEL_NET_R_MIN", "ML_SIZE_ALLOC_ENABLED",
    "ML_TRAIN_WINDOW_DAYS", "ML_ENTRY_PRICE_BASIS", "ML_DRIFT_PSI_MAX",
    "ML_PROMOTION_MIN_LIVE_AUC", "REGIME_EXP_SIZING_ENABLED",
    "POST_TP1_LOCK_FRAC", "POST_TP1_TRAIL_ENABLED",
]
for k in KEYS:
    mr = re.search(rf"- key: {k}\s*\n\s*value:\s*\"?([^\"\n#]+)\"?", rnd)
    mc = re.search(rf"^\s*{k}:\s*\w+\s*=\s*([^\n#]+)", cfg, re.M)
    rv = mr.group(1).strip() if mr else "—"
    cv = mc.group(1).strip() if mc else "—"
    flag = ""
    if rv != "—" and cv != "—":
        norm = lambda v: v.strip().strip('"').strip("'").lower().rstrip("0").rstrip(".")
        if norm(rv) != norm(cv):
            flag = "   <-- РАСХОЖДЕНИЕ"
    print(f"  {k:36} render={rv:28} config={cv:20}{flag}")

print(f"\nИТОГ: OK={ok}  НЕТ={bad}  файлов-нет={missing}")
sys.exit(1 if bad else 0)