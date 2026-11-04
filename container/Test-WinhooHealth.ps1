[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$client = New-Object Net.Sockets.TcpClient
$reader = $null
try {
    if ((Get-Service -Name sshd).Status -ne 'Running') {
        throw 'SSH service is not running'
    }
    if (-not $client.ConnectAsync('127.0.0.1', 22).Wait(3000)) {
        throw 'SSH connection timed out'
    }
    $client.ReceiveTimeout = 3000
    $stream = $client.GetStream()
    $stream.ReadTimeout = 3000
    $reader = New-Object IO.StreamReader($stream, [Text.Encoding]::ASCII)
    $banner = $reader.ReadLine()
    if ($banner -notmatch '^SSH-2\.0-OpenSSH_') {
        throw 'SSH server did not provide an OpenSSH protocol banner'
    }
    exit 0
}
catch {
    Write-Output $_.Exception.Message
    exit 1
}
finally {
    if ($reader) { $reader.Dispose() }
    $client.Dispose()
}
