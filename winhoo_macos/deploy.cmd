@echo off
setlocal EnableExtensions EnableDelayedExpansion
wpeinit
set "SRC="
set "OUT="
for %%L in (C D E F G H I J K L M N O P Q R S T U V W Y Z) do (
  if exist %%L:\winhoo-owner.txt (
    if defined SRC goto :noowner
    set "SRC=%%L:"
  )
  if exist %%L:\WHSTATE.TAG (
    if defined OUT goto :noowner
    set "OUT=%%L:"
  )
)
if not defined SRC goto :noowner
if not defined OUT goto :noowner
if /i "%SRC%"=="%OUT%" goto :noowner
set "OWNER="
set "JOURNAL="
set /p OWNER=<"%SRC%\winhoo-owner.txt"
set /p JOURNAL=<"%OUT%\WHSTATE.TAG"
if not defined OWNER goto :noowner
if not defined JOURNAL goto :noowner
if not "!OWNER!"=="!JOURNAL!" goto :noowner
call :phase identify-disk
set "DISK="
for %%N in (0 1 2 3 4 5 6 7) do (
  >X:\winhoo-dp.txt echo select disk %%N
  >>X:\winhoo-dp.txt echo detail disk
  diskpart /s X:\winhoo-dp.txt >X:\winhoo-detail.txt
  copy /y X:\winhoo-detail.txt "%OUT%\disk%%N.txt" >nul
  find /i "QEMU NVMe Ctrl" X:\winhoo-detail.txt >nul && (
    if defined DISK goto :failed
    set "DISK=%%N"
  )
)
if not defined DISK goto :failed
>X:\winhoo-dp.txt echo select disk %DISK%
>>X:\winhoo-dp.txt echo list partition
diskpart /s X:\winhoo-dp.txt >"%OUT%\blank.txt"
find /i "There are no partitions on this disk" "%OUT%\blank.txt" >nul || goto :failed
call :phase partition-new-nvme
>X:\winhoo-dp.txt echo select disk %DISK%
>>X:\winhoo-dp.txt echo convert gpt
>>X:\winhoo-dp.txt echo create partition efi size=260
>>X:\winhoo-dp.txt echo format quick fs=fat32 label=WinHooEFI
>>X:\winhoo-dp.txt echo assign letter=S
>>X:\winhoo-dp.txt echo create partition msr size=16
>>X:\winhoo-dp.txt echo create partition primary
>>X:\winhoo-dp.txt echo format quick fs=ntfs label=WinHooOS
>>X:\winhoo-dp.txt echo assign letter=W
diskpart /s X:\winhoo-dp.txt >"%OUT%\disk.txt" || goto :failed
if not exist W:\ goto :failed
call :phase apply-windows
dism /Apply-Image /ImageFile:"%SRC%\sources\install.swm" /SWMFile:"%SRC%\sources\install*.swm" /Index:1 /ApplyDir:W:\ /LogPath:"%OUT%\dism.log" >"%OUT%\apply.txt" || goto :failed
call :phase stage-guest-tools
xcopy "%SRC%\sources\$OEM$\$1\*" W:\ /e /h /i /y /q >"%OUT%\oem.txt"
if errorlevel 1 goto :failed
if not exist W:\Windows\Panther mkdir W:\Windows\Panther
copy /y "%SRC%\winhoo-answer.xml" W:\Windows\Panther\unattend.xml >nul || goto :failed
dism /Image:W:\ /Add-Driver /Driver:W:\ProgramData\WinHoo\NetKVM\netkvm.inf /LogPath:"%OUT%\netkvm.log" >"%OUT%\driver.txt" || goto :failed
call :phase install-uefi-boot
bcdboot W:\Windows /s S: /f UEFI >"%OUT%\bcd.txt" || goto :failed
if not exist S:\EFI\Boot\bootaa64.efi goto :failed
call :phase image-applied
wpeutil shutdown
exit /b 0
:phase
>"%OUT%\phase.txt" echo %1
exit /b 0
:failed
copy /y "%OUT%\phase.txt" "%OUT%\failed.txt" >nul
call :phase failed
wpeutil shutdown
exit /b 1
:noowner
echo WinHoo: installer and receipt disk missing or ownership mismatch.
wpeutil shutdown
exit /b 1
