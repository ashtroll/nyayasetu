@echo off
:: =============================================================================
:: mount_gdrive.bat  –  Mount Google Drive via Rclone (Windows)
:: =============================================================================
:: Prerequisites:
::   1. rclone for Windows installed  →  https://rclone.org/install/
::      Add rclone.exe to your PATH, or place it next to this script.
::   2. rclone remote configured  →  open CMD and run:  rclone config
::      • Choose "Google Drive", name the remote:  lsse_gdrive
::   3. WinFsp installed (required for rclone mount on Windows)
::      →  https://github.com/winfsp/winfsp/releases
:: =============================================================================

setlocal

set REMOTE_NAME=lsse_gdrive
:: Mount as a virtual drive letter  (change X: to any free letter you prefer)
set MOUNT_DRIVE=X:

echo [LSSE] Mounting Google Drive as %MOUNT_DRIVE% ...

rclone mount %REMOTE_NAME%: %MOUNT_DRIVE% ^
    --vfs-cache-mode full ^
    --vfs-cache-max-size 100G ^
    --tpslimit 10 ^
    --drive-chunk-size 64M ^
    --allow-non-empty ^
    --network-mode

:: rclone mount blocks until the drive is unmounted.
:: To unmount, close this window or press Ctrl-C, then run:
::   taskkill /IM rclone.exe /F
echo [LSSE] Google Drive unmounted.
endlocal
