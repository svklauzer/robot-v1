# -*- coding: utf-8 -*-
"""Относит 3 поля из main к группам Конфигурации (ratchet-тест). Из корня репозитория. Идемпотентен."""
import ast, shutil, sys
P = "apps/api/services/config_inspector.py"
raw = open(P, "rb").read(); crlf = b"\r\n" in raw
s = raw.decode("utf-8").replace("\r\n", "\n")
reps = [
 ('"PROTECTIVE_", "FAILED_SETUP_")),', '"PROTECTIVE_", "FAILED_SETUP_", "MIN_POST_TP1_")),'),
 ('("ENABLE_", "RANGE_", "SCALP_", "CRT_", "REVERSAL_")),',
  '("ENABLE_", "RANGE_", "SCALP_", "CRT_", "REVERSAL_",\n                       "REGIME_CHOP_", "REGIME_TREND_")),'),
]
changed = 0
for a, b in reps:
    if b in s: continue
    if s.count(a) != 1: sys.exit(f"не найден ровно один фрагмент: {a!r}")
    s = s.replace(a, b); changed += 1
ast.parse(s)
if changed:
    shutil.copyfile(P, P + ".bak-before-groups")
    open(P, "wb").write((s.replace("\n", "\r\n") if crlf else s).encode("utf-8"))
print(f"ok, изменений: {changed}")
