# -*- coding: utf-8 -*-
"""Разрешает конфликт в apps/api/services/ml_meta_labeler.py (одна пара маркеров):
берём сторону HEAD (сплит 80/20 с ограничением cut). Для n>=150 она численно
совпадает со стороной feat (int(n*0.80)). Запуск из корня репозитория."""
import ast, re, shutil, sys
p = "apps/api/services/ml_meta_labeler.py"
raw = open(p, "rb").read()
crlf = b"\r\n" in raw
s = raw.decode("utf-8").replace("\r\n", "\n")
pat = re.compile(r"<<<<<<< HEAD\n(.*?)=======\n(.*?)>>>>>>> [^\n]*\n", re.S)
m = pat.findall(s)
if len(m) != 1:
    sys.exit(f"ожидался 1 конфликт, найдено {len(m)} - файл не тронут")
shutil.copyfile(p, p + ".bak-before-conflict")
s = pat.sub(lambda mm: mm.group(1), s, count=1)
ast.parse(s)
open(p, "wb").write((s.replace("\n", "\r\n") if crlf else s).encode("utf-8"))
print("ok: ml_meta_labeler.py разрешён, синтаксис корректен")
