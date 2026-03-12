#!/usr/bin/env bash
# Run component build + test in Docker (CI-like sandbox). Mirrors test-component job.
# Uses .cache/ for MySQL source + Boost; subsequent runs are fast (~1–2 min).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"
BUILD_DIR="${REPO_ROOT}/.sandbox-out"
CACHE_DIR="${REPO_ROOT}/.cache"
rm -rf "$BUILD_DIR" && mkdir -p "$BUILD_DIR"
trap 'rm -rf "$BUILD_DIR"' EXIT

mkdir -p "$CACHE_DIR/boost_cache"

echo "=== Phase 1: Build component (Linux) ==="
docker run --rm \
  -v "$REPO_ROOT:/work" \
  -v "$CACHE_DIR:/cache" \
  -v "$BUILD_DIR:/out" \
  -w /work \
  ubuntu:22.04 bash -c '
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq \
  build-essential cmake libmysqlclient-dev pkg-config git \
  libssl-dev libncurses5-dev bison libtirpc-dev libldap2-dev libsasl2-dev \
  libudev-dev libre2-dev libcurl4-openssl-dev libprotobuf-dev protobuf-compiler

MYSQL_TAG=mysql-8.4.8
MS_DIR=/cache/mysql-server-8.4.8
BOOST_CACHE=/cache/boost_cache
# Build and test both use 8.4 to avoid header/runtime ABI mismatch

if [ ! -f "$MS_DIR/include/mysql/components/component_implementation.h" ]; then
  echo "Cloning MySQL $MYSQL_TAG (one-time, ~2 min)..."
  rm -rf "$MS_DIR"
  git clone -q --depth 1 --branch $MYSQL_TAG https://github.com/mysql/mysql-server.git "$MS_DIR"
fi

if [ ! -f "$MS_DIR/bld/CMakeCache.txt" ]; then
  echo "Configuring MySQL (one-time, ~3–5 min)..."
  mkdir -p "$MS_DIR/bld"
  ( cd "$MS_DIR/bld" && cmake .. \
    -DDOWNLOAD_BOOST=1 -DWITH_BOOST="$BOOST_CACHE" \
    -DWITH_UNIT_TESTS=OFF -DWITH_ROUTER=OFF -DWITH_RAPID=OFF \
    -DWITH_NDB=OFF -DWITH_NDBCLUSTER=OFF -DWITH_EXAMPLE_STORAGE_ENGINE=OFF \
    -DCMAKE_BUILD_TYPE=Release )
fi

echo "Building MyVector component..."
cd /work
rm -rf build
cmake -S . -B build -DMYSQL_SOURCE_DIR="$MS_DIR" -DMYSQL_BUILD_DIR="$MS_DIR/bld" -DCMAKE_BUILD_TYPE=Release
make -C build -j"$(nproc)"
mkdir -p build/component
cp build/libmyvector_component.so src/component_src/myvector.json build/component/
cp build/component/libmyvector_component.so build/component/myvector.json /out/
'

echo ""
echo "=== Phase 2: Test with mysql:8.4 ==="
# Use 8.4 to match MS_DIR build (mysql-8.4.8); avoids header/runtime ABI mismatch
# MYSQL_ROOT_HOST=% allows root from any host (needed for docker exec)
CID=$(docker run -d -e MYSQL_ROOT_PASSWORD=root -e MYSQL_ROOT_HOST=% mysql:8.4)
trap 'docker rm -f "$CID" 2>/dev/null; rm -rf "$BUILD_DIR"' EXIT

echo "Waiting for MySQL..."
for i in $(seq 1 30); do
  docker exec "$CID" mysqladmin ping -uroot -proot --silent 2>/dev/null && break
  sleep 2
done
if ! docker exec "$CID" mysqladmin ping -uroot -proot --silent 2>/dev/null; then
  echo "Error: MySQL in container $CID did not become ready within the timeout" >&2
  exit 1
fi

PLUGIN_DIR=$(docker exec "$CID" mysql -uroot -proot -N -e "SELECT @@plugin_dir;")
docker cp "$BUILD_DIR/libmyvector_component.so" "$CID:$PLUGIN_DIR/myvector.so"
docker exec "$CID" mkdir -p /usr/share/mysql/component
docker cp "$BUILD_DIR/myvector.json" "$CID:/usr/share/mysql/component/myvector.json"

docker exec "$CID" mysql -uroot -proot -e "INSTALL COMPONENT 'file://myvector';"
docker exec "$CID" mysql -uroot -proot -e "SELECT myvector_display(myvector_construct('[1.0, 2.0, 3.0]'));"
docker exec "$CID" mysql -uroot -proot -e "SELECT myvector_distance(myvector_construct('[1.0, 2.0, 3.0]'), myvector_construct('[4.0, 5.0, 6.0]'));"

echo "Stanford 50d dataset..."
docker exec "$CID" mysql -uroot -proot -e "CREATE DATABASE IF NOT EXISTS test;"
docker cp "$REPO_ROOT/examples/stanford50d/create.sql" "$CID:/tmp/create.sql"
docker exec -i "$CID" mysql -uroot -proot test -e "SOURCE /tmp/create.sql;"
gunzip -c "$REPO_ROOT/examples/stanford50d/insert50d.sql.gz" | awk 'BEGIN{RS=";"} NR<=500{printf "%s;", $0} NR==500{exit}' | docker exec -i "$CID" mysql -uroot -proot test
docker exec "$CID" mysql -uroot -proot test -e "SELECT word, myvector_distance(wordvec, (SELECT wordvec FROM words50d WHERE word='the')) AS dist FROM words50d ORDER BY dist LIMIT 5;"

docker exec "$CID" mysql -uroot -proot -e "UNINSTALL COMPONENT 'file://myvector';"
docker rm -f "$CID" >/dev/null 2>&1

echo ""
echo "=== All component tests passed ==="
