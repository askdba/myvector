#!/usr/bin/env bash
# Run MyVector tests against the prebuilt Docker image (fast, no build).
# Usage: ./scripts/test-docker-image.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

IMAGE="${MYVECTOR_IMAGE:-ghcr.io/askdba/myvector:mysql8.4}"
CONTAINER="myvector-test-$$"

echo "=== Testing with prebuilt image: $IMAGE ==="
docker run -d --name "$CONTAINER" \
  -e MYSQL_ROOT_PASSWORD=myvector \
  -e MYSQL_DATABASE=test \
  -e MYSQL_ROOT_HOST=% \
  "$IMAGE"
trap "docker rm -f $CONTAINER 2>/dev/null || true" EXIT

echo "Waiting for MySQL..."
for i in $(seq 1 60); do
  if docker exec "$CONTAINER" mysql -uroot -pmyvector -e "SELECT 1" 2>/dev/null; then
    break
  fi
  sleep 2
done
docker exec "$CONTAINER" mysql -uroot -pmyvector -e "SELECT 1" || { echo "MySQL not ready"; exit 1; }

echo ""
echo "=== Basic UDF tests ==="
docker exec "$CONTAINER" mysql -uroot -pmyvector -e "
SELECT myvector_display(myvector_construct('[1.0, 2.0, 3.0]')) AS vec;
SELECT myvector_distance(myvector_construct('[1.0, 0.0]'), myvector_construct('[0.0, 1.0]'), 'L2') AS dist_l2;
SELECT myvector_distance(myvector_construct('[1.0, 0.0]'), myvector_construct('[0.0, 1.0]'), 'COSINE') AS dist_cosine;
"

echo ""
echo "=== Distance with nested myvector_construct (issue #79 pattern) ==="
docker exec "$CONTAINER" mysql -uroot -pmyvector test -e "
CREATE TABLE t79 (id INT PRIMARY KEY, v VARBINARY(128));
INSERT INTO t79 VALUES (1, myvector_construct('[1,2,3]')), (2, myvector_construct('[2,3,4]')), (3, myvector_construct('[3,4,5]'));
SELECT COUNT(*) AS cnt FROM (SELECT myvector_distance(v, myvector_construct('[1,2,3]'), 'L2') AS d FROM t79) sub;
DROP TABLE t79;
"

echo ""
echo "=== All tests passed ==="
