<#
.SYNOPSIS
    Builds btstack_sink.exe for WinBTsink.

.DESCRIPTION
    1. Finds MSYS2 (MSYS2_ROOT, C:\msys64, C:\tools\msys64) or installs it.
    2. Installs/updates the MinGW-w64 toolchain, CMake and git inside MSYS2.
    3. Clones BTstack at the pinned tag (re-clones when the pin changed or -Force).
    4. Applies WinBTsink's BTstack patches (patches\apply_patches.py).
    5. Runs CMake + make to produce btstack\build\btstack_sink.exe.

    The environment (PATH/TEMP) is restored on exit, so this script can be
    dot-sourced or called from build.ps1 without side effects.

.NOTES
    Run from the project root:  .\btstack\build.ps1
    Or from inside btstack/:    .\build.ps1
#>

param(
    [switch]$Force   # re-clone BTstack and rebuild from scratch
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Paths ────────────────────────────────────────────────────────────────────
$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$BtstackSrc  = Join-Path $ScriptDir "btstack-src"
$BuildDir    = Join-Path $ScriptDir "build"
$PatchScript = Join-Path $ScriptDir "patches\apply_patches.py"
$ExePath     = Join-Path $BuildDir "btstack_sink.exe"

# Pinned BTstack tag (stable, tested with WinBTsink and its patches)
$BTSTACK_REPO   = "https://github.com/bluekitchen/btstack.git"
$BTSTACK_COMMIT = "v1.6.1"

# ── Helpers ──────────────────────────────────────────────────────────────────
function Write-Step([string]$msg) {
    Write-Host "`n==> $msg" -ForegroundColor Cyan
}

function Invoke-Cmd([string]$exe, [string[]]$cmdArgs) {
    & $exe @cmdArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed (exit $LASTEXITCODE): $exe $($cmdArgs -join ' ')"
    }
}

function Find-Msys2Root {
    $candidates = @()
    if ($env:MSYS2_ROOT) { $candidates += $env:MSYS2_ROOT }
    $candidates += "C:\msys64", "C:\tools\msys64"
    foreach ($c in $candidates) {
        if (Test-Path (Join-Path $c "usr\bin\bash.exe")) { return $c }
    }
    return $null
}

function Install-Msys2([string]$root) {
    Write-Host "MSYS2 not found. Downloading installer..." -ForegroundColor Yellow
    $installer = Join-Path $env:TEMP "msys2-x86_64-latest.exe"
    $url = "https://github.com/msys2/msys2-installer/releases/latest/download/msys2-x86_64-latest.exe"
    Write-Host "Downloading from: $url"
    Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing
    Write-Host "Running installer (unattended) into $root ..."
    # The MSYS2 installer is Qt Installer Framework based; these are its
    # unattended-install switches (see msys2.org/docs/installer).
    $rootSlash = $root -replace '\\', '/'
    $p = Start-Process -FilePath $installer -Wait -PassThru -ArgumentList @(
        "in", "--confirm-command", "--accept-messages", "--root", $rootSlash)
    Remove-Item $installer -Force -ErrorAction SilentlyContinue
    if ($p.ExitCode -ne 0 -or -not (Test-Path (Join-Path $root "usr\bin\bash.exe"))) {
        throw "MSYS2 installation failed (exit $($p.ExitCode)). Install manually from https://www.msys2.org/"
    }
}

# ── Step 1: MSYS2 ────────────────────────────────────────────────────────────
Write-Step "Checking MSYS2..."

$MSYS2Root = Find-Msys2Root
if (-not $MSYS2Root) {
    $MSYS2Root = "C:\msys64"
    Install-Msys2 $MSYS2Root
}
$MingwBin = Join-Path $MSYS2Root "mingw64\bin"
$Bash     = Join-Path $MSYS2Root "usr\bin\bash.exe"
Write-Host "MSYS2 found at $MSYS2Root" -ForegroundColor Green

# ── Step 2: toolchain ────────────────────────────────────────────────────────
Write-Step "Updating MSYS2 and installing MinGW-w64 toolchain..."

# A fresh MSYS2 snapshot ships an old package database; sync before installing.
# The core update can ask for a shell restart, so run it twice.
& $Bash -lc "pacman -Syuu --noconfirm" | Out-Host
& $Bash -lc "pacman -Syuu --noconfirm" | Out-Host
# mingw32-make.exe is its own package (mingw-w64-x86_64-make); gcc/cmake do not pull it in.
Invoke-Cmd $Bash @("-lc", "pacman -S --noconfirm --needed mingw-w64-x86_64-gcc mingw-w64-x86_64-cmake mingw-w64-x86_64-make git")

$cmakeExe = Join-Path $MingwBin "cmake.exe"
if (-not (Test-Path $cmakeExe)) {
    throw "CMake not found at $cmakeExe after installation."
}
Write-Host "CMake: $cmakeExe" -ForegroundColor Green

# ── Step 3: BTstack source ───────────────────────────────────────────────────
Write-Step "Fetching BTstack ($BTSTACK_COMMIT)..."

$btstackUnix = $BtstackSrc -replace '\\', '/'
$needClone = $Force -or -not (Test-Path $BtstackSrc)

if (-not $needClone) {
    # Re-clone when the pinned tag changed since the last checkout
    $checkedOut = & $Bash -lc "cd '$btstackUnix' && git describe --tags --exact-match 2>/dev/null"
    if ($LASTEXITCODE -ne 0 -or "$checkedOut".Trim() -ne $BTSTACK_COMMIT) {
        Write-Host "Checked-out BTstack ('$checkedOut') differs from pin '$BTSTACK_COMMIT' - re-cloning." -ForegroundColor Yellow
        $needClone = $true
    }
}

if ($needClone) {
    if (Test-Path $BtstackSrc) { Remove-Item $BtstackSrc -Recurse -Force }
    Invoke-Cmd $Bash @("-lc", "git clone --depth 1 --branch '$BTSTACK_COMMIT' '$BTSTACK_REPO' '$btstackUnix'")
} else {
    Write-Host "BTstack source already present (use -Force to re-clone)." -ForegroundColor DarkGray
}

# ── Step 4: patches ──────────────────────────────────────────────────────────
Write-Step "Applying BTstack patches..."

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
if (-not $python) { throw "Python not found on PATH. Install Python 3 and retry." }

Invoke-Cmd $python.Source @($PatchScript, $BtstackSrc)

# ── Step 5: CMake configure + build ─────────────────────────────────────────
Write-Step "Building btstack_sink.exe..."

if (-not (Test-Path $BuildDir)) {
    New-Item -ItemType Directory -Path $BuildDir | Out-Null
}

# CMake parses -D values as CMake code (backslashes are escapes): use forward slashes.
$makeSlash    = (Join-Path $MingwBin "mingw32-make.exe") -replace '\\', '/'
$gccSlash     = (Join-Path $MingwBin "gcc.exe")          -replace '\\', '/'
$scriptSlash  = $ScriptDir  -replace '\\', '/'
$buildSlash   = $BuildDir   -replace '\\', '/'
$btstackSlash = $BtstackSrc -replace '\\', '/'

$savedPath = $env:PATH
$savedTemp = $env:TEMP
$savedTmp  = $env:TMP
try {
    # gcc.exe needs its sibling DLLs on PATH when cmake spawns it, and a TEMP
    # directory without exotic characters.
    $env:PATH = "$MingwBin;$env:PATH"
    $msysTmp = Join-Path $MSYS2Root "tmp"
    if (-not (Test-Path $msysTmp)) { New-Item -ItemType Directory -Path $msysTmp | Out-Null }
    $env:TEMP = $msysTmp
    $env:TMP  = $msysTmp

    Invoke-Cmd $cmakeExe @(
        "-S", $scriptSlash,
        "-B", $buildSlash,
        "-G", "MinGW Makefiles",
        "-DCMAKE_MAKE_PROGRAM=$makeSlash",
        "-DCMAKE_C_COMPILER=$gccSlash",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DBTSTACK_ROOT=$btstackSlash"
    )
    Invoke-Cmd $cmakeExe @("--build", $buildSlash, "--target", "btstack_sink", "-j4")
}
finally {
    $env:PATH = $savedPath
    $env:TEMP = $savedTemp
    $env:TMP  = $savedTmp
}

# ── Done ─────────────────────────────────────────────────────────────────────
if (Test-Path $ExePath) {
    $size = (Get-Item $ExePath).Length / 1KB
    Write-Host "`nBuilt: $ExePath ($([int]$size) KB)" -ForegroundColor Green
} else {
    throw "Build completed but $ExePath not found."
}
