#!/usr/bin/env bash
# Build MyVector component for MySQL 9.6.
# Uses oraclelinux:9 for build tools; copies libmysqlclient from mysql:9.6
# to guarantee ABI compatibility with mysql:9.6 runtime.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MYSQL_TAG="${1:-mysql-9.6.0}"
MYSQL_LIBS_TMP="/tmp/mysql-libs-${MYSQL_TAG}"

echo "==> Building MyVector component for $MYSQL_TAG"

# Cleanup: container and temp dir. Install trap immediately so early failures are cleaned up.
trap 'docker rm -f myv96libs 2>/dev/null || true; rm -rf "$MYSQL_LIBS_TMP"' EXIT

# Extract MySQL 9.6 libs from the official image for ABI compatibility
echo "==> Extracting libmysqlclient from mysql:9.6..."
docker create --name myv96libs mysql:9.6
rm -rf "$MYSQL_LIBS_TMP"
mkdir -p "$MYSQL_LIBS_TMP/lib64"
# Oracle Linux MySQL image: libs in /usr/lib64/mysql/ or /usr/lib/mysql/
docker cp myv96libs:/usr/lib64/mysql/. "$MYSQL_LIBS_TMP/lib64/" 2>/dev/null || \
  docker cp myv96libs:/usr/lib/mysql/. "$MYSQL_LIBS_TMP/lib64/" 2>/dev/null || \
  { echo "Could not find libmysqlclient in mysql:9.6"; exit 1; }
docker rm -f myv96libs

docker run --rm \
  -v "$REPO_ROOT:/workspace:rw" \
  -v "$MYSQL_LIBS_TMP:/mysql-libs:ro" \
  -w /workspace \
  -e MYSQL_TAG="$MYSQL_TAG" \
  oraclelinux:9 \
  bash -c '
    set -e
    echo "==> Installing build dependencies..."
    dnf install -y --nodocs \
      gcc gcc-c++ cmake make git \
      bison pkg-config \
      libtirpc-devel libldap-devel cyrus-sasl-devel \
      libcurl-devel protobuf-devel protobuf-compiler \
      zlib-devel openssl-devel ncurses-devel \
      >/dev/null 2>&1

    echo "==> Cloning MySQL source ($MYSQL_TAG)..."
    MYSQL_WORKSPACE="/workspace/mysql-server-${MYSQL_TAG}"
    NEED_CLONE=true
    if [ -d "$MYSQL_WORKSPACE/.git" ]; then
      CURRENT_TAG="$(git -C "$MYSQL_WORKSPACE" describe --tags --exact-match 2>/dev/null || git -C "$MYSQL_WORKSPACE" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
      if [ "$CURRENT_TAG" = "$MYSQL_TAG" ] && [ -f "$MYSQL_WORKSPACE/include/mysql/components/component_implementation.h" ]; then
        NEED_CLONE=false
      fi
    fi
    if [ "$NEED_CLONE" = true ]; then
      rm -rf "$MYSQL_WORKSPACE"
      git clone --depth 1 --branch "$MYSQL_TAG" \
        https://github.com/mysql/mysql-server.git "$MYSQL_WORKSPACE"
    fi
    MYSQL_SRC="$MYSQL_WORKSPACE"

    echo "==> Configuring MySQL (generate headers)..."
    mkdir -p "$MYSQL_SRC/bld" && cd "$MYSQL_SRC/bld"
    cmake .. \
      -DDOWNLOAD_BOOST=1 \
      -DWITH_UNIT_TESTS=OFF \
      -DWITH_ROUTER=OFF \
      -DWITH_RAPID=OFF \
      -DWITH_NDB=OFF \
      -DWITH_NDBCLUSTER=OFF \
      -DWITH_EXAMPLE_STORAGE_ENGINE=OFF \
      -DCMAKE_BUILD_TYPE=Release \
      >/dev/null 2>&1

    echo "==> Building MyVector component (MYSQL_DIR=/mysql-libs for extracted libs)..."
    cd /workspace
    rm -rf build
    mkdir -p build
    cmake -B build -S . \
      -DCMAKE_BUILD_TYPE=Release \
      -DMYSQL_SOURCE_DIR="$MYSQL_SRC" \
      -DMYSQL_BUILD_DIR="$MYSQL_SRC/bld" \
      -DMYSQL_DIR=/mysql-libs
    make -C build -j$(nproc) VERBOSE=1

    echo "==> Packaging artifact..."
    mkdir -p build/component
    cp build/libmyvector_component.so build/component/
    cp src/component_src/myvector.json build/component/
    echo "==> Built: build/component/libmyvector_component.so"
  '
