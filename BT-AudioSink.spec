# BT-AudioSink.spec – PyInstaller configuration
# ================================================
# Builds a standalone Windows .exe with a bundled FFmpeg binary.
#
# Usage:
#   pyinstaller BT-AudioSink.spec

import os
import sys
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

# Locate the FFmpeg binary provided by imageio-ffmpeg
try:
    import imageio_ffmpeg
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    ffmpeg_exe = None

# ---------------------------------------------------------------------------
# CustomTkinter: needs everything (themes, images, font data)
# ---------------------------------------------------------------------------
ctk_datas, ctk_binaries, ctk_hiddenimports = collect_all('customtkinter')

# ---------------------------------------------------------------------------
# Bumble: collect_all() would pull in all gRPC / Android-netsim protobuf
# files that we never use.  Instead:
#   - collect_data_files()   gets only the non-Python data bumble needs
#   - list explicit submodules for the BT profiles we actually import
# ---------------------------------------------------------------------------
bumble_datas = collect_data_files('bumble')

BUMBLE_MODULES = [
    # Core BT stack
    'bumble.core', 'bumble.hci', 'bumble.device',
    'bumble.l2cap', 'bumble.sdp', 'bumble.smp',
    'bumble.att', 'bumble.gatt', 'bumble.gap',
    'bumble.rfcomm', 'bumble.keys', 'bumble.pairing',
    # Profiles used by the app
    'bumble.avdtp', 'bumble.avrcp', 'bumble.a2dp',
    'bumble.profiles',
    # Transport layer (USB only)
    'bumble.transport', 'bumble.transport.usb',
]

a = Analysis(
    ['src/gui.py'],
    pathex=['.'],
    binaries=(
        [(ffmpeg_exe, '.')] if ffmpeg_exe and os.path.exists(ffmpeg_exe) else []
    ) + ctk_binaries,
    datas=bumble_datas + ctk_datas,
    hiddenimports=[
        # App dependencies
        'sounddevice', 'numpy', 'PIL', 'PIL._tkinter_finder',
        'customtkinter', 'pystray', 'backend', 'winusb_installer',
        # Bumble modules that may be imported at runtime
    ] + BUMBLE_MODULES + ctk_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Exclude transports and optional backends we never use
    excludes=[
        'grpc', 'grpcio',
        'bumble.transport.grpc_transport',
        'bumble.transport.grpc_protobuf',
        'bumble.transport.android_netsim',
        'bumble.transport.serial',
        'bumble.transport.hci_socket',
        'bumble.transport.tcp_client',
        'bumble.transport.tcp_server',
        'bumble.transport.vhci',
        'bumble.transport.udp',
        'bumble.apps',
        # Unused stdlib
        'sqlite3', 'unittest',
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
