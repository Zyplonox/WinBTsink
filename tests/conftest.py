r"""
Shared fixtures.

The tests must never touch the real %APPDATA%\BT-AudioSink, so every test
that can reach config.py gets a redirected APPDATA. src/ is put on sys.path
by the `pythonpath` setting in pyproject.toml.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def appdata(tmp_path, monkeypatch):
    """Redirects %APPDATA% at the environment level and returns the directory."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    return tmp_path
