#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$ROOT_DIR/analytics_24h"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="$OUT_DIR/run_$TS"
mkdir -p "$RUN_DIR"

API_URL="${API_URL:-http://localhost:8000}"
COMPOSE_BIN="${COMPOSE_BIN:-docker compose}"
PYTHON_BIN="${PYTHON_BIN:-}"

cmd_out() {
  local name="$1"; shift
  {
    echo "# CMD: $*"
    "$@"
  } >"$RUN_DIR/${name}.txt" 2>&1 || true
}

curl_json() {
  local name="$1"; shift
  local url="$1"
  {
    echo "# GET $url"
    curl -sS "$url"
  } >"$RUN_DIR/${name}.json" 2>&1 || true
}

# --- Meta / git ---
cmd_out git_meta git rev-parse --short HEAD
cmd_out git_branch git branch --show-current
cmd_out git_status git status --short --branch
cmd_out utc_time date -u

# --- Docker / runtime ---
cmd_out compose_ps $COMPOSE_BIN ps
cmd_out compose_logs_api_tail bash -lc "$COMPOSE_BIN logs --since=8h api | tail -n 2000"
cmd_out compose_logs_web_tail bash -lc "$COMPOSE_BIN logs --since=8h web | tail -n 600"

# --- API endpoints ---
curl_json health "$API_URL/health"
curl_json bot_state "$API_URL/bot/state"
curl_json loop_state "$API_URL/robot/loop-state"
curl_json signals_latest "$API_URL/signals?limit=200"
curl_json positions_latest "$API_URL/positions?limit=200"
curl_json analytics_summary "$API_URL/analytics/summary"
curl_json analytics_reason_breakdown "$API_URL/analytics/reason-breakdown"
curl_json analytics_signal_quality "$API_URL/analytics/signal-quality"
curl_json intelligence_events "$API_URL/intelligence/events?limit=300"

# --- ML outcomes raw excerpt ---
if [[ -f "$ROOT_DIR/storage/ml/trade_outcomes.jsonl" ]]; then
  cmd_out ml_outcomes_wc wc -l "$ROOT_DIR/storage/ml/trade_outcomes.jsonl"
  cmd_out ml_outcomes_tail tail -n 120 "$ROOT_DIR/storage/ml/trade_outcomes.jsonl"

  if [[ -z "$PYTHON_BIN" ]]; then
    if command -v python3 >/dev/null 2>&1; then
      PYTHON_BIN="python3"
    elif command -v python >/dev/null 2>&1; then
      PYTHON_BIN="python"
    else
      PYTHON_BIN=""
    fi
  fi

  if [[ -n "$PYTHON_BIN" ]]; then
    if ! "$PYTHON_BIN" - <<'PY' >"$RUN_DIR/ml_outcomes_summary.json"; then

  python3 - <<'PY' >"$RUN_DIR/ml_outcomes_summary.json"

import json
from collections import Counter, defaultdict
from pathlib import Path

p = Path("storage/ml/trade_outcomes.jsonl")
rows = []
for line in p.read_text(encoding="utf-8").splitlines():
    line=line.strip()
    if not line:
        continue
    try:
        rows.append(json.loads(line))
    except Exception:
        pass

closed = [r for r in rows if str(r.get("status")) == "closed"]
res = {
  "total_rows": len(rows),
  "closed_rows": len(closed),
}
if closed:
  pnl = [float(r.get("closed_net_pnl") or 0.0) for r in closed]
  wins = sum(1 for x in pnl if x > 0)
  losses = sum(1 for x in pnl if x <= 0)
  res.update({
    "net_pnl_sum": round(sum(pnl), 6),
    "winrate_pct": round(wins / len(closed) * 100, 2),
    "wins": wins,
    "losses": losses,
  })
  reason = Counter(str(r.get("closed_reason") or "unknown") for r in closed)
  res["closed_reason_top"] = reason.most_common(10)

  sym=defaultdict(lambda: {"count":0,"pnl":0.0})
  for r in closed:
    s=str(r.get("symbol") or "unknown")
    sym[s]["count"] += 1
    sym[s]["pnl"] += float(r.get("closed_net_pnl") or 0.0)
  res["symbol_pnl"] = sorted(
    [{"symbol":k, "count":v["count"], "net_pnl":round(v["pnl"],6)} for k,v in sym.items()],
    key=lambda x: x["net_pnl"]
  )[:12]

print(json.dumps(res, ensure_ascii=False, indent=2))
PY
      echo "{\"status\": \"error\", \"reason\": \"ml_summary_python_failed\", \"python_bin\": \"$PYTHON_BIN\"}" > "$RUN_DIR/ml_outcomes_summary.json"
    fi
  else
    echo "{\"status\": \"error\", \"reason\": \"python_not_found\", \"hint\": \"Set PYTHON_BIN=python or install python3\"}" > "$RUN_DIR/ml_outcomes_summary.json"
  fi
fi

# --- Single compact bundle for chat/github ---
{
  echo "RUN_DIR=$RUN_DIR"
  echo "UTC_TS=$TS"
  echo
  echo "=== git_meta.txt ==="; cat "$RUN_DIR/git_meta.txt" 2>/dev/null || true
  echo "=== git_branch.txt ==="; cat "$RUN_DIR/git_branch.txt" 2>/dev/null || true
  echo "=== compose_ps.txt ==="; cat "$RUN_DIR/compose_ps.txt" 2>/dev/null || true
  echo "=== analytics_summary.json ==="; cat "$RUN_DIR/analytics_summary.json" 2>/dev/null || true
  echo "=== analytics_reason_breakdown.json ==="; cat "$RUN_DIR/analytics_reason_breakdown.json" 2>/dev/null || true
  echo "=== ml_outcomes_summary.json ==="; cat "$RUN_DIR/ml_outcomes_summary.json" 2>/dev/null || true
} > "$RUN_DIR/_report_compact.txt"

cp "$RUN_DIR/_report_compact.txt" "$OUT_DIR/latest_report_compact.txt"

echo "Saved report to: $RUN_DIR"
echo "Compact file:    $RUN_DIR/_report_compact.txt"
echo "Latest compact:  $OUT_DIR/latest_report_compact.txt"
