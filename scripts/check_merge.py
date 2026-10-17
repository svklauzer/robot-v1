# -*- coding: utf-8 -*-
"""Проверка после слияния: нет маркеров конфликта, config импортируется, все settings.X из кода существуют."""
import pathlib, re, sys
sys.path.insert(0, "apps/api")
bad = [str(f) for f in pathlib.Path("apps").rglob("*.py") if re.search(r"^(<<<<<<<|>>>>>>>) ", f.read_text(encoding="utf-8", errors="ignore"), re.M)]
print("файлы с маркерами конфликта:", bad or "нет")
from core.config import settings
for k in ("ML_MODE", "MAX_LEVERAGE", "TP1_PARTIAL_ENABLED", "ML_TRAIN_WINDOW_DAYS", "HTX_API_HOSTNAME_FALLBACKS"):
    print(k, "=", getattr(settings, k))
used = set()
for f in pathlib.Path("apps/api").rglob("*.py"):
    used |= set(re.findall(r"settings\.([A-Z][A-Z0-9_]+)", f.read_text(encoding="utf-8", errors="ignore")))
missing = sorted(k for k in used if not hasattr(settings, k))
print("используются, но отсутствуют в Settings:", missing or "нет")
