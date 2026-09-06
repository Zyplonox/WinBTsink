"""
api_server.py – local HTTP/JSON control API
============================================
A tiny REST-style API on 127.0.0.1 so the sink can be controlled from
scripts, a phone browser on the same PC, Stream Deck style tools, or the
headless runner. It talks to a *controller* object that both the GUI and
the headless runner provide:

    status() -> dict                      snapshot (see SinkBackend.snapshot)
    start() / stop()                      run / stop the Bluetooth stack
    set_pairing(allowed: bool)
    set_master_volume(percent: int)
    set_device_volume(addr, percent)
    set_mute(addr, muted: bool)
    player(addr, action)                  play|pause|stop|next|prev
    connect(addr) / disconnect(addr)
    set_eq(bass, mid, treble)             dB
    record(addr, enabled: bool)

Endpoints (all JSON, all on http://127.0.0.1:<port>):
    GET  /api/status
    POST /api/start            POST /api/stop
    POST /api/pairing          {"allowed": true}
    POST /api/volume           {"percent": 80}
    POST /api/eq               {"bass": 3, "mid": 0, "treble": -2}
    POST /api/devices/<addr>/volume   {"percent": 50}
    POST /api/devices/<addr>/mute     {"muted": true}
    POST /api/devices/<addr>/player   {"action": "pause"}
    POST /api/devices/<addr>/record   {"enabled": true}
    POST /api/devices/<addr>/connect  POST /api/devices/<addr>/disconnect
    GET  /                     minimal HTML page that renders /api/status

Binding to 127.0.0.1 keeps the API off the network; there is no auth.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from api_page import INDEX_HTML

log = logging.getLogger("bt-sink.api")

_ADDR_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status




class ApiServer:
    """Runs a ThreadingHTTPServer on a daemon thread; stop() shuts it down."""

    def __init__(self, controller: Any, host: str = "127.0.0.1", port: int = 8765):
        if not 1 <= int(port) <= 65535:
            raise ValueError(f"API port out of range: {port}")
        self._controller = controller
        self._host = host
        self._port = int(port)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}/"

    def start(self) -> None:
        controller = self._controller

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):   # keep the console quiet
                log.debug("api: " + fmt, *args)

            def _send(self, status: int, body: Any, content_type: str = "application/json") -> None:
                data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type + "; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def _body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    return {}
                try:
                    data = json.loads(self.rfile.read(length))
                except ValueError:
                    raise ApiError(400, "body must be JSON") from None
                if not isinstance(data, dict):
                    raise ApiError(400, "body must be a JSON object")
                return data

            def do_GET(self):
                try:
                    if self.path in ("/", "/index.html"):
                        self._send(200, INDEX_HTML, "text/html")
                    elif self.path == "/api/status":
                        self._send(200, controller.status())
                    else:
                        raise ApiError(404, "not found")
                except ApiError as exc:
                    self._send(exc.status, {"error": str(exc)})
                except Exception as exc:  # never let the handler thread die silently
                    log.exception("api GET failed")
                    self._send(500, {"error": str(exc)})

            def do_POST(self):
                try:
                    result = dispatch_post(controller, self.path, self._body())
                    self._send(200, result if result is not None else {"ok": True})
                except ApiError as exc:
                    self._send(exc.status, {"error": str(exc)})
                except Exception as exc:
                    log.exception("api POST failed")
                    self._send(500, {"error": str(exc)})

        self._server = ThreadingHTTPServer((self._host, self._port), Handler)
        self._server.daemon_threads = True
        # Never join handler threads in server_close(): a handler may be
        # waiting for the GUI thread (controller.start/stop use after()),
        # and the GUI thread is the one calling stop() → deadlock.
        self._server.block_on_close = False
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="api")
        self._thread.start()
        log.info("API listening on %s", self.url)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


# ---------------------------------------------------------------------------
# Request routing (module-level so it is easy to unit-test)
# ---------------------------------------------------------------------------

def _pct(body: dict, key: str = "percent", lo: int = 0, hi: int = 200) -> int:
    try:
        value = int(body[key])
    except (KeyError, TypeError, ValueError):
        raise ApiError(400, f"'{key}' (integer) required") from None
    if not lo <= value <= hi:
        raise ApiError(400, f"'{key}' must be {lo}..{hi}")
    return value


def _flag(body: dict, key: str) -> bool:
    if key not in body or not isinstance(body[key], bool):
        raise ApiError(400, f"'{key}' (boolean) required")
    return body[key]


def dispatch_post(controller: Any, path: str, body: dict) -> dict | None:
    if path == "/api/start":
        controller.start()
    elif path == "/api/stop":
        controller.stop()
    elif path == "/api/pairing":
        controller.set_pairing(_flag(body, "allowed"))
    elif path == "/api/volume":
        controller.set_master_volume(_pct(body))
    elif path == "/api/eq":
        bands = []
        for key in ("bass", "mid", "treble"):
            try:
                bands.append(max(-12, min(12, int(body.get(key, 0)))))
            except (TypeError, ValueError):
                raise ApiError(400, f"'{key}' must be an integer") from None
        controller.set_eq(*bands)
    else:
        m = re.match(r"^/api/devices/([^/]+)/([a-z]+)$", path)
        if not m:
            raise ApiError(404, "not found")
        addr, action = m.group(1).upper(), m.group(2)
        if not _ADDR_RE.match(addr):
            raise ApiError(400, "invalid address")
        if action == "volume":
            controller.set_device_volume(addr, _pct(body, hi=100))
        elif action == "mute":
            controller.set_mute(addr, _flag(body, "muted"))
        elif action == "player":
            act = body.get("action")
            if act not in ("play", "pause", "stop", "next", "prev"):
                raise ApiError(400, "'action' must be play|pause|stop|next|prev")
            controller.player(addr, act)
        elif action == "record":
            controller.record(addr, _flag(body, "enabled"))
        elif action == "connect":
            controller.connect(addr)
        elif action == "disconnect":
            controller.disconnect(addr)
        else:
            raise ApiError(404, "not found")
    return None


class BackendController:
    """
    Controller for a SinkBackend that is managed by someone else (headless
    runner or GUI). start/stop are delegated to callables so the owner
    decides how a backend is created.
    """

    def __init__(self, get_backend: Callable[[], Any], start: Callable[[], None],
                 stop: Callable[[], None], settings: Any = None):
        self._get_backend = get_backend
        self._start = start
        self._stop = stop
        self._settings = settings

    def _backend(self):
        backend = self._get_backend()
        if backend is None:
            raise ApiError(409, "Bluetooth stack is not running")
        return backend

    def status(self) -> dict:
        backend = self._get_backend()
        if backend is None:
            return {"state": "stopped", "pairing_allowed": None, "master_volume": None,
                    "devices": [], "remembered": []}
        return backend.snapshot()

    def start(self) -> None:
        self._start()

    def stop(self) -> None:
        self._stop()

    def set_pairing(self, allowed: bool) -> None:
        self._backend().set_pairing_mode(allowed)

    def set_master_volume(self, percent: int) -> None:
        if self._settings is not None:
            self._settings.volume = percent / 100.0
        self._backend().set_volume(percent / 100.0)

    def set_device_volume(self, addr: str, percent: int) -> None:
        backend = self._backend()
        backend.set_device_volume(addr, percent / 100.0)
        backend.notify_volume_changed(round(percent * 127 / 100), addr)

    def set_mute(self, addr: str, muted: bool) -> None:
        self._backend().set_device_mute(addr, muted)

    def player(self, addr: str, action: str) -> None:
        self._backend().player_control(addr, action)

    def connect(self, addr: str) -> None:
        self._backend().connect_device(addr)

    def disconnect(self, addr: str) -> None:
        self._backend().disconnect_device(addr)

    def set_eq(self, bass: int, mid: int, treble: int) -> None:
        from backend import build_eq_filter
        if self._settings is not None:
            self._settings.eq_bass, self._settings.eq_mid, self._settings.eq_treble = bass, mid, treble
        backend = self._get_backend()
        if backend is not None:
            backend.set_audio_filter(build_eq_filter(bass, mid, treble))

    def record(self, addr: str, enabled: bool) -> None:
        backend = self._backend()
        if enabled:
            directory = (self._settings.effective_recording_dir if self._settings is not None
                         else ".")
            backend.start_recording(addr, directory, backend.display_name(addr))
        else:
            backend.stop_recording(addr)
