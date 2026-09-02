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


def _probe_host(
    host: str, *, timeout: float, role: str = "exchange", path: str | None = None
) -> dict[str, Any]:
    """(#diag-control-role-2026-07-27) role различает СМЫСЛ проверки.

    Для хоста биржи важен рабочий ответ на публичный эндпоинт (по умолчанию —
    HTX; для второй биржи см. diagnose_okx() ниже, у неё свой публичный путь,
    передаётся через `path`). Для контрольного хоста важно ровно одно: доходит
    ли до него сеть. Дёргать у Kraken и Telegram путь HTX бессмысленно — его
    там нет, и ответ 302/404 ЕСТЬ доказательство живой сети, а не ошибка.

    Первая версия этого не различала и красила живой Kraken в красный с
    подписью «биржа отвечает ошибкой». Контрольная группа существует, чтобы
    снимать ложную тревогу, — а сама её и создавала.
    """
    step: dict[str, Any] = {"host": host, "role": role}

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

    # 4. HTTP. Путь и критерий успеха зависят от роли хоста.
    is_control = role == "control"
    if path is None:
        path = "/" if is_control else "/v1/common/timestamp"
    t0 = time.time()
    try:
        import httpx

        r = httpx.get(f"https://{host}{path}", timeout=timeout, follow_redirects=False)
        ms = round((time.time() - t0) * 1000, 1)

        if is_control:
            # Любой HTTP-ответ = сеть до хоста доходит. Это всё, что от него нужно.
            step["http"] = {"ok": True, "status": r.status_code, "ms": ms}
            step["verdict"] = f"сеть доходит (HTTP {r.status_code})"
            return step

        step["http"] = {
            "ok": r.status_code == 200,
            "status": r.status_code,
            "ms": ms,
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
        step["verdict"] = (
            "TLS встаёт, но HTTP-ответа нет — сеть до хоста есть, отвечать он перестал"
            if is_control
            else "TLS есть, но HTTP-ответа нет — биржа принимает соединение и молчит"
        )

    return step


def diagnose(timeout: float | None = None) -> dict[str, Any]:
    """Полный отчёт по всем хостам HTX + состояние размыкателя."""
    from services.htx_client import HTXClient

    timeout = float(timeout or 8.0)
    hosts = HTXClient._cb_hosts() or ["api.huobi.pro"]

    # (#egress-guard-2026-07-26) Контрольные хосты вне HTX. В инциденте 26.07
    # одновременно лёг и Kraken — если не резолвятся и они, проблема в egress/DNS
    # ДЦ, а не в бирже, и менять HTX_API_HOSTNAME бесполезно.
    control = ["futures.kraken.com", "api.telegram.org"]
    results = [_probe_host(h, timeout=timeout, role="exchange") for h in hosts]
    control_results = [_probe_host(h, timeout=timeout, role="control") for h in control]

    reachable = [r for r in results if (r.get("http") or {}).get("ok")]
    control_dns_ok = [r for r in control_results if (r.get("dns") or {}).get("ok")]

    if not reachable and not control_dns_ok:
        verdict = (
            "НЕ РЕЗОЛВЯТСЯ НИ БИРЖИ, НИ КОНТРОЛЬНЫЕ ХОСТЫ — проблема исходящей "
            "сети/DNS всего инстанса, а не HTX. Менять HTX_API_HOSTNAME бесполезно; "
            "смотреть сторону Render (egress, DNS-резолвер)"
        )
    elif not reachable and control_dns_ok:
        verdict = (
            "Контрольные хосты живы, а хосты HTX — нет: проблема адресная. "
            "Это либо блокировка HTX для этого ДЦ, либо смена эндпоинта — "
            "нужен HTX_PROXY_URL или другой HTX_API_HOSTNAME"
        )
    else:
        verdict = "Есть доступный хост HTX — см. recommended_hostname"

    return {
        "status": "ok",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "verdict": verdict,
        "any_host_reachable": bool(reachable),
        "reachable_hosts": [r["host"] for r in reachable],
        "recommended_hostname": reachable[0]["host"] if reachable else None,
        "proxy_configured": bool(str(getattr(settings, "HTX_PROXY_URL", "") or "").strip()),
        "circuit": HTXClient.circuit_state(),
        "hosts": results,
        "control_hosts": control_results,
        "note": (
            "Публичный /v1/common/timestamp не требует API-ключа: если он не "
            "отвечает, статус ключа значения не имеет. Если доступен НЕ основной "
            "хост — переставь HTX_API_HOSTNAME на него. Если недоступны все и "
            "виден 403/451 — нужен HTX_PROXY_URL через разрешённый регион."
        ),
    }


def diagnose_okx(timeout: float | None = None) -> dict[str, Any]:
    """(#okx-satellite-2026-09-02) То же самое для OKX — см. diagnose() выше
    для полного обоснования формата. Только чтение, не завязано на то, какая
    биржа сейчас активна (ACTIVE_EXCHANGE): видимость обеих бирж полезна
    независимо от того, кто торгует, в т.ч. для проверки перед переключением."""
    from services.okx_client import OKXClient

    timeout = float(timeout or 8.0)
    hosts = OKXClient._cb_hosts() or ["www.okx.com"]

    control = ["futures.kraken.com", "api.telegram.org"]
    results = [
        _probe_host(h, timeout=timeout, role="exchange", path="/api/v5/public/time")
        for h in hosts
    ]
    control_results = [_probe_host(h, timeout=timeout, role="control") for h in control]

    reachable = [r for r in results if (r.get("http") or {}).get("ok")]
    control_dns_ok = [r for r in control_results if (r.get("dns") or {}).get("ok")]

    if not reachable and not control_dns_ok:
        verdict = (
            "НЕ РЕЗОЛВЯТСЯ НИ БИРЖИ, НИ КОНТРОЛЬНЫЕ ХОСТЫ — проблема исходящей "
            "сети/DNS всего инстанса, а не OKX. Менять OKX_API_HOSTNAME бесполезно; "
            "смотреть сторону Render (egress, DNS-резолвер)"
        )
    elif not reachable and control_dns_ok:
        verdict = (
            "Контрольные хосты живы, а хосты OKX — нет: проблема адресная. "
            "Это либо блокировка OKX для этого ДЦ, либо смена эндпоинта — "
            "нужен OKX_PROXY_URL или другой OKX_API_HOSTNAME"
        )
    else:
        verdict = "Есть доступный хост OKX — см. recommended_hostname"

    return {
        "status": "ok",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "verdict": verdict,
        "any_host_reachable": bool(reachable),
        "reachable_hosts": [r["host"] for r in reachable],
        "recommended_hostname": reachable[0]["host"] if reachable else None,
        "proxy_configured": bool(str(getattr(settings, "OKX_PROXY_URL", "") or "").strip()),
        "circuit": OKXClient.circuit_state(),
        "hosts": results,
        "control_hosts": control_results,
        "note": (
            "Публичный /api/v5/public/time не требует API-ключа: если он не "
            "отвечает, статус ключа значения не имеет. Если доступен НЕ основной "
            "хост — переставь OKX_API_HOSTNAME на него. Если недоступны все и "
            "виден 403/451 — нужен OKX_PROXY_URL через разрешённый регион."
        ),
    }


def diagnose_all(timeout: float | None = None) -> dict[str, Any]:
    """(#okx-satellite-2026-09-02) Обе биржи одним вызовом — health/venues
    страницы показывают карточку HTX и карточку OKX независимо от того, какая
    сейчас активна (settings.active_exchange)."""
    return {
        "active_exchange": settings.active_exchange,
        "htx": diagnose(timeout),
        "okx": diagnose_okx(timeout),
    }
