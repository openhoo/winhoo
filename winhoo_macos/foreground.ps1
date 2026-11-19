# The controller supplies $request, $who and Get-WinHooConsole. OpenSSH is
# in a service session; sample the dedicated user's actual input desktop.
$user = "$env:COMPUTERNAME\$env:USERNAME"
function Read-OwnedConsole {
    $s = Get-WinHooConsole
    if ($s.account -ine $user -or !$s.id -or $s.connection -ne 'active' -or $s.lock -ne 'unlocked') {
        throw 'The dedicated physical console is unavailable or locked'
    }
    return $s
}
if ($who.owner -cne $request.owner -or $who.account -ine $request.account) { throw 'Different desktop owner' }
$before = Read-OwnedConsole
$name = 'WinHoo-Foreground-' + $request.id
$dir = (New-Item -Path (Join-Path $env:TEMP $name) -ItemType Directory -ErrorAction Stop).FullName
$registered = $false
try {
    # The passive probe and the explicitly requested activation helper are
    # separate GUI executables; neither opens a console window.
    $exe = Join-Path $dir 'foreground.exe'
    Add-Type -OutputAssembly $exe -OutputType WindowsApplication -TypeDefinition $request.probeSource
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    if ($request.mode -ceq 'focus') {
        $target = Get-Process -Id ([int]$request.pid) -ErrorAction Stop
        if ($target.SessionId -ne $before.id -or $target.HasExited) { throw 'Target is not on the dedicated active console' }
        $ticks = $target.StartTime.ToUniversalTime().Ticks
        if ($null -ne $request.startedTicks -and $ticks -ne [long]$request.startedTicks) { throw 'Target process has changed since discovery' }
        $action = New-ScheduledTaskAction -Execute $exe -Argument ("{0} {1}" -f $target.Id,$ticks)
    } else { $action = New-ScheduledTaskAction -Execute $exe }
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Seconds 10)
    $null = Register-ScheduledTask -TaskName $name -Principal $principal -Action $action -Settings $settings
    $registered = $true
    Start-ScheduledTask -TaskName $name
    $file = Join-Path $dir 'sample'
    $timer = [Diagnostics.Stopwatch]::StartNew()
    while (!(Test-Path -LiteralPath $file -PathType Leaf)) {
        if ($timer.Elapsed.TotalSeconds -ge 12) { throw 'The interactive foreground probe did not return a sample' }
        Start-Sleep -Milliseconds 100
    }
    $lines = [IO.File]::ReadAllLines($file)
    if ($request.mode -ceq 'focus') {
        if ($lines.Length -ne 4 -or $lines[3] -cnotin @('already-active','caption-click')) { throw 'No verified caption or foreground window for the requested process' }
        $who.method = $lines[3]
    } elseif ($lines.Length -ne 3) { throw 'No stable foreground window in the active session' }
    $after = Read-OwnedConsole
    if ($after.id -ne $before.id -or [int]$lines[0] -ne $before.id) { throw 'Console session changed during observation' }
    $who.request = $request.id; $who.session = [int]$lines[0]; $who.pid = [int]$lines[1]; $who.process = $lines[2]
    $who.consoleSession = $after; $who.console = $after.account
    $who | ConvertTo-Json -Depth 3 -Compress
} finally {
    if ($registered) { Unregister-ScheduledTask -TaskName $name -Confirm:$false }
    # The sample file can arrive just before the GUI executable exits.
    for ($try = 0; $try -lt 20; $try++) {
        try { Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction Stop; break } catch {
            if ($try -eq 19) { throw }
            Start-Sleep -Milliseconds 100
        }
    }
}
