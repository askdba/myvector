#!/usr/bin/env bash
# Build MyVector Docker image locally (for testing).
# Produces myvector-amd64.so via Docker, then builds the image.
#
# Usage: ./scripts/build-docker-local.sh [mysql-version]
# Example: ./scripts/build-docker-local.sh 8.4
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

MYSQL_VERSION="${1:-8.4}"
MYSQL_TAG="mysql-8.4.8"
[ "$MYSQL_VERSION" = "8.0" ] && MYSQL_TAG="mysql-8.0.45"
[ "$MYSQL_VERSION" = "9.0" ] && MYSQL_TAG="mysql-9.0.0"

echo "=== Building MyVector plugin for Docker (mysql $MYSQL_VERSION) ==="

# Build plugin inside Docker (same as CI)
docker run --rm \
  -v "$REPO_ROOT:/work" -w /work \
  ubuntu:22.04 bash -c "
    set -e
    apt-get update -qq
    apt-get install -y -qq cmake g++-10 gcc-10 git libssl-dev libncurses5-dev \
      pkg-config bison libtirpc-dev libldap2-dev libsasl2-dev libudev-dev \
      libre2-dev libcurl4-openssl-dev libprotobuf-dev protobuf-compiler
    rm -rf mysql-server
    git clone --depth 1 --branch $MYSQL_TAG https://github.com/mysql/mysql-server.git
    mkdir -p mysql-server/plugin/myvector
    cp src/*.cc mysql-server/plugin/myvector/
    cp include/*.h mysql-server/plugin/myvector/
    cp include/*.i mysql-server/plugin/myvector/ 2>/dev/null || true
    cp CMakeLists.txt mysql-server/plugin/myvector/
    mkdir -p mysql-server/bld && cd mysql-server/bld
    cmake .. -DCMAKE_C_COMPILER=gcc-10 -DCMAKE_CXX_COMPILER=g++-10 \
      -DDOWNLOAD_BOOST=1 -DWITH_UNIT_TESTS=OFF -DWITH_ROUTER=OFF \
      -DWITH_RAPID=OFF -DWITH_NDB=OFF -DWITH_NDBCLUSTER=OFF \
      -DWITH_EXAMPLE_STORAGE_ENGINE=OFF -DCMAKE_BUILD_TYPE=Release
    make myvector -j\$(nproc)
    cp plugin_output_directory/myvector.so /work/myvector-amd64.so
"
cp sql/myvectorplugin.sql .

echo "Building Docker image..."
docker build -f Dockerfile.oraclelinux9 \
  --build-arg MYSQL_VERSION="$MYSQL_VERSION" \
  -t "myvector:mysql${MYSQL_VERSION}-local" .

echo ""
echo "Done. Run: ./scripts/test-online-updates.sh myvector:mysql${MYSQL_VERSION}-local"
