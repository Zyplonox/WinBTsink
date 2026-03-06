"""
gui.py – BT-AudioSink GUI
==========================
CustomTkinter dark-mode interface for the Bluetooth A2DP Sink backend.
Entry-point for PyInstaller (see BT-AudioSink.spec).

Callback threading model
------------------------
All SinkBackend callbacks arrive on the backend daemon thread.
The GUI schedule every callback through root.after(0, fn, arg) so Tkinter
state is only ever mutated from the mainloop thread.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from datetime import datetime
from typing import Optional

import customtkinter as ctk
import sounddevice as sd
from PIL import Image, ImageDraw

from backend import SinkBackend, SinkState

try:
    from winusb_installer import list_native_bt_devices, download_and_run_zadig
    _WINUSB_AVAILABLE = sys.platform == "win32"
except ImportError:
    _WINUSB_AVAILABLE = False

try:
    import pystray as _pystray
    _TRAY_AVAILABLE = True
except ImportError:
    _TRAY_AVAILABLE = False


# ---------------------------------------------------------------------------
# AppData directory helpers
# ---------------------------------------------------------------------------

def _appdata_dir() -> str:
    """Returns %APPDATA%\\BT-AudioSink (or ~/BT-AudioSink on non-Windows)."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "BT-AudioSink")

def _config_file() -> str:
    return os.path.join(_appdata_dir(), "config.json")

def _keys_file() -> str:
    return os.path.join(_appdata_dir(), "keys.json")

def _allowed_macs_file() -> str:
    return os.path.join(_appdata_dir(), "allowed_macs.json")


# ---------------------------------------------------------------------------
# USB dongle enumeration
# ---------------------------------------------------------------------------

#: USB class triple that identifies a Bluetooth HCI transport.
def scan_bt_dongles() -> list[tuple[int, str]]:
    """
    Enumerate USB Bluetooth HCI dongles that have WinUSB as their active
    driver. Returns (btstack_index, label) pairs for use as usb:N transport.

    Reads HKLM\\SYSTEM\\CurrentControlSet\\Enum\\USB from the registry —
    fast, no DLL calls that can block.
    """
    import winreg
    import re

    # Bluetooth device class GUID (standard Windows)
    BT_CLASS_GUID = "{E0CBF06C-CD8B-4647-BB8A-263B43F0F974}"

    results: list[tuple[int, str]] = []
    bt_idx = 0

    try:
        usb_root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                  r"SYSTEM\CurrentControlSet\Enum\USB")
    except OSError:
        return []

    with usb_root:
        i = 0
        while True:
            try:
                dev_id = winreg.EnumKey(usb_root, i)
            except OSError:
                break
            i += 1

            try:
                dev_key = winreg.OpenKey(usb_root, dev_id)
            except OSError:
                continue

            with dev_key:
                j = 0
                while True:
                    try:
                        instance = winreg.EnumKey(dev_key, j)
                    except OSError:
                        break
                    j += 1

                    try:
                        inst_key = winreg.OpenKey(dev_key, instance)
                    except OSError:
                        continue

                    with inst_key:
                        # Must have WinUSB as service
                        try:
                            service = winreg.QueryValueEx(inst_key, "Service")[0]
                        except OSError:
                            continue
                        if service.upper() != "WINUSB":
                            continue

                        # Identify as Bluetooth: class GUID, hardware ID, or friendly name
                        is_bt = False
                        try:
                            cg = winreg.QueryValueEx(inst_key, "ClassGUID")[0].upper()
                            is_bt = cg == BT_CLASS_GUID
                        except OSError:
                            pass

                        if not is_bt:
                            try:
                                hw = winreg.QueryValueEx(inst_key, "HardwareID")[0]
                                hw_str = (" ".join(hw) if isinstance(hw, list) else hw).upper()
                                is_bt = "CLASS_E0" in hw_str or "SUBCLASS_01" in hw_str
                            except OSError:
                                pass

                        if not is_bt:
                            try:
                                fn = winreg.QueryValueEx(inst_key, "FriendlyName")[0].upper()
                                is_bt = "BLUETOOTH" in fn
                            except OSError:
                                pass

                        if not is_bt:
                            continue

                        try:
                            friendly = winreg.QueryValueEx(inst_key, "FriendlyName")[0]
                        except OSError:
                            friendly = "Bluetooth HCI"

                        m = re.search(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})",
                                      dev_id, re.I)
                        vid_pid = (f"  [VID:{m.group(1).upper()} PID:{m.group(2).upper()}]"
                                   if m else "")

                        results.append((bt_idx, f"usb:{bt_idx}  {friendly}{vid_pid}"))
                        bt_idx += 1

    return results


def _get_ffmpeg() -> str:
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
_AUTOSTART_REG_NAME = "BT-AudioSink"


def _get_autostart() -> bool:
    """Returns True when the autostart registry entry exists."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _AUTOSTART_REG_KEY, 0, winreg.KEY_QUERY_VALUE
        )
        winreg.QueryValueEx(key, _AUTOSTART_REG_NAME)
        winreg.CloseKey(key)
        return True
    except Exception:
        return False


def _set_autostart(enabled: bool) -> None:
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
            exe = sys.executable
            if getattr(sys, "frozen", False):
                value = f'"{exe}" --minimized'
            else:
                value = f'"{exe}" "{os.path.abspath(__file__)}" --minimized'
            winreg.SetValueEx(key, _AUTOSTART_REG_NAME, 0, winreg.REG_SZ, value)
        else:
            try:
                winreg.DeleteValue(key, _AUTOSTART_REG_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        log.warning("Autostart registry: %s", e)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bt-sink.gui")


# ---------------------------------------------------------------------------
# State display mappings
# ---------------------------------------------------------------------------

STATE_COLORS = {
    SinkState.IDLE:      "#6B7280",  # grey  – not started
    SinkState.STARTING:  "#F59E0B",  # amber – connecting
    SinkState.READY:     "#3B82F6",  # blue  – waiting for source
    SinkState.CONNECTED: "#10B981",  # green – streaming
    SinkState.ERROR:     "#EF4444",  # red   – transport or pipeline error
    SinkState.STOPPED:   "#6B7280",  # grey  – cleanly stopped
}

STATE_LABELS = {
    SinkState.IDLE:      "Ready",
    SinkState.STARTING:  "Starting…",
    SinkState.READY:     "Waiting for device…",
    SinkState.CONNECTED: "Connected",
    SinkState.ERROR:     "Error",
    SinkState.STOPPED:   "Stopped",
}


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
    bt_address: str = "F0:F1:F2:F3:F4:F5"
    transport: str = "usb:0"       # Determined at runtime via scan, not persisted
    latency_ms: int = 50
    max_bitpool: int = 53
    audio_device_index: Optional[int] = None
    debug_mode: bool = False
    autostart: bool = False
    volume: float = 1.0

    #: Keys written to / read from config.json.  'transport' is excluded.
    _PERSIST = (
        "device_name", "bt_address", "latency_ms", "max_bitpool",
        "audio_device_index", "debug_mode", "volume",
    )

    def load(self) -> None:
        """Loads persisted values from config.json and autostart state from registry."""
        try:
            with open(_config_file(), encoding="utf-8") as f:
                data = json.load(f)
            for key in self._PERSIST:
                if key in data:
                    setattr(self, key, data[key])
        except (FileNotFoundError, json.JSONDecodeError):
            pass  # First launch or corrupted file – keep defaults
        self.autostart = _get_autostart()

    def save(self) -> None:
        """Writes persisted values to config.json, creating the directory if needed."""
        try:
            os.makedirs(_appdata_dir(), exist_ok=True)
            with open(_config_file(), "w", encoding="utf-8") as f:
                json.dump({k: getattr(self, k) for k in self._PERSIST}, f, indent=2)
        except OSError as e:
            log.warning("Save settings: %s", e)


settings = Settings()


# ---------------------------------------------------------------------------
# Settings Dialog
# ---------------------------------------------------------------------------

class SettingsDialog(ctk.CTkToplevel):
    """
    Modal settings window.  Changes are only applied when the user clicks
    Save; Cancel leaves all settings unchanged.
    """

    def __init__(self, parent: "App"):
        super().__init__(parent)
        self.title("Settings")
        self.geometry("420x670")
        self.resizable(False, False)
        self.grab_set()  # Block interaction with the main window

        self._device_indices: list[Optional[int]] = []  # parallel to audio dropdown

        self._add_device_name_row()
        self._add_bt_address_row()
        self._add_latency_row()
        self._add_bitpool_row()
        self._add_audio_device_row()
        self._add_checkboxes()
        self._add_clear_keys_row()
        self._add_buttons()

    # ------------------------------------------------------------------
    # Row builders  (each adds one logical setting to the dialog)
    # ------------------------------------------------------------------

    def _add_device_name_row(self) -> None:
        """Text entry for the Bluetooth advertised name."""
        ctk.CTkLabel(self, text="Device name", anchor="w").pack(
            fill="x", padx=20, pady=8
        )
        self._name_var = ctk.StringVar(value=settings.device_name)
        ctk.CTkEntry(self, textvariable=self._name_var).pack(
            fill="x", padx=20, pady=0
        )

    def _add_bt_address_row(self) -> None:
        """Text entry for the local Bluetooth address used by bumble."""
        ctk.CTkLabel(self, text="Bluetooth address", anchor="w").pack(
            fill="x", padx=20, pady=8
        )
        self._btaddr_var = ctk.StringVar(value=settings.bt_address)
        ctk.CTkEntry(self, textvariable=self._btaddr_var).pack(
            fill="x", padx=20, pady=0
        )
        ctk.CTkLabel(
            self,
            text="Format: AA:BB:CC:DD:EE:FF  (change if address conflicts with another device)",
            anchor="w", font=ctk.CTkFont(size=11), text_color="#9CA3AF",
        ).pack(fill="x", padx=20)

    def _add_latency_row(self) -> None:
        """Slider for the sounddevice output buffer size (in milliseconds)."""
        ctk.CTkLabel(self, text="Buffer latency", anchor="w").pack(
            fill="x", padx=20, pady=8
        )
        self._latency_var = ctk.IntVar(value=settings.latency_ms)
        ctk.CTkSlider(
            self, from_=20, to=500, number_of_steps=48,
            variable=self._latency_var,
            command=self._on_latency_change,
        ).pack(fill="x", padx=20, pady=0)
        self._latency_label = ctk.CTkLabel(
            self,
            text=self._latency_text(settings.latency_ms),
            anchor="w", font=ctk.CTkFont(size=11), text_color="#9CA3AF",
        )
        self._latency_label.pack(fill="x", padx=20)

    def _add_bitpool_row(self) -> None:
        """Slider for the maximum SBC bitpool value (audio quality ceiling)."""
        ctk.CTkLabel(self, text="Max SBC bitpool", anchor="w").pack(
            fill="x", padx=20, pady=8
        )
        self._bitpool_var = ctk.IntVar(value=settings.max_bitpool)
        ctk.CTkSlider(
            self, from_=2, to=75, number_of_steps=73,
            variable=self._bitpool_var,
            command=self._on_bitpool_change,
        ).pack(fill="x", padx=20, pady=0)
        self._bitpool_label = ctk.CTkLabel(
            self,
            text=self._bitpool_text(settings.max_bitpool),
            anchor="w", font=ctk.CTkFont(size=11), text_color="#9CA3AF",
        )
        self._bitpool_label.pack(fill="x", padx=20)

    def _add_audio_device_row(self) -> None:
        """Dropdown listing all WASAPI output devices."""
        ctk.CTkLabel(self, text="Audio output device", anchor="w").pack(
            fill="x", padx=20, pady=8
        )
        display_names, self._device_indices = self._enumerate_output_devices()

        # Pre-select the currently configured device
        current_idx = self._index_of_current_device(self._device_indices)
        self._audio_var = ctk.StringVar(value=display_names[current_idx])
        ctk.CTkOptionMenu(
            self, values=display_names, variable=self._audio_var
        ).pack(fill="x", padx=20, pady=0)

    def _add_checkboxes(self) -> None:
        """Debug mode and autostart toggle checkboxes."""
        self._debug_var = ctk.BooleanVar(value=settings.debug_mode)
        ctk.CTkCheckBox(
            self,
            text="Debug log (shows AVDTP/L2CAP protocol)",
            variable=self._debug_var,
        ).pack(anchor="w", padx=20, pady=(16, 0))

        self._autostart_var = ctk.BooleanVar(value=settings.autostart)
        ctk.CTkCheckBox(
            self,
            text="Start with Windows (autostart, minimized to tray)",
            variable=self._autostart_var,
        ).pack(anchor="w", padx=20, pady=(8, 0))

    def _add_clear_keys_row(self) -> None:
        """Button to wipe all saved bonding keys."""
        ctk.CTkButton(
            self,
            text="Clear saved devices (delete keys.json)",
            fg_color="#374151", hover_color="#6B7280",
            command=self._clear_keys,
        ).pack(fill="x", padx=20, pady=(20, 0))

    def _clear_keys(self) -> None:
        deleted = []
        errors = []
        for path in (_keys_file(), _allowed_macs_file()):
            try:
                if os.path.exists(path):
                    os.remove(path)
                    deleted.append(os.path.basename(path))
            except Exception as exc:
                errors.append(f"{os.path.basename(path)}: {exc}")

        # Also wipe the in-memory allowed-MACs set in the running backend
        if hasattr(self.master, "_backend") and self.master._backend:
            self.master._backend.clear_allowed_macs()

        if errors:
            msg = "Error clearing devices: " + ", ".join(errors)
        elif deleted:
            msg = f"Saved devices cleared ({', '.join(deleted)}) – all devices must re-pair."
        else:
            msg = "No saved devices found (nothing to delete)."

        if hasattr(self.master, "_log"):
            self.master._log(msg)

    def _add_buttons(self) -> None:
        """Cancel / Save button row at the bottom of the dialog."""
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.pack(fill="x", padx=20, pady=20)
        ctk.CTkButton(
            frame, text="Cancel", fg_color="#6B7280", command=self.destroy
        ).pack(side="left", expand=True, padx=(0, 5))
        ctk.CTkButton(
            frame, text="Save", command=self._save
        ).pack(side="left", expand=True, padx=(5, 0))

    # ------------------------------------------------------------------
    # Slider label helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _latency_text(ms: int) -> str:
        note = "  ⚠ may cause audio dropouts" if ms < 50 else \
               "  (BT radio latency ~60 ms is not configurable)"
        return f"{ms} ms{note}"

    @staticmethod
    def _bitpool_text(bp: int) -> str:
        return f"{bp}  (higher = better audio quality, more bandwidth)"

    def _on_latency_change(self, value: float) -> None:
        self._latency_label.configure(text=self._latency_text(int(value)))

    def _on_bitpool_change(self, value: float) -> None:
        self._bitpool_label.configure(text=self._bitpool_text(int(value)))

    # ------------------------------------------------------------------
    # Audio device helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _enumerate_output_devices() -> tuple[list[str], list[Optional[int]]]:
        """
        Returns (display_names, device_indices) lists for all available
        WASAPI output devices.  The first entry is always "Default" (index None).
        """
        names: list[str] = ["Default"]
        indices: list[Optional[int]] = [None]
        for i, dev in enumerate(sd.query_devices()):
            if dev["max_output_channels"] > 0:  # type: ignore[index]
                names.append(f"{i}: {dev['name']}")  # type: ignore[index]
                indices.append(i)
        return names, indices

    @staticmethod
    def _index_of_current_device(indices: list[Optional[int]]) -> int:
        """Returns the position of the currently configured audio device in *indices*."""
        if settings.audio_device_index is not None:
            try:
                return indices.index(settings.audio_device_index)
            except ValueError:
                pass
        return 0  # Fall back to "Default"

    def _resolve_audio_device_index(self, selected_label: str) -> Optional[int]:
        """
        Maps the selected dropdown label back to a sounddevice device index.
        Returns None (system default) when the label is not found.
        """
        display_names, indices = self._enumerate_output_devices()
        try:
            return indices[display_names.index(selected_label)]
        except (ValueError, IndexError):
            return None

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _save(self) -> None:
        """Writes all dialog values back to the Settings object and persists them."""
        settings.device_name = self._name_var.get().strip() or "PC-AudioSink"
        settings.bt_address = self._btaddr_var.get().strip() or "F0:F1:F2:F3:F4:F5"
        settings.latency_ms = int(self._latency_var.get())
        settings.max_bitpool = int(self._bitpool_var.get())
        settings.audio_device_index = self._resolve_audio_device_index(
            self._audio_var.get()
        )
        settings.debug_mode = self._debug_var.get()
        settings.autostart = self._autostart_var.get()
        _set_autostart(settings.autostart)  # Sync registry immediately
        settings.save()
        self.destroy()


# ---------------------------------------------------------------------------
# WinUSB Driver Dialog
# ---------------------------------------------------------------------------

class WinUSBDialog(ctk.CTkToplevel):
    """
    Shows BT dongles that still use the native Windows driver and guides
    the user through installing WinUSB via Zadig.
    """

    def __init__(self, parent, on_close=None):
        super().__init__(parent)
        self._on_close_cb = on_close
        self.title("Install WinUSB Driver")
        self.geometry("520x520")
        self.resizable(False, False)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._close)

        self._build_description()
        self._build_instructions()
        self._build_device_list()
        self._build_action_buttons()

        # Auto-scan when the dialog opens
        self.after(200, self._scan)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_description(self) -> None:
        ctk.CTkLabel(
            self,
            text=(
                "Windows requires the WinUSB driver so Bumble can access\n"
                "the Bluetooth USB dongle directly."
            ),
            wraplength=480, justify="left", anchor="w",
        ).pack(fill="x", padx=20, pady=(16, 4))

    def _build_instructions(self) -> None:
        ctk.CTkLabel(
            self,
            text=(
                "How to use Zadig:\n"
                "  1. Enable Options → List All Devices\n"
                "  2. Select your Bluetooth dongle from the list\n"
                "  3. Set driver to »WinUSB«\n"
                "  4. Click »Install Driver«"
            ),
            wraplength=480, justify="left", anchor="w",
            font=ctk.CTkFont(size=12), text_color="#9CA3AF",
        ).pack(fill="x", padx=20, pady=(0, 8))

        if not _WINUSB_AVAILABLE:
            ctk.CTkLabel(
                self, text="Windows only.", text_color="#9CA3AF", anchor="w",
            ).pack(fill="x", padx=20)

    def _build_device_list(self) -> None:
        ctk.CTkLabel(
            self, text="Detected BT dongles without WinUSB:", anchor="w",
            font=ctk.CTkFont(size=12),
        ).pack(fill="x", padx=20, pady=(4, 4))

        self._device_frame = ctk.CTkScrollableFrame(self, height=80, corner_radius=8)
        self._device_frame.pack(fill="x", padx=16, pady=(0, 4))
        ctk.CTkLabel(
            self._device_frame,
            text='Click "Scan" to search for devices…',
            text_color="#9CA3AF",
        ).pack(pady=6)

        # Status line below the device list (updated by scan / Zadig launch)
        self._status_label = ctk.CTkLabel(
            self, text="", anchor="w",
            font=ctk.CTkFont(size=11), text_color="#9CA3AF", wraplength=480,
        )
        self._status_label.pack(fill="x", padx=20, pady=(0, 4))

    def _build_action_buttons(self) -> None:
        self._zadig_btn = ctk.CTkButton(
            self,
            text="Download & launch Zadig",
            height=40, fg_color="#1D4ED8", hover_color="#1E40AF",
            state="normal" if _WINUSB_AVAILABLE else "disabled",
            command=self._run_zadig,
        )
        self._zadig_btn.pack(fill="x", padx=20, pady=(4, 4))

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(4, 20))

        self._scan_btn = ctk.CTkButton(
            row, text="Scan dongles",
            fg_color="#374151", hover_color="#4B5563",
            state="normal" if _WINUSB_AVAILABLE else "disabled",
            command=self._scan,
        )
        self._scan_btn.pack(side="left", expand=True, padx=(0, 4))

        ctk.CTkButton(
            row, text="Close",
            fg_color="#6B7280", hover_color="#4B5563",
            command=self._close,
        ).pack(side="left", expand=True, padx=(4, 0))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _close(self) -> None:
        self.destroy()
        if self._on_close_cb:
            self._on_close_cb()

    def _scan(self) -> None:
        """Starts a background thread to enumerate dongles without WinUSB."""
        self._scan_btn.configure(state="disabled", text="…")
        self._status_label.configure(text="Scanning…", text_color="#9CA3AF")
        threading.Thread(target=self._do_scan, daemon=True).start()

    def _do_scan(self) -> None:
        devs = list_native_bt_devices()
        try:
            self.after(0, self._on_scan_done, devs)
        except Exception:
            pass  # Dialog may have been closed while scanning

    def _on_scan_done(self, devices: list) -> None:
        self._scan_btn.configure(state="normal", text="Scan")
        # Clear previous results
        for widget in self._device_frame.winfo_children():
            widget.destroy()

        if not devices:
            self._status_label.configure(
                text="No dongle without WinUSB found.\n"
                     "Is the dongle connected? WinUSB may already be installed.",
                text_color="#F59E0B",
            )
            ctk.CTkLabel(
                self._device_frame, text="No devices found", text_color="#9CA3AF",
            ).pack(pady=6)
            return

        for dev in devices:
            ctk.CTkLabel(
                self._device_frame, text=f"  {dev}", anchor="w", text_color="#D1D5DB",
            ).pack(anchor="w", padx=8, pady=2)

        self._status_label.configure(
            text=f"{len(devices)} device(s) without WinUSB – select in Zadig and install driver.",
            text_color="#9CA3AF",
        )

    def _run_zadig(self) -> None:
        """Disables buttons and kicks off the async Zadig download."""
        self._zadig_btn.configure(state="disabled")
        self._scan_btn.configure(state="disabled")
        self._status_label.configure(text="Connecting to GitHub…", text_color="#F59E0B")
        download_and_run_zadig(
            on_status=lambda msg: self.after(0, self._on_zadig_status, msg),
            on_done=lambda ok, msg: self.after(0, self._on_zadig_done, ok, msg),
        )

    def _on_zadig_status(self, msg: str) -> None:
        self._status_label.configure(text=msg, text_color="#F59E0B")

    def _on_zadig_done(self, ok: bool, msg: str) -> None:
        """Re-enables buttons and shows the final status in green or red."""
        state = "normal" if _WINUSB_AVAILABLE else "disabled"
        self._zadig_btn.configure(state=state)
        self._scan_btn.configure(state=state)
        self._status_label.configure(
            text=msg, text_color="#10B981" if ok else "#EF4444"
        )


# ---------------------------------------------------------------------------
# Pairing Request Dialog
# ---------------------------------------------------------------------------

class PairingDialog(ctk.CTkToplevel):
    """
    Modal dialog shown when an unknown device requests to pair.

    Calls resolve(approved, remember) exactly once:
      - approved: True = allow, False = deny
      - remember: True = persist bonding key to disk
    Auto-denies after TIMEOUT seconds if the user does not respond.
    """

    TIMEOUT = 30

    def __init__(self, parent, name: str, address: str, resolve):
        super().__init__(parent)
        self._resolve = resolve
        self._answered = False

        self.title("Pairing Request")
        self.geometry("380x290")
        self.resizable(False, False)
        self.grab_set()
        self.lift()
        self.focus_force()
        self.protocol("WM_DELETE_WINDOW", self._deny)

        ctk.CTkLabel(
            self, text="Pairing Request",
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(pady=(22, 6))

        ctk.CTkLabel(self, text=name, font=ctk.CTkFont(size=13)).pack()
        ctk.CTkLabel(
            self, text=address,
            font=ctk.CTkFont(size=11), text_color="#9CA3AF",
        ).pack(pady=(2, 18))

        self._remember_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            self, text="Remember this device",
            variable=self._remember_var,
        ).pack(pady=(0, 12))

        self._countdown_var = ctk.StringVar()
        ctk.CTkLabel(
            self, textvariable=self._countdown_var,
            font=ctk.CTkFont(size=11), text_color="#9CA3AF",
        ).pack(pady=(0, 16))

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(fill="x", padx=24, pady=(0, 16))
        ctk.CTkButton(
            btn_row, text="Deny",
            fg_color="#EF4444", hover_color="#DC2626",
            command=self._deny,
        ).pack(side="left", expand=True, padx=(0, 8))
        ctk.CTkButton(
            btn_row, text="Allow",
            command=self._allow,
        ).pack(side="left", expand=True)

        self._remaining = self.TIMEOUT
        self._tick()

    def _tick(self) -> None:
        if self._answered:
            return
        if self._remaining <= 0:
            self._deny()
            return
        self._countdown_var.set(f"Auto-deny in {self._remaining}s")
        self._remaining -= 1
        self.after(1000, self._tick)

    def _allow(self) -> None:
        if self._answered:
            return
        self._answered = True
        remember = bool(self._remember_var.get())
        self.destroy()
        self._resolve(True, remember)

    def _deny(self) -> None:
        if self._answered:
            return
        self._answered = True
        self.destroy()
        self._resolve(False, False)


# ---------------------------------------------------------------------------
# Main Application Window
# ---------------------------------------------------------------------------

class App(ctk.CTk):
    """
    Root window.  Owns the SinkBackend lifecycle and all UI state.

    When start_minimized=True (set by the --minimized CLI flag used by
    Windows autostart), the window is hidden and a tray icon shown instead.
    The only way to quit is via the tray icon's Quit menu item.
    """

    def __init__(self, start_minimized: bool = False):
        super().__init__()

        settings.load()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("BT-AudioSink")
        self.geometry("480x720")
        self.resizable(False, False)

        self._backend: Optional[SinkBackend] = None
        self._running = False
        self._current_state = SinkState.IDLE
        self._level_smooth = 0.0
        self._available_dongles: list[tuple[int, str]] = []
        self._tray_icon: Optional[object] = None
        self._in_tray = False  # Guards against recursive tray transitions
        self._connected_devices: dict[str, str] = {}  # name -> address
        self._autostart_bt = start_minimized  # Start BT after dongle scan on autostart
        self._pairing_switch: Optional[ctk.CTkSwitch] = None

        self._build_ui()
        self._log("Ready – scanning USB dongles…")

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Unmap>", self._on_unmap)  # Minimize button → tray
        self.bind("<Map>", self._on_map)       # Window restored → reset flag
        self.after(400, self._scan_dongles)

        if start_minimized and _TRAY_AVAILABLE:
            self.after(100, self._minimize_to_tray)

    # ------------------------------------------------------------------
    # UI construction (split into per-section helpers for readability)
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        """Assembles the main window layout from individual section builders."""
        self._build_header()
        self._build_dongle_section()
        self._build_device_section()
        self._build_level_section()
        self._build_volume_section()
        self._build_action_buttons()
        self._build_pairing_row()
        self._build_log_section()

    def _build_header(self) -> None:
        """Card showing the BT icon, device name, and connection status dot."""
        card = ctk.CTkFrame(self, corner_radius=12)
        card.pack(fill="x", padx=16, pady=(16, 8))

        bt_img = self._make_bt_icon(40)
        icon_lbl = ctk.CTkLabel(card, image=bt_img, text="")
        icon_lbl.image = bt_img  # type: ignore[attr-defined]  – keep reference
        icon_lbl.pack(side="left", padx=(12, 0), pady=12)

        info = ctk.CTkFrame(card, fg_color="transparent")
        info.pack(side="left", fill="both", expand=True, padx=12, pady=12)

        self._title_label = ctk.CTkLabel(
            info, text=settings.device_name,
            font=ctk.CTkFont(size=18, weight="bold"), anchor="w",
        )
        self._title_label.pack(fill="x")

        self._status_label = ctk.CTkLabel(
            info, text="● Ready",
            font=ctk.CTkFont(size=13),
            text_color=STATE_COLORS[SinkState.IDLE], anchor="w",
        )
        self._status_label.pack(fill="x")

    def _build_dongle_section(self) -> None:
        """Dongle dropdown + Scan button + Install WinUSB link."""
        header_row = ctk.CTkFrame(self, fg_color="transparent")
        header_row.pack(fill="x", padx=20, pady=(10, 2))

        ctk.CTkLabel(
            header_row, text="USB Dongle",
            font=ctk.CTkFont(size=12), text_color="#9CA3AF",
        ).pack(side="left")

        ctk.CTkButton(
            header_row, text="Install WinUSB…",
            width=160, height=24, font=ctk.CTkFont(size=11),
            fg_color="#374151", hover_color="#4B5563",
            command=self._open_winusb_dialog,
        ).pack(side="right")

        selector_row = ctk.CTkFrame(self, fg_color="transparent")
        selector_row.pack(fill="x", padx=16, pady=(0, 2))

        self._dongle_var = ctk.StringVar(value="Scanning…")
        self._dongle_menu = ctk.CTkOptionMenu(
            selector_row, variable=self._dongle_var, values=["Scanning…"],
            state="disabled", command=self._on_dongle_selected,
        )
        self._dongle_menu.pack(side="left", fill="x", expand=True)

        self._scan_dongle_btn = ctk.CTkButton(
            selector_row, text="Scan", width=64,
            fg_color="#374151", hover_color="#4B5563",
            command=self._scan_dongles,
        )
        self._scan_dongle_btn.pack(side="left", padx=(8, 0))

        self._dongle_status = ctk.CTkLabel(
            self, text="", anchor="w",
            font=ctk.CTkFont(size=11), text_color="#9CA3AF",
        )
        self._dongle_status.pack(fill="x", padx=20, pady=(0, 4))

    def _build_device_section(self) -> None:
        """Single-line frame showing the currently connected BT source device."""
        ctk.CTkLabel(
            self, text="Connected Device",
            font=ctk.CTkFont(size=12), text_color="#9CA3AF", anchor="w",
        ).pack(fill="x", padx=20, pady=(6, 2))

        frame = ctk.CTkFrame(self, corner_radius=8, height=44)
        frame.pack(fill="x", padx=16, pady=(0, 8))
        frame.pack_propagate(False)

        self._device_label = ctk.CTkLabel(
            frame, text="—", font=ctk.CTkFont(size=14), anchor="w",
        )
        self._device_label.pack(fill="both", expand=True, padx=12)

    def _build_level_section(self) -> None:
        """Real-time VU meter progress bar."""
        ctk.CTkLabel(
            self, text="Audio Level",
            font=ctk.CTkFont(size=12), text_color="#9CA3AF", anchor="w",
        ).pack(fill="x", padx=20, pady=(4, 2))

        self._level_bar = ctk.CTkProgressBar(self, height=16, corner_radius=6)
        self._level_bar.pack(fill="x", padx=16, pady=(0, 8))
        self._level_bar.set(0)

    def _build_volume_section(self) -> None:
        """Volume slider (0–200%) with live percentage label on the right."""
        header_row = ctk.CTkFrame(self, fg_color="transparent")
        header_row.pack(fill="x", padx=20, pady=(4, 0))

        ctk.CTkLabel(
            header_row, text="Volume",
            font=ctk.CTkFont(size=12), text_color="#9CA3AF",
        ).pack(side="left")

        self._vol_pct_label = ctk.CTkLabel(
            header_row, text=f"{int(settings.volume * 100)}%",
            font=ctk.CTkFont(size=12), text_color="#9CA3AF",
        )
        self._vol_pct_label.pack(side="right")

        self._vol_var = ctk.DoubleVar(value=settings.volume * 100)
        ctk.CTkSlider(
            self, from_=0, to=200, number_of_steps=200,
            variable=self._vol_var, command=self._on_volume_change,
        ).pack(fill="x", padx=16, pady=(2, 8))

    def _build_action_buttons(self) -> None:
        """Start/Stop toggle and Settings button side by side."""
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=6)

        self._start_btn = ctk.CTkButton(
            row, text="▶  Start",
            font=ctk.CTkFont(size=14, weight="bold"), height=42,
            state="disabled",  # Enabled only after a successful dongle scan
            command=self._toggle_backend,
        )
        self._start_btn.pack(side="left", expand=True, padx=(0, 6))

        ctk.CTkButton(
            row, text="⚙  Settings",
            font=ctk.CTkFont(size=14), height=42,
            fg_color="#374151", hover_color="#4B5563",
            command=self._open_settings,
        ).pack(side="left", expand=True, padx=(6, 0))

    def _build_pairing_row(self) -> None:
        """Toggle to allow or block pairing requests from unknown devices."""
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(0, 4))

        ctk.CTkLabel(
            row, text="Allow new pairings:",
            font=ctk.CTkFont(size=12), text_color="#9CA3AF",
        ).pack(side="left")

        self._pairing_switch = ctk.CTkSwitch(
            row, text="", width=46,
            command=self._on_pairing_toggle,
        )
        self._pairing_switch.select()  # Default: new pairings allowed
        self._pairing_switch.pack(side="left", padx=10)

    def _build_log_section(self) -> None:
        """Scrolling monospace log output at the bottom of the window."""
        ctk.CTkLabel(
            self, text="Log",
            font=ctk.CTkFont(size=12), text_color="#9CA3AF", anchor="w",
        ).pack(fill="x", padx=20, pady=(8, 2))

        self._log_box = ctk.CTkTextbox(
            self, height=180, corner_radius=8,
            font=ctk.CTkFont(family="Consolas", size=11),
            state="disabled",
        )
        self._log_box.pack(fill="both", expand=True, padx=16, pady=(0, 16))

    # ------------------------------------------------------------------
    # Icon helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_bt_icon(size: int) -> ctk.CTkImage:
        """Draws a minimal Bluetooth symbol as a CTkImage (no .ico file needed)."""
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        cx = size // 2
        # Vertical spine
        d.rectangle([cx - 3, 4, cx + 3, size - 4], fill="#3B82F6")
        # Right-pointing serifs
        d.polygon(
            [(cx, size // 4), (cx + size // 4, size // 2), (cx, size * 3 // 4)],
            fill="#3B82F6",
        )
        d.polygon(
            [(cx, size // 4 + 4), (cx + size // 4 - 2, size // 2), (cx, size * 3 // 4 - 4)],
            outline="#1E40AF", width=1,
        )
        return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))

    @staticmethod
    def _make_tray_image(size: int = 64) -> Image.Image:
        """
        Draws a filled blue circle with a white BT symbol for the system tray.
        Returns a plain PIL Image (pystray does not accept CTkImage).
        """
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse([0, 0, size - 1, size - 1], fill="#3B82F6")
        cx = size // 2
        d.rectangle([cx - 3, size // 8, cx + 3, size - size // 8], fill="white")
        d.polygon(
            [(cx, size // 4), (cx + size // 4, size // 2), (cx, size * 3 // 4)],
            fill="white",
        )
        d.polygon(
            [(cx, size * 3 // 4), (cx + size // 4, size // 2), (cx, size // 4)],
            outline="white", width=2,
        )
        return img

    # ------------------------------------------------------------------
    # USB dongle scan
    # ------------------------------------------------------------------

    def _scan_dongles(self) -> None:
        """Kicks off the background scan and disables controls during the wait."""
        self._scan_dongle_btn.configure(state="disabled", text="…")
        self._dongle_status.configure(text="Scanning…", text_color="#9CA3AF")
        self._start_btn.configure(state="disabled")
        threading.Thread(target=self._do_scan_dongles, daemon=True).start()

    def _do_scan_dongles(self) -> None:
        """Runs scan_bt_dongles() on a worker thread; posts result to mainloop."""
        dongles = scan_bt_dongles()
        self.after(0, self._on_dongles_scanned, dongles)

    def _on_dongles_scanned(self, dongles: list[tuple[int, str]]) -> None:
        """Updates the dongle dropdown and enables/disables Start based on results."""
        self._scan_dongle_btn.configure(state="normal", text="Scan")
        self._available_dongles = dongles

        if not dongles:
            self._dongle_var.set("No WinUSB dongle found")
            self._dongle_menu.configure(values=["No WinUSB dongle found"], state="disabled")
            self._dongle_status.configure(
                text="⚠ No WinUSB dongle found – install WinUSB driver first",
                text_color="#EF4444",
            )
            self._start_btn.configure(state="disabled")
            self._log("No BT dongle with WinUSB found. Install driver then scan again.")
            return

        labels = [label for _, label in dongles]
        self._dongle_var.set(labels[0])
        self._dongle_menu.configure(values=labels, state="normal")
        settings.transport = f"usb:{dongles[0][0]}"
        self._dongle_status.configure(
            text=f"{len(dongles)} dongle(s) found – ready", text_color="#10B981"
        )
        self._start_btn.configure(state="normal")
        self._log(f"Dongle found: {labels[0]}")

        if self._autostart_bt:
            self._autostart_bt = False
            self._start_backend()

    def _on_dongle_selected(self, label: str) -> None:
        """Updates settings.transport when the user picks a different dongle."""
        for idx, lbl in self._available_dongles:
            if lbl == label:
                settings.transport = f"usb:{idx}"
                break

    # ------------------------------------------------------------------
    # Volume
    # ------------------------------------------------------------------

    def _on_volume_change(self, value: float) -> None:
        """Called by the volume slider; updates label and live pipeline volume."""
        pct = int(value)
        self._vol_pct_label.configure(text=f"{pct}%")
        vol = value / 100.0
        settings.volume = vol
        if self._backend:
            self._backend.set_volume(vol)

    # ------------------------------------------------------------------
    # Backend lifecycle
    # ------------------------------------------------------------------

    def _toggle_backend(self) -> None:
        """Toggles between starting and stopping the BT backend."""
        if self._running:
            self._stop_backend()
        else:
            self._start_backend()

    def _start_backend(self) -> None:
        """Creates and starts a SinkBackend with the current settings."""
        # Resolve transport from the currently selected dropdown item
        selected = self._dongle_var.get()
        for idx, label in self._available_dongles:
            if label == selected:
                settings.transport = f"usb:{idx}"
                break

        self._running = True
        self._start_btn.configure(text="■  Stop", fg_color="#EF4444", hover_color="#DC2626")
        self._scan_dongle_btn.configure(state="disabled")

        self._backend = SinkBackend(
            device_name=settings.device_name,
            bt_address=settings.bt_address,
            transport=settings.transport,
            latency_ms=settings.latency_ms,
            max_bitpool=settings.max_bitpool,
            volume=settings.volume,
            audio_device_index=settings.audio_device_index,
            ffmpeg_exe=_get_ffmpeg(),
            debug=settings.debug_mode,
            keystore_path=_keys_file(),
            allowed_macs_path=_allowed_macs_file(),
            # Route all callbacks through after() to stay on the mainloop thread
            on_state_change=lambda s: self.after(0, self._on_state_change, s),
            on_device_connected=lambda n, a: self.after(0, self._on_device_connected, n, a),
            on_device_disconnected=lambda n: self.after(0, self._on_device_disconnected, n),
            on_audio_level=lambda l: self.after(0, self._on_audio_level, l),
            on_log=lambda m: self.after(0, self._log, m),
            on_pairing_request=lambda n, a, r: self.after(0, self._on_pairing_request, n, a, r),
        )
        self._backend.start()
        self._title_label.configure(text=settings.device_name)

    def _stop_backend(self) -> None:
        """Signals the backend to stop and resets UI to the idle state."""
        self._running = False
        self._start_btn.configure(
            text="▶  Start",
            fg_color=["#3B82F6", "#1D4ED8"],
            hover_color=["#2563EB", "#1E40AF"],
        )
        self._scan_dongle_btn.configure(state="normal")
        if self._backend:
            self._log("BT stack stopped.")
            # Stop on a daemon thread so the UI stays responsive during cleanup
            threading.Thread(target=self._backend.stop, daemon=True).start()
            self._backend = None
        self._connected_devices.clear()
        self._device_label.configure(text="—")
        self._level_bar.set(0)
        self._on_state_change(SinkState.STOPPED)

    # ------------------------------------------------------------------
    # Backend callbacks (always called from mainloop thread via after())
    # ------------------------------------------------------------------

    def _on_state_change(self, state: SinkState) -> None:
        self._current_state = state
        self._status_label.configure(
            text=f"● {STATE_LABELS[state]}", text_color=STATE_COLORS[state]
        )

    def _on_device_connected(self, name: str, address: str) -> None:
        self._connected_devices[name] = address
        self._update_device_label()
        # Automatically lock out new pairings once any device is connected
        if self._pairing_switch and self._pairing_switch.get():
            self._pairing_switch.deselect()
            if self._backend:
                self._backend.set_pairing_mode(False)
            self._log("New pairings: blocked (auto)")

    def _on_device_disconnected(self, name: str) -> None:
        self._connected_devices.pop(name, None)
        self._update_device_label()
        if not self._connected_devices:
            self._level_bar.set(0)

    def _update_device_label(self) -> None:
        if not self._connected_devices:
            self._device_label.configure(text="—")
        else:
            text = "\n".join(f"{n}  ({a})" for n, a in self._connected_devices.items())
            self._device_label.configure(text=text)

    def _on_audio_level(self, level: float) -> None:
        """
        Updates the VU meter with exponential smoothing so it doesn't flicker.
        The × 4 pre-scale maps typical RMS values (≈0.25 peak) to full bar width.
        """
        self._level_smooth = 0.7 * self._level_smooth + 0.3 * min(level * 4, 1.0)
        self._level_bar.set(self._level_smooth)

    def _on_pairing_request(self, name: str, address: str, resolve) -> None:
        """Shows a confirmation dialog when an unknown device wants to pair."""
        self._log(f"Pairing request from: {name} ({address})")
        PairingDialog(self, name, address, resolve)

    def _on_pairing_toggle(self) -> None:
        """Relays the pairing mode switch state to the backend."""
        allowed = bool(self._pairing_switch.get())
        if self._backend:
            self._backend.set_pairing_mode(allowed)
        self._log(f"New pairings: {'allowed' if allowed else 'blocked'}")

    def _log(self, msg: str) -> None:
        """Appends a timestamped line to the log textbox."""
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_box.configure(state="normal")
        self._log_box.insert("end", f"{ts}  {msg}\n")
        self._log_box.see("end")
        self._log_box.configure(state="disabled")

    # ------------------------------------------------------------------
    # Dialogs
    # ------------------------------------------------------------------

    def _open_settings(self) -> None:
        """Opens the settings dialog (blocked while backend is running)."""
        if self._running:
            return
        SettingsDialog(self)

    def _open_winusb_dialog(self) -> None:
        """Opens the WinUSB install dialog; re-scans dongles when it closes."""
        WinUSBDialog(self, on_close=self._scan_dongles)

    # ------------------------------------------------------------------
    # System tray
    # ------------------------------------------------------------------

    def _minimize_to_tray(self) -> None:
        """
        Hides the main window and shows a pystray icon in the notification area.
        The _in_tray flag prevents re-entrancy when withdraw() fires <Unmap>.
        Falls back to iconify() when pystray is not installed.
        """
        if self._in_tray:
            return
        self._in_tray = True

        if not _TRAY_AVAILABLE:
            self.iconify()
            return

        self.withdraw()

        # Create the tray icon only once; subsequent minimize calls reuse it
        if self._tray_icon is None:
            self._tray_icon = _pystray.Icon(
                "BT-AudioSink",
                self._make_tray_image(64),
                "BT-AudioSink",
                self._build_tray_menu(),
            )
            self._tray_icon.run_detached()  # Non-blocking; icon runs on its own thread

    def _build_tray_menu(self) -> "_pystray.Menu":
        """Constructs the right-click context menu for the tray icon."""
        return _pystray.Menu(
            _pystray.MenuItem("Show Window", self._tray_show, default=True),
            _pystray.MenuItem(
                "Start BT",
                self._tray_start,
                enabled=lambda _: not self._running,
            ),
            _pystray.MenuItem(
                "Stop BT",
                self._tray_stop,
                enabled=lambda _: self._running,
            ),
            _pystray.Menu.SEPARATOR,
            _pystray.MenuItem("Quit", self._tray_quit),
        )

    def _on_unmap(self, event) -> None:
        """Intercepts the window minimize button to go to tray instead of taskbar."""
        if event.widget is self and not self._in_tray:
            self._minimize_to_tray()

    def _on_map(self, event) -> None:
        """Resets the in-tray flag when the window becomes visible again."""
        if event.widget is self:
            self._in_tray = False

    def _tray_show(self, icon=None, item=None) -> None:
        self.after(0, self._do_show_window)

    def _do_show_window(self) -> None:
        """Restores the main window from tray."""
        self._in_tray = False
        self.deiconify()
        self.lift()
        self.focus_force()

    def _tray_start(self, icon=None, item=None) -> None:
        self.after(0, self._start_backend)

    def _tray_stop(self, icon=None, item=None) -> None:
        self.after(0, self._stop_backend)

    def _tray_quit(self, icon=None, item=None) -> None:
        self.after(0, self._do_quit)

    def _do_quit(self) -> None:
        """Full application exit: stop backend, destroy tray icon, close window."""
        self._cleanup()
        self.destroy()

    def _cleanup(self) -> None:
        """Stops the backend and tray icon; safe to call from any context."""
        if self._backend:
            self._backend.stop()
            self._backend = None
        if self._tray_icon is not None:
            try:
                self._tray_icon.stop()  # type: ignore[union-attr]
            except Exception:
                pass
            self._tray_icon = None

    # ------------------------------------------------------------------
    # Window close → tray  (X button and OS close signal)
    # ------------------------------------------------------------------

    def _on_close(self) -> None:
        """Closes the app when the window is visible; quits cleanly."""
        self._tray_quit()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # The --minimized flag is written into the Windows autostart registry entry
    # so the app starts hidden when the user logs in.
    start_minimized = "--minimized" in sys.argv
    app = App(start_minimized=start_minimized)
    app.mainloop()
