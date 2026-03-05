"""
backend.py – Bluetooth A2DP Sink Backend
=========================================
Encapsulates all Bluetooth and audio logic without any GUI dependency.
The GUI communicates exclusively via callbacks and the start() / stop() API.

Architecture overview
---------------------
┌─ SinkBackend ──────────────────────────────────────────────────────────┐
│  start()  → daemon thread → asyncio loop → _async_main()              │
│                                              ├─ launch btstack_sink.exe│
│                                              ├─ read stderr (events)   │
│                                              ├─ read stdout (SBC audio)│
│                                              └─ wait for stop event    │
│                                                                        │
│  stop()   → sends {"cmd":"stop"} → joins pipeline                     │
│                                                                        │
│  Callbacks (always called from the BT thread):                        │
│    on_state_change, on_device_connected, on_device_disconnected,       │
│    on_audio_level, on_log                                              │
│    → GUI must forward these to the Tk mainloop via root.after(0, …)   │
└────────────────────────────────────────────────────────────────────────┘

Audio pipeline
--------------
btstack_sink.exe stdout (length-prefixed SBC frames)
  → _stdout_audio_thread reads frames
  → SbcAudioPipeline.write_sbc() feeds FFmpeg subprocess
  → background reader thread fills a PCM queue
  → sounddevice OutputStream callback drains the queue in real time
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import queue
import subprocess
import sys
import threading
from enum import Enum, auto
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import sounddevice as sd

log = logging.getLogger("bt-sink.backend")

#: Suppress the FFmpeg/subprocess console window on Windows.
_POPEN_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


# ---------------------------------------------------------------------------
# Allowed-MAC persistence  (no BT stack dependency)
# ---------------------------------------------------------------------------

def _load_allowed_macs(path: Path) -> set:
    """Loads the set of previously allowed MAC addresses from disk."""
    try:
        if path.exists():
            with open(path, encoding="utf-8") as f:
                data = _json.load(f)
            if isinstance(data, list):
                return {str(m).upper() for m in data}
    except Exception as e:
        log.debug("Load allowed_macs: %s", e)
    return set()


def _save_allowed_macs(macs: set, path: Path) -> None:
    """Persists the allowed MAC set to disk."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            _json.dump(sorted(macs), f, indent=2)
    except Exception as e:
        log.debug("Save allowed_macs: %s", e)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class SinkState(Enum):
    """Lifecycle states of the SinkBackend."""
    IDLE = auto()       # Not started yet
    STARTING = auto()   # Thread launched, launching btstack_sink.exe
    READY = auto()      # BTstack powered on, discoverable, waiting for a source
    CONNECTED = auto()  # A Bluetooth source is connected and streaming
    ERROR = auto()      # Unrecoverable error (transport failure, etc.)
    STOPPED = auto()    # Cleanly stopped by the user


# ---------------------------------------------------------------------------
# SBC Audio Pipeline  (FFmpeg → sounddevice)
# ---------------------------------------------------------------------------

class SbcAudioPipeline:
    """
    Decodes a stream of SBC frames to PCM and plays it via sounddevice.

    Thread model
    ------------
    write_sbc()  is called from the btstack-audio reader thread.
    _pcm_reader_loop() runs in its own daemon thread.
    _audio_callback() is called by the sounddevice WASAPI thread.
    A bounded queue decouples the reader from the callback.
    """

    #: Number of stereo int16 frames delivered to sounddevice per callback.
    BLOCK_SIZE = 512

    def __init__(
        self,
        ffmpeg_exe: str = "ffmpeg",
        latency_ms: int = 150,
        device_index: Optional[int] = None,
        on_level: Optional[Callable[[float], None]] = None,
    ):
        self._ffmpeg_exe = ffmpeg_exe
        self._latency_ms = latency_ms
        self._device_index = device_index
        self._on_level = on_level

        # Inter-thread PCM queue.  Max size limits buffering to ~6 s at 44.1 kHz.
        self._pcm_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=500)

        self._ffmpeg: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._sd_stream: Optional[sd.OutputStream] = None
        self._lock = threading.Lock()  # Serialises write_sbc() calls
        self._active = False
        self._sample_rate = 44100
        self._channels = 2
        self._volume: float = 1.0  # Linear multiplier; 1.0 = unity, 2.0 = double

    def set_volume(self, volume: float) -> None:
        """Sets the output volume as a linear multiplier in [0.0, 2.0]."""
        self._volume = max(0.0, min(2.0, volume))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, sample_rate: int, channels: int) -> None:
        """
        Starts the FFmpeg subprocess, PCM reader thread, and sounddevice stream.
        Must only be called once; re-use requires creating a new instance.
        """
        if self._active:
            return
        self._sample_rate = sample_rate
        self._channels = channels
        log.info("Audio pipeline starting: %d Hz, %d ch", sample_rate, channels)

        self._ffmpeg = self._start_ffmpeg(sample_rate, channels)
        self._reader = self._start_reader_thread()
        self._sd_stream = self._start_sd_stream(sample_rate, channels)
        self._active = True

        out_dev = sd.query_devices(kind="output")
        log.info("Audio output: %s", out_dev["name"])

    def stop(self) -> None:
        """Stops the stream, drains FFmpeg, and frees all resources."""
        self._active = False

        if self._sd_stream:
            self._sd_stream.stop()
            self._sd_stream.close()
            self._sd_stream = None

        if self._ffmpeg:
            self._stop_ffmpeg()
            self._ffmpeg = None

        log.info("Audio pipeline stopped")

    def write_sbc(self, data: bytes) -> None:
        """
        Feeds raw SBC frame data into FFmpeg's stdin.
        Thread-safe; silently discards data if the pipeline is inactive
        or the pipe is broken.
        """
        if not self._active or not self._ffmpeg:
            return
        with self._lock:
            try:
                assert self._ffmpeg.stdin
                self._ffmpeg.stdin.write(data)
                self._ffmpeg.stdin.flush()
            except (BrokenPipeError, OSError, AssertionError):
                pass

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _start_ffmpeg(self, sample_rate: int, channels: int) -> subprocess.Popen:
        """Launches FFmpeg with SBC input and raw s16le PCM output via pipes."""
        return subprocess.Popen(
            [
                self._ffmpeg_exe,
                "-loglevel", "quiet",
                "-f", "sbc",        # Input format: raw SBC frames
                "-i", "pipe:0",
                "-f", "s16le",      # Output: signed 16-bit little-endian PCM
                "-ar", str(sample_rate),
                "-ac", str(channels),
                "pipe:1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=_POPEN_FLAGS,
        )

    def _start_reader_thread(self) -> threading.Thread:
        """Starts the background thread that reads PCM blocks from FFmpeg."""
        t = threading.Thread(
            target=self._pcm_reader_loop, daemon=True, name="pcm-reader"
        )
        t.start()
        return t

    def _start_sd_stream(self, sample_rate: int, channels: int) -> sd.OutputStream:
        """Creates and starts the sounddevice output stream."""
        kwargs: dict = dict(
            samplerate=sample_rate,
            channels=channels,
            dtype="int16",
            blocksize=self.BLOCK_SIZE,
            latency=self._latency_ms / 1000,  # sounddevice expects seconds
            callback=self._audio_callback,
        )
        if self._device_index is not None:
            kwargs["device"] = self._device_index
        stream = sd.OutputStream(**kwargs)
        stream.start()
        return stream

    def _stop_ffmpeg(self) -> None:
        """Closes FFmpeg's stdin and waits for it to exit (with a timeout)."""
        try:
            assert self._ffmpeg and self._ffmpeg.stdin
            self._ffmpeg.stdin.close()
            self._ffmpeg.wait(timeout=2)
        except Exception:
            if self._ffmpeg:
                self._ffmpeg.kill()

    def _pcm_reader_loop(self) -> None:
        """
        Background thread: reads fixed-size PCM blocks from FFmpeg's stdout
        and enqueues them for the sounddevice callback.

        Exits when FFmpeg's stdout closes (EOF) or raises an unexpected error.
        Pads the final short block with silence to keep the queue block-aligned.
        """
        bytes_per_block = self.BLOCK_SIZE * self._channels * 2  # int16 = 2 bytes
        assert self._ffmpeg and self._ffmpeg.stdout

        while True:
            try:
                raw = self._ffmpeg.stdout.read(bytes_per_block)
                if not raw:
                    break  # FFmpeg process exited / pipe closed

                arr = np.frombuffer(raw, dtype=np.int16)

                # Pad the last (potentially short) block with silence
                expected = self.BLOCK_SIZE * self._channels
                if len(arr) < expected:
                    pad = np.zeros(expected - len(arr), dtype=np.int16)
                    arr = np.concatenate([arr, pad])

                block = arr.reshape(self.BLOCK_SIZE, self._channels)
                self._pcm_q.put(block, timeout=0.5)

            except queue.Full:
                log.debug("PCM queue full – frames dropped")
            except Exception:
                break

    def _apply_volume(self, block: np.ndarray) -> np.ndarray:
        """
        Scales PCM samples by self._volume.
        Uses float32 arithmetic to avoid int16 overflow, then clips back.
        Skipped entirely at unity gain to avoid unnecessary allocations.
        """
        if self._volume == 1.0:
            return block
        scaled = block.astype(np.float32) * self._volume
        return np.clip(scaled, -32768, 32767).astype(np.int16)

    def _audio_callback(
        self, outdata: np.ndarray, frames: int, time_info, status
    ) -> None:
        """
        sounddevice output callback – called from the WASAPI thread.

        Drains one block from the PCM queue; fills with silence on underrun.
        Computes RMS level for the VU meter after applying volume.
        """
        if status:
            log.debug("sounddevice: %s", status)

        try:
            block = self._pcm_q.get_nowait()
            outdata[:] = self._apply_volume(block)
        except queue.Empty:
            outdata.fill(0)
            block = None  # Underrun – report zero level

        self._report_level(block)

    def _report_level(self, block: Optional[np.ndarray]) -> None:
        """Computes RMS of the current PCM block and forwards it to the GUI."""
        if not self._on_level:
            return
        if block is not None:
            rms = float(np.sqrt(np.mean(block.astype(np.float32) ** 2))) / 32768.0
        else:
            rms = 0.0
        try:
            self._on_level(rms)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# SinkBackend – public API consumed by the GUI
# ---------------------------------------------------------------------------

class SinkBackend:
    """
    Manages the full Bluetooth + audio lifecycle using btstack_sink.exe.

    Usage
    -----
    backend = SinkBackend(device_name="PC-AudioSink", transport="usb:0", ...)
    backend.start()   # returns immediately; work happens in a daemon thread
    backend.stop()    # blocks briefly to signal the asyncio loop
    """

    def __init__(
        self,
        device_name: str = "PC-AudioSink",
        bt_address: str = "F0:F1:F2:F3:F4:F5",
        transport: str = "usb:0",
        latency_ms: int = 50,
        max_bitpool: int = 53,
        volume: float = 1.0,
        audio_device_index: Optional[int] = None,
        ffmpeg_exe: str = "ffmpeg",
        debug: bool = False,
        keystore_path: Optional[str] = None,
        allowed_macs_path: Optional[str] = None,
        # Callbacks
        on_state_change: Optional[Callable[[SinkState], None]] = None,
        on_device_connected: Optional[Callable[[str, str], None]] = None,
        on_device_disconnected: Optional[Callable[[str], None]] = None,
        on_audio_level: Optional[Callable[[float], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        on_pairing_request: Optional[Callable] = None,
    ):
        # BT / USB parameters
        self._device_name = device_name
        self._bt_address = bt_address
        self._transport_str = transport
        self._max_bitpool = max_bitpool

        # Audio parameters
        self._latency_ms = latency_ms
        self._volume = volume
        self._audio_device_index = audio_device_index
        self._ffmpeg_exe = ffmpeg_exe

        # Feature flags
        self._debug = debug
        self._allowed_macs_path = Path(allowed_macs_path) if allowed_macs_path else None
        # keystore_path kept for API compatibility; BTstack manages its own bonding
        self._keystore_path = Path(keystore_path) if keystore_path else None

        # Allowed MACs: devices the user has previously approved with "Remember"
        self._allowed_macs: set = (
            _load_allowed_macs(self._allowed_macs_path)
            if self._allowed_macs_path else set()
        )

        # GUI callbacks
        self._cb_state = on_state_change
        self._cb_connected = on_device_connected
        self._cb_disconnected = on_device_disconnected
        self._cb_level = on_audio_level
        self._cb_log = on_log
        self._cb_pairing_request = on_pairing_request

        # Pairing control
        self._pairing_allowed = True
        self._remember_map: dict[str, bool] = {}  # addr_upper → persist key to disk

        # Runtime state – set during start()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._pipeline: Optional[SbcAudioPipeline] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._state = SinkState.IDLE
        self._btstack_proc: Optional[subprocess.Popen] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def state(self) -> SinkState:
        """Current backend state (thread-safe read; set only from BT thread)."""
        return self._state

    def set_volume(self, volume: float) -> None:
        """Updates the output volume (linear multiplier, 0.0–2.0)."""
        self._volume = max(0.0, min(2.0, volume))
        if self._pipeline:
            self._pipeline.set_volume(self._volume)

    def clear_allowed_macs(self) -> None:
        """Wipes the in-memory allowed-MAC set and its JSON file on disk."""
        self._allowed_macs.clear()
        if self._allowed_macs_path and self._allowed_macs_path.exists():
            try:
                self._allowed_macs_path.unlink()
                log.debug("allowed_macs.json deleted")
            except Exception as exc:
                log.debug("Could not delete allowed_macs: %s", exc)
        # BTstack bonding keys (TLV file) are managed inside btstack_sink.exe.
        # A future "clear_bonding_keys" command can be added to btstack_sink.exe.

    def set_pairing_mode(self, allowed: bool) -> None:
        """Allow (True) or block (False) pairing requests from unknown devices."""
        self._pairing_allowed = allowed
        self._send_btstack_cmd({"cmd": "set_discoverable", "enabled": allowed})

    def start(self) -> None:
        """Transitions to STARTING and launches the background daemon thread."""
        if self._state not in (SinkState.IDLE, SinkState.STOPPED, SinkState.ERROR):
            return
        self._set_state(SinkState.STARTING)
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="bt-backend"
        )
        self._thread.start()

    def stop(self) -> None:
        """Signals the asyncio loop to exit and stops the audio pipeline."""
        if self._loop and self._loop.is_running() and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        if self._pipeline:
            self._pipeline.stop()
            self._pipeline = None
        self._set_state(SinkState.STOPPED)

    # ------------------------------------------------------------------
    # Background thread / asyncio loop
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """Entry point for the daemon thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_event = asyncio.Event()
        try:
            self._loop.run_until_complete(self._async_main())
        except Exception as exc:
            self._log(f"Fatal error: {exc}")
            self._set_state(SinkState.ERROR)
        finally:
            self._loop.close()
            self._loop = None
            self._stop_event = None

    # ------------------------------------------------------------------
    # BTstack subprocess management
    # ------------------------------------------------------------------

    def _find_btstack_exe(self) -> Optional[str]:
        """Locates btstack_sink.exe relative to this script or in a PyInstaller bundle."""
        candidates = []

        # Development: btstack/build/btstack_sink.exe next to project root
        here = Path(__file__).resolve().parent.parent
        candidates.append(here / "btstack" / "build" / "btstack_sink.exe")

        # PyInstaller bundle: next to the running .exe
        if getattr(sys, "frozen", False):
            candidates.append(Path(sys.executable).parent / "btstack_sink.exe")

        # Same directory as this script (alternative bundle layout)
        candidates.append(Path(__file__).parent / "btstack_sink.exe")

        for p in candidates:
            if p.exists():
                return str(p)
        return None

    async def _async_main(self) -> None:
        """Launches btstack_sink.exe and drives the event loop until stop()."""
        exe = self._find_btstack_exe()
        if not exe:
            self._log("Error: btstack_sink.exe not found.")
            self._log("Build it first: cd btstack && .\\build.ps1")
            self._set_state(SinkState.ERROR)
            return

        # Parse USB index from "usb:N"
        usb_index = 0
        if self._transport_str.startswith("usb:"):
            try:
                usb_index = int(self._transport_str.split(":")[1])
            except (IndexError, ValueError):
                pass

        cmd = [
            exe,
            str(usb_index),
            self._device_name,
            self._bt_address,
            str(self._max_bitpool),
        ]
        self._log(f"Launching BTstack: {Path(exe).name} (usb:{usb_index})")

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=_POPEN_FLAGS,
            )
        except OSError as exc:
            self._log(f"Failed to launch btstack_sink.exe: {exc}")
            self._log("Possible causes:")
            self._log("  • btstack_sink.exe not found or not built")
            self._log("  • WinUSB driver (Zadig) not installed for the dongle")
            self._set_state(SinkState.ERROR)
            return

        self._btstack_proc = proc
        loop = asyncio.get_event_loop()

        # Thread: read stderr events → dispatch to asyncio loop
        t_events = threading.Thread(
            target=self._stderr_reader_thread,
            args=(proc.stderr, loop),
            daemon=True,
            name="btstack-events",
        )
        t_events.start()

        # Thread: read stdout SBC frames → feed to audio pipeline
        t_audio = threading.Thread(
            target=self._stdout_audio_thread,
            args=(proc.stdout,),
            daemon=True,
            name="btstack-audio",
        )
        t_audio.start()

        # Block until stop() signals the event
        assert self._stop_event is not None
        await self._stop_event.wait()

        # Graceful shutdown
        self._send_btstack_cmd({"cmd": "stop"})
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()

        self._btstack_proc = None

    def _stderr_reader_thread(
        self, stderr_pipe, loop: asyncio.AbstractEventLoop
    ) -> None:
        """Reads JSON event lines from btstack_sink.exe stderr; dispatches to asyncio loop."""
        for raw_line in stderr_pipe:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = _json.loads(line)
            except _json.JSONDecodeError:
                loop.call_soon_threadsafe(self._log, f"[btstack] {line}")
                continue
            loop.call_soon_threadsafe(self._on_btstack_event, event)

    def _stdout_audio_thread(self, stdout_pipe) -> None:
        """
        Reads length-prefixed SBC frames from btstack_sink.exe stdout.
        Frame format: [uint32_le length][SBC payload bytes]
        Feeds payload bytes to the active audio pipeline.
        """
        while True:
            header = stdout_pipe.read(4)
            if not header or len(header) < 4:
                break
            frame_len = int.from_bytes(header, "little")
            if frame_len == 0 or frame_len > 65536:
                continue  # Sanity check; skip malformed frames
            data = b""
            remaining = frame_len
            while remaining > 0:
                chunk = stdout_pipe.read(remaining)
                if not chunk:
                    return
                data += chunk
                remaining -= len(chunk)
            if self._pipeline:
                self._pipeline.write_sbc(data)

    def _send_btstack_cmd(self, cmd: dict) -> None:
        """Sends a JSON command line to btstack_sink.exe via stdin."""
        proc = self._btstack_proc
        if proc and proc.stdin and proc.poll() is None:
            try:
                line = (_json.dumps(cmd) + "\n").encode("utf-8")
                proc.stdin.write(line)
                proc.stdin.flush()
            except (OSError, BrokenPipeError):
                pass

    # ------------------------------------------------------------------
    # BTstack event handling  (always called from asyncio loop thread)
    # ------------------------------------------------------------------

    def _on_btstack_event(self, event: dict) -> None:
        """Routes events from btstack_sink.exe to the appropriate handler."""
        evt = event.get("event", "")

        if evt == "ready":
            addr = event.get("address", "")
            self._log(f"BTstack ready! Address: {addr}")
            self._set_state(SinkState.READY)
            # Apply current discoverability setting
            self._send_btstack_cmd(
                {"cmd": "set_discoverable", "enabled": self._pairing_allowed}
            )

        elif evt == "l2cap_request":
            # iPhone / Switch is requesting an AVDTP connection.
            # This fires BEFORE L2CAP is accepted — the key BTstack advantage.
            addr = event.get("addr", "").upper()
            cid = event.get("cid", 0)
            asyncio.ensure_future(self._handle_l2cap_request(addr, cid))

        elif evt == "connected":
            addr = event.get("addr", "").upper()
            name = event.get("name", addr)
            self._log(f"A2DP connected: {name} ({addr})")
            self._set_state(SinkState.CONNECTED)
            if self._cb_connected:
                self._cb_connected(name, addr)

        elif evt == "audio_start":
            sample_rate = event.get("sample_rate", 44100)
            channels = event.get("channels", 2)
            self._log(f"Stream START → {sample_rate} Hz, {channels} ch")
            self._start_audio_pipeline(sample_rate, channels)

        elif evt == "audio_stop":
            self._log("Stream STOP")
            if self._pipeline:
                self._pipeline.stop()
                self._pipeline = None

        elif evt == "disconnected":
            addr = event.get("addr", "").upper()
            name = event.get("name", addr)
            self._log(f"A2DP disconnected: {name}")
            should_remember = self._remember_map.pop(addr, True)
            if not should_remember:
                # "Allow once" — remove from allowed set so dialog shows again
                self._allowed_macs.discard(addr)
                if self._allowed_macs_path:
                    _save_allowed_macs(self._allowed_macs, self._allowed_macs_path)
            if self._pipeline:
                self._pipeline.stop()
                self._pipeline = None
            self._set_state(SinkState.READY)
            if self._cb_disconnected:
                self._cb_disconnected(name)

        elif evt == "log":
            self._log(f"[btstack] {event.get('msg', '')}")

        elif evt == "error":
            self._log(f"[btstack error] {event.get('msg', '')}")
            self._set_state(SinkState.ERROR)

    async def _handle_l2cap_request(self, addr_upper: str, cid: int) -> None:
        """
        Gate an incoming AVDTP L2CAP connection on user approval.

        Known device (in _allowed_macs) → auto-approve, no dialog.
        Unknown device + pairing disabled → auto-deny.
        Unknown device + pairing enabled → show GUI dialog, wait for answer.
        """
        if addr_upper in self._allowed_macs:
            self._log(f"AVDTP: auto-approving known device {addr_upper}")
            self._send_btstack_cmd(
                {"cmd": "approve", "addr": addr_upper, "cid": cid}
            )
            return

        if not self._pairing_allowed:
            self._log(f"AVDTP: rejecting unknown device (pairing off): {addr_upper}")
            self._send_btstack_cmd(
                {"cmd": "deny", "addr": addr_upper, "cid": cid}
            )
            return

        # Unknown device + pairing allowed → show dialog
        self._log(f"AVDTP connection from unknown device: {addr_upper}")

        if not self._cb_pairing_request:
            self._send_btstack_cmd(
                {"cmd": "deny", "addr": addr_upper, "cid": cid}
            )
            return

        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()

        def resolve(approved: bool, remember: bool) -> None:
            if not future.done():
                loop.call_soon_threadsafe(future.set_result, (approved, remember))

        self._cb_pairing_request(addr_upper, addr_upper, resolve)

        try:
            approved, remember = await asyncio.wait_for(future, timeout=30.0)
        except asyncio.TimeoutError:
            approved, remember = False, False

        if approved:
            self._remember_map[addr_upper] = remember
            self._allowed_macs.add(addr_upper)
            if remember and self._allowed_macs_path:
                _save_allowed_macs(self._allowed_macs, self._allowed_macs_path)
            self._send_btstack_cmd(
                {"cmd": "approve", "addr": addr_upper, "cid": cid}
            )
        else:
            self._log(f"AVDTP connection denied: {addr_upper}")
            self._send_btstack_cmd(
                {"cmd": "deny", "addr": addr_upper, "cid": cid}
            )

    def _start_audio_pipeline(self, sample_rate: int, channels: int) -> None:
        """Creates (or replaces) the SBC audio pipeline."""
        if self._pipeline:
            self._pipeline.stop()
            self._pipeline = None
        pipeline = SbcAudioPipeline(
            ffmpeg_exe=self._ffmpeg_exe,
            latency_ms=self._latency_ms,
            device_index=self._audio_device_index,
            on_level=self._cb_level,
        )
        pipeline.set_volume(self._volume)
        try:
            pipeline.start(sample_rate, channels)
            self._log("Audio pipeline started")
        except Exception as exc:
            self._log(f"Pipeline error: {exc}")
            return
        self._pipeline = pipeline

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _set_state(self, state: SinkState) -> None:
        """Updates internal state and fires the on_state_change callback."""
        self._state = state
        if self._cb_state:
            try:
                self._cb_state(state)
            except Exception:
                pass

    def _log(self, msg: str) -> None:
        """Logs to the Python logger and forwards to the GUI callback."""
        log.info(msg)
        if self._cb_log:
            try:
                self._cb_log(msg)
            except Exception:
                pass
