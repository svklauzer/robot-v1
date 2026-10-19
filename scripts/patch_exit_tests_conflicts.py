"""Правка двух расхождений, внесённых d19e89a (22.09.2026).

1) exit_policy.before_tp1_decision: ранний CRITICAL_BE_RESCUE не должен работать
   в TZ-ветке (трендовый выход по ТЗ). Там порядок такой: структурный выход ->
   бэкстоп с гейтом MIN_PROTECTIVE_NET_USDT. Вне TZ-ветки (скальп, range,
   trend без tz_context) блок остаётся как есть.
2) tp_reachability: добавить post_tp1_atr_trailing_stop в _VOLUNTARY_REASONS
   (это наш собственный добровольный выход, иначе он считается настоящим
   промахом цели и ужимает гейт входа).

Запуск из корня репозитория:  python scripts/patch_exit_tests_conflicts.py
Идемпотентен, делает .bak-before-tz-be-fix. Кодировка UTF-8, переносы строк сохраняются.
"""
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
EP = ROOT / "apps/api/services/exit_policy.py"
TR = ROOT / "apps/api/services/tp_reachability.py"


def load(p):
    raw = p.read_bytes().decode("utf-8")
    return raw.replace("\r\n", "\n"), "\r\n" in raw


def save(p, text, crlf):
    p.with_name(p.name + ".bak-before-tz-be-fix").write_bytes(p.read_bytes())
    p.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))


def replace_once(text, old, new, label):
    n = text.count(old)
    if n != 1:
        sys.exit(f"[{label}] якорь найден {n} раз(а), ожидался 1 - файл не менялся")
    return text.replace(old, new)


# ---- 1. exit_policy -------------------------------------------------------
t, crlf = load(EP)
if "tz_path_active" in t:
    print("exit_policy: уже применено")
else:
    old = (
        "        if be_enabled and mfe >= be_arm:\n"
        "            if current_pct <= be_floor or current_pct <= be_hard_floor:\n"
    )
    new = (
        "        # (#be-rescue-not-in-tz-2026-10-19) В TZ-ветке порядок приоритета другой:\n"
        "        # структурный выход ТЗ -> бэкстоп отдачи (с гейтом MIN_PROTECTIVE_NET_USDT).\n"
        "        # Ранний замок пробивал и структуру (tz_kama), и net-гейт (ADA #464).\n"
        "        tz_path_active = (\n"
        "            bool(getattr(settings, \"TZ_TREND_EXIT_ONLY\", False))\n"
        "            and str(trade_mode or \"\").lower() in (\"trend\", \"trend_up\", \"trend_down\", \"ride\")\n"
        "            and isinstance(tz_context, dict)\n"
        "        )\n"
        "        if be_enabled and not tz_path_active and mfe >= be_arm:\n"
        "            if current_pct <= be_floor or current_pct <= be_hard_floor:\n"
    )
    t = replace_once(t, old, new, "exit_policy")
    save(EP, t, crlf)
    print("exit_policy: ok")

# ---- 2. tp_reachability ---------------------------------------------------
t, crlf = load(TR)
if "post_tp1_atr_trailing_stop" in t:
    print("tp_reachability: уже применено")
else:
    old = '    "failed_setup_exit", "low_grade_capital_release",\n'
    new = (
        '    "failed_setup_exit", "low_grade_capital_release",\n'
        '    "post_tp1_atr_trailing_stop",\n'
    )
    t = replace_once(t, old, new, "tp_reachability")
    save(TR, t, crlf)
    print("tp_reachability: ok")
