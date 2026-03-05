"""
apply_avdtp_patch.py — Patches BTstack's avdtp.c and avdtp.h to add a
deferred-accept callback API for incoming AVDTP (L2CAP PSM=25) connections.

Without this patch, avdtp.c calls l2cap_accept_connection() immediately
when an L2CAP connection request arrives, before the application can
decide to approve or deny it.

After this patch, the application can register a callback:
    avdtp_register_incoming_connection_handler(my_callback);

When a connection arrives, my_callback fires BEFORE acceptance. The app
can then call:
    avdtp_accept_incoming_connection(local_cid);   // proceed
    avdtp_decline_incoming_connection(local_cid);  // reject

Usage:
    python apply_avdtp_patch.py <btstack_root>

Example:
    python apply_avdtp_patch.py btstack-src
"""

import sys
import os
import re
import shutil
from pathlib import Path


# ── New code to inject into avdtp.c ──────────────────────────────────────────

AVDTP_C_NEW_GLOBALS = """\
/* WinBTsink deferred-accept extension --------------------------------------- */
typedef void (*avdtp_incoming_connection_handler_t)(uint16_t local_cid,
                                                     bd_addr_t addr);
static avdtp_incoming_connection_handler_t avdtp_incoming_connection_handler_cb = NULL;

void avdtp_register_incoming_connection_handler(
        avdtp_incoming_connection_handler_t handler) {
    avdtp_incoming_connection_handler_cb = handler;
}

void avdtp_accept_incoming_connection(uint16_t local_cid) {
    l2cap_accept_connection(local_cid);
}

void avdtp_decline_incoming_connection(uint16_t local_cid) {
    l2cap_decline_connection(local_cid);
}
/* end WinBTsink deferred-accept extension ----------------------------------- */
"""

# ── New declarations to inject into avdtp.h ──────────────────────────────────

AVDTP_H_NEW_DECLS = """\
/* WinBTsink deferred-accept extension */
typedef void (*avdtp_incoming_connection_handler_t)(uint16_t local_cid,
                                                     bd_addr_t addr);
void avdtp_register_incoming_connection_handler(
        avdtp_incoming_connection_handler_t handler);
void avdtp_accept_incoming_connection(uint16_t local_cid);
void avdtp_decline_incoming_connection(uint16_t local_cid);
/* end WinBTsink deferred-accept extension */
"""


def patch_avdtp_c(path: Path) -> bool:
    """
    Patches avdtp.c:
      1. Inserts the new global functions after the #include block.
      2. Replaces the bare `l2cap_accept_connection(local_cid);` call inside
         the L2CAP_EVENT_INCOMING_CONNECTION handler with a conditional that
         calls the callback (if registered) instead.

    Returns True on success, False if the file was already patched or a
    required anchor string was not found.
    """
    text = path.read_text(encoding="utf-8")

    # ── Idempotency check ────────────────────────────────────────────────────
    if "WinBTsink deferred-accept extension" in text:
        print(f"  {path.name}: already patched — skipping.")
        return True

    # ── 1. Insert global functions ───────────────────────────────────────────
    # Anchor: insert after the last top-level #include line in the file.
    # We look for the last '#include' that appears before any function definition.
    include_pattern = re.compile(r'^#include\s+[<"][^>"]+[>"]\s*$', re.MULTILINE)
    matches = list(include_pattern.finditer(text))
    if not matches:
        print(f"  ERROR: no #include lines found in {path.name}")
        return False

    insert_pos = matches[-1].end()
    text = text[:insert_pos] + "\n\n" + AVDTP_C_NEW_GLOBALS + text[insert_pos:]

    # ── 2. Replace l2cap_accept_connection(local_cid) in INCOMING_CONNECTION ─
    # Strategy: find the L2CAP_EVENT_INCOMING_CONNECTION case block, then
    # replace the FIRST occurrence of l2cap_accept_connection(local_cid) after it.
    incoming_pattern = re.compile(
        r'(case\s+L2CAP_EVENT_INCOMING_CONNECTION\s*:.*?)'
        r'(l2cap_accept_connection\s*\(\s*local_cid\s*\)\s*;)',
        re.DOTALL
    )
    match = incoming_pattern.search(text)
    if not match:
        print(
            f"  ERROR: could not find 'case L2CAP_EVENT_INCOMING_CONNECTION' + "
            f"'l2cap_accept_connection(local_cid)' pattern in {path.name}\n"
            f"  You will need to apply this change manually — see comments below."
        )
        # Still write the file with the globals inserted (partial patch)
        path.write_text(text, encoding="utf-8")
        return False

    replacement = (
        match.group(1)
        + "if (avdtp_incoming_connection_handler_cb) {\n"
        + "            /* Notify application; it must call avdtp_accept/decline_incoming_connection() */\n"
        + "            avdtp_incoming_connection_handler_cb(local_cid, event_addr);\n"
        + "        } else {\n"
        + "            l2cap_accept_connection(local_cid);\n"
        + "        }"
    )
    text = text[:match.start()] + replacement + text[match.end():]

    # ── Write ────────────────────────────────────────────────────────────────
    backup = path.with_suffix(".c.orig")
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text(text, encoding="utf-8")
    print(f"  {path.name}: patched successfully.")
    return True


def patch_avdtp_h(path: Path) -> bool:
    """
    Patches avdtp.h: inserts the new function declarations before the
    closing #endif of the include guard.
    """
    text = path.read_text(encoding="utf-8")

    if "WinBTsink deferred-accept extension" in text:
        print(f"  {path.name}: already patched — skipping.")
        return True

    # Insert before the final #endif (handles both /* */ and // style comments)
    endif_pattern = re.compile(r'\n#endif\s*(?:(?:/\*[^*]*\*/)|(?://[^\n]*))?[ \t]*$')
    match = endif_pattern.search(text)
    if not match:
        print(f"  ERROR: could not find closing #endif in {path.name}")
        return False

    insert_pos = match.start()
    text = text[:insert_pos] + "\n" + AVDTP_H_NEW_DECLS + text[insert_pos:]

    backup = path.with_suffix(".h.orig")
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text(text, encoding="utf-8")
    print(f"  {path.name}: patched successfully.")
    return True


def main():
    if len(sys.argv) < 2:
        print("Usage: python apply_avdtp_patch.py <btstack_root>")
        sys.exit(1)

    btstack_root = Path(sys.argv[1])
    avdtp_c = btstack_root / "src" / "classic" / "avdtp.c"
    avdtp_h = btstack_root / "src" / "classic" / "avdtp.h"

    for p in (avdtp_c, avdtp_h):
        if not p.exists():
            print(f"ERROR: {p} not found. Is btstack_root correct?")
            sys.exit(1)

    print("Patching BTstack AVDTP for deferred-accept support...")
    ok_c = patch_avdtp_c(avdtp_c)
    ok_h = patch_avdtp_h(avdtp_h)

    if ok_c and ok_h:
        print("\nPatch applied successfully.")
    else:
        print(
            "\nPartial patch — see errors above.\n"
            "Manual changes needed in avdtp.c:\n"
            "  Find: case L2CAP_EVENT_INCOMING_CONNECTION:\n"
            "  Find the line: l2cap_accept_connection(local_cid);\n"
            "  Replace with the conditional block from AVDTP_C_NEW_GLOBALS above."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
