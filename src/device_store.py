"""
device_store.py – remembered Bluetooth devices
===============================================
Persists the devices the user approved with "Remember this device":

    %APPDATA%\\BT-AudioSink\\allowed_macs.json
    {
      "devices": {
        "AA:BB:CC:DD:EE:FF": {"name": "Pixel 8", "volume": 1.0}
      },
      "forget_keys": ["11:22:33:44:55:66"]
    }

"forget_keys" lists devices whose Bluetooth bonding key still has to be
dropped inside btstack_sink.exe; the backend sends a forget_key command for
each of them the next time the engine is ready.  Older files that contain a
plain list of addresses are migrated on load.

The store is shared by the GUI (settings dialog, only while the backend is
stopped) and by the backend (under its own lock), so it does no locking.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger("bt-sink.devices")


class DeviceStore:
    def __init__(self, path: Optional[str]):
        self.path = Path(path) if path else None
        self.devices: dict[str, dict] = {}
        self.forget_keys: list[str] = []
        self.load_error: Optional[str] = None
        self.load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self) -> None:
        self.devices = {}
        self.forget_keys = []
        self.load_error = None
        if not self.path or not self.path.exists():
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):                      # legacy: ["AA:BB:...", ...]
                self.devices = {str(a).upper(): {"name": "", "volume": 1.0} for a in data}
            elif isinstance(data, dict):
                for addr, info in (data.get("devices") or {}).items():
                    info = info if isinstance(info, dict) else {}
                    self.devices[str(addr).upper()] = {
                        "name": str(info.get("name", "")),
                        "volume": float(info.get("volume", 1.0)),
                        "auto_connect": bool(info.get("auto_connect", False)),
                    }
                self.forget_keys = [str(a).upper() for a in data.get("forget_keys", [])]
            else:
                raise ValueError("unexpected JSON layout")
        except Exception as exc:
            self.load_error = f"{self.path.name}: {exc}"
            log.warning("device store unreadable: %s", exc)

    def save(self) -> Optional[str]:
        """Writes the file; returns an error message instead of raising."""
        if not self.path:
            return None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"devices": self.devices, "forget_keys": self.forget_keys},
                          f, indent=2)
            return None
        except OSError as exc:
            log.warning("device store save failed: %s", exc)
            return f"{self.path.name}: {exc}"

    # ------------------------------------------------------------------
    # Queries and updates (callers save() when done)
    # ------------------------------------------------------------------

    def is_remembered(self, addr: str) -> bool:
        return addr.upper() in self.devices

    def remember(self, addr: str, name: str = "") -> None:
        addr = addr.upper()
        entry = self.devices.setdefault(addr, {"name": "", "volume": 1.0})
        if name:
            entry["name"] = name
        if addr in self.forget_keys:
            self.forget_keys.remove(addr)

    def forget(self, addr: str) -> None:
        """Drops the device from the approval list and queues its bonding key for deletion."""
        addr = addr.upper()
        self.devices.pop(addr, None)
        if addr not in self.forget_keys:
            self.forget_keys.append(addr)

    def forget_all(self) -> None:
        self.devices.clear()
        self.forget_keys.clear()

    def name(self, addr: str) -> str:
        return self.devices.get(addr.upper(), {}).get("name", "")

    def set_name(self, addr: str, name: str) -> bool:
        """Returns True when the stored name changed."""
        entry = self.devices.get(addr.upper())
        if entry is None or not name or entry.get("name") == name:
            return False
        entry["name"] = name
        return True

    def volume(self, addr: str) -> float:
        return float(self.devices.get(addr.upper(), {}).get("volume", 1.0))

    def set_volume(self, addr: str, volume: float) -> bool:
        entry = self.devices.get(addr.upper())
        if entry is None or abs(entry.get("volume", 1.0) - volume) < 1e-6:
            return False
        entry["volume"] = round(max(0.0, min(2.0, volume)), 3)
        return True

    def auto_connect(self, addr: str) -> bool:
        return bool(self.devices.get(addr.upper(), {}).get("auto_connect", False))

    def set_auto_connect(self, addr: str, enabled: bool) -> None:
        entry = self.devices.get(addr.upper())
        if entry is not None:
            entry["auto_connect"] = bool(enabled)

    def auto_connect_addrs(self) -> list[str]:
        return [a for a, info in self.devices.items() if info.get("auto_connect")]

    def pop_pending_forget(self) -> list[str]:
        pending, self.forget_keys = self.forget_keys, []
        return pending
