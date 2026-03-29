# start_services.ps1 - Start MeiliSearch + remount Google Drive
# Run this at the start of every work session after first-time setup.

$ErrorActionPreference = "Stop"
$ROOT = $PSScriptRoot
$BIN  = "$ROOT\bin"
$env:PATH = "$BIN;$env:PATH"

function Write-Step { param($msg) Write-Host "" ; Write-Host "[>>] $msg" -ForegroundColor Cyan }
function Write-OK   { param($msg) Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Warn { param($msg) Write-Host "  [!!] $msg" -ForegroundColor Yellow }

# -- Tor Daemon ---------------------------------------------------------------
Write-Step "Starting Tor daemon..."
$torRunning = $false
try {
    $s = [System.Net.Sockets.TcpClient]::new("127.0.0.1", 9050)
    $s.Close(); $torRunning = $true
} catch { }

if ($torRunning) {
    Write-OK "Tor already running on port 9050"
} else {
    $launchScript = "$BIN\launch_tor.ps1"
    if (Test-Path $launchScript) {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $launchScript
    } else {
        Write-Warn "launch_tor.ps1 not found. Run setup_tor.ps1 first."
    }
}

# -- MeiliSearch --------------------------------------------------------------
Write-Step "Starting MeiliSearch..."
$meiliRunning = $false
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:7700/health" -UseBasicParsing -TimeoutSec 2
    if ($r.StatusCode -eq 200) { $meiliRunning = $true }
} catch { }

if ($meiliRunning) {
    Write-OK "MeiliSearch already running"
} else {
    $meiliInPath = Get-Command meilisearch -ErrorAction SilentlyContinue
    if ($meiliInPath) {
        $meiliExe = $meiliInPath.Source
    } else {
        $meiliExe = "$BIN\meilisearch.exe"
    }
    Start-Process -FilePath "$meiliExe" `
        -ArgumentList "--db-path", "`"$ROOT\database\meili`"", "--http-addr", "127.0.0.1:7700", "--no-analytics" `
        -WindowStyle Hidden
    Start-Sleep -Seconds 3
    Write-OK "MeiliSearch started at http://127.0.0.1:7700"
}

# -- Google Drive Mount -------------------------------------------------------
Write-Step "Mounting Google Drive as X:..."
if (Test-Path "X:\") {
    Write-OK "X: already mounted"
} else {
    $rcloneInPath = Get-Command rclone -ErrorAction SilentlyContinue
    if ($rcloneInPath) {
        $rcloneExe = $rcloneInPath.Source
    } else {
        $rcloneExe = "$BIN\rclone.exe"
    }
    Start-Process -FilePath "$rcloneExe" `
        -ArgumentList "mount","lsse_gdrive:","X:","--vfs-cache-mode","full","--vfs-cache-max-size","100G","--tpslimit","10","--drive-chunk-size","64M","--allow-non-empty","--network-mode" `
        -WindowStyle Hidden
    Start-Sleep -Seconds 4
    if (Test-Path "X:\") {
        Write-OK "Google Drive mounted as X:"
    } else {
        Write-Warn "X: not yet ready - check Explorer in a few seconds"
    }
}

Write-Host ""
Write-Host "  Services ready. Activate venv with:" -ForegroundColor Green
Write-Host "    .\venv\Scripts\Activate.ps1" -ForegroundColor White
Write-Host ""
