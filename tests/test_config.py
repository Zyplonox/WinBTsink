"""Settings: typed loading, clamping and the derived backend arguments."""

from __future__ import annotations

import json
import sys

import pytest

import config
from config import Settings


@pytest.fixture
def settings(appdata, monkeypatch):
    """A Settings instance whose files live in a temporary APPDATA."""
    monkeypatch.setattr(config, "get_autostart", lambda: False)
    return Settings()


def write_config(appdata, data):
    path = appdata / "BT-AudioSink"
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.json").write_text(json.dumps(data), encoding="utf-8")


def test_defaults_survive_a_missing_file(settings):
    settings.load()
    assert settings.device_name == "PC-AudioSink"
    assert settings.max_bitpool == 53


def test_unreadable_file_falls_back_to_defaults(settings, appdata):
    (appdata / "BT-AudioSink").mkdir(parents=True)
    (appdata / "BT-AudioSink" / "config.json").write_text("{oops", encoding="utf-8")
    settings.load()
    assert settings.device_name == "PC-AudioSink"


def test_values_of_the_wrong_type_are_ignored(settings, appdata):
    write_config(appdata, {"device_name": 42, "latency_ms": "slow", "max_bitpool": 40})
    settings.load()
    assert settings.device_name == "PC-AudioSink"   # int rejected
    assert settings.latency_ms == 50                # str rejected
    assert settings.max_bitpool == 40               # int accepted


def test_a_bool_does_not_slip_into_an_int_field(settings, appdata):
    write_config(appdata, {"latency_ms": True})
    settings.load()
    assert settings.latency_ms == 50


def test_api_port_outside_the_allowed_range_is_reset(settings, appdata):
    write_config(appdata, {"api_port": 80})
    settings.load()
    assert settings.api_port == 8765
    write_config(appdata, {"api_port": 9001})
    settings.load()
    assert settings.api_port == 9001


def test_save_then_load_round_trip(settings, appdata):
    settings.device_name = "Living room"
    settings.eq_bass = 4
    settings.multi_device_mode = "duck"
    settings.save()
    fresh = Settings()
    fresh.load()
    assert fresh.device_name == "Living room"
    assert fresh.eq_bass == 4
    assert fresh.multi_device_mode == "duck"


def test_audio_filter_follows_the_equalizer(settings):
    assert settings.audio_filter == ""
    settings.eq_bass, settings.eq_treble = 3, -2
    assert "bass=g=3" in settings.audio_filter
    assert "treble=g=-2" in settings.audio_filter


def test_recording_dir_defaults_into_the_user_profile(settings):
    assert settings.recording_dir == ""
    assert settings.effective_recording_dir.endswith("BT-AudioSink")
    settings.recording_dir = "D:/rec"
    assert settings.effective_recording_dir == "D:/rec"


def test_backend_kwargs_are_complete_and_scaled(settings, monkeypatch):
    monkeypatch.setattr(config, "resolve_output_device_index", lambda name: None)
    monkeypatch.setattr(config, "get_ffmpeg", lambda: "ffmpeg")
    settings.duck_level = 25
    kwargs = settings.backend_kwargs()
    assert kwargs["duck_level"] == 0.25
    assert kwargs["device_name"] == settings.device_name
    assert kwargs["keystore_path"].endswith("btstack_keys.db")


def test_backend_kwargs_match_the_backend_signature(settings, monkeypatch):
    """Guards the config -> SinkBackend contract against a renamed parameter."""
    monkeypatch.setattr(config, "resolve_output_device_index", lambda name: None)
    monkeypatch.setattr(config, "get_ffmpeg", lambda: "ffmpeg")
    import inspect

    from backend import SinkBackend
    accepted = set(inspect.signature(SinkBackend.__init__).parameters)
    assert set(settings.backend_kwargs()) <= accepted


def test_paths_live_under_appdata(appdata):
    assert config.appdata_dir().startswith(str(appdata))
    assert config.keystore_file().endswith("btstack_keys.db")
    assert config.allowed_macs_file().endswith("allowed_macs.json")


@pytest.mark.skipif(sys.platform != "win32", reason="registry autostart is Windows-only")
def test_autostart_command_targets_an_existing_interpreter():
    command = config.autostart_command("src/gui.py")
    assert command.startswith('"') and "--minimized" in command
