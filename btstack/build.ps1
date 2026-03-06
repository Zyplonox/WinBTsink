<#
.SYNOPSIS
    Builds btstack_sink.exe for WinBTsink.

.DESCRIPTION
    1. Installs MSYS2 (if missing) via direct download.
    2. Installs MinGW-w64 toolchain + CMake inside MSYS2.
    3. Clones BTstack at a pinned commit (if not already present).
    4. Applies the AVDTP deferred-accept patch (Python script).
    5. Runs CMake + make to produce btstack/build/btstack_sink.exe.

.NOTES
    Run from the project root:  .\btstack\build.ps1
    Or from inside btstack/:    .\build.ps1
#>

param(
    [switch]$Force   # re-clone and rebuild even if exe already exists
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Paths ────────────────────────────────────────────────────────────────────
$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$BtstackSrc  = Join-Path $ScriptDir "btstack-src"
$BuildDir    = Join-Path $ScriptDir "build"
$PatchScript = Join-Path $ScriptDir "patches\apply_avdtp_patch.py"
$ExePath     = Join-Path $BuildDir "btstack_sink.exe"

# Pinned BTstack tag (stable, tested with WinBTsink)
$BTSTACK_REPO   = "https://github.com/bluekitchen/btstack.git"
$BTSTACK_COMMIT = "v1.6.1"

# MSYS2 paths
$MSYS2Root = "C:\msys64"
$MingwBin  = "$MSYS2Root\mingw64\bin"
$Bash      = "$MSYS2Root\usr\bin\bash.exe"

# ── Helper ───────────────────────────────────────────────────────────────────
function Write-Step([string]$msg) {
    Write-Host "`n==> $msg" -ForegroundColor Cyan
}

function Invoke-Cmd([string]$exe, [string[]]$cmdArgs) {
    & $exe @cmdArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed (exit $LASTEXITCODE): $exe $($cmdArgs -join ' ')"
    }
}

# ── Step 1: MSYS2 ────────────────────────────────────────────────────────────
Write-Step "Checking MSYS2..."

if (-not (Test-Path $MSYS2Root)) {
    Write-Host "MSYS2 not found. Downloading installer..." -ForegroundColor Yellow

    $installer = "$env:TEMP\msys2-installer.exe"
    $url = "https://github.com/msys2/msys2-installer/releases/download/2024-01-13/msys2-x86_64-20240113.exe"

    Write-Host "Downloading from: $url"
    Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing
    Write-Host "Running installer (silent)..."
    Start-Process -FilePath $installer -ArgumentList "/S" -Wait
    Remove-Item $installer -Force -ErrorAction SilentlyContinue

    if (-not (Test-Path $MSYS2Root)) {
        throw "MSYS2 installation failed. Install manually from https://www.msys2.org/ to C:\msys64"
    }
}

Write-Host "MSYS2 found at $MSYS2Root" -ForegroundColor Green

# ── Step 2: MinGW toolchain + CMake + Git + patch ────────────────────────────
Write-Step "Installing MinGW-w64 toolchain..."

$packages = "mingw-w64-x86_64-gcc mingw-w64-x86_64-cmake git"
Invoke-Cmd $Bash @("-lc", "pacman -S --noconfirm --needed $packages")

$cmake = "$MingwBin\cmake.exe"
if (-not (Test-Path $cmake)) {
    throw "CMake not found at $cmake after installation."
}
Write-Host "CMake: $cmake" -ForegroundColor Green

# ── Step 3: Clone BTstack ────────────────────────────────────────────────────
Write-Step "Fetching BTstack ($BTSTACK_COMMIT)..."

$btstackUnix = $BtstackSrc -replace '\\', '/'

if (-not (Test-Path $BtstackSrc) -or $Force) {
    if (Test-Path $BtstackSrc) {
        Remove-Item $BtstackSrc -Recurse -Force
    }
    Invoke-Cmd $Bash @("-lc", "git clone --depth 1 --branch '$BTSTACK_COMMIT' '$BTSTACK_REPO' '$btstackUnix'")
} else {
    Write-Host "BTstack source already present (use -Force to re-clone)." -ForegroundColor DarkGray
}

# ── Step 4: Apply AVDTP deferred-accept patch ────────────────────────────────
Write-Step "Applying AVDTP deferred-accept patch..."

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    $python = Get-Command python3 -ErrorAction SilentlyContinue
}
if (-not $python) {
    throw "Python not found on PATH. Install Python 3 and retry."
}

& $python.Source $PatchScript $BtstackSrc
if ($LASTEXITCODE -ne 0) {
    throw "AVDTP patch failed."
}
Write-Host "Patch applied." -ForegroundColor Green

# ── Step 5: CMake configure + build ─────────────────────────────────────────
Write-Step "Building btstack_sink.exe..."

if (-not (Test-Path $BuildDir)) {
    New-Item -ItemType Directory -Path $BuildDir | Out-Null
}

# cmake.exe, gcc.exe and mingw32-make.exe are native Windows executables —
# call them directly from PowerShell to avoid bash PATH issues.
$cmakeExe = "$MingwBin\cmake.exe"

# CMake parses its -D arguments as CMake code, so backslashes are escape
# sequences. Use forward slashes for all paths passed as -D values.
$makeSlash     = "$MingwBin\mingw32-make.exe" -replace '\\', '/'
$gccSlash      = "$MingwBin\gcc.exe"          -replace '\\', '/'
$scriptSlash   = $ScriptDir                   -replace '\\', '/'
$buildSlash    = $BuildDir                    -replace '\\', '/'
$btstackSlash  = $BtstackSrc                  -replace '\\', '/'

# GCC needs a writable TEMP directory; the default C:\Windows\Temp is
# inaccessible in some environments.
$env:TEMP = "$MSYS2Root\tmp"
$env:TMP  = "$MSYS2Root\tmp"

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

# ── Done ─────────────────────────────────────────────────────────────────────
if (Test-Path $ExePath) {
    $size = (Get-Item $ExePath).Length / 1KB
    Write-Host "`nBuilt: $ExePath ($([int]$size) KB)" -ForegroundColor Green
} else {
    throw "Build completed but $ExePath not found."
}
