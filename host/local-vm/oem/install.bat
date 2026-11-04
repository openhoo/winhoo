@echo off
setlocal EnableExtensions
set "WINHOO_STATE=%ProgramData%\WinhooHost"
if not exist "%WINHOO_STATE%\" mkdir "%WINHOO_STATE%" >nul 2>&1
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "C:\OEM\Provision-WinhooContainerHost.ps1" >>"%WINHOO_STATE%\provisioning.log" 2>&1
set "WINHOO_RC=%ERRORLEVEL%"
endlocal & exit /b %WINHOO_RC%
