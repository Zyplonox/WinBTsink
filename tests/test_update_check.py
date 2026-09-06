"""Release comparison. No test here reaches the network."""

from __future__ import annotations

import pytest

import update_check
from update_check import Release, check_for_update, parse_version


@pytest.mark.parametrize(("text", "expected"), [
    ("v2.1.0", (2, 1, 0)),
    ("2.1", (2, 1)),
    ("release-2.1.0-beta", (2, 1, 0)),
    ("", ()),
    ("no digits here", ()),
    (None, ()),
])
def test_version_parsing(text, expected):
    assert parse_version(text) == expected


def release(tag):
    return Release(tag=tag, version=parse_version(tag), url="https://example.invalid", name=tag)


@pytest.mark.parametrize(("latest", "current", "is_update"), [
    ("v2.2.0", "2.1.0", True),
    ("v2.1.1", "2.1.0", True),
    ("v3.0", "2.9.9", True),
    ("v2.1.0", "2.1.0", False),
    ("v2.1", "2.1.0", False),      # a shorter tag is padded, not treated as older
    ("v2.0.0", "2.1.0", False),
    ("v2.1.0", "2.1.1", False),
])
def test_only_a_newer_release_is_offered(monkeypatch, latest, current, is_update):
    monkeypatch.setattr(update_check, "fetch_latest", lambda timeout=10.0: release(latest))
    assert (check_for_update(current) is not None) is is_update


def test_an_unreachable_api_is_not_an_update(monkeypatch):
    monkeypatch.setattr(update_check, "fetch_latest", lambda timeout=10.0: None)
    assert check_for_update("2.1.0") is None


def test_a_tag_without_a_version_is_ignored(monkeypatch):
    monkeypatch.setattr(update_check, "fetch_latest", lambda timeout=10.0: release("nightly"))
    assert check_for_update("2.1.0") is None


def test_the_release_url_points_at_this_repository():
    assert update_check.RELEASES_URL.endswith("/releases")
    assert "Zyplonox/WinBTsink" in update_check.RELEASES_URL


@pytest.mark.parametrize(("html_url", "expected_prefix"), [
    ("https://github.com/Zyplonox/WinBTsink/releases/tag/v2.2.0", "https://github.com/Zyplonox"),
    ("http://github.com/Zyplonox/WinBTsink/releases", update_check.RELEASES_URL),
    ("https://evil.example/releases", update_check.RELEASES_URL),
    ("file:///C:/Windows/System32/calc.exe", update_check.RELEASES_URL),
    ("", update_check.RELEASES_URL),
])
def test_only_an_https_github_release_page_is_kept(html_url, expected_prefix):
    """The URL comes from the API response and the GUI opens it in a browser."""
    assert update_check._safe_release_url(html_url).startswith(expected_prefix)
