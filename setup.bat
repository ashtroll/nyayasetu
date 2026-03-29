@echo off
:: =============================================================================
:: setup.bat  —  One-click launcher for setup.ps1
:: Double-click this file OR run it from CMD to kick off the full setup.
:: =============================================================================
echo.
echo  [LSSE] Launching automated setup script...
echo.

:: Run PowerShell with execution-policy bypass so no manual config is needed
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"

echo.
pause
