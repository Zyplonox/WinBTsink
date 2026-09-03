"""
update_check.py – look for a newer release on GitHub
=====================================================
Asks the GitHub releases API for the latest release of the project and
compares its tag with the running version. Network access only happens
when check_for_update() is called; the GUI runs it on a background thread
a few seconds after start-up (setting "Check for updates", default on).
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("bt-sink.update")

GITHUB_REPO = "Zyplonox/WinBTsink"
_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases"


@dataclass(frozen=True)
class Release:
    tag: str
    version: tuple[int, ...]
    url: str
    name: str


def parse_version(text: str) -> tuple[int, ...]:
    """'v2.1.0' / '2.1' / 'release-2.1.0-beta' → (2, 1, 0); unparsable → ()."""
    m = re.search(r"(\d+(?:\.\d+)*)", text or "")
    if not m:
        return ()
    return tuple(int(p) for p in m.group(1).split("."))


def _newer(a: tuple[int, ...], b: tuple[int, ...]) -> bool:
    """True when a > b, comparing like (2,1) vs (2,1,0) as equal."""
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def fetch_latest(timeout: float = 10.0) -> Optional[Release]:
    """Latest published release, or None on any network/API problem."""
    try:
        req = urllib.request.Request(_API, headers={"User-Agent": "BT-AudioSink",
                                                    "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        tag = str(data.get("tag_name", ""))
        return Release(tag=tag, version=parse_version(tag),
                       url=str(data.get("html_url") or RELEASES_URL),
                       name=str(data.get("name") or tag))
    except Exception as exc:
        log.info("update check skipped: %s", exc)
        return None


def check_for_update(current_version: str, timeout: float = 10.0) -> Optional[Release]:
    """Returns the latest release if it is newer than current_version, else None."""
    latest = fetch_latest(timeout)
    if latest is None or not latest.version:
        return None
    return latest if _newer(latest.version, parse_version(current_version)) else None
