@echo off
:: Start Tor Expert Bundle in the background
:: Called by start_services.ps1 and setup_tor.ps1

set BIN=%~dp0
set TORRC=%BIN%torrc

echo [Tor] Starting tor.exe with %TORRC%
start "" /B "%BIN%tor.exe" -f "%TORRC%"
echo [Tor] Launched. Check %BIN%tor.log for status.
