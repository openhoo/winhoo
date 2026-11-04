[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$sshRoot = 'C:\ProgramData\ssh'
$sshBin = 'C:\Program Files\OpenSSH'
$stagedKeys = 'C:\winhoo\access\authorized_keys'
$stream = $null
$reader = $null

function Set-PrivateDirectoryAcl {
    param([string] $Path)
    $acl = New-Object System.Security.AccessControl.DirectorySecurity
    $acl.SetSecurityDescriptorSddlForm('O:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)')
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Set-PrivateFileAcl {
    param([string] $Path)
    $acl = New-Object System.Security.AccessControl.FileSecurity
    $acl.SetSecurityDescriptorSddlForm('O:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)')
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Initialize-WinhooUser {
    $user = Get-LocalUser -Name 'winhoo' -ErrorAction SilentlyContinue
    if (-not $user) {
        $random = New-Object byte[] 32
        $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
        $password = New-Object Security.SecureString
        try {
            $generator.GetBytes($random)
            foreach ($character in 'A!z9-'.ToCharArray()) { $password.AppendChar($character) }
            foreach ($value in $random) {
                foreach ($character in $value.ToString('x2').ToCharArray()) { $password.AppendChar($character) }
            }
            $password.MakeReadOnly()
            # ContainerAdministrator is a special container identity, not a normal SAM user.
            $user = New-LocalUser -Name 'winhoo' -Password $password -AccountNeverExpires `
                -PasswordNeverExpires -UserMayNotChangePassword -Description 'winhoo key-only SSH operator'
        }
        finally {
            $generator.Dispose()
            [Array]::Clear($random, 0, $random.Length)
            $password.Dispose()
        }
    }
    if (-not $user.Enabled) { Enable-LocalUser -Name 'winhoo' }
    $administratorSid = New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')
    $membership = @(Get-LocalGroupMember -SID $administratorSid | Where-Object { $_.SID.Value -eq $user.SID.Value })
    if ($membership.Count -eq 0) {
        Add-LocalGroupMember -SID $administratorSid -Member $user
    }
}

try {
    if (-not (Test-Path -LiteralPath $stagedKeys -PathType Leaf)) {
        throw 'Stage authorized_keys at C:\winhoo\access\authorized_keys before starting the container, or mount its directory read-only'
    }
    if ((Get-Item -LiteralPath $stagedKeys).Length -gt 131072) {
        throw 'The authorized_keys input exceeds 128 KiB'
    }
    $keys = [IO.File]::ReadAllLines($stagedKeys) | Where-Object { $_.Trim() -and -not $_.TrimStart().StartsWith('#') }
    if (-not $keys) {
        throw 'The staged authorized_keys file contains no public keys'
    }
    foreach ($key in $keys) {
        if ($key -notmatch '^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)) [A-Za-z0-9+/]+={0,3}([ \t].*)?$') {
            throw 'authorized_keys must contain plain SSH public keys, one per line'
        }
    }
    Initialize-WinhooUser

    New-Item -ItemType Directory -Path $sshRoot -Force | Out-Null
    Set-PrivateDirectoryAcl -Path $sshRoot
    $authorized = Join-Path $sshRoot 'administrators_authorized_keys'
    [IO.File]::WriteAllText($authorized, (($keys -join "`n") + "`n"), [Text.Encoding]::ASCII)
    Set-PrivateFileAcl -Path $authorized
    & "$sshBin\ssh-keygen.exe" -l -f $authorized
    if ($LASTEXITCODE -ne 0) { throw 'OpenSSH rejected the staged public keys' }

    Copy-Item -LiteralPath 'C:\winhoo\container\sshd_config' -Destination (Join-Path $sshRoot 'sshd_config') -Force
    Set-PrivateFileAcl -Path (Join-Path $sshRoot 'sshd_config')
    & "$sshBin\ssh-keygen.exe" -A
    if ($LASTEXITCODE -ne 0) { throw 'SSH host key generation failed' }
    foreach ($key in Get-ChildItem -LiteralPath $sshRoot -Filter 'ssh_host_*_key' -File) {
        Set-PrivateFileAcl -Path $key.FullName
    }
    & "$sshBin\sshd.exe" -t -f (Join-Path $sshRoot 'sshd_config')
    if ($LASTEXITCODE -ne 0) { throw 'SSH server configuration validation failed' }

    $logRoot = Join-Path $sshRoot 'logs'
    New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
    Set-PrivateDirectoryAcl -Path $logRoot
    $log = Join-Path $logRoot 'sshd.log'
    if (-not (Test-Path -LiteralPath $log)) {
        [IO.File]::WriteAllText($log, '')
    }
    Set-PrivateFileAcl -Path $log
    $share = [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
    $stream = [IO.File]::Open($log, [IO.FileMode]::Open, [IO.FileAccess]::Read, $share)
    [void] $stream.Seek(0, [IO.SeekOrigin]::End)
    $reader = New-Object IO.StreamReader($stream)

    # LocalSystem supplies the Windows token privileges required by authenticated sessions.
    Start-Service -Name sshd
    Write-Output 'winhoo: SSH service started; public-key authentication only; user=winhoo'
    while ((Get-Service -Name sshd).Status -eq 'Running') {
        while (($line = $reader.ReadLine()) -ne $null) {
            Write-Output $line
        }
        Start-Sleep -Milliseconds 500
    }
    throw 'The SSH service stopped unexpectedly'
}
catch {
    Write-Error -ErrorRecord $_ -ErrorAction Continue
    exit 1
}
finally {
    if ($reader) { $reader.Dispose() }
    elseif ($stream) { $stream.Dispose() }
    Stop-Service -Name sshd -ErrorAction SilentlyContinue
}
