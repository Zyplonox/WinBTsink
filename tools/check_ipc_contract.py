"""
check_ipc_contract.py - keep btstack_sink.c and backend.py in sync
==================================================================
The C engine and the Python backend talk over three fixed interfaces:

    argv     positional arguments passed at launch
    stdin    JSON command lines  {"cmd": "..."}
    stderr   JSON event lines    {"event": "..."}

Nothing at build time notices when one side gains a name the other does not
know, so this script compares them by reading both sources. It is pure text
analysis: no compiler, no dongle, no imports from the project.

Run it directly or via CI:

    python tools/check_ipc_contract.py

Exit code 0 means both sides agree, 1 means they do not.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "btstack" / "btstack_sink.c"
BACKEND = ROOT / "src" / "backend.py"

# Events the backend deliberately does not branch on: they are logged or
# consumed by the generic handler rather than by name.
EVENTS_WITHOUT_A_HANDLER = {"log"}


def read(path: Path) -> str:
    if not path.exists():
        sys.exit(f"missing source file: {path}")
    return path.read_text(encoding="utf-8", errors="replace")


def engine_events(source: str) -> set[str]:
    r"""Names in the emitted JSON: "{\"event\":\"ready\", ...}"."""
    return set(re.findall(r'\\"event\\":\\"([a-z0-9_]+)', source))


def engine_commands(source: str) -> set[str]:
    """Names accepted in process_command: strcmp(cmd, "approve")."""
    return set(re.findall(r'strcmp\(\s*cmd\s*,\s*"([a-z0-9_]+)"\s*\)', source))


def engine_argv_slots(source: str) -> int:
    """Highest argv index the engine reads, so argv[10] means 11 slots."""
    indexes = [int(n) for n in re.findall(r"argv\[(\d+)\]", source)]
    return max(indexes) + 1 if indexes else 0


def backend_events(source: str) -> set[str]:
    """Names compared against in _on_btstack_event."""
    names = set(re.findall(r'evt\s*==\s*"([a-z0-9_]+)"', source))
    for group in re.findall(r'evt\s+in\s*\(([^)]*)\)', source):
        names.update(re.findall(r'"([a-z0-9_]+)"', group))
    return names


def backend_commands(source: str) -> set[str]:
    """Names sent through _send_cmd: {"cmd": "approve", ...}."""
    return set(re.findall(r'"cmd"\s*:\s*"([a-z0-9_]+)"', source))


def backend_argv_slots(source: str) -> int:
    """Length of the argv list built in _launch, executable included."""
    match = re.search(r"argv layout.*?cmd = \[(.*?)\n\s*\]", source, re.DOTALL)
    if not match:
        return -1
    body = match.group(1)
    depth, items, current = 0, [], ""
    for char in body:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if char == "," and depth == 0:
            items.append(current)
            current = ""
        else:
            current += char
    if current.strip():
        items.append(current)
    return len([i for i in items if i.strip()])


def report(problems: list[str], title: str, names: set[str], detail: str) -> None:
    if names:
        problems.append(f"{title}: {', '.join(sorted(names))}\n    {detail}")


def main() -> int:
    engine, backend = read(ENGINE), read(BACKEND)

    emitted = engine_events(engine)
    handled = backend_events(backend)
    accepted = engine_commands(engine)
    sent = backend_commands(backend)

    if not emitted or not accepted or not handled or not sent:
        sys.exit("could not extract the interface; the source layout changed, "
                 "update tools/check_ipc_contract.py")

    problems: list[str] = []
    report(problems, "events the engine emits but the backend ignores",
           emitted - handled - EVENTS_WITHOUT_A_HANDLER,
           "add a branch in SinkBackend._on_btstack_event (src/backend.py)")
    report(problems, "events the backend expects but the engine never emits",
           handled - emitted,
           "emit them with emit_event() in btstack/btstack_sink.c, or drop the branch")
    report(problems, "commands the backend sends but the engine rejects",
           sent - accepted,
           "handle them in process_command() in btstack/btstack_sink.c")
    report(problems, "commands the engine accepts but nothing sends",
           accepted - sent,
           "send them from src/backend.py, or remove them from the engine")

    engine_slots, backend_slots = engine_argv_slots(engine), backend_argv_slots(backend)
    if backend_slots < 0:
        problems.append("could not find the argv list in _launch (src/backend.py)\n"
                        "    keep the 'argv layout' comment above it")
    elif engine_slots != backend_slots:
        problems.append(f"argv length differs: the engine reads {engine_slots} slots, "
                        f"the backend passes {backend_slots}\n"
                        "    mirror the change in main() (btstack/btstack_sink.c) and "
                        "_launch (src/backend.py)")

    if problems:
        print("IPC contract broken between btstack_sink.c and backend.py:\n")  # noqa: T201
        for problem in problems:
            print(f"  - {problem}\n")  # noqa: T201
        return 1

    print(f"IPC contract OK: {len(emitted)} events, {len(accepted)} commands, "  # noqa: T201
          f"{engine_slots} argv slots")
    return 0


if __name__ == "__main__":
    sys.exit(main())
