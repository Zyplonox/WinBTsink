"""
winusb_installer.py – WinUSB driver helpers
============================================
Windows 10/11 enforces Kernel-Mode Code Signing (KMCS) for all driver
packages, including pure WinUSB/inbox references.  This cannot be bypassed
without an EV-certificate or bcdedit test-signing mode.

This module:
  - Lists BT USB dongles that still use the native Windows HCI driver
  - Downloads Zadig automatically from GitHub
  - Launches Zadig so the user can install the WinUSB driver
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Device data class
# ---------------------------------------------------------------------------

@dataclass
class BTDevice:
    """Represents a Bluetooth USB dongle discovered via PnP / libusb."""
    name: str
    vid: int
    pid: int
    instance_id: str

    def __str__(self) -> str:
        return f"{self.name}  [VID:{self.vid:04X} PID:{self.pid:04X}]"


# ---------------------------------------------------------------------------
# USB class helpers  (shared between detection and scan functions)
# ---------------------------------------------------------------------------

#: USB class triple that identifies a Bluetooth HCI transport endpoint.
_BT_HCI = (0xE0, 0x01, 0x01)


def _is_bt_hci_class(cls: int, sub: int, proto: int) -> bool:
    """Returns True when (class, subclass, protocol) match Bluetooth HCI."""
    return (cls, sub, proto) == _BT_HCI


def _device_has_bt_hci_interface(dev) -> bool:
    """
    Walks all interface alternate-settings of a USB device looking for a
    Bluetooth HCI class descriptor.

    This is needed for *composite* devices (device class 0x00) where Bluetooth
    is one function among many.  A simple BT-only dongle will have the class
    at device level and won't reach this function.
    """
    try:
        for cfg in dev:
            for intf in cfg:
                for setting in intf:
                    if _is_bt_hci_class(
                        setting.getClass(),
                        setting.getSubClass(),
                        setting.getProtocol(),
                    ):
                        return True
    except Exception:
        # Ignore devices whose descriptors cannot be read (access errors, etc.)
        pass
    return False


def _is_bt_hci_device(dev) -> bool:
    """
    Returns True if the USB device is a Bluetooth HCI adapter.

    Detection strategy:
      1. Check device-level class triple (covers simple single-function dongles).
      2. If the device class is 0x00 ("per-interface"), check every interface
         descriptor (covers composite USB devices).
    """
    # Fast path: device class directly declares BT HCI
    if _is_bt_hci_class(
        dev.getDeviceClass(),
        dev.getDeviceSubClass(),
        dev.getDeviceProtocol(),
    ):
        return True

    # Device class 0x00 means class is defined per-interface
    if dev.getDeviceClass() == 0x00:
        return _device_has_bt_hci_interface(dev)

    return False


def _winusb_active(dev) -> bool:
    """
    Returns True when libusb can open the device, which only succeeds when
    WinUSB (or another libusb-compatible driver) is the active Windows driver.

    When the native Windows HCI driver is bound, open() raises a USBError.
    We use this to distinguish "needs WinUSB" from "WinUSB already installed".
    """
    try:
        handle = dev.open()
        handle.close()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Friendly-name resolution via PowerShell
# ---------------------------------------------------------------------------

def _fallback_devices(candidates: list[tuple[int, int]]) -> list[BTDevice]:
    """
    Returns a list of generic BTDevice entries when friendly-name resolution
    is unavailable (PowerShell not found, timeout, parse error, etc.).
    """
    return [
        BTDevice(name="Bluetooth USB Dongle", vid=v, pid=p, instance_id="")
        for v, p in candidates
    ]


def _build_pnp_filter(candidates: list[tuple[int, int]]) -> str:
    """
    Builds the PowerShell Where-Object filter clause that matches any of
    the given (VID, PID) pairs against the device's HardwareId property.
    """
    return " -or ".join(
        f"($_.HardwareId -like '*VID_{vid:04X}&PID_{pid:04X}*')"
        for vid, pid in candidates
    )


def _query_friendly_names(candidates: list[tuple[int, int]]) -> list[BTDevice]:
    """
    Queries Windows PnP (Get-PnpDevice via PowerShell) to resolve VID/PID
    pairs into human-readable device names.

    Searches across *all* device classes – not just Bluetooth – because a
    device with the usbser driver appears in the "Ports" class, not "Bluetooth".

    Returns a list of BTDevice objects, or calls _fallback_devices() on any
    error (PowerShell unavailable, empty output, JSON parse failure, …).
    """
    vid_pid_filter = _build_pnp_filter(candidates)

    # PowerShell script: find USB devices matching the VID/PID list,
    # extract VID/PID from HardwareId, and emit compact JSON.
    ps_script = f"""
$devs = Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue |
    Where-Object {{ $_.InstanceId -match '^USB\\\\' -and ({vid_pid_filter}) }}
$out = foreach ($d in $devs) {{
    $hw = ($d.HardwareId | Select-Object -First 1)
    if ($hw -match 'VID_([0-9A-Fa-f]{{4}}).*PID_([0-9A-Fa-f]{{4}})') {{
        @{{
            name = $d.FriendlyName
            vid  = [int]('0x' + $Matches[1])
            pid  = [int]('0x' + $Matches[2])
            id   = $d.InstanceId
        }}
    }}
}}
$out | ConvertTo-Json -Compress
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        raw = result.stdout.strip()
        if not raw:
            # PowerShell ran fine but found no matching devices
            return _fallback_devices(candidates)

        data = json.loads(raw)
        # ConvertTo-Json wraps a single object in a dict, not a list
        if isinstance(data, dict):
            data = [data]

        return [
            BTDevice(
                name=d.get("name") or "Bluetooth USB Dongle",
                vid=int(d["vid"]),
                pid=int(d["pid"]),
                instance_id=d.get("id", ""),
            )
            for d in data
        ]
    except Exception:
        return _fallback_devices(candidates)


# ---------------------------------------------------------------------------
# Public API: list native BT devices (no WinUSB yet)
# ---------------------------------------------------------------------------

def _collect_native_bt_candidates() -> list[tuple[int, int]]:
    """
    Uses libusb to enumerate USB devices and collects (VID, PID) pairs for
    BT HCI devices that do *not* yet have WinUSB as their driver.

    A device is considered "native" (no WinUSB) if open() fails: the Windows
    HCI driver holds the device exclusively, blocking libusb access.

    Returns an empty list on non-Windows or when libusb/usb1 is unavailable.
    """
    if sys.platform != "win32":
        return []

    try:
        from bumble.transport.usb import load_libusb
        import usb1
        load_libusb()
    except ImportError:
        return []

    candidates: list[tuple[int, int]] = []
    try:
        context = usb1.USBContext()
        context.open()
        for dev in context.getDeviceIterator(skip_on_error=True):
            if not _is_bt_hci_device(dev):
                continue
            # open() succeeds only if WinUSB is active; failure means native driver
            if _winusb_active(dev):
                continue
            vid_pid = (dev.getVendorID(), dev.getProductID())
            if vid_pid not in candidates:
                candidates.append(vid_pid)
        context.close()
    except Exception:
        return []

    return candidates


def _list_native_bt_devices() -> list[BTDevice]:
    """
    Returns BTDevice entries for BT USB dongles that still use the native
    Windows HCI driver (i.e., WinUSB has not been installed yet).
    """
    candidates = _collect_native_bt_candidates()
    if not candidates:
        return []
    return _query_friendly_names(candidates)


def list_native_bt_devices() -> list[BTDevice]:
    """Public entry point: BT dongles with native Windows driver (WinUSB not yet installed)."""
    return _list_native_bt_devices()


# ---------------------------------------------------------------------------
# Zadig download and launch
# ---------------------------------------------------------------------------

def _find_zadig_asset(release_data: dict) -> Optional[dict]:
    """
    Locates the Zadig executable asset inside a GitHub release JSON object.
    Returns the asset dict (containing browser_download_url and name) or None.
    """
    return next(
        (a for a in release_data.get("assets", []) if "zadig" in a["name"].lower()),
        None,
    )


def _resolve_zadig_dest() -> str:
    """
    Returns the local path where Zadig will be cached.
    When frozen (PyInstaller .exe), place it next to the executable so it
    survives across runs.  Otherwise use the system temp directory.
    """
    if getattr(sys, "frozen", False):
        dest_dir = os.path.dirname(sys.executable)
    else:
        dest_dir = tempfile.gettempdir()
    return os.path.join(dest_dir, "zadig.exe")


def _download_zadig(dest: str, on_status: Callable[[str], None]) -> bool:
    """
    Fetches the latest Zadig release from GitHub and saves it to *dest*.

    Calls on_status() with progress messages.
    Returns True on success, False on any network or file-system error.
    The caller is expected to emit the failure reason via on_status itself.
    """
    # GitHub REST API – latest release metadata
    try:
        on_status("Fetching release info from GitHub…")
        api_url = "https://api.github.com/repos/pbatard/libwdi/releases/latest"
        req = urllib.request.Request(api_url, headers={"User-Agent": "BT-AudioSink"})
        with urllib.request.urlopen(req, timeout=15) as r:
            release_data = json.loads(r.read())
    except Exception as exc:
        on_status(f"GitHub request failed: {exc}")
        return False

    asset = _find_zadig_asset(release_data)
    if not asset:
        on_status("Zadig asset not found in release.")
        return False

    try:
        url = asset["browser_download_url"]
        name = asset["name"]
        on_status(f"Downloading {name}…")
        urllib.request.urlretrieve(url, dest)
    except Exception as exc:
        on_status(f"Download failed: {exc}")
        return False

    return True


def _launch_zadig(dest: str) -> None:
    """
    Launches the Zadig executable with UAC elevation (ShellExecuteW "runas").
    Raises OSError when ShellExecuteW returns an error code ≤ 32 (Win32 API
    convention: values > 32 indicate success).
    """
    if sys.platform == "win32":
        import ctypes
        ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", dest, None, None, 1)
        if ret <= 32:
            raise OSError(f"ShellExecuteW failed (code {ret})")
    else:
        # Non-Windows: just try to execute directly (useful for dev/testing)
        subprocess.Popen([dest])


def download_and_run_zadig(
    on_status: Callable[[str], None],
    on_done: Callable[[bool, str], None],
) -> None:
    """
    Downloads the latest Zadig release from GitHub and launches it with
    administrator privileges.  Runs asynchronously in a daemon thread.

    Args:
        on_status: Called with progress strings during download.
        on_done:   Called with (success: bool, message: str) when finished
                   or on any error.  Both callbacks may be called from a
                   background thread – the caller is responsible for
                   dispatching to the GUI thread if needed.
    """
    import threading

    def _run() -> None:
        dest = _resolve_zadig_dest()

        # Re-use a previously downloaded copy to avoid redundant network traffic
        if os.path.isfile(dest):
            on_status("Zadig already cached – launching…")
        else:
            ok = _download_zadig(dest, on_status)
            if not ok:
                # on_status already reported the error; signal failure to caller
                on_done(False, "Download failed – see status above.")
                return

        try:
            _launch_zadig(dest)
            on_done(True, "Zadig launched – please confirm the admin prompt.")
        except Exception as exc:
            on_done(False, f"Could not launch Zadig: {exc}")

    threading.Thread(target=_run, daemon=True, name="zadig-download").start()
