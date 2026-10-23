"""Отчёт по наблюдению trend_resume: входы «с возобновлением» против «без».

Критерий РЕШЕНИЯ зафиксирован заранее (23.10.2026), по новым сделкам, не по истории:
  • сделки trade_mode=trend, у которых в plan.trend_resume history_complete=true;
  • всего >= 100 и в каждой группе (resumed / не resumed) >= 25;
  • нижняя граница 95% блочного ДИ разницы среднего net (resumed − не resumed) > 0.
Только тогда обсуждаем превращение события в условие входа. Иначе — не превращаем.

Запуск (из корня репозитория), нужны ОБА файла выгрузки:
  python scripts\\trend_resume_report.py signals.json storage\\ml\\trade_outcomes.jsonl
signals.json — выгрузка /signals ({"items": [...]}) или список; plan внутри каждого сигнала.
"""
import json, random, statistics as st, sys

if len(sys.argv) < 3:
    sys.exit(__doc__)
raw = json.load(open(sys.argv[1], encoding="utf-8"))
items = raw["items"] if isinstance(raw, dict) else raw
plan = {int(x["id"]): (x.get("plan") or {}) for x in items if str(x.get("id", "")).isdigit()}
rows = [json.loads(l) for l in open(sys.argv[2], encoding="utf-8") if l.strip()]


def net(x):
    t = x.get("total_net_pnl")
    return (x.get("closed_net_pnl") if t is None else t) or 0.0


data = []
for x in sorted(rows, key=lambda r: str(r.get("opened_at", ""))):
    if x.get("trade_mode") != "trend":
        continue
    try:
        tr = plan.get(int(x["signal_id"]), {}).get("trend_resume")
    except (TypeError, ValueError):
        continue
    if isinstance(tr, dict):
        data.append((tr, net(x), x))

seen = sum(1 for tr, _, _ in data if tr.get("samples"))
full = [(tr, n, x) for tr, n, x in data if tr.get("history_complete") and tr.get("resumed") is not None]
print(f"трендовых сделок с полем trend_resume: {len(data)}; с замерами: {seen}; с полной историей: {len(full)}")
A = [n for tr, n, _ in full if tr["resumed"]]
B = [n for tr, n, _ in full if not tr["resumed"]]
print(f"resumed: n={len(A)} среднее net={st.mean(A):+.2f}" if A else "resumed: нет данных")
print(f"не resumed: n={len(B)} среднее net={st.mean(B):+.2f}" if B else "не resumed: нет данных")
if len(A) < 2 or len(B) < 2:
    sys.exit("Недостаточно данных для сравнения.")

nets = [n for _, n, _ in full]
flags = [bool(tr["resumed"]) for tr, _, _ in full]
diff = st.mean(A) - st.mean(B)
rnd = random.Random(42)
blk = 20
bs = []
for _ in range(2000):
    idx = []
    while len(idx) < len(nets):
        s0 = rnd.randrange(0, len(nets))
        idx.extend(range(s0, min(s0 + blk, len(nets))))
    idx = idx[: len(nets)]
    a = [nets[i] for i in idx if flags[i]]
    b = [nets[i] for i in idx if not flags[i]]
    if a and b:
        bs.append(st.mean(a) - st.mean(b))
bs.sort()
lo, hi = bs[int(0.025 * len(bs))], bs[int(0.975 * len(bs)) - 1]
print(f"разница (resumed − нет): {diff:+.2f} USDT/сделку, CI95 [{lo:+.2f}; {hi:+.2f}]")
ok = len(full) >= 100 and len(A) >= 25 and len(B) >= 25 and lo > 0
if len(full) < 100 or len(A) < 25 or len(B) < 25:
    print("РЕШЕНИЕ: рано (нужно >=100 всего и >=25 в каждой группе).")
else:
    print("РЕШЕНИЕ:", "критерий выполнен — можно обсуждать условие входа." if ok else "критерий НЕ выполнен — событие условием входа не делаем.")
