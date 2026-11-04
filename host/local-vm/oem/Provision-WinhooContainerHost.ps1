#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [switch] $Resume,
    [string] $DependenciesPath = (Join-Path $PSScriptRoot 'dependencies.json')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$stateRoot = 'C:\ProgramData\WinhooHost'
$sharedStatus = '\\host.lan\Data\bootstrap\host-status.json'
$taskName = 'WinhooHost-ContinueProvisioning'
$operatorName = 'WinhooHost'
$dockerRoot = 'C:\Program Files\Docker'
$sshRoot = 'C:\ProgramData\ssh'
$sshBin = 'C:\Program Files\OpenSSH'
$phase = 'validate-host'
$mutex = New-Object Threading.Mutex($false, 'Global\WinhooHostProvisioning')
$ownsMutex = $false
$lock = $null
$lockHash = $null
$instanceId = $null
$os = $null

function Write-AtomicJson {
    param([string] $Path, $Value)
    [void] [IO.Directory]::CreateDirectory((Split-Path -Parent $Path))
    $temporary = "$Path.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        $utf8 = New-Object Text.UTF8Encoding($false)
        [IO.File]::WriteAllText($temporary, (($Value | ConvertTo-Json -Depth 8) + "`n"), $utf8)
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    }
    finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

function Publish-Status {
    param([string] $Status, [string] $Phase, [string] $ErrorMessage = '', $Details = $null, [switch] $RequireShared)
    $receipt = [ordered]@{
        schemaVersion = 1
        role = 'winhoo-container-host'
        instanceId = $script:instanceId
        status = $Status
        phase = $Phase
        generatedAt = [DateTime]::UtcNow.ToString('o')
        dependencyLockSha256 = $script:lockHash
        error = if ($ErrorMessage) { $ErrorMessage } else { $null }
        user = $script:operatorName
        sshHostPublicKey = if ($Details) { $Details.sshHostPublicKey } else { $null }
        dockerVersion = if ($Details) { $Details.dockerVersion } else { $null }
        details = $Details
    }
    Write-AtomicJson -Path (Join-Path $script:stateRoot 'host-status.json') -Value $receipt
    try {
        Write-AtomicJson -Path $script:sharedStatus -Value $receipt
    }
    catch {
        if ($RequireShared) { throw "Could not publish the host receipt to the shared folder: $($_.Exception.Message)" }
        Write-Output ('winhoo host: shared status is not available yet: ' + $_.Exception.Message)
    }
    return $receipt
}

function Set-PrivateDirectoryAcl {
    param([string] $Path)
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetSecurityDescriptorSddlForm('O:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)')
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Set-PrivateFileAcl {
    param([string] $Path)
    $acl = New-Object Security.AccessControl.FileSecurity
    $acl.SetSecurityDescriptorSddlForm('O:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)')
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Get-PinnedArchive {
    param([string] $Name, $Spec)
    if (-not $Spec.version -or $Spec.version -notmatch '^[0-9A-Za-z.\-]+$' -or
        $Spec.sha256 -notmatch '^[0-9A-Fa-f]{64}$' -or ([uri] $Spec.url).Scheme -ne 'https') {
        throw "The dependency lock does not contain a valid pinned $Name archive"
    }
    $cache = Join-Path $script:stateRoot 'cache'
    [void] [IO.Directory]::CreateDirectory($cache)
    $archive = Join-Path $cache "$Name-$($Spec.version).zip"
    if ((Test-Path -LiteralPath $archive) -and
        ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -eq $Spec.sha256)) {
        return $archive
    }
    $temporary = "$archive.download"
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $Spec.url -TimeoutSec 1800 -OutFile $temporary
        if ((Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash -ne $Spec.sha256) {
            throw "$Name archive checksum mismatch"
        }
        Move-Item -LiteralPath $temporary -Destination $archive -Force
    }
    finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
    return $archive
}

function Initialize-Operator {
    $user = Get-LocalUser -Name $script:operatorName -ErrorAction SilentlyContinue
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
            $user = New-LocalUser -Name $script:operatorName -Password $password -AccountNeverExpires `
                -PasswordNeverExpires -UserMayNotChangePassword -Description 'winhoo local container host operator'
        }
        finally {
            $generator.Dispose()
            [Array]::Clear($random, 0, $random.Length)
            $password.Dispose()
        }
    }
    if (-not $user.Enabled) { Enable-LocalUser -Name $script:operatorName }
    Set-LocalUser -Name $script:operatorName -PasswordNeverExpires $true
    $groupSid = New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')
    if (@(Get-LocalGroupMember -SID $groupSid | Where-Object { $_.SID.Value -eq $user.SID.Value }).Count -eq 0) {
        Add-LocalGroupMember -SID $groupSid -Member $user
    }
}

function Install-OpenSsh {
    $markerPath = Join-Path $script:stateRoot 'openssh-installed.json'
    $intentPath = Join-Path $script:stateRoot 'openssh-installing.json'
    if (-not (Test-Path -LiteralPath $markerPath)) {
        if ((Test-Path -LiteralPath $intentPath) -and
            ((Get-Content -LiteralPath $intentPath -Raw | ConvertFrom-Json).sha256 -ne $script:lock.openSsh.sha256)) {
            throw 'The pending OpenSSH installation uses a different dependency lock'
        }
        if (-not (Test-Path -LiteralPath $intentPath) -and
            ((Get-Service -Name sshd -ErrorAction SilentlyContinue) -or (Test-Path -LiteralPath $script:sshBin))) {
            throw 'Refusing to replace an OpenSSH installation not owned by this bootstrap'
        }
        $archive = Get-PinnedArchive -Name 'openssh' -Spec $script:lock.openSsh
        $expanded = Join-Path $script:stateRoot 'expand-openssh'
        try {
            Write-AtomicJson -Path $intentPath -Value $script:lock.openSsh
            Expand-Archive -LiteralPath $archive -DestinationPath $expanded -Force
            Stop-Service -Name sshd -ErrorAction SilentlyContinue
            Stop-Service -Name ssh-agent -ErrorAction SilentlyContinue
            [void] [IO.Directory]::CreateDirectory($script:sshBin)
            Copy-Item -Path (Join-Path $expanded 'OpenSSH-Win64\*') -Destination $script:sshBin -Recurse -Force
            & "$script:sshBin\install-sshd.ps1" -Confirm:$false
            if (-not (Get-Service -Name sshd -ErrorAction Stop)) { throw 'OpenSSH service registration failed' }
            Write-AtomicJson -Path $markerPath -Value $script:lock.openSsh
            Remove-Item -LiteralPath $intentPath -Force
        }
        finally {
            Remove-Item -LiteralPath $expanded -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    $installed = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
    if ($installed.sha256 -ne $script:lock.openSsh.sha256) { throw 'OpenSSH lock changed; create a fresh local VM' }

    [void] [IO.Directory]::CreateDirectory($script:sshRoot)
    Set-PrivateDirectoryAcl -Path $script:sshRoot
    $authorized = Join-Path $script:sshRoot 'administrators_authorized_keys'
    Copy-Item -LiteralPath (Join-Path $script:stateRoot 'authorized_keys') -Destination $authorized -Force
    Set-PrivateFileAcl -Path $authorized
    & "$script:sshBin\ssh-keygen.exe" -l -f $authorized | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'The SSH public-key file is invalid' }
    & "$script:sshBin\ssh-keygen.exe" -A
    if ($LASTEXITCODE -ne 0) { throw 'SSH host key generation failed' }
    foreach ($key in Get-ChildItem -LiteralPath $script:sshRoot -Filter 'ssh_host_*_key' -File) {
        Set-PrivateFileAcl -Path $key.FullName
    }
    $configuration = @'
Port 22
AddressFamily inet
ListenAddress 0.0.0.0
HostKey C:/ProgramData/ssh/ssh_host_ed25519_key
HostKey C:/ProgramData/ssh/ssh_host_rsa_key
AuthorizedKeysFile C:/ProgramData/ssh/administrators_authorized_keys
AllowUsers winhoohost
AuthenticationMethods publickey
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
StrictModes yes
AllowAgentForwarding no
AllowTcpForwarding local
GatewayPorts no
PermitTunnel no
X11Forwarding no
PrintMotd no
SyslogFacility LOCAL0
LogLevel VERBOSE
Subsystem sftp "C:/Program Files/OpenSSH/sftp-server.exe"
'@
    $configPath = Join-Path $script:sshRoot 'sshd_config'
    [IO.File]::WriteAllText($configPath, $configuration, [Text.Encoding]::ASCII)
    Set-PrivateFileAcl -Path $configPath
    & "$script:sshBin\sshd.exe" -t -f $configPath
    if ($LASTEXITCODE -ne 0) { throw 'SSH server configuration is invalid' }
    New-Item -Path 'HKLM:\SOFTWARE\OpenSSH' -Force | Out-Null
    New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell -PropertyType String `
        -Value 'C:\Windows\System32\cmd.exe' -Force | Out-Null
    New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShellCommandOption -PropertyType String -Value '/c' -Force | Out-Null
    Set-Service -Name sshd -StartupType Automatic
    Set-Service -Name ssh-agent -StartupType Disabled
    $firewallName = 'WinhooHost-OpenSSH'
    if (-not (Get-NetFirewallRule -Name $firewallName -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name $firewallName -DisplayName 'Winhoo host key-only SSH' -Enabled True `
            -Direction Inbound -Action Allow -Protocol TCP -LocalPort 22 -Profile Any `
            -Program "$script:sshBin\sshd.exe" | Out-Null
    }
    else { Set-NetFirewallRule -Name $firewallName -Enabled True | Out-Null }
}

function Install-DockerEngine {
    $markerPath = Join-Path $script:stateRoot 'docker-installed.json'
    $intentPath = Join-Path $script:stateRoot 'docker-installing.json'
    if (-not (Test-Path -LiteralPath $markerPath)) {
        if ((Test-Path -LiteralPath $intentPath) -and
            ((Get-Content -LiteralPath $intentPath -Raw | ConvertFrom-Json).sha256 -ne $script:lock.dockerEngine.sha256)) {
            throw 'The pending Docker installation uses a different dependency lock'
        }
        if (-not (Test-Path -LiteralPath $intentPath) -and
            ((Get-Service -Name docker -ErrorAction SilentlyContinue) -or (Test-Path -LiteralPath $script:dockerRoot))) {
            throw 'Refusing to replace a Docker installation not owned by this bootstrap'
        }
        $archive = Get-PinnedArchive -Name 'docker' -Spec $script:lock.dockerEngine
        $expanded = Join-Path $script:stateRoot 'expand-docker'
        try {
            Write-AtomicJson -Path $intentPath -Value $script:lock.dockerEngine
            Expand-Archive -LiteralPath $archive -DestinationPath $expanded -Force
            $payload = Join-Path $expanded 'docker'
            foreach ($binary in @('docker.exe', 'dockerd.exe')) {
                if (-not (Test-Path -LiteralPath (Join-Path $payload $binary))) { throw "Docker archive is missing $binary" }
            }
            Stop-Service -Name docker -ErrorAction SilentlyContinue
            [void] [IO.Directory]::CreateDirectory($script:dockerRoot)
            Copy-Item -Path (Join-Path $payload '*') -Destination $script:dockerRoot -Recurse -Force
            if (-not (Get-Service -Name docker -ErrorAction SilentlyContinue)) {
                & "$script:dockerRoot\dockerd.exe" --register-service
                if ($LASTEXITCODE -ne 0) { throw 'Docker service registration failed' }
            }
            Write-AtomicJson -Path $markerPath -Value $script:lock.dockerEngine
            Remove-Item -LiteralPath $intentPath -Force
        }
        finally {
            Remove-Item -LiteralPath $expanded -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    $installed = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
    if ($installed.sha256 -ne $script:lock.dockerEngine.sha256) { throw 'Docker lock changed; create a fresh local VM' }
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    if (($machinePath -split ';') -notcontains $script:dockerRoot) {
        [Environment]::SetEnvironmentVariable('Path', ($machinePath.TrimEnd(';') + ';' + $script:dockerRoot), 'Machine')
    }
    if (($env:Path -split ';') -notcontains $script:dockerRoot) { $env:Path += ';' + $script:dockerRoot }
    $configPath = 'C:\ProgramData\docker\config\daemon.json'
    Write-AtomicJson -Path $configPath -Value ([ordered]@{
        hosts = @('npipe:////./pipe/docker_engine')
        'exec-opts' = @('isolation=process')
        'data-root' = 'C:\ProgramData\docker'
    })
    Set-Service -Name docker -StartupType Automatic
}

function Register-Continuation {
    $executable = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
    $arguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "C:\ProgramData\WinhooHost\Provision-WinhooContainerHost.ps1" -Resume'
    $action = New-ScheduledTaskAction -Execute $executable -Argument $arguments
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Hours 4)
    Register-ScheduledTask -TaskName $script:taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
}

function Wait-DockerReady {
    $deadline = [DateTime]::UtcNow.AddMinutes(15)
    $lastOutput = ''
    while ([DateTime]::UtcNow -lt $deadline) {
        $previousPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $output = & "$script:dockerRoot\docker.exe" --host npipe:////./pipe/docker_engine info --format '{{json .}}' 2>&1
            $code = $LASTEXITCODE
        }
        finally { $ErrorActionPreference = $previousPreference }
        $lastOutput = $output -join "`n"
        if ($code -eq 0) {
            $info = $lastOutput | ConvertFrom-Json
            if ($info.OSType -ne 'windows' -or $info.Architecture -notin @('amd64', 'x86_64')) {
                throw 'Docker did not expose a native Windows AMD64 engine'
            }
            if ($info.ServerVersion -ne $script:lock.dockerEngine.version.TrimStart('v')) {
                throw 'The running Docker Engine version differs from the dependency lock'
            }
            return $info
        }
        Start-Sleep -Seconds 5
    }
    throw "Docker did not become ready: $lastOutput"
}

try {
    $ownsMutex = $mutex.WaitOne(0)
    if (-not $ownsMutex) { Write-Output 'winhoo host: another provisioning process owns this boot'; exit 0 }
    [void] [IO.Directory]::CreateDirectory($stateRoot)
    Set-PrivateDirectoryAcl -Path $stateRoot
    $instanceSource = Join-Path $PSScriptRoot 'instance-id'
    $instanceId = ([Guid]::Parse(([IO.File]::ReadAllText($instanceSource)).Trim())).ToString()
    $persistedInstance = Join-Path $stateRoot 'instance-id'
    if ((Test-Path -LiteralPath $persistedInstance) -and
        ([Guid]::Parse(([IO.File]::ReadAllText($persistedInstance)).Trim()).ToString() -ne $instanceId)) {
        throw 'This guest belongs to a different local winhoo instance'
    }
    $os = Get-CimInstance Win32_OperatingSystem
    if ($os.ProductType -ne 3 -or [int] $os.BuildNumber -ne 20348 -or $env:PROCESSOR_ARCHITECTURE -ne 'AMD64') {
        throw 'This OEM bootstrap requires a dedicated Windows Server 2022 AMD64 guest'
    }
    $lockHash = (Get-FileHash -LiteralPath $DependenciesPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $lock = Get-Content -LiteralPath $DependenciesPath -Raw | ConvertFrom-Json
    foreach ($name in @('openSsh', 'dockerEngine')) {
        if (-not $lock.PSObject.Properties[$name]) { throw "dependencies.json is missing $name" }
    }
    $persistedLock = Join-Path $stateRoot 'dependencies.json'
    if ((Test-Path -LiteralPath $persistedLock) -and
        ((Get-FileHash -LiteralPath $persistedLock -Algorithm SHA256).Hash -ne $lockHash)) {
        throw 'The dependency lock changed for an existing guest; create a fresh local VM'
    }
    if ([IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\') -ne $stateRoot) {
        Copy-Item -LiteralPath $PSCommandPath -Destination (Join-Path $stateRoot 'Provision-WinhooContainerHost.ps1') -Force
        Copy-Item -LiteralPath $DependenciesPath -Destination $persistedLock -Force
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'authorized_keys') -Destination (Join-Path $stateRoot 'authorized_keys') -Force
        Copy-Item -LiteralPath $instanceSource -Destination $persistedInstance -Force
    }
    $publicKeys = [IO.File]::ReadAllText((Join-Path $stateRoot 'authorized_keys'))
    if ($publicKeys.Length -gt 131072 -or $publicKeys -notmatch '(?m)^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)) [A-Za-z0-9+/]+=*') {
        throw 'The OEM public-key input is absent or invalid'
    }
    # The local Docker share is an anonymous SMB share; SYSTEM cannot reuse a user's Z: mapping.
    Set-SmbClientConfiguration -EnableInsecureGuestLogons $true -Force
    Register-Continuation
    [void] (Publish-Status -Status 'running' -Phase $phase)

    $phase = 'configure-operator'
    Initialize-Operator
    $phase = 'install-openssh'
    [void] (Publish-Status -Status 'running' -Phase $phase)
    Install-OpenSsh
    $phase = 'install-docker-engine'
    [void] (Publish-Status -Status 'running' -Phase $phase)
    Install-DockerEngine

    $phase = 'enable-containers'
    [void] (Publish-Status -Status 'running' -Phase $phase)
    $feature = Get-WindowsFeature -Name Containers
    $requiresReboot = $false
    if (-not $feature.Installed) {
        $result = Install-WindowsFeature -Name Containers -Restart:$false
        if (-not $result.Success) { throw 'Windows failed to enable the Containers feature' }
        $requiresReboot = $result.RestartNeeded.ToString() -eq 'Yes'
    }
    $resumePath = Join-Path $stateRoot 'continuation.json'
    $reboots = 0
    $pendingDuringThisBoot = $false
    $bootTime = $os.LastBootUpTime.ToUniversalTime().ToString('o')
    if (Test-Path -LiteralPath $resumePath) {
        $continuation = Get-Content -LiteralPath $resumePath -Raw | ConvertFrom-Json
        $reboots = [int] $continuation.rebootsRequested
        $pendingDuringThisBoot = $continuation.requestedDuringBoot -eq $bootTime
    }
    # An installed feature can still need its requested reboot. A manual rerun must not publish readiness in that boot.
    if ($requiresReboot -or $pendingDuringThisBoot) {
        if (-not $pendingDuringThisBoot) {
            if ($reboots -ge 2) { throw 'Containers installation exceeded two guest reboot attempts' }
            $reboots++
        }
        Write-AtomicJson -Path $resumePath -Value ([ordered]@{
            rebootsRequested = $reboots
            requestedAt = [DateTime]::UtcNow.ToString('o')
            requestedDuringBoot = $bootTime
        })
        [void] (Publish-Status -Status 'reboot-required' -Phase 'restart-guest')
        & shutdown.exe /r /t 60 /f /d p:4:1 /c 'winhoo: finish enabling Windows containers in this guest'
        # ERROR_SHUTDOWN_IS_SCHEDULED is expected when rerunning during the pending 60-second reboot.
        if ($LASTEXITCODE -notin @(0, 1190)) { throw 'Guest reboot scheduling failed' }
        exit 0
    }

    $phase = 'wait-engine'
    if (-not (Get-WindowsFeature -Name Containers).Installed) { throw 'The Containers feature is not installed after continuation' }
    Start-Service -Name docker
    Start-Service -Name sshd
    $engine = Wait-DockerReady
    if ((Get-Service -Name sshd).Status -ne 'Running') { throw 'SSH service is not running' }
    $hostKey = ([IO.File]::ReadAllText((Join-Path $sshRoot 'ssh_host_ed25519_key.pub'))).Trim()
    $details = [ordered]@{
        os = $os.Caption
        osVersion = $os.Version
        architecture = 'amd64'
        containersFeatureInstalled = $true
        processIsolationConfigured = $true
        nativeContainerTested = $false
        dockerVersion = $engine.ServerVersion
        dockerOSType = $engine.OSType
        dockerArchitecture = $engine.Architecture
        dockerEndpoint = 'npipe:////./pipe/docker_engine'
        sshUser = $operatorName
        sshHostPublicKey = $hostKey
        lastBootTime = $os.LastBootUpTime.ToUniversalTime().ToString('o')
    }
    $receipt = Publish-Status -Status 'ready' -Phase 'ready-for-native-container-test' -Details $details -RequireShared
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    $receipt | ConvertTo-Json -Depth 8
}
catch {
    $message = $_.Exception.Message
    try { [void] (Publish-Status -Status 'failed' -Phase $phase -ErrorMessage $message) }
    catch { Write-Output ('winhoo host: could not write failure receipt: ' + $_.Exception.Message) }
    Write-Error -Message $message -ErrorAction Continue
    exit 1
}
finally {
    if ($ownsMutex) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
