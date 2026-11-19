# Wait for the single, already dispatched console helper. It publishes its
# result with an atomic rename, so an existing empty file is also a result.
function Wait-WinHooForegroundSample([string]$Path, [string]$TaskName, [datetime]$RequestedAt) {
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $nextStatus = 8
    $detail = 'scheduler status unavailable'
    while (!(Test-Path -LiteralPath $Path -PathType Leaf)) {
        if ($watch.Elapsed.TotalSeconds -ge $nextStatus) {
            $nextStatus = $watch.Elapsed.TotalSeconds + 4
            $finished = $false
            try {
                $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop
                $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
                $detail = "scheduler state=$($task.State), last result=$($info.LastTaskResult)"
                # Start-ScheduledTask is asynchronous. Ready with an old
                # last-run date is not completion of this request.
                $finished = $task.State -eq 'Ready' -and $info.LastRunTime -ge $RequestedAt.AddSeconds(-1) -and
                    $info.LastTaskResult -notin @(0x41300,0x41301,0x41303,0x41325)
            } catch {}
            if (Test-Path -LiteralPath $Path -PathType Leaf) { break }
            if ($finished) { throw "Foreground task exited without a sample ($detail)" }
        }
        if ($watch.Elapsed.TotalSeconds -ge 30) {
            throw "No foreground sample after 30 seconds ($detail)"
        }
        Start-Sleep -Milliseconds 100
    }
    return $watch.ElapsedMilliseconds
}
