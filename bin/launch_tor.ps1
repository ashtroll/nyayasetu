Get-Process tor -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

$torExe  = "C:\Users\as157\OneDrive\Desktop\torr expert bundle\tor\tor.exe"
$torrcFile = "C:\Users\as157\OneDrive\Desktop\torr expert bundle\data\torrc"

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName         = $torExe
$psi.Arguments        = "-f `"$torrcFile`""
$psi.WorkingDirectory = Split-Path $torExe
$psi.UseShellExecute  = $false
$psi.CreateNoWindow   = $true
[System.Diagnostics.Process]::Start($psi) | Out-Null

Write-Host "Tor launched. Waiting 20s for bootstrap..."
Start-Sleep -Seconds 20

$socks = $false
$ctrl  = $false
try { $s = New-Object System.Net.Sockets.TcpClient("127.0.0.1", 9050); $s.Close(); $socks = $true } catch {}
try { $s = New-Object System.Net.Sockets.TcpClient("127.0.0.1", 9051); $s.Close(); $ctrl  = $true } catch {}

Write-Host "SOCKS 9050 : $socks"
Write-Host "Control 9051: $ctrl"

if ($socks -and $ctrl) {
    Write-Host "[OK] Tor is fully operational." -ForegroundColor Green
} else {
    Write-Host "[!!] One or both ports not yet ready. Check:" -ForegroundColor Yellow
    Write-Host "     C:\Users\as157\OneDrive\Desktop\torr expert bundle\data\notice.log"
}
