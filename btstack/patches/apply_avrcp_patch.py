"""
apply_avrcp_patch.py — Patches BTstack's avrcp.c and avrcp.h to add a
deferred-accept callback API for incoming AVRCP (L2CAP PSM=23) connections.

Without this patch, avrcp.c calls l2cap_accept_connection() immediately
when an L2CAP connection request arrives, before the application can
decide to approve or deny it.

After this patch, the application can register a callback:
    avrcp_register_incoming_connection_handler(my_callback);

When a connection arrives, my_callback fires BEFORE acceptance. The app
can then call:
    avrcp_accept_incoming_connection(local_cid);   // proceed
    avrcp_decline_incoming_connection(local_cid);  // reject

Usage:
    python apply_avrcp_patch.py <btstack_root>

Example:
    python apply_avrcp_patch.py btstack-src
"""

import sys
import os
import re
import shutil
from pathlib import Path


# ── New code to inject into avrcp.c ──────────────────────────────────────────

AVRCP_C_NEW_GLOBALS = """\
/* WinBTsink AVRCP deferred-accept extension --------------------------------- */
typedef void (*avrcp_incoming_connection_handler_t)(uint16_t local_cid,
                                                     bd_addr_t addr);
static avrcp_incoming_connection_handler_t avrcp_incoming_connection_handler_cb = NULL;

void avrcp_register_incoming_connection_handler(
        avrcp_incoming_connection_handler_t handler) {
    avrcp_incoming_connection_handler_cb = handler;
}

void avrcp_accept_incoming_connection(uint16_t local_cid) {
    l2cap_accept_connection(local_cid);
}

void avrcp_decline_incoming_connection(uint16_t local_cid) {
    l2cap_decline_connection(local_cid);
}
/* end WinBTsink AVRCP deferred-accept extension ----------------------------- */
"""

# ── New declarations to inject into avrcp.h ──────────────────────────────────

AVRCP_H_NEW_DECLS = """\
/* WinBTsink AVRCP deferred-accept extension */
typedef void (*avrcp_incoming_connection_handler_t)(uint16_t local_cid,
                                                     bd_addr_t addr);
void avrcp_register_incoming_connection_handler(
        avrcp_incoming_connection_handler_t handler);
void avrcp_accept_incoming_connection(uint16_t local_cid);
void avrcp_decline_incoming_connection(uint16_t local_cid);
/* end WinBTsink AVRCP deferred-accept extension */
"""


def patch_avrcp_c(path: Path) -> bool:
    """
    Patches avrcp.c:
      1. Inserts the new global functions after the #include block.
      2. Replaces the bare `l2cap_accept_connection(local_cid);` call inside
         the L2CAP_EVENT_INCOMING_CONNECTION handler with a conditional that
         calls the callback (if registered) instead.

    Returns True on success, False if the file was already patched or a
    required anchor string was not found.
    """
    text = path.read_text(encoding="utf-8")

    # ── Idempotency check ────────────────────────────────────────────────────
    if "WinBTsink AVRCP deferred-accept extension" in text:
        print(f"  {path.name}: already patched — skipping.")
        return True

    # ── 1. Insert global functions ───────────────────────────────────────────
    # Anchor: insert after the last top-level #include line in the file.
    include_pattern = re.compile(r'^#include\s+[<"][^>"]+[>"]\s*$', re.MULTILINE)
    matches = list(include_pattern.finditer(text))
    if not matches:
        print(f"  ERROR: no #include lines found in {path.name}")
        return False

    insert_pos = matches[-1].end()
    text = text[:insert_pos] + "\n\n" + AVRCP_C_NEW_GLOBALS + text[insert_pos:]

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
        + "if (avrcp_incoming_connection_handler_cb) {\n"
        + "                        /* Notify application; it must call avrcp_accept/decline_incoming_connection() */\n"
        + "                        avrcp_incoming_connection_handler_cb(local_cid, event_addr);\n"
        + "                    } else {\n"
        + "                        l2cap_accept_connection(local_cid);\n"
        + "                    }"
    )
    text = text[:match.start()] + replacement + text[match.end():]

    # ── Write ────────────────────────────────────────────────────────────────
    backup = path.with_suffix(".c.orig")
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text(text, encoding="utf-8")
    print(f"  {path.name}: patched successfully.")
    return True


def patch_avrcp_h(path: Path) -> bool:
    """
    Patches avrcp.h: inserts the new function declarations before the
    closing #endif of the include guard.
    """
    text = path.read_text(encoding="utf-8")

    if "WinBTsink AVRCP deferred-accept extension" in text:
        print(f"  {path.name}: already patched — skipping.")
        return True

    # Insert before the final #endif (handles both /* */ and // style comments)
    endif_pattern = re.compile(r'\n#endif\s*(?:(?:/\*[^*]*\*/)|(?://[^\n]*))?[ \t]*$')
    match = endif_pattern.search(text)
    if not match:
        print(f"  ERROR: could not find closing #endif in {path.name}")
        return False

    insert_pos = match.start()
    text = text[:insert_pos] + "\n" + AVRCP_H_NEW_DECLS + text[insert_pos:]

    backup = path.with_suffix(".h.orig")
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text(text, encoding="utf-8")
    print(f"  {path.name}: patched successfully.")
    return True


def main():
    if len(sys.argv) < 2:
        print("Usage: python apply_avrcp_patch.py <btstack_root>")
        sys.exit(1)

    btstack_root = Path(sys.argv[1])
    avrcp_c = btstack_root / "src" / "classic" / "avrcp.c"
    avrcp_h = btstack_root / "src" / "classic" / "avrcp.h"

    for p in (avrcp_c, avrcp_h):
        if not p.exists():
            print(f"ERROR: {p} not found. Is btstack_root correct?")
            sys.exit(1)

    print("Patching BTstack AVRCP for deferred-accept support...")
    ok_c = patch_avrcp_c(avrcp_c)
    ok_h = patch_avrcp_h(avrcp_h)

    if ok_c and ok_h:
        print("\nPatch applied successfully.")
    else:
        print(
            "\nPartial patch — see errors above.\n"
            "Manual changes needed in avrcp.c:\n"
            "  Find: case L2CAP_EVENT_INCOMING_CONNECTION:\n"
            "  Find the line: l2cap_accept_connection(local_cid);\n"
            "  Replace with the conditional block from AVRCP_C_NEW_GLOBALS above."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
