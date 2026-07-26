"""Диагностика доступности биржи (#htx-outage-2026-07-26).

Мотивация: в логах инцидента 26.07 было только
`{"error": "htx GET https://api.huobi.pro/v1/common/timestamp"}` — это формат
ccxt.RequestTimeout, из которого НЕЛЬЗЯ понять причину: DNS не резолвится,
TCP не проходит, TLS не встаёт, прилетел 403 (гео-блокировка ДЦ) или биржа
реально молчит. Владелец потратил время на проверку API-ключа, хотя падал
публичный эндпоинт, где ключ вообще не используется.

Сервис разбирает цепочку по шагам и по КАЖДОМУ хосту, чтобы ответ был
однозначным. Только чтение, на торговлю не влияет.
"""
from __future__ import annotations

import socket
import ssl
import time
from typing import Any

from core.config import settings


def _probe_host(host: str, *, timeout: float) -> dict[str, Any]:
    step: dict[str, Any] = {"host": host}

    # 1. DNS
    t0 = time.time()
    try:
        infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
        step["dns"] = {
            "ok": True,
            "ms": round((time.time() - t0) * 1000, 1),
            "addresses": sorted({i[4][0] for i in infos})[:5],
        }
    except Exception as e:  # noqa: BLE001
        step["dns"] = {"ok": False, "error_type": type(e).__name__, "error": str(e)}
        step["verdict"] = "DNS не резолвится — проблема резолвера или домен заблокирован на уровне сети"
        return step

    ip = step["dns"]["addresses"][0]

    # 2. TCP
    t0 = time.time()
    try:
        with socket.create_connection((ip, 443), timeout=timeout):
            step["tcp"] = {"ok": True, "ms": round((time.time() - t0) * 1000, 1)}
    except Exception as e:  # noqa: BLE001
        step["tcp"] = {"ok": False, "error_type": type(e).__name__, "error": str(e)}
        step["verdict"] = (
            "DNS есть, но TCP:443 не проходит — трафик из ДЦ режется "
            "(гео-блок по IP или сетевая фильтрация). Именно так выглядел инцидент 26.07"
        )
        return step

    # 3. TLS
    t0 = time.time()
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((ip, 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host):
                step["tls"] = {"ok": True, "ms": round((time.time() - t0) * 1000, 1)}
    except Exception as e:  # noqa: BLE001
        step["tls"] = {"ok": False, "error_type": type(e).__name__, "error": str(e)}
        step["verdict"] = "TCP есть, TLS не встаёт — вероятен MITM/перехват или SNI-фильтрация"
        return step

    # 4. HTTP на публичный эндпоинт (ключи не нужны)
    t0 = time.time()
    try:
        import httpx

        r = httpx.get(f"https://{host}/v1/common/timestamp", timeout=timeout)
        step["http"] = {
            "ok": r.status_code == 200,
            "status": r.status_code,
            "ms": round((time.time() - t0) * 1000, 1),
            "body": r.text[:200],
        }
        if r.status_code == 200:
            step["verdict"] = "OK — хост полностью доступен"
        elif r.status_code in (403, 451):
            step["verdict"] = (
                f"HTTP {r.status_code} — биржа отвечает, но ОТКАЗЫВАЕТ этому IP "
                "(гео-блокировка ДЦ). Нужен прокси через разрешённый регион: HTX_PROXY_URL"
            )
        else:
            step["verdict"] = f"HTTP {r.status_code} — биржа отвечает ошибкой, не сеть"
    except Exception as e:  # noqa: BLE001
        step["http"] = {"ok": False, "error_type": type(e).__name__, "error": str(e)}
        step["verdict"] = "TLS есть, но HTTP-ответа нет — биржа принимает соединение и молчит"

    return step


def diagnose(timeout: float | None = None) -> dict[str, Any]:
    """Полный отчёт по всем хостам HTX + состояние размыкателя."""
    from services.htx_client import HTXClient

    timeout = float(timeout or 8.0)
    hosts = HTXClient._cb_hosts() or ["api.huobi.pro"]
    results = [_probe_host(h, timeout=timeout) for h in hosts]

    reachable = [r for r in results if (r.get("http") or {}).get("ok")]
    return {
        "status": "ok",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "any_host_reachable": bool(reachable),
        "reachable_hosts": [r["host"] for r in reachable],
        "recommended_hostname": reachable[0]["host"] if reachable else None,
        "proxy_configured": bool(str(getattr(settings, "HTX_PROXY_URL", "") or "").strip()),
        "circuit": HTXClient.circuit_state(),
        "hosts": results,
        "note": (
            "Публичный /v1/common/timestamp не требует API-ключа: если он не "
            "отвечает, статус ключа значения не имеет. Если доступен НЕ основной "
            "хост — переставь HTX_API_HOSTNAME на него. Если недоступны все и "
            "виден 403/451 — нужен HTX_PROXY_URL через разрешённый регион."
        ),
    }
