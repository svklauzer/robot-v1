# -*- coding: utf-8 -*-
"""Разрешение конфликта apps/api/core/config.py. Запуск из корня репозитория, пока слияние не завершено.

Правило (по config_report.txt):
  * основа - стадия 3 (feat): код использует 48 настроек, которых в main нет;
  * значения 30 общих полей берутся из стадии 2 (main) - слияние не должно молча менять
    боевые параметры main; исключение HTX_API_HOSTNAME_FALLBACKS: у main значение сломано
    ("://htx.com"), берётся значение feat;
  * 6 полей, которые есть только в main, дописываются в конец Settings.
Файл не коммитится, есть бэкап .bak-before-conflict."""
import ast, shutil, subprocess, sys

PATH = "apps/api/core/config.py"
KEEP_THEIRS = {"HTX_API_HOSTNAME_FALLBACKS"}

def stage(n):
    return subprocess.run(["git", "show", f":{n}:{PATH}"], capture_output=True, check=True).stdout

def settings_cls(tree):
    return next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "Settings")

def annassigns(tree):
    return [s for s in settings_cls(tree).body if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)]

def merge(ours_b: bytes, theirs_b: bytes) -> bytes:
    crlf = b"\r\n" in theirs_b
    o = ours_b.decode("utf-8").replace("\r\n", "\n"); t = theirs_b.decode("utf-8").replace("\r\n", "\n")
    ot, tt = ast.parse(o), ast.parse(t)
    ours = {}
    for s in annassigns(ot):
        ours[s.target.id] = s
    tnames = {s.target.id for s in annassigns(tt)}
    tl = t.split("\n"); ol = o.split("\n")
    def seg(lines, node):  # текст значения по позициям (col_offset - в байтах UTF-8)
        v = node.value
        if v.lineno != v.end_lineno: return None
        b = lines[v.lineno - 1].encode("utf-8")
        return b[v.col_offset:v.end_col_offset].decode("utf-8")
    edits = []
    for s in annassigns(tt):
        k = s.target.id
        if k in ours and k not in KEEP_THEIRS and s.value is not None and ours[k].value is not None:
            a, b = seg(tl, s), seg(ol, ours[k])
            if a is None or b is None:
                sys.exit(f"многострочное значение у {k} - разрулить вручную")
            if a != b: edits.append((s.value.lineno - 1, s.value.col_offset, s.value.end_col_offset, b))
    for ln, c0, c1, new in sorted(edits, reverse=True):
        bl = tl[ln].encode("utf-8"); tl[ln] = (bl[:c0] + new.encode("utf-8") + bl[c1:]).decode("utf-8")
    only = [s for k, s in ours.items() if k not in tnames]
    last = max(s.end_lineno for s in annassigns(tt))
    add = ["", "    # поля, которые есть только в main (перенесены при слиянии)"]
    for s in only:
        add += ol[s.lineno - 1: s.end_lineno]
    tl[last:last] = add
    out = "\n".join(tl)
    ast.parse(out)
    return (out.replace("\n", "\r\n") if crlf else out).encode("utf-8"), len(edits), [s.target.id for s in only]

if __name__ == "__main__":
    res, n_edit, only = merge(stage(2), stage(3))
    shutil.copyfile(PATH, PATH + ".bak-before-conflict")
    open(PATH, "wb").write(res)
    print(f"ok: значений main восстановлено {n_edit}, полей только из main добавлено {len(only)}: {only}")
