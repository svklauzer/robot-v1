param(
  [string]$ApiUrl = "http://localhost:8000",
  [string]$ComposeBin = "docker compose"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = (Resolve-Path (Join-Path $ScriptDir "..")).Path

if (-not (Get-Command bash -ErrorAction SilentlyContinue)) {
  Write-Error "bash is not found. Install Git Bash or WSL, then run this script again."
}

$env:API_URL = $ApiUrl
$env:COMPOSE_BIN = $ComposeBin

$bashScript = "$RootDir/scripts/collect_run_report.sh"
& bash -lc "cd '$RootDir' && '$bashScript'"

if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}
