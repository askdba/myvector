#!/usr/bin/env bash
# Build the MyVector component (out-of-tree). Uses MySQL server source for
# component headers and system or MYSQL_DIR for libmysqlclient.
# See docs/COMPONENT_MIGRATION_PLAN.md.
#
# Usage:
#   ./scripts/build-component.sh [mysql-version] [mysql-source-dir]
#   MYSQL_SOURCE_DIR=/path ./scripts/build-component.sh
#
# - If mysql-source-dir is given, use it as MYSQL_SOURCE_DIR.
# - Else downloads mysql-version tarball into .cache/mysql-server-<version>/
#   (workspace-local, sandbox-friendly, no git; reused on next run).
#   Set MYVECTOR_BUILD_TEMP_DIR to use a different path (e.g. /tmp/my-mysql).
# - Download is silent (curl -sL / wget -q).
# Output: build/libmyvector_component.so and build/component/myvector.json
#   (or build/libmyvector_component.dylib on macOS).

set -e

MYSQL_VERSION="${1:-mysql-8.4.8}"
MYSQL_SOURCE_DIR_OVERRIDE="${2}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ARCH="$(uname -m)"
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"

if [ -n "$MYSQL_SOURCE_DIR_OVERRIDE" ]; then
  MYSQL_SOURCE_DIR="$MYSQL_SOURCE_DIR_OVERRIDE"
  if [ ! -f "$MYSQL_SOURCE_DIR/include/mysql/components/component_implementation.h" ]; then
    echo "Error: $MYSQL_SOURCE_DIR does not contain include/mysql/components/component_implementation.h" >&2
    exit 1
  fi
  echo "Using existing MySQL source: $MYSQL_SOURCE_DIR"
else
  # Use workspace-local cache (sandbox-friendly, no .git). Reused across runs.
  # Override with MYVECTOR_BUILD_TEMP_DIR to use a different path (e.g. /tmp).
  SANITIZED_VERSION="${MYSQL_VERSION//\//-}"
  CACHE_DIR="${MYVECTOR_BUILD_TEMP_DIR:-$REPO_ROOT/.cache/mysql-server-$SANITIZED_VERSION}"
  if [ -f "$CACHE_DIR/include/mysql/components/component_implementation.h" ]; then
    echo "Using cached MySQL source: $CACHE_DIR"
    MYSQL_SOURCE_DIR="$CACHE_DIR"
  else
    PARENT="$(dirname "$CACHE_DIR")"
    mkdir -p "$PARENT"
    rm -rf "$CACHE_DIR"
    # Prefer tag (refs/tags) — CI uses e.g. mysql-8.4.8; fall back to branch (refs/heads)
    for REF in "refs/tags/${MYSQL_VERSION}" "refs/heads/${MYSQL_VERSION}"; do
      TARBALL_URL="https://github.com/mysql/mysql-server/archive/${REF}.tar.gz"
      TARBALL="$PARENT/mysql-server-$SANITIZED_VERSION.tar.gz"
      echo "Downloading MySQL $MYSQL_VERSION (quiet) to $CACHE_DIR ..."
      if command -v curl >/dev/null 2>&1; then
        HTTP="$(curl -sL -o "$TARBALL" -w "%{http_code}" "$TARBALL_URL")"
      elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$TARBALL" "$TARBALL_URL" && HTTP=200 || HTTP=000
      else
        echo "Error: need curl or wget to download MySQL source" >&2
        exit 1
      fi
      if [ "$HTTP" = "200" ] && [ -s "$TARBALL" ] && tar tzf "$TARBALL" >/dev/null 2>&1; then
        break
      fi
      rm -f "$TARBALL"
      if [ "$REF" = "refs/heads/${MYSQL_VERSION}" ]; then
        echo "Error: failed to download MySQL source (tried tag and branch)" >&2
        exit 1
      fi
    done
    EXTRACTED_TOP="$(tar tzf "$TARBALL" 2>/dev/null | head -1 | cut -d/ -f1)"
    tar xzf "$TARBALL" -C "$PARENT"
    mv "$PARENT/$EXTRACTED_TOP" "$CACHE_DIR"
    rm -f "$TARBALL"
    MYSQL_SOURCE_DIR="$CACHE_DIR"
  fi
fi

export MYSQL_SOURCE_DIR

# Install build deps (optional; may already be present)
if [ "$OS" = "linux" ]; then
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq \
      build-essential \
      cmake \
      libmysqlclient-dev \
      pkg-config \
      >/dev/null 2>&1 || true
  fi
elif [ "$OS" = "darwin" ]; then
  if command -v brew >/dev/null 2>&1; then
    brew list mysql-client >/dev/null 2>&1 || brew install mysql-client || true
  fi
fi

# Configure and build from repo root
cd "$REPO_ROOT"
rm -rf build
mkdir -p build
cd build

CMAKE_EXTRA=()
if [ -n "${MYSQL_DIR:-}" ]; then
  CMAKE_EXTRA+=(-DMYSQL_DIR="$MYSQL_DIR")
fi
if [ -n "${MYSQL_BUILD_DIR:-}" ]; then
  CMAKE_EXTRA+=(-DMYSQL_BUILD_DIR="$MYSQL_BUILD_DIR")
fi

cmake -DCMAKE_BUILD_TYPE=Release \
  -DMYSQL_SOURCE_DIR="$MYSQL_SOURCE_DIR" \
  "${CMAKE_EXTRA[@]}" \
  ..

if [ "$OS" = "linux" ]; then
  make -j"$(nproc)"
else
  make -j"$(sysctl -n hw.ncpu 2>/dev/null || echo 1)"
fi

# Copy manifest next to library for packaging
mkdir -p component
if [ -f "libmyvector_component.so" ]; then
  cp libmyvector_component.so component/ || { echo "Error: failed to copy libmyvector_component.so to component/" >&2; exit 1; }
  cp "$REPO_ROOT/src/component_src/myvector.json" component/ || { echo "Error: failed to copy myvector.json to component/" >&2; exit 1; }
  echo "Built: build/libmyvector_component.so, build/component/myvector.json"
elif [ -f "libmyvector_component.dylib" ]; then
  cp libmyvector_component.dylib component/ || { echo "Error: failed to copy libmyvector_component.dylib to component/" >&2; exit 1; }
  cp "$REPO_ROOT/src/component_src/myvector.json" component/ || { echo "Error: failed to copy myvector.json to component/" >&2; exit 1; }
  echo "Built: build/libmyvector_component.dylib, build/component/myvector.json"
else
  echo "Error: build did not produce libmyvector_component.so or libmyvector_component.dylib" >&2
  find . -name 'myvector_component*' -o -name 'myvector.json' 2>/dev/null || true
  exit 1
fi
