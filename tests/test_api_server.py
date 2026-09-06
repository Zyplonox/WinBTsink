"""HTTP control API: request validation, routing and one end-to-end call."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request

import pytest

from api_server import ApiError, ApiServer, BackendController, dispatch_post

ADDR = "AA:BB:CC:DD:EE:FF"


class FakeController:
    """Records what the API asked the backend to do."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args):
            self.calls.append((name, *args))
        return record

    def status(self):
        self.calls.append(("status",))
        return {"state": "ready", "pairing_allowed": True, "master_volume": 1.0,
                "devices": [], "remembered": []}


def post(path, body=None):
    controller = FakeController()
    dispatch_post(controller, path, body or {})
    return controller.calls


# ------------------------------------------------------------------ routing

def test_simple_routes_reach_the_controller():
    assert post("/api/start") == [("start",)]
    assert post("/api/stop") == [("stop",)]
    assert post("/api/pairing", {"allowed": False}) == [("set_pairing", False)]
    assert post("/api/volume", {"percent": 80}) == [("set_master_volume", 80)]


def test_device_routes_carry_an_uppercased_address():
    assert post(f"/api/devices/{ADDR.lower()}/connect") == [("connect", ADDR)]
    assert post(f"/api/devices/{ADDR}/disconnect") == [("disconnect", ADDR)]
    assert post(f"/api/devices/{ADDR}/mute", {"muted": True}) == [("set_mute", ADDR, True)]
    assert post(f"/api/devices/{ADDR}/record", {"enabled": True}) == [("record", ADDR, True)]
    assert post(f"/api/devices/{ADDR}/player", {"action": "next"}) == [("player", ADDR, "next")]
    assert post(f"/api/devices/{ADDR}/volume", {"percent": 50}) == [("set_device_volume", ADDR, 50)]


def test_equalizer_values_are_clamped_to_twelve_decibels():
    assert post("/api/eq", {"bass": 99, "mid": -99, "treble": 3}) == [("set_eq", 12, -12, 3)]


def test_missing_equalizer_bands_default_to_flat():
    assert post("/api/eq", {}) == [("set_eq", 0, 0, 0)]


# --------------------------------------------------------------- validation

@pytest.mark.parametrize(("path", "body"), [
    ("/api/pairing", {}),                                   # flag missing
    ("/api/pairing", {"allowed": "yes"}),                   # flag not a boolean
    ("/api/volume", {}),                                    # percent missing
    ("/api/volume", {"percent": "loud"}),                   # percent not a number
    ("/api/volume", {"percent": 500}),                      # percent out of range
    ("/api/eq", {"bass": "boom"}),                          # band not a number
    (f"/api/devices/{ADDR}/volume", {"percent": 150}),      # per-device max is 100
    (f"/api/devices/{ADDR}/player", {"action": "eject"}),   # unknown player action
    ("/api/devices/not-an-address/connect", {}),            # malformed address
])
def test_bad_requests_are_rejected_with_400(path, body):
    with pytest.raises(ApiError) as err:
        dispatch_post(FakeController(), path, body)
    assert err.value.status == 400


@pytest.mark.parametrize("path", ["/api/nope", f"/api/devices/{ADDR}/explode", "/"])
def test_unknown_paths_are_404(path):
    with pytest.raises(ApiError) as err:
        dispatch_post(FakeController(), path, {})
    assert err.value.status == 404


def test_a_validation_error_never_reaches_the_controller():
    controller = FakeController()
    with pytest.raises(ApiError):
        dispatch_post(controller, "/api/volume", {"percent": 500})
    assert controller.calls == []


# -------------------------------------------------------------- controller

def test_controller_reports_stopped_without_a_backend():
    controller = BackendController(lambda: None, lambda: None, lambda: None)
    assert controller.status()["state"] == "stopped"


def test_controller_refuses_device_actions_without_a_backend():
    controller = BackendController(lambda: None, lambda: None, lambda: None)
    with pytest.raises(ApiError) as err:
        controller.set_pairing(True)
    assert err.value.status == 409


# ------------------------------------------------------------- end to end

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def running_api():
    controller = FakeController()
    server = ApiServer(controller, port=free_port())
    server.start()
    try:
        yield server, controller
    finally:
        server.stop()


def test_the_server_serves_the_page_and_the_status(running_api):
    server, _controller = running_api
    with urllib.request.urlopen(server.url, timeout=5) as response:
        page = response.read().decode("utf-8")
    assert page.startswith("<!doctype html>")

    with urllib.request.urlopen(server.url + "api/status", timeout=5) as response:
        status = json.loads(response.read())
    assert status["state"] == "ready"


def test_the_server_answers_a_bad_request_with_400(running_api):
    server, _ = running_api
    request = urllib.request.Request(server.url + "api/volume", method="POST",
                                     data=b'{"percent": 500}',
                                     headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(request, timeout=5)
    assert err.value.code == 400


def test_the_server_binds_only_to_localhost(running_api):
    server, _ = running_api
    assert server.url.startswith("http://127.0.0.1:")
