#!/usr/bin/env bash
# Run mysql-test (mtr) for myvector suite in Docker sandbox.
# Builds MySQL with plugin in-tree, then runs mtr.
# Usage: ./scripts/run-mtr-sandbox.sh [test-name]
#   test-name: e.g. udf_construct_per_row (default: run full myvector suite)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CACHE_DIR="${REPO_ROOT}/.cache"
TEST_NAME="${1:-}"
cd "$REPO_ROOT"

mkdir -p "$CACHE_DIR/boost_cache"

echo "=== Build MySQL + MyVector plugin and run mtr (Docker) ==="
docker run --rm \
  -e MTR_TEST_NAME="$TEST_NAME" \
  -v "$REPO_ROOT:/work" \
  -v "$CACHE_DIR:/cache" \
  -w /work \
  ubuntu:22.04 bash -c '
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq \
  build-essential cmake git pkg-config \
  libssl-dev libncurses5-dev bison libtirpc-dev libldap2-dev libsasl2-dev \
  libudev-dev libre2-dev libcurl4-openssl-dev libprotobuf-dev protobuf-compiler

MYSQL_TAG=mysql-8.4.8
MS_DIR=/cache/mysql-server-8.4.8
BOOST_CACHE=/cache/boost_cache

if [ ! -f "$MS_DIR/include/mysql/components/component_implementation.h" ]; then
  echo "Cloning MySQL $MYSQL_TAG..."
  rm -rf "$MS_DIR"
  git clone -q --depth 1 --branch $MYSQL_TAG https://github.com/mysql/mysql-server.git "$MS_DIR"
fi

echo "Copying MyVector plugin..."
mkdir -p "$MS_DIR/plugin/myvector"
cp /work/src/myvector_plugin.cc /work/src/myvector_binlog.cc /work/src/myvector.cc /work/src/myvectorutils.cc "$MS_DIR/plugin/myvector/"
cp /work/include/*.h "$MS_DIR/plugin/myvector/" 2>/dev/null || true
cp /work/include/*.i "$MS_DIR/plugin/myvector/" 2>/dev/null || true
cp /work/CMakeLists.txt "$MS_DIR/plugin/myvector/"

echo "Configuring MySQL..."
BLD_DIR=/tmp/mysql-bld
rm -rf "$BLD_DIR"
mkdir -p "$BLD_DIR"
( cd "$BLD_DIR" && cmake "$MS_DIR" \
  -DDOWNLOAD_BOOST=1 -DWITH_BOOST="$BOOST_CACHE" \
  -DWITH_UNIT_TESTS=OFF -DWITH_ROUTER=OFF -DWITH_RAPID=OFF \
  -DWITH_NDB=OFF -DWITH_NDBCLUSTER=OFF -DWITH_GROUP_REPLICATION=OFF \
  -DWITH_EXAMPLE_STORAGE_ENGINE=OFF \
  -DCMAKE_BUILD_TYPE=Release ) || true

echo "Building MySQL (full build for mtr)..."
cd "$BLD_DIR"
make -j"$(nproc)" 2>&1 | tail -20

echo "Copying mysql-test suite..."
mkdir -p "$MS_DIR/mysql-test/suite/myvector"
cp -r /work/mysql-test/suite/myvector/t /work/mysql-test/suite/myvector/r "$MS_DIR/mysql-test/suite/myvector/" 2>/dev/null || true
[ -f /work/mysql-test/suite/myvector/my.cnf ] && cp /work/mysql-test/suite/myvector/my.cnf "$MS_DIR/mysql-test/suite/myvector/" || true

MTR_SUITE="suite/myvector"
if [ -n "${MTR_TEST_NAME:-}" ]; then
  MTR_SUITE="suite/myvector/${MTR_TEST_NAME}"
fi

echo "Running mtr: $MTR_SUITE"
./mysql-test/mtr --force --max-test-fail=0 "$MTR_SUITE" 2>&1
'
