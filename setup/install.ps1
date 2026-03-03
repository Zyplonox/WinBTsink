# Bluetooth A2DP Sink – Windows 11 Setup Script
# ================================================
# Installs Python dependencies & FFmpeg.
#
# Run: Right-Click → "Run with PowerShell"
# Or:  powershell -ExecutionPolicy Bypass -File setup\install.ps1

$ErrorActionPreference = "Stop"
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir

function Write-Step($msg) { Write-Host "`n[STEP]  $msg" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "[OK]    $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "[WARN]  $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "[ERROR] $msg" -ForegroundColor Red }

Write-Host ""
Write-Host "============================================" -ForegroundColor Magenta
Write-Host "  Bluetooth A2DP Sink – Setup" -ForegroundColor Magenta
Write-Host "============================================" -ForegroundColor Magenta

# ---------------------------------------------------------------------------
# 1. Check Python
# ---------------------------------------------------------------------------
Write-Step "Checking Python..."
try {
    $pyver = python --version 2>&1
    Write-OK "Found: $pyver"

    # Require Python 3.10+
    $vernum = ($pyver -replace "Python ", "").Split(".")
    if ([int]$vernum[0] -lt 3 -or ([int]$vernum[0] -eq 3 -and [int]$vernum[1] -lt 10)) {
        Write-Err "Python 3.10 or newer is required. Installed: $pyver"
        Write-Err "Download: https://www.python.org/downloads/"
        Read-Host "Press Enter to exit"
        exit 1
    }
} catch {
    Write-Err "Python not found!"
    Write-Err "Download: https://www.python.org/downloads/"
    Write-Err "Important: check 'Add Python to PATH' during installation!"
    Read-Host "Press Enter to exit"
    exit 1
}

# ---------------------------------------------------------------------------
# 2. Update pip
# ---------------------------------------------------------------------------
Write-Step "Updating pip..."
python -m pip install --upgrade pip --quiet
Write-OK "pip up to date"

# ---------------------------------------------------------------------------
# 3. Install Python packages
# ---------------------------------------------------------------------------
Write-Step "Installing Python packages (bumble, sounddevice, numpy, pyusb)..."
python -m pip install -r "$ProjectDir\requirements.txt"
Write-OK "Python packages installed"

# ---------------------------------------------------------------------------
# 4. Check / install FFmpeg
# ---------------------------------------------------------------------------
Write-Step "Checking FFmpeg..."
$ffmpegOk = $false
try {
    $null = & ffmpeg -version 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-OK "FFmpeg already installed"
        $ffmpegOk = $true
    }
} catch { }

if (-not $ffmpegOk) {
    Write-Warn "FFmpeg not found – attempting installation via winget..."
    try {
        winget install --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
        Write-OK "FFmpeg installed"
        Write-Warn "IMPORTANT: Open a new terminal so FFmpeg is available on PATH!"
    } catch {
        Write-Err "winget installation failed."
        Write-Host ""
        Write-Host "Install FFmpeg manually:" -ForegroundColor Yellow
        Write-Host "  1. https://www.gyan.dev/ffmpeg/builds/ → ffmpeg-release-essentials.zip"
        Write-Host "  2. Extract to e.g. C:\ffmpeg\"
        Write-Host "  3. Add C:\ffmpeg\bin\ to the system PATH"
        Write-Host "     (Control Panel → Environment Variables → PATH)"
    }
}

# ---------------------------------------------------------------------------
# 5. Verify start.bat
# ---------------------------------------------------------------------------
Write-Step "Checking launch script..."
if (Test-Path "$ProjectDir\start.bat") {
    Write-OK "start.bat found"
} else {
    Write-Warn "start.bat not found"
}

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "  Setup complete!" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host @"

Next steps:
  1. Open a new terminal
  2. Double-click 'start.bat'  OR
     run 'python src\gui.py'
  3. On your BT device (Switch, phone) pair with 'PC-AudioSink'
  4. Play audio → sound comes from your PC speakers

Troubleshooting:
  – Wrong USB index: Settings → USB Dongle → try usb:1, usb:2
  – Reset driver: Device Manager → device → Update driver

"@
Read-Host "Press Enter to close"