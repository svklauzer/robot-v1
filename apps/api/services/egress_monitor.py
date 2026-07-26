"""Непрерывный монитор исходящей сети (#egress-monitor-2026-07-26).

Зачем. Статус-страница Render показывает «All Systems Operational» и «No downtime
recorded» за 26 июля, при этом инстанс в те же часы не мог достучаться ни до HTX,
ни до Kraken. Противоречия здесь нет: глобальная статус-страница отражает
платформенные сервисы (Dashboard, API, билды), а не исходящую сеть КОНКРЕТНОГО
инстанса в конкретном регионе к конкретному внешнему хосту. Такой слой на ней
и не может быть виден.

Значит доказательства нужно собирать самим — с той стороны, где проблема.

Монитор раз в минуту замеряет доступность набора хостов и пишет компактный
журнал. На выходе — временной ряд, пригодный для тикета в поддержку: когда
именно, к каким хостам, с какой задержкой и с какой ошибкой.

Ключевая деталь методики — КОНТРОЛЬНАЯ ГРУППА. Кроме бирж проверяются
нейтральные хосты (Cloudflare, Google, Telegram). Это позволяет разделить:

  * лежат биржи, контрольные живы      → адресная проблема (блокировка/маршрут);
  * лежит ВСЁ, включая контрольные     → egress/DNS самого инстанса;
  * DNS медленный, а TCP быстрый       → проблема резолвера, а не канала.

Только чтение сети, на торговлю не влияет.
"""
from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from typing import Any

from core.config import settings

# (хост, группа). Контрольная группа обязана быть разнородной: разные компании,
# разные AS, разные регионы — иначе она не отличает «сеть» от «совпадения».
TARGETS: list[tuple[str, str]] = [
    ("api.huobi.pro", "exchange"),
    ("api-aws.huobi.pro", "exchange"),
    ("futures.kraken.com", "exchange"),
    ("api.telegram.org", "control"),
    ("one.one.one.one", "control"),      # Cloudflare
    ("dns.google", "control"),           # Google
]


def _probe(host: str, timeout: float) -> dict[str, Any]:
    """DNS + TCP:443 с раздельным замером — важно различать, где именно теряется время."""
    out: dict[str, Any] = {"host": host}

    t0 = time.time()
    try:
        infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
        out["dns_ms"] = round((time.time() - t0) * 1000, 1)
        ip = infos[0][4][0]
    except Exception as e:  # noqa: BLE001
        out["dns_ms"] = round((time.time() - t0) * 1000, 1)
        out["ok"] = False
        out["stage"] = "dns"
        out["error_type"] = type(e).__name__
        return out

    t1 = time.time()
    try:
        with socket.create_connection((ip, 443), timeout=timeout):
            out["tcp_ms"] = round((time.time() - t1) * 1000, 1)
            out["ok"] = True
            out["stage"] = "ok"
    except Exception as e:  # noqa: BLE001
        out["tcp_ms"] = round((time.time() - t1) * 1000, 1)
        out["ok"] = False
        out["stage"] = "tcp"
        out["error_type"] = type(e).__name__
    return out


def probe_once(timeout: float | None = None) -> dict[str, Any]:
    """Один срез по всем целям. БЛОКИРУЮЩАЯ функция — звать через to_thread."""
    timeout = float(timeout or getattr(settings, "EGRESS_MONITOR_TIMEOUT_SEC", 5.0))
    results = [{**_probe(h, timeout), "group": g} for h, g in TARGETS]

    ex = [r for r in results if r["group"] == "exchange"]
    ctl = [r for r in results if r["group"] == "control"]
    ex_ok = sum(1 for r in ex if r.get("ok"))
    ctl_ok = sum(1 for r in ctl if r.get("ok"))

    if ex_ok == 0 and ctl_ok == 0:
        verdict = "egress_down"          # не работает исходящая сеть инстанса
    elif ex_ok == 0 and ctl_ok > 0:
        verdict = "exchanges_unreachable"  # адресная проблема с биржами
    elif ex_ok < len(ex):
        verdict = "partial"
    else:
        verdict = "ok"

    return {
        "ts": time.time(),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "verdict": verdict,
        "exchange_ok": ex_ok,
        "exchange_total": len(ex),
        "control_ok": ctl_ok,
        "control_total": len(ctl),
        "targets": results,
    }


def _path() -> Path:
    return Path(
        str(getattr(settings, "EGRESS_MONITOR_PATH", "") or "storage/ml/egress_monitor.jsonl")
    )


def log_snapshot(snapshot: dict[str, Any]) -> None:
    """Компактная строка в журнал (полные детали только когда что-то не так)."""
    row = {
        "ts": round(snapshot["ts"], 1),
        "v": snapshot["verdict"],
        "ex": f'{snapshot["exchange_ok"]}/{snapshot["exchange_total"]}',
        "ctl": f'{snapshot["control_ok"]}/{snapshot["control_total"]}',
    }
    if snapshot["verdict"] != "ok":
        row["bad"] = [
            {"h": t["host"], "s": t.get("stage"), "e": t.get("error_type"),
             "dns_ms": t.get("dns_ms"), "tcp_ms": t.get("tcp_ms")}
            for t in snapshot["targets"] if not t.get("ok")
        ]
    else:
        row["dns_ms_max"] = max((t.get("dns_ms") or 0) for t in snapshot["targets"])

    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — журнал не должен ронять воркер
        pass


def _load(limit: int = 20000) -> list[dict]:
    path = _path()
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:  # noqa: BLE001 — битая строка не валит историю
                    continue
    except Exception:  # noqa: BLE001
        return []
    return rows[-int(limit):]


def history(hours: float = 24.0) -> dict[str, Any]:
    """Сводка для тикета: сколько времени сеть была недоступна и в какие окна."""
    since = time.time() - float(hours) * 3600
    rows = [r for r in _load() if float(r.get("ts") or 0) >= since]
    if not rows:
        return {
            "status": "no_data",
            "hours": hours,
            "note": "монитор ещё не накопил данных — он пишет раз в минуту после старта",
        }

    total = len(rows)
    by_verdict: dict[str, int] = {}
    for r in rows:
        by_verdict[r.get("v", "?")] = by_verdict.get(r.get("v", "?"), 0) + 1

    # Непрерывные окна, когда сеть была не в порядке — это и есть доказательство.
    windows: list[dict] = []
    start = None
    prev = None
    for r in rows:
        bad = r.get("v") != "ok"
        if bad and start is None:
            start = r
        if not bad and start is not None:
            windows.append({
                "from": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start["ts"])),
                "to": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(prev["ts"])),
                "minutes": round((prev["ts"] - start["ts"]) / 60, 1),
                "verdict": start.get("v"),
                "sample": start.get("bad"),
            })
            start = None
        prev = r
    if start is not None and prev is not None:
        windows.append({
            "from": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start["ts"])),
            "to": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(prev["ts"])),
            "minutes": round((prev["ts"] - start["ts"]) / 60, 1),
            "verdict": start.get("v"),
            "sample": start.get("bad"),
            "ongoing": True,
        })

    ok = by_verdict.get("ok", 0)
    dns_samples = [r["dns_ms_max"] for r in rows if r.get("dns_ms_max") is not None]
    return {
        "status": "ok",
        "hours": hours,
        "samples": total,
        "availability_pct": round(ok / total * 100, 2),
        "by_verdict": by_verdict,
        "outage_windows": windows[-50:],
        "worst_dns_ms": max(dns_samples) if dns_samples else None,
        "note": (
            "Доказательная база для тикета: замеры сделаны С САМОГО инстанса. "
            "verdict=egress_down — не отвечали и биржи, и контрольные хосты "
            "(Cloudflare/Google/Telegram), то есть проблема в исходящей сети "
            "инстанса, а не у биржи. Глобальная статус-страница платформы этот "
            "слой не покрывает и «All Systems Operational» ему не противоречит."
        ),
    }
