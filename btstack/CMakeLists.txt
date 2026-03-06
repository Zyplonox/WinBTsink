cmake_minimum_required(VERSION 3.16)
project(btstack_sink C)

set(CMAKE_C_STANDARD 11)

# ── BTstack root ─────────────────────────────────────────────────────────────
if(NOT DEFINED BTSTACK_ROOT)
    set(BTSTACK_ROOT "${CMAKE_SOURCE_DIR}/btstack-src")
endif()

if(NOT EXISTS "${BTSTACK_ROOT}/src/btstack.h")
    message(FATAL_ERROR
        "BTstack source not found at ${BTSTACK_ROOT}.\n"
        "Run btstack/build.ps1 first, or pass -DBTSTACK_ROOT=<path>.")
endif()

# ── Source directories ────────────────────────────────────────────────────────
set(BT_SRC       "${BTSTACK_ROOT}/src")
set(BT_CLASSIC   "${BTSTACK_ROOT}/src/classic")
set(BT_PLATFORM  "${BTSTACK_ROOT}/platform/windows")
set(BT_POSIX     "${BTSTACK_ROOT}/platform/posix")
set(BT_BLUEDROID_ENC "${BTSTACK_ROOT}/3rd-party/bluedroid/encoder/srce")
set(BT_BLUEDROID_DEC "${BTSTACK_ROOT}/3rd-party/bluedroid/decoder/srce")
set(BT_RIJNDAEL  "${BTSTACK_ROOT}/3rd-party/rijndael")
set(BT_UECC      "${BTSTACK_ROOT}/3rd-party/micro-ecc")
set(BT_LC3       "${BTSTACK_ROOT}/3rd-party/lc3-google/src")

# ── Collect sources ───────────────────────────────────────────────────────────
file(GLOB BT_CORE_SRCS    "${BT_SRC}/*.c")
file(GLOB BT_CLASSIC_SRCS "${BT_CLASSIC}/*.c")
file(GLOB BT_BLUEDROID_SRCS
    "${BT_BLUEDROID_ENC}/*.c"
    "${BT_BLUEDROID_DEC}/*.c"
)
file(GLOB BT_LC3_SRCS     "${BT_LC3}/*.c")

# Remove files that require external/commercial libraries or optional features not used
list(FILTER BT_CORE_SRCS EXCLUDE REGEX "btstack_lc3plus_fraunhofer\\.c$")
list(FILTER BT_CLASSIC_SRCS EXCLUDE REGEX "pbap_client\\.c$")
list(FILTER BT_CLASSIC_SRCS EXCLUDE REGEX "avrcp_browsing")

set(BT_PLATFORM_SRCS
    "${BT_PLATFORM}/btstack_run_loop_windows.c"
    "${BT_PLATFORM}/hci_transport_h2_winusb.c"
    "${BT_PLATFORM}/btstack_stdin_windows.c"
    "${BT_PLATFORM}/btstack_tlv_windows.c"
)

set(BT_3RDPARTY_SRCS
    "${BT_RIJNDAEL}/rijndael.c"
    "${BT_UECC}/uECC.c"
)

# ── Executable ────────────────────────────────────────────────────────────────
add_executable(btstack_sink
    "${CMAKE_SOURCE_DIR}/btstack_sink.c"
    ${BT_CORE_SRCS}
    ${BT_CLASSIC_SRCS}
    ${BT_BLUEDROID_SRCS}
    ${BT_LC3_SRCS}
    ${BT_PLATFORM_SRCS}
    ${BT_3RDPARTY_SRCS}
)

# ── Include paths ─────────────────────────────────────────────────────────────
target_include_directories(btstack_sink PRIVATE
    "${CMAKE_SOURCE_DIR}"           # btstack_config.h lives here
    "${BT_SRC}"
    "${BT_CLASSIC}"
    "${BT_PLATFORM}"
    "${BT_POSIX}"
    "${BTSTACK_ROOT}/platform/embedded"
    "${BTSTACK_ROOT}/3rd-party/bluedroid/encoder/include"
    "${BTSTACK_ROOT}/3rd-party/bluedroid/decoder/include"
    "${BTSTACK_ROOT}/3rd-party/lc3-google/include"
    "${BTSTACK_ROOT}/3rd-party/micro-ecc"
    "${BTSTACK_ROOT}/3rd-party/rijndael"
    "${BTSTACK_ROOT}/3rd-party/md5"
)

# ── Windows libraries ─────────────────────────────────────────────────────────
target_link_libraries(btstack_sink PRIVATE
    setupapi
    winusb
    ws2_32
)

# ── Compile definitions ───────────────────────────────────────────────────────
# Note: ENABLE_CLASSIC and other features are defined in btstack_config.h
target_compile_definitions(btstack_sink PRIVATE
    UNUSED=
)

# ── Release optimisation ──────────────────────────────────────────────────────
if(CMAKE_BUILD_TYPE STREQUAL "Release")
    target_compile_options(btstack_sink PRIVATE -O2)
endif()

# ── Output directory ─────────────────────────────────────────────────────────
set_target_properties(btstack_sink PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build"
)

message(STATUS "BTstack root : ${BTSTACK_ROOT}")
message(STATUS "Output dir   : ${CMAKE_SOURCE_DIR}/build")
