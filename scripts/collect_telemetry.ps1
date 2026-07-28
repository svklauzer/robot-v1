# Сбор телеметрии одной командой (#telemetry-snapshot-2026-07-27).
#
# Зачем. Боевые данные живут на Render, а в репозитории лежал снимок от 5 июня —
# отставший на 7 недель и 182 сделки. Прямого доступа к API у ассистента нет,
# поэтому единственный канал — выгрузка. Раньше это были километры curl,
# копируемые в чат вручную.
#
# Теперь: одна команда → один файл в analytics_24h/ → коммит.
# Ассистент читает файл напрямую, включая ТРАЕКТОРИИ сделок, по которым
# делается вся калибровка выходов.
#
# Запуск (PowerShell, из корня репозитория):
#     $env:TOKEN="<OWNER_API_TOKEN>"
#     $env:API="https://robot-api-1rgi.onrender.com"
#     .\scripts\collect_telemetry.ps1
#
# Затем:  git add analytics_24h && git commit -m "telemetry snapshot"

param(
    [string]$ApiBase = $env:API,
    [string]$Token   = $env:TOKEN,
    [int]$SignalsLimit = 40,
    [int]$EventsLimit  = 60
)

if (-not $ApiBase) { Write-Error "Не задан `$env:API"; exit 1 }
if (-not $Token)   { Write-Error "Не задан `$env:TOKEN"; exit 1 }

$stamp   = Get-Date -Format "yyyy-MM-dd_HHmm"
$outDir  = Join-Path $PSScriptRoot "..\analytics_24h"
$outFile = Join-Path $outDir "telemetry_$stamp.json"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

# Что собираем. Порядок важен только для читаемости.
# signals — САМОЕ ценное: в plan_json.lifecycle.traj лежат траектории,
# на которых строится вся калибровка выходов.
$endpoints = [ordered]@{
    "validation_gates"  = "/analytics/validation-gates"
    "summary"           = "/analytics/summary"
    "mfe_mae"           = "/analytics/mfe-mae"
    "depth_coverage"    = "/analytics/depth-coverage"
    "daily_quality_72h" = "/analytics/daily-quality-report?hours=72"
    "signal_quality"    = "/analytics/signal-quality"
    "reason_breakdown"  = "/analytics/reason-breakdown?limit=500"
    "symbol_perf"       = "/analytics/symbol-performance?lookback=12"
    # (#expectancy-2026-07-27) Ожидание на сделку — то, по чему судим о символе
    # вместо win-rate. Разрезы по символам, причинам входа и их парам.
    "expectancy"        = "/analytics/expectancy"
    # (#venue-expectancy-2026-07-27) Раздельно spot/swap: round-trip 0.40% против
    # 0.10%, смешивать нельзя. Здесь же сверка booked против achievable.
    "venue_expectancy"  = "/analytics/venue-expectancy"
    # exit-replay теперь по профилям: trend — основная масса сделок, раньше
    # эндпоинт её вообще не видел.
    "exit_replay_trend" = "/ml/exit-replay?profile=trend"
    "exit_replay_scalp" = "/ml/exit-replay?profile=scalp"
    # (#walk-forward-2026-07-27) Out-of-sample: подбор оценён на данных, которых
    # он не видел. exit_replay — in-sample и систематически оптимистичен.
    "wf_trend"          = "/ml/walk-forward?regime=trend"
    "wf_range"          = "/ml/walk-forward?regime=range"
    "wf_scalp"          = "/ml/walk-forward?regime=scalp"
    "wf_history"        = "/ml/walk-forward/history"
    "ml_outcomes"       = "/ml/outcomes/stats"
    "ml_status"         = "/ml/status"
    "signals"           = "/signals?limit=$SignalsLimit&offset=0"
    "positions"         = "/positions?limit=$SignalsLimit"
    "events"            = "/intelligence/events?limit=$EventsLimit"
    "scan"              = "/intelligence/scan"
    "grid_state"        = "/grid/state"
    "funding_arb"       = "/funding-arb/positions?limit=50"
    "funding_summary"   = "/funding-arb/summary"
    "cross_arb"         = "/venues/cross-arb"
    "venues_history"    = "/venues/compare/history?days=7"
    "egress_history"    = "/system/egress-history?hours=24"
    "exchange_diag"     = "/system/exchange-diagnostics"
    "system_health"     = "/system/health"
    "readiness"         = "/system/readiness"
}

$result = [ordered]@{
    collected_at = (Get-Date -Format "o")
    api_base     = $ApiBase
    data         = [ordered]@{}
    errors       = [ordered]@{}
}

$headers = @{ "X-Owner-Token" = $Token }
$i = 0
foreach ($name in $endpoints.Keys) {
    $i++
    $path = $endpoints[$name]
    Write-Host ("[{0,2}/{1}] {2,-18} {3}" -f $i, $endpoints.Count, $name, $path)
    try {
        $resp = Invoke-RestMethod -Uri ($ApiBase + $path) -Headers $headers `
                                  -Method Get -TimeoutSec 60
        $result.data[$name] = $resp
    } catch {
        $msg = $_.Exception.Message
        Write-Host ("        ! ошибка: {0}" -f $msg) -ForegroundColor Yellow
        $result.errors[$name] = $msg
    }
}

$result | ConvertTo-Json -Depth 40 -Compress | Set-Content -Path $outFile -Encoding UTF8

$sizeKb = [math]::Round((Get-Item $outFile).Length / 1KB, 1)
Write-Host ""
Write-Host "Готово: $outFile ($sizeKb KB)" -ForegroundColor Green
if ($result.errors.Count -gt 0) {
    Write-Host "Эндпоинтов с ошибкой: $($result.errors.Count)" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "Дальше:  git add analytics_24h && git commit -m `"telemetry $stamp`" && git push"
