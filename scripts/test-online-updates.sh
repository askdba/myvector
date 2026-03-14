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
MYSQL_PWD="${MYSQL_ROOT_PASSWORD:-myvector}"
DB="${MYSQL_DATABASE:-vectordb}"

echo "=== Online Index Updates Test (docs/ONLINE_INDEX_UPDATES.md) ==="
echo "Image: $IMAGE"
echo ""

# Create myvector config for binlog thread and index build (root has REPLICATION CLIENT)
CONFIG_DIR=$(mktemp -d)
trap "rm -rf $CONFIG_DIR" EXIT
cat > "$CONFIG_DIR/myvector.cnf" <<EOF
myvector_user_id=root
myvector_user_password=$MYSQL_PWD
myvector_host=127.0.0.1
myvector_port=3306
EOF

MYSQL_CONF_DIR=$(mktemp -d)
trap "rm -rf $CONFIG_DIR $MYSQL_CONF_DIR" EXIT
cat > "$MYSQL_CONF_DIR/custom.cnf" <<EOF
[mysqld]
binlog_format=ROW
log_bin=mysql-bin
myvector_config_file=/tmp/myvector.cnf
EOF

docker run -d --name "$CONTAINER" \
  -e MYSQL_ROOT_PASSWORD="$MYSQL_PWD" \
  -e MYSQL_DATABASE="$DB" \
  -v "$CONFIG_DIR/myvector.cnf:/tmp/myvector.cnf:ro" \
  -v "$MYSQL_CONF_DIR/custom.cnf:/etc/mysql/conf.d/zzz-myvector.cnf:ro" \
  "$IMAGE"
trap "docker rm -f $CONTAINER 2>/dev/null || true; rm -rf $CONFIG_DIR $MYSQL_CONF_DIR" EXIT

echo "Waiting for MySQL..."
for i in $(seq 1 60); do
  if docker exec "$CONTAINER" mysql -uroot -p"$MYSQL_PWD" -e "SELECT 1" 2>/dev/null; then
    break
  fi
  sleep 2
done
docker exec "$CONTAINER" mysql -uroot -p"$MYSQL_PWD" -e "SELECT 1" || { echo "MySQL not ready"; exit 1; }

echo "Configuring MyVector (index dir, config path)..."
docker exec "$CONTAINER" mysql -uroot -p"$MYSQL_PWD" -e "
  SET GLOBAL myvector_index_dir='/var/lib/mysql';
  SET GLOBAL myvector_config_file='/tmp/myvector.cnf';
" 2>/dev/null || true

echo "Checking binlog and myvector..."
docker exec "$CONTAINER" mysql -uroot -p"$MYSQL_PWD" -e "
  SHOW VARIABLES LIKE 'binlog_format';
  SHOW VARIABLES LIKE 'log_bin';
  SHOW VARIABLES LIKE 'myvector_config_file';
  SHOW VARIABLES LIKE 'myvector_index_dir';
  SELECT myvector_construct('[1.0, 2.0, 3.0]') AS test;
"

echo ""
echo "Creating table with online=Y (3-dim vectors)..."
docker exec "$CONTAINER" mysql -uroot -p"$MYSQL_PWD" "$DB" -e "
  SET GLOBAL binlog_format = 'ROW';
  DROP TABLE IF EXISTS embeddings;
  CREATE TABLE embeddings (
    id INT PRIMARY KEY,
    text VARCHAR(512),
    vec MYVECTOR(type=HNSW,dim=3,size=1000,online=Y,idcol=id,dist=L2)
  );
"

echo "Building initial index..."
docker exec "$CONTAINER" mysql -uroot -p"$MYSQL_PWD" "$DB" -e "
  CALL mysql.myvector_index_build('${DB}.embeddings.vec', 'id');
"

echo "Inserting initial row..."
docker exec "$CONTAINER" mysql -uroot -p"$MYSQL_PWD" "$DB" -e "
  INSERT INTO embeddings (id, text, vec) VALUES (1, 'hello', myvector_construct('[1.1, 2.2, 3.3]'));
"

echo "Inserting row 2 (online update)..."
docker exec "$CONTAINER" mysql -uroot -p"$MYSQL_PWD" "$DB" -e "
  INSERT INTO embeddings (id, text, vec) VALUES (2, 'world', myvector_construct('[2.2, 3.3, 4.4]'));
"
sleep 2  # Allow binlog thread to process

echo "Inserting row 3 (online update)..."
docker exec "$CONTAINER" mysql -uroot -p"$MYSQL_PWD" "$DB" -e "
  INSERT INTO embeddings (id, text, vec) VALUES (3, 'foo', myvector_construct('[3.3, 4.4, 5.5]'));
"
sleep 2  # Allow binlog thread to process

echo "Search for nearest to [2.0, 3.0, 4.0] (expect 2 or 3)..."
docker exec "$CONTAINER" mysql -uroot -p"$MYSQL_PWD" "$DB" -e "
  SELECT id, text, myvector_row_distance(id) AS dist
  FROM embeddings
  WHERE MYVECTOR_IS_ANN('${DB}.embeddings.vec', 'id', myvector_construct('[2.0, 3.0, 4.0]'), 3);
"

echo ""
echo "Updating row 2 (online update)..."
docker exec "$CONTAINER" mysql -uroot -p"$MYSQL_PWD" "$DB" -e "
  UPDATE embeddings SET vec = myvector_construct('[9.9, 9.9, 9.9]') WHERE id = 2;
"
sleep 2

echo "Search for nearest to [9.9, 9.9, 9.9] (expect id=2)..."
docker exec "$CONTAINER" mysql -uroot -p"$MYSQL_PWD" "$DB" -e "
  SELECT id, text, myvector_row_distance(id) AS dist
  FROM embeddings
  WHERE MYVECTOR_IS_ANN('${DB}.embeddings.vec', 'id', myvector_construct('[9.9, 9.9, 9.9]'), 1);
"

echo ""
echo "Deleting row 2 (online update)..."
docker exec "$CONTAINER" mysql -uroot -p"$MYSQL_PWD" "$DB" -e "
  DELETE FROM embeddings WHERE id = 2;
"
sleep 2

echo "Search for [9.9, 9.9, 9.9] again (should NOT find id=2)..."
SEARCH_OUT=$(docker exec "$CONTAINER" mysql -uroot -p"$MYSQL_PWD" "$DB" -N -e "
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
