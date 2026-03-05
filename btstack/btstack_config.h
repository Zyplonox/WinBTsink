// btstack_config.h for btstack_sink (A2DP audio sink, Windows WinUSB)
#ifndef BTSTACK_CONFIG_H
#define BTSTACK_CONFIG_H

// ── Platform ─────────────────────────────────────────────────────────────────
#define HAVE_ASSERT
#define HAVE_MALLOC
#define HAVE_POSIX_FILE_IO
#define HAVE_POSIX_TIME

// ── Enable Classic BT only (no BLE, no Mesh) ─────────────────────────────────
#define ENABLE_CLASSIC
#define ENABLE_LOG_ERROR
#define ENABLE_LOG_INFO
#define ENABLE_SCO_OVER_HCI
#define ENABLE_SOFTWARE_AES128
#define ENABLE_PRINTF_HEXDUMP

// ── Buffer sizes ──────────────────────────────────────────────────────────────
#define HCI_ACL_PAYLOAD_SIZE         (1691 + 4)
#define HCI_INCOMING_PRE_BUFFER_SIZE 14

// ── Link key / device DB ──────────────────────────────────────────────────────
#define NVM_NUM_DEVICE_DB_ENTRIES    4
#define NVM_NUM_LINK_KEYS            4

// ── A2DP / AVDTP ─────────────────────────────────────────────────────────────
#define ENABLE_AVDTP_ACCEPTOR_EXPLICIT_START_STREAM_BB

#endif // BTSTACK_CONFIG_H
