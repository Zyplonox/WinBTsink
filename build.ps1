# build.ps1 - BT-AudioSink build script
# =======================================
# Produces dist\BT-AudioSink.exe in a single call.
#
# Prerequisites:
#   * Python 3.10+ on PATH
#   * Internet connection (pip downloads on first run; MSYS2/MinGW installed by btstack\build.ps1)
#
# Usage (from a PowerShell prompt):
#   powershell -ExecutionPolicy Bypass -File .\build.ps1

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "=== BT-AudioSink Build ===" -ForegroundColor Cyan
Write-Host ""

# 1. Build btstack_sink.exe (installs MSYS2/MinGW if needed, clones + patches BTstack).
#    Run in a child PowerShell so its PATH/TEMP changes cannot leak into the
#    pip/PyInstaller steps below.
Write-Host "[1/4] Building btstack_sink.exe (C / BTstack)..." -ForegroundColor Yellow
& powershell -NoProfile -ExecutionPolicy Bypass -File "$PSScriptRoot\btstack\build.ps1"
if ($LASTEXITCODE -ne 0) { Write-Error "btstack build failed"; exit 1 }

# 2. Install / update Python dependencies + PyInstaller (same interpreter for both)
Write-Host "[2/4] Installing Python dependencies..." -ForegroundColor Yellow
python -m pip install -r "$PSScriptRoot\requirements.txt" "pyinstaller>=6.0"
if ($LASTEXITCODE -ne 0) { Write-Error "pip install failed"; exit 1 }

# 3. Make sure the imageio-ffmpeg binary is present (it is bundled into the EXE)
Write-Host "[3/4] Locating FFmpeg via imageio-ffmpeg..." -ForegroundColor Yellow
python -c "import imageio_ffmpeg; print('FFmpeg:', imageio_ffmpeg.get_ffmpeg_exe())"
if ($LASTEXITCODE -ne 0) { Write-Error "imageio-ffmpeg could not provide an FFmpeg binary"; exit 1 }

# 4. Run PyInstaller
Write-Host "[4/4] Building EXE..." -ForegroundColor Yellow
python -m PyInstaller "$PSScriptRoot\BT-AudioSink.spec" --noconfirm
if ($LASTEXITCODE -ne 0) { Write-Error "PyInstaller build failed"; exit 1 }

Write-Host ""
Write-Host "=== Build successful! ===" -ForegroundColor Green
Write-Host ""
Write-Host "Output: dist\BT-AudioSink.exe" -ForegroundColor Green
Write-Host ""
