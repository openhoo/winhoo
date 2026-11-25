[CmdletBinding()]
param(
    [string]$Launcher = 'C:\ProgramData\WinHoo\tools\WinHoo.RunHidden.exe',
    [Parameter(Mandatory = $true)][string]$Probe
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = Join-Path $env:TEMP ('winhoo-runner-' + [guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($root)
function Invoke-Runner([string]$Arguments) {
    # A command-line string exercises the same boundary as Scheduled Tasks.
    # QGA's Windows argv conversion cannot transport an empty argument itself.
    $start = [Diagnostics.ProcessStartInfo]::new($Launcher, $Arguments)
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $process = [Diagnostics.Process]::Start($start)
    try {
        if (-not $process.WaitForExit(120000)) {
            $process.Kill()
            throw 'Hidden launcher exceeded the native probe deadline'
        }
        return $process.ExitCode
    }
    finally { $process.Dispose() }
}
try {
    $stdout = Join-Path $root 'stdout.log'
    $stderr = Join-Path $root 'stderr.log'
    $status = Join-Path $root 'process.json'
    $arguments = '--stdout "{0}" --stderr "{1}" --status "{2}" -- "{3}" "Grüße – 日本語" "spaces ; $(literal) `literal`" "embedded \"quotes\" and backslashes\\" "" --flood' -f $stdout, $stderr, $status, $Probe
    $exitCode = Invoke-Runner $arguments
    if ($exitCode -ne 37) { throw "Native exit code changed to $exitCode" }
    $receipt = [IO.File]::ReadAllText($status) | ConvertFrom-Json
    if ($receipt.schema -cne 'winhoo.process/1' -or $receipt.status -cne 'exited' -or $receipt.exitcode -ne 37 -or $receipt.pid -le 0) {
        throw 'Native process receipt invalid'
    }
    $output = [IO.File]::ReadAllText($stdout, [Text.Encoding]::UTF8)
    $errorOutput = [IO.File]::ReadAllText($stderr, [Text.Encoding]::UTF8)
    $lines = @($output -split '\r?\n')
    if ($lines[0] -cne 'console=0') { throw "Child received a console: $($lines[0])" }
    $expected = @('Grüße – 日本語', 'spaces ; $(literal) `literal`', 'embedded "quotes" and backslashes\', '', '--flood')
    $actual = @($lines | Where-Object { $_.StartsWith('argument=') } | ForEach-Object {
        [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($_.Substring(9)))
    })
    if ($actual.Count -ne $expected.Count) { throw "Argument count changed: $($actual.Count)" }
    for ($index = 0; $index -lt $expected.Count; $index++) {
        if ($actual[$index] -cne $expected[$index]) { throw "Argument $index changed" }
    }
    $stdoutBytes = (Get-Item -LiteralPath $stdout).Length
    $stderrBytes = (Get-Item -LiteralPath $stderr).Length
    if ($stdoutBytes -lt 500000 -or $stderrBytes -lt 500000 -or -not $errorOutput.StartsWith('stderr=Grüße – 日本語')) {
        throw 'Concurrent Unicode output capture incomplete'
    }
    $collision = Join-Path $root 'collision.log'
    $collisionStatus = Join-Path $root 'collision.json'
    $invalidExit = Invoke-Runner ('--stdout "{0}" --stderr "{0}" --status "{1}" -- "{2}"' -f $collision, $collisionStatus, $Probe)
    $invalidReceipt = [IO.File]::ReadAllText($collisionStatus) | ConvertFrom-Json
    if ($invalidExit -ne 125 -or $invalidReceipt.status -cne 'failed' -or $null -ne $invalidReceipt.pid -or [IO.File]::ReadAllText($collision).Contains('console=')) {
        throw 'Colliding output files were not rejected before starting the child'
    }
    [ordered]@{
        schema = 'winhoo.hidden-runner-acceptance/1'
        status = 'passed'
        nativeExit = $exitCode
        consoleWindowHandle = 0
        unicode = $true
        literalArguments = $true
        emptyArgument = $true
        concurrentLargePipes = $true
        stdoutBytes = $stdoutBytes
        stderrBytes = $stderrBytes
        invalidOutputPathsRejected = $true
        launcherSha256 = (Get-FileHash -LiteralPath $Launcher -Algorithm SHA256).Hash.ToLowerInvariant()
        generatedUtc = [DateTime]::UtcNow.ToString('o')
    } | ConvertTo-Json -Depth 4 -Compress
}
finally { Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue }
