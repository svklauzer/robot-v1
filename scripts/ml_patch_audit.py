# scripts/ml_patch_audit.py
import pathlib, re
R = pathlib.Path(".")
CHECKS = [
 ("apps/api/services/ml_features.py",        r"FEATURE_VERSION:\s*int\s*=\s*6", "v6"),
 ("apps/api/services/ml_features.py",        r"def logged_features",            "logged_features"),
 ("apps/api/services/ml_features.py",        r"def features_dict",              "features_dict"),
 ("apps/api/services/ml_features.py",        r"ML_ENTRY_PRICE_BASIS",           "единая база цены"),
 ("apps/api/services/ml_meta_labeler.py",    r"logged_features\(r\)",           "обучение на logged ★"),
 ("apps/api/services/ml_meta_labeler.py",    r"ML_LABEL_NET_R_MIN",             "честный label_min_r"),
 ("apps/api/services/ml_meta_labeler.py",    r"min_samples_leaf=8",             "регуляризация"),
 ("apps/api/services/ml_controller.py",      r'"features"',                     "лог вектора ★"),
 ("apps/api/services/ml_controller.py",      r"ML_PROMOTION_MIN_LIVE_AUC",      "live-гейт"),
 ("apps/api/services/ml_controller.py",      r"train_events_per_feature",       "гейт по train_epv"),
 ("apps/api/services/ml_intelligence_hub.py",r'"outcome_stats":\s*0\.70',       "ПОДМЕНА грейдом ★ (должно отсутствовать)"),
 ("apps/api/services/ml_intelligence_hub.py",r"train_events_per_feature",       "связывающие метрики"),
 ("apps/api/services/ml_intelligence_hub.py",r'"implemented":\s*True',          "DriftMonitor"),
 ("apps/api/services/ml_shadow_report.py",   r"feature_version",                "сегментация версий"),
 ("apps/api/services/tz_entry_shadow.py",    r"di_weak",                        "DI-пол ★"),
 ("apps/api/services/trend_trigger.py",      r"TREND_MIN_EXTENSION_ENABLED",    "нижняя граница ★"),
 ("apps/api/core/config.py",                 r"TZ_DI_SPREAD_MIN",               "ключ DI"),
 ("apps/api/core/config.py",                 r"TREND_MIN_EXTENSION_ENABLED",    "ключ min-ext"),
 ("apps/api/core/config.py",                 r"MOMENTUM_GATE_BLOCK_OPPOSED",    "ключ opposed"),
 ("apps/api/core/config.py",                 r"ML_SIZE_ALLOC_ENABLED:\s*bool\s*=\s*False", "тень не режет размер"),
]
INVERT = {"ПОДМЕНА грейдом ★ (должно отсутствовать)"}
for path, pat, name in CHECKS:
    p = R / path
    if not p.exists():
        print(f"?? {name:38} {path} — файл не найден"); continue
    hit = bool(re.search(pat, p.read_text(encoding="utf-8", errors="replace")))
    ok = (not hit) if name in INVERT else hit
    print(f"{'OK ' if ok else 'НЕТ'} {name:38} {path}")

rnd = (R/"render.yaml").read_text(encoding="utf-8")
for k in ("ANTI_DRAIN_MAX_DAILY_LOSS_PCT","MAX_DAILY_LOSS_PCT","TZ_ADX_MIN",
          "RANGE_MIN_WIDTH_PCT","RANGE_MIN_TP1_NET_PCT","TZ_DI_SPREAD_MIN"):
    m = re.search(rf"- key: {k}\s*\n\s*value: \"?([^\"\n]+)\"?", rnd)
    print(f"    render {k:34} = {m.group(1) if m else '(нет ключа)'}")
