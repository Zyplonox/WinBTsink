<#
.SYNOPSIS
    Builds btstack_sink.exe for WinBTsink.

.DESCRIPTION
    1. Installs MSYS2 (if missing) via winget or manual download.
    2. Installs MinGW-w64 toolchain + CMake inside MSYS2.
    3. Clones BTstack at a pinned commit (if not already present).
    4. Applies the AVDTP deferred-accept patch.
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
$PatchFile   = Join-Path $ScriptDir "patches\avdtp_deferred_accept.patch"
$ExePath     = Join-Path $BuildDir "btstack_sink.exe"

# Pinned BTstack commit (stable, tested with WinBTsink)
$BTSTACK_REPO   = "https://github.com/bluekitchen/btstack.git"
$BTSTACK_COMMIT = "v1.6.1"   # update when re-testing

# MSYS2 install path (default location used by winget)
$MSYS2Root = "C:\msys64"
$MingwBin  = "$MSYS2Root\mingw64\bin"
$Pacman    = "$MSYS2Root\usr\bin\pacman.exe"
$Bash      = "$MSYS2Root\usr\bin\bash.exe"

# ── Helpers ──────────────────────────────────────────────────────────────────
function Write-Step([string]$msg) {
    Write-Host "`n==> $msg" -ForegroundColor Cyan
}

function Invoke-Native([string]$exe, [string[]]$args) {
    & $exe @args
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed (exit $LASTEXITCODE): $exe $args"
    }
}

# ── Step 1: MSYS2 ────────────────────────────────────────────────────────────
Write-Step "Checking MSYS2..."

if (-not (Test-Path $MSYS2Root)) {
    Write-Host "MSYS2 not found. Installing via winget..." -ForegroundColor Yellow

    # Try winget first
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Invoke-Native winget @("install", "--id", "MSYS2.MSYS2", "--silent", "--accept-package-agreements", "--accept-source-agreements")
    } else {
        # Fallback: direct download
        $installer = "$env:TEMP\msys2-installer.exe"
        Write-Host "Downloading MSYS2 installer..."
        $url = "https://github.com/msys2/msys2-installer/releases/download/2024-01-13/msys2-x86_64-20240113.exe"
        Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing
        Start-Process -FilePath $installer -ArgumentList "/S" -Wait
        Remove-Item $installer -Force
    }

    if (-not (Test-Path $MSYS2Root)) {
        throw "MSYS2 installation failed. Please install MSYS2 manually from https://www.msys2.org/ to C:\msys64"
    }
}

Write-Host "MSYS2 found at $MSYS2Root" -ForegroundColor Green

# ── Step 2: MinGW toolchain + CMake + Git ────────────────────────────────────
Write-Step "Installing MinGW-w64 toolchain..."

# Run pacman in MSYS2 shell (non-interactive, no confirm)
$packages = "mingw-w64-x86_64-gcc mingw-w64-x86_64-cmake mingw-w64-x86_64-ninja git patch"
Invoke-Native $Bash @("-lc", "pacman -S --noconfirm --needed $packages")

# Verify cmake is accessible
$cmake = "$MingwBin\cmake.exe"
if (-not (Test-Path $cmake)) {
    throw "CMake not found at $cmake after installation."
}
Write-Host "CMake: $cmake" -ForegroundColor Green

# ── Step 3: Clone BTstack ────────────────────────────────────────────────────
Write-Step "Fetching BTstack ($BTSTACK_COMMIT)..."

if (-not (Test-Path $BtstackSrc) -or $Force) {
    if (Test-Path $BtstackSrc) {
        Remove-Item $BtstackSrc -Recurse -Force
    }
    Invoke-Native $Bash @("-lc", "git clone --depth 1 --branch '$BTSTACK_COMMIT' '$BTSTACK_REPO' '$($BtstackSrc -replace '\\', '/')'")
} else {
    Write-Host "BTstack source already present (use -Force to re-clone)." -ForegroundColor DarkGray
}

# ── Step 4: Apply AVDTP deferred-accept patch ────────────────────────────────
Write-Step "Applying AVDTP deferred-accept patch..."

$patchUnix   = $PatchFile  -replace '\\', '/'
$btstackUnix = $BtstackSrc -replace '\\', '/'

# Check if already applied (idempotent)
$checkCmd = "patch --dry-run -d '$btstackUnix' -p1 < '$patchUnix' 2>&1"
$alreadyApplied = $false
try {
    $out = & $Bash -lc "cd '$btstackUnix' && patch --dry-run -p1 < '$patchUnix' 2>&1"
    if ($out -match "already applied|Reversed") {
        $alreadyApplied = $true
    }
} catch { }

if (-not $alreadyApplied) {
    Invoke-Native $Bash @("-lc", "cd '$btstackUnix' && patch -p1 < '$patchUnix'")
    Write-Host "Patch applied." -ForegroundColor Green
} else {
    Write-Host "Patch already applied." -ForegroundColor DarkGray
}

# ── Step 5: CMake configure + build ─────────────────────────────────────────
Write-Step "Building btstack_sink.exe..."

if (-not (Test-Path $BuildDir)) {
    New-Item -ItemType Directory -Path $BuildDir | Out-Null
}

$scriptUnix = $ScriptDir -replace '\\', '/'
$buildUnix  = $BuildDir  -replace '\\', '/'

$cmakeCmd = "cmake -S '$scriptUnix' -B '$buildUnix' -G 'MinGW Makefiles' " +
            "-DCMAKE_BUILD_TYPE=Release " +
            "-DBTSTACK_ROOT='$btstackUnix'"

Invoke-Native $Bash @("-lc", $cmakeCmd)
Invoke-Native $Bash @("-lc", "cmake --build '$buildUnix' --target btstack_sink -j4")

# ── Done ─────────────────────────────────────────────────────────────────────
if (Test-Path $ExePath) {
    $size = (Get-Item $ExePath).Length / 1KB
    Write-Host "`nBuilt: $ExePath ($([int]$size) KB)" -ForegroundColor Green
} else {
    throw "Build succeeded but $ExePath not found."
}
