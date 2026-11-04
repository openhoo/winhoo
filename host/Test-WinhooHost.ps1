[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$os = Get-CimInstance Win32_OperatingSystem
$computer = Get-CimInstance Win32_ComputerSystem
$featureNames = @('Containers', 'Microsoft-Hyper-V-All')
$features = @{}
foreach ($name in $featureNames) {
    try {
        $features[$name] = (Get-WindowsOptionalFeature -Online -FeatureName $name).State.ToString()
    }
    catch { $features[$name] = 'Unknown: ' + $_.Exception.Message }
}

$docker = Get-Command docker.exe -ErrorAction SilentlyContinue
$dockerPath = if ($docker) { $docker.Source } else { 'C:\Program Files\Docker\Docker\resources\bin\docker.exe' }
$engine = $null
$engineError = $null
if (Test-Path -LiteralPath $dockerPath) {
    try {
        $output = & $dockerPath --host npipe:////./pipe/docker_engine info --format '{{json .}}' 2>&1
        if ($LASTEXITCODE -ne 0) { throw ($output -join "`n") }
        $info = ($output -join "`n") | ConvertFrom-Json
        $engine = [ordered]@{
            osType = $info.OSType
            architecture = $info.Architecture
            version = $info.ServerVersion
            operatingSystem = $info.OperatingSystem
        }
    }
    catch { $engineError = $_.Exception.Message }
}
else { $engineError = 'Docker CLI is not installed.' }

$rebootPending = (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') -or
    (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') -or
    (@($features.Values | Where-Object { $_ -match 'Pending' }).Count -gt 0)
$ready = ($features['Containers'] -eq 'Enabled') -and
    ($features['Microsoft-Hyper-V-All'] -eq 'Enabled') -and
    $computer.HypervisorPresent -and
    ($null -ne $engine) -and ($engine.osType -eq 'windows') -and (-not $rebootPending)

[ordered]@{
    schema = 1
    computerName = $env:COMPUTERNAME
    os = $os.Caption
    version = $os.Version
    architecture = $os.OSArchitecture
    isAdministrator = $isAdmin
    ramBytes = $computer.TotalPhysicalMemory
    logicalProcessors = $computer.NumberOfLogicalProcessors
    hypervisorPresent = $computer.HypervisorPresent
    features = $features
    rebootRequired = [bool]$rebootPending
    engine = $engine
    engineError = $engineError
    readyForHyperVContainers = [bool]$ready
} | ConvertTo-Json -Depth 5
if (-not $ready) { exit 1 }
