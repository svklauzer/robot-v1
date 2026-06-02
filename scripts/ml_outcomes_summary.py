#!/usr/bin/env python3
"""Build a compact JSON summary for storage/ml/trade_outcomes.jsonl.

The 24h collection script uses this file instead of an inline here-doc so the
ML outcomes report fails loudly in tests and does not degrade to
`ml_summary_python_failed` because of shell syntax issues.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


def _root_dir() -> Path:
    if len(sys.argv) > 1:
        return Path(sys.argv[1]).resolve()
    return Path(os.environ.get("ROOT_DIR", ".")).resolve()


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _freshness(rows: list[dict], stale_hours: int = 72) -> dict:
    latest = None
    for row in rows:
        logged_at = _parse_datetime(row.get("logged_at"))
        if logged_at is not None and (latest is None or logged_at > latest):
            latest = logged_at

    if latest is None:
        return {
            "latest_logged_at": None,
            "latest_age_hours": None,
            "latest_age_days": None,
            "stale": False,
            "freshness_status": "empty" if not rows else "missing_logged_at",
            "stale_after_hours": stale_hours,
        }

    age_hours = max(round((datetime.now(timezone.utc) - latest).total_seconds() / 3600, 2), 0.0)
    stale = age_hours > stale_hours
    return {
        "latest_logged_at": latest.isoformat(),
        "latest_age_hours": age_hours,
        "latest_age_days": round(age_hours / 24, 2),
        "stale": stale,
        "freshness_status": "stale" if stale else "fresh",
        "stale_after_hours": stale_hours,
    }


def build_summary(root: Path) -> dict:
    path = root / "storage/ml/trade_outcomes.jsonl"

    if not path.exists():
        return {
            "status": "empty",
            "reason": "trade_outcomes_file_missing",
            "source_path": str(path),
            "total_rows": 0,
            "closed_rows": 0,
            **_freshness([]),
        }

    rows: list[dict] = []
    parse_errors = 0

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            if isinstance(payload, dict):
                rows.append(payload)
            else:
                parse_errors += 1
        except Exception:
            parse_errors += 1

    closed = [row for row in rows if str(row.get("status")) == "closed"]
    freshness = _freshness(rows)
    result = {
        "status": "stale" if freshness["stale"] else ("ok" if rows else "empty"),
        "source_path": str(path),
        "total_rows": len(rows),
        "closed_rows": len(closed),
        "parse_errors": parse_errors,
        **freshness,
    }

    if not closed:
        return result

    pnl = [float(row.get("closed_net_pnl") or 0.0) for row in closed]
    wins = sum(1 for value in pnl if value > 0)
    losses = sum(1 for value in pnl if value <= 0)
    result.update(
        {
            "net_pnl_sum": round(sum(pnl), 6),
            "winrate_pct": round(wins / len(closed) * 100, 2),
            "wins": wins,
            "losses": losses,
        }
    )

    reason = Counter(str(row.get("closed_reason") or "unknown") for row in closed)
    result["closed_reason_top"] = reason.most_common(10)

    symbol_pnl = defaultdict(lambda: {"count": 0, "pnl": 0.0})
    for row in closed:
        symbol = str(row.get("symbol") or "unknown")
        symbol_pnl[symbol]["count"] += 1
        symbol_pnl[symbol]["pnl"] += float(row.get("closed_net_pnl") or 0.0)

    result["symbol_pnl"] = sorted(
        [
            {"symbol": symbol, "count": data["count"], "net_pnl": round(data["pnl"], 6)}
            for symbol, data in symbol_pnl.items()
        ],
        key=lambda item: item["net_pnl"],
    )[:12]

    return result


def main() -> int:
    print(json.dumps(build_summary(_root_dir()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
