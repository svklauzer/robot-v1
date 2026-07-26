"""Предохранитель исходящей сети (#egress-guard-2026-07-26).

Инцидент 26.07, второй раунд логов показал то, что не было видно раньше:

  18:05:39  старт uvicorn ("running on 0.0.0.0:10000")
  18:06:21  ==> No open HTTP ports detected on 0.0.0.0
  18:06:44  htx_retry attempt 1
  18:07:47  attempt 2   (+63с)
  18:08:52  attempt 3   (+65с)
  18:10:59  ==> Port scan timeout reached — Render сдался

Два вывода:

1. **Порт слушается, но Render не может подключиться.** accept() не выполняется,
   потому что event loop заблокирован ещё до первого соединения. Это уже не
   «health-check упал», это «сервис не поднялся».

2. **65 секунд между попытками при таймауте ccxt 15с.** Столько не может уйти в
   HTTP. Время уходит в `getaddrinfo` — DNS-резолв синхронный, идёт в libc и
   **не подчиняется** socket/requests timeout. Настройки resolv.conf
   (timeout × attempts × число серверов) дают десятки секунд, и ccxt тут бессилен.

Плюс в тех же логах лежит и Kraken (`kraken_retry` на futures.kraken.com) —
две независимые биржи одновременно означают проблему исходящей сети/DNS
на стороне ДЦ, а не проблему конкретной биржи.

Решение: резолвим хост в ОТДЕЛЬНОМ потоке с жёстким таймаутом. Поток может
висеть в libc сколько угодно — мы его просто бросаем и получаем ответ вовремя.
Результат кэшируется, чтобы не платить за проверку на каждом вызове.
"""
from __future__ import annotations

import logging
import socket
import time
import threading

from core.config import settings
from core.logging import get_logger, log_event

logger = get_logger(__name__)

# host -> (ok, checked_at)
_cache: dict[str, tuple[bool, float]] = {}


def _blocking_resolve(host: str) -> bool:
    socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
    return True


def _resolve_with_deadline(host: str, timeout: float) -> tuple[bool, bool]:
    """(ok, timed_out). Резолв уходит в DAEMON-поток.

    Daemon важен: зависший в libc getaddrinfo нельзя прервать, и обычный поток
    задержал бы остановку процесса (Python джойнит не-daemon потоки на выходе).
    Для сервиса, который Render перезапускает по SIGTERM, это означало бы
    зависание при shutdown. Daemon-поток просто умирает вместе с процессом.
    """
    result: dict[str, bool] = {}
    done = threading.Event()

    def _worker() -> None:
        try:
            result["ok"] = _blocking_resolve(host)
        except Exception:  # noqa: BLE001
            result["ok"] = False
        finally:
            done.set()

    threading.Thread(target=_worker, name=f"dns-probe-{host}", daemon=True).start()
    if not done.wait(timeout):
        return False, True
    return bool(result.get("ok")), False


def resolve_ok(host: str, *, timeout: float | None = None, ttl: float | None = None) -> bool:
    """Резолвится ли хост за отведённое время.

    Никогда не блокирует дольше `timeout`, даже если libc висит минуту.
    fail-open по смыслу «не знаем» здесь НЕ применяется: если резолв не успел,
    сеть считается недоступной — именно это и защищает event loop.
    """
    if not bool(getattr(settings, "EGRESS_GUARD_ENABLED", True)):
        return True

    host = str(host or "").strip()
    if not host:
        return True

    timeout = float(timeout if timeout is not None else getattr(settings, "EGRESS_DNS_TIMEOUT_SEC", 3.0))
    ttl = float(ttl if ttl is not None else getattr(settings, "EGRESS_CACHE_TTL_SEC", 30.0))

    now = time.time()
    cached = _cache.get(host)
    if cached and (now - cached[1]) < ttl:
        return cached[0]

    started = now
    ok, timed_out = _resolve_with_deadline(host, timeout)

    if timed_out:
        log_event(
            logger,
            logging.WARNING,
            "egress_dns_timeout",
            host=host,
            timeout_sec=timeout,
            note="DNS не ответил вовремя — getaddrinfo не подчиняется таймауту ccxt, "
                 "поэтому проверяем его отдельно и не пускаем блокировку в event loop",
        )
    elif not ok:
        log_event(logger, logging.WARNING, "egress_dns_failed", host=host)
    else:
        elapsed = time.time() - started
        if elapsed > 1.0:
            log_event(logger, logging.WARNING, "egress_dns_slow", host=host, seconds=round(elapsed, 2))

    _cache[host] = (ok, time.time())
    return ok


def state() -> dict:
    """Снимок для /system/health: что и когда проверяли."""
    now = time.time()
    return {
        "enabled": bool(getattr(settings, "EGRESS_GUARD_ENABLED", True)),
        "dns_timeout_sec": float(getattr(settings, "EGRESS_DNS_TIMEOUT_SEC", 3.0)),
        "hosts": {
            host: {"resolves": ok, "checked_sec_ago": round(now - ts, 1)}
            for host, (ok, ts) in sorted(_cache.items())
        },
    }


def reset_cache() -> None:
    _cache.clear()
