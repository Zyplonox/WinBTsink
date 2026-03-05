# BT-AudioSink.spec – PyInstaller configuration
# ================================================
# Builds a standalone Windows .exe with a bundled FFmpeg binary
# and btstack_sink.exe (the C BTstack subprocess).
#
# Usage:
#   pyinstaller BT-AudioSink.spec
#
# Prerequisites:
#   Run btstack/build.ps1 first to produce btstack/build/btstack_sink.exe.

import os
import sys
from PyInstaller.utils.hooks import collect_all, collect_data_files

# Locate the FFmpeg binary provided by imageio-ffmpeg
try:
    import imageio_ffmpeg
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    ffmpeg_exe = None

# Locate btstack_sink.exe built by btstack/build.ps1
btstack_exe = os.path.join(os.path.dirname(os.path.abspath(SPEC)),
                           'btstack', 'build', 'btstack_sink.exe')
if not os.path.exists(btstack_exe):
    raise FileNotFoundError(
        f"btstack_sink.exe not found at {btstack_exe}\n"
        "Run btstack/build.ps1 first."
    )

# ---------------------------------------------------------------------------
# CustomTkinter: needs everything (themes, images, font data)
# ---------------------------------------------------------------------------
ctk_datas, ctk_binaries, ctk_hiddenimports = collect_all('customtkinter')

# ---------------------------------------------------------------------------
# Bundled binaries
# ---------------------------------------------------------------------------
bundled_binaries = ctk_binaries + [(btstack_exe, '.')]
if ffmpeg_exe and os.path.exists(ffmpeg_exe):
    bundled_binaries.append((ffmpeg_exe, '.'))

a = Analysis(
    ['src/gui.py'],
    pathex=['.'],
    binaries=bundled_binaries,
    datas=ctk_datas,
    hiddenimports=[
        # App dependencies
        'sounddevice', 'numpy', 'PIL', 'PIL._tkinter_finder',
        'customtkinter', 'pystray', 'backend', 'winusb_installer',
    ] + ctk_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Bumble is no longer used
        'bumble',
        # Unused stdlib
        'sqlite3', 'unittest',
        'grpc', 'grpcio',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='BT-AudioSink',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX disabled: GUARD_CF on FFmpeg/numpy/Python DLLs blocks compression
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,      # No console window (windowed app)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,          # Optional: set to 'icon.ico'
    onefile=True,
)
