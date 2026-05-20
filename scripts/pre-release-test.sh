#!/usr/bin/env bash
# Pre-release gate: smoke + RFC-004 + edge cases for MySQL 8.4 and 9.7.
# Run before tagging a release. Exit 0 = safe to tag. Exit 1 = do not tag.
#
# Usage:
#   ./scripts/pre-release-test.sh          # both 8.4 and 9.7
#   ./scripts/pre-release-test.sh 8.4      # 8.4 only
#   ./scripts/pre-release-test.sh 9.7      # 9.7 only
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

VERSION_ARG="${1:-all}"
case "$VERSION_ARG" in
  8.4)  VERSIONS=("8.4") ;;
  9.7)  VERSIONS=("9.7") ;;
  all)  VERSIONS=("8.4" "9.7") ;;
  *)    echo "Usage: $0 [8.4|9.7]" >&2; exit 1 ;;
esac

declare -A COMPONENT_DIRS=(
  ["8.4"]="dist/component-8.4"
  ["9.7"]="dist/component-9.7"
)

# ── counters ──────────────────────────────────────────────────────────────────
PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0

pass() { echo "  PASS: $*"; ((PASS_COUNT++)) || true; }
fail() { echo "  FAIL: $*" >&2; ((FAIL_COUNT++)) || true; }
skip() { echo "  SKIP: $*"; ((SKIP_COUNT++)) || true; }
die()  { echo "ERROR: $*" >&2; exit 1; }

print_summary() {
  echo ""
  echo "=== Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed, ${SKIP_COUNT} skipped ==="
  [[ "$FAIL_COUNT" -eq 0 ]]
}
trap print_summary EXIT

echo "=== MyVector Pre-Release Test Suite ==="
echo "MySQL versions : ${VERSIONS[*]}"
for VER in "${VERSIONS[@]}"; do
  echo "Component dir  : ${COMPONENT_DIRS[$VER]}"
done
echo ""

# ── pre-flight ────────────────────────────────────────────────────────────────
for VER in "${VERSIONS[@]}"; do
  DIR="${COMPONENT_DIRS[$VER]}"
  if [[ ! -f "$DIR/libmyvector_component.so" ]]; then
    die "Artifact missing: $DIR/libmyvector_component.so
Build it first:
  MySQL 8.4: ./scripts/build-component-8.4-docker.sh mysql-8.4.8 dist/component-8.4
  MySQL 9.7: ./scripts/build-component-9.7-docker.sh mysql-9.7.0 dist/component-9.7"
  fi
done

# ── Phase 1: smoke (happy path) ───────────────────────────────────────────────
for VER in "${VERSIONS[@]}"; do
  DIR="${COMPONENT_DIRS[$VER]}"
  echo "--- Phase 1 Smoke ($VER) ---"
  COMPONENT_DIR="$DIR" bash scripts/smoke-component.sh "$VER" 50000 \
    || die "Phase 1 smoke failed for MySQL $VER — fix before proceeding"
  echo ""
done

# ── Phase 2 helpers ───────────────────────────────────────────────────────────
CONTAINER=""
ROOT_PW="prerelroot"

mq() {
  docker exec -e MYSQL_PWD="$ROOT_PW" "$CONTAINER" \
    mysql -uroot -h 127.0.0.1 "$@"
}

mq_stdin() {
  docker exec -i -e MYSQL_PWD="$ROOT_PW" "$CONTAINER" \
    mysql -uroot -h 127.0.0.1 "$@"
}

cleanup_container() {
  [[ -n "$CONTAINER" ]] && docker rm -f "$CONTAINER" 2>/dev/null || true
  CONTAINER=""
}

start_container() {
  local VER="$1"
  CONTAINER="myvector-prerelease-$$-${VER//./}"
  docker run -d --name "$CONTAINER" \
    -e MYSQL_ROOT_PASSWORD="$ROOT_PW" \
    -e MYSQL_ROOT_HOST=% \
    "mysql:$VER" >/dev/null

  local READY=0
  for _i in $(seq 1 60); do
    if mq -e "SELECT 1" >/dev/null 2>&1; then
      ((READY++)) || true
      [[ $READY -ge 3 ]] && break
    else
      READY=0
    fi
    sleep 2
  done
  [[ $READY -ge 3 ]] || die "MySQL $VER container did not become ready"
  echo "  MySQL $VER ready."
}

install_component() {
  local COMP_DIR="$1"
  local PLUGIN_DIR
  PLUGIN_DIR=$(mq -N -e "SELECT @@plugin_dir;" 2>/dev/null | tr -d '[:space:]')

  # Install libmysqlclient if server image omits it
  if ! docker exec "$CONTAINER" sh -c "ldconfig -p 2>/dev/null | grep -q libmysqlclient" 2>/dev/null; then
    local SRV_VER ARCH BASE VER_RPM
    SRV_VER=$(mq -N -e "SELECT @@version;" 2>/dev/null | tr -d '[:space:]')
    ARCH=$(docker exec "$CONTAINER" uname -m)
    BASE="https://cdn.mysql.com/Downloads/MySQL-${SRV_VER%.*}"
    VER_RPM="${SRV_VER}-1.el9"
    docker exec "$CONTAINER" bash -c "
      rpm -ivh --nodeps '${BASE}/mysql-community-common-${VER_RPM}.${ARCH}.rpm' 2>/dev/null || true
      rpm -ivh --nodeps '${BASE}/mysql-community-client-plugins-${VER_RPM}.${ARCH}.rpm' 2>/dev/null || true
      rpm -ivh --nodeps '${BASE}/mysql-community-libs-${VER_RPM}.${ARCH}.rpm' 2>/dev/null
      ldconfig 2>/dev/null || true
    " || echo "  WARNING: libmysqlclient CDN install failed"
  fi

  docker cp "$COMP_DIR/libmyvector_component.so" "$CONTAINER:$PLUGIN_DIR/myvector.so"
  docker cp "$COMP_DIR/myvector.json"            "$CONTAINER:$PLUGIN_DIR/myvector.json"
  mq -e "INSTALL COMPONENT 'file://myvector';"

  local DATADIR
  DATADIR=$(mq -N -e "SELECT @@datadir;" 2>/dev/null | tr -d '[:space:]')
  local OWNER
  OWNER=$(docker exec "$CONTAINER" stat -c '%U' "$DATADIR" 2>/dev/null || echo "mysql")
  local CNF
  CNF="myvector_host=127.0.0.1
myvector_user_id=root
myvector_user_password=${ROOT_PW}
myvector_port=3306
"
  docker exec "$CONTAINER" bash -c "
    printf '%s' '$CNF' > '${DATADIR}myvector.cnf'
    chmod 0600 '${DATADIR}myvector.cnf' && chown '${OWNER}' '${DATADIR}myvector.cnf'
    printf '%s' '$CNF' > /myvector.cnf
    chmod 0600 /myvector.cnf && chown '${OWNER}' /myvector.cnf
  "
  mq -e "SET GLOBAL myvector_index_dir='${DATADIR}';" 2>/dev/null || true

  mq -e "
    DROP FUNCTION IF EXISTS myvector_row_distance;
    DROP FUNCTION IF EXISTS myvector_is_valid;
    DROP FUNCTION IF EXISTS myvector_search_open_udf;
    CREATE FUNCTION myvector_row_distance    RETURNS REAL    SONAME 'myvector.so';
    CREATE FUNCTION myvector_is_valid        RETURNS INTEGER SONAME 'myvector.so';
    CREATE FUNCTION myvector_search_open_udf RETURNS STRING  SONAME 'myvector.so';
  " mysql 2>/dev/null

  install_procs
}

install_procs() {
  mq_stdin mysql <<'PROCS'
DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_INTERNAL;
DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_STATUS;
DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_DROP;
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

CREATE PROCEDURE MYVECTOR_INDEX_INTERNAL(
    IN myvectorcolumn VARCHAR(256), IN pkidcolumn VARCHAR(64),
    IN action VARCHAR(64), IN extra VARCHAR(1024))
BEGIN
  DECLARE pos    INT; DECLARE status VARCHAR(1024);
  DECLARE temp   VARCHAR(256); DECLARE dbname VARCHAR(64);
  DECLARE tname  VARCHAR(64); DECLARE cname  VARCHAR(64);
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
}

run_rfc004_zero_vector() {
  echo "  [RFC-004] Zero-vector rejection"
  mq -e "CREATE DATABASE IF NOT EXISTS prerel;"

  # Setup: cosine table with one valid row and one zero-magnitude row
  mq -D prerel -e "
    DROP TABLE IF EXISTS cosine_t;
    CREATE TABLE cosine_t (
      id  INT PRIMARY KEY,
      vec VARBINARY(256) COMMENT 'MYVECTOR COLUMN type=hnsw,dim=3,size=100,m=16,ef=50,idcol=id,dist=cosine'
    );
    INSERT INTO cosine_t VALUES (1, myvector_construct('[1.0,0.0,0.0]'));
    INSERT INTO cosine_t VALUES (2, myvector_construct('[0.0,0.0,0.0]'));
  " 2>/dev/null

  # Test 1: index build on cosine table with zero vector should fail
  BUILD_COSINE=$(mq -D prerel -e \
    "CALL mysql.MYVECTOR_INDEX_BUILD('prerel.cosine_t.vec', 'id');" 2>&1 || true)
  if echo "$BUILD_COSINE" | grep -qiE "ERROR|zero|invalid|reject|magnitude"; then
    pass "zero-vector rejected on cosine index (build failed as expected)"
  else
    fail "zero-vector on cosine: expected rejection, got: $BUILD_COSINE"
  fi

  # Test 2: L2 index with zero vector should succeed
  mq -D prerel -e "
    DROP TABLE IF EXISTS l2_t;
    CREATE TABLE l2_t (
      id  INT PRIMARY KEY,
      vec VARBINARY(256) COMMENT 'MYVECTOR COLUMN type=hnsw,dim=3,size=100,m=16,ef=50,idcol=id,dist=L2'
    );
    INSERT INTO l2_t VALUES (1, myvector_construct('[0.0,0.0,0.0]'));
    INSERT INTO l2_t VALUES (2, myvector_construct('[1.0,0.0,0.0]'));
  " 2>/dev/null
  BUILD_L2=$(mq -D prerel -e \
    "CALL mysql.MYVECTOR_INDEX_BUILD('prerel.l2_t.vec', 'id');" 2>&1 || true)
  if echo "$BUILD_L2" | grep -qE "^ERROR [0-9]"; then
    fail "zero-vector on L2: expected success, got error: $BUILD_L2"
  else
    pass "zero-vector accepted on L2 index (not rejected)"
  fi

  # Test 3: valid non-zero vectors on cosine index should succeed
  mq -D prerel -e "
    DROP TABLE IF EXISTS cosine_ok;
    CREATE TABLE cosine_ok (
      id  INT PRIMARY KEY,
      vec VARBINARY(256) COMMENT 'MYVECTOR COLUMN type=hnsw,dim=3,size=100,m=16,ef=50,idcol=id,dist=cosine'
    );
    INSERT INTO cosine_ok VALUES (1, myvector_construct('[1.0,0.0,0.0]'));
    INSERT INTO cosine_ok VALUES (2, myvector_construct('[0.0,1.0,0.0]'));
  " 2>/dev/null
  BUILD_OK=$(mq -D prerel -e \
    "CALL mysql.MYVECTOR_INDEX_BUILD('prerel.cosine_ok.vec', 'id');" 2>&1 || true)
  if echo "$BUILD_OK" | grep -qE "^ERROR [0-9]"; then
    fail "valid cosine index build: expected success, got: $BUILD_OK"
  else
    pass "valid (non-zero) vectors accepted on cosine index"
  fi
}

run_rfc004_max_dim() {
  echo "  [RFC-004] myvector_max_vector_dim sysvar"

  # Test 1: default is 4096
  MAX_DIM=$(mq -N -e "SELECT @@myvector_max_vector_dim;" 2>/dev/null | tr -d '[:space:]')
  if [[ "$MAX_DIM" == "4096" ]]; then
    pass "myvector_max_vector_dim default is 4096"
  else
    fail "myvector_max_vector_dim default: expected 4096, got '$MAX_DIM'"
  fi

  # Test 2: read-only — SET GLOBAL must fail
  SET_OUT=$(mq -e "SET GLOBAL myvector_max_vector_dim = 8192;" 2>&1 || true)
  if echo "$SET_OUT" | grep -qiE "read.only|read only|ERROR"; then
    pass "myvector_max_vector_dim is read-only (SET GLOBAL rejected)"
  else
    fail "myvector_max_vector_dim should be read-only, SET GLOBAL did not error: $SET_OUT"
  fi

  # Test 3: building an index with dim=4097 must fail when limit is 4096
  VEC_4097=$(awk 'BEGIN{printf "["; for(i=1;i<=4097;i++) printf (i>1?",":"") "1.0"; printf "]"}')
  mq -D prerel -e "
    DROP TABLE IF EXISTS bigdim_t;
    CREATE TABLE bigdim_t (
      id  INT PRIMARY KEY,
      vec VARBINARY(17000)
        COMMENT 'MYVECTOR COLUMN type=hnsw,dim=4097,size=10,m=16,ef=50,idcol=id,dist=L2'
    );
    INSERT INTO bigdim_t VALUES (1, myvector_construct('${VEC_4097}'));
  " 2>/dev/null || true
  BUILD_BIG=$(mq -D prerel -e \
    "CALL mysql.MYVECTOR_INDEX_BUILD('prerel.bigdim_t.vec', 'id');" 2>&1 || true)
  if echo "$BUILD_BIG" | grep -qiE "ERROR|dimension|max|exceed|invalid|too large"; then
    pass "4097-dim index build rejected when myvector_max_vector_dim=4096"
  else
    fail "4097-dim index build: expected rejection at default limit, got: $BUILD_BIG"
  fi
}

run_rfc004_crash_injection() {
  echo "  [RFC-004] Crash injection"
  # Detect debug build: SET SESSION debug='' succeeds only in debug builds
  if mq -e "SET SESSION debug='';" >/dev/null 2>&1; then
    # Debug build: trigger crash injection during index flush
    mq -D prerel -e "
      DROP TABLE IF EXISTS crash_t;
      CREATE TABLE crash_t (
        id  INT PRIMARY KEY,
        vec VARBINARY(256)
          COMMENT 'MYVECTOR COLUMN type=hnsw,dim=3,size=100,m=16,ef=50,idcol=id,dist=L2'
      );
      INSERT INTO crash_t VALUES (1, myvector_construct('[1.0,2.0,3.0]'));
    " 2>/dev/null
    mq -e "SET SESSION debug='+d,simulate_vector_crash';" 2>/dev/null || true
    mq -D prerel -e \
      "CALL mysql.MYVECTOR_INDEX_BUILD('prerel.crash_t.vec', 'id');" 2>/dev/null || true
    sleep 3
    if ! docker inspect "$CONTAINER" --format '{{.State.Running}}' 2>/dev/null | grep -q "^true$"; then
      pass "crash injection: server aborted as expected (simulate_vector_crash)"
    else
      fail "crash injection: server still running after simulate_vector_crash (debug hook may be missing)"
    fi
  else
    skip "crash injection (release build — requires -DWITH_DEBUG=1)"
  fi
}

run_edge_cases() {
  echo "  [Edge cases]"

  # NULL input
  NULL_OUT=$(mq -N -e "SELECT myvector_construct(NULL);" 2>/dev/null | tr -d '[:space:]')
  if [[ -z "$NULL_OUT" || "$NULL_OUT" == "NULL" ]]; then
    pass "myvector_construct(NULL) returns NULL"
  else
    fail "myvector_construct(NULL): expected NULL, got '$NULL_OUT'"
  fi

  # Non-array JSON (must not return a valid binary vector)
  NONARR=$(mq -N -e "SELECT myvector_construct('{\"a\":1}');" 2>/dev/null | tr -d '[:space:]')
  if [[ -z "$NONARR" || "$NONARR" == "NULL" ]]; then
    pass "myvector_construct(non-array JSON) returns NULL"
  else
    fail "myvector_construct(non-array JSON): expected NULL, got '$NONARR'"
  fi

  # Empty array (must not return a valid binary vector)
  EMPTY=$(mq -N -e "SELECT myvector_construct('[]');" 2>/dev/null | tr -d '[:space:]')
  if [[ -z "$EMPTY" || "$EMPTY" == "NULL" ]]; then
    pass "myvector_construct([]) returns NULL"
  else
    fail "myvector_construct([]): expected NULL, got '$EMPTY'"
  fi

  # Dimension mismatch: 5d vector in 3d index should fail at build
  mq -D prerel -e "
    DROP TABLE IF EXISTS mismatch_t;
    CREATE TABLE mismatch_t (
      id  INT PRIMARY KEY,
      vec VARBINARY(256)
        COMMENT 'MYVECTOR COLUMN type=hnsw,dim=3,size=100,m=16,ef=50,idcol=id,dist=L2'
    );
    INSERT INTO mismatch_t VALUES (1, myvector_construct('[1.0,2.0,3.0]'));
    INSERT INTO mismatch_t VALUES (2, myvector_construct('[1.0,2.0,3.0,4.0,5.0]'));
  " 2>/dev/null || true
  DIM_BUILD=$(mq -D prerel -e \
    "CALL mysql.MYVECTOR_INDEX_BUILD('prerel.mismatch_t.vec', 'id');" 2>&1 || true)
  if echo "$DIM_BUILD" | grep -qiE "ERROR|dimension|mismatch|invalid"; then
    pass "dimension mismatch: index build rejected mismatched vector"
  else
    fail "dimension mismatch: expected error, got: $DIM_BUILD"
  fi

  # myvector_distance with mismatched dims (must not return a numeric distance)
  DIST_MM=$(mq -N -e "
    SELECT myvector_distance(
      myvector_construct('[1.0,0.0]'),
      myvector_construct('[1.0,0.0,0.0]')
    );" 2>/dev/null | tr -d '[:space:]')
  if [[ -z "$DIST_MM" || "$DIST_MM" == "NULL" ]]; then
    pass "myvector_distance(dim_mismatch) returns NULL"
  else
    fail "myvector_distance(dim_mismatch): expected NULL, got '$DIST_MM'"
  fi

  # myvector_is_valid with wrong dimension arg → 0
  ISVALID=$(mq -N -e \
    "SELECT myvector_is_valid(myvector_construct('[1.0,2.0,3.0]'), 5);" \
    2>/dev/null | tr -d '[:space:]')
  if [[ "$ISVALID" == "0" ]]; then
    pass "myvector_is_valid(3d_vec, dim=5) returns 0"
  else
    fail "myvector_is_valid(3d_vec, dim=5): expected 0, got '$ISVALID'"
  fi
}

# ── Phase 2: RFC-004 + edge cases ────────────────────────────────────────────
for VER in "${VERSIONS[@]}"; do
  DIR="${COMPONENT_DIRS[$VER]}"
  echo "--- Phase 2 RFC-004 + Edge Cases ($VER) ---"
  cleanup_container
  start_container "$VER"
  install_component "$DIR"
  mq -e "CREATE DATABASE IF NOT EXISTS prerel;" 2>/dev/null || true

  run_rfc004_zero_vector
  run_rfc004_max_dim
  run_rfc004_crash_injection
  run_edge_cases

  cleanup_container
  echo ""
done
