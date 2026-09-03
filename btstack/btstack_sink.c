/*
 * btstack_sink.c — WinBTsink Bluetooth A2DP Sink using BTstack
 * ============================================================
 *
 * Launched as a subprocess by backend.py.
 *
 * stdin  (text):   JSON command lines from Python
 *                  {"cmd":"approve","addr":"XX:XX:XX:XX:XX:XX","cid":64}
 *                  {"cmd":"deny","addr":"XX:XX:XX:XX:XX:XX","cid":64}
 *                  {"cmd":"set_discoverable","enabled":true}
 *                  {"cmd":"set_volume","addr":"XX:XX:XX:XX:XX:XX","volume":90}
 *                  {"cmd":"player","addr":"XX:XX:XX:XX:XX:XX","action":"play|pause|stop|next|prev"}
 *                  {"cmd":"forget_key","addr":"XX:XX:XX:XX:XX:XX"}   drop the bonding key
 *                  {"cmd":"stop"}
 *
 * stdout (binary): Audio frames, each prefixed by a header:
 *                  [uint32_le total_len = 6 + audio_len]
 *                  [6 bytes bd_addr (big-endian, MSB first)]
 *                  [audio_len bytes payload (SBC or AAC-LATM)]
 *
 * stderr (text):   JSON event lines to Python
 *                  {"event":"ready","address":"AA:BB:CC:DD:EE:FF"}
 *                  {"event":"l2cap_request","addr":"...","cid":64}
 *                  {"event":"connected","addr":"..."}
 *                  {"event":"name","addr":"...","name":"iPhone"}     (async, after connected)
 *                  {"event":"disconnected","addr":"..."}
 *                  {"event":"audio_start","addr":"...","sample_rate":44100,"channels":2,"codec":"sbc",
 *                   "block_length":16,"subbands":8,"allocation":"loudness","bitpool":53}
 *                   (the four SBC fields are present for codec "sbc" only)
 *                  {"event":"audio_stop","addr":"..."}
 *                  {"event":"volume_changed","addr":"...","volume":90}
 *                  {"event":"metadata","addr":"...","title":"...","artist":"...","album":"..."}
 *                  {"event":"playback","addr":"...","status":"playing|paused|stopped|seeking|error"}
 *                  {"event":"log","msg":"..."}
 *                  {"event":"error","msg":"..."}
 *                  All string values are JSON-escaped.
 *
 * Command-line arguments (all optional, positional):
 *   btstack_sink.exe <usb_filter> <device_name> <max_bitpool> <debug> <cod_hex> <keystore_path>
 *                    <sbc_block_length> <sbc_subbands> <sbc_allocation> <vendor_codecs>
 *     usb_filter     case-insensitive substring of the WinUSB device path that
 *                    selects the dongle, e.g. "vid_0a12&pid_0001#5&2c1f8b6&0&3#".
 *                    Empty = first Bluetooth dongle found.
 *     device_name    advertised Bluetooth name
 *     max_bitpool    SBC max bitpool advertised in the sink capabilities
 *     debug          1 = verbose protocol logging
 *     cod_hex        Class of Device, hex without prefix (e.g. 240418)
 *     keystore_path  full path of the link-key TLV file. Empty = btstack_keys.db
 *                    next to this executable.
 *     sbc_block_length  4, 8, 12, 16 or 0 = offer all (source chooses)
 *     sbc_subbands      4, 8 or 0 = offer all
 *     sbc_allocation    1 = loudness, 2 = SNR, 0 = offer both
 *     vendor_codecs     bitmask of extra codecs to offer: 1 = aptX, 2 = aptX HD
 *                       (audio_start then reports codec "aptx" / "aptx_hd")
 *   The SBC settings restrict the capabilities the sink advertises; A2DP
 *   sources must support every SBC block length, subband count and
 *   allocation method, so the source then encodes with exactly these values.
 *
 * Shutting down: send {"cmd":"stop"} or simply close stdin; both power off
 * HCI and exit the run loop.
 */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <windows.h>
#include <io.h>
#include <fcntl.h>
#include <process.h>

/* BTstack headers */
#include "btstack.h"
#include "btstack_run_loop_windows.h"
#include "hci_transport_usb.h"
#include "classic/a2dp_sink.h"
#include "classic/avdtp.h"
#include "classic/avrcp.h"
#include "classic/avrcp_target.h"
#include "classic/avrcp_controller.h"
#include "classic/sdp_server.h"
#include "classic/a2dp.h"
#include "classic/avdtp_util.h"
#include "bluetooth_sdp.h"
#include "hci_cmd.h"
#include "btstack_tlv.h"
#include "btstack_tlv_windows.h"
#include "classic/btstack_link_key_db_tlv.h"

/* btstack_crypto.c references hci_le_rand (an LE HCI command descriptor)
 * even in Classic-only builds. Provide the stub so the linker is satisfied.
 * In a Classic-only build this command is never actually sent. */
#ifndef ENABLE_BLE
const hci_cmd_t hci_le_rand = { 0x2018u, "" };
#endif

/* -------------------------------------------------------------------------
 * Configuration
 * ---------------------------------------------------------------------- */

#define MAX_CONNECTIONS         4       /* max simultaneous A2DP sources */
#define META_BUF_SIZE           131     /* AVRCP max attribute size + NUL */
#define JSON_ESC_FACTOR         6       /* worst case: one byte -> \u00XX */
#define PRE_APPROVE_TTL_MS      15000   /* how long an AVDTP approval pre-approves AVRCP */

/* Vendor codecs (AVDTP_CODEC_NON_A2DP). Media codec information layout:
 *   [vendor id, 4 bytes LE][codec id, 2 bytes LE][codec specific]
 * aptX:    codec specific = 1 byte: sample rate (bits 7..4) | channels (bits 3..0)
 * aptX HD: same byte followed by 4 reserved bytes */
#define VENDOR_APTX             1
#define VENDOR_APTX_HD          2
#define APTX_VENDOR_ID          0x0000004Fu   /* APT Ltd */
#define APTX_CODEC_ID           0x0001u
#define APTX_HD_VENDOR_ID       0x000000D7u   /* Qualcomm Technologies International */
#define APTX_HD_CODEC_ID        0x0024u
#define APTX_RATE_16000         0x80
#define APTX_RATE_32000         0x40
#define APTX_RATE_44100         0x20
#define APTX_RATE_48000         0x10
#define APTX_CH_STEREO          0x02
#define APTX_CH_MONO            0x01

/* -------------------------------------------------------------------------
 * Per-connection state
 * ---------------------------------------------------------------------- */

typedef struct {
    int      active;            /* slot in use */
    uint16_t a2dp_cid;          /* BTstack A2DP connection identifier */
    uint16_t avrcp_cid;         /* BTstack AVRCP connection identifier (0 = not yet) */
    uint8_t  local_seid;        /* local stream endpoint ID used by this conn */
    uint8_t  codec_type;        /* AVDTP_CODEC_SBC, AVDTP_CODEC_MPEG_2_4_AAC or AVDTP_CODEC_NON_A2DP */
    uint8_t  vendor_codec;      /* VENDOR_APTX / VENDOR_APTX_HD when codec_type is NON_A2DP */
    bd_addr_t addr;             /* remote device address */
    char     addr_str[18];      /* "XX:XX:XX:XX:XX:XX" */
    int      sample_rate;
    int      channels;
    /* Negotiated SBC parameters (valid when codec_type == AVDTP_CODEC_SBC) */
    int      sbc_block_length;
    int      sbc_subbands;
    int      sbc_allocation;    /* AVDTP_SBC_ALLOCATION_METHOD_LOUDNESS / _SNR */
    int      sbc_bitpool;       /* max bitpool the source may use */
    /* AVRCP "Now Playing" metadata (accumulated per info-done event) */
    char     meta_title[META_BUF_SIZE];
    char     meta_artist[META_BUF_SIZE];
    char     meta_album[META_BUF_SIZE];
    int      name_pending;      /* remote name request still to be issued/answered */
} a2dp_conn_t;

static a2dp_conn_t g_conns[MAX_CONNECTIONS];

/* Per-SEP SBC config buffers (one per registered endpoint) */
static uint8_t g_sbc_cfg[MAX_CONNECTIONS][4];

/* Per-SEP AAC config buffers */
static uint8_t g_aac_cfg[MAX_CONNECTIONS][6];

/* Per-SEP vendor codec config buffers. BTstack only reports a configuration
 * when the buffer length equals the codec information length exactly. */
static uint8_t g_aptx_cfg[MAX_CONNECTIONS][7];
static uint8_t g_aptxhd_cfg[MAX_CONNECTIONS][11];

/* -------------------------------------------------------------------------
 * Pending AVDTP L2CAP connections awaiting Python approve/deny
 * ---------------------------------------------------------------------- */

typedef struct {
    int      valid;
    uint16_t l2cap_cid;
    bd_addr_t addr;
} pending_conn_t;

static pending_conn_t g_pending[MAX_CONNECTIONS];

/* -------------------------------------------------------------------------
 * AVRCP bookkeeping
 *
 * AVRCP uses its own L2CAP PSM and may connect before, during, or after the
 * AVDTP approval round-trip, so two small tables track it:
 *
 * g_pending_avrcp[]  AVRCP L2CAP connection gated on the AVDTP decision.
 *   valid=0  empty
 *   valid=1  AVRCP l2cap_cid parked, waiting for AVDTP approve/deny
 *   valid=2  AVDTP approved but AVRCP has not arrived yet (pre-approval).
 *            Expires after PRE_APPROVE_TTL_MS so a stale approval can never
 *            let a later AVRCP connection bypass the gate.
 *
 * g_early_avrcp[]    AVRCP connection *established* while no A2DP connection
 *                    exists for that address. Promoted into conn->avrcp_cid
 *                    when A2DP establishes; A2DP release parks it here again.
 * ---------------------------------------------------------------------- */

typedef struct {
    int      valid;
    uint16_t l2cap_cid;
    uint8_t  addr[6];
    uint32_t created_ms;   /* run-loop time when valid=2 was set */
} pending_avrcp_t;

static pending_avrcp_t g_pending_avrcp[MAX_CONNECTIONS];

typedef struct { uint8_t addr[6]; uint16_t cid; int valid; } avrcp_early_t;
static avrcp_early_t g_early_avrcp[MAX_CONNECTIONS];

/* -------------------------------------------------------------------------
 * Global state
 * ---------------------------------------------------------------------- */

static char     g_device_name[64]  = "PC-AudioSink";
static char     g_usb_filter[256]  = "";
static char     g_keystore_path[MAX_PATH] = "";
static int      g_max_bitpool      = 53;
static int      g_sbc_block_length = 0;  /* 4/8/12/16, 0 = offer all */
static int      g_sbc_subbands     = 0;  /* 4/8, 0 = offer all */
static int      g_sbc_allocation   = 0;  /* 1 = loudness, 2 = SNR, 0 = offer both */
static int      g_vendor_codecs    = 0;  /* bitmask: VENDOR_APTX | VENDOR_APTX_HD */
static int      g_discoverable     = 0;  /* set via cmd after ready */
static int      g_debug            = 0;  /* verbose protocol logging when 1 */
static uint32_t g_cod              = 0x240418; /* Class of Device: Headphones */

/* Bonding / link key persistence via Windows TLV store */
static btstack_tlv_windows_t    g_tlv_context;
static const btstack_tlv_t     *g_tlv_impl = NULL;

/* Set when we intentionally power off (stop command) — suppresses the
   HCI_STATE_OFF error that would otherwise fire during normal shutdown. */
static int g_shutdown_requested = 0;

/* SDP records (buffers must outlive sdp_register_service) */
static uint8_t  g_sdp_a2dp_sink_service[150];
static uint8_t  g_sdp_avrcp_tg_service[200];   /* AVRCP Target */
static uint8_t  g_sdp_avrcp_ct_service[200];   /* AVRCP Controller */

/* stdin reader thread → run loop hand-off */
static CRITICAL_SECTION      g_cs;
static btstack_data_source_t g_stdin_ds;
static HANDLE                g_stdin_event;   /* manual-reset, signalled by reader thread */

/* Ring buffer for commands arriving from Python */
#define CMD_BUF_LINES 16
#define CMD_LINE_MAX  256
static char   g_cmd_buf[CMD_BUF_LINES][CMD_LINE_MAX];
static int    g_cmd_head = 0;
static int    g_cmd_tail = 0;
static int    g_cmd_dropped = 0;   /* lines lost to ring-buffer overflow */

/* -------------------------------------------------------------------------
 * JSON emit helpers
 * ---------------------------------------------------------------------- */

static void emit_event(const char *json) {
    fprintf(stderr, "%s\n", json);
    fflush(stderr);
}

/* Escape src (len bytes, need not be NUL-terminated) as a JSON string body
 * into dst. dst_size must be >= len * JSON_ESC_FACTOR + 1 to be lossless;
 * output is truncated on a character boundary otherwise. */
static void json_escape(char *dst, size_t dst_size, const uint8_t *src, size_t len) {
    static const char hex[] = "0123456789abcdef";
    size_t o = 0;
    for (size_t i = 0; i < len && src[i]; i++) {
        uint8_t c = src[i];
        const char *simple = NULL;
        switch (c) {
            case '"':  simple = "\\\""; break;
            case '\\': simple = "\\\\"; break;
            case '\n': simple = "\\n";  break;
            case '\r': simple = "\\r";  break;
            case '\t': simple = "\\t";  break;
            default: break;
        }
        if (simple) {
            if (o + 2 >= dst_size) break;
            dst[o++] = simple[0];
            dst[o++] = simple[1];
        } else if (c < 0x20) {
            if (o + 6 >= dst_size) break;
            dst[o++] = '\\'; dst[o++] = 'u'; dst[o++] = '0'; dst[o++] = '0';
            dst[o++] = hex[c >> 4]; dst[o++] = hex[c & 0x0F];
        } else {
            if (o + 1 >= dst_size) break;
            dst[o++] = (char)c;
        }
    }
    dst[o] = '\0';
}

static void emit_log(const char *msg) {
    char esc[512];
    json_escape(esc, sizeof(esc), (const uint8_t *)msg, strlen(msg));
    fprintf(stderr, "{\"event\":\"log\",\"msg\":\"%s\"}\n", esc);
    fflush(stderr);
}

/* Format a bd_addr_t as "XX:XX:XX:XX:XX:XX" into buf (must be >=18 bytes). */
static void addr_to_str(const bd_addr_t addr, char *buf) {
    snprintf(buf, 18, "%02X:%02X:%02X:%02X:%02X:%02X",
             addr[0], addr[1], addr[2], addr[3], addr[4], addr[5]);
}

/* Copy a length-delimited AVRCP attribute into a NUL-terminated buffer. */
static void copy_attr(char *dst, size_t dst_size, const uint8_t *val, uint8_t len) {
    size_t copy = (len < dst_size - 1) ? len : dst_size - 1;
    memcpy(dst, val, copy);
    dst[copy] = '\0';
}

/* -------------------------------------------------------------------------
 * Connection slot helpers
 * ---------------------------------------------------------------------- */

static a2dp_conn_t *find_conn_by_cid(uint16_t cid) {
    for (int i = 0; i < MAX_CONNECTIONS; i++) {
        if (g_conns[i].active && g_conns[i].a2dp_cid == cid)
            return &g_conns[i];
    }
    return NULL;
}

static a2dp_conn_t *find_conn_by_seid(uint8_t seid) {
    for (int i = 0; i < MAX_CONNECTIONS; i++) {
        if (g_conns[i].active && g_conns[i].local_seid == seid)
            return &g_conns[i];
    }
    return NULL;
}

static a2dp_conn_t *find_conn_by_addr(const bd_addr_t addr) {
    for (int i = 0; i < MAX_CONNECTIONS; i++) {
        if (g_conns[i].active && memcmp(g_conns[i].addr, addr, 6) == 0)
            return &g_conns[i];
    }
    return NULL;
}

static a2dp_conn_t *find_conn_by_avrcp_cid(uint16_t avrcp_cid) {
    for (int i = 0; i < MAX_CONNECTIONS; i++) {
        if (g_conns[i].active && g_conns[i].avrcp_cid == avrcp_cid)
            return &g_conns[i];
    }
    return NULL;
}

static a2dp_conn_t *alloc_conn(void) {
    for (int i = 0; i < MAX_CONNECTIONS; i++) {
        if (!g_conns[i].active) {
            memset(&g_conns[i], 0, sizeof(g_conns[i]));
            g_conns[i].active      = 1;
            g_conns[i].sample_rate = 44100;
            g_conns[i].channels    = 2;
            g_conns[i].codec_type  = AVDTP_CODEC_SBC;
            return &g_conns[i];
        }
    }
    return NULL;
}

/* -------------------------------------------------------------------------
 * Early-AVRCP table helpers
 * ---------------------------------------------------------------------- */

static avrcp_early_t *find_early_avrcp_by_addr(const uint8_t *addr) {
    for (int i = 0; i < MAX_CONNECTIONS; i++) {
        if (g_early_avrcp[i].valid && memcmp(g_early_avrcp[i].addr, addr, 6) == 0)
            return &g_early_avrcp[i];
    }
    return NULL;
}

static avrcp_early_t *find_early_avrcp_by_cid(uint16_t cid) {
    for (int i = 0; i < MAX_CONNECTIONS; i++) {
        if (g_early_avrcp[i].valid && g_early_avrcp[i].cid == cid)
            return &g_early_avrcp[i];
    }
    return NULL;
}

/* Remember an established AVRCP cid for an address without an A2DP conn. */
static void park_early_avrcp(const uint8_t *addr, uint16_t cid) {
    avrcp_early_t *e = find_early_avrcp_by_addr(addr);
    if (!e) {
        for (int i = 0; i < MAX_CONNECTIONS; i++) {
            if (!g_early_avrcp[i].valid) { e = &g_early_avrcp[i]; break; }
        }
    }
    if (!e) {
        emit_log("avrcp: early table full, dropping connection reference");
        return;
    }
    memcpy(e->addr, addr, 6);
    e->cid   = cid;
    e->valid = 1;
}

/* Release an A2DP slot. A still-open AVRCP channel goes back to the early
 * table so a later A2DP reconnect from the same device finds it again. */
static void free_conn(a2dp_conn_t *conn) {
    if (!conn) return;
    if (conn->avrcp_cid) park_early_avrcp(conn->addr, conn->avrcp_cid);
    memset(conn, 0, sizeof(*conn));
}

/* -------------------------------------------------------------------------
 * Pending AVDTP connection helpers
 * ---------------------------------------------------------------------- */

static pending_conn_t *alloc_pending(void) {
    for (int i = 0; i < MAX_CONNECTIONS; i++) {
        if (!g_pending[i].valid) return &g_pending[i];
    }
    return NULL;
}

static pending_conn_t *find_pending_by_cid(uint16_t cid) {
    for (int i = 0; i < MAX_CONNECTIONS; i++) {
        if (g_pending[i].valid && g_pending[i].l2cap_cid == cid)
            return &g_pending[i];
    }
    return NULL;
}

/* -------------------------------------------------------------------------
 * Pending AVRCP helpers
 * ---------------------------------------------------------------------- */

static pending_avrcp_t *find_pending_avrcp(const uint8_t *addr, int state) {
    uint32_t now = btstack_run_loop_get_time_ms();
    for (int i = 0; i < MAX_CONNECTIONS; i++) {
        pending_avrcp_t *p = &g_pending_avrcp[i];
        if (p->valid == 2 && (now - p->created_ms) > PRE_APPROVE_TTL_MS) {
            p->valid = 0;   /* expired pre-approval */
            continue;
        }
        if (p->valid == state && memcmp(p->addr, addr, 6) == 0)
            return p;
    }
    return NULL;
}

static pending_avrcp_t *alloc_pending_avrcp(void) {
    for (int i = 0; i < MAX_CONNECTIONS; i++) {
        if (!g_pending_avrcp[i].valid) return &g_pending_avrcp[i];
    }
    return NULL;
}

/* Forget a pre-approval (valid=2) for an address; parked channels are kept. */
static void clear_preapproval_for_addr(const uint8_t *addr) {
    for (int i = 0; i < MAX_CONNECTIONS; i++) {
        pending_avrcp_t *p = &g_pending_avrcp[i];
        if (p->valid == 2 && memcmp(p->addr, addr, 6) == 0) p->valid = 0;
    }
}

/* Drop every pending/pre-approved AVRCP entry for an address. Parked
 * (valid=1) channels are declined so the remote is not left hanging. */
static void clear_pending_avrcp_for_addr(const uint8_t *addr) {
    for (int i = 0; i < MAX_CONNECTIONS; i++) {
        pending_avrcp_t *p = &g_pending_avrcp[i];
        if (!p->valid || memcmp(p->addr, addr, 6) != 0) continue;
        if (p->valid == 1) {
            emit_log("avrcp: declining parked connection");
            avrcp_decline_incoming_connection(p->l2cap_cid);
        }
        p->valid = 0;
    }
}

/* Remote name requests are serialised by the controller: issue the next
 * outstanding one, if any. Called after connect and after each completion. */
static void request_next_remote_name(void) {
    for (int i = 0; i < MAX_CONNECTIONS; i++) {
        a2dp_conn_t *c = &g_conns[i];
        if (!c->active || !c->name_pending) continue;
        if (gap_remote_name_request(c->addr, 0x01 /* page scan repetition mode R1 */, 0)
                == ERROR_CODE_SUCCESS) {
            return;   /* one in flight; the rest follow on completion */
        }
    }
}

/* -------------------------------------------------------------------------
 * Audio output — writes addr-tagged length-prefixed frames to stdout
 *
 * Frame format (unchanged; Python knows codec from audio_start event):
 *   [uint32_le total_len = 6 + data_len]
 *   [6 bytes bd_addr, byte[0]..byte[5]]
 *   [data_len bytes audio payload]
 * ---------------------------------------------------------------------- */

static void write_audio_to_stdout(const bd_addr_t addr,
                                   const uint8_t *data, uint16_t len) {
    uint32_t total = 6u + len;
    if (fwrite(&total, 4, 1, stdout) != 1 ||
        fwrite(addr,   1, 6, stdout) != 6 ||
        fwrite(data,   1, len, stdout) != len ||
        fflush(stdout) != 0) {
        /* Parent closed the audio pipe — it is gone or shutting down. */
        if (!g_shutdown_requested) {
            emit_log("stdout closed by parent, shutting down");
            g_shutdown_requested = 1;
            hci_power_control(HCI_POWER_OFF);
        }
    }
}

/* -------------------------------------------------------------------------
 * AVDTP deferred-accept hook  (avdtp_*_incoming_connection() are added to
 * BTstack by patches/apply_patches.py and declared in classic/avdtp.h)
 * ---------------------------------------------------------------------- */

/* Called by patched avdtp.c BEFORE L2CAP accept — true deferred accept */
static void on_avdtp_incoming_connection(uint16_t local_cid, bd_addr_t addr) {
    char addr_str[18];
    addr_to_str(addr, addr_str);
    emit_log("avdtp: incoming connection hook fired");

    /* If this addr already has an established A2DP signaling connection, the
     * new L2CAP connection is the MEDIA channel (opened after AVDTP OPEN).
     * Auto-accept it immediately — no Python round-trip, avoids the timing
     * gap that causes strict sources (e.g. Nintendo Switch 2) to time out. */
    if (find_conn_by_addr(addr)) {
        emit_log("avdtp: auto-accepting media channel for established connection");
        avdtp_accept_incoming_connection(local_cid);
        return;
    }

    /* First connection from this addr — signaling channel.  Gate via Python. */
    pending_conn_t *p = alloc_pending();
    if (!p) {
        emit_log("avdtp: too many pending connections, declining");
        avdtp_decline_incoming_connection(local_cid);
        return;
    }
    p->valid     = 1;
    p->l2cap_cid = local_cid;
    memcpy(p->addr, addr, 6);

    /* A new gate starts: forget any earlier pre-approval for this address.
     * An AVRCP channel parked before this request stays parked and is
     * answered together with the AVDTP decision. */
    clear_preapproval_for_addr(addr);

    char evt[128];
    snprintf(evt, sizeof(evt),
             "{\"event\":\"l2cap_request\",\"addr\":\"%s\",\"cid\":%u}",
             addr_str, (unsigned)local_cid);
    emit_event(evt);
}

/* -------------------------------------------------------------------------
 * AVRCP deferred-accept hook  (avrcp_*_incoming_connection() are added to
 * BTstack by patches/apply_patches.py and declared in classic/avrcp.h)
 * ---------------------------------------------------------------------- */

/* Called by patched avrcp.c BEFORE L2CAP accept */
static void on_avrcp_incoming_connection(uint16_t local_cid, bd_addr_t addr) {
    /* Already an established A2DP connection → auto-accept AVRCP */
    if (find_conn_by_addr(addr)) {
        emit_log("avrcp: auto-accepting for established A2DP connection");
        avrcp_accept_incoming_connection(local_cid);
        return;
    }

    /* AVDTP already approved this addr (pre-approval slot) → auto-accept */
    pending_avrcp_t *pre = find_pending_avrcp(addr, 2);
    if (pre) {
        emit_log("avrcp: auto-accepting (AVDTP pre-approved)");
        pre->valid = 0;
        avrcp_accept_incoming_connection(local_cid);
        return;
    }

    /* Park until AVDTP is approved/denied */
    pending_avrcp_t *p = alloc_pending_avrcp();
    if (!p) {
        emit_log("avrcp: too many pending connections, declining");
        avrcp_decline_incoming_connection(local_cid);
        return;
    }
    p->valid     = 1;
    p->l2cap_cid = local_cid;
    memcpy(p->addr, addr, 6);
    emit_log("avrcp: gated — waiting for AVDTP approval");
}

/* -------------------------------------------------------------------------
 * AVRCP shared event handler (connection established / released)
 * Must be registered with avrcp_register_packet_handler().
 * ---------------------------------------------------------------------- */

static void on_avrcp_event(uint8_t packet_type, uint16_t channel,
                           uint8_t *packet, uint16_t size) {
    UNUSED(channel); UNUSED(size);
    if (packet_type != HCI_EVENT_PACKET) return;
    if (hci_event_packet_get_type(packet) != HCI_EVENT_AVRCP_META) return;

    uint8_t subevent = hci_event_avrcp_meta_get_subevent_code(packet);

    switch (subevent) {

    case AVRCP_SUBEVENT_CONNECTION_ESTABLISHED: {
        if (avrcp_subevent_connection_established_get_status(packet) != ERROR_CODE_SUCCESS) break;
        uint16_t avrcp_cid = avrcp_subevent_connection_established_get_avrcp_cid(packet);
        bd_addr_t bd;
        avrcp_subevent_connection_established_get_bd_addr(packet, bd);
        emit_log("avrcp: connected (audio may still be pending approval)");

        /* Link into established A2DP connection, or park in early table */
        a2dp_conn_t *conn = find_conn_by_addr(bd);
        if (conn) {
            if (conn->avrcp_cid != 0 && conn->avrcp_cid != avrcp_cid) {
                /* A second AVRCP channel from a device we already track.
                 * Keep the first one; this one gets no notifications. */
                emit_log("avrcp: duplicate connection for tracked device, ignoring");
                break;
            }
            conn->avrcp_cid = avrcp_cid;
        } else {
            /* A2DP not yet established (AVRCP arrived before AVDTP accept).
             * Park the cid; A2DP handler will pick it up when conn is allocated. */
            park_early_avrcp(bd, avrcp_cid);
        }
        /* Volume-change notifications (target role) */
        avrcp_target_support_event(avrcp_cid, AVRCP_NOTIFICATION_EVENT_VOLUME_CHANGED);
        /* Track-change notifications (controller role) — metadata arrives on event */
        avrcp_controller_enable_notification(avrcp_cid,
            AVRCP_NOTIFICATION_EVENT_TRACK_CHANGED);
        avrcp_controller_enable_notification(avrcp_cid,
            AVRCP_NOTIFICATION_EVENT_PLAYBACK_STATUS_CHANGED);
        avrcp_controller_get_now_playing_info(avrcp_cid);
        break;
    }

    case AVRCP_SUBEVENT_CONNECTION_RELEASED: {
        uint16_t avrcp_cid = avrcp_subevent_connection_released_get_avrcp_cid(packet);
        a2dp_conn_t *conn = find_conn_by_avrcp_cid(avrcp_cid);
        if (conn) conn->avrcp_cid = 0;
        avrcp_early_t *e = find_early_avrcp_by_cid(avrcp_cid);
        if (e) e->valid = 0;
        break;
    }

    default:
        break;
    }
}

/* -------------------------------------------------------------------------
 * AVRCP Target event handler (volume sync)
 * ---------------------------------------------------------------------- */

static void on_avrcp_target_event(uint8_t packet_type, uint16_t channel,
                                   uint8_t *packet, uint16_t size) {
    UNUSED(channel); UNUSED(size);
    if (packet_type != HCI_EVENT_PACKET) return;
    if (hci_event_packet_get_type(packet) != HCI_EVENT_AVRCP_META) return;

    uint8_t subevent = hci_event_avrcp_meta_get_subevent_code(packet);
    char evt[256];

    switch (subevent) {

    case AVRCP_SUBEVENT_NOTIFICATION_VOLUME_CHANGED: {
        /* Source is setting our volume (SET_ABSOLUTE_VOLUME) */
        uint16_t avrcp_cid = avrcp_subevent_notification_volume_changed_get_avrcp_cid(packet);
        uint8_t  abs_vol   = avrcp_subevent_notification_volume_changed_get_absolute_volume(packet);
        /* Acknowledge to the source */
        avrcp_target_volume_changed(avrcp_cid, abs_vol);
        a2dp_conn_t *conn = find_conn_by_avrcp_cid(avrcp_cid);
        if (!conn) {
            if (g_debug) emit_log("avrcp: volume change from untracked connection, ignored");
            break;
        }
        snprintf(evt, sizeof(evt),
                 "{\"event\":\"volume_changed\",\"addr\":\"%s\",\"volume\":%u}",
                 conn->addr_str, (unsigned)abs_vol);
        emit_event(evt);
        break;
    }

    default:
        break;
    }
}

/* -------------------------------------------------------------------------
 * AVRCP Controller event handler (metadata)
 * ---------------------------------------------------------------------- */

static void on_avrcp_controller_event(uint8_t packet_type, uint16_t channel,
                                       uint8_t *packet, uint16_t size) {
    UNUSED(channel); UNUSED(size);
    if (packet_type != HCI_EVENT_PACKET) return;
    if (hci_event_packet_get_type(packet) != HCI_EVENT_AVRCP_META) return;

    uint8_t subevent = hci_event_avrcp_meta_get_subevent_code(packet);

    switch (subevent) {

    case AVRCP_SUBEVENT_NOTIFICATION_TRACK_CHANGED: {
        /* Track changed — re-register (one-shot in AVRCP 1.3) and refetch */
        uint16_t avrcp_cid = avrcp_subevent_notification_track_changed_get_avrcp_cid(packet);
        avrcp_controller_enable_notification(avrcp_cid,
            AVRCP_NOTIFICATION_EVENT_TRACK_CHANGED);
        avrcp_controller_get_now_playing_info(avrcp_cid);
        break;
    }

    case AVRCP_SUBEVENT_NOTIFICATION_PLAYBACK_STATUS_CHANGED: {
        uint16_t avrcp_cid = avrcp_subevent_notification_playback_status_changed_get_avrcp_cid(packet);
        uint8_t  status    = avrcp_subevent_notification_playback_status_changed_get_play_status(packet);
        a2dp_conn_t *conn  = find_conn_by_avrcp_cid(avrcp_cid);
        if (!conn) break;
        const char *name;
        switch (status) {
            case AVRCP_PLAYBACK_STATUS_PLAYING:  name = "playing"; break;
            case AVRCP_PLAYBACK_STATUS_PAUSED:   name = "paused";  break;
            case AVRCP_PLAYBACK_STATUS_STOPPED:  name = "stopped"; break;
            case AVRCP_PLAYBACK_STATUS_FWD_SEEK:
            case AVRCP_PLAYBACK_STATUS_REV_SEEK: name = "seeking"; break;
            default:                             name = "error";   break;
        }
        char evt[96];
        snprintf(evt, sizeof(evt),
                 "{\"event\":\"playback\",\"addr\":\"%s\",\"status\":\"%s\"}",
                 conn->addr_str, name);
        emit_event(evt);
        break;
    }

    case AVRCP_SUBEVENT_NOW_PLAYING_TITLE_INFO: {
        uint16_t avrcp_cid = avrcp_subevent_now_playing_title_info_get_avrcp_cid(packet);
        uint8_t  len       = avrcp_subevent_now_playing_title_info_get_value_len(packet);
        const uint8_t *val = avrcp_subevent_now_playing_title_info_get_value(packet);
        a2dp_conn_t *conn  = find_conn_by_avrcp_cid(avrcp_cid);
        if (conn) copy_attr(conn->meta_title, sizeof(conn->meta_title), val, len);
        break;
    }

    case AVRCP_SUBEVENT_NOW_PLAYING_ARTIST_INFO: {
        uint16_t avrcp_cid = avrcp_subevent_now_playing_artist_info_get_avrcp_cid(packet);
        uint8_t  len       = avrcp_subevent_now_playing_artist_info_get_value_len(packet);
        const uint8_t *val = avrcp_subevent_now_playing_artist_info_get_value(packet);
        a2dp_conn_t *conn  = find_conn_by_avrcp_cid(avrcp_cid);
        if (conn) copy_attr(conn->meta_artist, sizeof(conn->meta_artist), val, len);
        break;
    }

    case AVRCP_SUBEVENT_NOW_PLAYING_ALBUM_INFO: {
        uint16_t avrcp_cid = avrcp_subevent_now_playing_album_info_get_avrcp_cid(packet);
        uint8_t  len       = avrcp_subevent_now_playing_album_info_get_value_len(packet);
        const uint8_t *val = avrcp_subevent_now_playing_album_info_get_value(packet);
        a2dp_conn_t *conn  = find_conn_by_avrcp_cid(avrcp_cid);
        if (conn) copy_attr(conn->meta_album, sizeof(conn->meta_album), val, len);
        break;
    }

    case AVRCP_SUBEVENT_NOW_PLAYING_INFO_DONE: {
        /* All attributes received — emit the full metadata event */
        uint16_t avrcp_cid = avrcp_subevent_now_playing_info_done_get_avrcp_cid(packet);
        a2dp_conn_t *conn  = find_conn_by_avrcp_cid(avrcp_cid);
        if (conn) {
            char title[META_BUF_SIZE * JSON_ESC_FACTOR];
            char artist[META_BUF_SIZE * JSON_ESC_FACTOR];
            char album[META_BUF_SIZE * JSON_ESC_FACTOR];
            json_escape(title,  sizeof(title),  (const uint8_t *)conn->meta_title,  META_BUF_SIZE);
            json_escape(artist, sizeof(artist), (const uint8_t *)conn->meta_artist, META_BUF_SIZE);
            json_escape(album,  sizeof(album),  (const uint8_t *)conn->meta_album,  META_BUF_SIZE);
            char evt[3 * META_BUF_SIZE * JSON_ESC_FACTOR + 96];
            snprintf(evt, sizeof(evt),
                     "{\"event\":\"metadata\",\"addr\":\"%s\","
                     "\"title\":\"%s\",\"artist\":\"%s\",\"album\":\"%s\"}",
                     conn->addr_str, title, artist, album);
            emit_event(evt);
        }
        break;
    }

    default:
        break;
    }
}

/* -------------------------------------------------------------------------
 * A2DP / AVDTP event handler
 * ---------------------------------------------------------------------- */

static void on_a2dp_sink_event(uint8_t packet_type, uint16_t channel,
                                uint8_t *packet, uint16_t size) {
    UNUSED(channel);
    UNUSED(size);

    if (packet_type != HCI_EVENT_PACKET) return;
    if (hci_event_packet_get_type(packet) != HCI_EVENT_A2DP_META) return;

    uint8_t subevent = hci_event_a2dp_meta_get_subevent_code(packet);
    char evt[256];

    switch (subevent) {

    case A2DP_SUBEVENT_SIGNALING_CONNECTION_ESTABLISHED: {
        uint8_t status = a2dp_subevent_signaling_connection_established_get_status(packet);
        if (status != ERROR_CODE_SUCCESS) break;

        uint16_t cid = a2dp_subevent_signaling_connection_established_get_a2dp_cid(packet);
        bd_addr_t bd;
        a2dp_subevent_signaling_connection_established_get_bd_addr(packet, bd);

        a2dp_conn_t *conn = alloc_conn();
        if (!conn) {
            emit_log("a2dp: too many connections, ignoring new one");
            break;
        }
        conn->a2dp_cid = cid;
        memcpy(conn->addr, bd, 6);
        addr_to_str(bd, conn->addr_str);

        /* Promote any AVRCP connection that arrived before AVDTP was accepted */
        avrcp_early_t *early = find_early_avrcp_by_addr(bd);
        if (early) {
            conn->avrcp_cid = early->cid;
            early->valid    = 0;
        }
        /* From now on AVRCP from this address is auto-accepted via the
         * active connection, so any pre-approval slot is obsolete. */
        clear_preapproval_for_addr(bd);

        /* Emit connected with address only — name arrives asynchronously */
        snprintf(evt, sizeof(evt),
                 "{\"event\":\"connected\",\"addr\":\"%s\"}", conn->addr_str);
        emit_event(evt);

        /* Request the human-readable device name; result via
           HCI_EVENT_REMOTE_NAME_REQUEST_COMPLETE in on_hci_event */
        conn->name_pending = 1;
        request_next_remote_name();
        break;
    }

    case A2DP_SUBEVENT_SIGNALING_MEDIA_CODEC_SBC_CONFIGURATION: {
        uint16_t cid  = a2dp_subevent_signaling_media_codec_sbc_configuration_get_a2dp_cid(packet);
        uint8_t  seid = a2dp_subevent_signaling_media_codec_sbc_configuration_get_local_seid(packet);
        a2dp_conn_t *conn = find_conn_by_cid(cid);
        if (conn) {
            conn->local_seid       = seid;
            conn->codec_type       = AVDTP_CODEC_SBC;
            conn->sample_rate      = a2dp_subevent_signaling_media_codec_sbc_configuration_get_sampling_frequency(packet);
            conn->channels         = a2dp_subevent_signaling_media_codec_sbc_configuration_get_num_channels(packet);
            conn->sbc_block_length = a2dp_subevent_signaling_media_codec_sbc_configuration_get_block_length(packet);
            conn->sbc_subbands     = a2dp_subevent_signaling_media_codec_sbc_configuration_get_subbands(packet);
            conn->sbc_allocation   = a2dp_subevent_signaling_media_codec_sbc_configuration_get_allocation_method(packet);
            conn->sbc_bitpool      = a2dp_subevent_signaling_media_codec_sbc_configuration_get_max_bitpool_value(packet);
            if (g_debug) {
                char dbg[160];
                snprintf(dbg, sizeof(dbg),
                         "SBC config: seid=%u rate=%d ch=%d blocks=%d subbands=%d alloc=%d bitpool=%u..%d",
                         seid, conn->sample_rate, conn->channels,
                         conn->sbc_block_length, conn->sbc_subbands, conn->sbc_allocation,
                         a2dp_subevent_signaling_media_codec_sbc_configuration_get_min_bitpool_value(packet),
                         conn->sbc_bitpool);
                emit_log(dbg);
            }
        }
        break;
    }

    case A2DP_SUBEVENT_SIGNALING_MEDIA_CODEC_MPEG_AAC_CONFIGURATION: {
        uint16_t cid  = a2dp_subevent_signaling_media_codec_mpeg_aac_configuration_get_a2dp_cid(packet);
        uint8_t  seid = a2dp_subevent_signaling_media_codec_mpeg_aac_configuration_get_local_seid(packet);
        a2dp_conn_t *conn = find_conn_by_cid(cid);
        if (conn) {
            conn->local_seid  = seid;
            conn->codec_type  = AVDTP_CODEC_MPEG_2_4_AAC;
            conn->sample_rate = (int)a2dp_subevent_signaling_media_codec_mpeg_aac_configuration_get_sampling_frequency(packet);
            conn->channels    = (int)a2dp_subevent_signaling_media_codec_mpeg_aac_configuration_get_num_channels(packet);
            if (g_debug) {
                char dbg[128];
                snprintf(dbg, sizeof(dbg),
                         "AAC config: seid=%u rate=%d ch=%d",
                         seid, conn->sample_rate, conn->channels);
                emit_log(dbg);
            }
        }
        break;
    }

    case A2DP_SUBEVENT_SIGNALING_MEDIA_CODEC_OTHER_CONFIGURATION: {
        uint16_t cid  = a2dp_subevent_signaling_media_codec_other_configuration_get_a2dp_cid(packet);
        uint8_t  seid = a2dp_subevent_signaling_media_codec_other_configuration_get_local_seid(packet);
        uint16_t len  = a2dp_subevent_signaling_media_codec_other_configuration_get_media_codec_information_len(packet);
        const uint8_t *info = a2dp_subevent_signaling_media_codec_other_configuration_get_media_codec_information(packet);
        a2dp_conn_t *conn = find_conn_by_cid(cid);
        if (!conn) break;
        if (len < 7) {
            emit_log("vendor codec configuration too short, ignoring");
            break;
        }
        uint32_t vendor_id = little_endian_read_32(info, 0);
        uint16_t codec_id  = little_endian_read_16(info, 4);
        uint8_t  params    = info[6];
        uint8_t  vendor_codec = 0;
        if (vendor_id == APTX_VENDOR_ID && codec_id == APTX_CODEC_ID)       vendor_codec = VENDOR_APTX;
        if (vendor_id == APTX_HD_VENDOR_ID && codec_id == APTX_HD_CODEC_ID) vendor_codec = VENDOR_APTX_HD;
        if (!vendor_codec) {
            char msg[96];
            snprintf(msg, sizeof(msg), "unknown vendor codec configured: vendor=0x%08x codec=0x%04x",
                     (unsigned)vendor_id, (unsigned)codec_id);
            emit_log(msg);
            break;
        }
        conn->local_seid   = seid;
        conn->codec_type   = AVDTP_CODEC_NON_A2DP;
        conn->vendor_codec = vendor_codec;
        conn->sample_rate  = (params & APTX_RATE_48000) ? 48000 :
                             (params & APTX_RATE_44100) ? 44100 :
                             (params & APTX_RATE_32000) ? 32000 : 16000;
        conn->channels     = (params & APTX_CH_MONO) ? 1 : 2;
        if (g_debug) {
            char dbg[96];
            snprintf(dbg, sizeof(dbg), "%s config: seid=%u rate=%d ch=%d",
                     vendor_codec == VENDOR_APTX_HD ? "aptX HD" : "aptX",
                     seid, conn->sample_rate, conn->channels);
            emit_log(dbg);
        }
        break;
    }

    case A2DP_SUBEVENT_STREAM_STARTED: {
        uint16_t cid = a2dp_subevent_stream_started_get_a2dp_cid(packet);
        a2dp_conn_t *conn = find_conn_by_cid(cid);
        if (conn) {
            if (conn->codec_type == AVDTP_CODEC_MPEG_2_4_AAC || conn->codec_type == AVDTP_CODEC_NON_A2DP) {
                const char *codec = conn->codec_type == AVDTP_CODEC_MPEG_2_4_AAC ? "aac" :
                                    conn->vendor_codec == VENDOR_APTX_HD ? "aptx_hd" : "aptx";
                snprintf(evt, sizeof(evt),
                         "{\"event\":\"audio_start\",\"addr\":\"%s\","
                         "\"sample_rate\":%d,\"channels\":%d,\"codec\":\"%s\"}",
                         conn->addr_str, conn->sample_rate, conn->channels, codec);
            } else {
                snprintf(evt, sizeof(evt),
                         "{\"event\":\"audio_start\",\"addr\":\"%s\","
                         "\"sample_rate\":%d,\"channels\":%d,\"codec\":\"sbc\","
                         "\"block_length\":%d,\"subbands\":%d,\"allocation\":\"%s\",\"bitpool\":%d}",
                         conn->addr_str, conn->sample_rate, conn->channels,
                         conn->sbc_block_length, conn->sbc_subbands,
                         conn->sbc_allocation == AVDTP_SBC_ALLOCATION_METHOD_SNR ? "snr" : "loudness",
                         conn->sbc_bitpool);
            }
            emit_event(evt);
        }
        break;
    }

    case A2DP_SUBEVENT_STREAM_SUSPENDED: {
        uint16_t cid = a2dp_subevent_stream_suspended_get_a2dp_cid(packet);
        a2dp_conn_t *conn = find_conn_by_cid(cid);
        if (conn) {
            snprintf(evt, sizeof(evt),
                     "{\"event\":\"audio_stop\",\"addr\":\"%s\"}", conn->addr_str);
            emit_event(evt);
        }
        break;
    }

    case A2DP_SUBEVENT_STREAM_STOPPED: {
        uint16_t cid = a2dp_subevent_stream_stopped_get_a2dp_cid(packet);
        a2dp_conn_t *conn = find_conn_by_cid(cid);
        if (conn) {
            snprintf(evt, sizeof(evt),
                     "{\"event\":\"audio_stop\",\"addr\":\"%s\"}", conn->addr_str);
            emit_event(evt);
        }
        break;
    }

    case A2DP_SUBEVENT_SIGNALING_CONNECTION_RELEASED: {
        uint16_t cid = a2dp_subevent_signaling_connection_released_get_a2dp_cid(packet);
        a2dp_conn_t *conn = find_conn_by_cid(cid);
        if (conn) {
            snprintf(evt, sizeof(evt),
                     "{\"event\":\"disconnected\",\"addr\":\"%s\"}",
                     conn->addr_str);
            emit_event(evt);
            clear_preapproval_for_addr(conn->addr);
            free_conn(conn);
        }
        break;
    }

    default:
        break;
    }
}

/* -------------------------------------------------------------------------
 * A2DP media data handler — receives RTP packets with SBC or AAC payload
 * ---------------------------------------------------------------------- */

static void on_a2dp_media_packet(uint8_t seid, uint8_t *packet, uint16_t size) {
    /*
     * BTstack does NOT strip the RTP header before calling this callback.
     *
     *   [12 bytes RTP fixed header][4 * CC bytes CSRC][optional extension]
     *   SBC:     [1 byte A2DP SBC media payload header][N bytes raw SBC frames]
     *   AAC:     [N bytes LATM/AudioMuxElement payload]
     *   aptX HD: [N bytes raw aptX HD samples]
     *   aptX:    the whole packet is raw aptX samples, there is NO RTP header
     */
    a2dp_conn_t *conn = find_conn_by_seid(seid);
    if (!conn) return;

    if (conn->codec_type == AVDTP_CODEC_NON_A2DP && conn->vendor_codec == VENDOR_APTX) {
        if (size) write_audio_to_stdout(conn->addr, packet, size);
        return;
    }
    if (size < 12) return;

    uint16_t offset = 12 + 4u * (packet[0] & 0x0F);       /* CSRC list */
    if (packet[0] & 0x10) {                                 /* header extension */
        if (size < offset + 4) return;
        uint16_t ext_words = (uint16_t)((packet[offset + 2] << 8) | packet[offset + 3]);
        offset = (uint16_t)(offset + 4 + 4u * ext_words);
    }

    if (conn->codec_type == AVDTP_CODEC_SBC) {
        if (size < offset + 1) return;
        uint8_t sbc_hdr = packet[offset];
        offset++;
        if (sbc_hdr & 0x80) {
            /* Fragmented SBC frame (F bit). FFmpeg's sbc demuxer needs whole
             * frames; fragments only occur with bitpools beyond the MTU. */
            static int warned = 0;
            if (!warned) {
                warned = 1;
                emit_log("sbc: fragmented frames not supported, dropping (lower max bitpool)");
            }
            return;
        }
    }

    if (size <= offset) return;
    write_audio_to_stdout(conn->addr, packet + offset, (uint16_t)(size - offset));
}

/* -------------------------------------------------------------------------
 * HCI packet handler — GAP events (inquiry, power-on, etc.)
 * ---------------------------------------------------------------------- */

static btstack_packet_callback_registration_t g_hci_event_cb;

static void on_hci_event(uint8_t packet_type, uint16_t channel,
                          uint8_t *packet, uint16_t size) {
    UNUSED(channel);

    if (packet_type != HCI_EVENT_PACKET) return;

    uint8_t type = hci_event_packet_get_type(packet);
    char evt[128];

    switch (type) {
    case BTSTACK_EVENT_STATE: {
        uint8_t bt_state = btstack_event_state_get_state(packet);
        if (bt_state == HCI_STATE_WORKING) {
            bd_addr_t local_addr;
            gap_local_bd_addr(local_addr);
            char addr_str[18];
            addr_to_str(local_addr, addr_str);

            snprintf(evt, sizeof(evt),
                     "{\"event\":\"ready\",\"address\":\"%s\"}", addr_str);
            emit_event(evt);
            emit_log(g_debug ? "build: v11 (debug)" : "build: v11");

            /* Apply initial discoverability (off by default, Python will
               send set_discoverable when the GUI toggle is set). */
            gap_discoverable_control(g_discoverable);
            gap_connectable_control(1);
        } else if (bt_state == HCI_STATE_OFF) {
            if (!g_shutdown_requested) {
                emit_event("{\"event\":\"error\",\"msg\":\"HCI powered off unexpectedly — USB dongle not accessible. Check WinUSB driver (Zadig) and kill any zombie btstack_sink.exe.\"}");
            }
            /* BTstack 1.6.1's Windows run loop never checks the exit flag
             * (btstack_run_loop_trigger_exit() is a no-op there), so leave
             * the process directly. stderr is the only thing worth flushing. */
            fflush(stderr);
            exit(g_shutdown_requested ? 0 : 1);
        }
        break;
    }

    case BTSTACK_EVENT_POWERON_FAILED:
        /* Transport could not be opened: no matching WinUSB dongle, wrong
         * driver, or another process holds it. Without this the parent would
         * wait for "ready" forever. */
        emit_event("{\"event\":\"error\",\"msg\":\"Could not open the USB dongle. "
                   "Is it plugged in, does it use the WinUSB driver (Zadig), and is no other "
                   "btstack_sink.exe running?\"}");
        fflush(stderr);
        exit(1);

    case HCI_EVENT_PIN_CODE_REQUEST:
        {
            bd_addr_t bd;
            hci_event_pin_code_request_get_bd_addr(packet, bd);
            gap_pin_code_response(bd, "0000");
        }
        break;

    case HCI_EVENT_USER_CONFIRMATION_REQUEST:
        {
            bd_addr_t bd;
            hci_event_user_confirmation_request_get_bd_addr(packet, bd);
            gap_ssp_confirmation_response(bd);
        }
        break;

    case HCI_EVENT_REMOTE_NAME_REQUEST_COMPLETE: {
        /* packet[2]=status, packet[3..8]=BD_ADDR, packet[9..]=name */
        if (size < 9) break;
        bd_addr_t bd;
        reverse_bd_addr(&packet[3], bd);
        a2dp_conn_t *named = find_conn_by_addr(bd);
        if (named) named->name_pending = 0;
        request_next_remote_name();
        if (packet[2] != ERROR_CODE_SUCCESS) break;

        char addr_s[18];
        addr_to_str(bd, addr_s);
        /* Name is NUL-terminated UTF-8, up to 248 bytes, but only trust what
         * the event actually carries */
        size_t name_len = (size_t)size - 9;
        if (name_len > 248) name_len = 248;
        char name_esc[248 * JSON_ESC_FACTOR + 1];
        json_escape(name_esc, sizeof(name_esc), &packet[9], name_len);
        char name_evt[sizeof(name_esc) + 64];
        snprintf(name_evt, sizeof(name_evt),
                 "{\"event\":\"name\",\"addr\":\"%s\",\"name\":\"%s\"}",
                 addr_s, name_esc);
        emit_event(name_evt);
        break;
    }

    default:
        break;
    }
}

/* -------------------------------------------------------------------------
 * Command processing — called from BTstack run loop
 * ---------------------------------------------------------------------- */

/* Extract a string field value from a minimal JSON line.
 * key must include the surrounding quotes, e.g. "\"addr\"".
 * Result written to dst (null-terminated), dst_size includes NUL. */
static void json_extract_str(const char *line, const char *key,
                              char *dst, size_t dst_size) {
    dst[0] = '\0';
    const char *p = strstr(line, key);
    if (!p) return;
    p += strlen(key);
    while (*p && *p != '"') p++;
    if (!*p) return;
    p++;  /* skip opening quote */
    size_t i = 0;
    while (*p && *p != '"' && i < dst_size - 1) dst[i++] = *p++;
    dst[i] = '\0';
}

static void process_command(const char *line) {
    char cmd[64]    = "";
    char addr[18]   = "";
    char action[16] = "";
    int enabled     = -1;
    int volume      = -1;
    uint16_t cid    = 0;

    json_extract_str(line, "\"cmd\"",    cmd,    sizeof(cmd));
    json_extract_str(line, "\"addr\"",   addr,   sizeof(addr));
    json_extract_str(line, "\"action\"", action, sizeof(action));

    {
        const char *p = strstr(line, "\"enabled\"");
        if (p) {
            p += 9;
            while (*p && (*p == ':' || *p == ' ')) p++;
            if (strncmp(p, "true",  4) == 0) enabled = 1;
            if (strncmp(p, "false", 5) == 0) enabled = 0;
        }
    }
    {
        const char *p = strstr(line, "\"cid\"");
        if (p) {
            p += 5;
            while (*p && (*p == ':' || *p == ' ')) p++;
            cid = (uint16_t)atoi(p);
        }
    }
    {
        const char *p = strstr(line, "\"volume\"");
        if (p) {
            p += 8;
            while (*p && (*p == ':' || *p == ' ')) p++;
            volume = atoi(p);
        }
    }

    if (strcmp(cmd, "approve") == 0) {
        pending_conn_t *p = find_pending_by_cid(cid);
        if (!p) {
            emit_log("avdtp: approve for unknown/expired cid, ignored");
        } else {
            uint8_t addr[6];
            memcpy(addr, p->addr, 6);
            emit_log("avdtp: accepting incoming connection");
            avdtp_accept_incoming_connection(p->l2cap_cid);
            p->valid = 0;

            /* Also accept any parked AVRCP for the same address, or
             * pre-approve the one that is still to come. */
            pending_avrcp_t *parked = find_pending_avrcp(addr, 1);
            if (parked) {
                emit_log("avrcp: accepting (AVDTP approved)");
                avrcp_accept_incoming_connection(parked->l2cap_cid);
                parked->valid = 0;
            } else {
                pending_avrcp_t *pre = alloc_pending_avrcp();
                if (pre) {
                    pre->valid      = 2;
                    pre->created_ms = btstack_run_loop_get_time_ms();
                    memcpy(pre->addr, addr, 6);
                }
            }
        }
    }
    else if (strcmp(cmd, "deny") == 0) {
        pending_conn_t *p = find_pending_by_cid(cid);
        if (!p) {
            emit_log("avdtp: deny for unknown/expired cid, ignored");
        } else {
            uint8_t addr[6];
            memcpy(addr, p->addr, 6);
            emit_log("avdtp: declining incoming connection");
            avdtp_decline_incoming_connection(p->l2cap_cid);
            p->valid = 0;
            clear_pending_avrcp_for_addr(addr);
        }
    }
    else if (strcmp(cmd, "set_discoverable") == 0) {
        if (enabled >= 0) {
            g_discoverable = enabled;
            gap_discoverable_control(enabled);
            char msg[64];
            snprintf(msg, sizeof(msg), "discoverable: %s", enabled ? "on" : "off");
            emit_log(msg);
        }
    }
    else if (strcmp(cmd, "set_volume") == 0 && volume >= 0) {
        /* Find connection by addr and push absolute volume to source via AVRCP */
        uint8_t abs_vol = (uint8_t)(volume > 127 ? 127 : volume);
        /* If addr provided, target that specific device; else notify all */
        int found = 0;
        for (int i = 0; i < MAX_CONNECTIONS; i++) {
            if (!g_conns[i].active || g_conns[i].avrcp_cid == 0) continue;
            if (addr[0] == '\0' || strcmp(g_conns[i].addr_str, addr) == 0) {
                avrcp_target_volume_changed(g_conns[i].avrcp_cid, abs_vol);
                found = 1;
            }
        }
        if (!found && g_debug) emit_log("set_volume: no AVRCP connection for addr");
    }
    else if (strcmp(cmd, "player") == 0) {
        /* AVRCP controller pass-through command to the source's player */
        a2dp_conn_t *conn = NULL;
        for (int i = 0; i < MAX_CONNECTIONS; i++) {
            if (g_conns[i].active && strcmp(g_conns[i].addr_str, addr) == 0) {
                conn = &g_conns[i];
                break;
            }
        }
        if (!conn || conn->avrcp_cid == 0) {
            emit_log("player: no AVRCP connection for that device");
        } else {
            uint8_t rc;
            if      (strcmp(action, "play")  == 0) rc = avrcp_controller_play(conn->avrcp_cid);
            else if (strcmp(action, "pause") == 0) rc = avrcp_controller_pause(conn->avrcp_cid);
            else if (strcmp(action, "stop")  == 0) rc = avrcp_controller_stop(conn->avrcp_cid);
            else if (strcmp(action, "next")  == 0) rc = avrcp_controller_forward(conn->avrcp_cid);
            else if (strcmp(action, "prev")  == 0) rc = avrcp_controller_backward(conn->avrcp_cid);
            else { emit_log("player: unknown action"); rc = ERROR_CODE_SUCCESS; }
            if (rc != ERROR_CODE_SUCCESS) {
                char msg[64];
                snprintf(msg, sizeof(msg), "player: %s failed (0x%02x)", action, rc);
                emit_log(msg);
            }
        }
    }
    else if (strcmp(cmd, "forget_key") == 0) {
        bd_addr_t bd;
        if (sscanf_bd_addr(addr, bd)) {
            gap_drop_link_key_for_bd_addr(bd);
            char msg[64];
            snprintf(msg, sizeof(msg), "forgot bonding key for %s", addr);
            emit_log(msg);
        } else {
            emit_log("forget_key: invalid address");
        }
    }
    else if (strcmp(cmd, "stop") == 0) {
        /* Idempotent: the parent sends "stop" and then closes stdin, which
         * enqueues a second one. Re-entering hci_power_control() while BTstack
         * is already halting would cancel its shutdown timers. */
        if (!g_shutdown_requested) {
            emit_log("stop command received");
            g_shutdown_requested = 1;
            hci_power_control(HCI_POWER_OFF);
        }
    }
}

/* -------------------------------------------------------------------------
 * stdin reader thread — reads lines into g_cmd_buf, signals g_stdin_event
 *
 * Only this thread touches stdin; it never calls into BTstack. On EOF
 * (parent closed the pipe or died) it enqueues a synthetic "stop" so the
 * run loop shuts HCI down cleanly instead of leaving a zombie holding the
 * WinUSB handle.
 * ---------------------------------------------------------------------- */

static void enqueue_command(const char *line) {
    EnterCriticalSection(&g_cs);
    int next = (g_cmd_head + 1) % CMD_BUF_LINES;
    if (next != g_cmd_tail) {
        strncpy(g_cmd_buf[g_cmd_head], line, CMD_LINE_MAX - 1);
        g_cmd_buf[g_cmd_head][CMD_LINE_MAX - 1] = '\0';
        g_cmd_head = next;
    } else {
        g_cmd_dropped++;
    }
    LeaveCriticalSection(&g_cs);
    SetEvent(g_stdin_event);
}

static unsigned __stdcall stdin_reader_thread(void *arg) {
    UNUSED(arg);
    char line[CMD_LINE_MAX];
    while (fgets(line, sizeof(line), stdin)) {
        int len = (int)strlen(line);
        while (len > 0 && (line[len-1] == '\n' || line[len-1] == '\r'))
            line[--len] = '\0';
        if (len == 0) continue;
        enqueue_command(line);
    }
    enqueue_command("{\"cmd\":\"stop\"}");
    return 0;
}

/* -------------------------------------------------------------------------
 * BTstack data source callback — drains g_cmd_buf in the run loop thread
 * ---------------------------------------------------------------------- */

static void stdin_ds_callback(btstack_data_source_t *ds, btstack_data_source_callback_type_t type) {
    UNUSED(ds);
    UNUSED(type);

    /* Reset BEFORE draining: a line enqueued while we drain re-signals the
     * event and we get called again. Resetting after the drain could clear a
     * signal for a line we never saw (lost wake-up). */
    ResetEvent(g_stdin_event);

    for (;;) {
        char line[CMD_LINE_MAX];
        int dropped;

        EnterCriticalSection(&g_cs);
        dropped = g_cmd_dropped;
        g_cmd_dropped = 0;
        if (g_cmd_tail == g_cmd_head) {
            LeaveCriticalSection(&g_cs);
            if (dropped) emit_log("stdin: command ring buffer overflow, lines dropped");
            break;
        }
        strncpy(line, g_cmd_buf[g_cmd_tail], CMD_LINE_MAX - 1);
        line[CMD_LINE_MAX - 1] = '\0';
        g_cmd_tail = (g_cmd_tail + 1) % CMD_BUF_LINES;
        LeaveCriticalSection(&g_cs);

        if (dropped) emit_log("stdin: command ring buffer overflow, lines dropped");
        process_command(line);
    }
}

/* -------------------------------------------------------------------------
 * SBC capability byte 1 from the user's block length / subband / allocation
 * choice. Bit layout per A2DP §4.3.2 (matches avdtp.h's AVDTP_SBC_* enums):
 *   bit7 blocks 4, bit6 blocks 8, bit5 blocks 12, bit4 blocks 16,
 *   bit3 subbands 4, bit2 subbands 8, bit1 SNR, bit0 loudness.
 * ---------------------------------------------------------------------- */

static uint8_t sbc_capability_byte1(void) {
    uint8_t b = 0;
    switch (g_sbc_block_length) {
        case 4:  b |= AVDTP_SBC_BLOCK_LENGTH_4  << 4; break;
        case 8:  b |= AVDTP_SBC_BLOCK_LENGTH_8  << 4; break;
        case 12: b |= AVDTP_SBC_BLOCK_LENGTH_12 << 4; break;
        case 16: b |= AVDTP_SBC_BLOCK_LENGTH_16 << 4; break;
        default: b |= 0xF0; break;
    }
    switch (g_sbc_subbands) {
        case 4:  b |= AVDTP_SBC_SUBBANDS_4 << 2; break;
        case 8:  b |= AVDTP_SBC_SUBBANDS_8 << 2; break;
        default: b |= 0x0C; break;
    }
    switch (g_sbc_allocation) {
        case 1:  b |= AVDTP_SBC_ALLOCATION_METHOD_LOUDNESS; break;
        case 2:  b |= AVDTP_SBC_ALLOCATION_METHOD_SNR; break;
        default: b |= 0x03; break;
    }
    return b;
}

/* -------------------------------------------------------------------------
 * SDP records
 * ---------------------------------------------------------------------- */

static void setup_sdp(void) {
    /* A2DP Sink service record */
    memset(g_sdp_a2dp_sink_service, 0, sizeof(g_sdp_a2dp_sink_service));
    a2dp_sink_create_sdp_record(g_sdp_a2dp_sink_service,
                                sdp_create_service_record_handle(),
                                AVDTP_SINK_FEATURE_MASK_HEADPHONE,
                                NULL, NULL);
    sdp_register_service(g_sdp_a2dp_sink_service);

    /* AVRCP Target service record */
    memset(g_sdp_avrcp_tg_service, 0, sizeof(g_sdp_avrcp_tg_service));
    avrcp_target_create_sdp_record(g_sdp_avrcp_tg_service,
                                   sdp_create_service_record_handle(),
                                   AVRCP_FEATURE_MASK_CATEGORY_PLAYER_OR_RECORDER,
                                   NULL, NULL);
    sdp_register_service(g_sdp_avrcp_tg_service);

    /* AVRCP Controller service record (allows us to query track metadata) */
    memset(g_sdp_avrcp_ct_service, 0, sizeof(g_sdp_avrcp_ct_service));
    avrcp_controller_create_sdp_record(g_sdp_avrcp_ct_service,
                                       sdp_create_service_record_handle(),
                                       AVRCP_FEATURE_MASK_CATEGORY_MONITOR_OR_AMPLIFIER,
                                       NULL, NULL);
    sdp_register_service(g_sdp_avrcp_ct_service);
}

/* -------------------------------------------------------------------------
 * main
 * ---------------------------------------------------------------------- */

int main(int argc, char *argv[]) {
    _setmode(_fileno(stdout), _O_BINARY);
    _setmode(_fileno(stdin),  _O_TEXT);

    if (argc >= 2)  strncpy(g_usb_filter,    argv[1], sizeof(g_usb_filter) - 1);
    if (argc >= 3)  strncpy(g_device_name,   argv[2], sizeof(g_device_name) - 1);
    if (argc >= 4)  g_max_bitpool = atoi(argv[3]);
    if (argc >= 5)  g_debug       = atoi(argv[4]);
    if (argc >= 6)  g_cod         = (uint32_t)strtoul(argv[5], NULL, 16);
    if (argc >= 7)  strncpy(g_keystore_path, argv[6], sizeof(g_keystore_path) - 1);
    if (argc >= 8)  g_sbc_block_length = atoi(argv[7]);
    if (argc >= 9)  g_sbc_subbands     = atoi(argv[8]);
    if (argc >= 10) g_sbc_allocation   = atoi(argv[9]);
    if (argc >= 11) g_vendor_codecs    = atoi(argv[10]);
    if (g_max_bitpool < 2 || g_max_bitpool > 250) g_max_bitpool = 53;

    /* Default TLV key-store path: next to this executable */
    if (g_keystore_path[0] == '\0') {
        strcpy(g_keystore_path, "btstack_keys.db");
        char module_path[MAX_PATH];
        if (GetModuleFileNameA(NULL, module_path, MAX_PATH)) {
            char *last_sep = strrchr(module_path, '\\');
            if (last_sep) {
                *(last_sep + 1) = '\0';
                snprintf(g_keystore_path, sizeof(g_keystore_path),
                         "%sbtstack_keys.db", module_path);
            }
        }
    }

    /* ---- BTstack init ---- */
    btstack_memory_init();
    btstack_run_loop_init(btstack_run_loop_windows_get_instance());

    /* Dongle selection (hci_transport_usb_set_path_filter is added to the
     * WinUSB transport by patches/apply_patches.py) */
    if (g_usb_filter[0]) hci_transport_usb_set_path_filter(g_usb_filter);
    hci_init(hci_transport_usb_instance(), NULL);

    /* Persistent link-key store */
    g_tlv_impl = btstack_tlv_windows_init_instance(&g_tlv_context, g_keystore_path);
    btstack_tlv_set_instance(g_tlv_impl, &g_tlv_context);
    hci_set_link_key_db(btstack_link_key_db_tlv_get_instance(g_tlv_impl, &g_tlv_context));

    g_hci_event_cb.callback = &on_hci_event;
    hci_add_event_handler(&g_hci_event_cb);

    l2cap_init();

    gap_set_local_name(g_device_name);
    gap_set_class_of_device(g_cod);
    gap_set_default_link_policy_settings(LM_LINK_POLICY_ENABLE_ROLE_SWITCH |
                                         LM_LINK_POLICY_ENABLE_SNIFF_MODE);

    sdp_init();
    setup_sdp();

    /* A2DP Sink + AVRCP (Target + Controller) */
    a2dp_sink_init();
    avrcp_init();
    avrcp_register_packet_handler(&on_avrcp_event);          /* shared: connect/disconnect */
    avrcp_register_incoming_connection_handler(on_avrcp_incoming_connection);
    avrcp_target_init();
    avrcp_target_register_packet_handler(&on_avrcp_target_event);
    avrcp_controller_init();
    avrcp_controller_register_packet_handler(&on_avrcp_controller_event);

    /* Register AVDTP deferred-accept hook BEFORE a2dp_sink registers its L2CAP service */
    avdtp_register_incoming_connection_handler(on_avdtp_incoming_connection);

    a2dp_sink_register_packet_handler(&on_a2dp_sink_event);
    a2dp_sink_register_media_handler(&on_a2dp_media_packet);

    /* Register SBC sink stream endpoints (one per simultaneous source).
     * Byte 0: all sample rates + all channel modes. Byte 1: block lengths /
     * subbands / allocation, narrowed to the user's choice (0 = offer all).
     * The source must pick from what we offer, so a narrowed capability set
     * is how the SBC settings take effect. FFmpeg decodes any combination.
     * The capability buffers must outlive the endpoints. */
    static uint8_t sbc_caps[4] = { 0xFF, 0x00, 2, 0 };
    sbc_caps[1] = sbc_capability_byte1();
    sbc_caps[3] = (uint8_t)g_max_bitpool;
    {
        char msg[96];
        snprintf(msg, sizeof(msg), "sbc caps: blocks=%s subbands=%s alloc=%s max_bitpool=%d",
                 g_sbc_block_length ? (g_sbc_block_length == 4 ? "4" : g_sbc_block_length == 8 ? "8" :
                                       g_sbc_block_length == 12 ? "12" : "16") : "all",
                 g_sbc_subbands ? (g_sbc_subbands == 4 ? "4" : "8") : "all",
                 g_sbc_allocation == 1 ? "loudness" : g_sbc_allocation == 2 ? "snr" : "all",
                 g_max_bitpool);
        emit_log(msg);
    }
    for (int i = 0; i < MAX_CONNECTIONS; i++) {
        a2dp_sink_create_stream_endpoint(AVDTP_AUDIO, AVDTP_CODEC_SBC,
                                         sbc_caps, sizeof(sbc_caps),
                                         g_sbc_cfg[i], sizeof(g_sbc_cfg[i]));
    }

    /* Register AAC sink stream endpoints:
     * MPEG-2 LC + MPEG-4 LC, all common sample rates, stereo + mono */
    static const uint8_t aac_caps[6] = {
        0xC0,   /* object types: MPEG-2 AAC LC | MPEG-4 AAC LC */
        0xFF,   /* sampling frequency bitmap high byte (all rates) */
        0xFC,   /* sampling frequency bitmap low nibble + channels (stereo+mono) */
        0x00,   /* VBR=no, bitrate high=0 */
        0x00,
        0x00,
    };
    for (int i = 0; i < MAX_CONNECTIONS; i++) {
        a2dp_sink_create_stream_endpoint(AVDTP_AUDIO, AVDTP_CODEC_MPEG_2_4_AAC,
                                         aac_caps, sizeof(aac_caps),
                                         g_aac_cfg[i], sizeof(g_aac_cfg[i]));
    }

    /* Optional vendor codecs. Sources pick their preferred codec among the
     * endpoints we expose, and Android prefers aptX over SBC when offered. */
    static const uint8_t aptx_caps[7] = {
        APTX_VENDOR_ID & 0xFF, (APTX_VENDOR_ID >> 8) & 0xFF, (APTX_VENDOR_ID >> 16) & 0xFF, (APTX_VENDOR_ID >> 24) & 0xFF,
        APTX_CODEC_ID & 0xFF, (APTX_CODEC_ID >> 8) & 0xFF,
        APTX_RATE_44100 | APTX_RATE_48000 | APTX_CH_STEREO,
    };
    static const uint8_t aptxhd_caps[11] = {
        APTX_HD_VENDOR_ID & 0xFF, (APTX_HD_VENDOR_ID >> 8) & 0xFF, (APTX_HD_VENDOR_ID >> 16) & 0xFF, (APTX_HD_VENDOR_ID >> 24) & 0xFF,
        APTX_HD_CODEC_ID & 0xFF, (APTX_HD_CODEC_ID >> 8) & 0xFF,
        APTX_RATE_44100 | APTX_RATE_48000 | APTX_CH_STEREO,
        0, 0, 0, 0,
    };
    if (g_vendor_codecs & VENDOR_APTX_HD) {
        for (int i = 0; i < MAX_CONNECTIONS; i++) {
            a2dp_sink_create_stream_endpoint(AVDTP_AUDIO, AVDTP_CODEC_NON_A2DP,
                                             aptxhd_caps, sizeof(aptxhd_caps),
                                             g_aptxhd_cfg[i], sizeof(g_aptxhd_cfg[i]));
        }
        emit_log("codecs: offering aptX HD");
    }
    if (g_vendor_codecs & VENDOR_APTX) {
        for (int i = 0; i < MAX_CONNECTIONS; i++) {
            a2dp_sink_create_stream_endpoint(AVDTP_AUDIO, AVDTP_CODEC_NON_A2DP,
                                             aptx_caps, sizeof(aptx_caps),
                                             g_aptx_cfg[i], sizeof(g_aptx_cfg[i]));
        }
        emit_log("codecs: offering aptX");
    }

    /* ---- stdin command reader (Windows thread) ---- */
    InitializeCriticalSection(&g_cs);
    g_stdin_event = CreateEvent(NULL, TRUE, FALSE, NULL);

    /* The thread runs until process exit; its handle is not needed. */
    HANDLE reader = (HANDLE)_beginthreadex(NULL, 0, stdin_reader_thread, NULL, 0, NULL);
    if (reader) CloseHandle(reader);

    g_stdin_ds.source.handle = g_stdin_event;
    btstack_run_loop_set_data_source_handler(&g_stdin_ds, &stdin_ds_callback);
    btstack_run_loop_enable_data_source_callbacks(&g_stdin_ds, DATA_SOURCE_CALLBACK_READ);
    btstack_run_loop_add_data_source(&g_stdin_ds);

    hci_power_control(HCI_POWER_ON);
    btstack_run_loop_execute();   /* does not return; exit() happens in on_hci_event */
    return 0;
}
