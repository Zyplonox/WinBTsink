# WinBTsink

> **Disclaimer:** This project was built entirely with the assistance of [Claude Code](https://claude.ai/code) (Anthropic's AI coding assistant). All code, scripts, and documentation were generated through an AI-guided development session.

Turns your USB Bluetooth dongle into a Bluetooth audio sink (like a speaker or headset).

Devices such as the Nintendo Switch, a phone, or tablet pair with your PC and stream their audio directly through your PC speakers.

---

## How it works

Windows 11 does **not** support Bluetooth A2DP Sink mode natively (it can only *send* audio, not receive it).
This program works around that by:

1. Installing the **WinUSB driver** for your dongle (via Zadig https://github.com/pbatard/libwdi)
2. Using **Bumble** (a Python Bluetooth stack) to access the dongle directly via USB – bypassing Windows entirely
3. Advertising the PC as a Bluetooth speaker
4. Decoding incoming SBC audio frames with **FFmpeg** (bundled)
5. Playing the audio through your PC speakers via **sounddevice** (WASAPI)

```
BT device (Switch / phone)
    │  Bluetooth A2DP / SBC
    ▼
USB dongle ──(libusb / WinUSB)──▶  Bumble (Python BT stack)
                                         │  SBC frames
                                         ▼
                                    FFmpeg (decoder, bundled)
                                         │  PCM audio
                                         ▼
                                   sounddevice → speakers
```

> **Why WinUSB?**  Windows automatically installs its own HCI driver for the dongle.
> Bumble needs direct USB access – the Windows driver must be replaced with **WinUSB**.
> WinUSB is a Microsoft inbox driver (included in Windows, signed by Microsoft) –

---

## Quick start (recommended)

### Option A – Pre-built EXE

1. Download `BT-AudioSink.exe` from the [Releases](../../releases) page
2. Run it
3. Install the WinUSB driver once: **Settings → Install WinUSB…**
5. Click **Start** → pair your device → done

> **Note:** The EXE is ~94 MB. This is expected — it bundles a full FFmpeg binary (~83 MB)
> needed to decode Bluetooth SBC audio. There is no lighter alternative.

### Option B – Run from source

```powershell
# One-time setup (installs Python packages and ffmpeg)
powershell -ExecutionPolicy Bypass -File setup\install.ps1

# Launch the GUI
python src\gui.py
```

### Option C – Build the EXE yourself

```powershell
.\build.ps1
# → dist\BT-AudioSink.exe
```

---

## Installation (one-time)

### Step 1 – Install Python

If not already installed: https://www.python.org/downloads/
**Important:** Check "Add Python to PATH" during setup!

### Step 2 – Install dependencies

```powershell
powershell -ExecutionPolicy Bypass -File setup\install.ps1
```

### Step 3 – Install the WinUSB driver

1. Plug in your Bluetooth dongle
2. Download **Zadig**: https://zadig.akeo.ie/
3. Run Zadig **as Administrator**
4. Go to **Options → List All Devices**
5. Select your Bluetooth device (not mice/keyboards/HID!)
6. Set driver to **WinUSB** → click **Replace Driver**

> **Note:** After this step the dongle is **no longer** usable for normal Windows Bluetooth
> (mouse, keyboard, Windows Settings). Use your built-in Bluetooth for that,
> or a second dongle.

---

## Usage

### GUI

| Element | Function |
|---------|----------|
| **Start** | Start the BT stack and activate the dongle |
| **Stop** | Stop everything cleanly |
| **Settings** | Device name, latency, audio device, autostart |
| **USB Dongle** | Dropdown + Scan button: select the WinUSB dongle |
| **Volume** | Output volume slider (0–200%) |
| **Install WinUSB…** | Download & launch Zadig |
| Status dot | grey=idle · amber=starting · blue=ready · green=connected · red=error |
| Audio Level | Real-time RMS meter |
| Log | Timestamped log messages |

### Pairing from a Bluetooth device (e.g. Nintendo Switch)

1. Click **Start** and wait until status shows "Waiting for device…" (blue)
2. Switch: **System Settings → Bluetooth Audio → Pair Device**
3. Select `PC-AudioSink` from the list
4. Play audio → it comes out of your PC speakers

---

## Settings

| Option | Default | Description |
|--------|---------|-------------|
| Device name | `PC-AudioSink` | Name shown to other BT devices |
| Bluetooth address | `F0:F1:F2:F3:F4:F5` | Local BT address used by Bumble |
| Buffer latency | `50 ms` | Audio buffer size; increase if audio stutters |
| Max SBC bitpool | `53` | Quality ceiling; higher = better quality, more bandwidth |
| Audio output device | Default | WASAPI output device |
| Autostart | off | Launch with Windows, minimized to tray |

Settings are saved at: `%APPDATA%\BT-AudioSink\config.json`

---

## Bonding / Device pairing

Paired devices (iPhone, Android, Switch) are **remembered automatically**.
On the next session the device reconnects without re-pairing as long as the app is running.

Bonding keys are stored at: `%APPDATA%\BT-AudioSink\keys.json`

To reset all pairings: delete that file and restart the app.

---

## System tray

Clicking the **×** or **–** button hides the window to the system tray instead of closing the app.
Right-click the tray icon for the menu: **Show Window / Start BT / Stop BT / Quit**.

When **Autostart** is enabled the app launches directly minimized to the tray on login.

---

## Troubleshooting

### "Transport error" / Dongle not found

- Is the dongle plugged in?
- Is the WinUSB driver installed? → Click "Install WinUSB…" in the main window
- Click Scan to re-detect dongles

### Device has to re-pair every time

Pairing is normally saved automatically. If not:
- Check that `%APPDATA%\BT-AudioSink\keys.json` exists and is writable
- Old EXE versions without a keystore: pair once, then it is saved

### No audio / device not found

- Restart the app, then search again from the device
- Bumble needs 2–3 seconds to initialise

### Audio stutters / dropouts

- Settings → Buffer latency → increase to 200 ms or higher
- Check CPU load

### Restore dongle for normal Windows Bluetooth use

Device Manager → `USB devices` → `Bluetooth USB Dongle (WinUSB)` → right-click →
**Update driver → Search automatically** → Windows re-installs the original BT driver.

---

## Technical details

| Component | Purpose |
|-----------|---------|
| [Bumble](https://github.com/google/bumble) | Python Bluetooth stack – implements HCI / L2CAP / AVDTP / A2DP |
| [Zadig](https://github.com/pbatard/libwdi) | USB driver replacement for direct winusb access |
| [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) | Bundles FFmpeg automatically |
| [sounddevice](https://python-sounddevice.readthedocs.io/) | WASAPI audio output |
| [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) | Modern dark-mode GUI |
| [pystray](https://github.com/moses-palmer/pystray) | System tray icon |
| [PyInstaller](https://pyinstaller.org/) | Packages everything into a single .exe |

**Supported codecs:** SBC (mandatory A2DP codec)

---

## Project structure

```
bluetooth-usb-sink/
├── dist\BT-AudioSink.exe   ← Built Windows EXE (after running build.ps1)
├── build.ps1               ← Build script (one click)
├── BT-AudioSink.spec       ← PyInstaller configuration
├── requirements.txt        ← Python dependencies
├── start.bat               ← Launches the GUI via Python
├── src/
│   ├── gui.py              ← CustomTkinter GUI (entry point)
│   ├── backend.py          ← Bluetooth + audio backend
│   └── winusb_installer.py ← Zadig helper
└── setup/
    └── install.ps1         ← One-time setup script

%APPDATA%\BT-AudioSink\     ← Created automatically on first launch
├── config.json             ← Saved settings
└── keys.json               ← BT bonding keys (auto-managed)
```

---

## License

[MIT](LICENSE)
