$binDir = $PSScriptRoot
Expand-Archive -Path "$binDir\rclone.zip" -DestinationPath "$binDir\rclone_tmp" -Force
$exe = Get-ChildItem "$binDir\rclone_tmp" -Recurse -Filter "rclone.exe" | Select-Object -First 1
Copy-Item $exe.FullName "$binDir\rclone.exe" -Force
Remove-Item "$binDir\rclone_tmp" -Recurse -Force
Remove-Item "$binDir\rclone.zip" -Force
Write-Host "rclone.exe ready at $binDir\rclone.exe"
