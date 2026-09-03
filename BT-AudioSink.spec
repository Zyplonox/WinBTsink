# BT-AudioSink.spec – PyInstaller configuration
# ================================================
# Builds a standalone one-file Windows .exe containing the Python GUI,
# btstack_sink.exe (the C BTstack engine) and one FFmpeg binary.
#
# Usage:
#   python -m PyInstaller BT-AudioSink.spec
#
# Prerequisites:
#   Run btstack\build.ps1 first to produce btstack\build\btstack_sink.exe.

import os
import shutil
from PyInstaller.utils.hooks import collect_all

SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))

# ---------------------------------------------------------------------------
# btstack_sink.exe built by btstack/build.ps1
# ---------------------------------------------------------------------------
btstack_exe = os.path.join(SPEC_DIR, 'btstack', 'build', 'btstack_sink.exe')
if not os.path.exists(btstack_exe):
    raise FileNotFoundError(
        f"btstack_sink.exe not found at {btstack_exe}\nRun btstack\\build.ps1 first."
    )

# ---------------------------------------------------------------------------
# FFmpeg: take the binary imageio-ffmpeg downloaded, but ship it exactly once
# under the fixed name gui._get_ffmpeg() looks for (ffmpeg.exe in _MEIPASS).
# imageio-ffmpeg's own PyInstaller hook would add a second ~80 MB copy under
# imageio_ffmpeg/binaries/, so that is filtered out below.
# ---------------------------------------------------------------------------
import imageio_ffmpeg
ffmpeg_src = imageio_ffmpeg.get_ffmpeg_exe()
ffmpeg_stage = os.path.join(SPEC_DIR, 'build', 'ffmpeg.exe')
os.makedirs(os.path.dirname(ffmpeg_stage), exist_ok=True)
shutil.copy2(ffmpeg_src, ffmpeg_stage)

# ---------------------------------------------------------------------------
# CustomTkinter: needs everything (themes, images, font data)
# ---------------------------------------------------------------------------
ctk_datas, ctk_binaries, ctk_hiddenimports = collect_all('customtkinter')

a = Analysis(
    ['src/gui.py'],
    pathex=['src'],
    binaries=ctk_binaries + [(btstack_exe, '.'), (ffmpeg_stage, '.')],
    datas=ctk_datas,
    hiddenimports=[
        'PIL._tkinter_finder',   # loaded dynamically by Pillow's ImageTk
        'pystray._win32',        # pystray picks its backend at runtime
    ] + ctk_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['sqlite3', 'unittest'],
    noarchive=False,
)

# Drop the duplicate FFmpeg that hook-imageio_ffmpeg collects.
a.datas = [d for d in a.datas if 'imageio_ffmpeg' not in d[0].replace('\\', '/')
           or '/binaries/' not in d[0].replace('\\', '/')]
a.binaries = [b for b in a.binaries if 'imageio_ffmpeg' not in b[0].replace('\\', '/')
              or '/binaries/' not in b[0].replace('\\', '/')]

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
