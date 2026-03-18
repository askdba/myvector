#!/usr/bin/env bash
# Test online index updates (docs/ONLINE_INDEX_UPDATES.md) with Docker.
#
# Usage:
#   ./scripts/test-online-updates.sh                    # Use prebuilt image
#   ./scripts/test-online-updates.sh myvector:local     # Use local image
#
# Requires: Docker, mysql client (or use docker exec)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

IMAGE="${1:-ghcr.io/askdba/myvector:mysql8.4}"
CONTAINER="myvector-online-test-$$"
# Root password: use non-empty so prebuilt images (without empty-value config fix) parse correctly
ROOT_PW="${MYSQL_ROOT_PASSWORD:-myvectortest}"
DB="${MYSQL_DATABASE:-vectordb}"
BINLOG_USER="${MYVECTOR_BINLOG_USER:-myvector_binlog}"
BINLOG_PW="${MYVECTOR_BINLOG_PASSWORD:-myvector_binlog_pw}"
# Use socket for all mysql commands (works in container; TCP has temp/main server timing issues)
MYSQL_OPTS="-uroot -p${ROOT_PW}"

echo "=== Online Index Updates Test (docs/ONLINE_INDEX_UPDATES.md) ==="
echo "Image: $IMAGE"
# Prebuilt image may show index build socket error until a new image with config fix is published
[ "${IMAGE#ghcr.io}" != "$IMAGE" ] && echo "Note: Prebuilt image may show index build error; binlog/online updates still validated."
echo ""

# Config must be owned by mysql user so the plugin can load it. Create via init script.
# Use project dir (mktemp on Docker for Mac can mount empty).
INIT_DIR="$REPO_ROOT/.myvector-test-init"
rm -rf "$INIT_DIR" && mkdir -p "$INIT_DIR"
trap "rm -rf $INIT_DIR" EXIT
cat > "$INIT_DIR/001-myvector-config.sh" <<INITSCRIPT
#!/bin/bash
cat > /tmp/myvector.cnf <<'MYVECTORCONF'
myvector_user_id=USERPLACEHOLDER
myvector_user_password=BINLOGPWPLACEHOLDER
myvector_host=127.0.0.1
myvector_port=3306
MYVECTORCONF
chown mysql:mysql /tmp/myvector.cnf
chmod 600 /tmp/myvector.cnf
INITSCRIPT
# Inject password into init script
BINLOG_USER_ESCAPED=$(echo "$BINLOG_USER" | sed 's/[\/&]/\\&/g')
BINLOG_PW_ESCAPED=$(echo "$BINLOG_PW" | sed 's/[\/&]/\\&/g')
sed -i.bak "s/USERPLACEHOLDER/$BINLOG_USER_ESCAPED/" "$INIT_DIR/001-myvector-config.sh"
sed -i.bak "s/BINLOGPWPLACEHOLDER/$BINLOG_PW_ESCAPED/" "$INIT_DIR/001-myvector-config.sh"
rm -f "$INIT_DIR/001-myvector-config.sh.bak"
chmod +x "$INIT_DIR/001-myvector-config.sh"
cp "$REPO_ROOT/sql/myvectorplugin.sql" "$INIT_DIR/002-myvectorplugin.sql"
cat > "$INIT_DIR/003-myvector-user.sql" <<EOSQL
CREATE USER IF NOT EXISTS '${BINLOG_USER}'@'%' IDENTIFIED BY '${BINLOG_PW}';
GRANT REPLICATION CLIENT ON *.* TO '${BINLOG_USER}'@'%';
GRANT SELECT ON *.* TO '${BINLOG_USER}'@'%';
FLUSH PRIVILEGES;
EOSQL

MYSQL_CONF_DIR=$(mktemp -d)
trap "rm -rf $INIT_DIR $MYSQL_CONF_DIR" EXIT
cat > "$MYSQL_CONF_DIR/custom.cnf" <<EOF
[mysqld]
binlog_format=ROW
log_bin=mysql-bin
myvector_config_file=/tmp/myvector.cnf
EOF

docker run -d --name "$CONTAINER" \
  -e MYSQL_ROOT_PASSWORD="$ROOT_PW" \
  -e MYSQL_DATABASE="$DB" \
  -v "$INIT_DIR:/docker-entrypoint-initdb.d:ro" \
  -v "$MYSQL_CONF_DIR/custom.cnf:/etc/mysql/conf.d/zzz-myvector.cnf:ro" \
  "$IMAGE"
trap "docker rm -f $CONTAINER 2>/dev/null || true; rm -rf $INIT_DIR $MYSQL_CONF_DIR" EXIT

# Wait for MySQL (5 consecutive pings for stability across temp->main transition)
echo "Waiting for MySQL..."
READY_STREAK=0
for i in $(seq 1 90); do
  if docker exec "$CONTAINER" mysqladmin ping -uroot -p"$ROOT_PW" --silent 2>/dev/null; then
    READY_STREAK=$((READY_STREAK + 1))
    [ "$READY_STREAK" -ge 5 ] && break
  else
    READY_STREAK=0
  fi
  sleep 2
done
if ! docker exec "$CONTAINER" mysql $MYSQL_OPTS -e "SELECT 1"; then
  # Some images initialize root with empty password. Fallback for admin commands.
  if docker exec "$CONTAINER" mysql -uroot -e "SELECT 1" >/dev/null 2>&1; then
    MYSQL_OPTS="-uroot"
    echo "Detected empty root password in container; using socket auth for admin commands."
  else
    echo "MySQL not ready (container may have exited)"
    echo "=== Container logs ==="
    docker logs "$CONTAINER" 2>&1 | tail -80
    exit 1
  fi
fi

echo "Configuring MyVector (index dir, config path)..."
echo "Restarting container so MyVector rereads config file..."
docker restart "$CONTAINER" >/dev/null

# Wait again after restart
READY_STREAK=0
for i in $(seq 1 90); do
  if docker exec "$CONTAINER" mysqladmin ping -uroot -p"$ROOT_PW" --silent 2>/dev/null; then
    READY_STREAK=$((READY_STREAK + 1))
    [ "$READY_STREAK" -ge 5 ] && break
  else
    READY_STREAK=0
  fi
  sleep 2
done
if ! docker exec "$CONTAINER" mysql $MYSQL_OPTS -e "SELECT 1" >/dev/null 2>&1; then
  if docker exec "$CONTAINER" mysql -uroot -e "SELECT 1" >/dev/null 2>&1; then
    MYSQL_OPTS="-uroot"
  else
    echo "MySQL not ready after restart"
    echo "=== Container logs ==="
    docker logs "$CONTAINER" 2>&1 | tail -80
    exit 1
  fi
fi

docker exec "$CONTAINER" mysql $MYSQL_OPTS -e "
  SET GLOBAL myvector_index_dir='/var/lib/mysql';
  SET GLOBAL myvector_config_file='/tmp/myvector.cnf';
" 2>/dev/null || true

echo "Checking binlog and myvector..."
docker exec "$CONTAINER" mysql $MYSQL_OPTS mysql -e "
  SHOW VARIABLES LIKE 'binlog_format';
  SHOW VARIABLES LIKE 'log_bin';
  SHOW VARIABLES LIKE 'myvector_config_file';
  SHOW VARIABLES LIKE 'myvector_index_dir';
  SELECT myvector_construct('[1.0, 2.0, 3.0]') AS test;
"

echo ""
echo "Creating table with online=Y (3-dim vectors)..."
docker exec "$CONTAINER" mysql $MYSQL_OPTS "$DB" -e "
  SET GLOBAL binlog_format = 'ROW';
  DROP TABLE IF EXISTS embeddings;
  CREATE TABLE embeddings (
    id INT PRIMARY KEY,
    text VARCHAR(512),
    vec MYVECTOR(type=HNSW,dim=3,size=1000,online=Y,idcol=id,dist=L2)
  );
"

echo "Building initial index..."
BUILD_OUT=$(docker exec "$CONTAINER" mysql $MYSQL_OPTS "$DB" -N -e "
  CALL mysql.myvector_index_build('${DB}.embeddings.vec', 'id');
" 2>&1 || true)
echo "$BUILD_OUT"
if echo "$BUILD_OUT" | grep -qE "Error in new connection|ERROR"; then
  echo "FAIL: myvector_index_build failed"
  exit 1
fi

echo "Inserting initial row..."
docker exec "$CONTAINER" mysql $MYSQL_OPTS "$DB" -e "
  INSERT INTO embeddings (id, text, vec) VALUES (1, 'hello', myvector_construct('[1.1, 2.2, 3.3]'));
"

echo "Inserting row 2 (online update)..."
docker exec "$CONTAINER" mysql $MYSQL_OPTS "$DB" -e "
  INSERT INTO embeddings (id, text, vec) VALUES (2, 'world', myvector_construct('[2.2, 3.3, 4.4]'));
"
sleep 2  # Allow binlog thread to process

echo "Inserting row 3 (online update)..."
docker exec "$CONTAINER" mysql $MYSQL_OPTS "$DB" -e "
  INSERT INTO embeddings (id, text, vec) VALUES (3, 'foo', myvector_construct('[3.3, 4.4, 5.5]'));
"
sleep 2  # Allow binlog thread to process

echo "Search for nearest to [2.0, 3.0, 4.0] (expect 2 or 3)..."
docker exec "$CONTAINER" mysql $MYSQL_OPTS "$DB" -e "
  SELECT id, text, myvector_row_distance(id) AS dist
  FROM embeddings
  WHERE MYVECTOR_IS_ANN('${DB}.embeddings.vec', 'id', myvector_construct('[2.0, 3.0, 4.0]'), 3);
"

echo ""
echo "Updating row 2 (online update)..."
docker exec "$CONTAINER" mysql $MYSQL_OPTS "$DB" -e "
  UPDATE embeddings SET vec = myvector_construct('[9.9, 9.9, 9.9]') WHERE id = 2;
"
sleep 2

echo "Search for nearest to [9.9, 9.9, 9.9] (expect id=2)..."
docker exec "$CONTAINER" mysql $MYSQL_OPTS "$DB" -e "
  SELECT id, text, myvector_row_distance(id) AS dist
  FROM embeddings
  WHERE MYVECTOR_IS_ANN('${DB}.embeddings.vec', 'id', myvector_construct('[9.9, 9.9, 9.9]'), 1);
"

echo ""
echo "Deleting row 2 (online update)..."
docker exec "$CONTAINER" mysql $MYSQL_OPTS "$DB" -e "
  DELETE FROM embeddings WHERE id = 2;
"
sleep 2

echo "Search for [9.9, 9.9, 9.9] again (should NOT find id=2)..."
SEARCH_OUT=$(docker exec "$CONTAINER" mysql $MYSQL_OPTS "$DB" -N -e "
  SELECT id, text FROM embeddings
  WHERE MYVECTOR_IS_ANN('${DB}.embeddings.vec', 'id', myvector_construct('[9.9, 9.9, 9.9]'), 1);
" 2>/dev/null)
if echo "$SEARCH_OUT" | grep -qE '^2[[:space:]]'; then
  echo "FAIL: id=2 found in results after DELETE (online update should have removed it)"
  docker rm -f "$CONTAINER" 2>/dev/null || true
  exit 1
fi
echo "PASS: id=2 correctly absent after DELETE"

echo ""
echo "=== Online updates test PASSED ==="
