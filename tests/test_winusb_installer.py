"""Download-URL validation. Nothing here downloads or launches anything."""

from __future__ import annotations

import pytest

import winusb_installer
from winusb_installer import _check_url, _find_zadig_asset


@pytest.mark.parametrize("url", [
    "https://github.com/pbatard/libwdi/releases/download/v1.5.0/zadig-2.9.exe",
    "https://api.github.com/repos/pbatard/libwdi/releases/latest",
    "https://objects.githubusercontent.com/github-production-release-asset/1/2",
    "https://GITHUB.COM/pbatard/libwdi",
])
def test_github_https_urls_are_accepted(url):
    assert _check_url(url) == url


@pytest.mark.parametrize("url", [
    "http://github.com/pbatard/libwdi/releases/download/zadig.exe",   # no TLS
    "file:///C:/Windows/System32/cmd.exe",                           # local file
    "ftp://github.com/zadig.exe",                                    # other scheme
    "https://evil.example/zadig.exe",                                # foreign host
    "https://github.com.evil.example/zadig.exe",                      # lookalike host
    "https://notgithubusercontent.com/zadig.exe",
    "",
])
def test_everything_else_is_refused(url):
    """The downloaded file is run with elevation, so the URL must be checked."""
    with pytest.raises(ValueError):
        _check_url(url)


def test_a_redirect_to_a_foreign_host_is_refused():
    handler = winusb_installer._CheckedRedirects()
    with pytest.raises(ValueError):
        handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/zadig.exe")


def test_the_zadig_asset_is_picked_by_name():
    release = {"assets": [
        {"name": "libwdi.zip", "browser_download_url": "https://github.com/a"},
        {"name": "zadig-2.9.exe", "browser_download_url": "https://github.com/b"},
    ]}
    assert _find_zadig_asset(release)["name"] == "zadig-2.9.exe"


@pytest.mark.parametrize("release", [
    {"assets": []},
    {"assets": [{"name": "readme.txt"}]},
    {"assets": [{"name": "zadig.zip"}]},
    {},
])
def test_a_release_without_a_zadig_executable_yields_nothing(release):
    assert _find_zadig_asset(release) is None
