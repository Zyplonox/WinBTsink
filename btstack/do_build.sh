#!/usr/bin/env bash
set -e
export PATH="/c/msys64/mingw64/bin:/mingw64/bin:/usr/bin:$PATH"
export TEMP="/tmp"
export TMP="/tmp"
export CC="/c/msys64/mingw64/bin/gcc.exe"
export CXX="/c/msys64/mingw64/bin/g++.exe"

BDIR="$(cd "$(dirname "$0")" && pwd)"
SRC="$BDIR/btstack-src"
BUILD="$BDIR/build"

echo "PATH=$PATH"
echo "CC=$CC"
echo "BDIR=$BDIR"

rm -rf "$BUILD"
mkdir -p "$BUILD"

echo "==> cmake configure"
cmake -S "$BDIR" -B "$BUILD" \
  -G "MinGW Makefiles" \
  -DCMAKE_MAKE_PROGRAM="/c/msys64/mingw64/bin/mingw32-make.exe" \
  -DCMAKE_C_COMPILER="/c/msys64/mingw64/bin/gcc.exe" \
  -DCMAKE_BUILD_TYPE=Release \
  -DBTSTACK_ROOT="$SRC"

echo "==> cmake build"
cmake --build "$BUILD" --target btstack_sink -j4

echo "==> Done"
ls -lh "$BUILD/btstack_sink.exe"
