#!/bin/bash
set -euo pipefail

IMAGE="${1:-myvector-smoke:local}"
MYSQL_ROOT_PASSWORD="${MYSQL_ROOT_PASSWORD:-myvector}"
MYSQL_DATABASE="${MYSQL_DATABASE:-vectordb}"
CONTAINER_NAME="myvector-smoke-$$"

cleanup() {
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run -d --rm \
  --name "$CONTAINER_NAME" \
  -e MYSQL_ROOT_PASSWORD="$MYSQL_ROOT_PASSWORD" \
  -e MYSQL_DATABASE="$MYSQL_DATABASE" \
  "$IMAGE" >/dev/null

echo "Waiting for MySQL to be ready..."
for _ in $(seq 1 30); do
  if docker exec "$CONTAINER_NAME" \
    mysqladmin ping -uroot -p"$MYSQL_ROOT_PASSWORD" --silent >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

docker exec "$CONTAINER_NAME" \
  mysqladmin ping -uroot -p"$MYSQL_ROOT_PASSWORD" --silent >/dev/null

echo "Waiting for MyVector UDFs to be available..."
for _ in $(seq 1 30); do
  if docker exec "$CONTAINER_NAME" \
    mysql -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" \
    -e "SELECT myvector_construct('[1.0, 2.0, 3.0]');" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

docker exec -i "$CONTAINER_NAME" \
  mysql -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" <<'SQL'
SELECT myvector_display(myvector_construct('[1.0, 2.0, 3.0]')) AS vec;
SELECT myvector_distance(
  myvector_construct('[1.0, 0.0]'),
  myvector_construct('[0.0, 1.0]'),
  'L2'
) AS dist_l2;
SQL
