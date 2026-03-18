#!/usr/bin/env bash
# Build MyVector Docker image locally (for testing).
# On Apple Silicon (arm64): builds arm64 plugin and image for native run.
# On x86_64: builds amd64 plugin and image.
#
# Usage: ./scripts/build-docker-local.sh [mysql-version]
# Example: ./scripts/build-docker-local.sh 8.4
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

MYSQL_VERSION="${1:-8.4}"
MYSQL_TAG="mysql-8.4.8"
[ "$MYSQL_VERSION" = "8.0" ] && MYSQL_TAG="mysql-8.0.45"
[ "$MYSQL_VERSION" = "9.0" ] && MYSQL_TAG="mysql-9.0.0"

# Build for native arch on Apple Silicon; amd64 on x86
# Set MYVECTOR_ARCH=amd64 to force amd64 (QEMU) on arm64 - slower but avoids compiler bugs
ARCH=$(uname -m)
HOST_ARM64=0
[ "$ARCH" = "arm64" ] && HOST_ARM64=1
# Detect Apple Silicon even under Rosetta (uname can report x86_64 there)
[ "$HOST_ARM64" -eq 0 ] && [ "$(uname -s)" = "Darwin" ] && \
  [ "$(sysctl -in hw.optional.arm64 2>/dev/null || echo 0)" = "1" ] && HOST_ARM64=1
TARGET_ARCH="${MYVECTOR_ARCH:-}"
[ -z "$TARGET_ARCH" ] && { [ "$HOST_ARM64" -eq 1 ] && TARGET_ARCH="arm64" || TARGET_ARCH="amd64"; }
PLATFORM="linux/${TARGET_ARCH}"
# arm64: use -j1 for clearer errors; amd64 can parallelize
MAKE_JOBS=4
[ "$TARGET_ARCH" = "arm64" ] && MAKE_JOBS=1
[ -n "${MYVECTOR_BUILD_JOBS:-}" ] && MAKE_JOBS="$MYVECTOR_BUILD_JOBS"

echo "=== Building MyVector plugin for Docker (mysql $MYSQL_VERSION, $TARGET_ARCH) ==="
echo "Using make -j$MAKE_JOBS (set MYVECTOR_BUILD_JOBS to override)"
[ "$HOST_ARM64" -eq 1 ] && echo "Apple Silicon host detected: using Clang (GCC ICEs under Docker/QEMU)"
[ "$TARGET_ARCH" = "amd64" ] && [ "$HOST_ARM64" -eq 1 ] && echo "Tip: amd64 under QEMU is slow (~30+ min)"
echo ""

# Apple Silicon host: GCC ICEs for both arm64 and amd64 (QEMU). Use Clang for both.
# x86_64 host: use gcc-10. Allow manual override via MYVECTOR_CC/MYVECTOR_CXX.
CC="${MYVECTOR_CC:-gcc-10}"
CXX="${MYVECTOR_CXX:-g++-10}"
CMAKE_EXTRA=""
[ "$HOST_ARM64" -eq 1 ] && [ -z "${MYVECTOR_CC:-}" ] && [ -z "${MYVECTOR_CXX:-}" ] && \
  CC=clang CXX=clang++ CMAKE_EXTRA=""
echo "Compiler: CC=$CC CXX=$CXX"

# Build plugin inside Docker for target arch.
# MAKEFLAGS propagates to submakes (avoids gcc ICE on arm64).
# Clone to /tmp (container-local) to avoid bind-mount issues on macOS (git pack failures).
# --privileged: Docker on macOS may restrict timer syscalls; needed for MySQL CMake timer detection
docker run --rm --privileged --platform "$PLATFORM" \
  -e MAKEFLAGS="-j${MAKE_JOBS}" \
  -e MYVECTOR_VERBOSE="${MYVECTOR_VERBOSE:-}" \
  -v "$REPO_ROOT:/work" -w /work \
  ubuntu:22.04 bash -c "
    set -e
    apt-get update -qq
    apt-get install -y -qq cmake clang g++-10 gcc-10 git libssl-dev libncurses5-dev \
      pkg-config bison libtirpc-dev libldap2-dev libsasl2-dev libudev-dev \
      libre2-dev libcurl4-openssl-dev libprotobuf-dev protobuf-compiler linux-libc-dev
    rm -rf /tmp/mysql-server
    git clone --depth 1 --branch $MYSQL_TAG https://github.com/mysql/mysql-server.git /tmp/mysql-server
    mkdir -p /tmp/mysql-server/plugin/myvector
    cp src/*.cc /tmp/mysql-server/plugin/myvector/
    cp include/*.h /tmp/mysql-server/plugin/myvector/
    cp include/*.i /tmp/mysql-server/plugin/myvector/ 2>/dev/null || true
    cp CMakeLists.txt /tmp/mysql-server/plugin/myvector/
    mkdir -p /tmp/mysql-server/bld && cd /tmp/mysql-server/bld
    cmake .. -DCMAKE_C_COMPILER=$CC -DCMAKE_CXX_COMPILER=$CXX \
      $CMAKE_EXTRA \
      -DDOWNLOAD_BOOST=1 -DWITH_UNIT_TESTS=OFF -DWITH_ROUTER=OFF \
      -DWITH_RAPID=OFF -DWITH_NDB=OFF -DWITH_NDBCLUSTER=OFF \
      -DWITH_EXAMPLE_STORAGE_ENGINE=OFF -DCMAKE_BUILD_TYPE=Release
    export MAKEFLAGS=-j${MAKE_JOBS}
    make myvector ${MYVECTOR_VERBOSE:+VERBOSE=1}
    cp plugin_output_directory/myvector.so /work/myvector-${TARGET_ARCH}.so
"
cp sql/myvectorplugin.sql .

echo "Building Docker image ($PLATFORM)..."
docker build -f Dockerfile.oraclelinux9 \
  --platform "$PLATFORM" \
  --build-arg MYSQL_VERSION="$MYSQL_VERSION" \
  -t "myvector:mysql${MYSQL_VERSION}-local" .

echo ""
echo "Done. Run: ./scripts/test-online-updates.sh myvector:mysql${MYSQL_VERSION}-local"
