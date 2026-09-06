"""SinkBackend helpers that need neither a dongle nor FFmpeg."""

from __future__ import annotations

import io

import pytest

from backend import SinkBackend, build_eq_filter

A = "AA:AA:AA:AA:AA:AA"
B = "BB:BB:BB:BB:BB:BB"


# ---------------------------------------------------------------- equalizer

def test_flat_equalizer_produces_no_filter():
    assert build_eq_filter(0, 0, 0) == ""


def test_each_band_appears_with_its_frequency():
    assert build_eq_filter(bass_db=3) == "bass=g=3:f=100"
    assert build_eq_filter(mid_db=-4) == "equalizer=f=1000:t=q:w=1:g=-4"
    assert build_eq_filter(treble_db=6) == "treble=g=6:f=8000"


def test_bands_are_combined_in_order():
    assert build_eq_filter(1, 2, 3).split(",") == [
        "bass=g=1:f=100", "equalizer=f=1000:t=q:w=1:g=2", "treble=g=3:f=8000"]


def test_gain_is_clamped_to_the_supported_range():
    assert "g=12" in build_eq_filter(bass_db=99)
    assert "g=-12" in build_eq_filter(bass_db=-99)


# ------------------------------------------------------ multi-device policy

def backend_with_two_streams(mode):
    backend = SinkBackend(multi_device_mode=mode, duck_level=0.25)
    with backend._lock:
        backend._connected_addrs.update({A, B})
    return backend


def gains(backend):
    with backend._lock:
        return round(backend._effective_gain(A), 3), round(backend._effective_gain(B), 3)


@pytest.mark.parametrize(("mode", "expected"), [
    ("mix", (1.0, 1.0)),      # both stay at full volume
    ("duck", (0.25, 1.0)),    # the older stream is turned down
    ("solo", (0.0, 1.0)),     # the older stream is silenced
])
def test_the_newest_stream_defines_the_foreground(mode, expected):
    backend = backend_with_two_streams(mode)
    backend._note_stream_started(A)
    assert gains(backend) == (1.0, 1.0)
    backend._note_stream_started(B)
    assert gains(backend) == expected


@pytest.mark.parametrize("mode", ["mix", "duck", "solo"])
def test_stopping_the_foreground_restores_the_other(mode):
    backend = backend_with_two_streams(mode)
    backend._note_stream_started(A)
    backend._note_stream_started(B)
    backend._note_stream_stopped(B)
    assert gains(backend) == (1.0, 1.0)


def test_mute_wins_over_every_volume():
    backend = backend_with_two_streams("mix")
    backend.set_device_volume(A, 2.0)
    backend.set_device_mute(A, True)
    assert gains(backend)[0] == 0.0


def test_device_volume_is_clamped():
    backend = backend_with_two_streams("mix")
    backend.set_device_volume(A, 9.0)
    assert gains(backend)[0] == 2.0
    backend.set_device_volume(A, -1.0)
    assert gains(backend)[0] == 0.0


# ------------------------------------------------------------------- naming

def test_display_name_prefers_the_resolved_remote_name():
    backend = SinkBackend()
    assert backend.display_name(A) == A
    with backend._lock:
        backend._names[A] = "Pixel 8"
    assert backend.display_name(a_lower := A.lower()) == "Pixel 8"
    assert a_lower.islower()


# ---------------------------------------------------------- audio framing

class FakePipeline:
    def __init__(self):
        self.frames = []

    def write_audio(self, payload):
        self.frames.append(payload)


def frame(addr_bytes: bytes, payload: bytes) -> bytes:
    body = addr_bytes + payload
    return len(body).to_bytes(4, "little") + body


def test_frames_are_routed_to_the_addressed_pipeline():
    backend = SinkBackend()
    pipe_a, pipe_b = FakePipeline(), FakePipeline()
    backend._pipelines = {A: pipe_a, B: pipe_b}
    stream = io.BytesIO(
        frame(b"\xaa" * 6, b"first")
        + frame(b"\xbb" * 6, b"second")
        + frame(b"\xcc" * 6, b"unknown device is dropped")
        + frame(b"\xaa" * 6, b"third")
    )
    backend._audio_thread(stream)
    assert pipe_a.frames == [b"first", b"third"]
    assert pipe_b.frames == [b"second"]


def test_a_malformed_header_stops_the_reader_instead_of_desynchronising():
    backend = SinkBackend()
    pipe = FakePipeline()
    backend._pipelines = {A: pipe}
    logged = []
    backend._cb_log = logged.append
    stream = io.BytesIO(
        frame(b"\xaa" * 6, b"good")
        + (999_999).to_bytes(4, "little") + b"junk"
        + frame(b"\xaa" * 6, b"never read")
    )
    backend._audio_thread(stream)
    assert pipe.frames == [b"good"]
    assert any("malformed" in m for m in logged)


def test_a_truncated_frame_ends_the_reader_quietly():
    backend = SinkBackend()
    pipe = FakePipeline()
    backend._pipelines = {A: pipe}
    stream = io.BytesIO(frame(b"\xaa" * 6, b"payload")[:-3])
    backend._audio_thread(stream)
    assert pipe.frames == []


# ------------------------------------------------------------------ snapshot

def test_snapshot_of_an_idle_backend_is_json_friendly():
    import json
    snapshot = SinkBackend().snapshot()
    assert snapshot["devices"] == []
    json.dumps(snapshot)
