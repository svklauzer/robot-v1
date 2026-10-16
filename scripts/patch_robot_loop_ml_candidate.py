"""Правит workers/robot_loop.py: в ML-кандидат добавляет rationale и trade_mode.

Без них row_to_features() на живом предсказании всегда даёт is_scalp=0 и is_crt=0,
хотя модель обучена на строках, где эти признаки считаются из rationale/trade_mode
(train/serve skew, тот же класс, что #audit-2026-08-27 со stop_price).

Запуск:  python scripts/patch_robot_loop_ml_candidate.py [путь к robot_loop.py]
Бэкап .bak-before-ml-candidate; повторный запуск ничего не меняет.
"""
import shutil
import sys
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else "apps/api/workers/robot_loop.py")
raw = path.read_bytes()
crlf = b"\r\n" in raw
src = raw.decode("utf-8").replace("\r\n", "\n")
if '"rationale": f"intelligence_{result.reason}",' in src and "#ml-serve-parity-2026-10-17" in src:
    print("уже применено")
    sys.exit(0)
anchor = '                    "entry_depth": _ml_depth,\n'
if src.count(anchor) != 1:
    sys.exit("ЯКОРЬ не найден или не единственный — файл не изменён")
add = (
    "                    # (#ml-serve-parity-2026-10-17) Признаки is_scalp/is_crt модель берёт из\n"
    "                    # rationale и trade_mode (как в trade_outcomes.jsonl). Без них на живом\n"
    "                    # предсказании оба были константой 0. Значения — те же, что пишутся в сигнал.\n"
    '                    "rationale": f"intelligence_{result.reason}",\n'
    '                    "trade_mode": "scalp" if str(result.regime or "") in ("range", "scalp") else "trend",\n'
)
out = src.replace(anchor, anchor + add)
shutil.copy2(path, str(path) + ".bak-before-ml-candidate")
path.write_bytes((out.replace("\n", "\r\n") if crlf else out).encode("utf-8"))
print("OK:", path)
