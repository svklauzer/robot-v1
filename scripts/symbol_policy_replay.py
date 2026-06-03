#!/usr/bin/env python3
"""Replay storage/ml/trade_outcomes.jsonl through current symbol policy rules."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _api_path(root: Path) -> Path:
    return root / "apps" / "api"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", help="Path to trade_outcomes.jsonl. Defaults to storage/ml/trade_outcomes.jsonl")
    parser.add_argument("--lookback", type=int, default=12)
    parser.add_argument("--sample-limit", type=int, default=25)
    args = parser.parse_args()

    root = _repo_root()
    sys.path.insert(0, str(_api_path(root)))

    from services.symbol_policy_replay import SymbolPolicyReplayService

    path = Path(args.path) if args.path else root / "storage" / "ml" / "trade_outcomes.jsonl"
    payload = SymbolPolicyReplayService().replay_path(path, lookback=args.lookback, sample_limit=args.sample_limit)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
