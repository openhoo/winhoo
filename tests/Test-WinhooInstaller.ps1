[CmdletBinding()]
param(
    [string] $PackageScript = (Join-Path $PSScriptRoot '..\container\Install-WinhooPackage.ps1')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ("winhoo-installer-test-$([Guid]::NewGuid().ToString('N'))")
$results = New-Object 'Collections.Generic.List[object]'
$fatalError = $null
$passed = $false

function ConvertTo-PowerShellLiteral {
    param([string] $Value)
    return "'" + $Value.Replace("'", "''") + "'"
}

try {
    New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
    $sourcePath = (Resolve-Path -LiteralPath $PackageScript).ProviderPath
    $source = [IO.File]::ReadAllText($sourcePath)
    foreach ($path in @('C:\winhoo\logs', 'C:\winhoo\downloads')) {
        if (-not $source.Contains((ConvertTo-PowerShellLiteral $path))) {
            throw "The installer helper's expected path '$path' changed; refusing to run a nonisolated test"
        }
    }
    $source = $source.Replace((ConvertTo-PowerShellLiteral 'C:\winhoo\logs'),
        (ConvertTo-PowerShellLiteral (Join-Path $testRoot 'logs')))
    $source = $source.Replace((ConvertTo-PowerShellLiteral 'C:\winhoo\downloads'),
        (ConvertTo-PowerShellLiteral (Join-Path $testRoot 'downloads')))
    $isolatedHelper = Join-Path $testRoot 'Install-WinhooPackage.ps1'
    [IO.File]::WriteAllText($isolatedHelper, $source, [Text.Encoding]::ASCII)

    $fixture = Join-Path $testRoot 'fixture.exe'
    $powershell = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $fixtureSource = @'
public static class WinhooInstallerFixture {
    public static int Main(string[] args) {
        return args.Length == 1 ? System.Int32.Parse(args[0]) : 99;
    }
}
'@
    # Compile out of process so no loaded assembly can hold the fixture open during cleanup.
    $compile = '$ErrorActionPreference = ''Stop''; Add-Type -TypeDefinition ' +
        (ConvertTo-PowerShellLiteral $fixtureSource) + ' -Language CSharp -OutputType ConsoleApplication -OutputAssembly ' +
        (ConvertTo-PowerShellLiteral $fixture)
    $compileEncoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($compile))
    $compilerOutput = & $powershell -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand $compileEncoded 2>&1
    if ($LASTEXITCODE -ne 0) { throw ('Fixture compilation failed: ' + ($compilerOutput -join "`n")) }
    $hash = (Get-FileHash -LiteralPath $fixture -Algorithm SHA256).Hash
    $cases = @(
        @{ name = 'checksum-mismatch'; code = 0; accept = @(0); badHash = $true; http = $false; status = 'failed'; processCode = $null; error = 'checksum mismatch' },
        @{ name = 'http-rejected'; code = 0; accept = @(0); badHash = $false; http = $true; status = 'failed'; processCode = $null; error = 'require HTTPS' },
        @{ name = 'exit-zero'; code = 0; accept = @(0); badHash = $false; http = $false; status = 'installed'; processCode = 0; error = '' },
        @{ name = 'exit-nonzero'; code = 47; accept = @(0); badHash = $false; http = $false; status = 'failed'; processCode = 47; error = 'exit code 47' },
        @{ name = 'reboot-3010-rejected'; code = 3010; accept = @(0, 3010); badHash = $false; http = $false; status = 'reboot-required'; processCode = 3010; error = 'requested a reboot' },
        @{ name = 'reboot-1641-rejected'; code = 1641; accept = @(0, 1641); badHash = $false; http = $false; status = 'reboot-required'; processCode = 1641; error = 'requested a reboot' }
    )
    foreach ($case in $cases) {
        $row = [ordered]@{ name = $case.name; passed = $false }
        try {
            $expectedExit = if ($case.status -eq 'installed') { 0 } else { 1 }
            $caseHash = if ($case.badHash) { '0' * 64 } else { $hash }
            $command = '& ' + (ConvertTo-PowerShellLiteral $isolatedHelper) +
                ' -Type exe -Name ' + (ConvertTo-PowerShellLiteral $case.name) +
                ' -Sha256 ' + (ConvertTo-PowerShellLiteral $caseHash) +
                ' -Arguments ' + (ConvertTo-PowerShellLiteral ([string] $case.code)) +
                ' -AcceptExitCodes @(' + ($case.accept -join ',') + ')'
            if ($case.http) {
                $command += ' -Uri ''http://127.0.0.1:9/fixture.exe'''
            }
            else {
                $command += ' -Path ' + (ConvertTo-PowerShellLiteral $fixture)
            }
            $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
            $output = & $powershell -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand $encoded 2>&1
            $helperExit = $LASTEXITCODE
            $payload = ($output -join "`n") | ConvertFrom-Json
            $row.helperExitCode = $helperExit
            $row.status = $payload.status
            $row.installerExitCode = $payload.exitCode
            $correct = ($helperExit -eq $expectedExit) -and ($payload.status -eq $case.status) -and
                ($payload.exitCode -eq $case.processCode)
            if ($case.error) {
                $correct = $correct -and ($payload.error -like ('*' + $case.error + '*'))
                $row.reportedError = $payload.error
            }
            $row.passed = [bool] $correct
        }
        catch {
            $row.error = $_.Exception.Message
        }
        $results.Add([pscustomobject] $row)
    }
    $passed = (@($results.ToArray() | Where-Object { -not $_.passed }).Count -eq 0)
}
catch {
    $fatalError = $_.Exception.Message
}
finally {
    try {
        if (Test-Path -LiteralPath $testRoot) {
            Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction Stop
        }
    }
    catch {
        $passed = $false
        $fatalError = 'Temporary test files could not be removed: ' + $_.Exception.Message
    }
}

[ordered]@{
    schemaVersion = 1
    passed = [bool] $passed
    cases = $results.ToArray()
    fatalError = $fatalError
} | ConvertTo-Json -Depth 6
if (-not $passed) { exit 1 }
exit 0
