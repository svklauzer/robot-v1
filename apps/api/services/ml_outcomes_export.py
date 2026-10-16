"""Выгрузка trade_outcomes.jsonl для разбора вне сервера (#outcomes-download-2026-10-17)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

_HEAVY = ("traj", "traj_step")


def iter_slim_lines(path: Path) -> Iterator[bytes]:
    """Строки файла без траектории (lifecycle.traj, traj_step) — в разы легче.

    Битая или недописанная (идёт запись) строка пропускается, а не роняет выгрузку.
    """
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            lc = row.get("lifecycle")
            if isinstance(lc, dict):
                row["lifecycle"] = {k: v for k, v in lc.items() if k not in _HEAVY}
            yield (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
