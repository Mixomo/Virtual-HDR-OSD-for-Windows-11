# Exercises Install-PinnedUv from the shipped Install.ps1 in isolation.
#
# The bootstrap used to fetch Astral's install script and run it with Invoke-Expression, so
# whatever that URL served ran unchecked. It now downloads the release zip and refuses it
# unless the SHA-256 matches a pin. The download is stubbed here: nothing touches the
# network, and nothing outside the work directory the caller supplies is written.

param(
    [Parameter(Mandatory = $true)][string]$FunctionsPath,
    [Parameter(Mandatory = $true)][string]$Work
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest   # matches Install.ps1 itself
$script:fail = 0
. $FunctionsPath

# A zip laid out the way the real release is: the three executables at its root.
$payload = Join-Path $Work 'payload'
New-Item -ItemType Directory -Path $payload | Out-Null
foreach ($name in 'uv.exe', 'uvx.exe', 'uvw.exe') {
    Set-Content -LiteralPath (Join-Path $payload $name) -Value "stub $name"
}
$script:asset = Join-Path $Work 'asset.zip'
Compress-Archive -Path (Join-Path $payload '*') -DestinationPath $script:asset
# Hashed here independently of the function under test, and in upper case, so the pin
# comparison's handling of case is exercised. Not with Get-FileHash, for the reason the
# installer gives: this harness runs in the same Windows PowerShell.
$bytes = [System.IO.File]::ReadAllBytes($script:asset)
$good = ([System.BitConverter]::ToString([System.Security.Cryptography.SHA256]::Create().ComputeHash($bytes)) -replace '-', '').ToUpperInvariant()

# Functions outrank cmdlets, so this is what Install-PinnedUv reaches.
$script:requested = @()
function Invoke-WebRequest {
    param($Uri, $OutFile, [switch]$UseBasicParsing)
    $script:requested += [string]$Uri
    Copy-Item -LiteralPath $script:asset -Destination $OutFile
}

function Check {
    param([string]$Name, [bool]$Ok, [string]$Detail = '')
    if ($Ok) { Write-Host "  PASS  $Name" } else { $script:fail++; Write-Host "  FAIL  $Name $Detail" }
}

# A matching download is installed, from the release the version names.
$dest = Join-Path $Work 'matching'
Install-PinnedUv -Destination $dest -Version '9.9.9' -Checksums @{ x86_64 = $good } -Architecture 'x86_64'
Check 'a matching zip installs uv.exe' (Test-Path -LiteralPath (Join-Path $dest 'uv.exe'))
Check 'and uvx.exe beside it' (Test-Path -LiteralPath (Join-Path $dest 'uvx.exe'))
Check 'fetched from the pinned release' `
    ($script:requested[-1] -eq 'https://github.com/astral-sh/uv/releases/download/9.9.9/uv-x86_64-pc-windows-msvc.zip') `
    $script:requested[-1]

# The hash helper itself, against the published test vector for "abc".
$vector = Join-Path $Work 'abc.txt'
[System.IO.File]::WriteAllBytes($vector, [byte[]](0x61, 0x62, 0x63))
Check 'the SHA-256 helper matches the published vector' `
    ((Get-Sha256Hex -Path $vector) -ceq 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad') `
    (Get-Sha256Hex -Path $vector)

# The pin is case-insensitive: the hash above is upper case, this pin is lower.
$dest = Join-Path $Work 'lowercase'
Install-PinnedUv -Destination $dest -Version '9.9.9' -Checksums @{ x86_64 = $good.ToLowerInvariant() } -Architecture 'x86_64'
Check 'a lower-case pin still matches' (Test-Path -LiteralPath (Join-Path $dest 'uv.exe'))

# A mismatch is refused, and nothing it contained reaches the destination.
$dest = Join-Path $Work 'mismatched'
$refused = $false
try {
    Install-PinnedUv -Destination $dest -Version '9.9.9' -Checksums @{ x86_64 = ('0' * 64) } -Architecture 'x86_64'
}
catch {
    $refused = $_.Exception.Message -match 'does not match'
}
Check 'a mismatched zip is refused with a reason' $refused
Check 'and none of it is installed' (-not (Test-Path -LiteralPath (Join-Path $dest 'uv.exe')))

# No pin for this processor: refused before anything is downloaded.
$before = $script:requested.Count
$refused = $false
try {
    Install-PinnedUv -Destination (Join-Path $Work 'unknown') -Version '9.9.9' -Checksums @{ x86_64 = $good } -Architecture 'sparc'
}
catch {
    $refused = $true
}
Check 'an unpinned processor is refused' $refused
Check 'without downloading anything' ($script:requested.Count -eq $before)

if ($script:fail -eq 0) { Write-Host 'ALL PASS' } else { Write-Host "$($script:fail) FAILURE(S)" }
exit $script:fail
