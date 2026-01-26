#!/bin/bash
set -ex

# Usage: ./scripts/build-release.sh <mysql-version> <output-dir>
MYSQL_VERSION=$1
OUTPUT_DIR=$2
ARCH=$(uname -m)
case $ARCH in
x86_64) ARCH="amd64" ;;
aarch64) ARCH="arm64" ;;
esac
OS=$(uname -s | tr '[:upper:]' '[:lower:]')
BOOST_VERSION="1.77.0"

# Create a temporary directory for the build
BUILD_DIR=$(mktemp -d)
trap 'rm -rf "$BUILD_DIR"' EXIT
BOOST_DIR="$BUILD_DIR/mysql-server/boost"

# Checkout MySQL Server
git clone --depth 1 --branch "mysql-${MYSQL_VERSION}" https://github.com/mysql/mysql-server.git "$BUILD_DIR/mysql-server"
(
	cd "$BUILD_DIR/mysql-server"
	git submodule update --init --recursive
)

# Copy MyVector plugin source from the local checkout
mkdir -p "$BUILD_DIR/mysql-server/plugin/myvector"
cp src/*.cc "$BUILD_DIR/mysql-server/plugin/myvector/"
cp include/*.h "$BUILD_DIR/mysql-server/plugin/myvector/"
cp include/*.i "$BUILD_DIR/mysql-server/plugin/myvector/" 2>/dev/null || true
cp CMakeLists.txt "$BUILD_DIR/mysql-server/plugin/myvector/"
cp -r sql "$BUILD_DIR/mysql-server/plugin/myvector/"

# Install Dependencies (for Ubuntu)
if [ "$OS" == "linux" ]; then
	sudo apt-get update
	sudo apt-get install -y cmake g++ bison libssl-dev libncurses5-dev libsasl2-dev libtirpc-dev
fi

# Pre-download Boost on macOS to avoid CMake extraction issues
if [ "$OS" == "darwin" ]; then
	mkdir -p "$BOOST_DIR"
	BOOST_TARBALL="boost_${BOOST_VERSION//./_}.tar.bz2"
	BOOST_URL="https://boostorg.jfrog.io/artifactory/main/release/${BOOST_VERSION}/source/${BOOST_TARBALL}"
	if [ ! -f "$BOOST_DIR/$BOOST_TARBALL" ]; then
		curl -L "$BOOST_URL" -o "$BOOST_DIR/$BOOST_TARBALL"
	fi
	tar -xjf "$BOOST_DIR/$BOOST_TARBALL" -C "$BOOST_DIR"
fi

# Configure and build
(
	cd "$BUILD_DIR/mysql-server"
	mkdir bld
	cd bld
	if [ "$OS" == "darwin" ]; then
		cmake .. -DDOWNLOAD_BOOST=0 -DWITH_BOOST="../boost/boost_${BOOST_VERSION//./_}" -DFORCE_INSOURCE_BUILD=1
	else
		cmake .. -DDOWNLOAD_BOOST=1 -DWITH_BOOST=../boost -DFORCE_INSOURCE_BUILD=1
	fi
	if [ "$OS" == "linux" ]; then
		NUM_CORES=$(nproc)
	elif [ "$OS" == "darwin" ]; then
		NUM_CORES=$(sysctl -n hw.ncpu)
	else
		NUM_CORES=1
	fi
	make -j"$NUM_CORES" VERBOSE=1 myvector
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
