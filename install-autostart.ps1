# Starts Web-API-Poster automatically when you log in to Windows (PowerShell 5.1, no admin rights needed).
# Install: powershell -ExecutionPolicy Bypass -File .\install-autostart.ps1
# Remove:  powershell -ExecutionPolicy Bypass -File .\install-autostart.ps1 -Remove
# A shortcut is put into your Startup folder; the server log goes to data\server.log.
param([switch]$Remove)
$ErrorActionPreference = "Stop"

$startup = [Environment]::GetFolderPath("Startup")
$link = Join-Path $startup "Web-API-Poster.lnk"

if ($Remove) {
    if (Test-Path $link) { Remove-Item $link }
    Write-Host "Autostart removed."
    exit 0
}

$runScript = Join-Path $PSScriptRoot "run.ps1"
$logFile = Join-Path $PSScriptRoot "data\server.log"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($link)
$shortcut.TargetPath = "powershell.exe"
$shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Minimized -File `"$runScript`" -LogFile `"$logFile`""
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.WindowStyle = 7  # minimized
$shortcut.Description = "Web-API-Poster"
$shortcut.Save()

Write-Host "Autostart installed: $link"
Write-Host "Web-API-Poster will start (minimized) every time you log in. Log: $logFile"
Write-Host "Keep the PC awake at publication times: Settings > System > Power & sleep > Sleep: Never."
