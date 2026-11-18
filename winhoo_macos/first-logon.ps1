$ErrorActionPreference = 'Stop'
$root = Join-Path $env:ProgramData 'WinHoo'
Start-Transcript -Path (Join-Path $root 'first-logon.log') -Append
try {
    $owned = Get-Content "$root\owner.json" -Raw | ConvertFrom-Json
    if ($env:USERNAME -ine $owned.account) { throw 'Unexpected interactive user' }
    $ssh = 'C:\Program Files\WinHoo-OpenSSH'
    Copy-Item -LiteralPath "$root\OpenSSH-ARM64" -Destination $ssh -Recurse -Force
    & "$ssh\install-sshd.ps1"
    $state = "$env:ProgramData\ssh"
    New-Item -ItemType Directory -Force $state | Out-Null
    Copy-Item "$root\host_ed25519" "$state\ssh_host_ed25519_key" -Force
    Copy-Item "$root\host_ed25519.pub" "$state\ssh_host_ed25519_key.pub" -Force
    Copy-Item "$root\client.pub" "$state\administrators_authorized_keys" -Force
    foreach ($file in @('ssh_host_ed25519_key','administrators_authorized_keys')) {
        & icacls.exe "$state\$file" /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null
        if ($LASTEXITCODE) { throw 'Failed to protect guest SSH files' }
        & icacls.exe "$state\$file" /setowner '*S-1-5-32-544' | Out-Null
        if ($LASTEXITCODE) { throw 'Failed to bind guest SSH file ownership' }
    }
    @"
HostKey __PROGRAMDATA__/ssh/ssh_host_ed25519_key
AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys
AllowUsers $($owned.account.ToLowerInvariant())
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
AllowTcpForwarding no
AllowAgentForwarding no
Subsystem sftp sftp-server.exe
"@ | Set-Content -Encoding ascii "$state\sshd_config"
    & "$ssh\sshd.exe" -t
    if ($LASTEXITCODE) { throw 'Invalid SSH configuration' }
    Remove-Item "$root\host_ed25519" -Force
    if (-not (Get-NetFirewallRule -Name WinHoo-Local-SSH -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name WinHoo-Local-SSH -DisplayName 'WinHoo local guest control' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 22 -Profile Any | Out-Null
    }
    Set-Service sshd -StartupType Automatic
    Restart-Service sshd
    & powercfg.exe /change standby-timeout-ac 0
    & powercfg.exe /change monitor-timeout-ac 0
    Write-Output 'WinHoo guest provisioned'
} finally { Stop-Transcript }
