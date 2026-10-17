"""Правит services/signal_lifecycle.py: result_pct снова в % НОМИНАЛА, а не маржи.

Запуск:  python scripts/patch_signal_lifecycle_units.py [путь к signal_lifecycle.py]
Бэкап .bak-before-units; если блок не найден — файл не меняется.
Убирает пересчёт на маржу внутри `if partial_net is not None` и ставит после него
безусловный пересчёт net_pnl / (qty*entry) * 100 — как было до сигнала #621.
"""
import re
import shutil
import sys
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else "apps/api/services/signal_lifecycle.py")
raw = path.read_bytes()
crlf = b"\r\n" in raw
src = raw.decode("utf-8").replace("\r\n", "\n")
if "pct_of_notional" in src:
    print("уже применено")
    sys.exit(0)

pat = re.compile(
    r"            # Консистентность фронта:.*?\n"
    r"(?:            #.*\n)*?"
    r"            try:\n"
    r"                base_margin = float\(signal\.required_margin or \(signal\.plan_json or \{\}\)\.get\(\"required_margin\"\) or 0\.0\)\n"
    r"                if base_margin > 0 and net_pnl is not None:\n"
    r"                    result_pct = round\(float\(net_pnl\) / base_margin \* 100\.0, 4\)\n"
    r"            except Exception:\n"
    r"                pass\n"
)
m = pat.search(src)
if not m:
    sys.exit("БЛОК НЕ НАЙДЕН — файл не изменён (код ушёл от версии из аудита)")
new_tail = (
    "\n"
    "        # (#result-units-2026-10-17) result_pct — % НОМИНАЛА (как mfe_pct, traj и\n"
    "        # инвариант closed_net_pnl == notional*result_pct/100), НЕ % маржи: от маржи он\n"
    "        # ×плечо и ломал phantom_fill / exit_replay / capture / ML. Считаем по итогу\n"
    "        # сделки (net_pnl уже с частями) при любом исходе close_result.\n"
    "        if net_pnl is not None:\n"
    "            from services.result_units import pct_of_notional\n"
    "            _pct = pct_of_notional(net_pnl, signal)\n"
    "            if _pct is not None:\n"
    "                result_pct = _pct\n"
)
out = src[: m.start()] + src[m.end():]
anchor = "\n        signal.status = \"closed\"\n        signal.result_pct = result_pct\n"
assert out.count(anchor) == 1, "якорь signal.status/result_pct не найден или не единственный"
out = out.replace(anchor, new_tail + anchor)
shutil.copy2(path, str(path) + ".bak-before-units")
path.write_bytes((out.replace("\n", "\r\n") if crlf else out).encode("utf-8"))
print("OK:", path)
