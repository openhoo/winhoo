[CmdletBinding()]
param([string]$Installer = (Join-Path $PSScriptRoot '..\desktop\Install-GuestTools.ps1'))
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Installer, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw "Guest-tools parser errors: $($errors[0].Message)" }
foreach ($name in @('Test-SerialDevice', 'Install-SerialDriver')) {
    $function = $ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name}, $true)
    if ($null -eq $function) { throw "Missing function $name" }
    . ([scriptblock]::Create($function.Extent.Text))
}
# Execute the real decision functions with inert PnP and installer boundaries.
$healthy = [pscustomobject]@{PNPDeviceID='PCI\VEN_1AF4&DEV_1003&SUBSYS_00031AF4'; ConfigManagerErrorCode=0}
function Get-CimInstance { param($ClassName, $Filter) return $script:Devices }
function pnputil.exe {
    $script:Calls += 1
    $global:LASTEXITCODE = $script:DriverExit
    if ($script:InstallWorks) { $script:Devices = @($healthy) }
}
$passed = @()
$script:Calls = 0
$script:Devices = @($healthy)
Install-SerialDriver 'unused.inf'
if ($script:Calls -ne 0) { throw 'A healthy higher-ranked driver was reinstalled' }
$passed += 'healthy existing serial device reused without pnputil'
$script:Devices = @()
$script:DriverExit = 0
$script:InstallWorks = $true
Install-SerialDriver 'fixture.inf'
if ($script:Calls -ne 1 -or -not (Test-SerialDevice)) { throw 'Missing driver was not installed and verified' }
$passed += 'missing device installed and health verified'
$script:InstallWorks = $false
foreach ($code in @(0, 259, 3010)) {
    $script:Devices = @()
    $script:DriverExit = $code
    $caught = $false
    try { Install-SerialDriver 'fixture.inf' }
    catch { if ($_.Exception.Message -notmatch 'serial device is not ready') { throw }; $caught = $true }
    if (-not $caught) { throw "Exit $code accepted without a healthy device" }
    $passed += "exit $code without a working serial device rejected"
}
foreach ($device in @(
    [pscustomobject]@{PNPDeviceID='PCI\VEN_DEAD&DEV_1003&SUBSYS_00031AF4'; ConfigManagerErrorCode=0},
    [pscustomobject]@{PNPDeviceID=$healthy.PNPDeviceID; ConfigManagerErrorCode=28}
)) {
    $script:Devices = @($device)
    if (Test-SerialDevice) { throw 'Incorrect or unhealthy device was accepted' }
}
$passed += 'incorrect hardware and unhealthy device rejected'
[ordered]@{schema='winhoo.guest-tools-fixture/1'; powershell=$PSVersionTable.PSVersion.ToString(); parsed=$Installer; passed=$passed} | ConvertTo-Json -Depth 4
