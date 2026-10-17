"""Снимает конфликт слияния в apps/api/workers/robot_loop.py (кандидат для ML).

Сторона HEAD (main) передаёт stop_price/required_margin/entry_price из локальных
переменных step() — они определены выше (stop, entry_price), это новее. Ветка
feat/regime-expectancy-report добавляет rationale и trade_mode для is_scalp/is_crt.
Результат: значения HEAD + rationale/trade_mode. Старые plan.* значения ветки
(plan.stop_price и т.п.) отбрасываются.

Запуск:  python scripts/resolve_robot_loop_merge.py [путь к robot_loop.py]
Бэкап .bak-before-conflict; без маркеров конфликта ничего не делает.
"""
import ast
import shutil
import sys
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else "apps/api/workers/robot_loop.py")
raw = path.read_bytes()
crlf = b"\r\n" in raw
src = raw.decode("utf-8").replace("\r\n", "\n").split("\n")

starts = [i for i, l in enumerate(src) if l.startswith("<<<<<<< ")]
mids = [i for i, l in enumerate(src) if l == "======="]
ends = [i for i, l in enumerate(src) if l.startswith(">>>>>>> ")]
if not starts:
    sys.exit("маркеров конфликта нет — ничего не делаю")
if not (len(starts) == len(mids) == len(ends) == 1):
    sys.exit(f"ожидался ровно один конфликт, найдено {len(starts)}/{len(mids)}/{len(ends)} — разберите вручную")
s, m, e = starts[0], mids[0], ends[0]
if not (s < m < e):
    sys.exit("маркеры в неожиданном порядке")
head = "\n".join(src[s + 1:m])
theirs = "\n".join(src[m + 1:e])
for need in ('"stop_price": float(stop)', '"lifecycle": {"entry_price": float(entry_price)}'):
    if need not in head:
        sys.exit(f"в блоке HEAD нет ожидаемого {need!r} — разберите вручную")
for need in ('"rationale": f"intelligence_{result.reason}"', '"trade_mode":'):
    if need not in theirs:
        sys.exit(f"в блоке ветки нет ожидаемого {need!r} — разберите вручную")

keep = [l for l in src[s + 1:m] if l.strip()]            # HEAD без пустых строк
theirs_lines = src[m + 1:e]
i0 = next(i for i, l in enumerate(theirs_lines) if "#ml-serve-parity" in l)
i1 = next(i for i, l in enumerate(theirs_lines) if l.lstrip().startswith('"trade_mode"'))
if i1 < i0:
    sys.exit("порядок строк в блоке ветки неожиданный — разберите вручную")
add = theirs_lines[i0:i1 + 1]
merged = keep + add
out = src[:s] + merged + src[e + 1:]
text = "\n".join(out)
try:
    ast.parse(text)
except SyntaxError as exc:
    sys.exit(f"после слияния файл не компилируется ({exc}) — файл не тронут")
shutil.copy2(path, str(path) + ".bak-before-conflict")
path.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))
print("OK: конфликт снят,", path)
