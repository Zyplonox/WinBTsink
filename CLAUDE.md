# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

WinBTsink turns a USB Bluetooth dongle into an A2DP audio sink on Windows, bypassing the Windows Bluetooth stack. It is a three-process pipeline: a C engine built on BTstack talks to the dongle over WinUSB, a Python backend decodes audio through an FFmpeg subprocess and plays it via sounddevice (WASAPI), and a CustomTkinter GUI drives the backend. Up to 4 sources stream simultaneously. Windows-only. The app, exe and AppData folder are named "BT-AudioSink"; WinBTsink is the project name.

## Commands

There is no test suite and no linter configured. Verification is manual: build, run, pair a device. `python -m pyflakes src/*.py` is a cheap sanity check.

```powershell
# Build the C engine (finds or installs MSYS2/MinGW, clones BTstack v1.6.1 into
# btstack/btstack-src, applies btstack/patches/apply_patches.py, runs CMake).
# Output: btstack\build\btstack_sink.exe
powershell -ExecutionPolicy Bypass -File .\btstack\build.ps1     # -Force re-clones BTstack

# Same from an MSYS2/Git Bash shell when the toolchain is already installed
bash btstack/do_build.sh

# Install Python deps (one-time)
python -m pip install -r requirements.txt

# Run from source
python src\gui.py
python src\headless.py            # no window; HTTP API on 127.0.0.1:8765 (see src/api_server.py)

# Full release build: C engine + pip + PyInstaller -> dist\BT-AudioSink.exe
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

MSYS2 is looked up via `MSYS2_ROOT`, `C:\msys64`, `C:\tools\msys64`. The C build needs `btstack-src` present and patched; CMake fails fast with a pointer to `btstack/build.ps1` if it is missing. Changing the BTstack pin in `btstack/build.ps1` (and `do_build.sh`) triggers a re-clone automatically.

## Architecture

### Modules

`src/config.py` holds `Settings` (persisted to `%APPDATA%\BT-AudioSink\config.json`, typed loading, `backend_kwargs()`), the data-file paths, the WASAPI output-device list and the autostart registry helpers; it imports no Tk so `headless.py` can use it. `device_store.py` is the remembered-device list shared by GUI and backend. `api_server.py` is the HTTP API; `BackendController` adapts a backend to it and is used by both the GUI and the headless runner. `media_keys.py` registers global media hotkeys on its own message-loop thread.

### Process topology and IPC

```
gui.py (Tk mainloop) or headless.py
  └─ SinkBackend (src/backend.py) — plain threads, no asyncio
       ├─ btstack_sink.exe  (btstack/btstack_sink.c)   spawned via subprocess.Popen
       │     argv   : <usb_filter> <device_name> <max_bitpool> <debug> <cod_hex> <keystore_path>
       │              <sbc_block_length> <sbc_subbands> <sbc_allocation> <vendor_codecs>
       │     stdin  : JSON command lines  (approve / deny / set_discoverable / set_volume / player /
       │              forget_key / connect / disconnect / stop)
       │     stderr : JSON event lines    (ready / l2cap_request / connected / name / audio_start /
       │              playback / metadata / stats / connect_failed / ...)
       │     stdout : BINARY audio frames [u32le len][6-byte bd_addr][SBC or AAC payload]
       └─ AudioPipeline (one per connected device)
             ffmpeg subprocess (stdin = raw codec frames, stdout = s16le PCM)
             → PCM queue → sounddevice OutputStream callback
```

The full contract is the header comment at the top of `btstack/btstack_sink.c`. Any change to an event, command or argv slot must be mirrored in `SinkBackend._on_btstack_event` / `_launch` in `src/backend.py` and in `process_command` / `main` in the C file. Stdout is binary-only: never `printf` to stdout in the C engine, use `emit_event` / `emit_log` (stderr). All string values in events go through `json_escape`.

Backend threads: `bt-events` (reads stderr, dispatches events, so handlers are serialized), `bt-audio` (reads stdout frames), `bt-watch` (waits for the process and reports a crash as ERROR). Closing the engine's stdin is enough to make it power off and exit; `stop()` sends the stop command, closes stdin, waits 3 s, then kills. A `SinkBackend` is single-use; the GUI creates a fresh one per Start.

Backend → GUI communication is callbacks only, fired on backend threads. `App._ui(gen, fn)` in `src/gui.py` wraps them onto the Tk mainloop and drops callbacks whose backend generation is stale (late events after Stop). The audio level is the exception: the sounddevice thread only stores a float that `_poll_level` reads every 50 ms, so the real-time thread never blocks on Tk. Keep both patterns for new callbacks.

### Dongle selection

`src/usb_devices.py` enumerates attached USB Bluetooth adapters through SetupAPI (ctypes) and reports the driver each one uses. The GUI lists the WinUSB ones and passes `UsbDevice.path_filter` (a lower-cased fragment of the device instance id such as `vid_0a12&pid_0001#5&2c1f8b6&0&3#`) as argv[1]. The `winusb` patch adds `hci_transport_usb_set_path_filter()` to BTstack's WinUSB transport so it only opens a device whose device path contains that fragment. The "Install WinUSB" dialog uses the same enumeration to show dongles that are still on the inbox driver.

### The BTstack patches (`btstack/patches/apply_patches.py`)

Stock BTstack accepts incoming AVDTP (PSM 25) and AVRCP (PSM 23) L2CAP connections immediately. The script rewrites `avdtp.c`/`avrcp.c` in the cloned source to add a deferred-accept hook (`*_register_incoming_connection_handler`, `*_accept_incoming_connection`, `*_decline_incoming_connection`), plus the WinUSB path filter above. Each patch is guarded by marker comments (globals and call-site), is only written when every anchor matched, keeps a `.orig` backup, and refuses to touch a half-patched file. If BTstack is bumped from v1.6.1, re-check the regex anchors.

### Connection approval flow

1. Patched `avdtp.c` calls `on_avdtp_incoming_connection` in the C engine before L2CAP accept. If the address already has an active `a2dp_conn_t` the connection is the media channel and is auto-accepted (strict sources like Switch 2 time out on any delay). Otherwise it is parked in `g_pending[]` and an `l2cap_request` event is emitted.
2. `SinkBackend._handle_l2cap_request` decides: remembered MAC in `allowed_macs.json` or allowed-once this session → approve; pairing toggle off → deny; else call `on_pairing_request(addr, resolve)`. The GUI dialog auto-denies after 30 s; the backend has a 35 s safety timer. Retries from the same address while the dialog is open are merged into the one question. "Allow once" lasts until the device disconnects (or 60 s if it never connects).
3. `approve`/`deny` resolve the parked AVDTP cid. AVRCP is gated on the same decision through `g_pending_avrcp[]` (valid=1 parked, valid=2 pre-approved with a 15 s TTL). AVRCP channels that establish before or outlive A2DP live in `g_early_avrcp[]` and are promoted into the connection slot when A2DP (re)connects.

Link keys (bonding) live in `btstack_keys.db` in `%APPDATA%\BT-AudioSink`, managed by BTstack's TLV store; the path is passed as argv[6] because the exe itself runs from PyInstaller's temp dir. The Python side owns `allowed_macs.json`. SSP confirmation is auto-accepted and legacy PIN is `0000`; Secure Connections is not used, for Switch compatibility.

### C engine threading

BTstack is single-threaded on its Windows run loop. A dedicated Windows thread reads stdin lines into a ring buffer and signals a manual-reset event registered as a run-loop data source; the callback resets the event *before* draining. `process_command` therefore always runs on the run-loop thread. Do not call BTstack APIs from the reader thread. Stdin EOF enqueues a synthetic stop command.

### Runtime file locations

- `%APPDATA%\BT-AudioSink\config.json`, `allowed_macs.json`, `btstack_keys.db`, cached `zadig.exe` (paths built in `src/gui.py`)
- `_find_btstack_exe` in `src/backend.py` and `_get_ffmpeg` in `src/gui.py` handle PyInstaller (`sys._MEIPASS`) vs source-tree layouts. `BT-AudioSink.spec` bundles `btstack_sink.exe` and a single `ffmpeg.exe` (the duplicate that imageio-ffmpeg's hook would add is filtered out).
