#!/usr/bin/env bash
# Pull each published MyVector image from GHCR and run scripts/smoke-readme.sh.
#
# Usage (from repository root):
#   ./scripts/smoke-published-images.sh
#   REGISTRY_IMAGE=ghcr.io/org/other ./scripts/smoke-published-images.sh
#
# Optional heavier load (Stanford subset):
#   MYVECTOR_SMOKE_STANFORD=1 ./scripts/smoke-published-images.sh
#
# Requires: Docker, bash. Log in to ghcr.io if packages are private.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

REGISTRY_IMAGE="${REGISTRY_IMAGE:-ghcr.io/askdba/myvector}"
TAGS=(mysql8.0 mysql8.4 mysql9.6)

MYSQL_ROOT_PASSWORD="${MYSQL_ROOT_PASSWORD:-myvector}"
MYSQL_DATABASE="${MYSQL_DATABASE:-vectordb}"
export MYSQL_ROOT_PASSWORD MYSQL_DATABASE

echo "=== Smoke published images: ${REGISTRY_IMAGE}:{${TAGS[*]}} ==="
echo ""

for tag in "${TAGS[@]}"; do
	full="${REGISTRY_IMAGE}:${tag}"
	echo ">>> Pull ${full}"
	docker pull "${full}"
	echo ">>> Smoke ${full}"
	bash "${REPO_ROOT}/scripts/smoke-readme.sh" "${full}"
	echo ""
done

echo "=== All smokes completed OK ==="
