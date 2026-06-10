"""Trigger the daily owner report from a Render Cron Job.

Запускается отдельным сервисом type: cron (см. render.yaml) каждое утро.
Делает POST на /reports/send-owner у работающего API — бот сам шлёт сводку
владельцу в Telegram. httpx уже есть в requirements.txt.
"""
import os
import sys

import httpx

API = os.environ.get("REPORT_API_URL", "https://robot-api-fx9h.onrender.com").rstrip("/")
TOKEN = os.environ.get("OWNER_API_TOKEN", "")
HOURS = int(os.environ.get("REPORT_HOURS", "24"))


def main() -> int:
    if not TOKEN:
        print("OWNER_API_TOKEN is not set — cannot send owner report", file=sys.stderr)
        return 1
    try:
        resp = httpx.post(
            f"{API}/reports/send-owner",
            params={"hours": HOURS},
            headers={"X-Owner-Token": TOKEN},
            timeout=60.0,
        )
        print(f"send-owner status={resp.status_code} body={resp.text[:500]}")
        resp.raise_for_status()
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"daily owner report failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
