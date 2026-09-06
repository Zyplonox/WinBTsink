"""
usb_devices.py – enumerate *present* USB Bluetooth dongles via SetupAPI
=======================================================================

Both the dongle dropdown in the GUI and the "Install WinUSB" dialog need
the same answer: which Bluetooth HCI dongles are plugged in right now, and
which driver each one uses.  This module asks Windows directly through
SetupAPI (ctypes, no third-party package), which only reports devices that
are currently attached — unlike the registry, which remembers every device
ever seen.

A device is treated as a Bluetooth HCI adapter when one of its compatible
IDs is the USB class triple E0/01/01 (Wireless Controller / RF Controller /
Bluetooth programming) or its setup class is the Bluetooth class.  This
covers plain dongles as well as the Bluetooth function of composite devices.
"""

from __future__ import annotations

import ctypes
import re
import sys
from dataclasses import dataclass

# Setup class GUID Windows assigns to Bluetooth radios with the inbox driver
BLUETOOTH_CLASS_GUID = "{E0CBF06C-CD8B-4647-BB8A-263B43F0F974}"
_BT_HCI_COMPAT_ID = "USB\\CLASS_E0&SUBCLASS_01&PROT_01"
_VID_PID_RE = re.compile(r"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", re.I)


@dataclass(frozen=True)
class UsbDevice:
    """One attached USB Bluetooth adapter."""

    instance_id: str   # e.g. USB\VID_0A12&PID_0001\5&2C1F8B6&0&3
    name: str          # friendly name or device description
    service: str       # bound driver service, e.g. "WinUSB" or "BTHUSB"
    vid: int
    pid: int

    @property
    def uses_winusb(self) -> bool:
        return self.service.upper() == "WINUSB"

    @property
    def path_filter(self) -> str:
        """
        Substring of the WinUSB device path that identifies exactly this
        device; passed to btstack_sink.exe as its dongle selector.

        SetupAPI reports the instance id as  USB\\VID_0A12&PID_0001\\5&2C1F8B6&0&3
        and the matching device path is     \\\\?\\usb#vid_0a12&pid_0001#5&2c1f8b6&0&3#{guid}
        so lower-casing, swapping '\\' for '#' and appending '#' yields a
        substring that cannot match a different instance.
        """
        inst = self.instance_id.lower().replace("\\", "#")
        if inst.startswith("usb#"):
            inst = inst[4:]
        return inst + "#"

    @property
    def label(self) -> str:
        return f"{self.name}  [VID:{self.vid:04X} PID:{self.pid:04X}]"


def list_bluetooth_dongles() -> list[UsbDevice]:
    """Returns every attached USB Bluetooth HCI adapter (any driver)."""
    if sys.platform != "win32":
        return []
    return _enumerate()


# ---------------------------------------------------------------------------
# SetupAPI plumbing
# ---------------------------------------------------------------------------

def _enumerate() -> list[UsbDevice]:
    from ctypes import wintypes

    DIGCF_PRESENT = 0x02
    DIGCF_ALLCLASSES = 0x04
    SPDRP_DEVICEDESC = 0x00
    SPDRP_COMPATIBLEIDS = 0x02
    SPDRP_SERVICE = 0x04
    SPDRP_CLASSGUID = 0x08
    SPDRP_FRIENDLYNAME = 0x0C
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class SP_DEVINFO_DATA(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("ClassGuid", GUID),
            ("DevInst", wintypes.DWORD),
            ("Reserved", ctypes.c_void_p),
        ]

    api = ctypes.WinDLL("setupapi", use_last_error=True)
    api.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wintypes.LPCWSTR,
                                         wintypes.HWND, wintypes.DWORD]
    api.SetupDiGetClassDevsW.restype = wintypes.HANDLE
    api.SetupDiEnumDeviceInfo.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.POINTER(SP_DEVINFO_DATA)]
    api.SetupDiEnumDeviceInfo.restype = wintypes.BOOL
    api.SetupDiGetDeviceInstanceIdW.argtypes = [wintypes.HANDLE, ctypes.POINTER(SP_DEVINFO_DATA),
                                                wintypes.LPWSTR, wintypes.DWORD,
                                                ctypes.POINTER(wintypes.DWORD)]
    api.SetupDiGetDeviceInstanceIdW.restype = wintypes.BOOL
    api.SetupDiGetDeviceRegistryPropertyW.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]
    api.SetupDiGetDeviceRegistryPropertyW.restype = wintypes.BOOL
    api.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
    api.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL

    def read_property(devinfo, data, prop) -> list[str]:
        """Returns the property as a list of strings (REG_SZ → one element)."""
        buf = ctypes.create_unicode_buffer(2048)
        required = wintypes.DWORD(0)
        ok = api.SetupDiGetDeviceRegistryPropertyW(
            devinfo, ctypes.byref(data), prop, None,
            ctypes.cast(buf, ctypes.c_void_p), ctypes.sizeof(buf), ctypes.byref(required))
        if not ok:
            return []
        raw = buf[: required.value // 2]
        return [s for s in raw.split("\0") if s]

    def instance_id(devinfo, data) -> str:
        buf = ctypes.create_unicode_buffer(512)
        ok = api.SetupDiGetDeviceInstanceIdW(devinfo, ctypes.byref(data), buf,
                                             len(buf), None)
        return buf.value if ok else ""

    devinfo = api.SetupDiGetClassDevsW(None, "USB", None, DIGCF_PRESENT | DIGCF_ALLCLASSES)
    if not devinfo or devinfo == INVALID_HANDLE_VALUE:
        return []

    found: list[UsbDevice] = []
    try:
        index = 0
        data = SP_DEVINFO_DATA()
        while True:
            data.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)
            if not api.SetupDiEnumDeviceInfo(devinfo, index, ctypes.byref(data)):
                break
            index += 1

            inst = instance_id(devinfo, data)
            m = _VID_PID_RE.search(inst)
            if not m:
                continue   # root hubs and other non-VID/PID entries

            compat = [c.upper() for c in read_property(devinfo, data, SPDRP_COMPATIBLEIDS)]
            class_guid = read_property(devinfo, data, SPDRP_CLASSGUID)
            is_bt = any(c.startswith(_BT_HCI_COMPAT_ID) for c in compat) or (
                bool(class_guid) and class_guid[0].upper() == BLUETOOTH_CLASS_GUID
            )
            if not is_bt:
                continue

            service = read_property(devinfo, data, SPDRP_SERVICE)
            name = (read_property(devinfo, data, SPDRP_FRIENDLYNAME)
                    or read_property(devinfo, data, SPDRP_DEVICEDESC)
                    or ["Bluetooth USB Dongle"])
            found.append(UsbDevice(
                instance_id=inst,
                name=name[0],
                service=service[0] if service else "",
                vid=int(m.group(1), 16),
                pid=int(m.group(2), 16),
            ))
    finally:
        api.SetupDiDestroyDeviceInfoList(devinfo)

    return found


if __name__ == "__main__":
    for dev in list_bluetooth_dongles():
        print(f"{dev.label:50s} driver={dev.service or '-':10s} filter={dev.path_filter}")  # noqa: T201
