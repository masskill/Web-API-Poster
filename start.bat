@echo off
rem Double-click to start Web-API-Poster. The browser opens by itself; close this window to stop.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" -OpenBrowser
pause
