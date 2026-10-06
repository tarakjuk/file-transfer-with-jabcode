@echo off
rem Deploy receiver web page to VibeDrop (needs Node.js)
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy_receiver.ps1"
pause
