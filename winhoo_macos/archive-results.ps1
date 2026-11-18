# The host prepends $request from JSON. Operate only on a nominated local
# application result directory after its producer has finished.
$owner = Get-Content C:\ProgramData\WinHoo\owner.json -Raw | ConvertFrom-Json
if ($request.owner -cne $owner.uuid -or $env:USERNAME -ine $owner.account) { throw 'Unexpected guest owner' }
$inputPath = [string]$request.source
if ($inputPath -notmatch '^[A-Za-z]:\\' -or $inputPath.Substring(2) -match '[:/]|(?:^|\\)\.{1,2}(?:\\|$)') {
    throw 'Use an absolute local result directory without traversal'
}
$src = Get-Item -LiteralPath $inputPath -Force -ErrorAction Stop
$profileRoot = [IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd('\')
$appRoot = [IO.Path]::GetFullPath($env:ProgramData).TrimEnd('\')
$underProfile = $src.FullName.StartsWith($profileRoot+'\',[StringComparison]::OrdinalIgnoreCase)
$underApp = $src.FullName.StartsWith($appRoot+'\',[StringComparison]::OrdinalIgnoreCase) -and
    $src.FullName.Substring($appRoot.Length+1).Contains('\')
if (-not $src.PSIsContainer -or -not ($underProfile -or $underApp)) {
    throw 'Select a result subdirectory in the test profile or in an application under ProgramData'
}
for ($entry=$src; $null -ne $entry; $entry=$entry.Parent) {
    if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Result directory has a reparse point ancestor' }
}
$name = [string]$request.name
if ($name -notmatch '^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,91}\.zip$') {throw 'Expected a plain ZIP basename'}
$out = Join-Path $profileRoot $name
if (Test-Path -LiteralPath $out) {throw 'An archive with this name already exists'}
$tmp = Join-Path $profileRoot ('winhoo-'+[guid]::NewGuid().ToString('N')+'.tmp')
try {
    # Windows PowerShell's ZipFile.CreateFromDirectory failed on real test
    # attachments exceeding MAX_PATH. bsdtar traverses relative to -C.
    & "$env:SystemRoot\System32\tar.exe" --format zip -cf $tmp -C $src.FullName .
    if ($LASTEXITCODE -ne 0) {throw "Windows tar exited $LASTEXITCODE"}
    $size = (Get-Item -LiteralPath $tmp).Length
    $hash = (Get-FileHash -LiteralPath $tmp -Algorithm SHA256).Hash.ToLowerInvariant()
    [IO.File]::Move($tmp,$out) # No replacement, including a concurrent producer.
    @{name=$name;bytes=$size;sha256=$hash;owner=$owner.uuid} | ConvertTo-Json -Compress
} finally {
    if ([IO.File]::Exists($tmp)) { [IO.File]::Delete($tmp) }
}
