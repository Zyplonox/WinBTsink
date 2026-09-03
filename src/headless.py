"""
headless.py – run the Bluetooth audio sink without the GUI
===========================================================
Uses the same settings file and remembered-device list as the GUI, so the
usual workflow is: pair devices once in the GUI, then run

    python src\\headless.py

on login, as a scheduled task, or on a headless box. Remembered devices are
approved automatically; unknown devices are denied unless --allow-unknown
is given (they are then approved *and remembered*).

The local HTTP API (api_server.py) is enabled by default on 127.0.0.1:8765
so the running sink can still be controlled: open http://127.0.0.1:8765/
in a browser or POST to the /api endpoints.

Options:
    --dongle TEXT      substring of the dongle label or device path to use
                       (default: first WinUSB Bluetooth dongle)
    --name NAME        advertised Bluetooth name (default: from settings)
    --allow-unknown    approve and remember devices that are not remembered yet
    --no-pairing       reject unknown devices and stay non-discoverable
    --api PORT         API port (default: settings, else 8765); --no-api disables it
    --list-dongles     print the attached Bluetooth dongles and exit
    --debug            verbose engine logging
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from typing import Optional

from api_server import ApiServer, BackendController
from backend import SinkBackend, SinkState
from config import allowed_macs_file, configure_logging, settings
from device_store import DeviceStore
from usb_devices import list_bluetooth_dongles

log = logging.getLogger("bt-sink.headless")


def pick_dongle(wanted: str) -> Optional[str]:
    dongles = [d for d in list_bluetooth_dongles() if d.uses_winusb]
    if not dongles:
        return None
    if wanted:
        wanted = wanted.lower()
        for d in dongles:
            if wanted in d.label.lower() or wanted in d.path_filter:
                return d.path_filter
        return None
    return dongles[0].path_filter


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="BT-AudioSink headless runner")
    parser.add_argument("--dongle", default="")
    parser.add_argument("--name", default="")
    parser.add_argument("--allow-unknown", action="store_true")
    parser.add_argument("--no-pairing", action="store_true")
    parser.add_argument("--api", type=int, default=None)
    parser.add_argument("--no-api", action="store_true")
    parser.add_argument("--list-dongles", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    configure_logging(logging.DEBUG if args.debug else logging.INFO)
    settings.load()
    if args.name:
        settings.device_name = args.name
    if args.debug:
        settings.debug_mode = True

    if args.list_dongles:
        for d in list_bluetooth_dongles():
            print(f"{d.label:50s} driver={d.service or '-':10s} filter={d.path_filter}")
        return 0

    usb_filter = pick_dongle(args.dongle)
    if not usb_filter:
        log.error("No WinUSB Bluetooth dongle found%s. Install WinUSB with Zadig first.",
                  f" matching '{args.dongle}'" if args.dongle else "")
        return 1
    settings.usb_filter = usb_filter

    store = DeviceStore(allowed_macs_file())
    stopped = threading.Event()
    state: dict = {"backend": None}

    def on_pairing(addr: str, resolve) -> None:
        if args.allow_unknown:
            log.info("Approving and remembering unknown device %s (--allow-unknown)", addr)
            resolve(True, True)
        else:
            log.info("Denying unknown device %s (start with --allow-unknown or pair in the GUI)", addr)
            resolve(False, False)

    def on_state(s: SinkState) -> None:
        log.info("state: %s", s.name)
        if s == SinkState.ERROR:
            state["error"] = True   # exit status 1 so a service wrapper can restart us
            stopped.set()

    def start() -> None:
        if state["backend"] is not None:
            return
        backend = SinkBackend(
            device_store=store,
            on_log=lambda m: log.info("%s", m),
            on_state_change=on_state,
            on_pairing_request=on_pairing,
            on_device_name=lambda a, n: log.info("device %s is %s", a, n),
            **settings.backend_kwargs(),
        )
        backend.set_pairing_mode(not args.no_pairing)
        state["backend"] = backend
        backend.start()

    def stop() -> None:
        backend, state["backend"] = state["backend"], None
        if backend is not None:
            backend.stop()

    api: Optional[ApiServer] = None
    port = args.api if args.api is not None else (settings.api_port or 8765)
    if not args.no_api:
        try:
            api = ApiServer(BackendController(lambda: state["backend"], start, stop, settings), port=port)
            api.start()
            log.info("Control API: %s", api.url)
        except (OSError, ValueError, OverflowError) as exc:
            log.warning("API not started (%s)", exc)
            api = None

    start()

    def handle_signal(signum, frame):
        stopped.set()

    signal.signal(signal.SIGINT, handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_signal)

    log.info("Running. Press Ctrl+C to stop.")
    try:
        while not stopped.wait(0.5):
            pass
    finally:
        stop()
        if api:
            api.stop()
    return 1 if state.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
