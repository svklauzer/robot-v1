"""Сколько памяти занимает процесс и контейнер (#memory-probe-2026-09-16).

Повод. 16.09 Render прислал письмо: robot-api превысил лимит памяти и был
перезапущен. В логах приложения при этом чисто — и это ожидаемо: при нехватке
памяти процесс убивает ядро, у Python нет шанса ничего записать. Своего замера
памяти в системе не было вовсе, поэтому отличить утечку (плавный рост) от
разового пика (тяжёлый запрос) было нечем.

Замер локально 16.09: импорт приложения ~220 МБ, рынки OKX +55 МБ (4443 рынка,
в работе спот и свопы), HTX +20 МБ — около 300 МБ из 512 до всякой работы.

Что читается
------------
  rss_mb / rss_peak_mb   процесс: VmRSS и VmHWM из /proc/self/status;
  container_*            cgroup контейнера — по нему Render и решает, кого
                         убить: memory.current / memory.max / memory.peak (v2)
                         или memory.usage_in_bytes / limit_in_bytes /
                         max_usage_in_bytes (v1). В контейнер входит и файловый
                         кеш, поэтому он больше RSS.

Только чтение нескольких маленьких файлов: микросекунды, без сети и без
зависимостей. Где файлов нет (Windows, не контейнер) — поле None.
"""
from __future__ import annotations

from pathlib import Path

MB = 1024 * 1024
# Значение memory.max без ограничения: "max" (v2) или огромное число (v1).
_UNLIMITED_BYTES = 1 << 60


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None


def _bytes(path: str) -> int | None:
    raw = _read(path)
    if raw is None or raw == "max":
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value < _UNLIMITED_BYTES else None


def _proc_status_kb(field: str, text: str | None) -> int | None:
    for line in (text or "").splitlines():
        if line.startswith(field + ":"):
            parts = line.split()
            try:
                return int(parts[1])
            except (IndexError, ValueError):
                return None
    return None


def _mb(value_bytes: int | None) -> float | None:
    return None if value_bytes is None else round(value_bytes / MB, 1)


def read_memory() -> dict:
    status = _read("/proc/self/status")
    rss_kb = _proc_status_kb("VmRSS", status)
    hwm_kb = _proc_status_kb("VmHWM", status)

    used = _bytes("/sys/fs/cgroup/memory.current")
    if used is not None:
        limit = _bytes("/sys/fs/cgroup/memory.max")
        peak = _bytes("/sys/fs/cgroup/memory.peak")
    else:
        used = _bytes("/sys/fs/cgroup/memory/memory.usage_in_bytes")
        limit = _bytes("/sys/fs/cgroup/memory/memory.limit_in_bytes")
        peak = _bytes("/sys/fs/cgroup/memory/memory.max_usage_in_bytes")

    return {
        "rss_mb": None if rss_kb is None else round(rss_kb / 1024, 1),
        "rss_peak_mb": None if hwm_kb is None else round(hwm_kb / 1024, 1),
        "container_used_mb": _mb(used),
        "container_limit_mb": _mb(limit),
        "container_peak_mb": _mb(peak),
        "container_used_share": (round(used / limit, 3) if used is not None and limit else None),
    }
