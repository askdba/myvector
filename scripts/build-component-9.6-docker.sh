#!/usr/bin/env bash
# Build MyVector component for MySQL 9.6.
# Uses oraclelinux:9 for build tools; copies libmysqlclient from mysql:9.6
# to guarantee ABI compatibility with mysql:9.6 runtime.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MYSQL_TAG="${1:-mysql-9.6.0}"

echo "==> Building MyVector component for $MYSQL_TAG"

# Extract MySQL 9.6 libs from the official image for ABI compatibility
echo "==> Extracting libmysqlclient from mysql:9.6..."
docker create --name myv96libs mysql:9.6
trap 'docker rm -f myv96libs 2>/dev/null || true' EXIT
rm -rf /tmp/mysql-libs-9.6
mkdir -p /tmp/mysql-libs-9.6/lib64
# Oracle Linux MySQL image: libs in /usr/lib64/mysql/ or /usr/lib/mysql/
docker cp myv96libs:/usr/lib64/mysql/. /tmp/mysql-libs-9.6/lib64/ 2>/dev/null || \
  docker cp myv96libs:/usr/lib/mysql/. /tmp/mysql-libs-9.6/lib64/ 2>/dev/null || \
  { echo "Could not find libmysqlclient in mysql:9.6"; exit 1; }
docker rm -f myv96libs
trap - EXIT

docker run --rm \
  -v "$REPO_ROOT:/workspace:rw" \
  -v "/tmp/mysql-libs-9.6:/mysql-libs:ro" \
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

rm -rf /tmp/mysql-libs-9.6
