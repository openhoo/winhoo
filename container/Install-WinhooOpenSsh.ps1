[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9A-Za-z.\-]+$')]
    [string] $Version,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9A-Fa-f]{64}$')]
    [string] $Sha256
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$archive = Join-Path $env:TEMP 'winhoo-openssh.zip'
$expanded = Join-Path $env:TEMP 'winhoo-openssh'
$destination = 'C:\Program Files\OpenSSH'

try {
    $uri = "https://github.com/PowerShell/Win32-OpenSSH/releases/download/$Version/OpenSSH-Win64.zip"
    Invoke-WebRequest -UseBasicParsing -Uri $uri -OutFile $archive
    $actual = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash
    if ($actual -ne $Sha256) {
        throw "OpenSSH archive checksum mismatch: expected $Sha256, got $actual"
    }

    Expand-Archive -LiteralPath $archive -DestinationPath $expanded -Force
    Move-Item -LiteralPath (Join-Path $expanded 'OpenSSH-Win64') -Destination $destination
    & "$destination\install-sshd.ps1" -Confirm:$false
    if (-not (Get-Service -Name sshd -ErrorAction Stop)) {
        throw 'OpenSSH installation did not register the sshd service'
    }
    Set-Service -Name sshd -StartupType Manual
    Set-Service -Name ssh-agent -StartupType Disabled

    New-Item -Path 'HKLM:\SOFTWARE\OpenSSH' -Force | Out-Null
    # An outer PowerShell -Command maps a failing native child's exit to 1.
    # cmd passes the exact code through; the controller starts PowerShell explicitly.
    New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell `
        -Value 'C:\Windows\System32\cmd.exe' `
        -PropertyType String -Force | Out-Null
    New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShellCommandOption `
        -Value '/c' -PropertyType String -Force | Out-Null

    foreach ($path in @('C:\work', 'C:\winhoo\access', 'C:\winhoo\logs', 'C:\winhoo\downloads')) {
        New-Item -ItemType Directory -Path $path -Force | Out-Null
    }

    # install-sshd registers the service without starting it: host keys are runtime state.
    if (Get-ChildItem -Path 'C:\ProgramData\ssh' -Filter 'ssh_host_*_key' -ErrorAction SilentlyContinue) {
        throw 'Refusing an image build that contains SSH host private keys'
    }
    [ordered]@{
        schemaVersion = 1
        image = 'winhoo'
        openSshVersion = $Version
        openSshArchiveSha256 = $Sha256.ToLowerInvariant()
        user = 'winhoo'
        shell = 'cmd.exe'
        commandRuntime = 'Windows PowerShell 5.1'
    } | ConvertTo-Json | Set-Content -LiteralPath 'C:\winhoo\image.json' -Encoding UTF8
}
finally {
    Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $expanded -Recurse -Force -ErrorAction SilentlyContinue
}
