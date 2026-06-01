# Robot run report (LLM compact)

- run_dir: /c/Users/svk/robot-v1/analytics_24h/run_20260528T143353Z
- utc_ts: 20260528T143353Z

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
## main...origin/main [behind 2]
?? analytics_24h/run_20260528T133826Z/
?? analytics_24h/run_20260528T143353Z/

### compose_ps.txt
# CMD: docker compose ps
NAME               IMAGE          COMMAND                  SERVICE   CREATED          STATUS          PORTS
robot-v1-api-1     robot-v1-api   "uvicorn main:app --…"   api       17 minutes ago   Up 17 minutes   0.0.0.0:8000->8000/tcp, [::]:8000->8000/tcp
robot-v1-db-1      postgres:16    "docker-entrypoint.s…"   db        17 minutes ago   Up 17 minutes   0.0.0.0:5432->5432/tcp, [::]:5432->5432/tcp
robot-v1-redis-1   redis:7        "docker-entrypoint.s…"   redis     17 minutes ago   Up 17 minutes   0.0.0.0:6379->6379/tcp, [::]:6379->6379/tcp
robot-v1-web-1     robot-v1-web   "docker-entrypoint.s…"   web       17 minutes ago   Up 17 minutes   0.0.0.0:3000->3000/tcp, [::]:3000->3000/tcp

### health.json
# GET http://localhost:8000/health
{"api":"ok","env":"development"}
### analytics_summary.json
# GET http://localhost:8000/analytics/summary
{"total_signals":39,"active_signals":0,"closed_signals":36,"expired_signals":3,"rejected_signals":0,"wins":11,"losses":25,"winrate":30.56,"total_result_pct":-19.5279,"total_net_pnl_usdt":-24.159891,"avg_net_pnl_usdt":-0.671108,"total_costs_usdt":22.053502,"exposure":{"used_margin":0.0,"max_allowed_margin":760.0,"free_margin":760.0,"active_signals_count":0}}
### analytics_reason_breakdown.json
# GET http://localhost:8000/analytics/reason-breakdown
{"status":"ok","sample_closed_signals":36,"total_net_pnl_usdt":-24.159891,"items":[{"reason":"failed_setup_exit","count":25,"share_pct":69.44,"wins":0,"losses":25,"avg_result_pct":-0.8034,"sum_net_pnl_usdt":-25.05804,"avg_net_pnl_usdt":-1.002322,"sum_costs_usdt":14.021797,"pnl_share_pct":103.72},{"reason":"protective_trailing_stop","count":1,"share_pct":2.78,"wins":1,"losses":0,"avg_result_pct":0.051,"sum_net_pnl_usdt":0.028371,"avg_net_pnl_usdt":0.028371,"sum_costs_usdt":0.249772,"pnl_share_pct":-0.12},{"reason":"protective_breakeven_profit_guard","count":10,"share_pct":27.78,"wins":10,"losses":0,"avg_result_pct":0.0506,"sum_net_pnl_usdt":0.869778,"avg_net_pnl_usdt":0.086978,"sum_costs_usdt":7.781933,"pnl_share_pct":-3.6}]}
### analytics_signal_quality.json
# GET http://localhost:8000/analytics/signal-quality
{"status":"ok","total_closed":36,"lifecycle_count":36,"legacy_count":0,"only_lifecycle":false,"went_positive":28,"positive_then_negative":17,"positive_then_negative_rate":47.22,"stop_loss_count":0,"breakeven_count":0,"trailing_count":1,"post_tp1_stop_count":0,"tp2_count":0,"tp2_rate":0.0,"trailing_rate":2.78,"post_tp1_stop_rate":0.0,"avg_mfe_pct":0.2505,"avg_mae_pct":-0.2637,"avg_missed_profit_pct":0.793,"avg_result_pct":-0.5424,"avg_net_pnl_usdt":-0.6711,"avg_costs_usdt":0.6126,"total_net_pnl_usdt":-24.159891,"total_costs_usdt":22.053502,"by_reason":{"protective_breakeven_profit_guard":10,"failed_setup_exit":25,"protective_trailing_stop":1},"by_reason_money":{"protective_breakeven_profit_guard":{"count":10,"net_pnl":0.869778,"costs":7.781933,"avg_result_pct":0.0506},"failed_setup_exit":{"count":25,"net_pnl":-25.05804,"costs":14.021797,"avg_result_pct":-0.8034},"protective_trailing_stop":{"count":1,"net_pnl":0.028371,"costs":0.249772,"avg_result_pct":0.051}},"items":[{"id":40,"symbol":"ETH/USDT","side":"short","grade":"A+","status":"closed","result_pct":0.051,"closed_reason":"protective_breakeven_profit_guard","closed_net_pnl":0.043663,"closed_total_cost":0.384408,"has_lifecycle":true,"mfe_pct":0.3802,"mae_pct":-0.225,"missed_profit_pct":0.3292,"positive_then_negative":false,"entry_price":1991.03,"max_profit_price":1983.46,"max_drawdown_price":1995.51,"exit_price":1981.07485,"close_reason":"protective_breakeven_profit_guard"},{"id":39,"symbol":"LTC/USDT","side":"short","grade":"A+","status":"closed","result_pct":-0.7856,"closed_reason":"failed_setup_exit","closed_net_pnl":-1.097902,"closed_total_cost":0.62979,"has_lifecycle":true,"mfe_pct":0.0,"mae_pct":-0.335,"missed_profit_pct":0.7856,"positive_then_negative":false,"entry_price":50.75,"max_profit_price":50.75,"max_drawdown_price":50.92,"exit_price":50.92,"close_reason":"failed_setup_exit"},{"id":37,"symbol":"TON/USDT","side":"short","grade":"A+","status":"closed","result_pct":0.051,"closed_reason":"protective_breakeven_profit_guard","closed_net_pnl":0.019656,"closed_total_cost":0.17305,"has_lifecycle":true,"mfe_pct":0.3101,"mae_pct":-0.1128,"missed_profit_pct":0.2591,"positive_then_negative":false,"entry_price":1.7738,"max_profit_price":1.7683,"max_drawdown_price":1.7758,"exit_price":1.764931,"close_reason":"protective_breakeven_profit_guard"},{"id":36,"symbol":"SUI/USDT","side":"short","grade":"A","status":"closed","result_pct":0.051,"closed_reason":"protective_breakeven_profit_guard","closed_net_pnl":0.033274,"closed_total_cost":0.29294,"has_lifecycle":true,"mfe_pct":0.3722,"mae_pct":0.0,"missed_profit_pct":0.3212,"positive_then_negative":false,"entry_price":0.9134,"max_profit_price":0.91,"max_drawdown_price":0.9134,"exit_price":0.908833,"close_reason":"protective_breakeven_profit_guard"},{"id":35,"symbol":"AVAX/USDT","side":"short","grade":"B","status":"closed","result_pct":-0.7804,"closed_reason":"failed_setup_exit","closed_net_pnl":-0.708495,"closed_total_cost":0.409115,"has_lifecycle":true,"mfe_pct":0.0714,"mae_pct":-0.3298,"missed_profit_pct":0.8518,"positive_then_negative":true,"entry_price":8.824,"max_profit_price":8.8177,"max_drawdown_price":8.8531,"exit_price":8.8531,"close_reason":"failed_setup_exit"},{"id":34,"symbol":"ADA/USDT","side":"short","grade":"A","status":"closed","result_pct":-0.8516,"closed_reason":"failed_setup_exit","closed_net_pnl":-0.705204,"closed_total_cost":0.373312,"has_lifecycle":true,"mfe_pct":0.0,"mae_pct":-0.4008,"missed_profit_pct":0.8516,"positive_then_negative":false,"entry_price":0.2298,"max_profit_price":0.2298,"max_drawdown_price":0.230721,"exit_price":0.230721,"close_reason":"failed_setup_exit"},{"id":32,"symbol":"TON/USDT","side":"short","grade":"A+","status":"closed","result_pct":-0.8438,"closed_reason":"failed_setup_exit","closed_net_pnl":-0.587965,"closed_total_cost":0.314098,"has_lifecycle":true,"mfe_pct":0.0775,"mae_pct":-0.393,"missed_profit_pct":0.9213,"positive_then_negative":true,"entry_price":1.8064,"max_profit_price":1.805,"max_drawdown_price":1.8135,"exit_price":1.8135,"close_reason":"failed_setup_exit"},{"id":30,"symbol":"TON/USDT","side":"short","grade":"B","status":"closed","result_pct":0.051,"closed_reason":"protective_trailing_stop","closed_net_pnl":0.028371,"closed_total_cost":0.249772,"has_lifecycle":true,"mfe_pct":1.2562,"mae_pct":0.0,"missed_profit_pct":1.2052,"positive_then_negative":false,"entry_price":1.9025,"max_profit_price":1.8786,"max_drawdown_price":1.9025,"exit_price":1.892988,"close_reason":"protective_trailing_stop"},{"id":29,"symbol":"TON/USDT","side":"short","grade":"B","status":"closed","result_pct":-0.7192,"closed_reason":"failed_setup_exit","closed_net_pnl":-0.39917,"closed_total_cost":0.250048,"has_lifecycle":true,"mfe_pct":0.0211,"mae_pct":-0.2687,"missed_profit_pct":0.7403,"positive_then_negative":true,"entry_price":1.8981,"max_profit_price":1.8977,"max_drawdown_price":1.9032,"exit_price":1.9032,"close_reason":"failed_setup_exit"},{"id":28,"symbol":"ETH/USDT","side":"short","grade":"A+","status":"closed","result_pct":-0.7622,"closed_reason":"failed_setup_exit","closed_net_pnl":-0.863115,"closed_total_cost":0.5103,"has_lifecycle":true,"mfe_pct":0.0,"mae_pct":-0.3116,"missed_profit_pct":0.7622,"positive_then_negative":false,"entry_price":2070.26,"max_profit_price":2070.26,"max_drawdown_price":2076.71,"exit_price":2076.71,"close_reason":"failed_setup_exit"},{"id":27,"symbol":"XRP/USDT","side":"short","grade":"A+","status":"closed","result_pct":-0.7125,"closed_reason":"failed_setup_exit","closed_net_pnl":-1.027467,"closed_total_cost":0.649643,"has_lifecycle":true,"mfe_pct":0.0,"mae_pct":-0.262,"missed_profit_pct":0.7125,"positive_then_negative":false,"entry_price":1.32815,"max_profit_price":1.32815,"max_drawdown_price":1.33163,"exit_price":1.33163,"close_reason":"failed_setup_exit"},{"id":26,"symbol":"AVAX/USDT","side":"short","grade":"A+","status":"closed","result_pct":-0.7135,"closed_reason":"failed_setup_exit","closed_net_pnl":-0.666852,"closed_total_cost":0.421097,"has_lifecycle":true,"mfe_pct":0.0,"mae_pct":-0.2629,"missed_profit_pct":0.7135,"positive_then_negative":false,"entry_price":9.1279,"max_profit_price":9.1279,"max_drawdown_price":9.1519,"exit_price":9.1519,"close_reason":"failed_setup_exit"},{"id":25,"symbol":"ADA/USDT","side":"short","grade":"A+","status":"closed","result_pct":-0.7737,"closed_reason":"failed_setup_exit","closed_net_pnl":-0.964951,"closed_total_cost":0.562046,"has_lifecycle":true,"mfe_pct":0.0,"mae_pct":-0.323,"missed_profit_pct":0.7737,"positive_then_negative":false,"entry_price":0.239594,"max_profit_price":0.239594,"max_drawdown_price":0.240368,"exit_price":0.240368,"close_reason":"failed_setup_exit"},{"id":24,"symbol":"ETH/USDT","side":"short","grade":"A+","status":"closed","result_pct":0.051,"closed_reason":"protective_breakeven_profit_guard","closed_net_pnl":0.068395,"closed_total_cost":0.602144,"has_lifecycle":true,"mfe_pct":0.492,"mae_pct":-0.1106,"missed_profit_pct":0.441,"positive_then_negative":false,"entry_price":2079.19,"max_profit_price":2068.96,"max_drawdown_price":2081.49,"exit_price":2068.79405,"close_reason":"protective_breakeven_profit_guard"},{"id":23,"symbol":"XRP/USDT","side":"short","grade":"A+","status":"closed","result_pct":0.051,"closed_reason":"protective_breakeven_profit_guard","closed_net_pnl":0.144881,"closed_total_cost":1.27552,"has_lifecycle":true,"mfe_pct":0.4511,"mae_pct":0.0,"missed_profit_pct":0.4001,"positive_then_negative":false,"entry_price":1.34565,"max_profit_price":1.33958,"max_drawdown_price":1.34565,"exit_price":1.338922,"close_reason":"protective_breakeven_profit_guard"},{"id":22,"symbol":"LINK/USDT","side":"short","grade":"A","status":"closed","result_pct":-0.8377,"closed_reason":"failed_setup_exit","closed_net_pnl":-2.512588,"closed_total_cost":1.352002,"has_lifecycle":true,"mfe_pct":0.0233,"mae_pct":-0.387,"missed_profit_pct":0.861,"positive_then_negative":true,"entry_price":9.4585,"max_profit_price":9.4563,"max_drawdown_price":9.4951,"exit_price":9.4951,"close_reason":"failed_setup_exit"},{"id":21,"symbol":"DOT/USDT","side":"short","grade":"A+","status":"closed","result_pct":-0.9553,"closed_reason":"failed_setup_exit","closed_net_pnl":-0.620867,"closed_total_cost":0.293122,"has_lifecycle":true,"mfe_pct":0.3842,"mae_pct":-0.5043,"missed_profit_pct":1.3395,"positive_then_negative":true,"entry_price":1.2493,"max_profit_price":1.2445,"max_drawdown_price":1.2556,"exit_price":1.2556,"close_reason":"failed_setup_exit"},{"id":20,"symbol":"SOL/USDT","side":"short","grade":"A+","status":"closed","result_pct":-0.7262,"closed_reason":"failed_setup_exit","closed_net_pnl":-0.649156,"closed_total_cost":0.40277,"has_lifecycle":true,"mfe_pct":0.0803,"mae_pct":-0.2756,"missed_profit_pct":0.8065,"positive_then_negative":true,"entry_price":84.2118,"max_profit_price":84.1442,"max_drawdown_price":84.4439,"exit_price":84.4439,"close_reason":"failed_setup_exit"},{"id":19,"symbol":"DOT/USDT","side":"short","grade":"A","status":"closed","result_pct":-0.7396,"closed_reason":"failed_setup_exit","closed_net_pnl":-0.371819,"closed_total_cost":0.226527,"has_lifecycle":true,"mfe_pct":0.0482,"mae_pct":-0.289,"missed_profit_pct":0.7878,"positive_then_negative":true,"entry_price":1.2457,"max_profit_price":1.2451,"max_drawdown_price":1.2493,"exit_price":1.2493,"close_reason":"failed_setup_exit"},{"id":18,"symbol":"SOL/USDT","side":"short","grade":"A","status":"closed","result_pct":-0.706,"closed_reason":"failed_setup_exit","closed_net_pnl":-0.520081,"closed_total_cost":0.331862,"has_lifecycle":true,"mfe_pct":0.0427,"mae_pct":-0.2555,"missed_profit_pct":0.7487,"positive_then_negative":true,"entry_price":84.3798,"max_profit_price":84.3438,"max_drawdown_price":84.5954,"exit_price":84.5954,"close_reason":"failed_setup_exit"},{"id":17,"symbol":"ETH/USDT","side":"short","grade":"A","status":"closed","result_pct":-0.8714,"closed_reason":"failed_setup_exit","closed_net_pnl":-1.553499,"closed_total_cost":0.803768,"has_lifecycle":true,"mfe_pct":0.1451,"mae_pct":-0.4205,"missed_profit_pct":1.0165,"positive_then_negative":true,"entry_price":2094.97,"max_profit_price":2091.93,"max_drawdown_price":2103.78,"exit_price":2103.78,"close_reason":"failed_setup_exit"},{"id":16,"symbol":"DOT/USDT","side":"short","grade":"A+","status":"closed","result_pct":-0.7411,"closed_reason":"failed_setup_exit","closed_net_pnl":-0.672824,"closed_total_cost":0.409088,"has_lifecycle":true,"mfe_pct":0.0888,"mae_pct":-0.2905,"missed_profit_pct":0.8299,"positive_then_negative":true,"entry_price":1.2393,"max_profit_price":1.2382,"max_drawdown_price":1.2429,"exit_price":1.2429,"close_reason":"failed_setup_exit"},{"id":15,"symbol":"AVAX/USDT","side":"short","grade":"A","status":"closed","result_pct":-0.9102,"closed_reason":"failed_setup_exit","closed_net_pnl":-1.378073,"closed_total_cost":0.682709,"has_lifecycle":true,"mfe_pct":0.2134,"mae_pct":-0.4593,"missed_profit_pct":1.1236,"positive_then_negative":true,"entry_price":9.2319,"max_profit_price":9.2122,"max_drawdown_price":9.2743,"exit_price":9.2743,"close_reason":"failed_setup_exit"},{"id":14,"symbol":"SOL/USDT","side":"short","grade":"A","status":"closed","result_pct":-0.7347,"closed_reason":"failed_setup_exit","closed_net_pnl":-0.50168,"closed_total_cost":0.307658,"has_lifecycle":true,"mfe_pct":0.1999,"mae_pct":-0.2841,"missed_profit_pct":0.9346,"positive_then_negative":true,"entry_price":84.2522,"max_profit_price":84.0838,"max_drawdown_price":84.4916,"exit_price":84.4916,"close_reason":"failed_setup_exit"},{"id":13,"symbol":"LTC/USDT","side":"short","grade":"A","status":"closed","result_pct":-0.8343,"closed_reason":"failed_setup_exit","closed_net_pnl":-1.7403,"closed_total_cost":0.9403,"has_lifecycle":true,"mfe_pct":0.0,"mae_pct":-0.3835,"missed_profit_pct":0.8343,"positive_then_negative":false,"entry_price":52.15,"max_profit_price":52.15,"max_drawdown_price":52.35,"exit_price":52.35,"close_reason":"failed_setup_exit"},{"id":12,"symbol":"ADA/USDT","side":"short","grade":"A","status":"closed","result_pct":-0.9678,"closed_reason":"failed_setup_exit","closed_net_pnl":-1.177722,"closed_total_cost":0.548861,"has_lifecycle":true,"mfe_pct":0.2501,"mae_pct":-0.5168,"missed_profit_pct":1.2179,"positive_then_negative":true,"entry_price":0.240337,"max_profit_price":0.239736,"max_drawdown_price":0.241579,"exit_price":0.241579,"close_reason":"failed_setup_exit"},{"id":11,"symbol":"LINK/USDT","side":"short","grade":"A+","status":"closed","result_pct":-0.9318,"closed_reason":"failed_setup_exit","closed_net_pnl":-1.283199,"closed_total_cost":0.621019,"has_lifecycle":true,"mfe_pct":0.3585,"mae_pct":-0.4809,"missed_profit_pct":1.2903,"positive_then_negative":true,"entry_price":9.4,"max_profit_price":9.3663,"max_drawdown_price":9.4452,"exit_price":9.4452,"close_reason":"failed_setup_exit"},{"id":10,"symbol":"DOT/USDT","side":"short","grade":"A","status":"closed","result_pct":-1.0246,"closed_reason":"failed_setup_exit","closed_net_pnl":-0.929384,"closed_total_cost":0.409238,"has_lifecycle":true,"mfe_pct":0.2423,"mae_pct":-0.5734,"missed_profit_pct":1.2669,"positive_then_negative":true,"entry_price":1.2382,"max_profit_price":1.2352,"max_drawdown_price":1.2453,"exit_price":1.2453,"close_reason":"failed_setup_exit"},{"id":9,"symbol":"LINK/USDT","side":"short","grade":"A","status":"closed","result_pct":-0.7169,"closed_reason":"failed_setup_exit","closed_net_pnl":-0.950778,"closed_total_cost":0.597528,"has_lifecycle":true,"mfe_pct":0.0,"mae_pct":-0.2663,"missed_profit_pct":0.7169,"positive_then_negative":false,"entry_price":9.3862,"max_profit_price":9.3862,"max_drawdown_price":9.4112,"exit_price":9.4112,"close_reason":"failed_setup_exit"},{"id":8,"symbol":"SOL/USDT","side":"short","grade":"A","status":"closed","result_pct":-0.7215,"closed_reason":"failed_setup_exit","closed_net_pnl":-0.98848,"closed_total_cost":0.617264,"has_lifecycle":true,"mfe_pct":0.188,"mae_pct":-0.271,"missed_profit_pct":0.9095,"positive_then_negative":true,"entry_price":84.0005,"max_profit_price":83.8426,"max_drawdown_price":84.2281,"exit_price":84.2281,"close_reason":"failed_setup_exit"},{"id":7,"symbol":"SOL/USDT","side":"short","grade":"A+","status":"closed","result_pct":0.051,"closed_reason":"protective_breakeven_profit_guard","closed_net_pnl":0.085636,"closed_total_cost":0.75393,"has_lifecycle":true,"mfe_pct":0.5791,"mae_pct":-0.0517,"missed_profit_pct":0.5281,"positive_then_negative":false,"entry_price":84.4634,"max_profit_price":83.9743,"max_drawdown_price":84.5071,"exit_price":84.041083,"close_reason":"protective_breakeven_profit_guard"},{"id":6,"symbol":"DOT/USDT","side":"short","grade":"A","status":"closed","result_pct":0.051,"closed_reason":"protective_breakeven_profit_guard","closed_net_pnl":0.057056,"closed_total_cost":0.502316,"has_lifecycle":true,"mfe_pct":0.8899,"mae_pct":0.0,"missed_profit_pct":0.8389,"positive_then_negative":false,"entry_price":1.2474,"max_profit_price":1.2363,"max_drawdown_price":1.2474,"exit_price":1.241163,"close_reason":"protective_breakeven_profit_guard"},{"id":4,"symbol":"SOL/USDT","side":"short","grade":"A","status":"closed","result_pct":0.051,"closed_reason":"protective_breakeven_profit_guard","closed_net_pnl":0.106132,"closed_total_cost":0.934381,"has_lifecycle":true,"mfe_pct":0.4859,"mae_pct":0.0,"missed_profit_pct":0.4349,"positive_then_negative":false,"entry_price":84.8706,"max_profit_price":84.4582,"max_drawdown_price":84.8706,"exit_price":84.446247,"close_reason":"protective_breakeven_profit_guard"},{"id":3,"symbol":"TRX/USDT","side":"long","grade":"A","status":"closed","result_pct":0.049,"closed_reason":"protective_breakeven_profit_guard","closed_net_pnl":0.147752,"closed_total_cost":1.359917,"has_lifecycle":true,"mfe_pct":0.7865,"mae_pct":-0.1675,"missed_profit_pct":0.7375,"positive_then_negative":false,"entry_price":0.371892,"max_profit_price":0.374817,"max_drawdown_price":0.371269,"exit_price":0.373751,"close_reason":"protective_breakeven_profit_guard"},{"id":2,"symbol":"TRX/USDT","side":"long","grade":"A+","status":"closed","result_pct":-0.7233,"closed_reason":"failed_setup_exit","closed_net_pnl":-2.186469,"closed_total_cost":1.358632,"has_lifecycle":true,"mfe_pct":0.0351,"mae_pct":-0.2739,"missed_profit_pct":0.7584,"positive_then_negative":true,"entry_price":0.37282,"max_profit_price":0.372951,"max_drawdown_price":0.371799,"exit_price":0.371799,"close_reason":"failed_setup_exit"},{"id":1,"symbol":"BTC/USDT","side":"long","grade":"A+","status":"closed","result_pct":0.049,"closed_reason":"protective_breakeven_profit_guard","closed_net_pnl":0.163333,"closed_total_cost":1.503327,"has_lifecycle":true,"mfe_pct":0.5458,"mae_pct":-0.0075,"missed_profit_pct":0.4968,"positive_then_negative":false,"entry_price":77303.33,"max_profit_price":77725.27,"max_drawdown_price":77297.52,"exit_price":77689.84665,"close_reason":"protective_breakeven_profit_guard"}]}
### ml_outcomes_summary.json
{"status": "degraded", "fallback_used": false, "reason": "ml_summary_python_failed", "python_bin": "python3"}

### compose_logs_api_tail.txt
# CMD: bash -lc docker compose logs --since=8h api | tail -n 2000
api-1  | INFO:     Started server process [1]
api-1  | INFO:     Waiting for application startup.
api-1  | INFO:     Application startup complete.
api-1  | INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
api-1  | INFO:     172.18.0.1:56896 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:56910 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:56914 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:59866 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:59840 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:59852 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:53298 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:53312 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:53306 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:43084 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:43068 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:43070 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:51026 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:51058 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:51042 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:38224 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:38216 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:38222 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:38562 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:38558 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:38560 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:34040 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:34020 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:34024 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:50788 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:50800 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:50794 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32768 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32792 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32776 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32768 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32776 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32820 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32818 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32806 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32792 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32818 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32792 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32806 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32818 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32806 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32792 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32818 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32792 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32806 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32818 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32806 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32792 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32818 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32792 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32806 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32818 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32806 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32792 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32818 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32792 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32806 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32818 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32806 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32792 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32818 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32792 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32806 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32818 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32806 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32792 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32818 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32792 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32806 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32818 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32806 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32792 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32818 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32792 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32806 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32792 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32806 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:32818 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | [TELEGRAM SEND ERROR] chat_id=1832004802: ConnectTimeout: ConnectTimeout('')
api-1  | [TELEGRAM SEND ERROR] chat_id=1832004802: ConnectTimeout: ConnectTimeout('')
api-1  | INFO:     172.18.0.1:56726 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:56710 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:56718 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:38398 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:38372 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:38384 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:50884 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:50864 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:50874 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:46666 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:46660 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:46662 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | [TELEGRAM SEND ERROR] chat_id=1832004802: ConnectTimeout: ConnectTimeout('')
api-1  | INFO:     172.18.0.1:35448 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:35434 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:35432 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | [HTX RETRY] attempt=1/3 error=htx GET https://api.huobi.pro/market/history/candles?period=1min&symbol=btcusdt&size=250
api-1  | INFO:     172.18.0.1:33146 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:33136 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:33140 - "GET /analytics/summary HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:33146 - "GET /signals?limit=10&offset=0 HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:33140 - "GET /bot/state HTTP/1.1" 200 OK
api-1  | INFO:     172.18.0.1:33136 - "GET /analytics/summary HTTP/1.1" 200 OK

### compose_logs_web_tail.txt
# CMD: bash -lc docker compose logs --since=8h web | tail -n 600
web-1  | 
web-1  | > robot-owner-ui@1.0.0 dev
web-1  | > next dev -H 0.0.0.0 -p 3000
web-1  | 
web-1  |   ▲ Next.js 14.2.5
web-1  |   - Local:        http://localhost:3000
web-1  |   - Network:      http://0.0.0.0:3000
web-1  | 
web-1  |  ✓ Starting...
web-1  | Attention: Next.js now collects completely anonymous telemetry regarding usage.
web-1  | This information is used to shape Next.js' roadmap and prioritize features.
web-1  | You can learn more, including how to opt-out if you'd not like to participate in this anonymous program, by visiting the following URL:
web-1  | https://nextjs.org/telemetry
web-1  | 
web-1  | 
web-1  |    We detected TypeScript in your project and reconfigured your tsconfig.json file for you. Strict-mode is set to false by default.
web-1  |    The following suggested values were added to your tsconfig.json. These values can be changed to fit your project's needs:
web-1  | 
web-1  |    	- include was updated to add '.next/types/**/*.ts'
web-1  | 
web-1  |  ✓ Ready in 2.9s

