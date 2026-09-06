"""DeviceStore: persistence, migration and the forget-key queue."""

from __future__ import annotations

import json

from device_store import DeviceStore


def store_at(tmp_path, data=None, raw=None):
    path = tmp_path / "allowed_macs.json"
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
    elif data is not None:
        path.write_text(json.dumps(data), encoding="utf-8")
    return DeviceStore(str(path))


def test_missing_file_starts_empty(tmp_path):
    store = DeviceStore(str(tmp_path / "nope.json"))
    assert store.devices == {} and store.forget_keys == []
    assert store.load_error is None


def test_legacy_list_is_migrated(tmp_path):
    store = store_at(tmp_path, ["aa:bb:cc:dd:ee:ff"])
    assert store.is_remembered("AA:BB:CC:DD:EE:FF")
    assert store.volume("aa:bb:cc:dd:ee:ff") == 1.0


def test_addresses_are_case_insensitive(tmp_path):
    store = store_at(tmp_path, {"devices": {"aa:bb:cc:dd:ee:ff": {"name": "Phone"}}})
    assert store.is_remembered("AA:BB:CC:DD:EE:FF")
    assert store.name("Aa:Bb:Cc:Dd:Ee:Ff") == "Phone"


def test_one_bad_field_does_not_drop_the_device(tmp_path):
    store = store_at(tmp_path, {"devices": {"AA:BB:CC:DD:EE:FF": {"name": "X", "volume": "loud"}}})
    assert store.is_remembered("AA:BB:CC:DD:EE:FF")
    assert store.volume("AA:BB:CC:DD:EE:FF") == 1.0


def test_volume_is_clamped_on_load_and_write(tmp_path):
    store = store_at(tmp_path, {"devices": {"AA:BB:CC:DD:EE:FF": {"volume": 99}}})
    assert store.volume("AA:BB:CC:DD:EE:FF") == 2.0
    store.set_volume("AA:BB:CC:DD:EE:FF", -5)
    assert store.volume("AA:BB:CC:DD:EE:FF") == 0.0


def test_unreadable_file_is_reported_and_backed_up(tmp_path):
    store = store_at(tmp_path, raw="{ this is not json")
    assert store.devices == {} and store.load_error
    store.remember("AA:BB:CC:DD:EE:FF", "Phone")
    assert store.save() is None
    assert (tmp_path / "allowed_macs.json.bak").exists()
    assert DeviceStore(str(tmp_path / "allowed_macs.json")).name("AA:BB:CC:DD:EE:FF") == "Phone"


def test_forget_queues_the_bonding_key_once(tmp_path):
    store = store_at(tmp_path, {"devices": {"AA:BB:CC:DD:EE:FF": {}}})
    store.forget("aa:bb:cc:dd:ee:ff")
    store.forget("AA:BB:CC:DD:EE:FF")
    assert store.forget_keys == ["AA:BB:CC:DD:EE:FF"]
    assert not store.is_remembered("AA:BB:CC:DD:EE:FF")
    assert store.pop_pending_forget() == ["AA:BB:CC:DD:EE:FF"]
    assert store.pop_pending_forget() == []


def test_remembering_again_cancels_a_pending_forget(tmp_path):
    store = store_at(tmp_path, {"devices": {"AA:BB:CC:DD:EE:FF": {}}})
    store.forget("AA:BB:CC:DD:EE:FF")
    store.remember("AA:BB:CC:DD:EE:FF", "Phone")
    assert store.forget_keys == []


def test_set_name_reports_whether_it_changed(tmp_path):
    store = store_at(tmp_path, {"devices": {"AA:BB:CC:DD:EE:FF": {"name": "Old"}}})
    assert store.set_name("AA:BB:CC:DD:EE:FF", "New") is True
    assert store.set_name("AA:BB:CC:DD:EE:FF", "New") is False
    assert store.set_name("AA:BB:CC:DD:EE:FF", "") is False
    assert store.set_name("11:22:33:44:55:66", "Ghost") is False


def test_auto_connect_round_trip(tmp_path):
    path = tmp_path / "allowed_macs.json"
    store = DeviceStore(str(path))
    store.remember("AA:BB:CC:DD:EE:FF", "Phone")
    store.set_auto_connect("AA:BB:CC:DD:EE:FF", True)
    store.save()
    assert DeviceStore(str(path)).auto_connect_addrs() == ["AA:BB:CC:DD:EE:FF"]
