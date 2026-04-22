#!/usr/bin/env bash
# Build MyVector component for MySQL 9.6.
# Uses oraclelinux:9 for build tools; copies libmysqlclient from mysql:9.6
# to guarantee ABI compatibility with mysql:9.6 runtime.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MYSQL_TAG="${1:-mysql-9.6.0}"
MYSQL_LIBS_TMP="/tmp/mysql-libs-${MYSQL_TAG}"
# Unique container name per run to avoid concurrency conflicts
CONTAINER_NAME="myv96libs-$$-$(date +%s)"

echo "==> Building MyVector component for $MYSQL_TAG"

# Cleanup: container and temp dir. Install trap immediately so early failures are cleaned up.
trap 'docker rm -f "$CONTAINER_NAME" 2>/dev/null || true; rm -rf "$MYSQL_LIBS_TMP"' EXIT

# Map MYSQL_TAG (e.g. mysql-9.6.0) to Docker image (e.g. mysql:9.6.0)
MYSQL_IMAGE_TAG="${MYSQL_TAG#mysql-}"
MYSQL_IMAGE="mysql:${MYSQL_IMAGE_TAG:-9.6}"

# Extract MySQL libs from the official image for ABI compatibility
echo "==> Extracting libmysqlclient from $MYSQL_IMAGE..."
docker create --name "$CONTAINER_NAME" "$MYSQL_IMAGE"
rm -rf "$MYSQL_LIBS_TMP"
mkdir -p "$MYSQL_LIBS_TMP/lib64"
# Oracle Linux MySQL image: libs in /usr/lib64/mysql/ or /usr/lib/mysql/
docker cp "$CONTAINER_NAME":/usr/lib64/mysql/. "$MYSQL_LIBS_TMP/lib64/" 2>/dev/null || \
  docker cp "$CONTAINER_NAME":/usr/lib/mysql/. "$MYSQL_LIBS_TMP/lib64/" 2>/dev/null || \
  { echo "Could not find libmysqlclient in $MYSQL_IMAGE"; exit 1; }
docker rm -f "$CONTAINER_NAME"

echo "==> Extracted libs from $MYSQL_IMAGE:"
ls -la "$MYSQL_LIBS_TMP/lib64/" 2>/dev/null || echo "(lib64 empty or missing)"
ls -la "$MYSQL_LIBS_TMP/" 2>/dev/null

# cmake find_library needs the unversioned .so symlink (only in mysql-devel, not the server image).
# Create it from the versioned lib if missing.
(cd "$MYSQL_LIBS_TMP/lib64" && for f in libmysqlclient.so.[0-9]*; do
  [ -f "$f" ] || continue
  echo "==> Creating symlink: libmysqlclient.so -> $f"
  [ -L libmysqlclient.so ] || ln -sf "$f" libmysqlclient.so
done)

docker run --rm \
  -v "$REPO_ROOT:/workspace:rw" \
  -v "$MYSQL_LIBS_TMP:/mysql-libs:ro" \
  -w /workspace \
  -e MYSQL_TAG="$MYSQL_TAG" \
  oraclelinux:9 \
  bash -c '
    set -e
    echo "==> Installing build dependencies..."
    # Enable CodeReady Builder repo for cyrus-sasl-devel and protobuf packages
    dnf install -y oraclelinux-developer-release-el9 dnf-plugins-core >/dev/null 2>&1
    dnf config-manager --enable ol9_codeready_builder >/dev/null 2>&1
    dnf install -y --nodocs \
      gcc gcc-c++ cmake make git \
      bison pkg-config \
      libtirpc-devel openldap-devel cyrus-sasl-devel \
      libcurl-devel protobuf-devel protobuf-compiler \
      zlib-devel openssl-devel ncurses-devel \
      gcc-toolset-14-gcc gcc-toolset-14-gcc-c++ \
      gcc-toolset-14-binutils \
      gcc-toolset-14-annobin-annocheck gcc-toolset-14-annobin-plugin-gcc \
      rpcgen \
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

    echo "==> Building MyVector component (MYSQL_DIR=/mysql-libs for extracted libs)..."
    cd /workspace
    rm -rf build
    mkdir -p build
    cmake -B build -S . \
      -DCMAKE_C_COMPILER=/opt/rh/gcc-toolset-14/root/usr/bin/gcc \
      -DCMAKE_CXX_COMPILER=/opt/rh/gcc-toolset-14/root/usr/bin/g++ \
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
