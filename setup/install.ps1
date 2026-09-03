# BT-AudioSink - Windows setup script (run from source)
# ======================================================
# Installs the Python dependencies. FFmpeg comes with the imageio-ffmpeg
# package, nothing else has to be installed by hand.
#
# Run: Right-Click -> "Run with PowerShell"
# Or:  powershell -ExecutionPolicy Bypass -File setup\install.ps1

$ErrorActionPreference = "Stop"
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir

function Write-Step($msg) { Write-Host "`n[STEP]  $msg" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "[OK]    $msg" -ForegroundColor Green }
function Write-Err($msg)  { Write-Host "[ERROR] $msg" -ForegroundColor Red }

function Fail($msg) {
    Write-Err $msg
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host ""
Write-Host "============================================" -ForegroundColor Magenta
Write-Host "  BT-AudioSink - Setup" -ForegroundColor Magenta
Write-Host "============================================" -ForegroundColor Magenta

# ---------------------------------------------------------------------------
# 1. Check Python (3.10+)
# ---------------------------------------------------------------------------
Write-Step "Checking Python..."
$pyver = ""
try { $pyver = (& python --version 2>&1) } catch { }
if ($LASTEXITCODE -ne 0 -or -not ($pyver -match "^Python (\d+)\.(\d+)")) {
    Write-Err "Python not found! Download: https://www.python.org/downloads/"
    Fail "Important: check 'Add Python to PATH' during installation."
}
Write-OK "Found: $pyver"
if ([int]$Matches[1] -lt 3 -or ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -lt 10)) {
    Fail "Python 3.10 or newer is required. Installed: $pyver"
}

# ---------------------------------------------------------------------------
# 2. Update pip
# ---------------------------------------------------------------------------
Write-Step "Updating pip..."
python -m pip install --upgrade pip --quiet
if ($LASTEXITCODE -ne 0) { Fail "pip upgrade failed." }
Write-OK "pip up to date"

# ---------------------------------------------------------------------------
# 3. Install Python packages (includes the FFmpeg binary via imageio-ffmpeg)
# ---------------------------------------------------------------------------
Write-Step "Installing Python packages..."
python -m pip install -r "$ProjectDir\requirements.txt"
if ($LASTEXITCODE -ne 0) { Fail "pip install failed." }
Write-OK "Python packages installed"

Write-Step "Checking FFmpeg (bundled with imageio-ffmpeg)..."
python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"
if ($LASTEXITCODE -ne 0) { Fail "imageio-ffmpeg could not provide an FFmpeg binary." }
Write-OK "FFmpeg available"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "  Setup complete!" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host @"

Next steps:
  1. Build the Bluetooth engine once:   .\btstack\build.ps1
  2. Install the WinUSB driver for your dongle (GUI: "Install WinUSB...")
  3. Double-click 'start.bat'  OR  run 'python src\gui.py'
  4. On your BT device (Switch, phone) pair with 'PC-AudioSink'
  5. Play audio -> sound comes from your PC speakers

"@
Read-Host "Press Enter to close"
