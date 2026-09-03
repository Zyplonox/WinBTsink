// btstack_config.h for btstack_sink (A2DP audio sink, Windows WinUSB)
#ifndef BTSTACK_CONFIG_H
#define BTSTACK_CONFIG_H

// ── Platform ─────────────────────────────────────────────────────────────────
#define HAVE_ASSERT
#define HAVE_MALLOC
#define HAVE_POSIX_FILE_IO
#define HAVE_POSIX_TIME

// ── Enable Classic BT only (no BLE, no Mesh, no SCO/HFP) ─────────────────────
// ENABLE_SCO_OVER_HCI is deliberately off: nothing here uses SCO, and with it
// the WinUSB transport refuses dongles that lack a second USB interface.
#define ENABLE_CLASSIC
#define ENABLE_LOG_ERROR
#define ENABLE_LOG_INFO
#define ENABLE_SOFTWARE_AES128
#define ENABLE_PRINTF_HEXDUMP

// ── Buffer sizes ──────────────────────────────────────────────────────────────
#define HCI_ACL_PAYLOAD_SIZE         (1691 + 4)
#define HCI_INCOMING_PRE_BUFFER_SIZE 14

// ── Link key store ────────────────────────────────────────────────────────────
// Number of bonded devices remembered in the TLV key store. The oldest key is
// evicted when it is full, so keep this comfortably above MAX_CONNECTIONS.
#define NVM_NUM_LINK_KEYS            16

#endif // BTSTACK_CONFIG_H
