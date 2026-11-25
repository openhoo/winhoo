@echo off
setlocal EnableExtensions
if not exist "%ProgramData%\WinHoo" mkdir "%ProgramData%\WinHoo"
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\OEM\winhoo-tools\Install-GuestTools.ps1 >>"%ProgramData%\WinHoo\guest-tools.log" 2>&1
if errorlevel 1 exit /b %ERRORLEVEL%
if exist C:\OEM\consumer-install.bat call C:\OEM\consumer-install.bat
exit /b %ERRORLEVEL%
