"""
backend.py – Bluetooth A2DP Sink Backend
=========================================
Encapsulates all Bluetooth and audio logic without any GUI dependency.
The GUI communicates exclusively via callbacks and the start() / stop() API.

Architecture overview
---------------------
┌─ SinkBackend ──────────────────────────────────────────────────────────┐
│  start()  → launches btstack_sink.exe and three daemon threads:        │
│               bt-events  reads stderr JSON lines → _on_btstack_event   │
│               bt-audio   reads stdout audio frames → AudioPipeline     │
│               bt-watch   waits for the process, reports a crash        │
│  stop()   → sends {"cmd":"stop"}, closes stdin, waits, kills if needed │
│                                                                        │
│  A SinkBackend instance is single-use: start() once, stop() once.      │
│                                                                        │
│  Callbacks fire on the bt-events thread (audio level: on the           │
│  sounddevice thread). The GUI must marshal them to its own thread.    │
└────────────────────────────────────────────────────────────────────────┘

Audio pipeline
--------------
btstack_sink.exe stdout (length-prefixed frames, SBC or AAC-LATM, tagged
with the source address)
  → bt-audio thread routes each frame to the device's AudioPipeline
  → AudioPipeline.write_audio() feeds an FFmpeg subprocess (decode to PCM)
  → pcm-reader thread fills a bounded queue
  → sounddevice OutputStream callback drains the queue in real time

IPC contract with btstack_sink.exe: see the header of btstack/btstack_sink.c.
"""

from __future__ import annotations

import json as _json
import logging
import queue
import subprocess
import sys
import threading
import time
from enum import Enum, auto
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import sounddevice as sd

from device_store import DeviceStore

log = logging.getLogger("bt-sink.backend")

#: Suppress the FFmpeg/subprocess console window on Windows.
_POPEN_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

#: Seconds the backend waits for the GUI to answer a pairing request before
#: it denies on its own. The GUI dialog auto-denies earlier (30 s); this is
#: only the safety net for a GUI that never answers.
PAIRING_TIMEOUT_S = 35

#: "Allow once" approvals stay valid this long if the device never actually
#: connects, so a phone that gives up mid-handshake is asked again later.
SESSION_ALLOW_TTL_S = 60


class SinkState(Enum):
    """Lifecycle states of the SinkBackend."""
    IDLE = auto()       # Not started yet
    STARTING = auto()   # Launching btstack_sink.exe
    READY = auto()      # BTstack powered on, waiting for a source
    CONNECTED = auto()  # At least one Bluetooth source is connected
    ERROR = auto()      # Unrecoverable error (transport failure, crash, ...)
    STOPPED = auto()    # Cleanly stopped by the user


# ---------------------------------------------------------------------------
# Audio Pipeline  (FFmpeg → sounddevice)  supports SBC and AAC
# ---------------------------------------------------------------------------

class AudioPipeline:
    """
    Decodes a stream of audio frames (SBC or AAC-LATM) to PCM via FFmpeg
    and plays it via sounddevice.

    Thread model
    ------------
    write_audio() is called from the bt-audio reader thread.
    _pcm_reader_loop() runs in its own daemon thread.
    _audio_callback() is called by the sounddevice WASAPI thread.
    A bounded queue decouples the reader from the callback.
    """

    #: Number of frames delivered to sounddevice per callback.
    BLOCK_SIZE = 512

    def __init__(
        self,
        codec: str = "sbc",         # "sbc" or "aac"
        ffmpeg_exe: str = "ffmpeg",
        latency_ms: int = 150,
        device_index: Optional[int] = None,
        on_level: Optional[Callable[[float], None]] = None,
    ):
        self._codec = codec
        self._ffmpeg_exe = ffmpeg_exe
        self._latency_ms = latency_ms
        self._device_index = device_index
        self._on_level = on_level

        # Inter-thread PCM queue.  Max size limits buffering to ~6 s at 44.1 kHz.
        self._pcm_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=500)

        self._ffmpeg: Optional[subprocess.Popen] = None
        self._sd_stream: Optional[sd.OutputStream] = None
        self._lock = threading.Lock()  # Guards _ffmpeg / _active across threads
        self._active = False
        self._sample_rate = 44100
        self._channels = 2
        self._volume: float = 1.0  # Linear multiplier; 1.0 = unity, 2.0 = double

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def channels(self) -> int:
        return self._channels

    def set_volume(self, volume: float) -> None:
        """Sets the output volume as a linear multiplier in [0.0, 2.0]."""
        self._volume = max(0.0, min(2.0, volume))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, sample_rate: int, channels: int) -> None:
        """
        Starts FFmpeg, the sounddevice stream and the PCM reader thread.
        Raises on failure and leaves nothing running.
        Must only be called once; re-use requires creating a new instance.
        """
        if self._active:
            return
        self._sample_rate = sample_rate
        self._channels = channels
        log.info("Audio pipeline starting: %d Hz, %d ch, codec=%s",
                 sample_rate, channels, self._codec)

        ffmpeg = self._start_ffmpeg(sample_rate, channels)
        try:
            stream = self._start_sd_stream(sample_rate, channels)
        except Exception:
            self._close_ffmpeg(ffmpeg, graceful=False)
            raise

        with self._lock:
            self._ffmpeg = ffmpeg
            self._sd_stream = stream
            self._active = True
        threading.Thread(target=self._pcm_reader_loop, daemon=True,
                         name="pcm-reader").start()
        log.info("Audio output: %s", self._output_device_name())

    def stop(self) -> None:
        """Stops the stream, drains FFmpeg, and frees all resources."""
        with self._lock:
            if not self._active:
                return
            self._active = False
            ffmpeg, self._ffmpeg = self._ffmpeg, None
            stream, self._sd_stream = self._sd_stream, None

        if stream:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:
                log.debug("sounddevice stream close: %s", exc)

        if ffmpeg:
            self._close_ffmpeg(ffmpeg, graceful=True)
        self._report_level(None)   # VU meter back to zero
        log.info("Audio pipeline stopped")

    def write_audio(self, data: bytes) -> None:
        """
        Feeds raw audio frame data (SBC or AAC-LATM) into FFmpeg's stdin.
        Called from the single bt-audio thread. The pipe write happens outside
        the lock: it can block when FFmpeg stalls, and stop() must still be
        able to kill FFmpeg (which unblocks the write with an OSError).
        """
        with self._lock:
            ffmpeg = self._ffmpeg if self._active else None
        if not ffmpeg or not ffmpeg.stdin:
            return
        try:
            ffmpeg.stdin.write(data)
            ffmpeg.stdin.flush()
        except (OSError, ValueError):
            pass  # BrokenPipe (FFmpeg gone) or closed file during stop()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _output_device_name(self) -> str:
        try:
            if self._device_index is None:
                return sd.query_devices(kind="output")["name"]  # type: ignore[index]
            return sd.query_devices(self._device_index)["name"]  # type: ignore[index]
        except Exception:
            return "unknown"

    #: FFmpeg demuxer per Bluetooth codec. aptX streams are raw sample data,
    #: so the demuxer has to be told the negotiated rate.
    _INPUT_ARGS = {
        "sbc":     ["-f", "sbc"],
        "aac":     ["-f", "latm"],
        "aptx":    ["-f", "aptx", "-sample_rate", "{rate}"],
        "aptx_hd": ["-f", "aptx_hd", "-sample_rate", "{rate}"],
    }

    def _start_ffmpeg(self, sample_rate: int, channels: int) -> subprocess.Popen:
        """Launches FFmpeg with codec-appropriate input and raw s16le PCM output via pipes."""
        input_args = [a.format(rate=sample_rate)
                      for a in self._INPUT_ARGS.get(self._codec, self._INPUT_ARGS["sbc"])]
        return subprocess.Popen(
            [
                self._ffmpeg_exe,
                "-loglevel", "quiet",
                *input_args,
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
        try:
            stream.start()
        except Exception:
            stream.close()
            raise
        return stream

    @staticmethod
    def _close_ffmpeg(ffmpeg: subprocess.Popen, graceful: bool) -> None:
        """Ends FFmpeg (EOF on stdin, then kill) and closes its pipes."""
        try:
            if graceful:
                if ffmpeg.stdin:
                    ffmpeg.stdin.close()
                ffmpeg.wait(timeout=2)
            else:
                ffmpeg.kill()
        except Exception:
            ffmpeg.kill()
        for pipe in (ffmpeg.stdin, ffmpeg.stdout):
            try:
                if pipe:
                    pipe.close()
            except OSError:
                pass

    def _pcm_reader_loop(self) -> None:
        """
        Background thread: reads fixed-size PCM blocks from FFmpeg's stdout
        and enqueues them for the sounddevice callback.

        Exits when FFmpeg's stdout closes (EOF) or raises an unexpected error.
        Pads the final short block with silence to keep the queue block-aligned.
        Never blocks on a full queue: FFmpeg must always be drained, otherwise
        it stops reading its stdin and the whole audio path backs up.
        """
        with self._lock:
            ffmpeg = self._ffmpeg
        if not ffmpeg or not ffmpeg.stdout:
            return
        expected = self.BLOCK_SIZE * self._channels
        bytes_per_block = expected * 2  # int16

        while True:
            try:
                raw = ffmpeg.stdout.read(bytes_per_block)
            except Exception:
                break
            if not raw:
                break  # FFmpeg process exited / pipe closed

            arr = np.frombuffer(raw, dtype=np.int16)
            if len(arr) < expected:
                arr = np.concatenate([arr, np.zeros(expected - len(arr), dtype=np.int16)])
            block = arr.reshape(self.BLOCK_SIZE, self._channels)
            try:
                self._pcm_q.put_nowait(block)
            except queue.Full:
                # Output device stalled: drop the oldest block, keep the newest
                try:
                    self._pcm_q.get_nowait()
                    self._pcm_q.put_nowait(block)
                except (queue.Empty, queue.Full):
                    pass

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
        Reports the RMS level of what is actually played (after volume).
        """
        if status:
            log.debug("sounddevice: %s", status)

        try:
            block = self._apply_volume(self._pcm_q.get_nowait())
            outdata[:] = block
        except queue.Empty:
            outdata.fill(0)
            block = None  # Underrun – report zero level

        self._report_level(block)

    def _report_level(self, block: Optional[np.ndarray]) -> None:
        """Computes RMS of the played PCM block and forwards it to the GUI."""
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
# Pairing approval bookkeeping
# ---------------------------------------------------------------------------

class _PendingApproval:
    """One open pairing question for a remote address (may cover several cids)."""

    def __init__(self, addr: str, cid: int):
        self.addr = addr
        self.cids = [cid]
        self.timer: Optional[threading.Timer] = None


# ---------------------------------------------------------------------------
# SinkBackend – public API consumed by the GUI
# ---------------------------------------------------------------------------

class SinkBackend:
    """
    Manages the full Bluetooth + audio lifecycle using btstack_sink.exe.

    Usage
    -----
    backend = SinkBackend(device_name="PC-AudioSink", usb_filter="vid_0a12&…", ...)
    backend.start()   # returns immediately; work happens on daemon threads
    backend.stop()    # blocks up to a few seconds while the engine shuts down

    An instance cannot be restarted; create a new one after stop().
    """

    def __init__(
        self,
        device_name: str = "PC-AudioSink",
        usb_filter: str = "",
        latency_ms: int = 50,
        max_bitpool: int = 53,
        volume: float = 1.0,
        audio_device_index: Optional[int] = None,
        ffmpeg_exe: str = "ffmpeg",
        debug: bool = False,
        keystore_path: Optional[str] = None,
        device_store: Optional[DeviceStore] = None,   # remembered devices (shared with the GUI)
        discoverable_timeout_s: int = 0,
        class_of_device: int = 0x240418,
        sbc_block_length: int = 0,          # 4/8/12/16, 0 = let the source choose
        sbc_subbands: int = 0,              # 4/8, 0 = let the source choose
        sbc_allocation: str = "auto",       # "loudness", "snr" or "auto"
        offer_aptx: bool = False,           # also advertise aptX and aptX HD endpoints
        multi_device_mode: str = "mix",     # "mix" | "duck" | "solo" (see _policy_gain)
        duck_level: float = 0.25,           # gain for background devices in "duck" mode
        # Callbacks
        on_state_change: Optional[Callable[[SinkState], None]] = None,
        on_device_connected: Optional[Callable[[str], None]] = None,          # addr
        on_device_disconnected: Optional[Callable[[str], None]] = None,       # addr
        on_device_name: Optional[Callable[[str, str], None]] = None,          # addr, name
        on_audio_level: Optional[Callable[[float], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        on_pairing_request: Optional[Callable[[str, Callable], None]] = None, # addr, resolve(approved, remember)
        on_volume_changed: Optional[Callable[[str, int], None]] = None,       # addr, vol_0_127
        on_metadata: Optional[Callable[[str, dict], None]] = None,            # addr, {title,artist,album}
        on_audio_start: Optional[Callable[[str, str, dict], None]] = None,    # addr, codec, stream info
        on_pairing_timeout: Optional[Callable[[], None]] = None,
        on_playback_status: Optional[Callable[[str, str], None]] = None,      # addr, playing|paused|stopped|...
        on_connect_failed: Optional[Callable[[str], None]] = None,            # addr (outgoing connect)
    ):
        # BT / USB parameters
        self._device_name = device_name
        self._usb_filter = usb_filter
        self._max_bitpool = max_bitpool
        self._class_of_device = class_of_device
        self._keystore_path = keystore_path
        self._sbc_block_length = sbc_block_length if sbc_block_length in (4, 8, 12, 16) else 0
        self._sbc_subbands = sbc_subbands if sbc_subbands in (4, 8) else 0
        self._sbc_allocation = {"loudness": 1, "snr": 2}.get(sbc_allocation, 0)
        self._vendor_codecs = 3 if offer_aptx else 0   # bit 1 aptX, bit 2 aptX HD
        self._multi_mode = multi_device_mode if multi_device_mode in ("mix", "duck", "solo") else "mix"
        self._duck_level = max(0.0, min(1.0, duck_level))
        self._stream_order: list[str] = []             # streaming devices, most recent last
        self._connect_queue: list[str] = []            # outgoing connects, one at a time
        self._connecting: Optional[str] = None

        # Audio parameters
        self._latency_ms = latency_ms
        self._volume = max(0.0, min(2.0, volume))
        self._audio_device_index = audio_device_index
        self._ffmpeg_exe = ffmpeg_exe

        # GUI callbacks (assigned first: _log() below needs them)
        self._cb_state = on_state_change
        self._cb_connected = on_device_connected
        self._cb_disconnected = on_device_disconnected
        self._cb_device_name = on_device_name
        self._cb_level = on_audio_level
        self._cb_log = on_log
        self._cb_pairing_request = on_pairing_request
        self._cb_volume_changed = on_volume_changed
        self._cb_metadata = on_metadata
        self._cb_audio_start = on_audio_start
        self._cb_pairing_timeout = on_pairing_timeout
        self._cb_playback_status = on_playback_status
        self._cb_connect_failed = on_connect_failed

        # Feature flags / persistence
        self._debug = debug
        self._store = device_store if device_store is not None else DeviceStore(None)
        if self._store.load_error:
            self._log(f"Could not read remembered devices ({self._store.load_error}); starting with none")

        # Pairing / discoverability control
        self._pairing_allowed = True
        self._discoverable_timeout_s = discoverable_timeout_s
        self._discoverable_timer: Optional[threading.Timer] = None
        self._discoverable_timer_id = 0
        self._pending: dict[str, _PendingApproval] = {}    # addr → open pairing question
        self._session_allowed: dict[str, float] = {}       # "allow once" addr → approval time

        # Runtime state
        self._lock = threading.RLock()                     # guards everything below
        self._started = False
        self._stopping = False
        self._state = SinkState.IDLE
        self._proc: Optional[subprocess.Popen] = None
        self._cmd_lock = threading.Lock()                  # serialises stdin writes
        self._threads: list[threading.Thread] = []
        self._pipelines: dict[str, AudioPipeline] = {}     # addr → pipeline
        self._pipeline_swap = threading.Lock()             # serialises pipeline replacement
        self._streaming: set[str] = set()                  # addrs between audio_start/stop
        self._connected_addrs: set[str] = set()
        self._codec_types: dict[str, str] = {}             # addr → "sbc" or "aac"
        self._device_audio_routes: dict[str, Optional[int]] = {}  # addr → sd device index
        self._device_volumes: dict[str, float] = {}        # addr → per-device gain
        self._device_muted: set[str] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def state(self) -> SinkState:
        return self._state

    # ---- volume: master gain × per-device gain × mute ----------------

    def set_volume(self, volume: float) -> None:
        """Updates the master output volume (linear multiplier, 0.0–2.0)."""
        self._volume = max(0.0, min(2.0, volume))
        self._apply_gains()

    def set_device_volume(self, addr: str, volume: float) -> None:
        """Per-device gain (0.0–2.0), multiplied with the master volume."""
        addr = addr.upper()
        with self._lock:
            self._device_volumes[addr] = max(0.0, min(2.0, volume))
        self._apply_gains(addr)

    def set_device_mute(self, addr: str, muted: bool) -> None:
        addr = addr.upper()
        with self._lock:
            if muted:
                self._device_muted.add(addr)
            else:
                self._device_muted.discard(addr)
        self._apply_gains(addr)

    def device_volume(self, addr: str) -> float:
        with self._lock:
            return self._device_volumes.get(addr.upper(), 1.0)

    def _effective_gain(self, addr: str) -> float:
        """Caller holds self._lock."""
        if addr in self._device_muted:
            return 0.0
        return self._volume * self._device_volumes.get(addr, 1.0) * self._policy_gain(addr)

    def _policy_gain(self, addr: str) -> float:
        """
        Multi-device policy (caller holds self._lock). The device whose stream
        started most recently is the foreground device.
          mix   every device plays at full gain (Windows mixes them)
          duck  background devices are reduced to duck_level while the
                foreground device streams
          solo  only the foreground device is audible
        """
        if self._multi_mode == "mix" or addr not in self._stream_order:
            return 1.0   # not streaming: nothing to attenuate, re-evaluated on start
        if addr == self._stream_order[-1]:
            return 1.0
        return self._duck_level if self._multi_mode == "duck" else 0.0

    def _note_stream_started(self, addr: str) -> None:
        with self._lock:
            if addr in self._stream_order:
                self._stream_order.remove(addr)
            self._stream_order.append(addr)
        self._apply_gains()

    def _note_stream_stopped(self, addr: str) -> None:
        with self._lock:
            if addr in self._stream_order:
                self._stream_order.remove(addr)
        self._apply_gains()

    def _apply_gains(self, only_addr: Optional[str] = None) -> None:
        with self._lock:
            targets = [(a, p, self._effective_gain(a)) for a, p in self._pipelines.items()
                       if only_addr is None or a == only_addr]
        for _, pipeline, gain in targets:
            pipeline.set_volume(gain)

    def notify_volume_changed(self, vol_0_127: int, addr: str = "") -> None:
        """
        Tell a source (or all sources when addr is empty) our volume via
        AVRCP absolute volume, so the phone's own slider follows.
        """
        cmd = {"cmd": "set_volume", "volume": int(max(0, min(127, vol_0_127)))}
        if addr:
            cmd["addr"] = addr.upper()
        self._send_cmd(cmd)

    # ---- sink-initiated connections -----------------------------------

    def connect_device(self, addr: str) -> None:
        """
        Connects to a (remembered) source from our side. Requests are queued
        and issued one at a time; the next starts when the previous one
        succeeded or failed.
        """
        addr = addr.upper()
        with self._lock:
            if addr in self._connected_addrs or addr == self._connecting or addr in self._connect_queue:
                return
            self._connect_queue.append(addr)
        self._connect_next()

    def disconnect_device(self, addr: str) -> None:
        self._send_cmd({"cmd": "disconnect", "addr": addr.upper()})

    def _connect_next(self) -> None:
        with self._lock:
            if self._connecting or not self._connect_queue or self._stopping:
                return
            if self._state not in (SinkState.READY, SinkState.CONNECTED):
                return
            self._connecting = self._connect_queue.pop(0)
            addr = self._connecting
        self._log(f"Connecting to {addr}…")
        self._send_cmd({"cmd": "connect", "addr": addr})

    def _connect_finished(self, addr: str) -> None:
        with self._lock:
            if self._connecting == addr:
                self._connecting = None
        self._connect_next()

    PLAYER_ACTIONS = ("play", "pause", "stop", "next", "prev")

    def player_control(self, addr: str, action: str) -> None:
        """Sends an AVRCP player command (play/pause/stop/next/prev) to a source."""
        if action not in self.PLAYER_ACTIONS:
            raise ValueError(f"unknown player action {action!r}")
        self._send_cmd({"cmd": "player", "addr": addr.upper(), "action": action})

    def set_pairing_mode(self, allowed: bool) -> None:
        """
        Allow (True) or block (False) pairing requests from unknown devices.
        May be called before start(); the setting is applied once BTstack is ready.
        """
        with self._lock:
            self._pairing_allowed = allowed
            self._cancel_discoverable_timer()
            if self._state in (SinkState.READY, SinkState.CONNECTED):
                self._send_cmd({"cmd": "set_discoverable", "enabled": allowed})
                if allowed:
                    self._arm_discoverable_timer()

    def set_device_audio_route(self, addr: str, device_index: Optional[int]) -> None:
        """
        Change the sounddevice output for a specific connected source on the
        fly. The pipeline restart takes up to a few seconds, so it runs on a
        worker thread rather than the caller's (GUI) thread.
        """
        addr = addr.upper()
        with self._lock:
            self._device_audio_routes[addr] = device_index
            pipeline = self._pipelines.get(addr)
            codec = self._codec_types.get(addr, "sbc")
        if pipeline:
            threading.Thread(
                target=self._start_audio_pipeline,
                args=(addr, pipeline.sample_rate, pipeline.channels, codec),
                daemon=True, name="bt-reroute").start()

    def start(self) -> None:
        """Launches btstack_sink.exe on a background thread; returns immediately."""
        with self._lock:
            if self._started:
                return
            self._started = True
        self._set_state(SinkState.STARTING)
        threading.Thread(target=self._launch, daemon=True, name="bt-launch").start()

    def stop(self) -> None:
        """
        Shuts the engine down: asks it to power off, closes its stdin, waits a
        few seconds, kills it if necessary, then stops all audio pipelines.
        Safe to call from any thread and more than once.
        """
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
            self._cancel_discoverable_timer()
            for pending in self._pending.values():
                if pending.timer:
                    pending.timer.cancel()
            self._pending.clear()
            proc = self._proc

        if proc and proc.poll() is None:
            self._send_cmd({"cmd": "stop"})
            try:
                if proc.stdin:
                    proc.stdin.close()   # EOF also triggers shutdown in the engine
            except OSError:
                pass
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self._log("btstack_sink.exe did not exit, killing it")
                proc.kill()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass

        for t in self._threads:
            t.join(timeout=2.0)

        self._stop_all_pipelines()
        with self._lock:
            for addr in self._connected_addrs:
                self._persist_device_volume(addr)
            self._connected_addrs.clear()
            self._codec_types.clear()
            self._device_audio_routes.clear()
            self._session_allowed.clear()
        self._set_state(SinkState.STOPPED, force=True)

    # ------------------------------------------------------------------
    # Process lifecycle
    # ------------------------------------------------------------------

    @staticmethod
    def _find_btstack_exe() -> Optional[Path]:
        """Locates btstack_sink.exe in a PyInstaller bundle or the source tree."""
        candidates: list[Path] = []
        if getattr(sys, "frozen", False):
            # PyInstaller onefile extracts bundled binaries into _MEIPASS
            candidates.append(Path(getattr(sys, "_MEIPASS")) / "btstack_sink.exe")
        here = Path(__file__).resolve().parent
        candidates.append(here.parent / "btstack" / "build" / "btstack_sink.exe")
        candidates.append(here / "btstack_sink.exe")
        return next((p for p in candidates if p.exists()), None)

    def _launch(self) -> None:
        exe = self._find_btstack_exe()
        if not exe:
            self._log("Error: btstack_sink.exe not found. Build it first: .\\btstack\\build.ps1")
            self._set_state(SinkState.ERROR)
            return

        # argv layout: see header of btstack/btstack_sink.c
        cmd = [
            str(exe),
            self._usb_filter,
            self._device_name,
            str(self._max_bitpool),
            "1" if self._debug else "0",
            format(self._class_of_device, "X"),
            self._keystore_path or "",
            str(self._sbc_block_length),
            str(self._sbc_subbands),
            str(self._sbc_allocation),
            str(self._vendor_codecs),
        ]
        self._log(f"Launching BTstack: {exe.name}"
                  + (f" (dongle {self._usb_filter})" if self._usb_filter else ""))

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
            self._set_state(SinkState.ERROR)
            return

        with self._lock:
            if self._stopping:       # stop() raced us: don't leave a zombie
                proc.kill()
                return
            self._proc = proc
            self._threads = [
                threading.Thread(target=self._event_thread, args=(proc.stderr,),
                                 daemon=True, name="bt-events"),
                threading.Thread(target=self._audio_thread, args=(proc.stdout,),
                                 daemon=True, name="bt-audio"),
                threading.Thread(target=self._watch_thread, args=(proc,),
                                 daemon=True, name="bt-watch"),
            ]
            for t in self._threads:
                t.start()

    def _watch_thread(self, proc: subprocess.Popen) -> None:
        """Reports an engine that died on its own (crash, dongle lost, kill)."""
        code = proc.wait()
        if self._stopping:
            return
        self._log(f"btstack_sink.exe exited unexpectedly (code {code})")
        self._stop_all_pipelines()
        with self._lock:
            self._connected_addrs.clear()
            self._cancel_discoverable_timer()
        self._set_state(SinkState.ERROR)

    def _event_thread(self, stderr_pipe) -> None:
        """Reads JSON event lines from btstack_sink.exe stderr and dispatches them."""
        for raw_line in stderr_pipe:
            if self._stopping:
                break
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = _json.loads(line)
            except _json.JSONDecodeError:
                event = None
            if not isinstance(event, dict):
                self._log(f"[btstack] {line}")
                continue
            try:
                self._on_btstack_event(event)
            except Exception as exc:  # a bad handler must not kill event dispatch
                log.exception("event handler failed")
                self._log(f"Internal error handling {event.get('event')}: {exc}")

    def _audio_thread(self, stdout_pipe) -> None:
        """
        Reads addr-tagged length-prefixed audio frames from btstack_sink.exe stdout:
          [uint32_le total_len = 6 + payload_len][6 bytes bd_addr][payload]
        BufferedReader.read(n) blocks until n bytes or EOF, so short reads
        mean the engine is gone.
        """
        read = stdout_pipe.read
        while True:
            header = read(4)
            if len(header) < 4:
                break
            total_len = int.from_bytes(header, "little")
            if total_len < 7 or total_len > 65541:
                # The stream cannot be resynchronised; give up on audio.
                self._log(f"audio: malformed frame header ({total_len}), stopping audio reader")
                break
            body = read(total_len)
            if len(body) < total_len:
                break
            addr = ":".join(f"{b:02X}" for b in body[:6])
            with self._lock:
                pipeline = self._pipelines.get(addr)
            if pipeline:
                pipeline.write_audio(body[6:])

    def _send_cmd(self, cmd: dict) -> None:
        """Sends a JSON command line to btstack_sink.exe via stdin (any thread)."""
        proc = self._proc
        if not proc or not proc.stdin or proc.poll() is not None:
            return
        line = (_json.dumps(cmd) + "\n").encode("utf-8")
        with self._cmd_lock:
            try:
                proc.stdin.write(line)
                proc.stdin.flush()
            except (OSError, ValueError):
                pass   # engine gone / stdin closed by stop()

    # ------------------------------------------------------------------
    # Event handling  (bt-events thread only)
    # ------------------------------------------------------------------

    def _on_btstack_event(self, event: dict) -> None:
        evt = event.get("event", "")
        addr = str(event.get("addr", "")).upper()

        if self._debug and evt not in ("log", "error"):
            self._log(f"[dbg] {event}")

        if evt == "ready":
            self._log(f"BTstack ready! Address: {event.get('address', '')}")
            self._set_state(SinkState.READY)
            with self._lock:
                self._send_cmd({"cmd": "set_discoverable", "enabled": self._pairing_allowed})
                if self._pairing_allowed:
                    self._arm_discoverable_timer()
                # Bonding keys of devices forgotten while the engine was down
                pending = self._store.pop_pending_forget()
                for forget_addr in pending:
                    self._send_cmd({"cmd": "forget_key", "addr": forget_addr})
                if pending:
                    self._save_store()
                auto = self._store.auto_connect_addrs()
            for auto_addr in auto:
                self.connect_device(auto_addr)

        elif evt == "connect_failed":
            self._log(f"Could not connect to {addr} (status {event.get('status', '?')})")
            self._connect_finished(addr)
            if self._cb_connect_failed:
                self._cb_connect_failed(addr)

        elif evt == "l2cap_request":
            self._handle_l2cap_request(addr, int(event.get("cid", 0)))

        elif evt == "name":
            name = event.get("name", "")
            if name:
                with self._lock:
                    if self._store.set_name(addr, name):
                        self._save_store()
                if self._cb_device_name:
                    self._cb_device_name(addr, name)

        elif evt == "connected":
            self._log(f"A2DP connected: {addr}")
            with self._lock:
                self._connected_addrs.add(addr)
                self._device_volumes.setdefault(addr, self._store.volume(addr))
            self._set_state(SinkState.CONNECTED)
            if self._cb_connected:
                self._cb_connected(addr)
            self._connect_finished(addr)

        elif evt == "audio_start":
            sample_rate = int(event.get("sample_rate", 44100))
            channels = int(event.get("channels", 2))
            codec = str(event.get("codec", "sbc"))
            with self._lock:
                self._codec_types[addr] = codec
                self._streaming.add(addr)
            info = {"sample_rate": sample_rate, "channels": channels}
            if codec == "sbc" and event.get("block_length"):
                info.update(
                    block_length=int(event.get("block_length", 0)),
                    subbands=int(event.get("subbands", 0)),
                    allocation=str(event.get("allocation", "")),
                    bitpool=int(event.get("bitpool", 0)),
                )
                detail = (f", SBC {info['block_length']} blocks / {info['subbands']} subbands / "
                          f"{info['allocation']} / bitpool {info['bitpool']}")
            else:
                detail = ""
            self._log(f"Stream START [{addr}] → {sample_rate} Hz, {channels} ch [{codec.upper()}]{detail}")
            self._note_stream_started(addr)
            self._start_audio_pipeline(addr, sample_rate, channels, codec)
            if self._cb_audio_start:
                self._cb_audio_start(addr, codec, info)

        elif evt == "audio_stop":
            self._log(f"Stream STOP [{addr}]")
            with self._lock:
                self._streaming.discard(addr)
            self._stop_pipeline(addr)
            self._note_stream_stopped(addr)

        elif evt == "volume_changed":
            if self._cb_volume_changed:
                self._cb_volume_changed(addr, int(event.get("volume", 0)))

        elif evt == "playback":
            if self._cb_playback_status:
                self._cb_playback_status(addr, str(event.get("status", "")))

        elif evt == "metadata":
            if self._cb_metadata:
                self._cb_metadata(addr, {
                    "title":  event.get("title", ""),
                    "artist": event.get("artist", ""),
                    "album":  event.get("album", ""),
                })

        elif evt == "disconnected":
            self._log(f"A2DP disconnected: {addr}")
            with self._lock:
                self._persist_device_volume(addr)
                self._connected_addrs.discard(addr)     # before stopping: blocks a
                self._streaming.discard(addr)           # concurrent pipeline restart
                self._codec_types.pop(addr, None)
                self._session_allowed.pop(addr, None)   # "allow once" ends here
                none_left = not self._connected_addrs
            self._stop_pipeline(addr)
            self._note_stream_stopped(addr)
            if none_left and self._state == SinkState.CONNECTED:
                self._set_state(SinkState.READY)
            if self._cb_disconnected:
                self._cb_disconnected(addr)

        elif evt == "log":
            self._log(f"[btstack] {event.get('msg', '')}")

        elif evt == "error":
            self._log(f"[btstack error] {event.get('msg', '')}")
            self._set_state(SinkState.ERROR)

    # ------------------------------------------------------------------
    # Pairing approval
    # ------------------------------------------------------------------

    def _handle_l2cap_request(self, addr: str, cid: int) -> None:
        """
        Gate an incoming AVDTP L2CAP connection on user approval.

        Known device (remembered or allowed-once this session) → approve.
        Unknown device + pairing disabled → deny.
        Unknown device + pairing enabled → ask the GUI, deny after PAIRING_TIMEOUT_S.
        """
        if self._store.is_remembered(addr) or self._session_allowed_ok(addr):
            self._log(f"AVDTP: auto-approving known device {addr}")
            self._send_cmd({"cmd": "approve", "addr": addr, "cid": cid})
            return

        if not self._pairing_allowed or not self._cb_pairing_request:
            self._log(f"AVDTP: rejecting unknown device (pairing off): {addr}")
            self._send_cmd({"cmd": "deny", "addr": addr, "cid": cid})
            return

        with self._lock:
            pending = self._pending.get(addr)
            if pending:
                # Dialog already open for this device (retry from the source):
                # answer both channels with the one decision.
                pending.cids.append(cid)
                return
            pending = _PendingApproval(addr, cid)
            self._pending[addr] = pending
            pending.timer = threading.Timer(
                PAIRING_TIMEOUT_S, self._resolve_pairing, args=(addr, False, False))
            pending.timer.daemon = True
            pending.timer.start()

        self._log(f"AVDTP connection from unknown device: {addr}")

        def resolve(approved: bool, remember: bool = False) -> None:
            self._resolve_pairing(addr, bool(approved), bool(remember))

        try:
            self._cb_pairing_request(addr, resolve)
        except Exception as exc:
            self._log(f"Pairing dialog failed ({exc}); denying {addr}")
            resolve(False, False)

    def _resolve_pairing(self, addr: str, approved: bool, remember: bool) -> None:
        """Answers an open pairing question exactly once (any thread)."""
        with self._lock:
            pending = self._pending.pop(addr, None)
            if pending is None:
                return   # already answered or timed out
            if pending.timer:
                pending.timer.cancel()
            cids = list(pending.cids)
            if self._stopping:
                return
            if approved:
                if remember:
                    self._store.remember(addr)
                    self._save_store()
                else:
                    self._session_allowed[addr] = time.monotonic()

        if approved:
            self._log(f"AVDTP connection approved: {addr}" + (" (remembered)" if remember else ""))
        else:
            self._log(f"AVDTP connection denied: {addr}")
        for cid in cids:
            self._send_cmd({"cmd": "approve" if approved else "deny", "addr": addr, "cid": cid})

    def _session_allowed_ok(self, addr: str) -> bool:
        with self._lock:
            approved_at = self._session_allowed.get(addr)
            if approved_at is None:
                return False
            if addr in self._connected_addrs:
                return True
            if time.monotonic() - approved_at <= SESSION_ALLOW_TTL_S:
                return True
            del self._session_allowed[addr]
            return False

    # ------------------------------------------------------------------
    # Discoverable auto-off timer
    # ------------------------------------------------------------------

    def _arm_discoverable_timer(self) -> None:
        """Caller holds self._lock."""
        if self._discoverable_timeout_s <= 0:
            return
        self._discoverable_timer_id += 1
        timer_id = self._discoverable_timer_id
        self._discoverable_timer = threading.Timer(
            self._discoverable_timeout_s, self._on_discoverable_timeout, args=(timer_id,))
        self._discoverable_timer.daemon = True
        self._discoverable_timer.start()

    def _cancel_discoverable_timer(self) -> None:
        """Caller holds self._lock."""
        self._discoverable_timer_id += 1   # invalidates a callback already in flight
        if self._discoverable_timer is not None:
            self._discoverable_timer.cancel()
            self._discoverable_timer = None

    def _on_discoverable_timeout(self, timer_id: int) -> None:
        with self._lock:
            if timer_id != self._discoverable_timer_id or self._stopping:
                return
            self._discoverable_timer = None
            self._pairing_allowed = False
            self._send_cmd({"cmd": "set_discoverable", "enabled": False})
        if self._cb_pairing_timeout:
            self._cb_pairing_timeout()

    # ------------------------------------------------------------------
    # Audio pipelines
    # ------------------------------------------------------------------

    def _start_audio_pipeline(self, addr: str, sample_rate: int, channels: int,
                              codec: str = "sbc") -> None:
        """
        Creates (or replaces) the per-device audio pipeline (SBC or AAC).
        Replacements are serialised, and a pipeline is only installed while
        the device is still streaming, so a route change racing an
        audio_stop/disconnect can neither leak nor resurrect a pipeline.
        """
        with self._pipeline_swap:
            with self._lock:
                if self._stopping or addr not in self._streaming:
                    return
                old = self._pipelines.pop(addr, None)
                device_index = self._device_audio_routes.get(addr, self._audio_device_index)
            if old:
                old.stop()
            pipeline = AudioPipeline(
                codec=codec,
                ffmpeg_exe=self._ffmpeg_exe,
                latency_ms=self._latency_ms,
                device_index=device_index,
                on_level=self._cb_level,
            )
            with self._lock:
                pipeline.set_volume(self._effective_gain(addr))
            try:
                pipeline.start(sample_rate, channels)
            except Exception as exc:
                self._log(f"Pipeline error [{addr}]: {exc}")
                return
            with self._lock:
                if self._stopping or addr not in self._streaming:
                    pipeline.stop()
                    return
                self._pipelines[addr] = pipeline
        self._log(f"Audio pipeline started [{addr}] codec={codec}")

    def _stop_pipeline(self, addr: str) -> None:
        with self._lock:
            pipeline = self._pipelines.pop(addr, None)
        if pipeline:
            pipeline.stop()

    def _stop_all_pipelines(self) -> None:
        with self._lock:
            pipelines = list(self._pipelines.values())
            self._pipelines.clear()
        for p in pipelines:
            p.stop()

    # ------------------------------------------------------------------
    # Remembered-device persistence (caller holds self._lock)
    # ------------------------------------------------------------------

    def _save_store(self) -> None:
        error = self._store.save()
        if error:
            self._log(f"Could not save remembered devices ({error})")

    def _persist_device_volume(self, addr: str) -> None:
        vol = self._device_volumes.get(addr)
        if vol is not None and self._store.set_volume(addr, vol):
            self._save_store()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _set_state(self, state: SinkState, force: bool = False) -> None:
        """Updates the state and fires on_state_change. Ignored after stop() unless forced."""
        if self._stopping and not force:
            return
        self._state = state
        if self._cb_state:
            try:
                self._cb_state(state)
            except Exception:
                log.debug("on_state_change callback failed", exc_info=True)

    def _log(self, msg: str) -> None:
        """Logs to the Python logger and forwards to the GUI callback."""
        log.info(msg)
        if self._cb_log:
            try:
                self._cb_log(msg)
            except Exception:
                log.debug("on_log callback failed", exc_info=True)
