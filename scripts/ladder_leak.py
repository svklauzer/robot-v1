# scripts/ladder_leak.py
import os, json, urllib.request
from collections import Counter

TOKEN = os.environ["OWNER_TOKEN"]
BASE = "https://robot-api-1rgi.onrender.com"
def get(p):
    r = urllib.request.Request(BASE + p); r.add_header("X-Owner-Token", TOKEN)
    return json.loads(urllib.request.urlopen(r, timeout=60).read())

items = []
for off in (0, 200, 400):
    items += get(f"/signals?limit=200&offset={off}").get("items", [])

BAND_ARM, BAND_FLOOR, BAND_GIVE = 0.25, 0.30, 0.25
RIDE_ARM, RIDE_GIVE = 0.80, 0.50
arm_eff = max(BAND_ARM, BAND_FLOOR / (1 - BAND_GIVE))
PROTECTIVE = {"trend_capture_band","adaptive_mfe_capture","trend_trailing_stop",
              "adaptive_trailing_stop","protective_trailing_stop","adaptive_post_tp1_stop",
              "post_tp1_lock_stop","post_tp1_giveback_trail","breakeven_lock",
              "tz_mfe_giveback_backstop","scalp_breakeven_lock","scalp_flow_exit"}

reasons, leak, rows = Counter(), 0.0, []
for s in items:
    if s.get("status") != "closed": continue
    p = s.get("plan") or {}
    if p.get("trade_mode") != "trend": continue
    reasons[s.get("closed_reason")] += 1
    lc = p.get("lifecycle") or {}
    mfe = float(lc.get("mfe_pct") or 0)
    tp1d = float((p.get("tp_reach") or {}).get("tp1_dist_pct") or 99)
    if mfe >= tp1d: continue                      # TP1 дошли — работает lock
    if mfe >= RIDE_ARM:  level, tier = mfe * (1 - RIDE_GIVE), "ride"
    elif mfe >= arm_eff: level, tier = max(mfe * (1 - BAND_GIVE), BAND_FLOOR), "band"
    else: continue
    traj = lc.get("traj") or []
    if not traj: continue
    peak = max(range(len(traj)), key=lambda i: traj[i][1])
    crossed = next((t for t in traj[peak+1:] if t[1] <= level), None)
    if crossed is None: continue
    if s.get("closed_reason") in PROTECTIVE: continue
    notional = float(s.get("qty") or 0) * float(lc.get("entry_price") or 0)
    rt = float(((p.get("config") or {}).get("market") or {}).get("round_trip_pct") or 0.12)
    would = notional * (level - rt) / 100.0
    actual = float(s.get("closed_net_pnl") or 0)
    leak += would - actual
    rows.append((s["id"], s["symbol"], tier, round(mfe,3), round(level,3),
                 round(would,2), round(actual,2), s.get("closed_reason")))

print("Причины закрытия TREND:", dict(reasons))
print(f"\nВзвелось, пересекло, НЕ защищено: {len(rows)} сделок")
for r in rows[:25]: print("  ", r)
print(f"\nОСТАВЛЕНО НА СТОЛЕ: {leak:+.2f} USDT")