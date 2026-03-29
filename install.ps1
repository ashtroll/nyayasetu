# install.ps1 - Two-phase pip install that works on Python 3.13
# Phase 1: install all standard packages
# Phase 2: install opennyai --no-deps (bypasses its stale spacy<3.3 pin)

$ErrorActionPreference = "Stop"
$ROOT = $PSScriptRoot
$PY   = "$ROOT\venv\Scripts\python.exe"

function Write-Step { param($msg) Write-Host "" ; Write-Host "[>>] $msg" -ForegroundColor Cyan }
function Write-OK   { param($msg) Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Fail { param($msg) Write-Host "  [XX] $msg" -ForegroundColor Red }

if (-not (Test-Path $PY)) {
    Write-Fail "venv not found at $ROOT\venv. Run setup.bat first."
    exit 1
}

# Phase 1: everything except opennyai
Write-Step "Phase 1 - Installing main packages (may take 10-20 min for torch)..."
& "$PY" -m pip install -r "$ROOT\requirements.txt"
if ($LASTEXITCODE -ne 0) {
    Write-Fail "Phase 1 failed. See errors above."
    exit 1
}
Write-OK "Phase 1 complete"

# Phase 2: opennyai without its stale dependency tree
Write-Step "Phase 2 - Installing opennyai (--no-deps to skip spacy version conflict)..."
& "$PY" -m pip install opennyai==0.0.13 --no-deps
if ($LASTEXITCODE -ne 0) {
    Write-Fail "Phase 2 failed."
    exit 1
}
Write-OK "opennyai installed (--no-deps)"

# Phase 3: opennyai's runtime deps that are missing but compatible with py313
Write-Step "Phase 3 - Installing opennyai runtime deps..."
$opennyaiDeps = @(
    "nltk>=3.6",
    "pandas>=1.2.4",
    "prettytable>=3.1.1",
    "scikit-learn>=1.3.2",
    "multiprocess>=0.70.15",
    "pytorch-transformers"
)
foreach ($dep in $opennyaiDeps) {
    Write-Host "  Installing $dep ..." -ForegroundColor Gray
    & "$PY" -m pip install $dep --quiet
}
Write-OK "opennyai runtime deps installed"

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  ALL PACKAGES INSTALLED SUCCESSFULLY" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
