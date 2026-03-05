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
 * stdout (binary): SBC audio frames, each prefixed by uint32_le length
 *                  [4 bytes len][len bytes SBC payload]
 *
 * stderr (text):   JSON event lines to Python
 *                  {"event":"ready","address":"AA:BB:CC:DD:EE:FF"}
 *                  {"event":"l2cap_request","addr":"...","cid":64}
 *                  {"event":"connected","addr":"...","name":"iPhone"}
 *                  {"event":"disconnected","addr":"..."}
 *                  {"event":"audio_start","sample_rate":44100,"channels":2}
 *                  {"event":"audio_stop"}
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
#include "hci_transport_h2_winusb.h"
#include "classic/a2dp_sink.h"
#include "classic/avdtp.h"
#include "classic/avrcp.h"
#include "classic/avrcp_target.h"
#include "classic/sdp_server.h"
#include "classic/a2dp.h"
#include "classic/avdtp_util.h"
#include "bluetooth_sdp.h"

/* -------------------------------------------------------------------------
 * Configuration
 * ---------------------------------------------------------------------- */

#define MAX_PENDING_CONNECTIONS 4
#define STDIN_BUF_SIZE          512
#define SBC_STORAGE_SIZE        1024

/* -------------------------------------------------------------------------
 * Global state
 * ---------------------------------------------------------------------- */

static char  g_device_name[64]  = "PC-AudioSink";
static char  g_bt_address[18]   = "";
static int   g_usb_path         = 0;
static int   g_max_bitpool      = 53;
static int   g_discoverable     = 0;  /* set via cmd after ready */

/* Current A2DP connection */
static uint8_t  g_a2dp_local_seid  = 0;
static uint16_t g_a2dp_cid         = 0;
static char     g_peer_addr_str[18] = "";

/* SDP records */
static uint8_t  g_sdp_a2dp_sink_service[150];
static uint8_t  g_sdp_avrcp_service[200];
static uint32_t g_sdp_handle_a2dp  = 0;
static uint32_t g_sdp_handle_avrcp = 0;

/* Pending L2CAP connections awaiting Python approval */
typedef struct {
    uint16_t cid;
    bd_addr_t addr;
    int      in_use;
} pending_conn_t;

static pending_conn_t g_pending[MAX_PENDING_CONNECTIONS];

/* SBC codec params extracted from SET_CONFIG */
static int g_sample_rate = 44100;
static int g_channels    = 2;

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
 * SBC audio output — writes length-prefixed frames to stdout
 * ---------------------------------------------------------------------- */

static void write_sbc_to_stdout(const uint8_t *data, uint16_t len) {
    uint32_t le_len = len;  /* little-endian on x86 already */
#ifdef _WIN32
    /* ensure stdout is binary mode */
    fwrite(&le_len, 4, 1, stdout);
    fwrite(data, 1, len, stdout);
    fflush(stdout);
#else
    fwrite(&le_len, 4, 1, stdout);
    fwrite(data, 1, len, stdout);
    fflush(stdout);
#endif
}

/* -------------------------------------------------------------------------
 * Pending connection management
 * ---------------------------------------------------------------------- */

static pending_conn_t *pending_find_by_cid(uint16_t cid) {
    for (int i = 0; i < MAX_PENDING_CONNECTIONS; i++) {
        if (g_pending[i].in_use && g_pending[i].cid == cid) {
            return &g_pending[i];
        }
    }
    return NULL;
}

static pending_conn_t *pending_find_by_addr(const bd_addr_t addr) {
    for (int i = 0; i < MAX_PENDING_CONNECTIONS; i++) {
        if (g_pending[i].in_use &&
            memcmp(g_pending[i].addr, addr, 6) == 0) {
            return &g_pending[i];
        }
    }
    return NULL;
}

static void pending_add(uint16_t cid, const bd_addr_t addr) {
    for (int i = 0; i < MAX_PENDING_CONNECTIONS; i++) {
        if (!g_pending[i].in_use) {
            g_pending[i].in_use = 1;
            g_pending[i].cid    = cid;
            memcpy(g_pending[i].addr, addr, 6);
            return;
        }
    }
}

static void pending_remove(uint16_t cid) {
    for (int i = 0; i < MAX_PENDING_CONNECTIONS; i++) {
        if (g_pending[i].in_use && g_pending[i].cid == cid) {
            g_pending[i].in_use = 0;
        }
    }
}

/* -------------------------------------------------------------------------
 * AVDTP deferred-accept API
 * (declared extern — implemented in patched avdtp.c)
 * ---------------------------------------------------------------------- */

extern void avdtp_register_incoming_connection_handler(
    void (*handler)(uint16_t local_cid, bd_addr_t addr));
extern void avdtp_accept_incoming_connection(uint16_t local_cid);
extern void avdtp_decline_incoming_connection(uint16_t local_cid);

/* -------------------------------------------------------------------------
 * Incoming L2CAP AVDTP connection handler (called from patched avdtp.c)
 * ---------------------------------------------------------------------- */

static void on_avdtp_incoming_connection(uint16_t local_cid, bd_addr_t addr) {
    char addr_str[18];
    addr_to_str(addr, addr_str);

    pending_add(local_cid, addr);

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
    char addr_str[18];
    char evt[256];

    switch (subevent) {

    case A2DP_SUBEVENT_INCOMING_CONNECTION_ESTABLISHED:
        /* Connection fully established (L2CAP + AVDTP both open) */
        g_a2dp_cid = a2dp_subevent_incoming_connection_established_get_a2dp_cid(packet);
        a2dp_subevent_incoming_connection_established_get_bd_addr(packet,
            (bd_addr_t)(void *)g_peer_addr_str);  /* reuse string slot */
        {
            bd_addr_t bd;
            a2dp_subevent_incoming_connection_established_get_bd_addr(packet, bd);
            addr_to_str(bd, g_peer_addr_str);
        }
        snprintf(evt, sizeof(evt),
                 "{\"event\":\"connected\",\"addr\":\"%s\",\"name\":\"%s\"}",
                 g_peer_addr_str, g_peer_addr_str);
        emit_event(evt);
        break;

    case A2DP_SUBEVENT_STREAM_STARTED:
        /* Codec configuration is available here */
        {
            uint8_t seid = a2dp_subevent_stream_started_get_local_seid(packet);
            avdtp_media_codec_configuration_sbc_t sbc_cfg;
            (void)seid;
            /* Use the sample rate/channels stored during SET_CONFIG */
            snprintf(evt, sizeof(evt),
                     "{\"event\":\"audio_start\",\"sample_rate\":%d,\"channels\":%d}",
                     g_sample_rate, g_channels);
            emit_event(evt);
        }
        break;

    case A2DP_SUBEVENT_STREAM_SUSPENDED:
    case A2DP_SUBEVENT_STREAM_STOPPED:
        emit_event("{\"event\":\"audio_stop\"}");
        break;

    case A2DP_SUBEVENT_CONNECTION_RELEASED:
        snprintf(evt, sizeof(evt),
                 "{\"event\":\"disconnected\",\"addr\":\"%s\"}",
                 g_peer_addr_str);
        emit_event(evt);
        g_a2dp_cid = 0;
        g_peer_addr_str[0] = '\0';
        break;

    case A2DP_SUBEVENT_CODEC_CONFIGURED:
        /* Extract sample rate and channel mode from SBC config */
        {
            uint8_t seid        = a2dp_subevent_codec_configured_get_local_seid(packet);
            uint8_t sf_flags    = a2dp_subevent_codec_configured_get_sampling_frequency(packet);
            uint8_t ch_flags    = a2dp_subevent_codec_configured_get_channel_mode(packet);
            (void)seid;

            /* sampling_frequency bitmask: bit3=16k, bit2=32k, bit1=44.1k, bit0=48k */
            if      (sf_flags & 0x01) g_sample_rate = 48000;
            else if (sf_flags & 0x02) g_sample_rate = 44100;
            else if (sf_flags & 0x04) g_sample_rate = 32000;
            else                      g_sample_rate = 16000;

            /* channel_mode: bit1=stereo/joint-stereo, bit0=mono/dual */
            g_channels = (ch_flags & 0x03) ? 2 : 1;
        }
        break;

    default:
        break;
    }
}

/* -------------------------------------------------------------------------
 * A2DP media data handler — receives RTP packets with SBC payload
 * ---------------------------------------------------------------------- */

static void on_a2dp_media_packet(uint8_t seid, uint8_t *packet, uint16_t size) {
    UNUSED(seid);

    /*
     * RTP header is already stripped by BTstack; 'packet' starts with the
     * SBC media payload header (1 byte: fragment/RFA/number_of_frames)
     * followed by the raw SBC frames.
     * We skip that 1-byte header to get raw SBC frames for FFmpeg.
     */
    if (size < 2) return;
    write_sbc_to_stdout(packet + 1, size - 1);
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
    case BTSTACK_EVENT_STATE:
        if (btstack_event_state_get_state(packet) == HCI_STATE_WORKING) {
            bd_addr_t local_addr;
            gap_local_bd_addr(local_addr);
            char addr_str[18];
            addr_to_str(local_addr, addr_str);

            snprintf(evt, sizeof(evt),
                     "{\"event\":\"ready\",\"address\":\"%s\"}", addr_str);
            emit_event(evt);

            /* Apply initial discoverability (off by default, Python will
               send set_discoverable when the GUI toggle is set). */
            gap_discoverable_control(g_discoverable);
            gap_connectable_control(1);
        }
        break;

    case HCI_EVENT_PIN_CODE_REQUEST:
        /* Legacy PIN: accept with empty PIN (no MITM for audio devices) */
        hci_event_pin_code_request_get_bd_addr(packet, (bd_addr_t)(void *)evt);
        gap_pin_code_response((bd_addr_t)(void *)evt, "0000");
        break;

    case HCI_EVENT_USER_CONFIRMATION_REQUEST:
        /* SSP numeric comparison: auto-confirm */
        hci_event_user_confirmation_request_get_bd_addr(packet, (bd_addr_t)(void *)evt);
        gap_ssp_confirmation_response((bd_addr_t)(void *)evt);
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

    char cmd[64]  = "";
    char addr[18] = "";
    unsigned cid  = 0;
    int enabled   = -1;

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

    /* Extract "addr" value */
    {
        const char *p = strstr(line, "\"addr\"");
        if (p) {
            p += 6;
            while (*p && *p != '"') p++;
            if (*p == '"') {
                p++;
                int i = 0;
                while (*p && *p != '"' && i < 17) addr[i++] = *p++;
                addr[i] = '\0';
            }
        }
    }

    /* Extract "cid" value */
    {
        const char *p = strstr(line, "\"cid\"");
        if (p) {
            p += 5;
            while (*p && (*p == ':' || *p == ' ')) p++;
            cid = (unsigned)atoi(p);
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

    if (strcmp(cmd, "approve") == 0) {
        if (cid != 0) {
            emit_log("avdtp: accepting l2cap connection");
            avdtp_accept_incoming_connection((uint16_t)cid);
            pending_remove((uint16_t)cid);
        }
    }
    else if (strcmp(cmd, "deny") == 0) {
        if (cid != 0) {
            emit_log("avdtp: declining l2cap connection");
            avdtp_decline_incoming_connection((uint16_t)cid);
            pending_remove((uint16_t)cid);
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

    /* Parse arguments: <usb_path_index> <device_name> <bt_address> <max_bitpool> */
    if (argc >= 2) g_usb_path    = atoi(argv[1]);
    if (argc >= 3) strncpy(g_device_name, argv[2], sizeof(g_device_name) - 1);
    if (argc >= 4) strncpy(g_bt_address,  argv[3], sizeof(g_bt_address) - 1);
    if (argc >= 5) g_max_bitpool = atoi(argv[4]);

    /* ---- BTstack init ---- */
    btstack_memory_init();
    btstack_run_loop_init(btstack_run_loop_windows_get_instance());

    /* HCI transport: WinUSB */
    hci_transport_config_usb_t usb_cfg;
    usb_cfg.type        = HCI_TRANSPORT_CONFIG_USB;
    usb_cfg.path_source = g_usb_path;

    hci_init(hci_transport_h2_winusb_instance(), &usb_cfg);

    /* Register HCI event handler (GAP events, power-on, etc.) */
    g_hci_event_cb.callback = &on_hci_event;
    hci_add_event_handler(&g_hci_event_cb);

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

    /* Register the deferred-accept handler BEFORE a2dp_sink registers
       its internal AVDTP listener (order matters in BTstack). */
    avdtp_register_incoming_connection_handler(on_avdtp_incoming_connection);

    /* A2DP event + media callbacks */
    a2dp_sink_register_packet_handler(&on_a2dp_sink_event);
    a2dp_sink_register_media_handler(&on_a2dp_media_packet);

    /* Register a local SBC sink stream endpoint */
    {
        avdtp_media_codec_configuration_sbc_t sbc_caps;
        memset(&sbc_caps, 0, sizeof(sbc_caps));
        sbc_caps.sampling_frequency =
            AVDTP_SBC_44100 | AVDTP_SBC_48000;
        sbc_caps.channel_mode =
            AVDTP_SBC_JOINT_STEREO | AVDTP_SBC_STEREO |
            AVDTP_SBC_DUAL_CHANNEL | AVDTP_SBC_MONO;
        sbc_caps.block_length =
            AVDTP_SBC_BLOCK_LENGTH_16 | AVDTP_SBC_BLOCK_LENGTH_12 |
            AVDTP_SBC_BLOCK_LENGTH_8  | AVDTP_SBC_BLOCK_LENGTH_4;
        sbc_caps.subbands =
            AVDTP_SBC_SUBBANDS_8 | AVDTP_SBC_SUBBANDS_4;
        sbc_caps.allocation_method =
            AVDTP_SBC_ALLOCATION_METHOD_LOUDNESS |
            AVDTP_SBC_ALLOCATION_METHOD_SNR;
        sbc_caps.min_bitpool_value = 2;
        sbc_caps.max_bitpool_value = (uint8_t)g_max_bitpool;

        g_a2dp_local_seid = a2dp_sink_create_stream_endpoint(
            AVDTP_AUDIO, AVDTP_CODEC_SBC,
            (uint8_t *)&sbc_caps, sizeof(sbc_caps),
            NULL, 0);
    }

    /* ---- stdin command reader (Windows thread) ---- */
    InitializeCriticalSection(&g_cs);
    g_stdin_event = CreateEvent(NULL, TRUE, FALSE, NULL);  /* manual-reset */

    g_stdin_thread = (HANDLE)_beginthreadex(
        NULL, 0, stdin_reader_thread, NULL, 0, NULL);

    /* Register stdin as a BTstack data source using the Win32 HANDLE */
    btstack_run_loop_windows_add_handle_callback(
        &g_stdin_ds, g_stdin_event, &stdin_ds_callback);

    /* ---- Power on and run ---- */
    hci_power_control(HCI_POWER_ON);
    btstack_run_loop_execute();

    /* Cleanup */
    WaitForSingleObject(g_stdin_thread, 2000);
    DeleteCriticalSection(&g_cs);
    CloseHandle(g_stdin_event);

    return 0;
}
