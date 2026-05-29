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
PYTHON_BIN_ARGS=()

cmd_out() {
  local name="$1"; shift
  {
    echo "# CMD: $*"
    "$@"
  } >"$RUN_DIR/${name}.txt" 2>&1 || true
}

append_limited() {
  local out_file="$1"
  local title="$2"
  local src_file="$3"
  local max_lines="${4:-120}"
  {
    echo "### $title"
    if [[ -f "$src_file" ]]; then
      sed -n "1,${max_lines}p" "$src_file"
      local total_lines
      total_lines="$(wc -l < "$src_file" 2>/dev/null || echo 0)"
      if [[ "${total_lines:-0}" -gt "$max_lines" ]]; then
        echo
        echo "... truncated: showing first $max_lines of $total_lines lines from $(basename "$src_file")"
      fi
    else
      echo "(missing: $src_file)"
    fi
    echo
  } >> "$out_file"
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
curl_json ml_outcomes_api_summary "$API_URL/ml/outcomes/summary"

# --- ML outcomes raw excerpt ---
if [[ -f "$ROOT_DIR/storage/ml/trade_outcomes.jsonl" ]]; then
  cmd_out ml_outcomes_wc wc -l "$ROOT_DIR/storage/ml/trade_outcomes.jsonl"
  cmd_out ml_outcomes_tail tail -n 120 "$ROOT_DIR/storage/ml/trade_outcomes.jsonl"

  if [[ -z "$PYTHON_BIN" ]]; then
    if command -v python3 >/dev/null 2>&1; then
      PYTHON_BIN="python3"
    elif command -v python >/dev/null 2>&1; then
      PYTHON_BIN="python"
    elif command -v py >/dev/null 2>&1; then
      PYTHON_BIN="py"
      PYTHON_BIN_ARGS=("-3")
    else
      PYTHON_BIN=""
    fi
  fi

  if [[ -n "$PYTHON_BIN" ]]; then
    if ! ROOT_DIR="$ROOT_DIR" "$PYTHON_BIN" "${PYTHON_BIN_ARGS[@]}" "$ROOT_DIR/scripts/ml_outcomes_summary.py" > "$RUN_DIR/ml_outcomes_summary.json"; then
      echo "{\"status\": \"degraded\", \"fallback_used\": false, \"reason\": \"ml_summary_python_failed\", \"python_bin\": \"$PYTHON_BIN\"}" > "$RUN_DIR/ml_outcomes_summary.json"
    fi
  else
    if command -v jq >/dev/null 2>&1; then
      jq -s '
        . as $rows |
        map(select(type=="object" and .status=="closed")) as $closed |
        {
          status: "degraded",
          fallback_used: true,
          total_rows: ($rows|length),
          closed_rows: ($closed|length),
          wins: ($closed|map(select((.closed_net_pnl // 0) > 0))|length),
          losses: ($closed|map(select((.closed_net_pnl // 0) <= 0))|length)
        }' "$ROOT_DIR/storage/ml/trade_outcomes.jsonl" > "$RUN_DIR/ml_outcomes_summary.json" 2>/dev/null || \
      echo "{\"status\":\"degraded\",\"fallback_used\":true,\"reason\":\"jq_fallback_failed\"}" > "$RUN_DIR/ml_outcomes_summary.json"
    else
      echo "{\"status\": \"degraded\", \"fallback_used\": false, \"reason\": \"python_not_found\", \"hint\": \"Set PYTHON_BIN=python or install python3\"}" > "$RUN_DIR/ml_outcomes_summary.json"
    fi
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
echo "$RUN_DIR" > "$OUT_DIR/latest_run_dir.txt"

# --- LLM-friendly report (bounded size for chat limits) ---
CHAT_REPORT="$RUN_DIR/_report_for_chat.md"
{
  echo "# Robot run report (LLM compact)"
  echo
  echo "- run_dir: $RUN_DIR"
  echo "- utc_ts: $TS"
  echo
  echo "## files"
  find "$RUN_DIR" -maxdepth 1 -type f -printf '%f\n' | sort
  echo
} > "$CHAT_REPORT"

append_limited "$CHAT_REPORT" "git_status.txt" "$RUN_DIR/git_status.txt" 80
append_limited "$CHAT_REPORT" "compose_ps.txt" "$RUN_DIR/compose_ps.txt" 120
append_limited "$CHAT_REPORT" "health.json" "$RUN_DIR/health.json" 120
append_limited "$CHAT_REPORT" "analytics_summary.json" "$RUN_DIR/analytics_summary.json" 200
append_limited "$CHAT_REPORT" "analytics_reason_breakdown.json" "$RUN_DIR/analytics_reason_breakdown.json" 200
append_limited "$CHAT_REPORT" "analytics_signal_quality.json" "$RUN_DIR/analytics_signal_quality.json" 200
append_limited "$CHAT_REPORT" "ml_outcomes_summary.json" "$RUN_DIR/ml_outcomes_summary.json" 220
append_limited "$CHAT_REPORT" "compose_logs_api_tail.txt" "$RUN_DIR/compose_logs_api_tail.txt" 220
append_limited "$CHAT_REPORT" "compose_logs_web_tail.txt" "$RUN_DIR/compose_logs_web_tail.txt" 160

cp "$CHAT_REPORT" "$OUT_DIR/latest_report_for_chat.md"

echo "Saved report to: $RUN_DIR"
echo "Compact file:    $RUN_DIR/_report_compact.txt"
echo "Latest compact:  $OUT_DIR/latest_report_compact.txt"
echo "Chat report:     $RUN_DIR/_report_for_chat.md"
echo "Latest chat:     $OUT_DIR/latest_report_for_chat.md"
