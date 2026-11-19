# Exercise the shipped sources under Windows PowerShell 5.1. Hosted runners
# can have no input desktop; only the retained local VM qualifies that path.
$ErrorActionPreference = 'Stop'
$src = Join-Path $PSScriptRoot '..\winhoo_macos'
foreach ($f in 'console-state.ps1','foreground.ps1') {
    $errors = $null
    $null = [Management.Automation.Language.Parser]::ParseFile((Join-Path $src $f), [ref]$null, [ref]$errors)
    if ($errors) { throw ($errors -join [Environment]::NewLine) }
}
$dir = (New-Item -Path (Join-Path $env:TEMP ([guid]::NewGuid().ToString('N'))) -ItemType Directory).FullName
try {
    $exe = Join-Path $dir 'foreground.exe'
    Add-Type -OutputAssembly $exe -OutputType WindowsApplication -TypeDefinition ([IO.File]::ReadAllText((Join-Path $src 'foreground.cs')))
    $bytes = [IO.File]::ReadAllBytes($exe)
    $pe = [BitConverter]::ToInt32($bytes, 0x3c)
    if ([BitConverter]::ToUInt16($bytes, $pe + 24 + 68) -ne 2) {throw 'Probe must use the Windows GUI subsystem'}
    $p = Start-Process -FilePath $exe -PassThru
    if (!$p.WaitForExit(5000) -or $p.ExitCode -ne 0) {throw 'Probe did not complete'}
    $rows = [IO.File]::ReadAllLines((Join-Path $dir 'sample'))
    if ($rows.Length -eq 0) {
        Write-Output 'Probe completed with no stable foreground window on this runner.'
    } elseif ($rows.Length -eq 3 -and [int]$rows[0] -gt 0 -and [int]$rows[1] -gt 0 -and $rows[2]) {
        Write-Output 'Probe sampled a foreground process in an interactive session.'
    } else { throw 'Invalid foreground sample' }
    Remove-Item -LiteralPath (Join-Path $dir 'sample')
    $focus = Join-Path $dir 'focus.exe'
    Add-Type -OutputAssembly $focus -OutputType WindowsApplication -TypeDefinition ([IO.File]::ReadAllText((Join-Path $src 'focus-caption.cs')))
    # A missing process identity must never send input, including on a runner
    # with an interactive desktop.
    $p = Start-Process -FilePath $focus -PassThru
    if (!$p.WaitForExit(5000) -or $p.ExitCode -ne 1) {throw 'Activation without a target was not refused'}
    if ([IO.File]::ReadAllLines((Join-Path $dir 'sample')).Length -ne 0) {throw 'Unexpected activation receipt'}
} finally {
    Remove-Item -LiteralPath $dir -Recurse -Force
}
