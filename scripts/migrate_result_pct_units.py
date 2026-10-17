"""Миграция result_pct закрытых сделок с #621: % маржи -> % номинала.

Сухой прогон по умолчанию; запись — флаг --apply (перед ним скопируйте БД).
Трогает ТОЛЬКО строки, где result_pct совпадает с closed_net_pnl/required_margin*100
(признак новой, ошибочной единицы). Пересчитывает result_pct, lifecycle.final_result_pct
и missed_profit_pct. Запуск из apps/api:  python ../../scripts/migrate_result_pct_units.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
from sqlalchemy.orm.attributes import flag_modified  # noqa: E402

try:
    from core.database import SessionLocal  # noqa: E402
except ImportError:
    from db.session import SessionLocal  # noqa: E402  # поправьте под свой проект
from models.signal import Signal  # noqa: E402
from services.result_units import pct_of_notional  # noqa: E402

apply = "--apply" in sys.argv
db = SessionLocal()
fixed = skipped = 0
for s in db.query(Signal).filter(Signal.status == "closed", Signal.id >= 621).all():
    try:
        r = float(s.result_pct)
        net = float(s.closed_net_pnl)
        margin = float(s.required_margin or (s.plan_json or {}).get("required_margin") or 0)
    except (TypeError, ValueError):
        skipped += 1
        continue
    if margin <= 0 or abs(r - net / margin * 100.0) > 0.01:
        skipped += 1          # уже в % номинала или иная формула — не трогаем
        continue
    new = pct_of_notional(net, s)
    if new is None:
        skipped += 1
        continue
    fixed += 1
    if fixed <= 5:
        print(f"#{s.id}: {r} -> {new}")
    if apply:
        s.result_pct = new
        plan = dict(s.plan_json or {})
        lc = dict(plan.get("lifecycle") or {})
        lc["final_result_pct"] = new
        try:
            lc["missed_profit_pct"] = round(max(float(lc["mfe_pct"]) - new, 0.0), 4)
        except (KeyError, TypeError, ValueError):
            pass
        plan["lifecycle"] = lc
        s.plan_json = plan
        flag_modified(s, "plan_json")
if apply:
    db.commit()
print(("ЗАПИСАНО" if apply else "СУХОЙ ПРОГОН"), "исправлено:", fixed, "пропущено:", skipped)
