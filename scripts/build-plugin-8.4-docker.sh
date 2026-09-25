#!/usr/bin/env bash
# Build the MyVector *plugin* (classic MySQL plugin, not the component) for
# MySQL 8.4 LTS inside an oraclelinux:9 container. Installs
# mysql-community-devel from MySQL CDN (version-pinned, no repo setup), same
# as build-component-8.4-docker.sh.
#
# Unlike the component build, this configures MySQL's *own* CMake build
# (superbuild) and builds the in-tree "myvector" plugin target via
# MYSQL_ADD_PLUGIN (the `if(COMMAND MYSQL_ADD_PLUGIN)` branch at the top of
# CMakeLists.txt) — the same recipe already proven by the "build" job in
# .github/workflows/ci.yml. The resulting myvector.so is the classic/
# original distribution form (still the default published tags: `INSTALL
# PLUGIN myvector SONAME 'myvector.so'`, e.g. ghcr.io/askdba/myvector:mysql8.4,
# mysql8.0, mysql9.7, and latest) and is the only
# form that supports query rewrite: the inline `col MYVECTOR(...)` DDL
# annotation and `WHERE MYVECTOR_IS_ANN(...)`, via the classic Audit Plugin
# pre-parse hook in src/myvector_plugin.cc.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MYSQL_TAG="${1:-mysql-8.4.8}"
OUTPUT_DIR="${2:-build/plugin}"

echo "==> Building MyVector plugin for $MYSQL_TAG"

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
    # The repo tree is bind-mounted from the host, owned by the host UID, but
    # this container runs as root — git refuses to touch a repo it does not
    # own ("dubious ownership in repository") unless told otherwise. Without
    # this, the "describe"/"rev-parse" reuse-detection below silently fails,
    # CURRENT_TAG stays empty, NEED_CLONE stays true, and the "rm -rf
    # $MYSQL_WORKSPACE" further down destroys the entire shared checkout
    # (including bld-${ARCH}, used by the component build) on every run.
    git config --global --add safe.directory "$MYSQL_WORKSPACE"
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

    echo "==> Copying MyVector plugin sources into plugin/myvector..."
    # Flatten src/ and include/ into a single directory, same as the proven
    # ".github/workflows/ci.yml" "Copy MyVector to Plugin Directory" step:
    # myvector.cc uses quoted #include "hnswdisk.h" etc, resolved by the
    # compiler searching the including file'"'"'s own directory first, so the
    # plugin CMakeLists.txt (in-tree MYSQL_ADD_PLUGIN branch) does not need
    # its own target_include_directories() for a nested include/ subdir.
    PLUGIN_DIR="$MYSQL_SRC/plugin/myvector"
    rm -rf "$PLUGIN_DIR"
    mkdir -p "$PLUGIN_DIR"
    cp /workspace/src/*.cc "$PLUGIN_DIR/"
    cp /workspace/include/*.h "$PLUGIN_DIR/"
    cp /workspace/include/*.i "$PLUGIN_DIR/" 2>/dev/null || true
    cp /workspace/CMakeLists.txt "$PLUGIN_DIR/"
    echo "==> Plugin directory contents:"
    ls -la "$PLUGIN_DIR"

    # Per-architecture MySQL build dir, kept separate from the component
    # build'"'"'s bld-${ARCH} (same MYSQL_SRC checkout is shared between the
    # component and plugin scripts, but the plugin build needs a real MySQL
    # server configure/build, not just header generation, and a different
    # WITH_BOOST path) so the two build modes never stomp on each other'"'"'s
    # CMake cache.
    MYSQL_BLD="${MYSQL_SRC}/bld-plugin-${ARCH}"
    BOOST_CACHE="${MYSQL_SRC}/boost_cache"

    echo "==> Configuring MySQL server build (in-tree plugin mode)..."
    mkdir -p "$MYSQL_BLD"
    cd "$MYSQL_BLD"
    # Always drop a stale cache before configuring: a CMakeCache.txt from a
    # run where plugin/myvector did not yet exist (or existed with different
    # sources) would make cmake skip/stale the target. See the same
    # "rm -f CMakeCache.txt" step and rationale in
    # .github/workflows/ci.yml ("Configure MySQL Build").
    rm -f CMakeCache.txt
    cmake .. \
      -DCMAKE_C_COMPILER=/opt/rh/gcc-toolset-14/root/usr/bin/gcc \
      -DCMAKE_CXX_COMPILER=/opt/rh/gcc-toolset-14/root/usr/bin/g++ \
      -DDOWNLOAD_BOOST=1 \
      -DWITH_BOOST="$BOOST_CACHE" \
      -DWITH_UNIT_TESTS=OFF \
      -DWITH_ROUTER=OFF \
      -DWITH_RAPID=OFF \
      -DWITH_NDB=OFF \
      -DWITH_NDBCLUSTER=OFF \
      -DWITH_GROUP_REPLICATION=OFF \
      -DWITH_EXAMPLE_STORAGE_ENGINE=OFF \
      -DCMAKE_BUILD_TYPE=Release

    echo "==> Building MyVector plugin target (make myvector)..."
    time make myvector -j$(nproc)

    echo "==> Packaging artifact..."
    PLUGIN_SO=$(find "$MYSQL_BLD" -name "myvector.so" | head -1)
    if [ -z "$PLUGIN_SO" ]; then
      echo "ERROR: myvector.so not found after build" >&2
      find "$MYSQL_BLD" -iname "*myvector*" >&2 || true
      exit 1
    fi
    mkdir -p "/workspace/$OUTPUT_DIR"
    cp "$PLUGIN_SO" "/workspace/$OUTPUT_DIR/"
    cp /workspace/sql/myvectorplugin.sql "/workspace/$OUTPUT_DIR/"
    echo "==> Built: /workspace/$OUTPUT_DIR/myvector.so"

    # Restore host ownership so runner can use the result and cache can save the source.
    chown -R "${HOST_UID}:${HOST_GID}" "/workspace/mysql-server-${MYSQL_TAG}" 2>/dev/null || true
    chown -R "${HOST_UID}:${HOST_GID}" "/workspace/${OUTPUT_DIR}" 2>/dev/null || true
  '
