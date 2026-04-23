#!/usr/bin/env bash
# Build MyVector component for MySQL 9.6 inside an oraclelinux:9 container.
# Clones MySQL source, builds libmysqlclient from source (no external repo),
# then builds the component using gcc-toolset-14 as required by MySQL 9.6.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MYSQL_TAG="${1:-mysql-9.6.0}"

echo "==> Building MyVector component for $MYSQL_TAG"

docker run --rm \
  -v "$REPO_ROOT:/workspace:rw" \
  -w /workspace \
  -e MYSQL_TAG="$MYSQL_TAG" \
  oraclelinux:9 \
  bash -c '
    set -e

    echo "==> Installing build dependencies..."
    dnf install -y oraclelinux-developer-release-el9 dnf-plugins-core >/dev/null 2>&1
    dnf config-manager --enable ol9_codeready_builder >/dev/null 2>&1

    dnf install -y --nodocs \
      gcc gcc-c++ cmake make git bison pkg-config rpcgen \
      libtirpc-devel openldap-devel cyrus-sasl-devel \
      libcurl-devel protobuf-devel protobuf-compiler \
      zlib-devel openssl-devel ncurses-devel \
      gcc-toolset-14-gcc gcc-toolset-14-gcc-c++ \
      gcc-toolset-14-binutils \
      gcc-toolset-14-annobin-annocheck gcc-toolset-14-annobin-plugin-gcc \
      >/dev/null 2>&1

    echo "==> Cloning MySQL source ($MYSQL_TAG)..."
    MYSQL_WORKSPACE="/workspace/mysql-server-${MYSQL_TAG}"
    NEED_CLONE=true
    if [ -d "$MYSQL_WORKSPACE/.git" ]; then
      CURRENT_TAG="$(git -C "$MYSQL_WORKSPACE" describe --tags --exact-match 2>/dev/null \
        || git -C "$MYSQL_WORKSPACE" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
      if [ "$CURRENT_TAG" = "$MYSQL_TAG" ] && \
         [ -f "$MYSQL_WORKSPACE/include/mysql/components/component_implementation.h" ]; then
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
      -DCMAKE_C_COMPILER=/opt/rh/gcc-toolset-14/root/usr/bin/gcc \
      -DCMAKE_CXX_COMPILER=/opt/rh/gcc-toolset-14/root/usr/bin/g++ \
      -DDOWNLOAD_BOOST=1 \
      -DWITH_BOOST=/tmp/boost_mysql96 \
      -DWITH_UNIT_TESTS=OFF \
      -DWITH_ROUTER=OFF \
      -DWITH_RAPID=OFF \
      -DWITH_NDB=OFF \
      -DWITH_NDBCLUSTER=OFF \
      -DWITH_GROUP_REPLICATION=OFF \
      -DWITH_EXAMPLE_STORAGE_ENGINE=OFF \
      -DCMAKE_BUILD_TYPE=Release

    echo "==> Building libmysqlclient from source..."
    make -C "$MYSQL_SRC/bld" mysqlclient -j$(nproc)
    LIBSO=$(find "$MYSQL_SRC/bld" -name "libmysqlclient.so*" -type f 2>/dev/null | head -1)
    LIBSO_DIR=$(dirname "$LIBSO")
    [ -f "$LIBSO_DIR/libmysqlclient.so" ] || ln -sf "$(basename "$LIBSO")" "$LIBSO_DIR/libmysqlclient.so"
    echo "==> libmysqlclient at: $LIBSO_DIR"

    echo "==> Building MyVector component..."
    cd /workspace
    rm -rf build
    mkdir -p build
    cmake -B build -S . \
      -DCMAKE_C_COMPILER=/opt/rh/gcc-toolset-14/root/usr/bin/gcc \
      -DCMAKE_CXX_COMPILER=/opt/rh/gcc-toolset-14/root/usr/bin/g++ \
      -DCMAKE_BUILD_TYPE=Release \
      -DMYSQL_SOURCE_DIR="$MYSQL_SRC" \
      -DMYSQL_BUILD_DIR="$MYSQL_SRC/bld" \
      -DMYSQL_DIR="$LIBSO_DIR"
    make -C build -j$(nproc) VERBOSE=1

    echo "==> Packaging artifact..."
    mkdir -p build/component
    cp build/libmyvector_component.so build/component/
    cp src/component_src/myvector.json build/component/
    echo "==> Built: build/component/libmyvector_component.so"
  '
