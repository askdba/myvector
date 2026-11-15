#!/usr/bin/env bash
# Build an AddressSanitizer MySQL 26.10 server with component_mcp and component_mcp_guard, for
# the fuzzing and soak run of RFC 0001 §10 (scripts/fuzz-mcp-endpoint.sh,
# .github/workflows/mcp-fuzz-soak.yml).
#
# The whole server is built with -DWITH_ASAN=ON: ASAN must be in the executable for the
# components' reports to be complete. component_mcp is built in the server tree (it links
# Router's HTTP libraries); the guard is compiled against the same tree with -fsanitize=address.
#
# Output: OUT_DIR/mysql-asan.tar.gz, a self-contained basedir (bin/, lib/plugin/, share/, and
# lib/ with every shared library the binaries need, libasan included), runnable in a plain
# oraclelinux:9 container with LD_LIBRARY_PATH=<basedir>/lib.
#
# Usage: scripts/build-mcp-asan-26.10-docker.sh [mysql-tag] [out-dir]
#   e.g. scripts/build-mcp-asan-26.10-docker.sh mysql-26.10.0 dist/mcp-asan
# Requires Docker. The MySQL source and build trees are kept in mysql-asan/ for reuse.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MYSQL_TAG="${1:-mysql-26.10.0}"
OUT_DIR="${2:-dist/mcp-asan}"
JOBS="${JOBS:-$(nproc)}"

docker run --rm \
  -v "$REPO_ROOT:/workspace:rw" -w /workspace \
  -e MYSQL_TAG="$MYSQL_TAG" -e OUT_DIR="$OUT_DIR" -e JOBS="$JOBS" \
  -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
  oraclelinux:9 bash -c '
    set -euo pipefail
    echo "==> Installing build dependencies"
    dnf install -y oraclelinux-developer-release-el9 dnf-plugins-core >/dev/null 2>&1
    dnf config-manager --enable ol9_codeready_builder >/dev/null 2>&1
    dnf install -y --nodocs \
      gcc gcc-c++ cmake make git bison pkg-config rpcgen \
      libtirpc-devel openldap-devel cyrus-sasl-devel \
      libcurl-devel protobuf-devel protobuf-compiler \
      zlib-devel openssl-devel ncurses-devel \
      gcc-toolset-14-gcc gcc-toolset-14-gcc-c++ gcc-toolset-14-binutils \
      gcc-toolset-14-libasan-devel \
      gcc-toolset-14-annobin-annocheck gcc-toolset-14-annobin-plugin-gcc >/dev/null
    TS=/opt/rh/gcc-toolset-14/root/usr/bin

    W=/workspace/mysql-asan
    SRC="$W/src-$MYSQL_TAG"
    BLD="$W/bld-$(uname -m)"
    git config --global --add safe.directory "$SRC"
    if [ ! -f "$SRC/include/mysql/components/component_implementation.h" ]; then
      echo "==> Cloning $MYSQL_TAG"
      rm -rf "$SRC"
      git clone -q --depth 1 --branch "$MYSQL_TAG" https://github.com/mysql/mysql-server.git "$SRC"
    fi
    # component_mcp is built in the tree, as for the release build (components are globbed).
    rm -rf "$SRC/components/mcp"
    cp -r /workspace/src/mcp/endpoint "$SRC/components/mcp"

    echo "==> Configuring with ASAN"
    mkdir -p "$BLD"
    cmake -S "$SRC" -B "$BLD" \
      -DCMAKE_C_COMPILER=$TS/gcc -DCMAKE_CXX_COMPILER=$TS/g++ \
      -DCMAKE_BUILD_TYPE=RelWithDebInfo -DWITH_ASAN=ON \
      -DDOWNLOAD_BOOST=1 -DWITH_BOOST=/workspace/mysql-asan/boost \
      -DWITH_UNIT_TESTS=OFF -DWITH_ROUTER=OFF -DWITH_RAPID=OFF -DWITH_NDB=OFF \
      -DWITH_NDBCLUSTER=OFF -DWITH_GROUP_REPLICATION=OFF -DWITH_AUTHENTICATION_WEBAUTHN=OFF \
      -DWITH_EXAMPLE_STORAGE_ENGINE=OFF >/dev/null

    # component_reference_cache: the server loads it from plugin_dir at startup, and without it
    # event tracking reaches no component, so component_mcp_guard would see no statements.
    echo "==> Building mysqld, mysql, component_reference_cache and component_mcp ($JOBS jobs)"
    cmake --build "$BLD" --target mysqld mysql component_reference_cache component_mcp -j"$JOBS"

    echo "==> Building component_mcp_guard with ASAN"
    $TS/g++ -std=c++20 -O1 -g -fno-omit-frame-pointer -fsanitize=address -fPIC -shared -pthread \
      -fvisibility=hidden -fvisibility-inlines-hidden \
      -I"$SRC/include" -I"$SRC" -I"$SRC/libs" -I"$BLD/include" \
      -o "$BLD/plugin_output_directory/component_mcp_guard.so" \
      /workspace/src/mcp/guard/component_mcp_guard.cc

    echo "==> Packaging"
    B=/tmp/mysql-asan
    rm -rf "$B"; mkdir -p "$B/bin" "$B/lib/plugin" "$B/share"
    cp "$BLD/runtime_output_directory/mysqld" "$BLD/runtime_output_directory/mysql" "$B/bin/"
    cp "$BLD/plugin_output_directory/component_mcp.so" \
       "$BLD/plugin_output_directory/component_mcp_guard.so" \
       "$BLD/plugin_output_directory/component_reference_cache.so" "$B/lib/plugin/"
    cp -r "$BLD/share/." "$B/share/"
    cp -r "$SRC/share/charsets" "$B/share/"
    # ICU data for REGEXP: mysqld looks for <basedir>/lib/private/icudt*.
    mkdir -p "$B/lib/private"
    cp -r "$SRC"/extra/icu/icudt* "$B/lib/private/"
    # Every shared library the binaries resolve, except the C library itself.
    for f in "$B/bin/"* "$B/lib/plugin/"*.so; do
      LD_LIBRARY_PATH="$BLD/library_output_directory" ldd "$f" | awk "/=> \\//{print \$3}"
    done | sort -u | while read -r lib; do
      case "$(basename "$lib")" in
        libc.so*|libm.so*|libpthread.so*|libdl.so*|librt.so*|libresolv.so*|ld-linux*) ;;
        *) cp -L "$lib" "$B/lib/" ;;
      esac
    done
    mkdir -p "/workspace/$OUT_DIR"
    tar -C /tmp -czf "/workspace/$OUT_DIR/mysql-asan.tar.gz" mysql-asan
    ls -la "/workspace/$OUT_DIR"
    chown -R "$HOST_UID:$HOST_GID" /workspace/mysql-asan "/workspace/$OUT_DIR"
  '
