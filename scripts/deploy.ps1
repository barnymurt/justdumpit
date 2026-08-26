param(
    [string]$AppName = "justdumpit-ytscraper",
    [switch]$NoCache,
    [switch]$OpenConsole
)

$ErrorActionPreference = "Stop"

function Check-Fly {
    if (-not (Get-Command fly -ErrorAction SilentlyContinue)) {
        Write-Error "fly CLI not found. Install with: iwr https://fly.io/install.ps1 -useb | iex"
    }
}

Check-Fly

Write-Host "==> Verifying fly auth" -ForegroundColor Cyan
fly auth whoami | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Not logged in. Running fly auth login..." -ForegroundColor Yellow
    fly auth login
}

Write-Host "==> Checking app exists" -ForegroundColor Cyan
$appInfo = fly apps list --json 2>$null | ConvertFrom-Json
$exists = $appInfo | Where-Object { $_.Name -eq $AppName }

if (-not $exists) {
    Write-Host "==> Creating app $AppName" -ForegroundColor Yellow
    fly apps create $AppName --org personal
    Write-Host "==> Creating persistent volume (1GB in lhr)" -ForegroundColor Yellow
    fly volumes create ytscraper_data --size 1 --region lhr
}

$envFile = Join-Path $PSScriptRoot "..\.env"
if (Test-Path $envFile) {
    Write-Host "==> Setting secrets from .env" -ForegroundColor Cyan
    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith('#')) {
            fly secrets set --stage $line
        }
    }
} else {
    Write-Host "==> No .env file found. Skipping secrets." -ForegroundColor Yellow
    Write-Host "   Run: fly secrets set MINIMAX_API_KEY=your-key" -ForegroundColor Yellow
}

Write-Host "==> Deploying" -ForegroundColor Cyan
$deployArgs = @("deploy", "--remote-only")
if ($NoCache) { $deployArgs += "--no-cache" }
& fly @deployArgs

if ($LASTEXITCODE -ne 0) {
    Write-Error "Deploy failed."
}

Write-Host "==> Deploy complete." -ForegroundColor Green
Write-Host "    URL:    https://$AppName.fly.dev" -ForegroundColor Green
Write-Host "    Logs:   fly logs" -ForegroundColor Green
Write-Host "    SSH:    fly ssh console" -ForegroundColor Green

if ($OpenConsole) {
    fly ssh console
}