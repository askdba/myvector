#!/usr/bin/env bash
# Build MyVector component for MySQL 9.7 LTS inside an oraclelinux:9 container.
# Installs mysql-community-devel from MySQL CDN (direct RPM, version-pinned, no repo setup).
# Uses gcc-toolset-14 as required by MySQL 9.7 cmake on OracleLinux 9.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MYSQL_TAG="${1:-mysql-9.7.0}"

echo "==> Building MyVector component for $MYSQL_TAG"

docker run --rm \
  -v "$REPO_ROOT:/workspace:rw" \
  -w /workspace \
  -e MYSQL_TAG="$MYSQL_TAG" \
  oraclelinux:9 \
  bash -c '
    set -e
    ARCH=$(uname -m)

    echo "==> Installing build dependencies..."
    dnf install -y oraclelinux-developer-release-el9 dnf-plugins-core >/dev/null 2>&1
    dnf config-manager --enable ol9_codeready_builder >/dev/null 2>&1

    # Install MySQL 9.7 devel RPMs from CDN (version-pinned, no repo setup required)
    BASE="https://cdn.mysql.com/Downloads/MySQL-9.7"
    VER="9.7.0-1.el9"
    dnf install -y --nodocs \
      "${BASE}/mysql-community-common-${VER}.${ARCH}.rpm" \
      "${BASE}/mysql-community-client-plugins-${VER}.${ARCH}.rpm" \
      "${BASE}/mysql-community-libs-${VER}.${ARCH}.rpm" \
      "${BASE}/mysql-community-devel-${VER}.${ARCH}.rpm" \
      >/dev/null 2>&1

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
      -DWITH_BOOST=/tmp/boost_mysql97 \
      -DWITH_UNIT_TESTS=OFF \
      -DWITH_ROUTER=OFF \
      -DWITH_RAPID=OFF \
      -DWITH_NDB=OFF \
      -DWITH_NDBCLUSTER=OFF \
      -DWITH_GROUP_REPLICATION=OFF \
      -DWITH_EXAMPLE_STORAGE_ENGINE=OFF \
      -DCMAKE_BUILD_TYPE=Release

    echo "==> Building MyVector component..."
    cd /workspace
    rm -rf build
    mkdir -p build

    # Prefer the static archive so the component .so has no libmysqlclient.so
    # runtime dependency (the mysql:9.7 Docker test image has no shared client lib).
    MYSQLCLIENT_LIB=$(find /usr/lib64 /usr/lib -name "libmysqlclient.a" 2>/dev/null | head -1)
    if [ -z "$MYSQLCLIENT_LIB" ]; then
      MYSQLCLIENT_LIB=$(find /usr/lib64 /usr/lib -name "libmysqlclient.so" 2>/dev/null | head -1)
    fi
    if [ -z "$MYSQLCLIENT_LIB" ]; then
      echo "ERROR: libmysqlclient not found after devel RPM install" >&2
      find /usr/lib64 /usr/lib -name "libmysql*" 2>/dev/null >&2 || true
      exit 1
    fi
    MYSQL_LIBDIR=$(dirname "$MYSQLCLIENT_LIB")
    echo "==> libmysqlclient at: $MYSQLCLIENT_LIB"

    cmake -B build -S . \
      -DCMAKE_C_COMPILER=/opt/rh/gcc-toolset-14/root/usr/bin/gcc \
      -DCMAKE_CXX_COMPILER=/opt/rh/gcc-toolset-14/root/usr/bin/g++ \
      -DCMAKE_BUILD_TYPE=Release \
      -DMYSQL_SOURCE_DIR="$MYSQL_SRC" \
      -DMYSQL_BUILD_DIR="$MYSQL_SRC/bld" \
      -DMYSQL_DIR="$MYSQL_LIBDIR" \
      -DMYSQLCLIENT_LIBRARY="$MYSQLCLIENT_LIB"
    make -C build -j$(nproc) VERBOSE=1

    echo "==> Packaging artifact..."
    mkdir -p build/component
    cp build/libmyvector_component.so build/component/
    cp src/component_src/myvector.json build/component/
    echo "==> Built: build/component/libmyvector_component.so"
  '
