# Выгрузка всех сигналов из боевой базы в один файл для офлайн-разбора.
#
# Зачем не trade_outcomes.jsonl: в нём нет цены входа/выхода, режима и данных о
# частичной фиксации на TP1 - без них честный пересчёт издержек не сделать.
# В базе это лежит в Signal.plan_json.lifecycle, и /signals его отдаёт.
#
# Эндпоинт режет limit до 200, поэтому идём страницами по offset.
#
# Запуск:
#   $env:API_URL   = "https://robot-api-1rgi.onrender.com"
#   $env:OWNER_TOKEN = "<OWNER_API_TOKEN>"
#   powershell -ExecutionPolicy Bypass -File scripts\export_signals.ps1

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$api   = if ($env:API_URL)     { $env:API_URL }     else { "https://robot-api-1rgi.onrender.com" }
$token = if ($env:OWNER_TOKEN) { $env:OWNER_TOKEN } else { $null }

if (-not $token) {
    Write-Error "Задайте `$env:OWNER_TOKEN - без owner-токена API вернёт 401."
    exit 1
}

$headers = @{ "X-Owner-Token" = $token }
$page    = 200
$offset  = 0
$all     = New-Object System.Collections.ArrayList
$total   = $null

do {
    $uri  = "$api/signals?limit=$page&offset=$offset"
    $resp = Invoke-RestMethod -Uri $uri -Headers $headers

    if ($null -eq $total) {
        $total = $resp.total
        Write-Host "всего сигналов в базе: $total"
    }

    $count = @($resp.items).Count
    if ($count -gt 0) { [void]$all.AddRange(@($resp.items)) }

    Write-Host ("  offset {0,5} -> получено {1,4}, накоплено {2,4}" -f $offset, $count, $all.Count)
    $offset += $page
} while ($count -eq $page -and $all.Count -lt $total)

$stamp = Get-Date -Format "yyyy-MM-dd_HHmm"
$dir   = Join-Path $PSScriptRoot "..\analytics_24h"
$out   = Join-Path $dir "signals_export_$stamp.json"

$payload = [ordered]@{
    collected_at = (Get-Date).ToString("o")
    api_base     = $api
    total        = $total
    count        = $all.Count
    items        = $all
}

# Без BOM: Python по умолчанию читает utf-8, а BOM ломает json.load.
$json = $payload | ConvertTo-Json -Depth 12 -Compress
[IO.File]::WriteAllText($out, $json, (New-Object Text.UTF8Encoding $false))

Write-Host ""
Write-Host "сохранено: $out"
Write-Host ("закрытых в выгрузке: {0}" -f (@($all | Where-Object { $_.status -eq 'closed' }).Count))
