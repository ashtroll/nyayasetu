# setup_tor.ps1 - Automates Tor Expert Bundle setup and launch
# Usage: powershell -ExecutionPolicy Bypass -File setup_tor.ps1

$ErrorActionPreference = "Stop"

function Write-Step { param($msg) Write-Host "" ; Write-Host "[>>] $msg" -ForegroundColor Cyan }
function Write-OK   { param($msg) Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Warn { param($msg) Write-Host "  [!!] $msg" -ForegroundColor Yellow }
function Write-Fail { param($msg) Write-Host "  [XX] $msg" -ForegroundColor Red }

# ------------------------------------------------------------
# 1. Find tor.exe — search common locations
# ------------------------------------------------------------
Write-Step "Locating Tor Expert Bundle..."

$torCandidates = @(
    "$env:USERPROFILE\Downloads\tor-expert-bundle*\tor\tor.exe",
    "$env:USERPROFILE\Desktop\tor-expert-bundle*\tor\tor.exe",
    "$env:USERPROFILE\Downloads\Tor\tor.exe",
    "$env:USERPROFILE\Desktop\Tor\tor.exe",
    "C:\Tor\tor.exe",
    "C:\tor\tor.exe"
)

$torExe = $null
foreach ($pattern in $torCandidates) {
    $found = Get-Item $pattern -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { $torExe = $found.FullName; break }
}

if (-not $torExe) {
    Write-Warn "Could not auto-locate tor.exe."
    Write-Host "  Enter the full path to tor.exe (e.g. C:\Users\you\Downloads\Tor\tor.exe):" -ForegroundColor Yellow
    $torExe = Read-Host "  Path"
    if (-not (Test-Path $torExe)) {
        Write-Fail "File not found: $torExe"
        exit 1
    }
}

$torDir = Split-Path $torExe
Write-OK "Found tor.exe at: $torExe"

# ------------------------------------------------------------
# 2. Copy tor.exe into project bin/ for easy access
# ------------------------------------------------------------
$ROOT  = $PSScriptRoot
$BIN   = "$ROOT\bin"
New-Item -ItemType Directory -Force -Path $BIN | Out-Null

$torDest = "$BIN\tor.exe"
if (-not (Test-Path $torDest)) {
    Copy-Item $torExe $torDest
    Write-OK "Copied tor.exe -> bin\tor.exe"
} else {
    Write-OK "bin\tor.exe already present"
}

# Also copy companion DLLs if they sit next to tor.exe
Get-ChildItem $torDir -Filter "*.dll" | ForEach-Object {
    $dest = "$BIN\$($_.Name)"
    if (-not (Test-Path $dest)) { Copy-Item $_.FullName $dest }
}

# ------------------------------------------------------------
# 3. Write torrc into bin/
# ------------------------------------------------------------
Write-Step "Writing torrc configuration..."

$torrcPath = "$BIN\torrc"
$torrcContent = @"
# LSSE torrc - Tor Expert Bundle configuration
SocksPort 9050
ControlPort 9051
CookieAuthentication 1
DataDirectory $BIN\tor_data
Log notice file $BIN\tor.log
"@

Set-Content -Path $torrcPath -Value $torrcContent -Encoding ASCII
New-Item -ItemType Directory -Force -Path "$BIN\tor_data" | Out-Null
Write-OK "torrc written to $torrcPath"

# ------------------------------------------------------------
# 4. Kill any existing tor.exe processes
# ------------------------------------------------------------
Write-Step "Stopping any existing Tor process..."
Get-Process tor -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2
Write-OK "Clear"

# ------------------------------------------------------------
# 5. Launch tor.exe as background process
# ------------------------------------------------------------
Write-Step "Starting Tor daemon..."

Start-Process -FilePath "$BIN\tor.exe" `
    -ArgumentList "-f", "`"$torrcPath`"" `
    -WindowStyle Hidden `
    -RedirectStandardOutput "$BIN\tor_stdout.log" `
    -RedirectStandardError  "$BIN\tor_stderr.log"

Write-Host "  Waiting for Tor to bootstrap (up to 30s)..." -ForegroundColor Gray

$ready = $false
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 2
    try {
        $s = [System.Net.Sockets.TcpClient]::new("127.0.0.1", 9050)
        $s.Close()
        $ready = $true
        break
    } catch { }
}

if ($ready) {
    Write-OK "Tor SOCKS proxy is UP on 127.0.0.1:9050"
} else {
    Write-Warn "Tor SOCKS did not respond in 30s. Check $BIN\tor_stderr.log"
}

$controlReady = $false
try {
    $s = [System.Net.Sockets.TcpClient]::new("127.0.0.1", 9051)
    $s.Close()
    $controlReady = $true
} catch { }

if ($controlReady) {
    Write-OK "Tor ControlPort is UP on 127.0.0.1:9051"
} else {
    Write-Warn "ControlPort 9051 not reachable yet."
}

# ------------------------------------------------------------
# 6. Verify with setup_tor.py
# ------------------------------------------------------------
Write-Step "Running Python connectivity check..."
$pyExe = "$ROOT\venv\Scripts\python.exe"
if (Test-Path $pyExe) {
    & "$pyExe" "$ROOT\setup_tor.py"
} else {
    Write-Warn "venv not found - run setup.bat first, then re-run this script"
}

# ------------------------------------------------------------
# 7. Add tor to start_services.ps1 automatically
# ------------------------------------------------------------
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  Tor is running. Keep this session open or add tor.exe" -ForegroundColor Green
Write-Host "  to start_services.ps1 (already done if you see OK above)." -ForegroundColor Green
Write-Host ""
Write-Host "  Now run the scraper with Tor:" -ForegroundColor White
Write-Host "    venv\Scripts\python.exe scraper.py --test 5" -ForegroundColor White
Write-Host "============================================================" -ForegroundColor Green
