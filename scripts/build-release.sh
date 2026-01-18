#!/bin/bash
set -ex

# Usage: ./scripts/build-release.sh <mysql-version> <output-dir>
MYSQL_VERSION=$1
OUTPUT_DIR=$2
ARCH=$(uname -m)
OS=$(uname -s | tr '[:upper:]' '[:lower:]')

# Create a temporary directory for the build
BUILD_DIR=$(mktemp -d)
trap 'rm -rf "$BUILD_DIR"' EXIT

# Checkout MySQL Server
git clone --depth 1 --branch "mysql-${MYSQL_VERSION}" https://github.com/mysql/mysql-server.git "$BUILD_DIR/mysql-server"
(
  cd "$BUILD_DIR/mysql-server"
  git submodule update --init --recursive
)

# Copy MyVector plugin source from the local checkout
cp -r . "$BUILD_DIR/mysql-server/plugin/myvector"

# Install Dependencies (for Ubuntu)
if [ "$OS" == "linux" ]; then
  sudo apt-get update
  sudo apt-get install -y cmake g++ bison libssl-dev libncurses5-dev libsasl2-dev libtirpc-dev
fi

# Configure and build
(
  cd "$BUILD_DIR/mysql-server"
  mkdir bld
  cd bld
  cmake .. -DDOWNLOAD_BOOST=1 -DWITH_BOOST=../boost -DFORCE_INSOURCE_BUILD=1
  make -j$(nproc) myvector
)

# Package the release artifact
ARTIFACT_NAME="myvector-mysql${MYSQL_VERSION}-${OS}-${ARCH}"
PACKAGE_DIR="$BUILD_DIR/$ARTIFACT_NAME"
mkdir -p "$PACKAGE_DIR"
cp "$BUILD_DIR/mysql-server/bld/plugin_output_directory/myvector.so" "$PACKAGE_DIR/"
cp "$BUILD_DIR/mysql-server/plugin/myvector/sql/myvectorplugin.sql" "$PACKAGE_DIR/"
(
  cd "$BUILD_DIR"
  tar -czf "$ARTIFACT_NAME.tar.gz" "$ARTIFACT_NAME"
)

# Move artifact to the final output directory
mv "$BUILD_DIR/$ARTIFACT_NAME.tar.gz" "$OUTPUT_DIR/"
echo "Created artifact: $OUTPUT_DIR/$ARTIFACT_NAME.tar.gz"
