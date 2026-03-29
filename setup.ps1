# setup.ps1 - Full automated setup for the Legal Strategy Search Engine
# Run with:  powershell -ExecutionPolicy Bypass -File setup.ps1

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step { param($msg) Write-Host "" ; Write-Host "[>>] $msg" -ForegroundColor Cyan }
function Write-OK   { param($msg) Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Warn { param($msg) Write-Host "  [!!] $msg" -ForegroundColor Yellow }
function Write-Fail { param($msg) Write-Host "  [XX] $msg" -ForegroundColor Red }

$ROOT = $PSScriptRoot
$BIN  = "$ROOT\bin"
$VENV = "$ROOT\venv"
$PIP  = "$VENV\Scripts\pip.exe"
$PY   = "$VENV\Scripts\python.exe"

Clear-Host
Write-Host "============================================================" -ForegroundColor Magenta
Write-Host "   Legal Strategy Search Engine (LSSE) - Automated Setup   " -ForegroundColor Magenta
Write-Host "============================================================" -ForegroundColor Magenta

# ------------------------------------------------------------
# 1. Verify Python 3.10+
# ------------------------------------------------------------
Write-Step "Checking Python version..."
try {
    $pyver = & python --version 2>&1
    if ($pyver -match "Python (\d+)\.(\d+)") {
        $major = [int]$Matches[1]
        $minor = [int]$Matches[2]
        if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 10)) {
            Write-Fail "Python 3.10+ required. Found: $pyver"
            exit 1
        }
        Write-OK "$pyver detected"
    }
} catch {
    Write-Fail "Python not found in PATH. Install from https://python.org"
    exit 1
}

# ------------------------------------------------------------
# 2. Create virtual environment
# ------------------------------------------------------------
Write-Step "Creating virtual environment..."
if (Test-Path "$VENV\Scripts\python.exe") {
    Write-Warn "venv already exists - skipping creation"
} else {
    & python -m venv "$VENV"
    Write-OK "venv created at $VENV"
}

# ------------------------------------------------------------
# 3. Upgrade pip then install requirements
# ------------------------------------------------------------
Write-Step "Upgrading pip..."
& "$PY" -m pip install --quiet --upgrade pip
Write-OK "pip upgraded"

Write-Step "Installing packages via install.ps1 (two-phase, handles opennyai/spacy conflict)..."
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$ROOT\install.ps1"
if ($LASTEXITCODE -ne 0) {
    Write-Fail "Package installation failed. Check the error output above."
    exit 1
}
Write-OK "All Python packages installed"

# ------------------------------------------------------------
# 4. Copy .env.example to .env
# ------------------------------------------------------------
Write-Step "Setting up .env file..."
if (Test-Path "$ROOT\.env") {
    Write-Warn ".env already exists - not overwriting"
} else {
    Copy-Item "$ROOT\.env.example" "$ROOT\.env"
    Write-OK ".env created from .env.example"
}

# ------------------------------------------------------------
# 5. Create local bin/ directory
# ------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $BIN | Out-Null

# ------------------------------------------------------------
# 6. Download rclone if not present
# ------------------------------------------------------------
Write-Step "Checking for rclone..."
$rcloneExe = "$BIN\rclone.exe"

$rcloneInPath = Get-Command rclone -ErrorAction SilentlyContinue
if ($rcloneInPath) {
    Write-OK "rclone found in system PATH"
    $rcloneExe = $rcloneInPath.Source
} elseif (Test-Path $rcloneExe) {
    Write-OK "rclone found at $rcloneExe"
} else {
    Write-Warn "rclone not found - downloading..."
    $rcloneZip = "$BIN\rclone.zip"
    $rcloneUrl = "https://downloads.rclone.org/rclone-current-windows-amd64.zip"
    try {
        Invoke-WebRequest -Uri $rcloneUrl -OutFile $rcloneZip -UseBasicParsing
        Expand-Archive -Path $rcloneZip -DestinationPath "$BIN\rclone_tmp" -Force
        $extracted = Get-ChildItem "$BIN\rclone_tmp" -Recurse -Filter "rclone.exe" | Select-Object -First 1
        Copy-Item $extracted.FullName $rcloneExe
        Remove-Item "$BIN\rclone_tmp" -Recurse -Force
        Remove-Item $rcloneZip -Force
        Write-OK "rclone downloaded to $rcloneExe"
    } catch {
        Write-Warn "Could not download rclone: $_"
        Write-Warn "Download manually from https://rclone.org/install/ and place rclone.exe in $BIN"
    }
}

# Add bin/ to session PATH
$env:PATH = "$BIN;$env:PATH"

# ------------------------------------------------------------
# 7. Check WinFsp
# ------------------------------------------------------------
Write-Step "Checking for WinFsp (required for rclone mount on Windows)..."
$wfKey1 = "HKLM:\SOFTWARE\WOW6432Node\WinFsp"
$wfKey2 = "HKLM:\SOFTWARE\WinFsp"
if ((Test-Path $wfKey1) -or (Test-Path $wfKey2)) {
    Write-OK "WinFsp is installed"
} else {
    Write-Warn "WinFsp NOT found. It is required for rclone mount on Windows."
    Write-Host "  Download from: https://github.com/winfsp/winfsp/releases/latest" -ForegroundColor Yellow
    $open = Read-Host "  Open download page in browser now? [Y/n]"
    if ($open -ne 'n' -and $open -ne 'N') {
        Start-Process "https://github.com/winfsp/winfsp/releases/latest"
    }
    Read-Host "  Install WinFsp, then press ENTER to continue"
}

# ------------------------------------------------------------
# 8. Download MeiliSearch binary
# ------------------------------------------------------------
Write-Step "Checking for MeiliSearch..."
$meiliExe = "$BIN\meilisearch.exe"

$meiliInPath = Get-Command meilisearch -ErrorAction SilentlyContinue
if ($meiliInPath) {
    Write-OK "meilisearch found in system PATH"
    $meiliExe = $meiliInPath.Source
} elseif (Test-Path $meiliExe) {
    Write-OK "meilisearch found at $meiliExe"
} else {
    Write-Warn "MeiliSearch not found - downloading v1.8.0..."
    $meiliUrl = "https://github.com/meilisearch/meilisearch/releases/download/v1.8.0/meilisearch-windows-amd64.exe"
    try {
        Invoke-WebRequest -Uri $meiliUrl -OutFile $meiliExe -UseBasicParsing
        Write-OK "MeiliSearch downloaded to $meiliExe"
    } catch {
        Write-Warn "Could not download MeiliSearch: $_"
        Write-Warn "Download manually from https://github.com/meilisearch/meilisearch/releases"
    }
}

# ------------------------------------------------------------
# 9. Configure rclone Google Drive remote
# ------------------------------------------------------------
Write-Step "Configuring rclone Google Drive remote..."

$existingRemotes = & "$rcloneExe" listremotes 2>$null
if ($existingRemotes -match "lsse_gdrive:") {
    Write-OK "rclone remote 'lsse_gdrive' already configured"
} else {
    Write-Host ""
    Write-Host "  rclone config wizard will open now." -ForegroundColor Yellow
    Write-Host "  Steps:" -ForegroundColor Yellow
    Write-Host "    1. Press 'n' -> New remote" -ForegroundColor White
    Write-Host "    2. Name it:    lsse_gdrive" -ForegroundColor White
    Write-Host "    3. Type:       Google Drive (pick the number shown)" -ForegroundColor White
    Write-Host "    4. Leave Client ID and Secret blank (press ENTER)" -ForegroundColor White
    Write-Host "    5. Scope:      Full access (option 1)" -ForegroundColor White
    Write-Host "    6. Press ENTER through remaining options" -ForegroundColor White
    Write-Host "    7. Say 'y' to auto config (browser opens for OAuth)" -ForegroundColor White
    Write-Host "    8. Sign in with Google, then press 'q' to quit config" -ForegroundColor White
    Read-Host "  Press ENTER to launch rclone config"
    & "$rcloneExe" config
    Write-OK "rclone config complete"
}

# ------------------------------------------------------------
# 10. Mount Google Drive as X:
# ------------------------------------------------------------
Write-Step "Mounting Google Drive as X:..."

if (Test-Path "X:\") {
    Write-Warn "X: is already mounted"
} else {
    $launchScript = "$BIN\start_mount.ps1"
    $launchContent = "Start-Process -FilePath `"$rcloneExe`" -ArgumentList 'mount','lsse_gdrive:','X:','--vfs-cache-mode','full','--vfs-cache-max-size','100G','--tpslimit','10','--drive-chunk-size','64M','--allow-non-empty','--network-mode' -WindowStyle Hidden"
    Set-Content -Path $launchScript -Value $launchContent -Encoding ASCII
    Start-Process powershell -ArgumentList "-ExecutionPolicy Bypass -File `"$launchScript`"" -WindowStyle Hidden
    Start-Sleep -Seconds 5
    if (Test-Path "X:\") {
        Write-OK "Google Drive mounted as X:"
    } else {
        Write-Warn "Mount may still be starting. Check X: in Explorer in a few seconds."
        Write-Warn "If it fails, run mount_gdrive.bat manually."
    }
}

# ------------------------------------------------------------
# 11. Start MeiliSearch in background
# ------------------------------------------------------------
Write-Step "Starting MeiliSearch on http://127.0.0.1:7700..."

$meiliRunning = $false
try {
    $resp = Invoke-WebRequest -Uri "http://127.0.0.1:7700/health" -UseBasicParsing -TimeoutSec 2
    if ($resp.StatusCode -eq 200) { $meiliRunning = $true }
} catch { }

if ($meiliRunning) {
    Write-OK "MeiliSearch already running"
} else {
    $meiliDbDir = "$ROOT\database\meili"
    Start-Process -FilePath "$meiliExe" `
        -ArgumentList "--db-path", "`"$meiliDbDir`"", "--http-addr", "127.0.0.1:7700", "--no-analytics" `
        -WindowStyle Hidden

    Write-Host "  Waiting for MeiliSearch..." -ForegroundColor Gray
    $tries = 0
    while (-not $meiliRunning -and $tries -lt 10) {
        Start-Sleep -Seconds 2
        $tries++
        try {
            $resp = Invoke-WebRequest -Uri "http://127.0.0.1:7700/health" -UseBasicParsing -TimeoutSec 2
            if ($resp.StatusCode -eq 200) { $meiliRunning = $true }
        } catch { }
    }
    if ($meiliRunning) {
        Write-OK "MeiliSearch running at http://127.0.0.1:7700"
    } else {
        Write-Warn "MeiliSearch did not respond yet. It may still be starting."
    }
}

# ------------------------------------------------------------
# 12. Done
# ------------------------------------------------------------
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  SETUP COMPLETE" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  venv       : $VENV\Scripts\python.exe" -ForegroundColor White
Write-Host "  rclone     : $rcloneExe" -ForegroundColor White
Write-Host "  MeiliSearch: http://127.0.0.1:7700" -ForegroundColor White
Write-Host "  GDrive     : X:\" -ForegroundColor White
Write-Host ""
Write-Host "  Activate venv in a new terminal:" -ForegroundColor Green
Write-Host "    PowerShell:  .\venv\Scripts\Activate.ps1" -ForegroundColor White
Write-Host "    CMD:         venv\Scripts\activate.bat" -ForegroundColor White
Write-Host ""
Write-Host "  Tell Claude you are ready for Step 2 (Tor Scraper)." -ForegroundColor Green
