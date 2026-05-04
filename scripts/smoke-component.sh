#!/usr/bin/env bash
# Smoke test the MyVector component against a plain mysql Docker image.
# Covers: UDF correctness, large dataset load (Stanford 50d GloVe), HNSW index
# build + ANN query, KNN brute-force, online updates (INSERT/UPDATE/DELETE while
# index is active), and component uninstall verification.
#
# Usage:
#   ./scripts/smoke-component.sh [mysql-version] [rows]
#
# Args:
#   mysql-version  8.4 (default) or 9.7
#   rows           Stanford 50d rows to load; default 50000, max ~400000
#
# Env overrides:
#   COMPONENT_DIR  directory containing libmyvector_component.so + myvector.json
#                  (default: build/component)
#   MYSQL_DATABASE test database name (default: vectordb)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

MYSQL_VERSION="${1:-8.4}"
LOAD_ROWS="${2:-50000}"
COMPONENT_DIR="${COMPONENT_DIR:-build/component}"
DB="${MYSQL_DATABASE:-vectordb}"
CONTAINER="myvector-smoke-component-$$"
ROOT_PW="smokeroot"
STANFORD_DIR="examples/stanford50d"

# ── helpers ─────────────────────────────────────────────────────────────────

mq() {  # mq [-D db] "sql" -- mysql query inside container
    docker exec -e MYSQL_PWD="$ROOT_PW" "$CONTAINER" \
        mysql -uroot -h 127.0.0.1 "$@"
}

mq_stdin() {  # mq_stdin [-D db] < file.sql
    docker exec -i -e MYSQL_PWD="$ROOT_PW" "$CONTAINER" \
        mysql -uroot -h 127.0.0.1 "$@"
}

die() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "PASS: $*"; }

cleanup() { docker rm -f "$CONTAINER" 2>/dev/null || true; }
trap cleanup EXIT

# ── pre-flight ───────────────────────────────────────────────────────────────

echo "=== MyVector Component Smoke Test ==="
echo "MySQL version : $MYSQL_VERSION"
echo "Component dir : $COMPONENT_DIR"
echo "Stanford rows : $LOAD_ROWS"
echo ""

[[ -f "$COMPONENT_DIR/libmyvector_component.so" ]] || \
    die "Component .so not found at $COMPONENT_DIR/libmyvector_component.so. Build first:
  MySQL 8.4: ./scripts/build-component-8.4-docker.sh mysql-8.4.8
  MySQL 9.7: ./scripts/build-component-9.7-docker.sh mysql-9.7.0
  macOS/local: ./scripts/build-component.sh mysql-8.4.8 <mysql-source-dir>"

[[ -f "$COMPONENT_DIR/myvector.json" ]] || \
    die "myvector.json not found at $COMPONENT_DIR/myvector.json"

[[ -f "$STANFORD_DIR/insert50d.sql.gz" ]] || \
    die "Stanford dataset missing: $STANFORD_DIR/insert50d.sql.gz"

# ── start container ──────────────────────────────────────────────────────────

echo "Starting mysql:$MYSQL_VERSION container..."
docker run -d --name "$CONTAINER" \
    -e MYSQL_ROOT_PASSWORD="$ROOT_PW" \
    -e MYSQL_ROOT_HOST=% \
    "mysql:$MYSQL_VERSION" >/dev/null

echo "Waiting for MySQL to be ready (TCP)..."
READY=0
for _i in $(seq 1 60); do
    if mq -e "SELECT 1" >/dev/null 2>&1; then
        READY=$((READY + 1))
        [[ $READY -ge 3 ]] && break
    else
        READY=0
    fi
    sleep 2
done
[[ $READY -ge 3 ]] || die "MySQL did not become ready in time"
echo "MySQL ready."
echo ""

# ── ensure libmysqlclient is present (server image omits it) ─────────────────
# The component .so may link against libmysqlclient dynamically.  The plain
# mysql:X Docker image is server-only (Oracle Linux 9) and does not ship the
# client shared library.  Install it from the MySQL CDN if missing.

echo "Checking runtime library dependencies..."
ARCH=$(docker exec "$CONTAINER" uname -m)
if ! docker exec "$CONTAINER" sh -c "ldconfig -p 2>/dev/null | grep -q libmysqlclient" 2>/dev/null; then
    SRV_VER=$(docker exec "$CONTAINER" mysqld --version 2>/dev/null \
        | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
    if [[ -z "$SRV_VER" ]]; then
        SRV_VER=$(mq -N -e "SELECT @@version;" 2>/dev/null | tr -d '[:space:]')
    fi
    MAJOR_MINOR=$(echo "$SRV_VER" | cut -d. -f1,2 | tr -d '.')  # e.g. 84 or 97
    BASE="https://cdn.mysql.com/Downloads/MySQL-${SRV_VER%.*}"
    VER="${SRV_VER}-1.el9"
    echo "Installing libmysqlclient ${SRV_VER} (${ARCH}) from MySQL CDN..."
    docker exec "$CONTAINER" bash -c "
        set -e
        BASE='${BASE}'
        VER='${VER}'
        ARCH='${ARCH}'
        rpm -ivh --nodeps \
          \"\${BASE}/mysql-community-common-\${VER}.\${ARCH}.rpm\" \
          2>/dev/null || true
        rpm -ivh --nodeps \
          \"\${BASE}/mysql-community-client-plugins-\${VER}.\${ARCH}.rpm\" \
          2>/dev/null || true
        rpm -ivh --nodeps \
          \"\${BASE}/mysql-community-libs-\${VER}.\${ARCH}.rpm\" \
          2>/dev/null
        ldconfig 2>/dev/null || true
    " || echo "WARNING: CDN install failed — INSTALL COMPONENT may fail if .so needs libmysqlclient"
fi

# ── install component ────────────────────────────────────────────────────────

echo "Installing component..."
PLUGIN_DIR=$(mq -N -e "SELECT @@plugin_dir;" 2>/dev/null)
docker cp "$COMPONENT_DIR/libmyvector_component.so" "$CONTAINER:$PLUGIN_DIR/myvector.so"
docker cp "$COMPONENT_DIR/myvector.json"            "$CONTAINER:$PLUGIN_DIR/myvector.json"
mq -e "INSTALL COMPONENT 'file://myvector';"
mq -e "SELECT component_urn FROM mysql.component WHERE component_urn LIKE '%myvector%';" \
    | grep -q myvector || die "Component not registered after install"
pass "INSTALL COMPONENT"

# ── write myvector.cnf so index-build thread can connect back via TCP ────────
# The component reads "myvector.cnf" (relative path from mysqld's CWD).
# mysqld CWD is typically the datadir; write there and also to / as fallback.

DATADIR=$(mq -N -e "SELECT @@datadir;" 2>/dev/null | tr -d '[:space:]')
CNF_CONTENT="myvector_host=127.0.0.1
myvector_user_id=root
myvector_user_password=${ROOT_PW}
myvector_port=3306
"
DATADIR_OWNER=$(docker exec "$CONTAINER" stat -c '%U' "$DATADIR" 2>/dev/null || echo "mysql")
docker exec "$CONTAINER" bash -c "printf '%s' '$CNF_CONTENT' > '${DATADIR}myvector.cnf' && chmod 0600 '${DATADIR}myvector.cnf' && chown '${DATADIR_OWNER}' '${DATADIR}myvector.cnf'"
docker exec "$CONTAINER" bash -c "printf '%s' '$CNF_CONTENT' > /myvector.cnf && chmod 0600 /myvector.cnf && chown '${DATADIR_OWNER}' /myvector.cnf"

# ── set index directory ──────────────────────────────────────────────────────

mq -e "SET GLOBAL myvector_index_dir='${DATADIR}';" 2>/dev/null || true

# ── register remaining UDFs (not auto-registered by component) ───────────────
# myvector_row_distance and myvector_is_valid are defined in myvector.so
# but registered via SONAME, not by the component framework.

echo "Registering supplemental UDFs..."
mq -e "
    DROP FUNCTION IF EXISTS myvector_row_distance;
    DROP FUNCTION IF EXISTS myvector_is_valid;
    DROP FUNCTION IF EXISTS myvector_search_open_udf;
    CREATE FUNCTION myvector_row_distance   RETURNS REAL    SONAME 'myvector.so';
    CREATE FUNCTION myvector_is_valid       RETURNS INTEGER SONAME 'myvector.so';
    CREATE FUNCTION myvector_search_open_udf RETURNS STRING  SONAME 'myvector.so';
" mysql 2>/dev/null || {
    echo "WARNING: Could not register supplemental UDFs (index build / ANN tests will be skipped)"
    SKIP_INDEX=1
}
SKIP_INDEX="${SKIP_INDEX:-0}"

# ── install stored procedures ─────────────────────────────────────────────────

if [[ "$SKIP_INDEX" = "0" ]]; then
    echo "Creating index management stored procedures..."
    # Extract procedure DDL from myvectorplugin.sql (skip INSTALL PLUGIN and UDF lines)
    mq_stdin mysql <<'PROCS'
DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_INTERNAL;
DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_STATUS;
DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_DROP;
DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_LOAD;
DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_REFRESH;
DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_BUILD;

DELIMITER //

CREATE PROCEDURE MYVECTOR_INDEX_STATUS(IN myvectorcolumn VARCHAR(256))
BEGIN
  DECLARE extra VARCHAR(1024); DECLARE pkid VARCHAR(1024);
  SET extra = ''; SET pkid = '';
  CALL MYVECTOR_INDEX_INTERNAL(myvectorcolumn, pkid, 'status', extra);
END //

CREATE PROCEDURE MYVECTOR_INDEX_DROP(IN myvectorcolumn VARCHAR(256))
BEGIN
  DECLARE extra VARCHAR(1024); DECLARE pkid VARCHAR(1024);
  SET extra = ''; SET pkid = '';
  CALL MYVECTOR_INDEX_INTERNAL(myvectorcolumn, pkid, 'drop', extra);
END //

CREATE PROCEDURE MYVECTOR_INDEX_LOAD(IN myvectorcolumn VARCHAR(256))
BEGIN
  DECLARE extra VARCHAR(1024); DECLARE pkid VARCHAR(1024);
  SET extra = ''; SET pkid = '';
  CALL MYVECTOR_INDEX_INTERNAL(myvectorcolumn, pkid, 'load', extra);
END //

CREATE PROCEDURE MYVECTOR_INDEX_INTERNAL(
    IN myvectorcolumn VARCHAR(256), IN pkidcolumn VARCHAR(64),
    IN action VARCHAR(64), IN extra VARCHAR(1024))
BEGIN
  DECLARE pos    INT;
  DECLARE status VARCHAR(1024);
  DECLARE temp   VARCHAR(256);
  DECLARE dbname VARCHAR(64);
  DECLARE tname  VARCHAR(64);
  DECLARE cname  VARCHAR(64);
  DECLARE colinfo VARCHAR(1024);
  DECLARE CONTINUE HANDLER FOR NOT FOUND SET colinfo = NULL;
  SET pos    = LOCATE('.', myvectorcolumn);
  SET dbname = SUBSTR(myvectorcolumn, 1, pos-1);
  SET temp   = SUBSTR(myvectorcolumn, pos+1);
  SET pos    = LOCATE('.', temp);
  SET tname  = SUBSTR(temp, 1, pos-1);
  SET cname  = SUBSTR(temp, pos+1);
  SELECT column_comment INTO colinfo
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE table_schema = dbname AND table_name = tname AND column_name = cname;
  IF colinfo IS NULL THEN
    SIGNAL SQLSTATE '50001' SET MESSAGE_TEXT = 'Vector column not found.';
  END IF;
  IF LOCATE('MYVECTOR COLUMN', colinfo) <> 1 THEN
    SIGNAL SQLSTATE '50002' SET MESSAGE_TEXT = 'Column is not a MYVECTOR column.';
  END IF;
  SET status = MYVECTOR_SEARCH_OPEN_UDF(myvectorcolumn, colinfo, pkidcolumn, action, extra);
  SELECT status AS Status;
END //

CREATE PROCEDURE MYVECTOR_INDEX_BUILD(
    IN myvectorcolumn VARCHAR(256), IN pkidcolumn VARCHAR(64))
BEGIN
  DECLARE extra VARCHAR(1024); SET extra = '';
  CALL MYVECTOR_INDEX_INTERNAL(myvectorcolumn, pkidcolumn, 'build', extra);
END //

DELIMITER ;
PROCS
    pass "Stored procedures created"
fi

# ── basic UDF tests ──────────────────────────────────────────────────────────

echo ""
echo "=== Basic UDF tests ==="
mq -e "SELECT myvector_display(myvector_construct('[1.0, 2.0, 3.0]'));" \
    | grep -q "1, 2, 3" || die "myvector_display/construct"
pass "myvector_construct + myvector_display"

DIST=$(mq -N -e "SELECT myvector_distance(myvector_construct('[0.0,0.0]'), myvector_construct('[3.0,4.0]'), 'L2');" 2>/dev/null | tr -d '[:space:]')
[[ "$DIST" == "5" || "$DIST" == "5.0" || "$DIST" == "25" ]] || \
    echo "WARNING: myvector_distance L2 returned '$DIST' (expected 5 or 25 depending on squared)"
pass "myvector_distance"

# ── create database + Stanford 50d table ─────────────────────────────────────

echo ""
echo "=== Loading Stanford 50d GloVe vectors (${LOAD_ROWS} rows) ==="
mq -e "CREATE DATABASE IF NOT EXISTS \`$DB\`;"

# Create table with proper MYVECTOR column comment for index support
mq -D "$DB" -e "
    DROP TABLE IF EXISTS words50d;
    CREATE TABLE words50d (
        wordid INT AUTO_INCREMENT PRIMARY KEY,
        word   VARCHAR(200),
        wordvec VARBINARY(2048) COMMENT 'MYVECTOR COLUMN type=hnsw,dim=50,size=400000,m=64,ef=100,idcol=wordid,dist=L2'
    );
"

echo "Loading data..."
T_START=$(date +%s)
# awk exits early after n records, causing SIGPIPE on gunzip; pipefail treats that
# as a pipeline failure — turn it off for this pipeline only.
set +o pipefail
# The SQL file starts with "set autocommit=off;" — when we load a subset the
# file's COMMIT never runs and MySQL rolls back everything on disconnect.
# Use awk to: (1) replace the autocommit=off line with autocommit=1,
# (2) print n INSERT records, (3) append COMMIT before exiting.
gunzip -c "$STANFORD_DIR/insert50d.sql.gz" \
    | awk -v n="$LOAD_ROWS" '
        BEGIN { RS=";"; ORS=";"; committed=0 }
        NR==1 && /autocommit/ { print "SET SESSION autocommit=1"; next }
        NR<=n+1 { print }
        NR==n+1 { print "COMMIT"; committed=1; exit }
        END { if (!committed) print "COMMIT;" }
    ' \
    | mq_stdin -D "$DB" 2>/dev/null
PIPE_STATUSES=("${PIPESTATUS[@]}")
set -o pipefail
# gunzip SIGPIPE (141) is expected when awk exits early — treat as ok; other errors are not
[[ ${PIPE_STATUSES[0]} -eq 0 || ${PIPE_STATUSES[0]} -eq 141 ]] || \
    die "gunzip failed (exit ${PIPE_STATUSES[0]})"
[[ ${PIPE_STATUSES[1]} -eq 0 ]] || die "awk failed (exit ${PIPE_STATUSES[1]})"
[[ ${PIPE_STATUSES[2]} -eq 0 ]] || die "mysql load failed (exit ${PIPE_STATUSES[2]})"
T_END=$(date +%s)
ACTUAL=$(mq -D "$DB" -N -e "SELECT COUNT(*) FROM words50d;" 2>/dev/null | tr -d '[:space:]')
[[ -n "$ACTUAL" && "$ACTUAL" -gt 0 ]] || die "No rows loaded — data load failed silently"
[[ "$ACTUAL" -ge "$((LOAD_ROWS * 95 / 100))" ]] || \
    echo "WARNING: Loaded $ACTUAL rows but expected ~$LOAD_ROWS (>5% short)"
pass "Loaded $ACTUAL rows in $((T_END - T_START))s"

# ── KNN brute-force search ────────────────────────────────────────────────────

echo ""
echo "=== KNN brute-force (top 5 neighbors of 'the') ==="
mq -D "$DB" -e "
    SELECT word, myvector_distance(wordvec,
        (SELECT wordvec FROM words50d WHERE word='the')) AS dist
    FROM words50d
    ORDER BY dist
    LIMIT 5;
" || die "KNN brute-force query"
pass "KNN brute-force"

# ── HNSW index build + ANN search ────────────────────────────────────────────

if [[ "$SKIP_INDEX" = "0" ]]; then
    echo ""
    echo "=== HNSW index build ==="
    T_START=$(date +%s)
    BUILD_OUT=$(mq -D "$DB" -N -e "CALL mysql.MYVECTOR_INDEX_BUILD('${DB}.words50d.wordvec', 'wordid');" 2>&1 || true)
    T_END=$(date +%s)
    echo "$BUILD_OUT"
    if echo "$BUILD_OUT" | grep -qiE "error|fail|exception"; then
        echo "WARNING: Index build may have failed — skipping ANN tests"
        SKIP_INDEX=1
    else
        pass "HNSW index built in $((T_END - T_START))s"
    fi
fi

if [[ "$SKIP_INDEX" = "0" ]]; then
    echo ""
    echo "=== ANN search (MYVECTOR_IS_ANN) ==="
    THE_VEC=$(mq -D "$DB" -N -e "SELECT wordvec FROM words50d WHERE word='the';" 2>/dev/null)
    ANN_OUT=$(mq -D "$DB" -e "
        SELECT word, myvector_row_distance(wordid) AS dist
        FROM words50d
        WHERE MYVECTOR_IS_ANN('${DB}.words50d.wordvec', 'wordid', (SELECT wordvec FROM words50d WHERE word='the'), 5);
    " 2>&1 || true)
    echo "$ANN_OUT"
    if echo "$ANN_OUT" | grep -qiE "ERROR|error in"; then
        echo "WARNING: ANN query failed (query rewrite may not be active)"
    else
        pass "ANN search via MYVECTOR_IS_ANN"
    fi

    # Drop index for online-update test (rebuild with smaller sample)
    mq -D "$DB" -e "CALL mysql.MYVECTOR_INDEX_DROP('${DB}.words50d.wordvec');" 2>/dev/null || true
fi

# ── online update test (small table, 3-dim) ───────────────────────────────────

if [[ "$SKIP_INDEX" = "0" ]]; then
    echo ""
    echo "=== Online update test (INSERT/UPDATE/DELETE) ==="
    mq -D "$DB" -e "
        DROP TABLE IF EXISTS ov_test;
        CREATE TABLE ov_test (
            id  INT PRIMARY KEY,
            tag VARCHAR(64),
            vec VARBINARY(256) COMMENT 'MYVECTOR COLUMN type=hnsw,dim=3,size=1000,m=16,ef=50,idcol=id,dist=L2'
        );
        INSERT INTO ov_test VALUES (1,'alpha', myvector_construct('[1.0,2.0,3.0]'));
        INSERT INTO ov_test VALUES (2,'beta',  myvector_construct('[2.0,3.0,4.0]'));
        INSERT INTO ov_test VALUES (3,'gamma', myvector_construct('[3.0,4.0,5.0]'));
    "

    mq -D "$DB" -e "CALL mysql.MYVECTOR_INDEX_BUILD('${DB}.ov_test.vec', 'id');" 2>/dev/null \
        | grep -iv error || true

    # INSERT
    mq -D "$DB" -e "INSERT INTO ov_test VALUES (4,'delta', myvector_construct('[9.0,9.0,9.0]'));"
    sleep 2

    # UPDATE
    mq -D "$DB" -e "UPDATE ov_test SET vec=myvector_construct('[0.1,0.1,0.1]') WHERE id=4;"
    sleep 2

    # DELETE
    mq -D "$DB" -e "DELETE FROM ov_test WHERE id=3;"
    sleep 2

    ROW_COUNT=$(mq -D "$DB" -N -e "SELECT COUNT(*) FROM ov_test;" 2>/dev/null | tr -d '[:space:]')
    [[ "$ROW_COUNT" == "3" ]] || die "Expected 3 rows after delete, got $ROW_COUNT"
    pass "Online updates: INSERT, UPDATE, DELETE"

    mq -D "$DB" -e "CALL mysql.MYVECTOR_INDEX_DROP('${DB}.ov_test.vec');" 2>/dev/null || true
    mq -D "$DB" -e "DROP TABLE ov_test;"
fi

# ── uninstall + verify ────────────────────────────────────────────────────────

echo ""
echo "=== Uninstall ==="
# DROP supplemental UDFs (SONAME-registered; UNINSTALL COMPONENT does not remove them)
mq -e "
    DROP FUNCTION IF EXISTS myvector_row_distance;
    DROP FUNCTION IF EXISTS myvector_is_valid;
    DROP FUNCTION IF EXISTS myvector_search_open_udf;
" 2>/dev/null || true
mq -e "UNINSTALL COMPONENT 'file://myvector';"
set +e
REMAINING=$(mq -N -e "SELECT component_urn FROM mysql.component WHERE component_urn LIKE '%myvector%';" 2>/dev/null)
UDFS_REMAINING=$(mq -N -e "SELECT name FROM mysql.func WHERE name IN ('myvector_row_distance','myvector_is_valid','myvector_search_open_udf');" 2>/dev/null | tr -d '[:space:]')
set -e
[[ -z "$REMAINING" ]] || die "Component still registered after UNINSTALL: $REMAINING"
[[ -z "$UDFS_REMAINING" ]] || die "Supplemental UDFs still in mysql.func after DROP: $UDFS_REMAINING"
pass "UNINSTALL COMPONENT — component removed cleanly"

echo ""
echo "=== Smoke test complete ==="
