[CmdletBinding(DefaultParameterSetName = 'Download')]
param(
    [Parameter(Mandatory = $true, ParameterSetName = 'Download')]
    [uri] $Uri,

    [Parameter(Mandatory = $true, ParameterSetName = 'Local')]
    [string] $Path,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9A-Fa-f]{64}$')]
    [string] $Sha256,

    [Parameter(Mandatory = $true)]
    [ValidateSet('msi', 'exe')]
    [string] $Type,

    [ValidatePattern('^[0-9A-Za-z][0-9A-Za-z_.-]{0,79}$')]
    [string] $Name = 'package',

    [string] $Arguments = '',

    [int[]] $AcceptExitCodes = @(0)
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssffffZ')
$id = "$Name-$stamp-$([Guid]::NewGuid().ToString('N').Substring(0, 8))"
$logRoot = 'C:\winhoo\logs'
$downloadRoot = 'C:\winhoo\downloads'
$downloaded = $false
$processExitCode = $null
$result = [ordered]@{
    schemaVersion = 1
    package = $Name
    startedAt = [DateTime]::UtcNow.ToString('o')
    sha256 = $Sha256.ToLowerInvariant()
    type = $Type
    status = 'failed'
    exitCode = $null
}

try {
    New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
    if ($PSCmdlet.ParameterSetName -eq 'Download') {
        if ($Uri.Scheme -ne 'https') { throw 'Installer downloads require HTTPS' }
        New-Item -ItemType Directory -Path $downloadRoot -Force | Out-Null
        $Path = Join-Path $downloadRoot "$id.$Type"
        $downloaded = $true
        Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $Path
    }
    $Path = (Resolve-Path -LiteralPath $Path).ProviderPath
    if ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -ne $Sha256) {
        throw 'Installer checksum mismatch; no installer was executed'
    }

    $start = @{
        Wait = $true
        PassThru = $true
        NoNewWindow = $true
    }
    if ($Type -eq 'msi') {
        $start.FilePath = 'C:\Windows\System32\msiexec.exe'
        $installerLog = Join-Path $logRoot "$id.msi.log"
        $start.ArgumentList = "/i `"$Path`" /qn /norestart /L*v `"$installerLog`" $Arguments"
        $result.log = $installerLog
    }
    else {
        $start.FilePath = $Path
        if (-not [string]::IsNullOrWhiteSpace($Arguments)) { $start.ArgumentList = $Arguments }
        $start.RedirectStandardOutput = Join-Path $logRoot "$id.stdout.log"
        $start.RedirectStandardError = Join-Path $logRoot "$id.stderr.log"
        $result.stdout = $start.RedirectStandardOutput
        $result.stderr = $start.RedirectStandardError
    }
    $process = Start-Process @start
    $processExitCode = $process.ExitCode
    $result.exitCode = $processExitCode
    if ($processExitCode -in @(1641, 3010)) {
        $result.status = 'reboot-required'
        throw "Installer requested a reboot (exit $processExitCode); installation is incomplete in a Windows container"
    }
    if ($processExitCode -notin $AcceptExitCodes) {
        throw "Installer failed with exit code $processExitCode"
    }
    $result.status = 'installed'
}
catch {
    $result.error = $_.Exception.Message
}
finally {
    $result.finishedAt = [DateTime]::UtcNow.ToString('o')
    $json = $result | ConvertTo-Json -Depth 4
    if (Test-Path -LiteralPath $logRoot) {
        $json | Set-Content -LiteralPath (Join-Path $logRoot "$id.result.json") -Encoding UTF8
    }
    Write-Output $json
    if ($downloaded) { Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue }
}

if ($result.status -ne 'installed') { exit 1 }
exit 0
