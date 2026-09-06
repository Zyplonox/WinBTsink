"""
apply_patches.py — applies WinBTsink's source patches to a BTstack checkout.

Usage:
    python apply_patches.py <btstack_root>

Patches (each guarded by a marker comment, so re-running is a no-op):

  avdtp    Deferred-accept hook for incoming AVDTP (L2CAP PSM 25) connections.
           Stock BTstack calls l2cap_accept_connection() immediately; with the
           patch the application registers a callback that fires BEFORE the
           accept and must then call
               avdtp_accept_incoming_connection(local_cid)   or
               avdtp_decline_incoming_connection(local_cid).

  avrcp    Same hook for incoming AVRCP (L2CAP PSM 23) connections
           (avrcp_register_incoming_connection_handler & friends).

  winusb   hci_transport_usb_set_path_filter(const char *substring) for the
           Windows WinUSB transport. When set, usb_open() only tries devices
           whose device path contains the (case-insensitive) substring, so
           the application can pick one specific dongle.

A file is only written when every step of its patch succeeded; the pristine
copy is saved next to it as <name>.orig the first time it is modified.
A file that carries some but not all markers of a patch is reported as
half-patched and left untouched — restore it from the .orig backup.
"""

from __future__ import annotations

import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


class PatchError(Exception):
    """A required anchor was not found; nothing has been written."""


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

_INCLUDE_RE = re.compile(r'^#include\s+[<"][^>"]+[>"][ \t]*$', re.MULTILINE)
# Final "#endif" of an include guard, optionally followed by a trailing comment.
_LAST_ENDIF_RE = re.compile(r'\n#endif\s*(?:(?:/\*[^*]*\*/)|(?://[^\n]*))?[ \t]*$')


def insert_after_includes(text: str, block: str) -> str:
    """Inserts *block* after the last top-level #include line."""
    matches = list(_INCLUDE_RE.finditer(text))
    if not matches:
        raise PatchError("no #include lines found")
    pos = matches[-1].end()
    return text[:pos] + "\n\n" + block + text[pos:]


def insert_before_last_endif(text: str, block: str) -> str:
    """
    Inserts *block* into a BTstack header: before its "/* API_END */" marker
    (inside the extern "C" block) when present, else before the closing
    #endif of the include guard.
    """
    pos = text.find("/* API_END */")
    if pos < 0:
        m = _LAST_ENDIF_RE.search(text)
        if not m:
            raise PatchError("closing #endif not found")
        pos = m.start()
    return text[:pos] + "\n" + block + "\n" + text[pos:]


def replace_once(text: str, pattern: re.Pattern, repl: Callable[[re.Match], str],
                 what: str) -> str:
    """Replaces exactly one occurrence of *pattern*; errors when it is missing."""
    m = pattern.search(text)
    if not m:
        raise PatchError(f"anchor not found: {what}")
    return text[:m.start()] + repl(m) + text[m.end():]


@dataclass
class FilePatch:
    relpath: str
    markers: tuple[str, ...]              # every marker must be present after patching
    apply: Callable[[str], str]           # text -> text, raises PatchError


# ---------------------------------------------------------------------------
# Deferred-accept patch (parametrised for avdtp / avrcp)
# ---------------------------------------------------------------------------

def deferred_accept_patch(prefix: str) -> list[FilePatch]:
    """Builds the .c/.h patches that add <prefix>_*_incoming_connection()."""
    marker_globals = f"WinBTsink {prefix} deferred-accept extension"
    marker_callsite = f"WinBTsink {prefix} deferred-accept call-site"

    c_globals = f"""\
/* {marker_globals} --------------------------------------------------------- */
typedef void (*{prefix}_incoming_connection_handler_t)(uint16_t local_cid,
                                                     bd_addr_t addr);
static {prefix}_incoming_connection_handler_t {prefix}_incoming_connection_handler_cb = NULL;

void {prefix}_register_incoming_connection_handler(
        {prefix}_incoming_connection_handler_t handler) {{
    {prefix}_incoming_connection_handler_cb = handler;
}}

void {prefix}_accept_incoming_connection(uint16_t local_cid) {{
    l2cap_accept_connection(local_cid);
}}

void {prefix}_decline_incoming_connection(uint16_t local_cid) {{
    l2cap_decline_connection(local_cid);
}}
/* end {marker_globals} ----------------------------------------------------- */
"""

    h_decls = f"""\
/* {marker_globals} */
typedef void (*{prefix}_incoming_connection_handler_t)(uint16_t local_cid,
                                                     bd_addr_t addr);
void {prefix}_register_incoming_connection_handler(
        {prefix}_incoming_connection_handler_t handler);
void {prefix}_accept_incoming_connection(uint16_t local_cid);
void {prefix}_decline_incoming_connection(uint16_t local_cid);
/* end {marker_globals} */
"""

    # The first l2cap_accept_connection(local_cid) after the INCOMING_CONNECTION case.
    callsite_re = re.compile(
        r'(case\s+L2CAP_EVENT_INCOMING_CONNECTION\s*:.*?)'
        r'([ \t]*)l2cap_accept_connection\s*\(\s*local_cid\s*\)\s*;',
        re.DOTALL,
    )

    def patch_c(text: str) -> str:
        text = insert_after_includes(text, c_globals)

        def repl(m: re.Match) -> str:
            indent = m.group(2)
            return (
                f"{m.group(1)}{indent}/* {marker_callsite} */\n"
                f"{indent}if ({prefix}_incoming_connection_handler_cb) {{\n"
                f"{indent}    /* Application decides; it must call "
                f"{prefix}_accept/decline_incoming_connection() */\n"
                f"{indent}    {prefix}_incoming_connection_handler_cb(local_cid, event_addr);\n"
                f"{indent}}} else {{\n"
                f"{indent}    l2cap_accept_connection(local_cid);\n"
                f"{indent}}}"
            )

        return replace_once(text, callsite_re, repl,
                            "case L2CAP_EVENT_INCOMING_CONNECTION + l2cap_accept_connection(local_cid)")

    def patch_h(text: str) -> str:
        return insert_before_last_endif(text, h_decls)

    return [
        FilePatch(f"src/classic/{prefix}.c", (marker_globals, marker_callsite), patch_c),
        FilePatch(f"src/classic/{prefix}.h", (marker_globals,), patch_h),
    ]


# ---------------------------------------------------------------------------
# WinUSB device-path filter patch
# ---------------------------------------------------------------------------

_WINUSB_MARKER_GLOBALS = "WinBTsink winusb path-filter extension"
_WINUSB_MARKER_CALLSITE = "WinBTsink winusb path-filter call-site"

_WINUSB_C_GLOBALS = f"""\
/* {_WINUSB_MARKER_GLOBALS} ------------------------------------------------- */
static char winbtsink_usb_path_filter[256] = "";

void hci_transport_usb_set_path_filter(const char * filter){{
    size_t i = 0;
    if (filter != NULL){{
        for (; i < sizeof(winbtsink_usb_path_filter) - 1 && filter[i]; i++){{
            char c = filter[i];
            winbtsink_usb_path_filter[i] = (c >= 'A' && c <= 'Z') ? (char)(c + 32) : c;
        }}
    }}
    winbtsink_usb_path_filter[i] = 0;
}}

static int winbtsink_usb_path_matches(const char * device_path){{
    char lower[512];
    size_t i;
    if (winbtsink_usb_path_filter[0] == 0) return 1;
    for (i = 0; i < sizeof(lower) - 1 && device_path[i]; i++){{
        char c = device_path[i];
        lower[i] = (c >= 'A' && c <= 'Z') ? (char)(c + 32) : c;
    }}
    lower[i] = 0;
    return strstr(lower, winbtsink_usb_path_filter) != NULL;
}}
/* end {_WINUSB_MARKER_GLOBALS} --------------------------------------------- */
"""

_WINUSB_H_DECLS = f"""\
/* {_WINUSB_MARKER_GLOBALS} */
/* Only open USB devices whose device path contains this substring (case-insensitive).
 * NULL or "" clears the filter. Must be called before hci_power_control(HCI_POWER_ON). */
void hci_transport_usb_set_path_filter(const char * filter);
/* end {_WINUSB_MARKER_GLOBALS} */
"""

_WINUSB_CALLSITE_RE = re.compile(
    r'([ \t]*)BOOL result = usb_try_open_device\(DevIntfDetailData->DevicePath\);'
)


def _patch_winusb_c(text: str) -> str:
    text = insert_after_includes(text, _WINUSB_C_GLOBALS)

    def repl(m: re.Match) -> str:
        indent = m.group(1)
        return (
            f"{indent}/* {_WINUSB_MARKER_CALLSITE} */\n"
            f"{indent}BOOL result = FALSE;\n"
            f"{indent}if (!winbtsink_usb_path_matches(DevIntfDetailData->DevicePath)){{\n"
            f"{indent}    log_info(\"usb_open: device does not match path filter, skipping\");\n"
            f"{indent}}} else {{\n"
            f"{indent}    result = usb_try_open_device(DevIntfDetailData->DevicePath);\n"
            f"{indent}}}"
        )

    return replace_once(text, _WINUSB_CALLSITE_RE, repl,
                        "BOOL result = usb_try_open_device(DevIntfDetailData->DevicePath);")


def _patch_winusb_h(text: str) -> str:
    return insert_before_last_endif(text, _WINUSB_H_DECLS)


WINUSB_PATCH = [
    FilePatch("platform/windows/hci_transport_h2_winusb.c",
              (_WINUSB_MARKER_GLOBALS, _WINUSB_MARKER_CALLSITE), _patch_winusb_c),
    FilePatch("src/hci_transport_usb.h", (_WINUSB_MARKER_GLOBALS,), _patch_winusb_h),
]


PATCHES: dict[str, list[FilePatch]] = {
    "avdtp":  deferred_accept_patch("avdtp"),
    "avrcp":  deferred_accept_patch("avrcp"),
    "winusb": WINUSB_PATCH,
}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def apply_file_patch(root: Path, fp: FilePatch) -> bool:
    """Applies one FilePatch. Returns True on success or already-patched."""
    path = root / fp.relpath
    if not path.exists():
        print(f"  ERROR: {path} not found")
        return False

    text = path.read_text(encoding="utf-8")
    present = [m in text for m in fp.markers]
    if all(present):
        print(f"  {fp.relpath}: already patched")
        return True
    if any(present):
        print(f"  ERROR: {fp.relpath} is half-patched (markers {present}); "
              f"restore it from {path.name}.orig and re-run")
        return False

    try:
        new_text = fp.apply(text)
    except PatchError as exc:
        print(f"  ERROR: {fp.relpath}: {exc}")
        return False

    missing = [m for m in fp.markers if m not in new_text]
    if missing:   # defensive: a patch that does not leave its own markers
        print(f"  ERROR: {fp.relpath}: patch did not produce markers {missing}")
        return False

    backup = path.with_name(path.name + ".orig")
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text(new_text, encoding="utf-8")
    print(f"  {fp.relpath}: patched")
    return True


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    root = Path(sys.argv[1])
    if not (root / "src" / "btstack.h").exists():
        print(f"ERROR: {root} does not look like a BTstack checkout (src/btstack.h missing)")
        return 1

    ok = True
    for name, files in PATCHES.items():
        print(f"Patch '{name}':")
        for fp in files:
            ok = apply_file_patch(root, fp) and ok

    if ok:
        print("\nAll patches applied.")
        return 0
    print("\nSome patches FAILED — see errors above. Unpatched files were not modified.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
