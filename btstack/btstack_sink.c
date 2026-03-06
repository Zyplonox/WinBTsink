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
 *                  {"cmd":"stop"}
 *
 * stdout (binary): SBC audio frames, each prefixed by a header:
 *                  [uint32_le total_len = 6 + sbc_len]
 *                  [6 bytes bd_addr (big-endian, MSB first)]
 *                  [sbc_len bytes SBC payload]
 *
 * stderr (text):   JSON event lines to Python
 *                  {"event":"ready","address":"AA:BB:CC:DD:EE:FF"}
 *                  {"event":"l2cap_request","addr":"...","cid":64}
 *                  {"event":"connected","addr":"...","name":"iPhone"}
 *                  {"event":"disconnected","addr":"..."}
 *                  {"event":"audio_start","addr":"...","sample_rate":44100,"channels":2}
 *                  {"event":"audio_stop","addr":"..."}
 *                  {"event":"log","msg":"..."}
 *                  {"event":"error","msg":"..."}
 *
 * Command-line arguments:
 *   btstack_sink.exe <usb_path> <device_name> <bt_address> <max_bitpool>
 *   Example:
 *   btstack_sink.exe 0 "PC-AudioSink" "AA:BB:CC:DD:EE:FF" 53
 */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#include <windows.h>
#include <io.h>
#include <fcntl.h>
#include <process.h>
#endif

/* BTstack headers */
#include "btstack.h"
#include "btstack_run_loop_windows.h"
#include "hci_transport_usb.h"
#include "classic/a2dp_sink.h"
#include "classic/avdtp.h"
#include "classic/avrcp.h"
#include "classic/avrcp_target.h"
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

#define STDIN_BUF_SIZE          512
#define SBC_STORAGE_SIZE        1024
#define MAX_CONNECTIONS         4       /* max simultaneous A2DP sources */

/* -------------------------------------------------------------------------
 * Per-connection state
 * ---------------------------------------------------------------------- */

typedef struct {
    int      active;            /* slot in use */
    uint16_t a2dp_cid;          /* BTstack A2DP connection identifier */
    uint8_t  local_seid;        /* local stream endpoint ID used by this conn */
    bd_addr_t addr;             /* remote device address */
    char     addr_str[18];      /* "XX:XX:XX:XX:XX:XX" */
    int      sample_rate;
    int      channels;
} a2dp_conn_t;

static a2dp_conn_t g_conns[MAX_CONNECTIONS];

/* Per-SEP SBC config buffers (one per registered endpoint) */
static uint8_t g_sbc_cfg[MAX_CONNECTIONS][4];

/* Local SEIDs assigned to our registered endpoints */
static uint8_t g_local_seids[MAX_CONNECTIONS];

/* -------------------------------------------------------------------------
 * Pending L2CAP connections awaiting Python approve/deny
 * ---------------------------------------------------------------------- */

typedef struct {
    int      valid;
    uint16_t l2cap_cid;
    bd_addr_t addr;
} pending_conn_t;

static pending_conn_t g_pending[MAX_CONNECTIONS];

/* -------------------------------------------------------------------------
 * Global state
 * ---------------------------------------------------------------------- */

static char  g_device_name[64]  = "PC-AudioSink";
static char  g_bt_address[18]   = "";
static int   g_usb_path         = 0;
static int   g_max_bitpool      = 53;
static int   g_discoverable     = 0;  /* set via cmd after ready */
static int   g_debug            = 0;  /* verbose protocol logging when 1 */

/* Bonding / link key persistence via Windows TLV store */
static btstack_tlv_windows_t    g_tlv_context;
static const btstack_tlv_t     *g_tlv_impl = NULL;

/* Set when we intentionally power off (stop command) — suppresses the
   HCI_STATE_OFF error that would otherwise fire during normal shutdown. */
static int g_shutdown_requested = 0;

/* SDP records */
static uint8_t  g_sdp_a2dp_sink_service[150];
static uint8_t  g_sdp_avrcp_service[200];
static uint32_t g_sdp_handle_a2dp  = 0;
static uint32_t g_sdp_handle_avrcp = 0;

/* stdin reader thread */
#ifdef _WIN32
static HANDLE g_stdin_thread = NULL;
static CRITICAL_SECTION g_cs;
#endif

/* btstack registered data source for stdin wakeup */
static btstack_data_source_t g_stdin_ds;
static HANDLE                g_stdin_event;   /* signalled by reader thread */

/* Ring buffer for commands arriving from Python */
#define CMD_BUF_LINES 16
#define CMD_LINE_MAX  256
static char   g_cmd_buf[CMD_BUF_LINES][CMD_LINE_MAX];
static int    g_cmd_head = 0;
static int    g_cmd_tail = 0;

/* -------------------------------------------------------------------------
 * JSON emit helpers
 * ---------------------------------------------------------------------- */

static void emit_event(const char *json) {
    fprintf(stderr, "%s\n", json);
    fflush(stderr);
}

static void emit_log(const char *msg) {
    fprintf(stderr, "{\"event\":\"log\",\"msg\":\"%s\"}\n", msg);
    fflush(stderr);
}

static void emit_error(const char *msg) {
    fprintf(stderr, "{\"event\":\"error\",\"msg\":\"%s\"}\n", msg);
    fflush(stderr);
}

/* Format a bd_addr_t as "XX:XX:XX:XX:XX:XX" into buf (must be >=18 bytes). */
static void addr_to_str(const bd_addr_t addr, char *buf) {
    snprintf(buf, 18, "%02X:%02X:%02X:%02X:%02X:%02X",
             addr[0], addr[1], addr[2], addr[3], addr[4], addr[5]);
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

static a2dp_conn_t *alloc_conn(void) {
    for (int i = 0; i < MAX_CONNECTIONS; i++) {
        if (!g_conns[i].active) {
            memset(&g_conns[i], 0, sizeof(g_conns[i]));
            g_conns[i].active = 1;
            g_conns[i].sample_rate = 44100;
            g_conns[i].channels    = 2;
            return &g_conns[i];
        }
    }
    return NULL;
}

static void free_conn(a2dp_conn_t *conn) {
    if (conn) memset(conn, 0, sizeof(*conn));
}

/* -------------------------------------------------------------------------
 * Pending connection helpers
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
 * SBC audio output — writes addr-tagged length-prefixed frames to stdout
 *
 * Frame format:
 *   [uint32_le total_len = 6 + sbc_len]
 *   [6 bytes bd_addr, byte[0]..byte[5]]
 *   [sbc_len bytes SBC payload]
 * ---------------------------------------------------------------------- */

static void write_sbc_to_stdout(const bd_addr_t addr,
                                 const uint8_t *data, uint16_t len) {
    uint32_t total = 6u + len;
#ifdef _WIN32
    fwrite(&total, 4, 1, stdout);
    fwrite(addr,   1, 6, stdout);
    fwrite(data,   1, len, stdout);
    fflush(stdout);
#else
    fwrite(&total, 4, 1, stdout);
    fwrite(addr,   1, 6, stdout);
    fwrite(data,   1, len, stdout);
    fflush(stdout);
#endif
}

/* -------------------------------------------------------------------------
 * AVDTP deferred-accept API  (patched into btstack-src/src/classic/avdtp.c)
 * ---------------------------------------------------------------------- */

extern void avdtp_register_incoming_connection_handler(
    void (*handler)(uint16_t local_cid, bd_addr_t addr));
extern void avdtp_accept_incoming_connection(uint16_t local_cid);
extern void avdtp_decline_incoming_connection(uint16_t local_cid);

/* Called by patched avdtp.c BEFORE L2CAP accept — true deferred accept */
static void on_avdtp_incoming_connection(uint16_t local_cid, bd_addr_t addr) {
    char addr_str[18];
    addr_to_str(addr, addr_str);
    emit_log("avdtp: incoming connection hook fired");

    /* If this addr already has an established A2DP signaling connection, the
     * new L2CAP connection is the MEDIA channel (opened after AVDTP OPEN).
     * Auto-accept it immediately — no Python round-trip, avoids the timing
     * gap that causes strict sources (e.g. Nintendo Switch 2) to time out. */
    for (int i = 0; i < MAX_CONNECTIONS; i++) {
        if (g_conns[i].active && memcmp(g_conns[i].addr, addr, 6) == 0) {
            emit_log("avdtp: auto-accepting media channel for established connection");
            avdtp_accept_incoming_connection(local_cid);
            return;
        }
    }

    /* First connection from this addr — signaling channel.  Gate via Python. */
    pending_conn_t *p = alloc_pending();
    if (!p) {
        /* No free pending slot — decline immediately */
        emit_log("avdtp: too many pending connections, declining");
        avdtp_decline_incoming_connection(local_cid);
        return;
    }
    p->valid     = 1;
    p->l2cap_cid = local_cid;
    memcpy(p->addr, addr, 6);

    char evt[128];
    snprintf(evt, sizeof(evt),
             "{\"event\":\"l2cap_request\",\"addr\":\"%s\",\"cid\":%u}",
             addr_str, (unsigned)local_cid);
    emit_event(evt);
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

        snprintf(evt, sizeof(evt),
                 "{\"event\":\"connected\",\"addr\":\"%s\",\"name\":\"%s\"}",
                 conn->addr_str, conn->addr_str);
        emit_event(evt);
        break;
    }

    case A2DP_SUBEVENT_SIGNALING_MEDIA_CODEC_SBC_CONFIGURATION: {
        uint16_t cid  = a2dp_subevent_signaling_media_codec_sbc_configuration_get_a2dp_cid(packet);
        uint8_t  seid = a2dp_subevent_signaling_media_codec_sbc_configuration_get_local_seid(packet);
        a2dp_conn_t *conn = find_conn_by_cid(cid);
        if (conn) {
            conn->local_seid  = seid;
            conn->sample_rate = a2dp_subevent_signaling_media_codec_sbc_configuration_get_sampling_frequency(packet);
            conn->channels    = a2dp_subevent_signaling_media_codec_sbc_configuration_get_num_channels(packet);
            if (g_debug) {
                char dbg[128];
                snprintf(dbg, sizeof(dbg),
                         "SBC config: seid=%u rate=%d ch=%d bitpool=%u..%u",
                         seid, conn->sample_rate, conn->channels,
                         a2dp_subevent_signaling_media_codec_sbc_configuration_get_min_bitpool_value(packet),
                         a2dp_subevent_signaling_media_codec_sbc_configuration_get_max_bitpool_value(packet));
                emit_log(dbg);
            }
        }
        break;
    }

    case A2DP_SUBEVENT_STREAM_STARTED: {
        uint16_t cid = a2dp_subevent_stream_started_get_a2dp_cid(packet);
        a2dp_conn_t *conn = find_conn_by_cid(cid);
        if (conn) {
            snprintf(evt, sizeof(evt),
                     "{\"event\":\"audio_start\",\"addr\":\"%s\","
                     "\"sample_rate\":%d,\"channels\":%d}",
                     conn->addr_str, conn->sample_rate, conn->channels);
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
            free_conn(conn);
        }
        break;
    }

    default:
        break;
    }
}

/* -------------------------------------------------------------------------
 * A2DP media data handler — receives RTP packets with SBC payload
 * ---------------------------------------------------------------------- */

static void on_a2dp_media_packet(uint8_t seid, uint8_t *packet, uint16_t size) {
    /*
     * BTstack does NOT strip the RTP header before calling this callback
     * (confirmed by a2dp_sink_demo.c which manually parses it).
     * Packet layout:
     *   [12 bytes RTP header (fixed, assuming CC=0, no extension)]
     *   [ 1 byte  A2DP SBC media payload header (num_frames etc.)]
     *   [ N bytes raw SBC frames, each starting with sync word 0x9C]
     *
     * We skip 13 bytes total to reach the raw SBC frames for FFmpeg.
     */
    if (size < 14) return;

    a2dp_conn_t *conn = find_conn_by_seid(seid);
    if (!conn) return;  /* unknown seid — skip */

    write_sbc_to_stdout(conn->addr, packet + 13, size - 13);
}

/* -------------------------------------------------------------------------
 * HCI packet handler — GAP events (inquiry, power-on, etc.)
 * ---------------------------------------------------------------------- */

static btstack_packet_callback_registration_t g_hci_event_cb;

static void on_hci_event(uint8_t packet_type, uint16_t channel,
                          uint8_t *packet, uint16_t size) {
    UNUSED(channel);
    UNUSED(size);

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
            emit_log(g_debug ? "build: media-auto-accept+debug v8" : "build: media-auto-accept v8");

            /* Apply initial discoverability (off by default, Python will
               send set_discoverable when the GUI toggle is set). */
            gap_discoverable_control(g_discoverable);
            gap_connectable_control(1);
        } else if (bt_state == HCI_STATE_OFF) {
            if (!g_shutdown_requested) {
                emit_event("{\"event\":\"error\",\"msg\":\"HCI powered off unexpectedly — USB dongle not accessible. Check WinUSB driver (Zadig) and kill any zombie btstack_sink.exe.\"}");
            }
            btstack_run_loop_trigger_exit();
        }
        break;
    }

    case HCI_EVENT_PIN_CODE_REQUEST:
        /* Legacy PIN: accept with empty PIN (no MITM for audio devices) */
        {
            bd_addr_t bd;
            hci_event_pin_code_request_get_bd_addr(packet, bd);
            gap_pin_code_response(bd, "0000");
        }
        break;

    case HCI_EVENT_USER_CONFIRMATION_REQUEST:
        /* SSP numeric comparison: auto-confirm */
        {
            bd_addr_t bd;
            hci_event_user_confirmation_request_get_bd_addr(packet, bd);
            gap_ssp_confirmation_response(bd);
        }
        break;

    default:
        break;
    }
}

/* -------------------------------------------------------------------------
 * Command processing — called from BTstack run loop
 * ---------------------------------------------------------------------- */

static void process_command(const char *line) {
    /* Minimal JSON parser — looks for "cmd" and relevant fields.
     * We keep it dependency-free (no cJSON etc.) */

    char cmd[64] = "";
    int enabled  = -1;
    uint16_t cid = 0;

    /* Extract "cmd" value */
    {
        const char *p = strstr(line, "\"cmd\"");
        if (p) {
            p += 5;
            while (*p && *p != '"') p++;
            if (*p == '"') {
                p++;
                int i = 0;
                while (*p && *p != '"' && i < 63) cmd[i++] = *p++;
                cmd[i] = '\0';
            }
        }
    }

    /* Extract "enabled" value */
    {
        const char *p = strstr(line, "\"enabled\"");
        if (p) {
            p += 9;
            while (*p && (*p == ':' || *p == ' ')) p++;
            if (strncmp(p, "true", 4) == 0)  enabled = 1;
            if (strncmp(p, "false", 5) == 0) enabled = 0;
        }
    }

    /* Extract "cid" value */
    {
        const char *p = strstr(line, "\"cid\"");
        if (p) {
            p += 5;
            while (*p && (*p == ':' || *p == ' ')) p++;
            cid = (uint16_t)atoi(p);
        }
    }

    if (strcmp(cmd, "approve") == 0) {
        pending_conn_t *p = find_pending_by_cid(cid);
        if (p) {
            emit_log("avdtp: accepting incoming connection");
            avdtp_accept_incoming_connection(p->l2cap_cid);
            p->valid = 0;
        }
    }
    else if (strcmp(cmd, "deny") == 0) {
        pending_conn_t *p = find_pending_by_cid(cid);
        if (p) {
            emit_log("avdtp: declining incoming connection");
            avdtp_decline_incoming_connection(p->l2cap_cid);
            p->valid = 0;
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
    else if (strcmp(cmd, "stop") == 0) {
        emit_log("stop command received");
        g_shutdown_requested = 1;
        hci_power_control(HCI_POWER_OFF);
        /* Run loop will exit after HCI_STATE_OFF */
    }
}

/* -------------------------------------------------------------------------
 * stdin reader thread — reads lines into g_cmd_buf, signals g_stdin_event
 * ---------------------------------------------------------------------- */

#ifdef _WIN32
static unsigned __stdcall stdin_reader_thread(void *arg) {
    UNUSED(arg);
    char line[CMD_LINE_MAX];
    while (fgets(line, sizeof(line), stdin)) {
        /* strip newline */
        int len = (int)strlen(line);
        while (len > 0 && (line[len-1] == '\n' || line[len-1] == '\r'))
            line[--len] = '\0';
        if (len == 0) continue;

        EnterCriticalSection(&g_cs);
        int next = (g_cmd_head + 1) % CMD_BUF_LINES;
        if (next != g_cmd_tail) {  /* not full */
            strncpy(g_cmd_buf[g_cmd_head], line, CMD_LINE_MAX - 1);
            g_cmd_buf[g_cmd_head][CMD_LINE_MAX - 1] = '\0';
            g_cmd_head = next;
        }
        LeaveCriticalSection(&g_cs);

        /* Wake up BTstack run loop */
        SetEvent(g_stdin_event);
    }
    return 0;
}
#endif

/* -------------------------------------------------------------------------
 * BTstack data source callback — drains g_cmd_buf in the run loop thread
 * ---------------------------------------------------------------------- */

static void stdin_ds_callback(btstack_data_source_t *ds, btstack_data_source_callback_type_t type) {
    UNUSED(ds);
    UNUSED(type);

    /* Drain all buffered commands */
    for (;;) {
        char line[CMD_LINE_MAX];

        EnterCriticalSection(&g_cs);
        if (g_cmd_tail == g_cmd_head) {
            LeaveCriticalSection(&g_cs);
            break;
        }
        strncpy(line, g_cmd_buf[g_cmd_tail], CMD_LINE_MAX - 1);
        line[CMD_LINE_MAX - 1] = '\0';
        g_cmd_tail = (g_cmd_tail + 1) % CMD_BUF_LINES;
        LeaveCriticalSection(&g_cs);

        process_command(line);
    }

    /* Reset the win32 event so we're not called again spuriously */
    ResetEvent(g_stdin_event);
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
    g_sdp_handle_a2dp = sdp_register_service(g_sdp_a2dp_sink_service);

    /* AVRCP Target service record (required by many A2DP sources) */
    memset(g_sdp_avrcp_service, 0, sizeof(g_sdp_avrcp_service));
    avrcp_target_create_sdp_record(g_sdp_avrcp_service,
                                   sdp_create_service_record_handle(),
                                   AVRCP_FEATURE_MASK_CATEGORY_PLAYER_OR_RECORDER,
                                   NULL, NULL);
    g_sdp_handle_avrcp = sdp_register_service(g_sdp_avrcp_service);
}

/* -------------------------------------------------------------------------
 * main
 * ---------------------------------------------------------------------- */

int main(int argc, char *argv[]) {
#ifdef _WIN32
    /* Switch stdout to binary mode so SBC frames aren't mangled */
    _setmode(_fileno(stdout), _O_BINARY);
    /* stdin in text mode (line-based JSON commands) */
    _setmode(_fileno(stdin), _O_TEXT);
#endif

    /* Parse arguments: <usb_path_index> <device_name> <bt_address> <max_bitpool> [debug] */
    if (argc >= 2) g_usb_path    = atoi(argv[1]);
    if (argc >= 3) strncpy(g_device_name, argv[2], sizeof(g_device_name) - 1);
    if (argc >= 4) strncpy(g_bt_address,  argv[3], sizeof(g_bt_address) - 1);
    if (argc >= 5) g_max_bitpool = atoi(argv[4]);
    if (argc >= 6) g_debug       = atoi(argv[5]);

    /* Compute TLV key-store path next to this executable */
    char tlv_path[MAX_PATH] = "btstack_keys.db";
    {
        char module_path[MAX_PATH];
        if (GetModuleFileNameA(NULL, module_path, MAX_PATH)) {
            char *last_sep = strrchr(module_path, '\\');
            if (last_sep) {
                *(last_sep + 1) = '\0';
                snprintf(tlv_path, sizeof(tlv_path), "%sbtstack_keys.db", module_path);
            }
        }
    }

    /* ---- BTstack init ---- */
    btstack_memory_init();
    btstack_run_loop_init(btstack_run_loop_windows_get_instance());

    /* HCI transport: WinUSB (no config struct needed for USB) */
    hci_init(hci_transport_usb_instance(), NULL);

    /* Persistent link-key store — survives restarts so phones don't need to re-pair */
    g_tlv_impl = btstack_tlv_windows_init_instance(&g_tlv_context, tlv_path);
    btstack_tlv_set_instance(g_tlv_impl, &g_tlv_context);
    hci_set_link_key_db(btstack_link_key_db_tlv_get_instance(g_tlv_impl, &g_tlv_context));

    /* Register HCI event handler (GAP events, power-on, etc.) */
    g_hci_event_cb.callback = &on_hci_event;
    hci_add_event_handler(&g_hci_event_cb);

    /* L2CAP — must be initialised before any L2CAP service (AVDTP etc.) */
    l2cap_init();

    /* GAP setup */
    gap_set_local_name(g_device_name);
    gap_set_class_of_device(0x240418);  /* Rendering|Audio svc | A/V major | Headphones */
    gap_set_default_link_policy_settings(LM_LINK_POLICY_ENABLE_ROLE_SWITCH |
                                         LM_LINK_POLICY_ENABLE_SNIFF_MODE);

    /* SDP */
    sdp_init();
    setup_sdp();

    /* A2DP Sink + AVRCP Target */
    a2dp_sink_init();
    avrcp_init();
    avrcp_target_init();

    /* Register deferred-accept hook BEFORE a2dp_sink registers its L2CAP service */
    avdtp_register_incoming_connection_handler(on_avdtp_incoming_connection);

    /* A2DP event + media callbacks */
    a2dp_sink_register_packet_handler(&on_a2dp_sink_event);
    a2dp_sink_register_media_handler(&on_a2dp_media_packet);

    /* Register MAX_CONNECTIONS local SBC sink stream endpoints.
     * Each endpoint can serve one simultaneous A2DP source.
     * SBC capabilities: 2 raw bytes in A2DP/AVDTP wire format:
     *   byte 0: sampling_freq (bits 7-4) | channel_mode (bits 3-0)
     *   byte 1: block_length  (bits 7-4) | subbands (bits 3-2) | alloc (bits 1-0)
     * 0xFF 0xFF = accept all combinations. */
    {
        static uint8_t sbc_caps[4] = {
            0xFF,  /* all sample rates + all channel modes */
            0xFF,  /* all block lengths + subbands + alloc methods */
            2,     /* min bitpool */
            53     /* max bitpool — overwritten below */
        };
        sbc_caps[3] = (uint8_t)g_max_bitpool;

        for (int i = 0; i < MAX_CONNECTIONS; i++) {
            avdtp_stream_endpoint_t *sep = a2dp_sink_create_stream_endpoint(
                AVDTP_AUDIO, AVDTP_CODEC_SBC,
                sbc_caps, sizeof(sbc_caps),
                g_sbc_cfg[i], sizeof(g_sbc_cfg[i]));
            if (sep) {
                g_local_seids[i] = avdtp_local_seid(sep);
            }
        }
    }

    /* ---- stdin command reader (Windows thread) ---- */
    InitializeCriticalSection(&g_cs);
    g_stdin_event = CreateEvent(NULL, TRUE, FALSE, NULL);  /* manual-reset */

    g_stdin_thread = (HANDLE)_beginthreadex(
        NULL, 0, stdin_reader_thread, NULL, 0, NULL);

    /* Register stdin event as a BTstack data source (Windows HANDLE in source.handle) */
    g_stdin_ds.source.handle = g_stdin_event;
    btstack_run_loop_set_data_source_handler(&g_stdin_ds, &stdin_ds_callback);
    btstack_run_loop_enable_data_source_callbacks(&g_stdin_ds, DATA_SOURCE_CALLBACK_READ);
    btstack_run_loop_add_data_source(&g_stdin_ds);

    /* ---- Power on and run ---- */
    hci_power_control(HCI_POWER_ON);
    btstack_run_loop_execute();

    /* Cleanup */
    WaitForSingleObject(g_stdin_thread, 2000);
    DeleteCriticalSection(&g_cs);
    CloseHandle(g_stdin_event);

    return 0;
}
