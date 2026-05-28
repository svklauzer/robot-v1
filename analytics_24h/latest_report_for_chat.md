# Robot run report (LLM compact)

- run_dir: /workspace/robot-v1/analytics_24h/run_20260528T124709Z
- utc_ts: 20260528T124709Z

## files
_report_compact.txt
_report_for_chat.md
analytics_reason_breakdown.json
analytics_signal_quality.json
analytics_summary.json
bot_state.json
compose_logs_api_tail.txt
compose_logs_web_tail.txt
compose_ps.txt
git_branch.txt
git_meta.txt
git_status.txt
health.json
intelligence_events.json
loop_state.json
ml_outcomes_summary.json
ml_outcomes_tail.txt
ml_outcomes_wc.txt
positions_latest.json
signals_latest.json
utc_time.txt

### git_status.txt
# CMD: git status --short --branch
## work
 M scripts/collect_run_report.sh
?? analytics_24h/run_20260528T124709Z/
?? apps/web/node_modules/

### compose_ps.txt
# CMD: docker compose ps
./scripts/collect_run_report.sh: line 19: docker: command not found

### health.json
# GET http://localhost:8000/health
curl: (7) Failed to connect to localhost port 8000 after 0 ms: Couldn't connect to server

### analytics_summary.json
# GET http://localhost:8000/analytics/summary
curl: (7) Failed to connect to localhost port 8000 after 0 ms: Couldn't connect to server

### analytics_reason_breakdown.json
# GET http://localhost:8000/analytics/reason-breakdown
curl: (7) Failed to connect to localhost port 8000 after 0 ms: Couldn't connect to server

### analytics_signal_quality.json
# GET http://localhost:8000/analytics/signal-quality
curl: (7) Failed to connect to localhost port 8000 after 0 ms: Couldn't connect to server

### ml_outcomes_summary.json
{
  "total_rows": 90,
  "closed_rows": 90,
  "net_pnl_sum": -96.208143,
  "winrate_pct": 45.56,
  "wins": 41,
  "losses": 49,
  "closed_reason_top": [
    [
      "failed_setup_exit",
      47
    ],
    [
      "protective_breakeven_profit_guard",
      37
    ],
    [
      "adaptive_trailing_stop",
      3
    ],
    [
      "stop_loss",
      2
    ],
    [
      "tp2_reached",
      1
    ]
  ],
  "symbol_pnl": [
    {
      "symbol": "SOL/USDT",
      "count": 9,
      "net_pnl": -17.681298
    },
    {
      "symbol": "AVAX/USDT",
      "count": 8,
      "net_pnl": -14.035086
    },
    {
      "symbol": "ETH/USDT",
      "count": 5,
      "net_pnl": -10.686827
    },
    {
      "symbol": "XRP/USDT",
      "count": 7,
      "net_pnl": -10.46274
    },
    {
      "symbol": "TON/USDT",
      "count": 23,
      "net_pnl": -9.55633
    },
    {
      "symbol": "LINK/USDT",
      "count": 12,
      "net_pnl": -9.491556
    },
    {
      "symbol": "DOT/USDT",
      "count": 14,
      "net_pnl": -8.615151
    },
    {
      "symbol": "BTC/USDT",
      "count": 8,
      "net_pnl": -8.12691
    },
    {
      "symbol": "DOGE/USDT",
      "count": 2,
      "net_pnl": -2.942533
    },
    {
      "symbol": "ADA/USDT",
      "count": 1,
      "net_pnl": -2.690775
    },
    {
      "symbol": "LTC/USDT",
      "count": 1,
      "net_pnl": -1.918937
    }
  ]
}

### compose_logs_api_tail.txt
# CMD: bash -lc docker compose logs --since=8h api | tail -n 2000
bash: command not found: docker

### compose_logs_web_tail.txt
# CMD: bash -lc docker compose logs --since=8h web | tail -n 600
bash: command not found: docker

