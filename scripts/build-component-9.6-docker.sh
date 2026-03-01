#!/usr/bin/env bash
# Build MyVector component for MySQL 9.6 inside mysql:9.6 container.
# Links against the container's libmysqlclient for ABI compatibility with mysql:9.6 runtime.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MYSQL_TAG="${1:-mysql-9.6.0}"

echo "==> Building MyVector component for $MYSQL_TAG inside mysql:9.6 container"

docker run --rm \
  -v "$REPO_ROOT:/workspace:rw" \
  -w /workspace \
  -e MYSQL_TAG="$MYSQL_TAG" \
  mysql:9.6 \
  bash -c '
    set -e
    echo "==> Installing build dependencies (Oracle Linux)..."
    dnf install -y --nodocs \
      gcc gcc-c++ cmake make git \
      bison pkg-config \
      libtirpc-devel libldap-devel cyrus-sasl-devel \
      libcurl-devel protobuf-devel protobuf-compiler \
      zlib-devel openssl-devel ncurses-devel \
      >/dev/null 2>&1

    echo "==> Cloning MySQL source ($MYSQL_TAG)..."
    if [ ! -f /workspace/mysql-server-9.6/include/mysql/components/component_implementation.h ]; then
      rm -rf /workspace/mysql-server-9.6
      git clone --depth 1 --branch "$MYSQL_TAG" \
        https://github.com/mysql/mysql-server.git /workspace/mysql-server-9.6
    fi
    MYSQL_SRC=/workspace/mysql-server-9.6

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

    echo "==> Building MyVector component (MYSQL_DIR=/usr for container libs)..."
    cd /workspace
    rm -rf build
    mkdir -p build
    cmake -B build -S . \
      -DCMAKE_BUILD_TYPE=Release \
      -DMYSQL_SOURCE_DIR="$MYSQL_SRC" \
      -DMYSQL_BUILD_DIR="$MYSQL_SRC/bld" \
      -DMYSQL_DIR=/usr
    make -C build -j$(nproc) VERBOSE=1

    echo "==> Packaging artifact..."
    mkdir -p build/component
    cp build/libmyvector_component.so build/component/
    cp src/component_src/myvector.json build/component/
    echo "==> Built: build/component/libmyvector_component.so"
  '
