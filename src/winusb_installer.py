"""
winusb_installer.py – WinUSB driver helpers
============================================
btstack_sink.exe needs direct USB access to the dongle, which on Windows
means the WinUSB driver has to replace the inbox Bluetooth (BTHUSB) driver.
Windows only accepts signed driver packages, so we do not install anything
ourselves; we download Zadig (from the libwdi GitHub releases) and launch it
elevated so the user can swap the driver.

This module:
  - lists BT dongles that still use a driver other than WinUSB
  - downloads Zadig into the app's data directory (cached across runs)
  - launches Zadig with UAC elevation
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from collections.abc import Callable

from usb_devices import UsbDevice, list_bluetooth_dongles

_RELEASES_API = "https://api.github.com/repos/pbatard/libwdi/releases/latest"
_USER_AGENT = "BT-AudioSink"
_ZADIG_MIN_BYTES = 1_000_000      # zadig.exe is ~5 MB; anything smaller is a broken download
_NET_TIMEOUT_S = 30

#: Hosts a Zadig download may come from. The download URL is taken from
#: the libwdi release JSON, so it is remote input, and the file it points
#: at is run with elevation afterwards.
_ALLOWED_HOSTS = ("github.com", "api.github.com")
_ALLOWED_HOST_SUFFIX = ".githubusercontent.com"


def list_native_bt_devices() -> list[UsbDevice]:
    """Attached Bluetooth dongles whose driver is not WinUSB yet."""
    return [d for d in list_bluetooth_dongles() if not d.uses_winusb]


# ---------------------------------------------------------------------------
# Zadig download and launch
# ---------------------------------------------------------------------------

def _check_url(url: str) -> str:
    """
    Returns *url* if it is an https URL on a GitHub host, else raises.

    Applied to every request and to every redirect: a manipulated release
    document could otherwise point at file:// (silently copying a local
    file into the cache) or at http:// on a foreign host, and the result
    is executed with UAC elevation.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        raise ValueError(f"refusing a non-https download URL: {url!r}")
    host = (parts.hostname or "").lower()
    if host not in _ALLOWED_HOSTS and not host.endswith(_ALLOWED_HOST_SUFFIX):
        raise ValueError(f"refusing a download from an unexpected host: {host!r}")
    return url


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    """Applies _check_url() to each redirect target; GitHub redirects to its CDN."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_CheckedRedirects)


def _find_zadig_asset(release_data: dict) -> dict | None:
    """Returns the zadig*.exe asset dict from a GitHub release, or None."""
    for asset in release_data.get("assets", []):
        name = asset.get("name", "").lower()
        if name.startswith("zadig") and name.endswith(".exe"):
            return asset
    return None


def _download_zadig(dest: str, on_status: Callable[[str], None]) -> str | None:
    """
    Downloads the latest Zadig release to *dest*.
    Returns None on success or an error message.  Writes to a temporary
    ".part" file first so an interrupted download never looks like a cached copy.
    """
    try:
        on_status("Fetching release info from GitHub…")
        req = urllib.request.Request(_check_url(_RELEASES_API),
                                     headers={"User-Agent": _USER_AGENT})
        with _opener.open(req, timeout=_NET_TIMEOUT_S) as r:
            release_data = json.loads(r.read())
    except Exception as exc:
        return f"GitHub request failed: {exc}"

    asset = _find_zadig_asset(release_data)
    if not asset:
        return "Zadig executable not found in the latest libwdi release."

    part = dest + ".part"
    try:
        on_status(f"Downloading {asset['name']}…")
        req = urllib.request.Request(_check_url(str(asset.get("browser_download_url", ""))),
                                     headers={"User-Agent": _USER_AGENT})
        with _opener.open(req, timeout=_NET_TIMEOUT_S) as r, open(part, "wb") as f:
            while True:
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
        if os.path.getsize(part) < _ZADIG_MIN_BYTES:
            raise OSError("downloaded file is too small to be Zadig")
        os.replace(part, dest)
    except Exception as exc:
        try:
            os.remove(part)
        except OSError:
            pass
        return f"Download failed: {exc}"

    return None


def _launch_zadig(path: str) -> None:
    """Launches Zadig with UAC elevation; raises OSError on failure."""
    if sys.platform == "win32":
        import ctypes
        # ShellExecuteW returns a value > 32 on success (Win32 convention)
        ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", path, None, None, 1)
        if ret <= 32:
            raise OSError(f"ShellExecuteW failed (code {ret})")
    else:
        subprocess.Popen([path])


def download_and_run_zadig(
    cache_dir: str,
    on_status: Callable[[str], None],
    on_done: Callable[[bool, str], None],
) -> None:
    """
    Downloads Zadig (unless a good copy is cached in *cache_dir*) and launches
    it elevated.  Runs on a daemon thread; both callbacks may be called from
    that thread, so GUI callers must marshal to their UI thread.
    """
    def run() -> None:
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except OSError as exc:
            on_done(False, f"Cannot create {cache_dir}: {exc}")
            return
        dest = os.path.join(cache_dir, "zadig.exe")

        cached = os.path.isfile(dest) and os.path.getsize(dest) >= _ZADIG_MIN_BYTES
        if cached:
            on_status("Using cached Zadig…")
        else:
            error = _download_zadig(dest, on_status)
            if error:
                on_done(False, error)
                return

        try:
            _launch_zadig(dest)
            on_done(True, "Zadig launched – please confirm the admin prompt.")
        except Exception as exc:
            on_done(False, f"Could not launch Zadig: {exc}")

    threading.Thread(target=run, daemon=True, name="zadig-download").start()
