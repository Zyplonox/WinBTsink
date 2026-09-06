"""
media_keys.py – global keyboard media keys (Play/Pause, Next, Prev, Stop)
=========================================================================
Registers the four media keys as global hotkeys so the GUI can forward them
to the streaming phone via AVRCP.  Windows delivers WM_HOTKEY only to the
thread that registered the key, so a small dedicated thread runs its own
message loop and hands each key press to a callback (on that thread).

Registering the keys takes them away from other applications for as long
as the listener runs; that is why the feature is opt-in and only active
while the Bluetooth stack is started.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from collections.abc import Callable

log = logging.getLogger("bt-sink.mediakeys")

# hotkey id → (name, virtual-key code)
_KEYS = {
    1: ("play_pause", 0xB3),   # VK_MEDIA_PLAY_PAUSE
    2: ("next", 0xB0),         # VK_MEDIA_NEXT_TRACK
    3: ("prev", 0xB1),         # VK_MEDIA_PREV_TRACK
    4: ("stop", 0xB2),         # VK_MEDIA_STOP
}
_MOD_NOREPEAT = 0x4000
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012


class MediaKeyListener:
    """Owns the hotkey thread. start() returns False if no key could be registered."""

    def __init__(self, on_key: Callable[[str], None]):
        self._on_key = on_key
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._registered = 0

    def start(self) -> bool:
        if sys.platform != "win32" or self._thread is not None:
            return False
        self._thread = threading.Thread(target=self._run, daemon=True, name="media-keys")
        self._thread.start()
        self._ready.wait(timeout=2.0)
        return self._registered > 0

    def stop(self) -> None:
        if self._thread is None:
            return
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
        self._thread.join(timeout=2.0)
        self._thread = None

    # ------------------------------------------------------------------

    def _run(self) -> None:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                       wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = ctypes.c_int

        self._thread_id = kernel32.GetCurrentThreadId()
        registered = []
        for hotkey_id, (name, vk) in _KEYS.items():
            if user32.RegisterHotKey(None, hotkey_id, _MOD_NOREPEAT, vk):
                registered.append(hotkey_id)
            else:
                log.warning("RegisterHotKey(%s) failed: error %d", name, ctypes.get_last_error())
        self._registered = len(registered)
        self._ready.set()
        if not registered:
            return

        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == _WM_HOTKEY and msg.wParam in _KEYS:
                    try:
                        self._on_key(_KEYS[msg.wParam][0])
                    except Exception:
                        log.exception("media key handler failed")
        finally:
            for hotkey_id in registered:
                user32.UnregisterHotKey(None, hotkey_id)
