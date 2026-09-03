"""
i18n.py – user-interface translations
=====================================
The GUI is written in English; this module provides German. Static widget
texts are translated automatically: install() wraps the CustomTkinter
widget constructors and configure() so every `text=` (and window title)
passes through tr(). Strings that are built at runtime use tr() directly.

Language selection: Settings → "Language" ("auto" follows the Windows UI
language). BTSINK_LANG=de|en overrides it (useful for tests).
"""

from __future__ import annotations

import ctypes
import os
import sys
from typing import Any

_LANG = "en"

# English source text → German. Keys must match the widget texts exactly.
_DE: dict[str, str] = {
    # window titles
    "Settings": "Einstellungen",
    "Install WinUSB Driver": "WinUSB-Treiber installieren",
    "Pairing Request": "Kopplungsanfrage",
    # main window
    "USB Dongle": "USB-Dongle",
    "Install WinUSB…": "WinUSB installieren…",
    "Scan": "Suchen",
    "Scanning…": "Suche…",
    "Connected Devices": "Verbundene Geräte",
    "Connect to": "Verbinden mit",
    "Connect": "Verbinden",
    "no remembered devices": "keine gemerkten Geräte",
    "No WinUSB dongle found": "Kein WinUSB-Dongle gefunden",
    "⚠ No WinUSB dongle found – install WinUSB driver first":
        "⚠ Kein WinUSB-Dongle gefunden – zuerst WinUSB-Treiber installieren",
    "{n} dongle(s) found – ready": "{n} Dongle gefunden – bereit",
    "Audio Level": "Pegel",
    "Volume": "Lautstärke",
    "Equalizer": "Equalizer",
    "Flat": "Neutral",
    "Bass": "Bass", "Mid": "Mitten", "Treble": "Höhen",
    "▶  Start": "▶  Start",
    "■  Stop": "■  Stopp",
    "⚙  Settings": "⚙  Einstellungen",
    "Allow new pairings:": "Neue Kopplungen erlauben:",
    "Log": "Protokoll",
    "● Ready": "● Bereit",
    "Ready": "Bereit",
    "Starting…": "Startet…",
    "Waiting for device…": "Warte auf Gerät…",
    "Connected": "Verbunden",
    "Error": "Fehler",
    "Stopped": "Gestoppt",
    # tray
    "Show Window": "Fenster anzeigen",
    "Start BT": "BT starten",
    "Stop BT": "BT stoppen",
    "Quit": "Beenden",
    "BT-AudioSink – stopped": "BT-AudioSink – gestoppt",
    "BT-AudioSink – waiting for device": "BT-AudioSink – warte auf Gerät",
    "BT-AudioSink – {n} device(s): {names}": "BT-AudioSink – {n} Gerät(e): {names}",
    # notifications
    "Device connected": "Gerät verbunden",
    "Device disconnected": "Gerät getrennt",
    "Pairing request": "Kopplungsanfrage",
    "{addr} wants to connect – answer within 30 s.":
        "{addr} möchte sich verbinden – Antwort innerhalb von 30 s.",
    "Connection failed": "Verbindung fehlgeschlagen",
    "{name} is not reachable.": "{name} ist nicht erreichbar.",
    "The Bluetooth stack stopped with an error. See the log.":
        "Der Bluetooth-Stack wurde mit einem Fehler beendet. Siehe Protokoll.",
    # device card
    "Unknown Device": "Unbekanntes Gerät",
    "▶ playing": "▶ Wiedergabe", "⏸ paused": "⏸ Pause", "■ stopped": "■ gestoppt",
    "⏩ seeking": "⏩ Suchlauf", "player error": "Player-Fehler",
    "Default": "Standard",
    # pairing dialog
    "Unknown device wants to connect": "Unbekanntes Gerät möchte sich verbinden",
    "Remember this device": "Dieses Gerät merken",
    "Auto-deny in {s}s": "Automatische Ablehnung in {s} s",
    "Deny": "Ablehnen",
    "Allow": "Erlauben",
    # settings dialog
    "Device name": "Gerätename",
    "Class of Device": "Geräteklasse",
    "Affects how your PC appears to phones/headsets. Change if a device behaves oddly.":
        "Bestimmt, wie der PC bei Handys/Headsets erscheint. Ändern, falls sich ein Gerät seltsam verhält.",
    "Headphones (0x240418)": "Kopfhörer (0x240418)",
    "Speaker / Loudspeaker (0x240414)": "Lautsprecher (0x240414)",
    "Car Audio (0x240420)": "Autoradio (0x240420)",
    "Wearable Headset (0x240404)": "Headset (0x240404)",
    "Discoverable timeout": "Sichtbarkeits-Timeout",
    "seconds  (0 = stay on until manually disabled)": "Sekunden  (0 = an, bis manuell deaktiviert)",
    "Buffer latency": "Puffer-Latenz",
    "  ⚠ may cause audio dropouts": "  ⚠ kann Aussetzer verursachen",
    "  (BT radio latency ~60 ms is not configurable)": "  (Funklatenz ~60 ms ist nicht einstellbar)",
    "Max SBC bitpool": "Max. SBC-Bitpool",
    "{bp}  (higher = better audio quality, more bandwidth)": "{bp}  (höher = bessere Qualität, mehr Bandbreite)",
    "SBC block length": "SBC-Blocklänge",
    "4 = lowest latency  ·  16 = best quality (most sources pick 16 on Auto)":
        "4 = geringste Latenz  ·  16 = beste Qualität (die meisten Quellen wählen bei Auto 16)",
    "SBC subbands": "SBC-Subbänder",
    "4 = lower latency  ·  8 = better frequency resolution":
        "4 = geringere Latenz  ·  8 = bessere Frequenzauflösung",
    "SBC allocation method": "SBC-Allokationsmethode",
    "Loudness = perceptually optimised  ·  SNR = mathematically optimal":
        "Loudness = gehörrichtig  ·  SNR = mathematisch optimal",
    "Offer aptX and aptX HD (experimental)": "aptX und aptX HD anbieten (experimentell)",
    "Android phones and many laptops then stream with aptX instead of SBC: "
    "better sound, lower latency. iPhones keep using AAC. Turn off if a "
    "device refuses to connect.":
        "Android-Handys und viele Laptops streamen dann mit aptX statt SBC: besserer Klang, "
        "weniger Latenz. iPhones bleiben bei AAC. Abschalten, falls sich ein Gerät nicht verbindet.",
    "Several devices playing": "Mehrere Geräte spielen",
    "Mix – all devices play together": "Mischen – alle Geräte spielen zusammen",
    "Duck – others get quieter while the latest plays": "Absenken – andere werden leiser, solange das neueste spielt",
    "Solo – only the latest device is audible": "Solo – nur das neueste Gerät ist hörbar",
    "Duck level": "Absenkung auf",
    "The device whose stream started last is the foreground device.":
        "Das Gerät, dessen Stream zuletzt gestartet ist, steht im Vordergrund.",
    "Recording folder": "Aufnahmeordner",
    "Browse…": "Durchsuchen…",
    "Audio output device": "Audio-Ausgabegerät",
    "Debug log (shows AVDTP/L2CAP protocol)": "Debug-Protokoll (zeigt AVDTP/L2CAP)",
    "Start with Windows (autostart, minimized to tray)": "Mit Windows starten (Autostart, minimiert im Tray)",
    "Show notifications (connect, disconnect, pairing request)":
        "Benachrichtigungen anzeigen (Verbinden, Trennen, Kopplungsanfrage)",
    "Local control API on port": "Lokale Steuer-API auf Port",
    "http://127.0.0.1:<port>/ shows a small remote control page; scripts can use "
    "the /api endpoints (see src/api_server.py). Only reachable from this PC. "
    "Takes effect after restarting the app.":
        "http://127.0.0.1:<port>/ zeigt eine kleine Fernbedienungsseite; Skripte nutzen die "
        "/api-Endpunkte (siehe src/api_server.py). Nur von diesem PC erreichbar. "
        "Wirksam nach Neustart der App.",
    "Keyboard media keys control the playing device": "Medientasten der Tastatur steuern das spielende Gerät",
    "Play/Pause, Next, Previous and Stop keys are sent to the phone via AVRCP "
    "while the BT stack runs. Other apps do not receive them meanwhile.":
        "Play/Pause, Weiter, Zurück und Stopp gehen per AVRCP an das Handy, solange der "
        "BT-Stack läuft. Andere Programme bekommen die Tasten währenddessen nicht.",
    "Language": "Sprache",
    "Auto (Windows language)": "Automatisch (Windows-Sprache)",
    "Takes effect after restarting the app.": "Wirksam nach Neustart der App.",
    "Remembered devices": "Gemerkte Geräte",
    "none": "keine",
    "Unknown device": "Unbekanntes Gerät",
    "Forget": "Vergessen",
    "auto-connect": "Auto-Verbinden",
    "Forget all paired devices": "Alle gekoppelten Geräte vergessen",
    "Deletes the remembered-device list and the Bluetooth bonding keys. "
    "Every device has to pair again. Takes effect immediately.":
        "Löscht die Liste gemerkter Geräte und die Bluetooth-Bonding-Schlüssel. "
        "Jedes Gerät muss neu koppeln. Wirkt sofort.",
    "Cancel": "Abbrechen",
    "Save": "Speichern",
    # WinUSB dialog
    "Windows requires the WinUSB driver so btstack_sink.exe\ncan access the Bluetooth USB dongle directly.":
        "Windows braucht den WinUSB-Treiber, damit btstack_sink.exe\ndirekt auf den Bluetooth-USB-Dongle zugreifen kann.",
    "How to use Zadig:\n  1. Enable Options → List All Devices\n  2. Select your Bluetooth dongle from the list\n"
    "  3. Set driver to »WinUSB«\n  4. Click »Install Driver«":
        "So geht es in Zadig:\n  1. Options → List All Devices aktivieren\n  2. Den Bluetooth-Dongle in der Liste wählen\n"
        "  3. Treiber auf »WinUSB« stellen\n  4. »Install Driver« klicken",
    "Windows only.": "Nur unter Windows.",
    "Detected BT dongles without WinUSB:": "Gefundene BT-Dongles ohne WinUSB:",
    'Click "Scan" to search for devices…': "„Suchen“ klicken, um Geräte zu finden…",
    "Download & launch Zadig": "Zadig herunterladen & starten",
    "Scan dongles": "Dongles suchen",
    "Close": "Schließen",
    "No dongle without WinUSB found.\nIs the dongle connected? WinUSB may already be installed.":
        "Kein Dongle ohne WinUSB gefunden.\nIst der Dongle eingesteckt? Vielleicht ist WinUSB schon installiert.",
    "No devices found": "Keine Geräte gefunden",
    "{n} device(s) without WinUSB – select in Zadig and install driver.":
        "{n} Gerät(e) ohne WinUSB – in Zadig auswählen und Treiber installieren.",
    "Connecting to GitHub…": "Verbinde mit GitHub…",
}

_TABLES = {"de": _DE}


def detect_language() -> str:
    """'de' when the Windows UI language is German, else 'en'."""
    env = os.environ.get("BTSINK_LANG", "").lower()
    if env in _TABLES or env == "en":
        return env
    if sys.platform == "win32":
        try:
            langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            if (langid & 0x3FF) == 0x07:
                return "de"
        except Exception:
            pass
    return "en"


def set_language(lang: str) -> None:
    """lang: 'auto', 'en' or a key of the translation tables."""
    global _LANG
    if os.environ.get("BTSINK_LANG"):
        _LANG = detect_language()
    elif lang == "auto" or lang not in _TABLES:
        _LANG = detect_language() if lang == "auto" else "en"
    else:
        _LANG = lang


def language() -> str:
    return _LANG


def tr(text: Any) -> Any:
    """Translates a UI string; unknown strings and non-strings pass through."""
    if _LANG == "en" or not isinstance(text, str):
        return text
    return _TABLES[_LANG].get(text, text)


# ---------------------------------------------------------------------------
# Automatic translation of CustomTkinter widget texts
# ---------------------------------------------------------------------------

_installed = False


def install() -> None:
    """Wraps CTk widget constructors/configure so `text=` is translated."""
    global _installed
    if _installed:
        return
    _installed = True
    import customtkinter as ctk

    def wrap(cls, attr: str, keys: tuple[str, ...]) -> None:
        original = getattr(cls, attr)

        def patched(self, *args, **kwargs):
            for key in keys:
                if key in kwargs:
                    kwargs[key] = tr(kwargs[key])
            return original(self, *args, **kwargs)

        patched.__name__ = original.__name__
        setattr(cls, attr, patched)

    for cls in (ctk.CTkLabel, ctk.CTkButton, ctk.CTkCheckBox, ctk.CTkSwitch, ctk.CTkRadioButton):
        wrap(cls, "__init__", ("text",))
        wrap(cls, "configure", ("text",))
    wrap(ctk.CTkEntry, "__init__", ("placeholder_text",))

    for cls in (ctk.CTk, ctk.CTkToplevel):
        original_title = cls.title

        def patched_title(self, string=None, _orig=original_title):
            return _orig(self, tr(string)) if string is not None else _orig(self)

        cls.title = patched_title
