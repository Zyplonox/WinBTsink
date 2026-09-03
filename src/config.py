"""
config.py – settings, data-directory paths and small host helpers
==================================================================
Shared by the GUI (gui.py) and the headless runner (headless.py); imports
nothing from Tk so it can run on a server without a display.

Settings are persisted to %APPDATA%\\BT-AudioSink\\config.json.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Optional

import sounddevice as sd

from backend import build_eq_filter

log = logging.getLogger("bt-sink.config")

APP_NAME = "BT-AudioSink"
VERSION = "2.1.0"


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )


# ---------------------------------------------------------------------------
# AppData directory helpers
# ---------------------------------------------------------------------------

def appdata_dir() -> str:
    """Returns %APPDATA%\\BT-AudioSink (or ~/BT-AudioSink on non-Windows)."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME)


def config_file() -> str:
    return os.path.join(appdata_dir(), "config.json")


def keystore_file() -> str:
    """BTstack link-key store (bonding keys), written by btstack_sink.exe."""
    return os.path.join(appdata_dir(), "btstack_keys.db")


def allowed_macs_file() -> str:
    """Remembered devices (see device_store.py)."""
    return os.path.join(appdata_dir(), "allowed_macs.json")


def migrate_legacy_keystore() -> Optional[str]:
    """
    Versions before 2.1 kept the bonding keys next to btstack_sink.exe
    (btstack\\build\\btstack_keys.db when running from source). Copy that
    file to the AppData location once, so already bonded devices keep
    connecting without pairing again. Returns the old path when migrated.
    """
    new = keystore_file()
    if os.path.exists(new):
        return None
    here = os.path.dirname(os.path.abspath(__file__))
    old = os.path.join(os.path.dirname(here), "btstack", "build", "btstack_keys.db")
    if not os.path.exists(old):
        return None
    try:
        import shutil
        os.makedirs(appdata_dir(), exist_ok=True)
        shutil.copy2(old, new)
        return old
    except OSError as exc:
        log.warning("Could not migrate bonding keys from %s: %s", old, exc)
        return None


# ---------------------------------------------------------------------------
# Audio output device enumeration
# ---------------------------------------------------------------------------

def enumerate_output_devices() -> tuple[list[str], list[Optional[int]]]:
    """
    Returns (display_names, device_indices) for the WASAPI output devices,
    with "Default" (index None) first.  Only the WASAPI host API is listed
    because sounddevice reports every device once per host API, and names
    are what gets persisted.
    """
    names: list[str] = ["Default"]
    indices: list[Optional[int]] = [None]
    try:
        wasapi = next((i for i, api in enumerate(sd.query_hostapis())
                       if "WASAPI" in api["name"].upper()), None)
        for i, dev in enumerate(sd.query_devices()):
            if dev["max_output_channels"] <= 0:  # type: ignore[index]
                continue
            if wasapi is not None and dev["hostapi"] != wasapi:  # type: ignore[index]
                continue
            names.append(dev["name"])  # type: ignore[index]
            indices.append(i)
    except Exception as exc:
        log.warning("Audio device enumeration failed: %s", exc)
    return names, indices


def resolve_output_device_index(name: Optional[str]) -> Optional[int]:
    """Maps a persisted device name to the current sounddevice index (None = default)."""
    if not name:
        return None
    names, indices = enumerate_output_devices()
    try:
        return indices[names.index(name)]
    except ValueError:
        log.warning("Audio device '%s' not found, using default", name)
        return None


# ---------------------------------------------------------------------------
# FFmpeg location
# ---------------------------------------------------------------------------

def get_ffmpeg() -> str:
    """
    Locates the FFmpeg executable.

    When running as a frozen PyInstaller bundle, look for ffmpeg.exe next
    to the extracted files in _MEIPASS.  Otherwise delegate to imageio-ffmpeg
    which bundles a pre-built binary, falling back to "ffmpeg" on PATH.
    """
    if getattr(sys, "frozen", False):
        path = os.path.join(sys._MEIPASS, "ffmpeg.exe")  # type: ignore[attr-defined]
        if os.path.exists(path):
            return path
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


# ---------------------------------------------------------------------------
# Windows autostart (registry)
# ---------------------------------------------------------------------------

_AUTOSTART_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_REG_NAME = APP_NAME


def autostart_command(gui_script: str) -> str:
    """The registry value that launches the GUI minimized on login."""
    exe = sys.executable
    if getattr(sys, "frozen", False):
        return f'"{exe}" --minimized'
    # From source: use pythonw.exe so no console window appears at login
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
        exe = pythonw
    return f'"{exe}" "{os.path.abspath(gui_script)}" --minimized'


def get_autostart() -> bool:
    """True when the autostart entry exists and points at an existing executable."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_REG_KEY, 0,
                            winreg.KEY_QUERY_VALUE) as key:
            value, _ = winreg.QueryValueEx(key, _AUTOSTART_REG_NAME)
        exe = value.split('"')[1] if value.startswith('"') else value.split(" ")[0]
        return os.path.exists(exe)
    except Exception:
        return False


def set_autostart(enabled: bool, gui_script: str) -> None:
    """
    Adds or removes the autostart registry entry.

    The --minimized flag is appended so Windows starts the app hidden in the
    system tray rather than showing the main window on login.
    """
    if sys.platform != "win32":
        return
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _AUTOSTART_REG_KEY, 0, winreg.KEY_SET_VALUE
        )
        if enabled:
            winreg.SetValueEx(key, _AUTOSTART_REG_NAME, 0, winreg.REG_SZ,
                              autostart_command(gui_script))
        else:
            try:
                winreg.DeleteValue(key, _AUTOSTART_REG_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        log.warning("Autostart registry: %s", e)


# ---------------------------------------------------------------------------
# Settings  (persisted to %APPDATA%\BT-AudioSink\config.json)
# ---------------------------------------------------------------------------

class Settings:
    """
    Holds all user-configurable parameters.

    Autostart state is intentionally read from the registry rather than
    config.json so it always reflects the true system state even if the
    registry entry was removed externally.
    """

    device_name: str = "PC-AudioSink"
    usb_filter: str = ""                   # Selected dongle; chosen at runtime, not persisted
    latency_ms: int = 50
    max_bitpool: int = 53
    audio_device_name: Optional[str] = None  # WASAPI output device; None = system default
    debug_mode: bool = False
    autostart: bool = False
    volume: float = 1.0
    discoverable_timeout_s: int = 0        # 0 = never auto-off
    class_of_device: int = 0x240418        # Headphones by default
    sbc_block_length: int = 0              # 4/8/12/16, 0 = Auto (source chooses)
    sbc_subbands: int = 0                  # 4/8, 0 = Auto
    sbc_allocation: str = "auto"           # "auto", "loudness" or "snr"
    media_keys: bool = False               # forward keyboard media keys via AVRCP
    notifications: bool = True             # Windows toasts on connect/disconnect/pairing
    offer_aptx: bool = False               # advertise aptX / aptX HD (experimental)
    multi_device_mode: str = "mix"         # "mix" | "duck" | "solo"
    duck_level: int = 25                   # background volume in percent for "duck"
    recording_dir: str = ""                # "" = ~/Music/BT-AudioSink
    eq_bass: int = 0                       # dB, -12..+12 (0 = flat)
    eq_mid: int = 0
    eq_treble: int = 0
    api_enabled: bool = False              # local HTTP control API (api_server.py)
    api_port: int = 8765
    language: str = "auto"                 # "auto" (Windows UI language), "en" or "de"
    update_check: bool = True              # ask GitHub for a newer release at start-up

    #: Keys persisted in config.json and the JSON types accepted for each.
    _PERSIST: dict[str, tuple[type, ...]] = {
        "device_name":            (str,),
        "latency_ms":             (int,),
        "max_bitpool":            (int,),
        "audio_device_name":      (str, type(None)),
        "debug_mode":             (bool,),
        "volume":                 (int, float),
        "discoverable_timeout_s": (int,),
        "class_of_device":        (int,),
        "sbc_block_length":       (int,),
        "sbc_subbands":           (int,),
        "sbc_allocation":         (str,),
        "media_keys":             (bool,),
        "notifications":          (bool,),
        "offer_aptx":             (bool,),
        "multi_device_mode":      (str,),
        "duck_level":             (int,),
        "recording_dir":          (str,),
        "eq_bass":                (int,),
        "eq_mid":                 (int,),
        "eq_treble":              (int,),
        "api_enabled":            (bool,),
        "api_port":               (int,),
        "language":               (str,),
        "update_check":           (bool,),
    }

    @property
    def audio_filter(self) -> str:
        return build_eq_filter(self.eq_bass, self.eq_mid, self.eq_treble)

    @property
    def effective_recording_dir(self) -> str:
        return self.recording_dir or os.path.join(os.path.expanduser("~"), "Music", APP_NAME)

    def backend_kwargs(self) -> dict:
        """SinkBackend constructor arguments derived from the settings."""
        return dict(
            device_name=self.device_name,
            usb_filter=self.usb_filter,
            latency_ms=self.latency_ms,
            max_bitpool=self.max_bitpool,
            volume=self.volume,
            audio_device_index=resolve_output_device_index(self.audio_device_name),
            ffmpeg_exe=get_ffmpeg(),
            debug=self.debug_mode,
            keystore_path=keystore_file(),
            discoverable_timeout_s=self.discoverable_timeout_s,
            class_of_device=self.class_of_device,
            sbc_block_length=self.sbc_block_length,
            sbc_subbands=self.sbc_subbands,
            sbc_allocation=self.sbc_allocation,
            offer_aptx=self.offer_aptx,
            multi_device_mode=self.multi_device_mode,
            duck_level=self.duck_level / 100.0,
            audio_filter=self.audio_filter,
        )

    def load(self) -> None:
        """
        Loads persisted values from config.json and autostart state from the
        registry.  Unreadable files and wrongly typed values fall back to the
        defaults instead of crashing the app at startup.
        """
        data: dict = {}
        try:
            with open(config_file(), encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except FileNotFoundError:
            pass  # First launch
        except Exception as e:
            log.warning("Config unreadable, using defaults: %s", e)

        for key, types in self._PERSIST.items():
            if key not in data:
                continue
            value = data[key]
            # bool is a subclass of int; keep it out of int-only fields
            if isinstance(value, bool) and bool not in types:
                continue
            if isinstance(value, types):
                setattr(self, key, value)
        if not 1024 <= self.api_port <= 65535:
            self.api_port = 8765
        self.autostart = get_autostart()

    def save(self) -> None:
        """Writes persisted values to config.json, creating the directory if needed."""
        try:
            os.makedirs(appdata_dir(), exist_ok=True)
            with open(config_file(), "w", encoding="utf-8") as f:
                json.dump({k: getattr(self, k) for k in self._PERSIST}, f, indent=2)
        except OSError as e:
            log.warning("Save settings: %s", e)


settings = Settings()
