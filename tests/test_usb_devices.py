"""Dongle identification: what gets derived from a Windows device instance id."""

from __future__ import annotations

import pytest

from usb_devices import UsbDevice

WINUSB_ID = r"USB\VID_0A12&PID_0001\5&2C1F8B6&0&3"
COMPOSITE_ID = r"USB\VID_8087&PID_0026&MI_00\6&1A2B&0&0000"


def device(instance_id=WINUSB_ID, name="BT dongle", service="WinUSB", vid=0x0A12, pid=0x0001):
    return UsbDevice(instance_id=instance_id, name=name, service=service, vid=vid, pid=pid)


def test_path_filter_is_the_lower_cased_instance_tail():
    assert device().path_filter == "vid_0a12&pid_0001#5&2c1f8b6&0&3#"


def test_path_filter_keeps_the_interface_of_a_composite_device():
    dev = device(COMPOSITE_ID, service="BTHUSB", vid=0x8087, pid=0x0026)
    assert dev.path_filter == "vid_8087&pid_0026&mi_00#6&1a2b&0&0000#"


def test_the_filter_ends_with_a_separator_so_it_cannot_match_a_longer_id():
    assert device().path_filter.endswith("#")


@pytest.mark.parametrize(("service", "expected"), [
    ("WinUSB", True), ("winusb", True), ("BTHUSB", False), ("", False),
])
def test_only_the_winusb_driver_counts_as_usable(service, expected):
    assert device(service=service).uses_winusb is expected


def test_label_shows_the_name_with_the_usb_ids():
    assert device().label == "BT dongle  [VID:0A12 PID:0001]"
