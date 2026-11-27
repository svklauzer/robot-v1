import os, json, urllib.request
from datetime import datetime, timezone
TOKEN = os.environ["OWNER_TOKEN"]; BASE = "https://robot-api-1rgi.onrender.com"
def get(p):
    r = urllib.request.Request(BASE + p); r.add_header("X-Owner-Token", TOKEN)
    return json.loads(urllib.request.urlopen(r, timeout=60).read())

now = datetime.now(timezone.utc)
print("now:", now.isoformat())

print("\n=== open signals: свежесть ведения ===")
for s in get("/signals?limit=30")["items"]:
    if s["status"] not in ("opened", "tp1", "published"):
        continue
    lc = (s.get("plan") or {}).get("lifecycle") or {}
    ls = lc.get("last_seen_at"); age = None
    if ls:
        try:
            age = (now - datetime.fromisoformat(str(ls).replace("Z", "+00:00"))).total_seconds()
        except Exception:
            pass
    print(s["id"], s["symbol"], s["status"], "updates=", lc.get("updates"),
          "age_sec=", round(age) if age is not None else None, "last_seen=", ls)

print("\n=== bot ===")
print(get("/intelligence/funnel").get("bot"))

print("\n=== последние события ===")
for e in get("/intelligence/events?limit=12")["items"]:
    print(e["id"], e["created_at"], e["symbol"], e["status"], e["decision"])
