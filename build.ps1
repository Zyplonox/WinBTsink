# build.ps1 - BT-AudioSink build script
# =======================================
# Produces dist/BT-AudioSink.exe in a single call.
#
# Prerequisites:
#   * Python 3.10+ on PATH
#   * Internet connection (pip downloads on first run)
#
# Usage:
#   .\build.ps1

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "=== BT-AudioSink Build ===" -ForegroundColor Cyan
Write-Host ""

# 1. Install / update Python dependencies
Write-Host "[1/4] Installing Python dependencies..." -ForegroundColor Yellow
pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { Write-Error "pip install failed"; exit 1 }

# 2. Ensure PyInstaller is available
Write-Host "[2/4] Installing PyInstaller..." -ForegroundColor Yellow
pip install "pyinstaller>=6.0"
if ($LASTEXITCODE -ne 0) { Write-Error "PyInstaller installation failed"; exit 1 }

# 3. Prime the imageio-ffmpeg cache (FFmpeg binary is embedded in the EXE)
Write-Host "[3/4] Loading FFmpeg via imageio-ffmpeg..." -ForegroundColor Yellow
python -c "import imageio_ffmpeg; print('FFmpeg:', imageio_ffmpeg.get_ffmpeg_exe())"
if ($LASTEXITCODE -ne 0) {
    Write-Warning "imageio-ffmpeg could not locate FFmpeg - continuing build anyway."
}

# 4. Run PyInstaller
Write-Host "[4/4] Building EXE..." -ForegroundColor Yellow
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
