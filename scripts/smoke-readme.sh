#!/bin/bash
set -euo pipefail

IMAGE="${1:-myvector-smoke:local}"
MYSQL_ROOT_PASSWORD="${MYSQL_ROOT_PASSWORD:-myvector}"
MYSQL_DATABASE="${MYSQL_DATABASE:-vectordb}"
CONTAINER_NAME="myvector-smoke-$$"
SMOKE_STANFORD="${MYVECTOR_SMOKE_STANFORD:-0}"
STANFORD_LINES="${MYVECTOR_SMOKE_STANFORD_LINES:-2000}"
STANFORD_DIR="examples/stanford50d"

cleanup() {
	docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run -d \
	--name "$CONTAINER_NAME" \
	-e MYSQL_ROOT_PASSWORD="$MYSQL_ROOT_PASSWORD" \
	-e MYSQL_DATABASE="$MYSQL_DATABASE" \
	"$IMAGE" >/dev/null

ensure_container_running() {
	local running
	running="$(docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null || true)"
	if [ "$running" != "true" ]; then
		echo "Container stopped before MySQL was ready."
		docker logs "$CONTAINER_NAME" || true
		exit 1
	fi
}

MAX_ATTEMPTS=120
READY_STREAK=0
UDF_STREAK=0
UDF_READY=0

echo "Waiting for MySQL and MyVector UDFs to be ready..."
for _ in $(seq 1 "$MAX_ATTEMPTS"); do
	ensure_container_running
	if docker exec "$CONTAINER_NAME" \
		mysqladmin ping -uroot -p"$MYSQL_ROOT_PASSWORD" --silent >/dev/null 2>&1; then
		READY_STREAK=$((READY_STREAK + 1))
	else
		READY_STREAK=0
		UDF_STREAK=0
	fi
	if [ "$READY_STREAK" -ge 2 ]; then
		if docker exec "$CONTAINER_NAME" \
			mysql -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" \
			-e "SELECT myvector_construct('[1.0, 2.0, 3.0]');" >/dev/null 2>&1; then
			UDF_STREAK=$((UDF_STREAK + 1))
		else
			UDF_STREAK=0
		fi
	else
		UDF_STREAK=0
	fi
	if [ "$UDF_STREAK" -ge 2 ]; then
		UDF_READY=1
		break
	fi
	sleep 2
done

ensure_container_running
if [ "$UDF_READY" -ne 1 ]; then
	echo "MyVector UDFs did not become available in time."
	docker logs "$CONTAINER_NAME" || true
	exit 1
fi

docker exec -i "$CONTAINER_NAME" \
	mysql -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" <<'SQL'
SELECT myvector_display(myvector_construct('[1.0, 2.0, 3.0]')) AS vec;
SELECT myvector_distance(
  myvector_construct('[1.0, 0.0]'),
  myvector_construct('[0.0, 1.0]'),
  'L2'
) AS dist_l2;
SQL

if [ "$SMOKE_STANFORD" = "1" ] && [ -d "$STANFORD_DIR" ]; then
	if [ ! -f "$STANFORD_DIR/create.sql" ] || [ ! -f "$STANFORD_DIR/insert50d.sql.gz" ]; then
		echo "Stanford 50d demo files missing, skipping sample load."
		exit 0
	fi

	tmpdir="$(mktemp -d)"
	gzip -cd "$STANFORD_DIR/insert50d.sql.gz" | head -n "$STANFORD_LINES" >"$tmpdir/insert50d_subset.sql"
	cp "$STANFORD_DIR/create.sql" "$tmpdir/create.sql"

	docker cp "$tmpdir/create.sql" "$CONTAINER_NAME":/tmp/create.sql
	docker cp "$tmpdir/insert50d_subset.sql" "$CONTAINER_NAME":/tmp/insert50d_subset.sql

	docker exec -i "$CONTAINER_NAME" \
		mysql -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" <<'SQL'
SOURCE /tmp/create.sql;
SOURCE /tmp/insert50d_subset.sql;
SELECT myvector_display(wordvec) AS vec FROM words50d WHERE word='the';
SELECT word, myvector_distance(wordvec, (SELECT wordvec FROM words50d WHERE word='the')) AS dist
FROM words50d
ORDER BY dist
LIMIT 5;
SQL
fi
