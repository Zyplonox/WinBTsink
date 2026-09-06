# Contributing to WinBTsink

The rules below are what this codebase already does. They exist because the
app is a three-process pipeline with real-time audio in the middle: a rule
broken here usually shows up as a dropout, a hang on Stop, or a device that
silently stops pairing, not as an exception in a log.

## Before you push

```powershell
python -m pip install -r requirements.txt -r requirements-dev.txt

python -m ruff check .              # lint (config in pyproject.toml)
python -m pytest                    # tests that need no dongle
python tools/check_ipc_contract.py  # C engine and Python still agree
```

All three run in CI on every push and pull request. There is no automatic
formatter: several tables in the sources are aligned by hand for readability,
and `ruff format` would flatten them. Keep the surrounding style instead.

Hardware paths cannot be tested automatically. Anything touching pairing,
streaming or the dongle has to be tried with a real device before it ships,
and the pull request should say which device it was tried with.

## 1. The process contract comes first

`btstack_sink.exe`, `SinkBackend` and the GUI exchange exactly three things:
positional arguments at launch, JSON commands on stdin, JSON events on stderr.
The authoritative description is the header comment of
[btstack/btstack_sink.c](btstack/btstack_sink.c).

- A new event, command or argument must be added on **both** sides in the same
  commit: `process_command` / `main` in the C file, and
  `SinkBackend._on_btstack_event` / `_launch` in [src/backend.py](src/backend.py).
- Argument slots are positional. Append, never reorder or reuse.
- `tools/check_ipc_contract.py` compares the two sources and fails when a name
  exists on one side only. Run it after touching either.

## 2. stdout carries audio, nothing else

The engine writes raw audio frames to stdout. A stray `printf` corrupts the
stream and the symptom is distorted sound, not an error.

- In C, log with `emit_log` and report with `emit_event`, both on stderr.
- Every string that goes into an event must pass through `json_escape`.
- Device names arrive from the remote side. Treat them as untrusted input:
  bound the length, never build a JSON string by concatenation.

## 3. Know which thread you are on

| Thread | Runs | Rule |
|--------|------|------|
| Tk mainloop | the GUI | Only this thread may touch widgets |
| `bt-events` | reads engine stderr, dispatches events | Handlers are serialized here; keep them short |
| `bt-audio` | reads engine stdout frames | Never block: a slow frame is a dropout |
| `bt-watch` | waits for the engine to exit | Reports a crash as an error state |
| sounddevice callback | fills the audio device | Real time: no locks held long, no I/O, no Tk |
| C run loop | all BTstack calls | BTstack is single threaded; the stdin reader thread may only enqueue |

Two patterns must be kept when adding callbacks:

- Backend callbacks fire on backend threads. In the GUI, wrap them with
  `App._ui(gen, fn)`, which hops to the mainloop and drops events that arrive
  after the backend they belong to was stopped.
- The audio level is polled, not pushed: the sounddevice callback stores a
  float and `_poll_level` reads it every 50 ms. Anything on the real-time path
  gets the same treatment rather than a direct callback.

## 4. A failing handler must not take a thread down

One bad event must not end event dispatch, and one bad device must not stop
the others.

- Catch the specific exception where you can name it: `OSError`, `ValueError`,
  `queue.Empty`.
- A broad `except Exception` is allowed only at a thread or callback boundary,
  and it must log what happened rather than pass silently.
- `try/except/pass` without a comment is not acceptable. Say why the failure
  is safe to ignore.

## 5. Broken files are expected, crashes at start-up are not

Settings, the remembered-device list and the bonding keys live in files the
user can edit or corrupt.

- Loading falls back to defaults and logs a warning. It never raises.
- Values are range-checked on load, not only when written: see the type table
  in `Settings._PERSIST` and the clamping in `DeviceStore.load`.
- Saving reports an error instead of raising, and never destroys an
  unreadable original without keeping a copy.

## 6. Paths

Everything the app writes at runtime goes to `%APPDATA%\BT-AudioSink`, through
the helpers in [src/config.py](src/config.py). Never write next to the
executable: in a PyInstaller build that is a temporary directory that
disappears. Anything read from the bundle must work in both layouts, source
tree and `sys._MEIPASS`.

## 7. Every user-visible string is translatable

- Wrap it in `tr("...")` from [src/i18n.py](src/i18n.py).
- Add the German entry to the table in the same module. Placeholders must
  match exactly; a missing `{name}` raises at runtime, and a test checks this.
- The key is the English source text. Keep it on one line so it can be grepped
  from the code that produces it.

## 8. Style

- Python 3.10 or newer. `from __future__ import annotations` at the top of
  every module, `X | None` instead of `Optional[X]`.
- Type hints on anything another module calls.
- Line length 110. Imports sorted by ruff into standard library, third party
  and project groups.
- Module docstrings follow the existing header format: what the module is, how
  it fits into the pipeline, what the caller has to know.
- Comments explain why, not what. The reason a delay exists, a lock is held or
  a device is treated specially is the part nobody can reconstruct later.
- No `print` in application code. Use the module logger, or the backend's
  `_log` when the user should see it. Command-line output in scripts is fine.
- Class-level constants get a `ClassVar` annotation so they are not mistaken
  for instance fields.

## 9. C engine

- BTstack APIs are called only from the run-loop thread. The stdin reader
  thread writes into the ring buffer and signals the run-loop data source.
- The run loop on Windows ignores `btstack_run_loop_trigger_exit()`. Shutdown
  goes through `exit()` once HCI reports powered off.
- Stock BTstack accepts incoming AVDTP and AVRCP connections immediately. The
  deferred-accept hooks come from `btstack/patches/apply_patches.py`, which is
  marker-guarded and refuses to touch a half-patched file. After bumping the
  BTstack pin, re-check the regex anchors there.
- Some sources are strict about timing. A device that already holds a
  connection must be accepted without a round trip to the user.

## 10. Tests

`tests/` holds what can run without hardware: file formats, validation,
version comparison, the gain policy, frame parsing and the HTTP API. They may
not open a dongle, a window, a toast or the network, and they must not write
outside `tmp_path`; the `appdata` fixture redirects the data directory.

When you fix a bug, add the case that failed. When you add a rule about what
an input may contain, add the input that violates it.

## 11. Commits

- One change per commit, present tense, saying what changes and why.
- Mention the hardware you verified against when the change touches Bluetooth.
- The `btstack/btstack-src` clone and `dist/` are build output and stay out of
  the repository.
