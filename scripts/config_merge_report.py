# -*- coding: utf-8 -*-
"""Отчёт по конфликту core/config.py: сравнивает поля Settings в "ours" (main) и "theirs"
(feat) из стадий индекса и проверяет, что каждое settings.X из кода есть в итоговом конфиге.
Ничего не меняет. Запуск из корня репозитория, пока слияние не завершено."""
import ast, re, subprocess, pathlib

def stage(n):
    return subprocess.run(["git", "show", f":{n}:apps/api/core/config.py"],
                          capture_output=True, check=True).stdout.decode("utf-8")

def fields(src):
    out = {}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            for st in node.body:
                if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                    out[st.target.id] = ast.get_source_segment(src, st.value) if st.value else None
    return out

ours, theirs = fields(stage(2)), fields(stage(3))
only_o = sorted(set(ours) - set(theirs)); only_t = sorted(set(theirs) - set(ours))
diff = sorted(k for k in set(ours) & set(theirs) if ours[k] != theirs[k])
print(f"ours={len(ours)} полей, theirs={len(theirs)}; только в ours={len(only_o)}, "
      f"только в theirs={len(only_t)}, разные значения={len(diff)}\n")
print("== РАЗНЫЕ ЗНАЧЕНИЯ (ours | theirs) ==")
for k in diff: print(f"{k}: {ours[k]} | {theirs[k]}")
print("\n== ТОЛЬКО В theirs (feat) ==");  [print(k, "=", theirs[k]) for k in only_t]
print("\n== ТОЛЬКО В ours (main) ==");    [print(k, "=", ours[k]) for k in only_o]

used = set()
for f in pathlib.Path("apps/api").rglob("*.py"):
    if ".bak" in f.name: continue
    used |= set(re.findall(r"settings\.([A-Z][A-Z0-9_]+)", f.read_text(encoding="utf-8", errors="ignore")))
print("\n== используются в коде, но НЕТ в ours (сломается, если взять main) ==")
print(sorted(used - set(ours)) or "нет")
print("== используются в коде, но НЕТ в theirs ==")
print(sorted(used - set(theirs)) or "нет")
