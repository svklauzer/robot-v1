# scripts/ladder_verify.py
import os, sys, json, urllib.request
from collections import Counter

TOKEN = os.environ["OWNER_TOKEN"]
BASE = "https://robot-api-1rgi.onrender.com"
SINCE = sys.argv[1] if len(sys.argv) > 1 else "2026-11-27T23:14:55+05:00"

def get(p):
    r = urllib.request.Request(BASE + p); r.add_header("X-Owner-Token", TOKEN)
    return json.loads(urllib.request.urlopen(r, timeout=60).read())

items = []
for off in (0, 200, 400):
    items += get(f"/signals?limit=200&offset={off}").get("items", [])

PROT = {"trend_ride_trailing_stop", "trend_capture_band", "adaptive_mfe_capture",
        "protective_trailing_stop", "adaptive_trailing_stop", "breakeven_lock",
        "post_tp1_lock_stop", "post_tp1_giveback_trail", "tz_mfe_giveback_backstop"}

eras = {"before": Counter(), "after": Counter()}
n = Counter()
for s in items:
    if s.get("status") != "closed": continue
    p = s.get("plan") or {}
    if p.get("trade_mode") != "trend": continue
    lc = p.get("lifecycle") or {}
    ca = str(lc.get("closed_at") or "")
    era = "after" if ca >= SINCE else "before"
    eras[era][s.get("closed_reason")] += 1
    n[era] += 1

print(f" cutoff = {SINCE}")
for era in ("before", "after"):
    print(f"\n{era.upper():6} n={n[era]}")
    for r, c in eras[era].most_common():
        mark = " ← НОВОЕ" if era == "after" and r in ("trend_ride_trailing_stop", "trend_capture_band") else ""
        print(f"   {c:4d}  {r}{mark}")