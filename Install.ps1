$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$UvDir = Join-Path $Root ".uv"
$UvExe = Join-Path $UvDir "uv.exe"
$PythonDir = Join-Path $Root ".python"
$VenvDir = Join-Path $Root ".venv"

# Pin the bootstrap version instead of downloading an unbounded "latest" build.
# This keeps installs reproducible and avoids unexpected antivirus regressions.
$UvVersion = "0.11.32"
# SHA-256 of each Windows release zip, as published beside it on the uv 0.11.32 GitHub
# release. A download that does not match is refused, so what runs is exactly what was
# pinned here -- the version pin alone said which file to fetch, not what would arrive.
# Update these together with $UvVersion.
$UvZipSha256 = @{
    "x86_64"  = "acfde570451cfdb8689fa159a138ee805ba4e241c466432750302c86254b0984"
    "aarch64" = "a7427ea0440bb826b6716d1837ff3d173b8e7d496cb09ee8f456b4e023a2fdcd"
    "i686"    = "e54e814a3963af3af607940f142169312d7550d44db8e2daa2bf6c524554ac3c"
}

$env:UV_PYTHON_INSTALL_DIR = $PythonDir
$env:UV_PYTHON_BIN_DIR = Join-Path $PythonDir "bin"
$env:UV_NO_CACHE = "1"
$env:UV_LINK_MODE = "copy"
$env:UV_PROJECT_ENVIRONMENT = $VenvDir
$env:UV_MANAGED_PYTHON = "1"
$env:UV_PYTHON_INSTALL_REGISTRY = "0"
$env:UV_NO_MODIFY_PATH = "1"

function Get-UvArchitecture {
    # A 32-bit PowerShell on 64-bit Windows reports x86 and names the real machine here.
    $machine = [Environment]::GetEnvironmentVariable("PROCESSOR_ARCHITEW6432")
    if (-not $machine) { $machine = [Environment]::GetEnvironmentVariable("PROCESSOR_ARCHITECTURE") }
    switch ($machine) {
        "AMD64" { return "x86_64" }
        "ARM64" { return "aarch64" }
        "x86" { return "i686" }
        default { return $null }
    }
}

function Get-Sha256Hex {
    # Get-FileHash is not used, because Windows PowerShell cannot always find it. It lives in
    # the script half of Microsoft.PowerShell.Utility, and a Windows PowerShell that inherits
    # PowerShell 7's module path -- which is what Install & Run gets when it is started from
    # a PowerShell 7 terminal -- loads the wrong edition's copy and reports the command as
    # not recognised. .NET needs no module.
    param([Parameter(Mandatory = $true)][string]$Path)
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try {
            return ([System.BitConverter]::ToString($sha.ComputeHash($stream)) -replace '-', '').ToLowerInvariant()
        }
        finally {
            $sha.Dispose()
        }
    }
    finally {
        $stream.Dispose()
    }
}

function Install-PinnedUv {
    # Fetches uv's own release zip and refuses it unless its SHA-256 matches the pin. This
    # replaces running Astral's install script straight from the network with
    # Invoke-Expression, where anything served at that URL -- through a TLS-inspecting
    # proxy, say -- would have run with this user's rights and nothing checked.
    param(
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$Version,
        [Parameter(Mandatory = $true)][hashtable]$Checksums,
        [string]$Architecture = (Get-UvArchitecture)
    )
    if (-not $Architecture -or -not $Checksums.ContainsKey($Architecture)) {
        throw "No uv $Version build is pinned for this processor ($([Environment]::GetEnvironmentVariable('PROCESSOR_ARCHITECTURE')))."
    }
    $asset = "uv-$Architecture-pc-windows-msvc.zip"
    $work = Join-Path ([System.IO.Path]::GetTempPath()) ("vhdr-uv-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $work | Out-Null
    try {
        $zip = Join-Path $work $asset
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        # Windows PowerShell's progress bar slows a download many times over.
        $ProgressPreference = "SilentlyContinue"
        Invoke-WebRequest -Uri "https://github.com/astral-sh/uv/releases/download/$Version/$asset" -OutFile $zip -UseBasicParsing
        $expected = ([string]$Checksums[$Architecture]).ToLowerInvariant()
        $actual = Get-Sha256Hex -Path $zip
        # Both normalised, then compared exactly. PowerShell's -ne ignores case, which
        # would make the normalising above look necessary while doing nothing.
        if ($actual -cne $expected) {
            throw "The downloaded $asset does not match the checksum pinned in Install.ps1 (expected $expected, received $actual), so it was not used."
        }
        $unpacked = Join-Path $work "unpacked"
        Expand-Archive -LiteralPath $zip -DestinationPath $unpacked -Force
        New-Item -ItemType Directory -Force -Path $Destination | Out-Null
        foreach ($name in "uv.exe", "uvx.exe", "uvw.exe") {
            $found = @(Get-ChildItem -LiteralPath $unpacked -Recurse -Filter $name -File)
            if ($found.Count -gt 0) {
                Copy-Item -LiteralPath $found[0].FullName -Destination (Join-Path $Destination $name) -Force
            }
        }
    }
    finally {
        $resolvedWork = [IO.Path]::GetFullPath($work)
        $tempParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
        if ([IO.Path]::GetDirectoryName($resolvedWork) -ine $tempParent -or
            -not [IO.Path]::GetFileName($resolvedWork).StartsWith('vhdr-uv-')) {
            throw 'Refusing to remove an unexpected bootstrap directory'
        }
        Remove-Item -LiteralPath $resolvedWork -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "Virtual HDR OSD for Windows - local installation" -ForegroundColor Cyan
Write-Host "Project: $Root"
Write-Host "Bootstrap uv: $UvVersion"

$UvRunner = $null

if (Test-Path $UvExe) {
    $UvRunner = $UvExe
}
else {
    Write-Host "Downloading project-local uv $UvVersion..."
    try {
        Install-PinnedUv -Destination $UvDir -Version $UvVersion -Checksums $UvZipSha256
    }
    catch {
        Write-Warning "The project-local uv bootstrap was blocked or failed: $($_.Exception.Message)"
    }

    if (Test-Path $UvExe) {
        $UvRunner = $UvExe
    }
}

# If Defender quarantines the local executable but the user already has uv installed,
# use that existing executable only for bootstrap. Python and .venv remain project-local.
if (-not $UvRunner) {
    $systemUv = Get-Command uv.exe -ErrorAction SilentlyContinue
    if ($systemUv) {
        $UvRunner = $systemUv.Source
        Write-Host "Using existing uv only for bootstrap: $UvRunner" -ForegroundColor Yellow
    }
}

if (-not $UvRunner) {
    Write-Host "" 
    Write-Host "Microsoft Defender appears to have blocked the official uv executable." -ForegroundColor Red
    Write-Host "The app itself has not been installed or executed yet." -ForegroundColor Red
    Write-Host "Do not disable Windows Security for this app." -ForegroundColor Yellow
    Write-Host "Update Defender security intelligence, then retry `"1- Install & Run.bat`"." -ForegroundColor Yellow
    Write-Host "You can also install uv from Astral separately and rerun this installer; it will use it only for bootstrap." -ForegroundColor Yellow
    throw "No usable uv executable is available."
}

Write-Host "Installing project-local Python 3.12..."
& $UvRunner python install 3.12 --no-bin --no-registry
if ($LASTEXITCODE -ne 0) { throw "uv python install failed with exit code $LASTEXITCODE" }

Write-Host "Synchronizing the project-local environment..."
& $UvRunner sync --no-cache --python 3.12
if ($LASTEXITCODE -ne 0) { throw "uv sync failed with exit code $LASTEXITCODE" }

Write-Host "Installation complete." -ForegroundColor Green
Write-Host "Python: $PythonDir"
Write-Host "Environment: $VenvDir"
Write-Host "Start the app with `"1- Install & Run.bat`""
