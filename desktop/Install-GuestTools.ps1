[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$expected = '19dcf8abc30f70c2eb6e87282601fd0476e4e0b19936f63d9c80be30db0796ed'
$root = Join-Path $env:ProgramData 'WinHoo\tools'
$status = Join-Path $root 'installed.json'
$startup = Join-Path ([Environment]::GetFolderPath('CommonStartup')) 'WinHoo-GuestTools.cmd'
$phase = 'start'
function Test-SerialDevice {
    $devices = @(Get-CimInstance Win32_PnPEntity -Filter "Service='VirtioSerial'" | Where-Object {
        $_.PNPDeviceID -match '^PCI\\VEN_1AF4&DEV_(1003|1043)&' -and $_.ConfigManagerErrorCode -eq 0
    })
    return $devices.Count -gt 0
}
function Install-SerialDriver([string]$DriverPath) {
    if (Test-SerialDevice) {
        [Console]::Out.WriteLine('Reusing the healthy VirtIO serial device')
        return
    }
    & pnputil.exe /add-driver $DriverPath /install
    $driverExit = $LASTEXITCODE
    # A successful package import alone does not attest a working device;
    # conversely, do not replace a healthy higher-ranked Windows driver.
    if (-not (Test-SerialDevice)) { throw "VirtIO serial device is not ready after driver installation (exit $driverExit)" }
}
function Publish-ToolsStatus([string]$State, [string]$Stage, [string]$Message = '') {
    $script:phase = $Stage
    $generation = $null
    try {
        $heartbeat = [IO.File]::ReadAllText('\\host.lan\Data\bootstrap\winhoo-owner-heartbeat')
        if ($heartbeat -match '^([a-f0-9]{32}) ') { $generation = $Matches[1] }
    } catch { }
    $json = ([ordered]@{schema='winhoo.guest-tools-status/1'; version='0.4.0'; ownerGeneration=$generation; status=$State; phase=$Stage; generatedUtc=[DateTime]::UtcNow.ToString('o'); error=if ($Message) {$Message} else {$null}} | ConvertTo-Json -Compress) + "`r`n"
    # Diagnostics must remain available before the agent or network is ready.
    foreach ($path in @((Join-Path $env:ProgramData 'WinHoo\bootstrap-status.json'), '\\host.lan\Data\bootstrap\winhoo-tools-status.json')) {
        try {
            [IO.Directory]::CreateDirectory((Split-Path -Parent $path)) | Out-Null
            $temporary = "$path.$([guid]::NewGuid().ToString('N')).tmp"
            [IO.File]::WriteAllText($temporary, $json, [Text.UTF8Encoding]::new($false))
            Move-Item -LiteralPath $temporary -Destination $path -Force
        } catch { [Console]::Error.WriteLine("Guest-tools status unavailable: $($_.Exception.Message)") }
    }
    [Console]::Out.WriteLine("WinHoo guest tools: $Stage ($State)")
}
try {
    Publish-ToolsStatus 'running' 'validate-administrator'
    if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'WinHoo guest tools require the dedicated VM administrator session'
    }
    Publish-ToolsStatus 'running' 'verify-installer'
    $installer = Join-Path $PSScriptRoot 'qemu-ga-x86_64.msi'
    if ((Get-Item -LiteralPath $installer).Length -ne 11909632 -or
        (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant() -cne $expected) {
        throw 'Pinned QEMU guest-agent installer size or SHA-256 mismatch'
    }
    [IO.Directory]::CreateDirectory($root) | Out-Null
    if (-not $PSScriptRoot.Equals($root, [StringComparison]::OrdinalIgnoreCase)) {
        Copy-Item -Path (Join-Path $PSScriptRoot '*') -Destination $root -Recurse -Force
    }
    & icacls.exe $root /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Guest-tools directory ACL configuration failed' }
    Publish-ToolsStatus 'running' 'install-serial-driver'
    Install-SerialDriver (Join-Path $root 'vioserial\vioser.inf')
    $service = Get-Service -Name QEMU-GA -ErrorAction SilentlyContinue
    if (-not $service) {
        Publish-ToolsStatus 'running' 'install-agent'
        $process = Start-Process -FilePath msiexec.exe -ArgumentList @('/i', ('"' + (Join-Path $root 'qemu-ga-x86_64.msi') + '"'), '/qn', '/norestart', '/L*v', ('"' + (Join-Path $root 'msi.log') + '"')) -Wait -PassThru
        if ($process.ExitCode -notin @(0, 3010)) { throw "QEMU guest-agent installation failed with exit $($process.ExitCode)" }
    }
    Publish-ToolsStatus 'running' 'start-agent'
    $registration = Get-CimInstance Win32_Service -Filter "Name='QEMU-GA'"
    if (-not $registration -or $registration.PathName -notmatch '^"?C:\\Program Files\\qemu-ga\\qemu-ga\.exe"?(?:\s|$)') {
        throw 'Unrecognized QEMU guest-agent service registration'
    }
    Set-Service -Name QEMU-GA -StartupType Automatic
    Start-Service -Name QEMU-GA
    [IO.File]::WriteAllText($status, (([ordered]@{schema='winhoo.guest-tools/1'; version='0.4.0'; sha256=$expected; installedUtc=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json -Compress) + "`r`n"), [Text.UTF8Encoding]::new($false))
    if (Test-Path -LiteralPath $startup) { Remove-Item -LiteralPath $startup -Force }
    Publish-ToolsStatus 'ready' 'ready'
    [Console]::Out.WriteLine('WinHoo guest tools ready; QEMU-GA uses the private VirtIO serial channel')
    exit 0
}
catch {
    Publish-ToolsStatus 'failed' $phase $_.Exception.Message
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
