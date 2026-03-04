"""
backend.py – Bluetooth A2DP Sink Backend
=========================================
Encapsulates all Bluetooth and audio logic without any GUI dependency.
The GUI communicates exclusively via callbacks and the start() / stop() API.

Architecture overview
---------------------
┌─ SinkBackend ──────────────────────────────────────────────────────────┐
│  start()  → daemon thread → asyncio loop → _async_main()              │
│                                              ├─ open USB transport     │
│                                              ├─ configure BT Device    │
│                                              ├─ register A2DP sink     │
│                                              └─ wait for stop event    │
│                                                                        │
│  stop()   → sets asyncio stop event → joins pipeline                  │
│                                                                        │
│  Callbacks (always called from the BT thread):                        │
│    on_state_change, on_device_connected, on_device_disconnected,       │
│    on_audio_level, on_log                                              │
│    → GUI must forward these to the Tk mainloop via root.after(0, …)   │
└────────────────────────────────────────────────────────────────────────┘

Audio pipeline
--------------
RTP payload (SBC frames)
  → FFmpeg subprocess (SBC → s16le PCM, via stdin/stdout pipes)
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

try:
    from bumble.device import Device
    from bumble.transport import open_transport
    from bumble import avdtp, avrcp
    from bumble.a2dp import (
        SbcMediaCodecInformation,
        A2DP_SBC_CODEC_TYPE as SBC_CODEC_TYPE,
        make_audio_sink_service_sdp_records,
    )
    from bumble.pairing import PairingConfig, PairingDelegate
    from bumble.keys import JsonKeyStore
except ImportError as e:
    raise ImportError(
        "Package 'bumble' is missing. Please run: pip install bumble"
    ) from e

log = logging.getLogger("bt-sink.backend")

#: Bluetooth device class: Rendering + Audio service | Audio/Video major | Headphones minor.
DEVICE_CLASS = 0x240418

#: Suppress the FFmpeg/subprocess console window on Windows.
_POPEN_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

#: Maps SBC SamplingFrequency enum members to integer sample-rate values.
_SBC_SAMPLE_RATE_MAP: dict = {}  # populated lazily after bumble imports succeed


def _build_sample_rate_map() -> dict:
    """
    Builds the SamplingFrequency → Hz mapping from bumble's enum.
    Called once on first use so that the module-level constant doesn't fail
    when bumble isn't installed.
    """
    SF = SbcMediaCodecInformation.SamplingFrequency
    return {
        SF.SF_48000: 48000,
        SF.SF_44100: 44100,
        SF.SF_32000: 32000,
        SF.SF_16000: 16000,
    }


# ---------------------------------------------------------------------------
# Keystore persistence
# ---------------------------------------------------------------------------

def _load_keystore(path: Path, namespace: str) -> "JsonKeyStore":
    """
    Loads bonding keys from a JSON file and returns a JsonKeyStore.

    The namespace (typically the local BT address) scopes the keys so that
    multiple devices with different addresses don't share the same key store.
    Returns an empty store when the file doesn't exist or is malformed.
    """
    try:
        if path.exists():
            with open(path, encoding="utf-8") as f:
                data = _json.load(f)
            return JsonKeyStore(namespace, data)
    except Exception as e:
        log.debug("Load keystore: %s", e)
    return JsonKeyStore(namespace)


def _save_keystore(keystore: "JsonKeyStore", path: Path) -> None:
    """
    Persists bonding keys to a JSON file so paired devices reconnect
    automatically on the next session without requiring re-pairing.

    Handles both the as_dict() API (newer bumble) and the raw .store
    attribute (older builds) for forward/backward compatibility.
    """
    try:
        if hasattr(keystore, "as_dict"):
            data = keystore.as_dict()
        elif hasattr(keystore, "store"):
            ns = getattr(keystore, "namespace", "__DEFAULT__")
            data = {ns: {str(a): k.to_dict() for a, k in keystore.store.items()}}
        else:
            return  # Unknown keystore format – give up silently

        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            _json.dump(data, f, indent=2)
    except Exception as e:
        log.debug("Save keystore: %s", e)


def _is_in_keystore(keystore, addr_str: str) -> bool:
    """Returns True if the given BT address already has a stored bonding key."""
    if keystore is None or not hasattr(keystore, "store"):
        return False
    return any(str(a).upper() == addr_str.upper() for a in keystore.store)


# ---------------------------------------------------------------------------
# Pairing confirmation delegate
# ---------------------------------------------------------------------------

class ConfirmingPairingDelegate(PairingDelegate):
    """
    Pairing delegate that calls an on_request callback to ask the user
    whether to accept or reject a new pairing.

    The callback signature is: on_request(name, address, resolve)
    where resolve(approved: bool, remember: bool) can be called from any thread.

    If the user does not respond within TIMEOUT seconds the pairing is
    automatically rejected.
    """

    TIMEOUT = 30.0

    def __init__(
        self,
        name: str,
        address: str,
        on_request: Callable,
        remember_map: dict,
    ):
        super().__init__(PairingDelegate.IoCapability.NO_OUTPUT_NO_INPUT)
        self._name = name
        self._address = address
        self._on_request = on_request
        self._remember_map = remember_map

    async def accept(self) -> bool:
        loop = asyncio.get_running_loop()
        future: "asyncio.Future[bool]" = loop.create_future()

        def resolve(approved: bool, remember: bool) -> None:
            self._remember_map[self._address] = remember
            if not future.done():
                loop.call_soon_threadsafe(future.set_result, approved)

        self._on_request(self._name, self._address, resolve)
        try:
            return await asyncio.wait_for(future, timeout=self.TIMEOUT)
        except asyncio.TimeoutError:
            return False


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class SinkState(Enum):
    """Lifecycle states of the SinkBackend."""
    IDLE = auto()       # Not started yet
    STARTING = auto()   # Thread launched, USB transport opening
    READY = auto()      # Device powered on, discoverable, waiting for a source
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
    write_sbc()  is called from the asyncio/bumble thread.
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
# Codec parameter extraction
# ---------------------------------------------------------------------------

def _extract_codec_params(sink) -> tuple[int, int]:
    """
    Reads the negotiated SBC codec parameters from the AVDTP sink's
    configuration and returns (sample_rate_hz, num_channels).

    Falls back to 44100 Hz stereo if the configuration is unreadable,
    which matches the mandatory SBC baseline in the A2DP specification.
    """
    global _SBC_SAMPLE_RATE_MAP
    if not _SBC_SAMPLE_RATE_MAP:
        _SBC_SAMPLE_RATE_MAP = _build_sample_rate_map()

    sample_rate = 44100  # A2DP mandatory baseline
    channels = 2

    try:
        for cap in sink.configuration:
            if not isinstance(cap, avdtp.MediaCodecCapabilities):
                continue
            info = cap.media_codec_information
            SF = SbcMediaCodecInformation.SamplingFrequency
            CM = SbcMediaCodecInformation.ChannelMode

            # Dict lookup replaces the previous if/elif chain
            for sf_flag, hz in _SBC_SAMPLE_RATE_MAP.items():
                if sf_flag in info.sampling_frequency:
                    sample_rate = hz
                    break

            channels = 1 if CM.MONO in info.channel_mode else 2
            break  # Only the first MediaCodecCapabilities entry is relevant
    except Exception as e:
        log.debug("Could not read codec parameters: %s", e)

    return sample_rate, channels


# ---------------------------------------------------------------------------
# SinkBackend – public API consumed by the GUI
# ---------------------------------------------------------------------------

class SinkBackend:
    """
    Manages the full Bluetooth + audio lifecycle.

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
        self._keystore_path = Path(keystore_path) if keystore_path else None

        # GUI callbacks
        self._cb_state = on_state_change
        self._cb_connected = on_device_connected
        self._cb_disconnected = on_device_disconnected
        self._cb_level = on_audio_level
        self._cb_log = on_log
        self._cb_pairing_request = on_pairing_request

        # Pairing control
        self._pairing_allowed = True           # Allow new (unknown) device pairings
        self._remember_map: dict[str, bool] = {}  # addr -> should persist key to disk

        # Runtime state – all set during start()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._pipeline: Optional[SbcAudioPipeline] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._state = SinkState.IDLE

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def state(self) -> SinkState:
        """Current backend state (thread-safe read; set only from BT thread)."""
        return self._state

    def set_volume(self, volume: float) -> None:
        """
        Updates the output volume (linear multiplier, 0.0–2.0).
        Takes effect immediately if a pipeline is already running.
        """
        self._volume = max(0.0, min(2.0, volume))
        if self._pipeline:
            self._pipeline.set_volume(self._volume)

    def set_pairing_mode(self, allowed: bool) -> None:
        """Allow (True) or block (False) pairing requests from unknown devices."""
        self._pairing_allowed = allowed

    def start(self) -> None:
        """
        Transitions to STARTING and launches the background daemon thread.
        No-op if the backend is already running.
        """
        if self._state not in (SinkState.IDLE, SinkState.STOPPED, SinkState.ERROR):
            return
        self._set_state(SinkState.STARTING)
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="bt-backend"
        )
        self._thread.start()

    def stop(self) -> None:
        """
        Signals the asyncio loop to exit and stops the audio pipeline.
        Safe to call from any thread.
        """
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
        """
        Entry point for the daemon thread.  Creates a fresh asyncio event loop,
        installs bumble log forwarding, runs the main coroutine, then cleans up.
        """
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_event = asyncio.Event()

        handlers = self._install_log_handlers()
        try:
            self._loop.run_until_complete(self._async_main())
        except Exception as exc:
            self._log(f"Fatal error: {exc}")
            self._set_state(SinkState.ERROR)
        finally:
            self._remove_log_handlers(handlers)
            self._loop.close()
            self._loop = None
            self._stop_event = None

    def _build_logger_config(self) -> dict[str, int]:
        """
        Returns a mapping of bumble logger names to the desired log level,
        depending on whether debug mode is active.
        """
        if self._debug:
            return {
                "bumble.avdtp": logging.DEBUG,
                "bumble.l2cap": logging.DEBUG,
                "bumble.device": logging.WARNING,
                "bumble.hci": logging.WARNING,
            }
        return {
            "bumble.avdtp": logging.WARNING,
            "bumble.l2cap": logging.WARNING,
            "bumble.device": logging.WARNING,
            "bumble.hci": logging.WARNING,
        }

    def _install_log_handlers(self) -> dict[str, logging.Handler]:
        """
        Attaches a custom log handler to the relevant bumble loggers so their
        output is forwarded to the GUI log panel.

        Returns the {logger_name: handler} dict so they can be removed later.
        """
        log_fn = self._log

        class _GuiHandler(logging.Handler):
            """Forwards bumble log records to the GUI callback."""
            def emit(self, record: logging.LogRecord) -> None:
                try:
                    # Strip the 'bumble.' prefix for compactness
                    tag = record.name.split(".")[-1]
                    log_fn(f"[{tag}] {record.getMessage()}")
                except Exception:
                    pass

        config = self._build_logger_config()
        installed: dict[str, logging.Handler] = {}
        for name, level in config.items():
            handler = _GuiHandler()
            handler.setLevel(level)
            lg = logging.getLogger(name)
            lg.addHandler(handler)
            lg.setLevel(level)
            installed[name] = handler
        return installed

    def _remove_log_handlers(self, handlers: dict[str, logging.Handler]) -> None:
        """Detaches the GUI log handlers installed by _install_log_handlers()."""
        for name, handler in handlers.items():
            logging.getLogger(name).removeHandler(handler)

    # ------------------------------------------------------------------
    # Async main
    # ------------------------------------------------------------------

    async def _async_main(self) -> None:
        """
        Opens the USB HCI transport and hands off to _setup_device().
        Reports transport errors (missing dongle, wrong WinUSB config, …) to the GUI.
        """
        self._log(f"Opening USB transport: {self._transport_str}")
        try:
            async with await open_transport(self._transport_str) as transport:
                await self._setup_device(transport.source, transport.sink)
        except Exception as exc:
            self._log(f"Transport error: {exc}")
            self._log("Possible causes:")
            self._log("  • No Bluetooth dongle connected")
            self._log("  • WinUSB driver (Zadig) not installed")
            self._log(f"  • Wrong transport index (try usb:0 … usb:3)")
            self._set_state(SinkState.ERROR)

    async def _setup_device(self, hci_source, hci_sink) -> None:
        """
        Configures the bumble Device, registers AVDTP/AVRCP, and enters the
        'advertise and wait for connection' loop.  Exits when the stop event fires.
        """
        device = self._create_device(hci_source, hci_sink)
        self._configure_sdp_records(device)
        self._attach_connection_handlers(device)

        # AVRCP Target is required by many sources (Switch, phones)
        # even if we don't use the remote control commands
        avrcp_protocol = avrcp.Protocol()
        avrcp_protocol.listen(device)

        # AVDTP listener – handles stream negotiation and media delivery
        listener = avdtp.Listener.for_device(device)
        listener.on("connection", lambda server: self._on_avdtp_connection(server, device))

        await device.power_on()
        await device.set_discoverable(True)
        await device.set_connectable(True)

        self._log(f"Ready! Device name: {self._device_name}")
        self._log("Open Bluetooth settings → pair with device → play audio")
        self._set_state(SinkState.READY)

        # Block until stop() signals the event
        assert self._stop_event is not None
        await self._stop_event.wait()

    def _create_device(self, hci_source, hci_sink) -> Device:
        """
        Instantiates and configures the bumble Device object.

        Key settings:
          - Classic BT only (LE disabled) – A2DP is a BR/EDR profile.
          - Secure Connections disabled – many consoles (Switch) require
            legacy pairing and cannot negotiate SC.
          - Persistent bonding keys loaded from disk (if configured).
        """
        device = Device.with_hci(
            name=self._device_name,
            address=self._bt_address,
            hci_source=hci_source,
            hci_sink=hci_sink,
        )
        device.classic_enabled = True
        device.le_enabled = False
        device.classic_sc_enabled = False  # Legacy pairing for console compatibility
        device.class_of_device = DEVICE_CLASS

        if self._keystore_path:
            # Load previously saved bonding keys so peers reconnect without re-pairing
            device.keystore = _load_keystore(self._keystore_path, self._bt_address)
            log.debug("Keystore loaded: %s", self._keystore_path)

        # Pairing policy: auto-accept known devices, ask user for unknown ones,
        # or silently reject unknown ones when pairing mode is disabled.
        def _pairing_config_factory(connection) -> PairingConfig:
            addr = str(connection.peer_address)
            name = str(getattr(connection, "peer_name", None) or connection.peer_address)
            is_known = _is_in_keystore(device.keystore, addr)

            if not self._pairing_allowed and not is_known:
                # Silently reject – user disabled new pairings
                class _Reject(PairingDelegate):
                    async def accept(self) -> bool:
                        return False
                return PairingConfig(mitm=False, delegate=_Reject())

            if self._cb_pairing_request and not is_known:
                # Ask the user via GUI dialog
                delegate = ConfirmingPairingDelegate(
                    name, addr, self._cb_pairing_request, self._remember_map
                )
                return PairingConfig(mitm=False, delegate=delegate)

            # Known device or no pairing callback – auto-accept
            return PairingConfig(mitm=False)

        device.pairing_config_factory = _pairing_config_factory

        return device

    def _configure_sdp_records(self, device: Device) -> None:
        """
        Registers the SDP service records required for A2DP Sink operation.
        The AVRCP Target record is also needed: many Bluetooth sources (Nintendo
        Switch, phones) will not initiate A2DP unless AVRCP is also advertised.
        """
        device.sdp_service_records = {
            0x00010001: make_audio_sink_service_sdp_records(0x00010001),
            0x00010002: avrcp.TargetServiceSdpRecord(0x00010002).to_service_attributes(),
        }

    def _attach_connection_handlers(self, device: Device) -> None:
        """
        Registers bumble device-level event handlers for BT connections and
        disconnections.  These fire for the Classic BT link, independent of
        the AVDTP audio stream state.
        """

        @device.on("connection")
        def on_connection(connection):
            name = str(connection.peer_name or connection.peer_address)
            addr = str(connection.peer_address)
            self._log(f"BT connected: {name} ({addr})")
            self._set_state(SinkState.CONNECTED)
            if self._cb_connected:
                self._cb_connected(name, addr)

        @device.on("disconnection")
        def on_disconnection(connection, reason):
            addr = str(connection.peer_address)
            name = str(getattr(connection, "peer_name", None) or connection.peer_address)
            self._log(f"BT disconnected: {name}")

            # Persist bonding keys unless the user explicitly chose not to remember
            # the device (remember_map entry False = "allow once, don't save").
            # Unknown devices that bypassed the dialog default to True (save).
            should_remember = self._remember_map.pop(addr, True)
            if should_remember and self._keystore_path and device.keystore:
                _save_keystore(device.keystore, self._keystore_path)

            if self._pipeline:
                self._pipeline.stop()
                self._pipeline = None

            self._set_state(SinkState.READY)
            if self._cb_disconnected:
                self._cb_disconnected(name)

    # ------------------------------------------------------------------
    # AVDTP stream registration
    # ------------------------------------------------------------------

    def _on_avdtp_connection(self, server, device: Device) -> None:
        """Called by the AVDTP Listener when a new AVDTP session is established."""
        self._log("AVDTP connection established")
        self._register_sbc_sink(server)

    def _register_sbc_sink(self, server) -> None:
        """
        Advertises SBC codec capabilities to the AVDTP server and wires up
        the stream lifecycle handlers (start/stop/rtp_packet).

        pipeline_ref is a one-element list so the closures can rebind the
        pipeline reference without needing nonlocal (Python 2 compatibility
        pattern; also makes it easy to null out on stop).
        """
        sbc_capabilities = self._build_sbc_capabilities()
        sink = server.add_sink(sbc_capabilities)

        # One-element list shared by all closures so they can exchange the pipeline ref
        pipeline_ref: list[Optional[SbcAudioPipeline]] = [None]

        @sink.on("configuration")
        def on_configuration():
            self._log("AVDTP: codec configured")

        @sink.on("open")
        def on_open():
            self._log("AVDTP: stream opened – waiting for RTP channel…")

        @sink.on("rtp_channel_open")
        def on_rtp_channel_open():
            self._log("AVDTP: RTP channel open – waiting for START…")

        @sink.on("start")
        def on_start():
            """Source sent AVDTP START – create and start the audio pipeline."""
            sample_rate, channels = _extract_codec_params(sink)
            self._log(f"Stream START → {sample_rate} Hz, {channels} ch")

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
            pipeline_ref[0] = pipeline

        @sink.on("stop")
        def on_stop():
            """Source sent AVDTP SUSPEND – tear down the audio pipeline."""
            self._log("Stream STOP")
            if pipeline_ref[0]:
                pipeline_ref[0].stop()
                pipeline_ref[0] = None
            self._pipeline = None

        @sink.on("rtp_packet")
        def on_rtp_packet(packet):
            """Receives RTP packets and strips the 1-byte SBC header before feeding FFmpeg."""
            payload = bytes(packet.payload)
            # The first byte is the SBC media header (fragment/RFA/frame count)
            if len(payload) < 2:
                return
            if pipeline_ref[0]:
                pipeline_ref[0].write_sbc(payload[1:])

        self._log("SBC sink registered")

    def _build_sbc_capabilities(self) -> avdtp.MediaCodecCapabilities:
        """
        Constructs the AVDTP MediaCodecCapabilities object advertising all
        standard SBC parameter combinations.  The remote source picks the
        best match from this set during capability negotiation.
        """
        SF = SbcMediaCodecInformation.SamplingFrequency
        CM = SbcMediaCodecInformation.ChannelMode
        BL = SbcMediaCodecInformation.BlockLength
        SB = SbcMediaCodecInformation.Subbands
        AM = SbcMediaCodecInformation.AllocationMethod

        sbc_info = SbcMediaCodecInformation(
            sampling_frequency=SF.SF_16000 | SF.SF_32000 | SF.SF_44100 | SF.SF_48000,
            channel_mode=CM.MONO | CM.DUAL_CHANNEL | CM.STEREO | CM.JOINT_STEREO,
            block_length=BL.BL_4 | BL.BL_8 | BL.BL_12 | BL.BL_16,
            subbands=SB.S_4 | SB.S_8,
            allocation_method=AM.SNR | AM.LOUDNESS,
            minimum_bitpool_value=2,
            maximum_bitpool_value=self._max_bitpool,  # Configurable quality ceiling
        )
        return avdtp.MediaCodecCapabilities(
            media_type=avdtp.AVDTP_AUDIO_MEDIA_TYPE,
            media_codec_type=SBC_CODEC_TYPE,
            media_codec_information=sbc_info,
        )

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
