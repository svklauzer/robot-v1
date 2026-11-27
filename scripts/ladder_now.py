import os, json, urllib.request

TOKEN = os.environ["OWNER_TOKEN"]
BASE = "https://robot-api-1rgi.onrender.com"

def get(p):
    r = urllib.request.Request(BASE + p)
    r.add_header("X-Owner-Token", TOKEN)
    return json.loads(urllib.request.urlopen(r, timeout=60).read())

items = get("/signals?limit=30")["items"]

print("=== last 30 ===")
for s in items:
    p = s.get("plan") or {}
    lc = p.get("lifecycle") or {}
    print(s["id"], s["symbol"], s["status"], p.get("trade_mode"),
          "mfe=", lc.get("mfe_pct"), "cur=", lc.get("current_pct"),
          "closed=", lc.get("closed_at"), "reason=", s.get("closed_reason"))

print()
print("=== open trend: expected tier now ===")
RIDE_ARM, RIDE_DD = 0.8, 0.5
BAND_ARM, BAND_GIVE, BAND_FLOOR = 0.25, 0.25, 0.30
found = False
for s in items:
    if s["status"] not in ("opened", "tp1"):
        continue
    p = s.get("plan") or {}
    if p.get("trade_mode") != "trend":
        continue
    found = True
    lc = p.get("lifecycle") or {}
    mfe = float(lc.get("mfe_pct") or 0)
    cur = float(lc.get("current_pct") or 0)
    dd = max(mfe - cur, 0.0)
    if mfe >= RIDE_ARM and dd >= mfe * RIDE_DD:
        v = "ride"
    elif (BAND_ARM <= mfe < RIDE_ARM and dd >= mfe * BAND_GIVE
          and min(mfe * (1 - BAND_GIVE), cur) >= BAND_FLOOR):
        v = "band"
    else:
        v = "hold"
    print(s["id"], s["symbol"], "mfe=", round(mfe, 4), "cur=", round(cur, 4),
          "dd=", round(dd, 4), "expected=", v)
if not found:
    print("нет открытых трендовых позиций")
