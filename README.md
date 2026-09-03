# WinBTsink

> **Disclaimer:** This project was built entirely with the assistance of [Claude Code](https://claude.ai/code) (Anthropic's AI coding assistant). All code, scripts, and documentation were generated through an AI-guided development session.

Turns your USB Bluetooth dongle into a Bluetooth audio sink (like a speaker or headset).

Devices such as a Nintendo Switch 2, phone, or tablet pair with your PC and stream their audio directly through your PC speakers. Up to 4 devices can stream simultaneously.

<img width="480" height="750" alt="gui1" src="https://github.com/user-attachments/assets/803581cd-3f2a-4d14-8c22-3abd1a3f5112" />
<img width="415" height="689" alt="gui2" src="https://github.com/user-attachments/assets/79230c87-235e-473f-889d-0b8a111baf97" />

> The application, its executable and its data folder are still called **BT-AudioSink**; WinBTsink is the project name.

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
4. Decoding incoming SBC or AAC audio with **FFmpeg** (bundled)
5. Playing the audio through your PC speakers via **sounddevice** (WASAPI)

```
BT device (Switch / phone / tablet)
    │  Bluetooth A2DP / SBC or AAC
    ▼
USB dongle ──(WinUSB)──▶  btstack_sink.exe  (C, BTstack)
                                │  audio frames (per-device, tagged)
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

## Quick start

### Option A – Pre-built EXE

1. Download `BT-AudioSink.exe` from the [Releases](../../releases) page
2. Run it
3. Install the WinUSB driver once: **Install WinUSB…**
4. Click **Start** → pair your device → done

> **Note:** The EXE is large (~50 MB) because it bundles a full FFmpeg binary
> needed to decode Bluetooth SBC/AAC audio.

### Option B – Run from source

```powershell
# One-time setup (installs the Python packages; FFmpeg comes with them)
powershell -ExecutionPolicy Bypass -File setup\install.ps1

# One-time build of the Bluetooth engine (installs MSYS2/MinGW if missing)
powershell -ExecutionPolicy Bypass -File btstack\build.ps1

# Launch the GUI
python src\gui.py
```

### Option C – Build the EXE yourself

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
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

Easiest: click **Install WinUSB…** in the app. It lists the attached Bluetooth
dongles that still use the Windows driver and downloads and launches Zadig for you.

Manually:

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

`btstack_sink.exe` is the C-based Bluetooth engine. The pre-built EXE includes it. When running from source you need to build it once:

```powershell
powershell -ExecutionPolicy Bypass -File btstack\build.ps1
```

The script installs MSYS2 with the MinGW-w64 toolchain if needed, clones BTstack v1.6.1
into `btstack\btstack-src`, applies the patches from `btstack\patches`, and builds.
The resulting binary lands at `btstack\build\btstack_sink.exe`.
If you already have MSYS2, `bash btstack/do_build.sh` does the clone/patch/build steps only.

---

## Usage

### GUI

| Element | Function |
|---------|----------|
| **Start** | Start the BT stack and activate the dongle |
| **Stop** | Stop everything cleanly |
| **Settings** | Device name, latency, audio device, autostart |
| **USB Dongle** | Dropdown + Scan button: select the WinUSB dongle to use |
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
| Class of Device | Headphones | How the PC presents itself (headphones, speaker, car audio, …) |
| Discoverable timeout | `0` (off) | Automatically block new pairings after N seconds |
| Buffer latency | `50 ms` | Audio buffer size; increase if audio stutters |
| Max SBC bitpool | `53` | Quality ceiling; higher = better quality, more bandwidth |
| SBC block length / subbands / allocation | Auto | Restrict what the sink offers; the source must encode with exactly these values (4 blocks / 4 subbands = lowest latency). Auto lets the source choose. The negotiated values are shown on the device card. |
| Audio output device | Default | WASAPI output device (per connected device via its card) |
| Debug log | off | Verbose protocol logging |
| Autostart | off | Launch with Windows, minimized to tray |

Settings are saved at: `%APPDATA%\BT-AudioSink\config.json`

---

## Security

### Pairing confirmation

When an unknown device tries to connect, a dialog appears showing its
MAC address. You can:

- **Allow** – accept for this session only (device must be confirmed again next time)
- **Allow** + **Remember this device** – accept and save the device permanently
- **Deny** – reject the connection

If the dialog is not answered within 30 seconds it auto-denies.

### Allow new pairings toggle

The **Allow new pairings** switch in the main window controls whether unknown devices
can initiate a pairing at all:

- **On** (default) – unknown devices trigger the confirmation dialog
- **Off** – only previously remembered devices can connect; all others are silently rejected

The switch turns itself off as soon as a device connects, and optionally after the
configured discoverable timeout.

### Notes on Bluetooth security

- Remembered devices reconnect automatically without a dialog.
- MITM protection is not available because Secure Connections must be disabled for
  Nintendo Switch compatibility. This is a Bluetooth protocol limitation.
- A2DP audio streams are not encrypted by the protocol.

## Bonding / Device pairing

Paired devices (iPhone, Android, Nintendo Switch 2) are remembered when you choose
**Remember this device** in the pairing dialog.

Everything lives in `%APPDATA%\BT-AudioSink\`:

- `allowed_macs.json` – the remembered device list used by the confirmation gate
- `btstack_keys.db` – the Bluetooth bonding keys managed by `btstack_sink.exe`

To reset all pairings use **Settings → Forget all paired devices** (or delete both files).

---

## System tray

Clicking the **–** button hides the window to the system tray instead of closing the app.
Right-click the tray icon for the menu: **Show Window / Start BT / Stop BT / Quit**.
Closing the window with **X** quits the app.

When **Autostart** is enabled the app launches directly minimized to the tray on login.

---

## Troubleshooting

### "No WinUSB dongle found"

- Is the dongle plugged in? The list only shows attached devices.
- Is the WinUSB driver installed? → Click "Install WinUSB…" in the main window
- Click Scan to re-detect dongles

### "exited unexpectedly" / "HCI powered off unexpectedly"

- Another program (or a stale `btstack_sink.exe`) may hold the dongle. Stop and start again.
- Re-check the WinUSB driver in Device Manager.

### Device has to re-pair every time

- Did you check **Remember this device** in the pairing dialog? Without it the device is
  only allowed for the current session.
- Check that `%APPDATA%\BT-AudioSink\btstack_keys.db` exists and is writable.

### No audio / device not found

- Restart the app, then search again from the device
- BTstack needs 2–3 seconds to initialise after clicking Start

### Audio stutters / dropouts

- Settings → Buffer latency → increase to 200 ms or higher
- Lower the max SBC bitpool if the log reports fragmented SBC frames
- Check CPU load

### Restore dongle for normal Windows Bluetooth use

Device Manager → `USB devices` → `Bluetooth USB Dongle (WinUSB)` → right-click →
**Update driver → Search automatically** → Windows re-installs the original BT driver.

---

## Technical details

| Component | Purpose |
|-----------|---------|
| [BTstack](https://github.com/bluekitchen/btstack) | C Bluetooth stack – implements HCI / L2CAP / AVDTP / A2DP / AVRCP; compiled to `btstack_sink.exe` |
| [Zadig](https://github.com/pbatard/libwdi) | USB driver replacement for direct WinUSB access |
| [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) | Provides the FFmpeg binary |
| [sounddevice](https://python-sounddevice.readthedocs.io/) | WASAPI audio output |
| [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) | Modern dark-mode GUI |
| [pystray](https://github.com/moses-palmer/pystray) | System tray icon |
| [PyInstaller](https://pyinstaller.org/) | Packages everything into a single .exe |

**Supported codecs:** SBC (mandatory A2DP codec), AAC, and optionally aptX / aptX HD
(Settings → "Offer aptX", experimental; decoded by FFmpeg)

**Supported devices:** iPhone, Android, Nintendo Switch 2, and any A2DP source

**AVRCP:** absolute volume in both directions and now-playing metadata (title / artist / album)

---

## Project structure

```
WinBTsink/
├── dist\BT-AudioSink.exe   ← Built Windows EXE (after running build.ps1)
├── build.ps1               ← Full build: C engine + PyInstaller
├── BT-AudioSink.spec       ← PyInstaller configuration
├── requirements.txt        ← Python dependencies
├── start.bat               ← Launches the GUI via Python
├── src/
│   ├── gui.py              ← CustomTkinter GUI (entry point)
│   ├── backend.py          ← Bluetooth + audio backend (launches btstack_sink.exe)
│   ├── usb_devices.py      ← Attached USB Bluetooth dongles via SetupAPI
│   └── winusb_installer.py ← Zadig download / launch helper
├── btstack/
│   ├── btstack_sink.c      ← C Bluetooth engine (HCI / AVDTP / A2DP / AVRCP sink)
│   ├── btstack_config.h    ← BTstack feature flags
│   ├── CMakeLists.txt      ← Build configuration
│   ├── build.ps1           ← Toolchain install + clone + patch + build
│   ├── do_build.sh         ← Clone + patch + build from an MSYS2 shell
│   ├── patches/apply_patches.py ← BTstack source patches (deferred accept, dongle filter)
│   ├── btstack-src/        ← BTstack source (cloned by build.ps1, git-ignored)
│   └── build/btstack_sink.exe ← Compiled BT engine (git-ignored)
└── setup/
    └── install.ps1         ← One-time Python setup script

%APPDATA%\BT-AudioSink\     ← Created automatically on first launch
├── config.json             ← Saved settings
├── allowed_macs.json       ← Remembered device addresses
├── btstack_keys.db         ← Bluetooth bonding keys
└── zadig.exe               ← Cached Zadig download (after "Install WinUSB…")
```

---

## License

[MIT](LICENSE)
