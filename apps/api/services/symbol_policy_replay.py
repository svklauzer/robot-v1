from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from services.symbol_performance_guard import SymbolPerformanceGuard


class _ReplayQuery:
    def __init__(self, rows: list[SimpleNamespace]):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, n: int):
        self._rows = self._rows[:n]
        return self

    def all(self):
        return self._rows


class _ReplayDB:
    def __init__(self, rows: list[SimpleNamespace]):
        self._rows = rows

    def query(self, _model):
        return _ReplayQuery(self._rows)


class SymbolPolicyReplayService:
    """Replay JSONL outcomes through the current per-symbol policy profiles.

    This is a lightweight paper/live-shadow backtest helper: it replays closed
    outcomes in file order and asks the symbol-performance guard what it would
    have allowed *before* each outcome was added to that symbol's rolling
    history. The result is not a market simulator; it is an audit of whether the
    current block/watch/tradeable policy would have avoided repeated bad symbols
    or missed profitable ones.
    """

    def __init__(self, guard: SymbolPerformanceGuard | None = None):
        self.guard = guard or SymbolPerformanceGuard()

    def replay_path(self, path: str | Path, lookback: int = 12, sample_limit: int = 25) -> dict[str, Any]:
        source = Path(path)
        if not source.exists():
            return {
                "status": "empty",
                "reason": "outcomes_file_missing",
                "source_path": str(source),
                "total_rows": 0,
                "closed_rows": 0,
                "parse_errors": 0,
            }

        rows, parse_errors = self._load_jsonl(source)
        return self.replay_rows(rows, lookback=lookback, sample_limit=sample_limit, source_path=str(source), parse_errors=parse_errors)

    def replay_rows(
        self,
        rows: list[dict[str, Any]],
        lookback: int = 12,
        sample_limit: int = 25,
        source_path: str | None = None,
        parse_errors: int = 0,
    ) -> dict[str, Any]:
        lookback = min(max(int(lookback or 12), 1), 100)
        sample_limit = max(int(sample_limit or 0), 0)
        closed_rows = [row for row in rows if str(row.get("status") or "") == "closed"]

        history_by_symbol: dict[str, list[SimpleNamespace]] = defaultdict(list)
        profile_counts: Counter[str] = Counter()
        reason_counts: Counter[str] = Counter()
        skipped_by_reason: Counter[str] = Counter()
        samples: list[dict[str, Any]] = []

        baseline_net_pnl = 0.0
        replay_net_pnl = 0.0
        published_count = 0
        skipped_count = 0
        avoided_loss = 0.0
        missed_profit = 0.0

        for row in closed_rows:
            symbol = self._symbol(row)
            net_pnl = self._float(row.get("closed_net_pnl"))
            baseline_net_pnl += net_pnl

            history = history_by_symbol[symbol]
            decision = self.guard.analyze(_ReplayDB(history[:lookback]), bot_id=int(row.get("bot_id") or 1), symbol=symbol, lookback=lookback)
            profile = self.guard.policy_profile(decision)
            would_publish = bool(profile.get("publish_allowed", True))

            profile_name = str(profile.get("profile") or "unknown")
            profile_counts[profile_name] += 1
            reason_counts[decision.reason] += 1

            if would_publish:
                published_count += 1
                replay_net_pnl += net_pnl
            else:
                skipped_count += 1
                skipped_by_reason[decision.reason] += 1
                if net_pnl < 0:
                    avoided_loss += abs(net_pnl)
                elif net_pnl > 0:
                    missed_profit += net_pnl

            if len(samples) < sample_limit:
                samples.append(
                    {
                        "signal_id": row.get("signal_id"),
                        "symbol": symbol,
                        "side": row.get("side"),
                        "closed_reason": row.get("closed_reason"),
                        "net_pnl": round(net_pnl, 6),
                        "would_publish": would_publish,
                        "profile": profile_name,
                        "policy_reason": decision.reason,
                        "history_count_before": len(history),
                    }
                )

            history.insert(0, self._row_to_signal(row, symbol=symbol))

        return {
            "status": "ok" if closed_rows else "empty",
            "source_path": source_path,
            "total_rows": len(rows),
            "closed_rows": len(closed_rows),
            "parse_errors": parse_errors,
            "lookback": lookback,
            "baseline": {
                "published_count": len(closed_rows),
                "net_pnl": round(baseline_net_pnl, 6),
            },
            "replay": {
                "published_count": published_count,
                "skipped_count": skipped_count,
                "net_pnl": round(replay_net_pnl, 6),
                "net_pnl_delta": round(replay_net_pnl - baseline_net_pnl, 6),
                "avoided_loss_usdt": round(avoided_loss, 6),
                "missed_profit_usdt": round(missed_profit, 6),
            },
            "profiles": dict(profile_counts),
            "reasons": dict(reason_counts),
            "skipped_by_reason": dict(skipped_by_reason),
            "samples": samples,
        }

    def _load_jsonl(self, path: Path) -> tuple[list[dict[str, Any]], int]:
        rows: list[dict[str, Any]] = []
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
        return rows, parse_errors

    def _row_to_signal(self, row: dict[str, Any], symbol: str) -> SimpleNamespace:
        lifecycle = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
        labels = row.get("labels") if isinstance(row.get("labels"), dict) else {}
        positive_then_negative = bool(lifecycle.get("positive_then_negative") or labels.get("positive_then_negative"))
        return SimpleNamespace(
            symbol=symbol,
            status="closed",
            closed_net_pnl=self._float(row.get("closed_net_pnl")),
            closed_reason=str(row.get("closed_reason") or "unknown"),
            plan_json={"lifecycle": {"positive_then_negative": positive_then_negative}},
        )

    def _symbol(self, row: dict[str, Any]) -> str:
        return str(row.get("symbol") or "UNKNOWN").strip().upper() or "UNKNOWN"

    def _float(self, value: object) -> float:
        try:
            return float(value or 0.0)
        except Exception:
            return 0.0
