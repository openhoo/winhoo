[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$probe = Join-Path 'C:\work' ("winhoo-smoke-$([Guid]::NewGuid().ToString('N')).txt")
$expected = "winhoo $([Guid]::NewGuid().ToString('N'))"

try {
    [IO.File]::WriteAllText($probe, $expected, [Text.Encoding]::UTF8)
    if ([IO.File]::ReadAllText($probe, [Text.Encoding]::UTF8) -ne $expected) {
        throw 'The work directory file round trip failed'
    }
    [ordered]@{
        status = 'passed'
        platform = $env:OS
        user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        powershellVersion = $PSVersionTable.PSVersion.ToString()
        workDirectory = 'C:\work'
        fileRoundTrip = $true
        testedAt = [DateTime]::UtcNow.ToString('o')
    } | ConvertTo-Json
}
finally {
    Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
}
