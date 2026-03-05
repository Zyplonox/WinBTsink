# build.ps1 - BT-AudioSink build script
# =======================================
# Produces dist/BT-AudioSink.exe in a single call.
#
# Prerequisites:
#   * Python 3.10+ on PATH
#   * Internet connection (pip downloads on first run; MSYS2/MinGW installed by btstack/build.ps1)
#
# Usage:
#   .\build.ps1

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "=== BT-AudioSink Build ===" -ForegroundColor Cyan
Write-Host ""

# 1. Build btstack_sink.exe (installs MSYS2/MinGW if needed, clones BTstack)
Write-Host "[1/5] Building btstack_sink.exe (C / BTstack)..." -ForegroundColor Yellow
& "$PSScriptRoot\btstack\build.ps1"
if ($LASTEXITCODE -ne 0) { Write-Error "btstack build failed"; exit 1 }

# 2. Install / update Python dependencies
Write-Host "[2/5] Installing Python dependencies..." -ForegroundColor Yellow
pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { Write-Error "pip install failed"; exit 1 }

# 3. Ensure PyInstaller is available
Write-Host "[3/5] Installing PyInstaller..." -ForegroundColor Yellow
pip install "pyinstaller>=6.0"
if ($LASTEXITCODE -ne 0) { Write-Error "PyInstaller installation failed"; exit 1 }

# 4. Prime the imageio-ffmpeg cache (FFmpeg binary is embedded in the EXE)
Write-Host "[4/5] Loading FFmpeg via imageio-ffmpeg..." -ForegroundColor Yellow
python -c "import imageio_ffmpeg; print('FFmpeg:', imageio_ffmpeg.get_ffmpeg_exe())"
if ($LASTEXITCODE -ne 0) {
    Write-Warning "imageio-ffmpeg could not locate FFmpeg - continuing build anyway."
}

# 5. Run PyInstaller
Write-Host "[5/5] Building EXE..." -ForegroundColor Yellow
python -m PyInstaller BT-AudioSink.spec --noconfirm
if ($LASTEXITCODE -ne 0) { Write-Error "PyInstaller build failed"; exit 1 }

Write-Host ""
Write-Host "=== Build successful! ===" -ForegroundColor Green
Write-Host ""
Write-Host "Output: dist\BT-AudioSink.exe" -ForegroundColor Green
Write-Host ""
Write-Host "Run:"
Write-Host "  dist\BT-AudioSink.exe"
Write-Host ""
