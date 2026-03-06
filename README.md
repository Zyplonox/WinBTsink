# WinBTsink

> **Disclaimer:** This project was built entirely with the assistance of [Claude Code](https://claude.ai/code) (Anthropic's AI coding assistant). All code, scripts, and documentation were generated through an AI-guided development session.

Turns your USB Bluetooth dongle into a Bluetooth audio sink (like a speaker or headset).

Devices such as a Nintendo Switch 2, phone, or tablet pair with your PC and stream their audio directly through your PC speakers. Up to 4 devices can stream simultaneously.

---

## How it works

Windows supports Bluetooth A2DP Sink mode natively on recent builds
(via [AudioPlaybackConnector](https://github.com/ysc3839/AudioPlaybackConnector)).
WinBTsink takes a different approach by bypassing the Windows Bluetooth stack entirely,
which has its own advantages:

- **Multi-device support** – up to 4 sources can stream simultaneously
- **Full control** over codec parameters, latency, and audio routing
- **Bonding persistence** – paired devices reconnect automatically after a restart

This program works by:

1. Installing the **WinUSB driver** for your dongle (via Zadig https://github.com/pbatard/libwdi)
2. Using **BTstack** (a C Bluetooth stack compiled to `btstack_sink.exe`) to access the dongle directly via USB – bypassing Windows entirely
3. Advertising the PC as a Bluetooth speaker
4. Decoding incoming SBC audio frames with **FFmpeg** (bundled)
5. Playing the audio through your PC speakers via **sounddevice** (WASAPI)

```
BT device (Switch / phone / tablet)
    │  Bluetooth A2DP / SBC
    ▼
USB dongle ──(WinUSB / libusb)──▶  btstack_sink.exe  (C, BTstack)
                                         │  SBC frames (per-device, tagged)
                                         ▼
                                    FFmpeg (decoder, bundled)
                                         │  PCM audio
                                         ▼
                                   sounddevice → speakers
```

> **Why WinUSB?**  Windows automatically installs its own HCI driver for the dongle.
> `btstack_sink.exe` needs direct USB access – the Windows driver must be replaced with **WinUSB**.
> WinUSB is a Microsoft inbox driver (included in Windows, signed by Microsoft) –
> no third-party kernel drivers are involved.

---

## Quick start (recommended)

### Option A – Pre-built EXE

1. Download `BT-AudioSink.exe` from the [Releases](../../releases) page
2. Run it
3. Install the WinUSB driver once: **Settings → Install WinUSB…**
4. Click **Start** → pair your device → done

> **Note:** The EXE is ~94 MB. This is expected — it bundles a full FFmpeg binary (~83 MB)
> needed to decode Bluetooth SBC audio. There is no lighter alternative.

### Option B – Run from source

```powershell
# One-time setup (installs Python packages and ffmpeg)
powershell -ExecutionPolicy Bypass -File setup\install.ps1

# Launch the GUI
python src\gui.py
```

> **Note:** Running from source also requires building `btstack_sink.exe` once — see [Building btstack_sink.exe](#building-btstack_sinkexe) below.

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

### Building btstack_sink.exe

`btstack_sink.exe` is the C-based Bluetooth engine. The pre-built EXE includes it automatically. When running from source you need to build it once:

```powershell
# Requires MSYS2/MinGW (installed automatically if missing)
cd btstack
bash do_build.sh
```

The resulting binary lands at `btstack\build\btstack_sink.exe`.

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
| **Allow new pairings** | Toggle: allow unknown devices to pair (see Security) |
| **Install WinUSB…** | Download & launch Zadig |
| Status dot | grey=idle · amber=starting · blue=ready · green=connected · red=error |
| Audio Level | Real-time RMS meter |
| Log | Timestamped log messages |

### Pairing from a Bluetooth device (e.g. Nintendo Switch 2)

1. Click **Start** and wait until status shows "Waiting for device…" (blue)
2. Make sure **Allow new pairings** is enabled (default: on)
3. Switch 2: **System Settings → Bluetooth Audio → Pair Device**
4. Select `PC-AudioSink` from the list
5. Confirm the pairing request in the dialog that appears on your PC
6. Play audio → it comes out of your PC speakers

---

## Settings

| Option | Default | Description |
|--------|---------|-------------|
| Device name | `PC-AudioSink` | Name shown to other BT devices |
| Bluetooth address | `F0:F1:F2:F3:F4:F5` | Local BT address used by BTstack |
| Buffer latency | `50 ms` | Audio buffer size; increase if audio stutters |
| Max SBC bitpool | `53` | Quality ceiling; higher = better quality, more bandwidth |
| Audio output device | Default | WASAPI output device |
| Autostart | off | Launch with Windows, minimized to tray |

Settings are saved at: `%APPDATA%\BT-AudioSink\config.json`

---

## Security

### Pairing confirmation

When an unknown device tries to pair, a dialog appears showing the device name and
MAC address. You can:

- **Allow** – accept for this session only (device must re-pair next time)
- **Allow** + **Remember this device** – accept and save the device permanently
- **Deny** – reject the pairing request

If the dialog is not answered within 30 seconds it auto-denies.

### Allow new pairings toggle

The **Allow new pairings** switch in the main window controls whether unknown devices
can initiate a pairing at all:

- **On** (default) – unknown devices trigger the confirmation dialog
- **Off** – only previously bonded devices can connect; all others are silently rejected

Recommended workflow: enable the toggle when adding a new device, then disable it
again for day-to-day use.

### Notes on Bluetooth security

- Already known devices reconnect automatically without a dialog.
- MITM protection is not available because Secure Connections must be disabled for
  Nintendo Switch compatibility. This is a Bluetooth protocol limitation.
- A2DP audio streams are not encrypted by the protocol.

## Bonding / Device pairing

Paired devices (iPhone, Android, Nintendo Switch 2) are remembered when you choose
**Remember this device** in the pairing dialog.
On the next session the device reconnects without re-pairing as long as the app is running.

Bonding keys are managed by `btstack_sink.exe` and stored in:
`btstack\build\btstack_keys.db` (next to the C binary)

Remembered device list (MACs): `%APPDATA%\BT-AudioSink\allowed_macs.json`

To reset all pairings: delete both files and restart the app.

---

## System tray

Clicking the **–** button hides the window to the system tray instead of closing the app.
Right-click the tray icon for the menu: **Show Window / Start BT / Stop BT / Quit**.

When **Autostart** is enabled the app launches directly minimized to the tray on login.

---

## Troubleshooting

### "Transport error" / Dongle not found

- Is the dongle plugged in?
- Is the WinUSB driver installed? → Click "Install WinUSB…" in the main window
- Click Scan to re-detect dongles

### Device has to re-pair every time

- Did you check **Remember this device** in the pairing dialog? Without it the key is not saved.
- Check that `btstack\build\btstack_keys.db` exists and is writable.

### No audio / device not found

- Restart the app, then search again from the device
- BTstack needs 2–3 seconds to initialise after clicking Start

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
| [BTstack](https://github.com/bluekitchen/btstack) | C Bluetooth stack – implements HCI / L2CAP / AVDTP / A2DP; compiled to `btstack_sink.exe` |
| [Zadig](https://github.com/pbatard/libwdi) | USB driver replacement for direct WinUSB access |
| [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) | Bundles FFmpeg automatically |
| [sounddevice](https://python-sounddevice.readthedocs.io/) | WASAPI audio output |
| [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) | Modern dark-mode GUI |
| [pystray](https://github.com/moses-palmer/pystray) | System tray icon |
| [PyInstaller](https://pyinstaller.org/) | Packages everything into a single .exe |

**Supported codecs:** SBC (mandatory A2DP codec)

**Supported devices:** iPhone, Android, Nintendo Switch 2, and any A2DP source

---

## Project structure

```
WinBTsink/
├── dist\BT-AudioSink.exe   ← Built Windows EXE (after running build.ps1)
├── build.ps1               ← Build script (one click)
├── BT-AudioSink.spec       ← PyInstaller configuration
├── requirements.txt        ← Python dependencies
├── start.bat               ← Launches the GUI via Python
├── src/
│   ├── gui.py              ← CustomTkinter GUI (entry point)
│   ├── backend.py          ← Bluetooth + audio backend (launches btstack_sink.exe)
│   └── winusb_installer.py ← Zadig helper
├── btstack/
│   ├── btstack_sink.c      ← C Bluetooth engine (HCI / AVDTP / A2DP sink)
│   ├── CMakeLists.txt      ← Build configuration
│   ├── btstack_config.h    ← BTstack feature flags
│   ├── do_build.sh         ← Build script (MSYS2/MinGW)
│   ├── btstack-src/        ← BTstack library source (submodule)
│   └── build/
│       ├── btstack_sink.exe ← Compiled BT engine
│       └── btstack_keys.db  ← Bonding keys (auto-created)
└── setup/
    └── install.ps1         ← One-time setup script

%APPDATA%\BT-AudioSink\     ← Created automatically on first launch
├── config.json             ← Saved settings
└── allowed_macs.json       ← Remembered device addresses
```

---

## License

[MIT](LICENSE)
