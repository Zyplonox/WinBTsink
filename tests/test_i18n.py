"""Translation lookup. Nothing here builds a window."""

from __future__ import annotations

import pytest

import i18n
from i18n import tr


@pytest.fixture(autouse=True)
def restore_language():
    before = i18n.language()
    yield
    i18n.set_language(before)


def test_english_returns_the_source_string():
    i18n.set_language("en")
    assert tr("Settings") == "Settings"


def test_german_translates_a_known_string():
    i18n.set_language("de")
    assert tr("Settings") == "Einstellungen"


def test_an_untranslated_string_falls_back_to_the_source():
    i18n.set_language("de")
    assert tr("A string nobody has translated") == "A string nobody has translated"


def test_empty_input_stays_empty():
    i18n.set_language("de")
    assert tr("") == ""


def test_placeholders_survive_translation():
    i18n.set_language("de")
    translated = tr("{n} device(s) without WinUSB – select in Zadig and install driver.")
    assert "{n}" in translated
    assert translated.format(n=2).startswith("2 ")


def test_every_german_translation_keeps_its_placeholders():
    """A missing {name} in a translation would raise KeyError at runtime."""
    import re
    table = i18n._TABLES["de"]
    for source, translated in table.items():
        assert set(re.findall(r"{(\w+)}", source)) == set(re.findall(r"{(\w+)}", translated)), source


def test_an_unknown_language_code_falls_back_to_english():
    i18n.set_language("fr")
    assert tr("Settings") == "Settings"
