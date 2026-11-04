#Requires -RunAsAdministrator
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [switch] $EnableFeatures,
    [switch] $InstallDocker,
    [switch] $AcceptDockerLicense,
    [switch] $StartDocker,
    [string] $CacheDirectory = (Join-Path $env:TEMP 'winhoo-downloads')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not ($EnableFeatures -or $InstallDocker -or $StartDocker)) {
    throw 'Select -EnableFeatures, -InstallDocker, or -StartDocker; add -WhatIf to preview.'
}
if ($InstallDocker -and -not $AcceptDockerLicense) {
    throw '-InstallDocker requires -AcceptDockerLicense after reviewing the Docker Subscription Service Agreement.'
}
$os = Get-CimInstance Win32_OperatingSystem
$edition = (Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').EditionID
if ($os.ProductType -ne 1 -or [int]$os.BuildNumber -lt 22631 -or $os.OSArchitecture -ne '64-bit') {
    throw 'This helper targets x64 Windows 11 Pro/Enterprise 23H2 or newer. Provision a Windows Server container engine separately.'
}
if ($edition -notmatch '^(Professional|Enterprise)(N|S|SN|Workstation|WorkstationN)?$') {
    throw "Native Windows containers require Windows Pro or Enterprise; found edition '$edition'."
}
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
if ($cpu.Architecture -ne 9) { throw 'Native winhoo images require an AMD64/x64 Windows host.' }
$lock = Get-Content -LiteralPath (Join-Path $PSScriptRoot '..\dependencies.json') -Raw | ConvertFrom-Json
$featureNames = @('Containers', 'Microsoft-Hyper-V-All')
$restartNeeded = $false
foreach ($name in $featureNames) {
    $feature = Get-WindowsOptionalFeature -Online -FeatureName $name
    if ($feature.State.ToString() -match 'Pending') { $restartNeeded = $true }
    if ($feature.State -ne 'Enabled') {
        if (-not $EnableFeatures) { throw "Enable Windows feature '$name' first with -EnableFeatures." }
        if ($PSCmdlet.ShouldProcess($env:COMPUTERNAME, "Enable $name without restarting")) {
            $result = Enable-WindowsOptionalFeature -Online -FeatureName $name -All -NoRestart
            if ($result.RestartNeeded) { $restartNeeded = $true }
        }
    }
}
if ($restartNeeded) {
    [ordered]@{ rebootRequired = $true; nextStep = 'Restart Windows, then rerun the installer step.' } | ConvertTo-Json
    exit 3010
}

$desktopPath = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
$dockerPath = 'C:\Program Files\Docker\Docker\resources\bin\docker.exe'
if ($InstallDocker) {
    if (Test-Path -LiteralPath $desktopPath) {
        throw 'Docker Desktop is already installed. Review its version and installation mode before replacing it.'
    }
    if ($PSCmdlet.ShouldProcess($env:COMPUTERNAME, "Install Docker Desktop $($lock.dockerDesktop.version) for all users with the Windows backend")) {
        New-Item -ItemType Directory -Path $CacheDirectory -Force | Out-Null
        $installer = Join-Path $CacheDirectory "DockerDesktop-$($lock.dockerDesktop.version).exe"
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        if (-not (Test-Path -LiteralPath $installer)) {
            Invoke-WebRequest -UseBasicParsing -Uri $lock.dockerDesktop.url -OutFile $installer
        }
        if ((Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash -ne $lock.dockerDesktop.sha256) {
            throw 'Docker Desktop installer SHA256 does not match dependencies.json.'
        }
        $signature = Get-AuthenticodeSignature -LiteralPath $installer
        if ($signature.Status -ne 'Valid') { throw "Docker installer signature is not valid: $($signature.Status)" }
        $process = Start-Process -FilePath $installer -ArgumentList @(
            'install', '--quiet', '--accept-license', '--backend=windows', '--always-run-service'
        ) -PassThru -Wait
        if ($process.ExitCode -in @(1641, 3010)) {
            [ordered]@{ rebootRequired = $true; nextStep = 'Restart Windows, then run -StartDocker.' } | ConvertTo-Json
            exit 3010
        }
        if ($process.ExitCode -ne 0) { throw "Docker Desktop installation failed with exit code $($process.ExitCode)." }
        if (-not (Test-Path -LiteralPath $desktopPath)) { throw 'Installer returned success, but the all-users Docker Desktop installation is absent.' }
    }
}
if ($StartDocker -and $PSCmdlet.ShouldProcess($env:COMPUTERNAME, 'Start Docker Desktop and verify its Windows engine')) {
    if (-not (Test-Path -LiteralPath $desktopPath)) { throw 'Install all-users Docker Desktop before starting it.' }
    Start-Service -Name com.docker.service
    Start-Process -FilePath $desktopPath | Out-Null
    $deadline = [DateTime]::UtcNow.AddMinutes(3)
    $lastOutput = ''
    while ([DateTime]::UtcNow -lt $deadline) {
        # Windows PowerShell 5.1 treats redirected native stderr as PowerShell errors.
        $savedPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $lastOutput = (& $dockerPath --host npipe:////./pipe/docker_engine info --format '{{.OSType}}' 2>&1) -join "`n"
            $probeExit = $LASTEXITCODE
        }
        finally { $ErrorActionPreference = $savedPreference }
        if ($probeExit -eq 0) {
            if ($lastOutput.Trim() -ne 'windows') { throw 'Docker is using the Linux engine. Explicitly select Windows containers in Docker Desktop.' }
            [ordered]@{ rebootRequired = $false; windowsEngineReady = $true } | ConvertTo-Json
            exit 0
        }
        Start-Sleep -Seconds 2
    }
    throw "The Windows Docker engine did not become ready within three minutes. Last response: $lastOutput"
}
if (-not $WhatIfPreference) { & (Join-Path $PSScriptRoot 'Test-WinhooHost.ps1') }
