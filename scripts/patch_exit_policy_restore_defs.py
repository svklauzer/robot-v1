# -*- coding: utf-8 -*-
"""Возвращает определения, удалённые коммитом d19e89a (22.09.2026) из функции решения до TP1
в apps/api/services/exit_policy.py (NameError в manage_loop: tp1_dist_pct, thr, age_ok, ...).

Вставляет ПЕРЕД строкой `be_enabled = bool(getattr(settings, "BREAKEVEN_LOCK_ENABLED", True))`
ровно тот блок, который коммит удалил (текст взят из его diff, drawdown_from_mfe он перенёс выше сам).
Защиты (при любом сбое файл не тронут): якорь ровно один; он внутри функции класса; в функции есть
аргументы stop_price, tp1_price, entry_price, signal_age_sec и присвоено fee_rate; в классе есть методы
_get_thresholds и _net_safe_floor_pct; ни одно из восстанавливаемых имён в функции не присваивается;
результат компилируется. После правки запускается pyflakes (если установлен) и печатает остаток
undefined name. Запуск из корня репозитория. Бэкап: exit_policy.py.bak-before-restore-defs."""
import ast, shutil, subprocess, sys

P = "apps/api/services/exit_policy.py"
ANCHOR = 'be_enabled = bool(getattr(settings, "BREAKEVEN_LOCK_ENABLED", True))'
RESTORE = ["stop_distance_pct", "thr", "threshold_source", "min_protective_exit_pct", "min_protective_net_usdt",
           "protective_drawdown_share", "adaptive_drawdown_pct", "min_age_sec", "age_ok", "tp1_dist_pct"]

raw = open(P, "rb").read(); crlf = b"\r\n" in raw
s = raw.decode("utf-8").replace("\r\n", "\n"); lines = s.split("\n")
hits = [i for i, l in enumerate(lines) if l.strip() == ANCHOR]
if not hits:
    sys.exit("якорь не найден - файл не тронут")
tree = ast.parse(s)

def owner(idx):
    """(класс, метод), внутри которого стоит строка idx."""
    res = (None, None)
    for c in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        for f in c.body:
            if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.lineno <= idx + 1 <= f.end_lineno:
                res = (c, f)
    return res

# Якорей несколько (блок безубытка есть в нескольких методах): берём тот, чей метод ИСПОЛЬЗУЕТ
# tp1_dist_pct без определения - именно в нём падает NameError.
def uses_tp1(f):
    return any(isinstance(n, ast.Name) and n.id == "tp1_dist_pct" and isinstance(n.ctx, ast.Load)
               for n in ast.walk(f))
cands = [h for h in hits if owner(h)[1] is not None and uses_tp1(owner(h)[1])]
if not cands:
    sys.exit(f"якорей {len(hits)}, но ни один не стоит в методе, использующем tp1_dist_pct - файл не тронут")
funcs = {owner(h)[1].lineno for h in cands}
if len(funcs) != 1:
    sys.exit(f"якоря в разных методах (строки {[h + 1 for h in cands]}) - файл не тронут, пришлите файл")
# Несколько якорей в ОДНОМ методе (блок безубытка встречается в нём дважды): берём первый, он выше
# всех использований; ниже проверяем это явно.
i = min(cands); indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
cls, func = owner(i)
first_use = min(n.lineno for n in ast.walk(func)
                if isinstance(n, ast.Name) and n.id in RESTORE and isinstance(n.ctx, ast.Load))
if first_use <= i + 1:
    sys.exit(f"первое использование восстанавливаемых имён (строка {first_use}) не ниже якоря "
             f"(строка {i + 1}) - файл не тронут, пришлите файл")

def targets(node):
    if isinstance(node, ast.Name): yield node.id
    elif isinstance(node, (ast.Tuple, ast.List)):
        for e in node.elts: yield from targets(e)

args = {a.arg for a in func.args.args + func.args.kwonlyargs}
assigned_all, assigned_before = set(), set()
for st in ast.walk(func):
    if isinstance(st, ast.Assign):
        for t in st.targets:
            for nm in targets(t):
                assigned_all.add(nm)
                if st.lineno <= i: assigned_before.add(nm)
problems = []
for need in ("stop_price", "tp1_price", "entry_price", "signal_age_sec"):
    if need not in args: problems.append(f"нет аргумента {need}")
if "fee_rate" not in assigned_before and "fee_rate" not in args: problems.append("fee_rate не определён выше якоря")
methods = {f.name for f in cls.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
for m in ("_get_thresholds", "_net_safe_floor_pct"):
    if m not in methods: problems.append(f"в классе {cls.name} нет метода {m}")
already = [n for n in RESTORE if n in assigned_all or n in args]
if already: problems.append(f"уже определены в функции: {already}")
if problems:
    sys.exit(f"функция {func.name}: " + "; ".join(problems) + " - файл не тронут, пришлите функцию")

block = f'''# (#restore-defs-2026-10-19) Определения ниже удалил коммит d19e89a (22.09.2026) при переписывании
# блока безубытка, а использования в теле функции остались -> NameError в manage_loop.
stop_distance_pct = (
    abs(entry_price - float(stop_price)) / entry_price * 100
    if stop_price is not None and float(stop_price) > 0
    else None
)
thr, threshold_source = self._get_thresholds(stop_distance_pct, self._net_safe_floor_pct(fee_rate))
min_protective_exit_pct = float(getattr(settings, "MIN_PROTECTIVE_EXIT_PCT", 1.20))
min_protective_net_usdt = float(getattr(settings, "MIN_PROTECTIVE_NET_USDT", 1.50))
protective_drawdown_share = float(settings.PROTECTIVE_DRAWDOWN_SHARE)
adaptive_drawdown_pct = float(settings.ADAPTIVE_TRAIL_DRAWDOWN_PCT)
min_age_sec = float(getattr(settings, "FAILED_SETUP_MIN_AGE_SEC", 300))
age_ok = signal_age_sec is not None and float(signal_age_sec) >= min_age_sec
tp1_dist_pct = (
    abs(float(tp1_price) - entry_price) / entry_price * 100
    if tp1_price is not None and float(tp1_price) > 0
    else None
)
'''.rstrip("\n").split("\n")
lines[i:i] = [(indent + l) if l else l for l in block]
out = "\n".join(lines)
compile(out, P, "exec")
shutil.copyfile(P, P + ".bak-before-restore-defs")
open(P, "wb").write((out.replace("\n", "\r\n") if crlf else out).encode("utf-8"))
print(f"ok: блок из {len(block)} строк добавлен в {cls.name}.{func.name} (строка {i + 1})")
try:
    r = subprocess.run([sys.executable, "-m", "pyflakes", P], capture_output=True, text=True)
    if "No module named" in r.stderr:
        print("pyflakes не установлен (pip install pyflakes) - проверка не выполнена")
    else:
        left = [l for l in r.stdout.splitlines() if "undefined name" in l]
        print("pyflakes: undefined name осталось:", len(left)); [print(" ", l) for l in left[:20]]
except Exception as e:
    print("pyflakes не запущен:", e)
