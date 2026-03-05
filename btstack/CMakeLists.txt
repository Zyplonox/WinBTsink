cmake_minimum_required(VERSION 3.16)
project(btstack_sink C)

set(CMAKE_C_STANDARD 11)

# ── BTstack root (set via -DBTSTACK_ROOT=... or auto-detected sibling) ──────
if(NOT DEFINED BTSTACK_ROOT)
    set(BTSTACK_ROOT "${CMAKE_SOURCE_DIR}/btstack-src")
endif()

if(NOT EXISTS "${BTSTACK_ROOT}/src/btstack.h")
    message(FATAL_ERROR
        "BTstack source not found at ${BTSTACK_ROOT}.\n"
        "Run btstack/build.ps1 first, or pass -DBTSTACK_ROOT=<path>.")
endif()

# ── BTstack source directories ───────────────────────────────────────────────
set(BT_SRC      "${BTSTACK_ROOT}/src")
set(BT_CLASSIC  "${BTSTACK_ROOT}/src/classic")
set(BT_PLATFORM "${BTSTACK_ROOT}/platform/windows")
set(BT_CHIP     "${BTSTACK_ROOT}/chipset")

# ── Collect BTstack sources ──────────────────────────────────────────────────
file(GLOB BT_CORE_SRCS
    "${BT_SRC}/*.c"
)

file(GLOB BT_CLASSIC_SRCS
    "${BT_CLASSIC}/*.c"
)

# Windows platform backend + WinUSB HCI transport
set(BT_PLATFORM_SRCS
    "${BT_PLATFORM}/btstack_run_loop_windows.c"
    "${BT_PLATFORM}/hci_transport_h2_winusb.c"
    "${BT_PLATFORM}/btstack_stdin_windows.c"
)

# ── Application source ───────────────────────────────────────────────────────
set(APP_SRCS
    "${CMAKE_SOURCE_DIR}/btstack_sink.c"
)

# ── Executable ───────────────────────────────────────────────────────────────
add_executable(btstack_sink
    ${APP_SRCS}
    ${BT_CORE_SRCS}
    ${BT_CLASSIC_SRCS}
    ${BT_PLATFORM_SRCS}
)

# ── Include paths ────────────────────────────────────────────────────────────
target_include_directories(btstack_sink PRIVATE
    "${BT_SRC}"
    "${BT_CLASSIC}"
    "${BT_PLATFORM}"
    "${BTSTACK_ROOT}/platform/embedded"
    "${BTSTACK_ROOT}/3rd-party/micro-ecc"
    "${BTSTACK_ROOT}/3rd-party/rijndael"
    "${CMAKE_SOURCE_DIR}"
)

# ── Windows libraries ─────────────────────────────────────────────────────────
target_link_libraries(btstack_sink PRIVATE
    setupapi
    winusb
    ws2_32
)

# ── Compile definitions ───────────────────────────────────────────────────────
target_compile_definitions(btstack_sink PRIVATE
    ENABLE_CLASSIC
    ENABLE_BLE=0
    # Silence MSVC/GCC warnings about unused params in BTstack headers
    UNUSED=
)

# ── Release optimisation ──────────────────────────────────────────────────────
if(CMAKE_BUILD_TYPE STREQUAL "Release")
    target_compile_options(btstack_sink PRIVATE -O2)
endif()

# ── Output to project root/btstack/build/ ────────────────────────────────────
set_target_properties(btstack_sink PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/build"
)

message(STATUS "BTstack root : ${BTSTACK_ROOT}")
message(STATUS "Output dir   : ${CMAKE_SOURCE_DIR}/build")
