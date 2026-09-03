#!/usr/bin/env bash
# Rebuilds btstack_sink.exe from an MSYS2/Git Bash shell.
#
# This is the quick path for people who already have MSYS2's MinGW-w64
# toolchain installed; build.ps1 does the same plus toolchain installation.
# It clones BTstack if missing, applies the patches, and builds
# incrementally (the build directory is NOT wiped, so an existing
# btstack_keys.db next to the exe survives).
set -euo pipefail

MSYS2_ROOT="${MSYS2_ROOT:-/c/msys64}"
MINGW_BIN="$MSYS2_ROOT/mingw64/bin"
export PATH="$MINGW_BIN:/mingw64/bin:/usr/bin:$PATH"
export TEMP="/tmp"
export TMP="/tmp"

BTSTACK_REPO="https://github.com/bluekitchen/btstack.git"
BTSTACK_COMMIT="v1.6.1"   # keep in sync with build.ps1

BDIR="$(cd "$(dirname "$0")" && pwd)"
SRC="$BDIR/btstack-src"
BUILD="$BDIR/build"

for tool in gcc.exe cmake.exe mingw32-make.exe; do
    if [ ! -x "$MINGW_BIN/$tool" ]; then
        echo "$tool not found in $MINGW_BIN. Run build.ps1 once, or:" >&2
        echo "  pacman -S --needed mingw-w64-x86_64-gcc mingw-w64-x86_64-cmake mingw-w64-x86_64-make git" >&2
        exit 1
    fi
done

if [ ! -d "$SRC" ]; then
    echo "==> cloning BTstack $BTSTACK_COMMIT"
    git clone --depth 1 --branch "$BTSTACK_COMMIT" "$BTSTACK_REPO" "$SRC"
fi

echo "==> applying patches"
python "$BDIR/patches/apply_patches.py" "$SRC"

mkdir -p "$BUILD"

echo "==> cmake configure"
cmake -S "$BDIR" -B "$BUILD" \
  -G "MinGW Makefiles" \
  -DCMAKE_MAKE_PROGRAM="$MINGW_BIN/mingw32-make.exe" \
  -DCMAKE_C_COMPILER="$MINGW_BIN/gcc.exe" \
  -DCMAKE_BUILD_TYPE=Release \
  -DBTSTACK_ROOT="$SRC"

echo "==> cmake build"
cmake --build "$BUILD" --target btstack_sink -j4

echo "==> Done"
ls -lh "$BUILD/btstack_sink.exe"
