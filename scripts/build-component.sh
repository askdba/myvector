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
# - Else if mysql-version is given, clone that branch into a temp dir and use it.
# - Else clone mysql-8.4.8 into a temp dir.
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
  BUILD_DIR="${MYVECTOR_BUILD_TEMP_DIR:-$(mktemp -d)}"
  if [ -z "$MYSQL_SOURCE_DIR_OVERRIDE" ] && [ -z "${MYVECTOR_BUILD_TEMP_DIR:-}" ]; then
    trap 'rm -rf "$BUILD_DIR"' EXIT
  fi
  MYSQL_SOURCE_DIR="$BUILD_DIR/mysql-server"
  echo "Cloning MySQL $MYSQL_VERSION into $MYSQL_SOURCE_DIR"
  git clone --depth 1 --branch "$MYSQL_VERSION" \
    https://github.com/mysql/mysql-server.git "$MYSQL_SOURCE_DIR"
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
  cp libmyvector_component.so component/ 2>/dev/null || true
  cp "$REPO_ROOT/src/component_src/myvector.json" component/
  echo "Built: build/libmyvector_component.so, build/component/myvector.json"
elif [ -f "libmyvector_component.dylib" ]; then
  cp libmyvector_component.dylib component/ 2>/dev/null || true
  cp "$REPO_ROOT/src/component_src/myvector.json" component/
  echo "Built: build/libmyvector_component.dylib, build/component/myvector.json"
else
  echo "Built (artifact location may vary):"
  find . -name 'myvector_component*' -o -name 'myvector.json' 2>/dev/null || true
fi
