#!/usr/bin/env bash
# Build MyVector component for MySQL 8.4 LTS inside an oraclelinux:9 container.
# Installs mysql-community-devel from MySQL CDN (version-pinned, no repo setup).
# Prefers the static archive (libmysqlclient.a) so the resulting .so has no
# libmysqlclient runtime dependency when deployed into the mysql:8.4 Docker image.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MYSQL_TAG="${1:-mysql-8.4.8}"
OUTPUT_DIR="${2:-build/component}"

echo "==> Building MyVector component for $MYSQL_TAG"

HOST_UID="$(id -u)"
HOST_GID="$(id -g)"

docker run --rm \
  ${DOCKER_PLATFORM:+--platform "$DOCKER_PLATFORM"} \
  -v "$REPO_ROOT:/workspace:rw" \
  -w /workspace \
  -e MYSQL_TAG="$MYSQL_TAG" \
  -e HOST_UID="$HOST_UID" \
  -e HOST_GID="$HOST_GID" \
  -e OUTPUT_DIR="$OUTPUT_DIR" \
  oraclelinux:9 \
  bash -c '
    set -e
    ARCH=$(uname -m)

    echo "==> Installing build dependencies..."
    dnf install -y oraclelinux-developer-release-el9 dnf-plugins-core >/dev/null 2>&1
    dnf config-manager --enable ol9_codeready_builder >/dev/null 2>&1

    # Install MySQL 8.4 devel RPMs from CDN — version derived from MYSQL_TAG.
    # Strip "mysql-" prefix (e.g. mysql-8.4.8 -> 8.4.8) and append distro suffix.
    MYSQL_VER="${MYSQL_TAG#mysql-}"
    MYSQL_MINOR="${MYSQL_VER%.*}"   # e.g. 8.4
    VER="${MYSQL_VER}-1.el9"
    # MySQL keeps only the latest point release on the main CDN path and moves
    # older ones to the archive path (e.g. 8.4.8 moved once a newer 8.4.x shipped).
    # Try the main Downloads path, then fall back to the archive path so a pinned
    # point release keeps installing. Note the case: MySQL-X.Y vs mysql-X.Y.
    install_mysql_rpms() {
      local base="$1"
      dnf install -y --nodocs \
        "${base}/mysql-community-common-${VER}.${ARCH}.rpm" \
        "${base}/mysql-community-client-plugins-${VER}.${ARCH}.rpm" \
        "${base}/mysql-community-libs-${VER}.${ARCH}.rpm" \
        "${base}/mysql-community-devel-${VER}.${ARCH}.rpm"
    }
    if ! install_mysql_rpms "https://cdn.mysql.com/Downloads/MySQL-${MYSQL_MINOR}" >/dev/null 2>&1; then
      echo "==> MySQL ${MYSQL_VER} not on main CDN path; using archive"
      install_mysql_rpms "https://cdn.mysql.com/archives/mysql-${MYSQL_MINOR}"
    fi

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
    # Per-architecture MySQL build dir. docker-publish builds amd64 then arm64
    # sequentially in the same mounted repo; a shared bld/ would let the second
    # arch reuse the CMake cache and generated headers of the first (wrong ABI).
    MYSQL_BLD="${MYSQL_SRC}/bld-${ARCH}"

    echo "==> Configuring MySQL (generate headers)..."
    mkdir -p "$MYSQL_BLD"
    if [ ! -f "$MYSQL_BLD/CMakeCache.txt" ]; then
      cd "$MYSQL_BLD"
      cmake .. \
        -DCMAKE_C_COMPILER=/opt/rh/gcc-toolset-14/root/usr/bin/gcc \
        -DCMAKE_CXX_COMPILER=/opt/rh/gcc-toolset-14/root/usr/bin/g++ \
        -DDOWNLOAD_BOOST=1 \
        -DWITH_BOOST=/tmp/boost_mysql84 \
        -DWITH_UNIT_TESTS=OFF \
        -DWITH_ROUTER=OFF \
        -DWITH_RAPID=OFF \
        -DWITH_NDB=OFF \
        -DWITH_NDBCLUSTER=OFF \
        -DWITH_GROUP_REPLICATION=OFF \
        -DWITH_EXAMPLE_STORAGE_ENGINE=OFF \
        -DCMAKE_BUILD_TYPE=Release
    else
      echo "==> Reusing existing MySQL bld/ (CMakeCache.txt present)"
    fi

    echo "==> Building MyVector component..."
    cd /workspace
    # Arch-specific component build dir. OUTPUT_DIR is build/component-${arch};
    # a shared "build" dir would be wiped by the next architecture in the
    # docker-publish multi-arch loop, deleting the prior arch output before it is
    # copied out. Keep the "build" prefix so .dockerignore (build*/) excludes it.
    COMPONENT_BUILD="build-comp-${ARCH}"
    rm -rf "$COMPONENT_BUILD"
    mkdir -p "$COMPONENT_BUILD"

    # Prefer the static archive so the component .so has no libmysqlclient.so
    # runtime dependency (the mysql:8.4 Docker test image has no shared client lib).
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

    cmake -B "$COMPONENT_BUILD" -S . \
      -DCMAKE_C_COMPILER=/opt/rh/gcc-toolset-14/root/usr/bin/gcc \
      -DCMAKE_CXX_COMPILER=/opt/rh/gcc-toolset-14/root/usr/bin/g++ \
      -DCMAKE_BUILD_TYPE=Release \
      -DMYSQL_SOURCE_DIR="$MYSQL_SRC" \
      -DMYSQL_BUILD_DIR="$MYSQL_BLD" \
      -DMYSQL_DIR="$MYSQL_LIBDIR" \
      -DMYSQLCLIENT_LIBRARY="$MYSQLCLIENT_LIB"
    make -C "$COMPONENT_BUILD" -j$(nproc) VERBOSE=1

    echo "==> Packaging artifact..."
    mkdir -p "/workspace/$OUTPUT_DIR"
    cp "$COMPONENT_BUILD/libmyvector_component.so" "/workspace/$OUTPUT_DIR/"
    cp src/component_src/myvector.json "/workspace/$OUTPUT_DIR/"
    echo "==> Built: /workspace/$OUTPUT_DIR/libmyvector_component.so"

    # Restore host ownership so runner can use the result and cache can save the source.
    chown -R "${HOST_UID}:${HOST_GID}" "/workspace/mysql-server-${MYSQL_TAG}" 2>/dev/null || true
    chown -R "${HOST_UID}:${HOST_GID}" "/workspace/$COMPONENT_BUILD" 2>/dev/null || true
    chown -R "${HOST_UID}:${HOST_GID}" "/workspace/${OUTPUT_DIR}" 2>/dev/null || true
  '
