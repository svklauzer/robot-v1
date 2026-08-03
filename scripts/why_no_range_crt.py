#!/usr/bin/env python3
"""Почему range и crt перестали закрываться после OB_MARKET_TYPE=swap.

Две конкурирующие версии, и они различимы данными:

  A. Книга сменилась, и depth-гейт режет их порогами, калиброванными на споте.
     `range` идёт по профилю scalp (спред <= OB_MAX_SPREAD_PCT, дефолт 0.08%),
     `crt` — по position. Если своповые спреды/OBI/поток отличаются, кандидаты
     будут отклоняться.

  B. Кандидатов просто не было. `range` требует подтверждённого боковика на 4h
     и 1h, `crt` — трёхсвечного свипа. Если рынок эти дни трендил, отсутствие
     сделок — правильное поведение, а не поломка.

Различаются так: если кандидаты порождались и были отклонены — они лежат в
`/intelligence/events` со `status=blocked`. Если их не порождалось вовсе —
там пусто, и версия A отпадает.

Дополнительно скрипт сравнивает характеристики книги ДО и ПОСЛЕ перехода по
`plan_json.entry_depth` из локальной выгрузки: спред, OBI, число сделок в CVD.
Сдвиг распределения — прямая проверка версии A.

Запуск:
    $env:OWNER_TOKEN = "<токен>"
    python scripts\\why_no_range_crt.py analytics_24h\\signals_export_<...>.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_API = "https://robot-api-1rgi.onrender.com"
DEPTH_KEYS = ("spread_pct", "obi", "cvd_ratio", "cvd_trades",
              "bid_wall_share", "ask_wall_share")


def fetch(api: str, token: str, path: str) -> dict:
    req = urllib.request.Request(f"{api}{path}", headers={"X-Owner-Token": token})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def plan(row):
    return row.get("plan_json") or row.get("plan") or {}


def is_new(row):
    return isinstance(plan(row).get("trend_trigger"), dict)


def summarise(name: str, values: list[float]) -> str:
    if not values:
        return f"    {name:<16} нет данных"
    values = sorted(values)
    q = lambda p: values[min(int(p * len(values)), len(values) - 1)]
    return (f"    {name:<16} n={len(values):<4} медиана {q(0.5):>8.4f}  "
            f"p10 {q(0.1):>8.4f}  p90 {q(0.9):>8.4f}  макс {values[-1]:>8.4f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("export", nargs="?", help="локальная выгрузка сигналов")
    ap.add_argument("--api", default=os.environ.get("API_URL", DEFAULT_API))
    ap.add_argument("--events", type=int, default=600, help="сколько событий тянуть")
    args = ap.parse_args()
    token = os.environ.get("OWNER_TOKEN")

    # ── Часть 1: были ли кандидаты вообще ───────────────────────────────────
    print("=" * 78)
    print("1. БЫЛИ ЛИ КАНДИДАТЫ range/crt (лента решений)")
    print("-" * 78)
    if not token:
        print("  $env:OWNER_TOKEN не задан — часть по API пропущена.")
    else:
        rows: list[dict] = []
        offset = 0
        while len(rows) < args.events:
            try:
                page = fetch(args.api, token, f"/intelligence/events?limit=200&offset={offset}")
            except Exception as exc:  # noqa: BLE001
                print(f"  не удалось получить события: {exc}")
                break
            items = page.get("items") or []
            if not items:
                break
            rows.extend(items)
            offset += 200
            if len(items) < 200:
                break

        if rows:
            print(f"  получено событий: {len(rows)}")
            by_regime = Counter(str(e.get("regime") or "?") for e in rows)
            print(f"  по режимам: {dict(by_regime)}")
            print()
            for target in ("range", "crt"):
                sub = [e for e in rows if str(e.get("regime") or "") == target]
                if not sub:
                    print(f"  {target}: кандидатов НЕ БЫЛО ВОВСЕ → версия A отпадает,")
                    print(f"           рынок не давал сетапа (версия B)")
                    continue
                st_c = Counter(str(e.get("status") or "?") for e in sub)
                dec = Counter(str(e.get("decision") or "?") for e in sub)
                print(f"  {target}: кандидатов {len(sub)}, статусы {dict(st_c)}")
                print(f"           причины: {dict(dec.most_common(8))}")

        try:
            funnel = fetch(args.api, token, "/intelligence/funnel?limit=200")
            print("\n  Воронка кандидат → публикация → позиция:")
            print("  " + json.dumps(funnel, ensure_ascii=False)[:1200])
        except Exception as exc:  # noqa: BLE001
            print(f"  воронка недоступна: {exc}")

    # ── Часть 2: изменилась ли книга ────────────────────────────────────────
    print("\n" + "=" * 78)
    print("2. ИЗМЕНИЛАСЬ ЛИ КНИГА (spot → swap) — сравнение entry_depth")
    print("-" * 78)
    if not args.export:
        print("  выгрузка не передана — раздел пропущен")
        return 0

    payload = json.loads(Path(args.export).read_text(encoding="utf-8"))
    items = payload.get("items") if isinstance(payload, dict) else payload
    closed = [r for r in (items or []) if r.get("status") == "closed"]

    buckets: dict[str, dict[str, list]] = {
        "старая (спот)": defaultdict(list),
        "новая (своп)": defaultdict(list),
    }
    for r in closed:
        d = plan(r).get("entry_depth")
        if not isinstance(d, dict):
            continue
        key = "новая (своп)" if is_new(r) else "старая (спот)"
        for k in DEPTH_KEYS:
            v = d.get(k)
            if isinstance(v, (int, float)):
                buckets[key][k].append(float(v))

    for label, data in buckets.items():
        print(f"\n  {label}:")
        for k in DEPTH_KEYS:
            print(summarise(k, data.get(k, [])))

    print("\n" + "-" * 78)
    print("  Пороги, с которыми это сравнивать:")
    print("    range/scalp профиль: спред <= OB_MAX_SPREAD_PCT (дефолт 0.08%)")
    print("    obi_hard_veto 0.45, cvd_min_trades 25")
    print("  Если медиана спреда на свопе перескочила 0.08 — версия A подтверждена.")
    print("  Если распределения похожи, а кандидатов range/crt не было — версия B.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
