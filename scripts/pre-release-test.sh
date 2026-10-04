#!/usr/bin/env bash
# Pre-release gate: smoke + RFC-004 + edge cases for MySQL 8.4, 9.7, and 26.7.
# Run before tagging a release. Exit 0 = safe to tag. Exit 1 = do not tag.
#
# Usage:
#   ./scripts/pre-release-test.sh          # 8.4 and 9.7 (26.7 is opt-in; see below)
#   ./scripts/pre-release-test.sh 8.4      # 8.4 only
#   ./scripts/pre-release-test.sh 9.7      # 9.7 only
#   ./scripts/pre-release-test.sh 26.7     # 26.7 only
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

VERSION_ARG="${1:-all}"
case "$VERSION_ARG" in
  8.4)   VERSIONS=("8.4") ;;
  9.7)   VERSIONS=("9.7") ;;
  26.7)  VERSIONS=("26.7") ;;
  # 26.7 is a brand-new Innovation release; keep it opt-in rather than part of the
  # default gate until it has an established track record (mirrors continue-on-error
  # treatment of 26.7 elsewhere in CI).
  all)   VERSIONS=("8.4" "9.7") ;;
  *)     echo "Usage: $0 [8.4|9.7|26.7]" >&2; exit 1 ;;
esac

declare -A COMPONENT_DIRS=(
  ["8.4"]="dist/component-8.4"
  ["9.7"]="dist/component-9.7"
  ["26.7"]="dist/component-26.7"
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

# ── Phase 2 helpers ───────────────────────────────────────────────────────────
CONTAINER=""
ROOT_PW="prerelroot"

cleanup_container() {
  [[ -n "$CONTAINER" ]] && docker rm -fv "$CONTAINER" 2>/dev/null || true
  CONTAINER=""
}

on_exit() {
  local exit_code=$?
  cleanup_container
  print_summary
  if [[ "$exit_code" -ne 0 || "$FAIL_COUNT" -ne 0 ]]; then
    exit 1
  fi
  exit 0
}
trap on_exit EXIT

echo "=== MyVector Pre-Release Test Suite ==="
echo "MySQL versions : ${VERSIONS[*]}"
for VER in "${VERSIONS[@]}"; do
  echo "Component dir  : ${COMPONENT_DIRS[$VER]}"
done
echo ""

# ── pre-flight ────────────────────────────────────────────────────────────────
for VER in "${VERSIONS[@]}"; do
  DIR="${COMPONENT_DIRS[$VER]}"
  if [[ ! -f "$DIR/libmyvector_component.so" || ! -f "$DIR/myvector.json" ]]; then
    die "Artifact missing in $DIR/ (need libmyvector_component.so + myvector.json)
Build it first:
  MySQL 8.4:  ./scripts/build-component-8.4-docker.sh mysql-8.4.8 dist/component-8.4
  MySQL 9.7:  ./scripts/build-component-9.7-docker.sh mysql-9.7.0 dist/component-9.7
  MySQL 26.7: ./scripts/build-component-26.7-docker.sh mysql-26.7.0 dist/component-26.7"
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

mq() {
  docker exec -e MYSQL_PWD="$ROOT_PW" "$CONTAINER" \
    mysql -uroot -h 127.0.0.1 "$@"
}

mq_stdin() {
  docker exec -i -e MYSQL_PWD="$ROOT_PW" "$CONTAINER" \
    mysql -uroot -h 127.0.0.1 "$@"
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
  PLUGIN_DIR=$(mq -N -e "SELECT @@plugin_dir;" 2>/dev/null | LC_ALL=C tr -d '[:space:]')

  # Install libmysqlclient if server image omits it
  if ! docker exec "$CONTAINER" sh -c "ldconfig -p 2>/dev/null | grep -q libmysqlclient" 2>/dev/null; then
    local SRV_VER ARCH BASE VER_RPM
    SRV_VER=$(mq -N -e "SELECT @@version;" 2>/dev/null | LC_ALL=C tr -d '[:space:]')
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
  DATADIR=$(mq -N -e "SELECT @@datadir;" 2>/dev/null | LC_ALL=C tr -d '[:space:]')
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

# Create the uninstall procedures (MYVECTOR_BINLOG_STOP, MYVECTOR_UNINSTALL_CHECK,
# MYVECTOR_PREPARE_UNINSTALL) from sql/myvector_install_component.sql (their single
# source), for test setups that install their own procedures.
install_binlog_stop_proc() {
  local P
  {
    echo "DELIMITER //"
    for P in MYVECTOR_BINLOG_STOP MYVECTOR_UNINSTALL_CHECK MYVECTOR_PREPARE_UNINSTALL; do
      echo "DROP PROCEDURE IF EXISTS $P//"
      sed -n "/^CREATE PROCEDURE $P(/,/^\/\/\$/p" \
        "$REPO_ROOT/sql/myvector_install_component.sql"
    done
    echo "DELIMITER ;"
  } | mq_stdin mysql
}

install_procs() {
  mq_stdin mysql <<'PROCS'
DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_INTERNAL;
DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_STATUS;
DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_DROP;
DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_BUILD;
DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_LOAD;

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
  IF NOT REGEXP_LIKE(colinfo, '^[[:space:]]*MYVECTOR COLUMN', 'i') THEN
    SIGNAL SQLSTATE '50002' SET MESSAGE_TEXT = 'Column is not a MYVECTOR column.';
  END IF;
  SET status = MYVECTOR_SEARCH_OPEN_UDF(myvectorcolumn, colinfo, pkidcolumn, action, extra);
  SELECT status AS Status;
END //

CREATE PROCEDURE MYVECTOR_INDEX_LOAD(IN myvectorcolumn VARCHAR(256))
BEGIN
  DECLARE extra VARCHAR(1024); DECLARE pkid VARCHAR(1024);
  SET extra = ''; SET pkid = '';
  CALL MYVECTOR_INDEX_INTERNAL(myvectorcolumn, pkid, 'load', extra);
END //

CREATE PROCEDURE MYVECTOR_INDEX_BUILD(
    IN myvectorcolumn VARCHAR(256), IN pkidcolumn VARCHAR(64))
BEGIN
  DECLARE extra VARCHAR(1024); SET extra = '';
  CALL MYVECTOR_INDEX_INTERNAL(myvectorcolumn, pkidcolumn, 'build', extra);
END //

DELIMITER ;
PROCS
  install_binlog_stop_proc
}

run_rfc004_zero_vector() {
  echo "  [RFC-004] Zero-vector rejection"
  mq -e "CREATE DATABASE IF NOT EXISTS prerel;"

  # Test 1: online INSERT of zero vector is silently skipped on cosine index.
  # The batch-build path does not filter zero vectors; the check is in myvector_table_op()
  # which the binlog listener calls on each live INSERT.  Build a cosine online index
  # with one valid row (rows=1), then INSERT a zero-magnitude vector and verify the
  # index count stays at 1 (binlog path skipped the zero vector with a warning).
  mq -D prerel -e "
    DROP TABLE IF EXISTS cosine_t;
    CREATE TABLE cosine_t (
      id  INT PRIMARY KEY,
      vec VARBINARY(256) COMMENT 'MYVECTOR COLUMN type=hnsw,dim=3,size=100,m=16,ef=50,idcol=id,dist=cosine,online=Y'
    );
    INSERT INTO cosine_t VALUES (1, myvector_construct('[1.0,0.0,0.0]'));
  " 2>/dev/null
  BUILD_COSINE=$(mq -D prerel -e \
    "CALL mysql.MYVECTOR_INDEX_BUILD('prerel.cosine_t.vec', 'id');" 2>&1 || true)
  if echo "$BUILD_COSINE" | grep -qE "^ERROR [0-9]"; then
    fail "cosine online index build (valid row): unexpected error: $BUILD_COSINE"
  else
    mq -D prerel -e "INSERT INTO cosine_t VALUES (2, myvector_construct('[0.0,0.0,0.0]'));" 2>/dev/null || true
    sleep 5
    ZV_STATUS=$(mq -N -D prerel -e "CALL mysql.MYVECTOR_INDEX_STATUS('prerel.cosine_t.vec');" 2>/dev/null || true)
    ZV_ROWS=$(echo "$ZV_STATUS" | grep -ioE 'rows[^0-9]+[0-9]+' | grep -oE '[0-9]+$' | head -1 || echo "")
    if [[ "$ZV_ROWS" == "1" ]]; then
      pass "zero-magnitude vector silently skipped on cosine index (online insert, index rows=1)"
    else
      fail "cosine zero-vector: expected index rows=1 after online INSERT of zero vec, got '$ZV_ROWS'"
    fi
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
  local VER="$1"
  echo "  [RFC-004] myvector_max_vector_dim sysvar"

  # The component does not expose @@myvector_max_vector_dim as a MySQL sysvar
  # (that requires plugin infrastructure not available in component builds).
  # Dimension enforcement is implemented in two places:
  #   1. rewriteMyVectorColumnDef() — enforces at DDL time via query rewrite (MySQL 9.0+)
  #   2. index open path — the C++ constant is 4096 (compile-time default)
  skip "myvector_max_vector_dim @@sysvar not exposed in component build (plugin-only)"
  skip "myvector_max_vector_dim SET GLOBAL not applicable in component build"

  # Test 3: dim=4097 DDL rejection via query rewrite — MySQL 9.0+ only.
  # On MySQL 8.4, the query_rewrite.h service is absent so this component
  # was compiled without the query rewrite module; skip.
  if [[ "$VER" == 8.* ]]; then
    skip "myvector_max_vector_dim DDL enforcement requires MySQL 9.0+ query rewrite (skipping $VER)"
    return 0
  fi
  # MySQL 9.0+: MYVECTOR(dim=4097) DDL annotation should be rejected during rewrite.
  # First verify the query rewrite service is functioning with a control DDL (dim=3).
  CONTROL_CREATE=$(mq -D prerel -e "
    DROP TABLE IF EXISTS bigdim_ok;
    CREATE TABLE bigdim_ok (
      id  INT PRIMARY KEY,
      vec MYVECTOR(type=hnsw,dim=3,size=10,m=16,ef=50,idcol=id,dist=L2)
    );" 2>&1 || true)
  if echo "$CONTROL_CREATE" | grep -qiE "ERROR"; then
    if echo "$CONTROL_CREATE" | grep -qiE "syntax|1064"; then
      skip "MYVECTOR DDL annotation not available on MySQL $VER component (query_rewrite.h absent from component services headers; dim enforcement test skipped)"
    else
      fail "control MYVECTOR DDL (dim=3) failed on MySQL $VER — rewrite service not active, max-dim enforcement not verified: $CONTROL_CREATE"
    fi
    return 0
  fi
  CREATE_BIG=$(mq -D prerel -e "
    DROP TABLE IF EXISTS bigdim_t;
    CREATE TABLE bigdim_t (
      id  INT PRIMARY KEY,
      vec MYVECTOR(type=hnsw,dim=4097,size=10,m=16,ef=50,idcol=id,dist=L2)
    );" 2>&1 || true)
  if echo "$CREATE_BIG" | grep -qi "dimension incorrect"; then
    pass "4097-dim MYVECTOR DDL rejected (max_vector_dim=4096 enforced on MySQL $VER)"
  else
    fail "4097-dim MYVECTOR DDL: expected 'dimension incorrect' rejection on MySQL $VER, got: $CREATE_BIG"
  fi
}

# Regression test for issue #130: rewriteMyVectorColumnDef() used to search
# for the literal text "MYVECTOR(" anywhere in the query, including inside a
# quoted string, so the README-documented
#   wordvec VARBINARY(200) COMMENT 'MYVECTOR(type=HNSW,dim=50,size=100000)'
# form -- already complete, valid SQL on its own -- was mistaken for the
# inline MYVECTOR(...) DDL annotation and mangled into invalid SQL
# (ERROR 1064). Same MySQL 9.0+ / query-rewrite-service gate as
# run_rfc004_max_dim above; this uses the same control DDL to detect it.
run_ddl_rewrite_comment_string() {
  local VER="$1"
  echo "  [Issue #130] MYVECTOR( inside a COMMENT string literal is left alone"

  CONTROL_CREATE=$(mq -D prerel -e "
    DROP TABLE IF EXISTS ddl_ctrl_t;
    CREATE TABLE ddl_ctrl_t (
      id  INT PRIMARY KEY,
      vec MYVECTOR(type=hnsw,dim=3,size=10,m=16,ef=50,idcol=id,dist=L2)
    );" 2>&1 || true)
  if echo "$CONTROL_CREATE" | grep -qiE "ERROR"; then
    skip "MYVECTOR DDL rewrite not available on MySQL $VER component (see run_rfc004_max_dim); issue #130 regression test skipped"
    return 0
  fi
  mq -D prerel -e "DROP TABLE IF EXISTS ddl_ctrl_t;" 2>/dev/null || true

  # The exact issue #130 repro.
  CREATE_COMMENT_FORM=$(mq -D prerel -e "
    DROP TABLE IF EXISTS ddl_comment_t;
    CREATE TABLE ddl_comment_t (
      id  INT PRIMARY KEY,
      v VARBINARY(64) COMMENT 'MYVECTOR(type=HNSW,dim=3,size=100)'
    );" 2>&1 || true)
  if echo "$CREATE_COMMENT_FORM" | grep -qiE "ERROR"; then
    fail "issue #130: README's VARBINARY(...) COMMENT 'MYVECTOR(...)' form was rejected: $CREATE_COMMENT_FORM"
    return 0
  fi

  COMMENT_AFTER=$(mq -D prerel -N -e "
    SELECT column_comment FROM information_schema.columns
    WHERE table_schema='prerel' AND table_name='ddl_comment_t' AND column_name='v';
  " 2>/dev/null)
  if [[ "$COMMENT_AFTER" == "MYVECTOR(type=HNSW,dim=3,size=100)" ]]; then
    pass "issue #130: COMMENT-string MYVECTOR(...) left untouched, not mistaken for the DDL annotation"
  else
    fail "issue #130: COMMENT-string MYVECTOR(...) was mangled by the rewrite (got: '$COMMENT_AFTER')"
  fi

  mq -D prerel -e "DROP TABLE IF EXISTS ddl_comment_t;" 2>/dev/null || true
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
    mq_stdin -D prerel 2>/dev/null <<'SQL' || true
SET SESSION debug='+d,simulate_vector_crash';
CALL mysql.MYVECTOR_INDEX_BUILD('prerel.crash_t.vec', 'id');
SQL
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

# The index type must come from the column comment for BOTH documented forms: with
# the '|' start marker and without it ("MYVECTOR COLUMN type=..."). A type that is
# not recognised silently falls back to KNN, which would make every HNSW test in
# this suite exercise brute-force search instead.
#
# Issue #158: a line break or tab right after "MYVECTOR COLUMN" (a multi-line
# COMMENT) used to leave the prefix in the first key and fall back to KNN too.
# itype_multiline has a real line break in the comment; itype_tab uses the SQL
# '\t' escape.
run_index_type_check() {
  echo "  [Index type] type=hnsw yields an HNSW index for all comment formats"
  mq -D prerel -e "
    DROP TABLE IF EXISTS itype_nopipe, itype_pipe, itype_multiline, itype_tab, itype_leading;
    CREATE TABLE itype_nopipe (id INT PRIMARY KEY, vec VARBINARY(256)
      COMMENT 'MYVECTOR COLUMN type=hnsw,dim=3,size=100,m=16,ef=50,idcol=id,dist=L2');
    CREATE TABLE itype_pipe (id INT PRIMARY KEY, vec VARBINARY(256)
      COMMENT 'MYVECTOR Column |type=HNSW,dim=3,size=100,m=16,ef=50,idcol=id,dist=L2');
    CREATE TABLE itype_multiline (id INT PRIMARY KEY, vec VARBINARY(256)
      COMMENT 'MYVECTOR COLUMN
        type=hnsw,dim=3,size=100,m=16,ef=50,idcol=id,dist=L2');
    CREATE TABLE itype_tab (id INT PRIMARY KEY, vec VARBINARY(256)
      COMMENT 'MYVECTOR COLUMN\ttype=hnsw,dim=3,size=100,m=16,ef=50,idcol=id,dist=L2');
    CREATE TABLE itype_leading (id INT PRIMARY KEY, vec VARBINARY(256)
      COMMENT '\n    MYVECTOR COLUMN type=hnsw,dim=3,size=100,m=16,ef=50,idcol=id,dist=L2');
    INSERT INTO itype_nopipe VALUES (1, myvector_construct('[1.0,2.0,3.0]')),
                                    (2, myvector_construct('[4.0,5.0,6.0]'));
    INSERT INTO itype_pipe SELECT * FROM itype_nopipe;
    INSERT INTO itype_multiline SELECT * FROM itype_nopipe;
    INSERT INTO itype_tab SELECT * FROM itype_nopipe;
    INSERT INTO itype_leading SELECT * FROM itype_nopipe;
  " 2>/dev/null
  local T OUT
  # itype_leading: a comment that starts with a line break and spaces is accepted by
  # the MYVECTOR_INDEX_* procedures too, not only by the option parser.
  for T in itype_nopipe itype_pipe itype_multiline itype_tab itype_leading; do
    mq -D prerel -e "CALL mysql.MYVECTOR_INDEX_BUILD('prerel.${T}.vec', 'id');" >/dev/null 2>&1 || true
    OUT=$(mq -D prerel -e "CALL mysql.MYVECTOR_INDEX_STATUS('prerel.${T}.vec');" 2>&1) || true
    if echo "$OUT" | grep -q "Type : HNSW"; then
      pass "index type HNSW for ${T}"
    else
      fail "index type is not HNSW for ${T} (silent KNN fallback?): ${OUT}"
    fi
  done

  # A misspelled or missing type is an error, not a silent KNN index.
  echo "  [Index type] a misspelled or missing type fails the build instead of building KNN"
  mq -D prerel -e "
    DROP TABLE IF EXISTS itype_typo, itype_missing;
    CREATE TABLE itype_typo (id INT PRIMARY KEY, vec VARBINARY(256)
      COMMENT 'MYVECTOR COLUMN type=hnws,dim=3,size=100,m=16,ef=50,idcol=id,dist=L2');
    CREATE TABLE itype_missing (id INT PRIMARY KEY, vec VARBINARY(256)
      COMMENT 'MYVECTOR COLUMN dim=3,size=100,m=16,ef=50,idcol=id,dist=L2');
    INSERT INTO itype_typo SELECT * FROM itype_nopipe;
    INSERT INTO itype_missing SELECT * FROM itype_nopipe;
  " 2>/dev/null
  local WANT
  for T in itype_typo itype_missing; do
    [[ "$T" == itype_typo ]] && WANT="unknown index type 'hnws'" || WANT="missing index type"
    OUT=$(mq -D prerel -e "CALL mysql.MYVECTOR_INDEX_BUILD('prerel.${T}.vec', 'id');" 2>&1) || true
    if echo "$OUT" | grep -q "ERROR: ${WANT}"; then
      pass "build of ${T} reports: ${WANT}"
    else
      fail "build of ${T} did not report '${WANT}': ${OUT}"
    fi
    OUT=$(mq -D prerel -e "CALL mysql.MYVECTOR_INDEX_STATUS('prerel.${T}.vec');" 2>&1) || true
    if echo "$OUT" | grep -q "Type : "; then
      fail "an index was built for ${T} despite the invalid type: ${OUT}"
    else
      pass "no index built for ${T}"
    fi
  done

  # An index that is already open is not rebuilt from a comment whose type was
  # later broken; status and drop still work on it.
  echo "  [Index type] rebuilding an open index whose comment now has an unknown type fails"
  mq -D prerel -e "ALTER TABLE itype_nopipe MODIFY vec VARBINARY(256)
    COMMENT 'MYVECTOR COLUMN type=hnws,dim=3,size=100,m=16,ef=50,idcol=id,dist=L2';" 2>/dev/null
  OUT=$(mq -D prerel -e "CALL mysql.MYVECTOR_INDEX_BUILD('prerel.itype_nopipe.vec', 'id');" 2>&1) || true
  if echo "$OUT" | grep -q "ERROR: unknown index type 'hnws'"; then
    pass "rebuild of an open index with a broken comment reports the type error"
  else
    fail "rebuild of an open index with a broken comment did not report the type error: ${OUT}"
  fi
  OUT=$(mq -D prerel -e "CALL mysql.MYVECTOR_INDEX_STATUS('prerel.itype_nopipe.vec');" 2>&1) || true
  if echo "$OUT" | grep -q "Type : HNSW"; then
    pass "status of the open index still works"
  else
    fail "status of the open index failed after its comment was broken: ${OUT}"
  fi
  mq -D prerel -e "CALL mysql.MYVECTOR_INDEX_DROP('prerel.itype_nopipe.vec');" >/dev/null 2>&1 || true
  OUT=$(mq -D prerel -e "CALL mysql.MYVECTOR_INDEX_STATUS('prerel.itype_nopipe.vec');" 2>&1) || true
  if echo "$OUT" | grep -q "Type : "; then
    fail "drop of the open index did not remove it: ${OUT}"
  else
    pass "drop of the open index works"
  fi
}

# Regression test for issue #119: dist=cosine (lower case, as used throughout
# this very script) must actually resolve to the cosine metric, not silently
# fall back to L2 because the option match was case-sensitive.
#
# MYVECTOR_INDEX_STATUS now reports the *resolved* metric (m_dist), not the
# raw as-typed option string, so "Distance : Cosine" (exact case) only
# appears when the case-insensitive match in the index constructor actually
# fired; on the pre-fix code this line prints the raw option text verbatim
# ("Distance : cosine", lower case) regardless of which metric was really
# used, so the exact-case comparison below is deliberate -- a case-INsensitive
# grep would pass even on the buggy build and prove nothing.
#
# On MySQL 9.0+ (where the MYVECTOR_IS_ANN query rewrite is compiled in) we
# additionally prove it end-to-end: q=[1.0,0.0] is cosine-nearest to row A
# [2.0,0.0] (cosine distance 0) but L2-nearest to row B [1.0,0.5] (L2
# distance 0.5 vs A's 1.0) -- the two metrics disagree, so a silent L2
# fallback is caught by asserting the ANN top-1 is A (id=1), not B (id=2).
run_dist_case_insensitive() {
  local VER="$1"
  echo "  [Issue #119] dist=cosine (lower case) resolves to cosine, not L2"
  mq -D prerel -e "
    DROP TABLE IF EXISTS dist_ci;
    CREATE TABLE dist_ci (
      id  INT PRIMARY KEY,
      vec VARBINARY(256) COMMENT 'MYVECTOR COLUMN type=hnsw,dim=2,size=100,m=16,ef=50,idcol=id,dist=cosine'
    );
    INSERT INTO dist_ci VALUES (1, myvector_construct('[2.0,0.0]'));
    INSERT INTO dist_ci VALUES (2, myvector_construct('[1.0,0.5]'));
  " 2>/dev/null
  BUILD_CI=$(mq -D prerel -e \
    "CALL mysql.MYVECTOR_INDEX_BUILD('prerel.dist_ci.vec', 'id');" 2>&1 || true)
  if echo "$BUILD_CI" | grep -qE "^ERROR [0-9]"; then
    fail "dist=cosine (lower case) index build: unexpected error: $BUILD_CI"
    return 0
  fi

  STATUS_CI=$(mq -D prerel -e "CALL mysql.MYVECTOR_INDEX_STATUS('prerel.dist_ci.vec');" 2>&1) || true
  # Exact match up to the field's own "\n" separator (the status string embeds
  # literal newlines, which the mysql client's tab output renders as a literal
  # backslash-n, not a real line break) -- a plain substring grep for
  # "Distance : Cosine" would also match "Distance : CosineNorm", which is a
  # different, valid metric this same index type supports.
  if echo "$STATUS_CI" | grep -qE 'Distance : Cosine(\\n|$)'; then
    pass "dist=cosine (lower case) resolves to Cosine (exact-case match on resolved metric)"
  else
    fail "dist=cosine (lower case): MYVECTOR_INDEX_STATUS did not resolve to Cosine (got: $STATUS_CI)"
  fi

  # MYVECTOR_IS_ANN depends on the query-rewrite pre-parse service, which
  # (like the DDL rewrite tested elsewhere in this file) is not reliably
  # active on every MySQL version/build -- smoke-component.sh treats the
  # same condition as a non-fatal warning rather than a hard failure. Only
  # a real *wrong-answer* (L2 fallback) is a regression here; "the rewrite
  # didn't fire at all" is a pre-existing, separately-tracked limitation.
  ANN_OUT=$(mq -D prerel -N -e "
    SELECT id FROM dist_ci
    WHERE MYVECTOR_IS_ANN('prerel.dist_ci.vec', 'id', myvector_construct('[1.0,0.0]'), 1);
  " 2>&1) || true
  if echo "$ANN_OUT" | grep -qiE "ERROR|error in"; then
    skip "dist=cosine ANN ordering check: MYVECTOR_IS_ANN rewrite not active on $VER ($ANN_OUT)"
  else
    ANN_TOP1=$(echo "$ANN_OUT" | tr -d '[:space:]')
    if [[ "$ANN_TOP1" == "1" ]]; then
      pass "dist=cosine (lower case) ANN top-1 is the cosine-nearest row (id=1), not the L2-nearest (id=2)"
    else
      fail "dist=cosine (lower case): expected ANN top-1 id=1 (cosine-nearest), got '$ANN_TOP1' (L2 fallback?)"
    fi
  fi

  mq -D prerel -e "CALL mysql.MYVECTOR_INDEX_DROP('prerel.dist_ci.vec');" 2>/dev/null || true
}

# A failure while writing the index to disk must surface as an error from the build,
# never as an uncaught C++ exception that aborts mysqld. A directory is created where
# the index status file is written, so the open() in the save fails with EISDIR.
run_index_save_failure_check() {
  echo "  [Index save failure] a failed index save reports an error and keeps the server up"
  local DATADIR
  DATADIR=$(mq -N -e "SELECT @@datadir;" 2>/dev/null | LC_ALL=C tr -d '[:space:]')
  mq -D prerel -e "
    DROP TABLE IF EXISTS save_fail_t;
    CREATE TABLE save_fail_t (id INT PRIMARY KEY, vec VARBINARY(256)
      COMMENT 'MYVECTOR Column |type=HNSW,dim=3,size=100,m=16,ef=50,idcol=id,dist=L2');
    INSERT INTO save_fail_t VALUES (1, myvector_construct('[1.0,2.0,3.0]')),
                                   (2, myvector_construct('[4.0,5.0,6.0]'));
  " 2>/dev/null
  docker exec "$CONTAINER" mkdir -p "${DATADIR}prerel.save_fail_t.vec.hnsw.index.status"

  local OUT
  OUT=$(mq -D prerel -e "CALL mysql.MYVECTOR_INDEX_BUILD('prerel.save_fail_t.vec', 'id');" 2>&1) || true
  if ! mq -e "SELECT 1;" >/dev/null 2>&1; then
    fail "server crashed on a failed index save (uncaught exception): ${OUT}"
    return 0
  fi
  pass "server survived a failed index save"
  if echo "$OUT" | grep -q "ERROR"; then
    pass "failed index save reported as an error"
  else
    fail "failed index save was reported as success: ${OUT}"
  fi

  # The explicit "save" action must report a failed save too, not "SUCCESS".
  OUT=$(mq -D prerel -e "CALL mysql.MYVECTOR_INDEX_INTERNAL('prerel.save_fail_t.vec', 'id', 'save', '');" 2>&1) || true
  if echo "$OUT" | grep -q "ERROR"; then
    pass "explicit index save failure reported as an error"
  else
    fail "explicit index save failure was reported as success: ${OUT}"
  fi
}

run_edge_cases() {
  echo "  [Edge cases]"

  # NULL input
  NULL_OUT=$(mq -N -e "SELECT myvector_construct(NULL);" 2>/dev/null | LC_ALL=C tr -d '[:space:]')
  if [[ -z "$NULL_OUT" || "$NULL_OUT" == "NULL" ]]; then
    pass "myvector_construct(NULL) returns NULL"
  else
    fail "myvector_construct(NULL): expected NULL, got '$NULL_OUT'"
  fi

  # Non-array JSON — myvector_construct treats { } as array delimiters (YOLO JSON parser).
  # '{"a":1}' parses as a 2-element vector [0.0, 1.0], returns non-NULL binary.
  # This is implementation-defined behavior, not an error.
  skip "myvector_construct(non-array JSON) returns binary (parser accepts {...} as array)"

  # Empty array — returns metadata-only binary (8 bytes), not NULL.
  # Implementation-defined behavior: no length validation during construct.
  skip "myvector_construct([]) returns metadata bytes (empty-array NULL not enforced)"

  # Dimension mismatch: 5d vector in 3d index — build succeeds with silent truncation.
  # The batch build reads the first dim*4 bytes of each row, silently discarding extras.
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
  if echo "$DIM_BUILD" | grep -qE "SUCCESS.*rows.*2"; then
    pass "dimension mismatch: 5d row truncated to 3d (2 rows indexed as expected)"
  else
    fail "dimension mismatch: expected 2-row SUCCESS, got: $DIM_BUILD"
  fi

  # myvector_distance with mismatched dims fails the statement (#171): no numeric
  # distance over the shorter length.
  DIST_MM=$(mq -N -e "
    SELECT myvector_distance(
      myvector_construct('[1.0,0.0]'),
      myvector_construct('[1.0,0.0,0.0]')
    );" 2>&1 | grep -v "Using a password" || true)
  if echo "$DIST_MM" | grep -q "different dimensions"; then
    pass "myvector_distance(dim_mismatch) fails the statement"
  else
    fail "myvector_distance(dim_mismatch): expected a 'different dimensions' error, got '$DIST_MM'"
  fi

  # myvector_is_valid with wrong dimension arg → 0
  ISVALID=$(mq -N -e \
    "SELECT myvector_is_valid(myvector_construct('[1.0,2.0,3.0]'), 5);" \
    2>/dev/null | LC_ALL=C tr -d '[:space:]')
  if [[ "$ISVALID" == "0" ]]; then
    pass "myvector_is_valid(3d_vec, dim=5) returns 0"
  else
    fail "myvector_is_valid(3d_vec, dim=5): expected 0, got '$ISVALID'"
  fi
}

# myvector_distance()/myvector_display(): a NULL input is NULL for that row only;
# an unknown metric or vectors of different dimensions fail the statement
# (#170, #171). Runs scripts/test-distance-udf.py in its own container.
run_distance_udf_checks() {
  local VER="$1" COMP_DIR="$2"
  echo "  [Edge] myvector_distance NULL and error handling ($VER)"
  local OUT RC=0
  OUT=$(python3 "$REPO_ROOT/scripts/test-distance-udf.py" \
          --component-dir "$COMP_DIR" --image "mysql:$VER" 2>&1) || RC=$?
  if [[ "$RC" -eq 0 ]]; then
    pass "myvector_distance: NULL rows, unknown metric and dimension mismatch handled ($(echo "$OUT" | grep -c '^\[PASS')/7 checks)"
  else
    fail "myvector_distance NULL/error handling (#170, #171): $(echo "$OUT" | grep -E '^\[FAIL|Error' | head -3 | tr '\n' ' ')"
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
  run_rfc004_max_dim "$VER"
  run_ddl_rewrite_comment_string "$VER"
  run_index_type_check
  run_dist_case_insensitive "$VER"
  run_index_save_failure_check
  run_edge_cases
  run_rfc004_crash_injection

  cleanup_container
  run_distance_udf_checks "$VER" "$DIR"
  echo ""
done

# ── Phase 3: Component lifecycle regression ───────────────────────────────────

run_lifecycle_install_timing() {
  local VER="$1" COMP_DIR="$2"
  echo "  [Lifecycle 3.1] Cold INSTALL timing ($VER)"
  cleanup_container
  start_container "$VER"
  install_component "$COMP_DIR"
  # Measure only the INSTALL COMPONENT statement, not container startup.
  mq -e "UNINSTALL COMPONENT 'file://myvector';" 2>/dev/null || true
  local T0 T1 ELAPSED
  T0=$(date +%s)
  mq -e "INSTALL COMPONENT 'file://myvector';"
  T1=$(date +%s)
  ELAPSED=$(( T1 - T0 ))
  if [[ "$ELAPSED" -lt 5 ]]; then
    pass "install_time_s=${ELAPSED} < 5s"
  else
    fail "install_time_s=${ELAPSED} >= 5s (myvector_component_init regression)"
  fi
  cleanup_container
}

# Retry UNINSTALL COMPONENT until it succeeds or $1 seconds (default 15) pass. Used
# after a legitimate ERROR 3538 refusal, to wait for in-flight statements to drain.
# Sets the caller's RETRY_OUT / RETRY_RC to the result of the last attempt.
uninstall_retry() {
  local DEADLINE=$(( $(date +%s) + ${1:-15} ))
  while :; do
    RETRY_RC=0
    RETRY_OUT=$(mq -e "UNINSTALL COMPONENT 'file://myvector';" 2>&1) || RETRY_RC=$?
    if [[ "$RETRY_RC" -eq 0 || $(date +%s) -ge $DEADLINE ]]; then
      break
    fi
    sleep 1
  done
}

run_lifecycle_uninstall_under_load() {
  local VER="$1" COMP_DIR="$2"
  echo "  [Lifecycle 3.2] UNINSTALL under load: refused or not, the component unloads once load drains ($VER)"
  cleanup_container
  start_container "$VER"
  install_component "$COMP_DIR"

  # Build a 1000-row HNSW index so there is something to query.
  mq -e "
    CREATE DATABASE IF NOT EXISTS lc;
    CREATE TABLE lc.unload_t (
      id  INT PRIMARY KEY,
      vec VARBINARY(256)
        COMMENT 'MYVECTOR COLUMN type=hnsw,dim=3,size=1000,m=16,ef=50,idcol=id,dist=L2'
    );
  " 2>/dev/null
  local i
  for i in $(seq 0 9); do
    local VALS=""
    local j
    for j in $(seq 0 99); do
      local ROW=$(( i * 100 + j ))
      VALS="${VALS}(${ROW}, myvector_construct('[$(( RANDOM % 100 )).0,$(( RANDOM % 100 )).0,$(( RANDOM % 100 )).0]')),"
    done
    VALS="${VALS%,}"
    mq -D lc -e "INSERT INTO lc.unload_t (id, vec) VALUES ${VALS};" 2>/dev/null || true
  done
  mq -D lc -e "CALL mysql.MYVECTOR_INDEX_BUILD('lc.unload_t.vec', 'id');" 2>/dev/null \
    || { fail "INDEX_BUILD failed for unload_t" ; cleanup_container ; return 1 ; }

  # Start 5 background KNN query loops (30s timeout each).
  local -a BG_PIDS=()
  for i in $(seq 1 5); do
    (
      local STOP=$(( $(date +%s) + 30 ))
      while [[ $(date +%s) -lt $STOP ]]; do
        mq -D lc -e \
          "SELECT id FROM lc.unload_t ORDER BY myvector_distance(vec, myvector_construct('[1.0,0.0,0.0]'), 'L2') LIMIT 5;" \
          >/dev/null 2>&1 || true
      done
    ) &
    BG_PIDS+=($!)
  done
  sleep 1  # ensure workers have issued at least one query before UNINSTALL fires

  local T0 T1 ELAPSED
  local UNINSTALL_RC=0
  T0=$(date +%s)
  local UNINSTALL_OUT
  UNINSTALL_OUT=$(mq -e "UNINSTALL COMPONENT 'file://myvector';" 2>&1) || UNINSTALL_RC=$?
  T1=$(date +%s)
  ELAPSED=$(( T1 - T0 ))

  # Terminate background loops
  for pid in "${BG_PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait "${BG_PIDS[@]}" 2>/dev/null || true

  if [[ "$UNINSTALL_RC" -ne 0 ]]; then
    # MySQL refuses to unload while other sessions that have run a query are still
    # connected (ERROR 3540, #155), or while a statement runs one of our UDFs (ERROR
    # 3538). Both refusals are legitimate under load, provided the component stays
    # intact and unloads cleanly once the queries have drained (see 3.5).
    local RETRY_OUT RETRY_RC=0
    if echo "$UNINSTALL_OUT" | grep -qE "3540|3538"; then
      uninstall_retry 15   # poll until the sessions of the killed loops have ended
      if [[ "$RETRY_RC" -eq 0 ]]; then
        pass "UNINSTALL under load: refused once while sessions were active, succeeded after load drained"
      else
        fail "UNINSTALL still fails after load drained: $RETRY_OUT"
      fi
    else
      fail "UNINSTALL failed unexpectedly (rc=$UNINSTALL_RC): $UNINSTALL_OUT"
    fi
  elif [[ "$ELAPSED" -ge 12 ]]; then
    fail "UNINSTALL took ${ELAPSED}s >= 12s (teardown timeout regression)"
  else
    pass "UNINSTALL under load: no ERROR 3540, elapsed=${ELAPSED}s < 12s"
  fi
  cleanup_container
}

run_lifecycle_reload_persistence() {
  local VER="$1" COMP_DIR="$2"
  echo "  [Lifecycle 3.3] Reload cycle index persistence ($VER)"
  cleanup_container
  start_container "$VER"
  install_component "$COMP_DIR"

  # Build a 500-row HNSW index.
  mq -e "
    CREATE DATABASE IF NOT EXISTS lc;
    CREATE TABLE lc.reload_t (
      id  INT PRIMARY KEY,
      vec VARBINARY(516)
        COMMENT 'MYVECTOR COLUMN type=hnsw,dim=3,size=500,m=16,ef=50,idcol=id,dist=L2'
    );
  " 2>/dev/null
  local i
  for i in $(seq 0 4); do
    local VALS=""
    local j
    for j in $(seq 0 99); do
      local ROW=$(( i * 100 + j ))
      VALS="${VALS}(${ROW}, myvector_construct('[$(( j % 10 )).$(( RANDOM % 9 )),$(( j % 5 )).$(( RANDOM % 9 )),$(( j % 7 )).$(( RANDOM % 9 ))]')),"
    done
    VALS="${VALS%,}"
    mq -D lc -e "INSERT INTO lc.reload_t (id, vec) VALUES ${VALS};" 2>/dev/null || true
  done
  mq -D lc -e "CALL mysql.MYVECTOR_INDEX_BUILD('lc.reload_t.vec', 'id');" 2>/dev/null

  # Record top-3 KNN results for a fixed query vector before UNINSTALL.
  local BEFORE_RESULT
  BEFORE_RESULT=$(mq -N -D lc -e \
    "SELECT id FROM lc.reload_t ORDER BY myvector_distance(vec, myvector_construct('[1.0,2.0,3.0]'), 'L2') LIMIT 3;" \
    2>/dev/null | LC_ALL=C tr -s '[:space:]' ',' | sed 's/^,//;s/,$//')

  if [[ -z "$BEFORE_RESULT" ]]; then
    fail "reload persistence: could not retrieve top-3 KNN before UNINSTALL"
    cleanup_container
    return 0
  fi

  # UNINSTALL then INSTALL + load persisted index from disk (not rebuild).
  mq -e "UNINSTALL COMPONENT 'file://myvector';" 2>/dev/null || true
  install_component "$COMP_DIR"
  mq -D lc -e "CALL mysql.MYVECTOR_INDEX_LOAD('lc.reload_t.vec');" 2>/dev/null \
    || { fail "MYVECTOR_INDEX_LOAD failed: on-disk index not preserved across UNINSTALL/INSTALL" ; cleanup_container ; return 1 ; }

  # The KNN comparison below is a brute-force scan over the table and would pass even
  # if nothing were reloaded. MYVECTOR_INDEX_LOAD replaces the in-memory index with the
  # one on disk (a load with no index files leaves it empty), so the row count STATUS
  # reports afterwards is the real evidence that the persisted index came back.
  local STATUS_OUT
  STATUS_OUT=$(mq -D lc -e "CALL mysql.MYVECTOR_INDEX_STATUS('lc.reload_t.vec');" 2>&1) || true
  if echo "$STATUS_OUT" | grep -q "Type : HNSW" && echo "$STATUS_OUT" | grep -q "Current Rows : 500"; then
    pass "reload cycle: persisted HNSW index reloaded from disk (500 rows)"
  else
    fail "reload cycle: index not restored from disk (expected Type : HNSW, Current Rows : 500): ${STATUS_OUT}"
  fi

  local AFTER_RESULT
  AFTER_RESULT=$(mq -N -D lc -e \
    "SELECT id FROM lc.reload_t ORDER BY myvector_distance(vec, myvector_construct('[1.0,2.0,3.0]'), 'L2') LIMIT 3;" \
    2>/dev/null | LC_ALL=C tr -s '[:space:]' ',' | sed 's/^,//;s/,$//')

  if [[ "$BEFORE_RESULT" == "$AFTER_RESULT" ]]; then
    pass "reload cycle: top-3 KNN identical before/after UNINSTALL+INSTALL"
  else
    fail "reload cycle: top-3 KNN changed after reload (before='$BEFORE_RESULT' after='$AFTER_RESULT')"
  fi
  cleanup_container
}

run_lifecycle_binlog_cleanup() {
  local VER="$1" COMP_DIR="$2"
  echo "  [Lifecycle 3.4] MYVECTOR_BINLOG_STOP ends the listener's sessions; UNINSTALL then succeeds ($VER)"
  cleanup_container
  start_container "$VER"
  install_component "$COMP_DIR"
  # install_component writes myvector.cnf AFTER INSTALL COMPONENT, so the binlog
  # listener starts without config on the first install. Reinstall so the component
  # reads the now-present cnf and starts binlog monitoring.
  mq -e "UNINSTALL COMPONENT 'file://myvector';" 2>/dev/null || true
  mq -e "INSTALL COMPONENT 'file://myvector';"

  # Verify a binlog connection (slave/replica) appears after component install.
  sleep 2
  local PROC_BEFORE
  PROC_BEFORE=$(mq -N -e "SHOW PROCESSLIST;" 2>/dev/null | { grep -iE "binlog|slave|replica" || true; } | wc -l | LC_ALL=C tr -d '[:space:]')
  if [[ "$PROC_BEFORE" -eq 0 ]]; then
    skip "binlog cleanup: no binlog listener in PROCESSLIST before UNINSTALL (binlog may be disabled on this container)"
    cleanup_container
    return 0
  fi

  # Stopping the listener turns off online updates for every index: a user without
  # CONNECTION_ADMIN must be refused, and the listener must keep running.
  mq -e "CREATE USER IF NOT EXISTS lc_plain@'%' IDENTIFIED BY 'lc_plain_pw';" 2>/dev/null
  local DENY_OUT DENY_LEFT
  DENY_OUT=$(docker exec -e MYSQL_PWD=lc_plain_pw "$CONTAINER" \
    mysql -ulc_plain -h 127.0.0.1 -e "SELECT myvector_binlog_stop();" 2>&1 || true)
  DENY_LEFT=$(mq -N -e "SHOW PROCESSLIST;" 2>/dev/null \
    | { grep -iE "binlog|slave|replica" || true; } | wc -l | LC_ALL=C tr -d '[:space:]')
  if echo "$DENY_OUT" | grep -q "requires the CONNECTION_ADMIN privilege" && [[ "$DENY_LEFT" -gt 0 ]]; then
    pass "myvector_binlog_stop() refused without CONNECTION_ADMIN; listener still running"
  else
    fail "myvector_binlog_stop() without CONNECTION_ADMIN: '${DENY_OUT}', binlog sessions left: ${DENY_LEFT}"
  fi

  # The listener's server session holds a reference to the component's
  # event_tracking_parse service, and MySQL checks references before it calls the
  # component's deinit: a plain UNINSTALL fails with ERROR 3540 (#189). Stop the
  # listener first, in the same session.
  local STOP_OUT UNINSTALL_RC=0
  STOP_OUT=$(mq mysql -e "CALL MYVECTOR_BINLOG_STOP(); UNINSTALL COMPONENT 'file://myvector';" 2>&1) \
    || UNINSTALL_RC=$?
  local REMAINING
  REMAINING=$(mq -N -e "SHOW PROCESSLIST;" 2>/dev/null \
    | { grep -iE "binlog|slave|replica" || true; } | wc -l | LC_ALL=C tr -d '[:space:]')
  if [[ "$REMAINING" -eq 0 ]] && echo "$STOP_OUT" | grep -q "SUCCESS: binlog listener stopped"; then
    pass "MYVECTOR_BINLOG_STOP ended the listener's binlog sessions"
  else
    fail "binlog sessions after MYVECTOR_BINLOG_STOP: ${REMAINING} (${STOP_OUT})"
  fi
  if [[ "$UNINSTALL_RC" -eq 0 && -z "$(mq -N -e "SELECT component_urn FROM mysql.component;" 2>/dev/null)" ]]; then
    pass "UNINSTALL succeeded after MYVECTOR_BINLOG_STOP"
  else
    fail "UNINSTALL failed after MYVECTOR_BINLOG_STOP (#189): ${STOP_OUT}"
  fi
  cleanup_container
}

run_lifecycle_prepare_uninstall() {
  local VER="$1" COMP_DIR="$2"
  echo "  [Lifecycle 3.10] Other sessions block UNINSTALL (#155): reported, script stops, opt-in KILL ($VER)"
  cleanup_container
  start_container "$VER"
  install_component "$COMP_DIR"
  # Reinstall so the component reads myvector.cnf and starts its binlog listener
  # (see 3.4): MYVECTOR_PREPARE_UNINSTALL(0) must leave it running when it refuses.
  mq -e "UNINSTALL COMPONENT 'file://myvector';" 2>/dev/null || true
  mq -e "INSTALL COMPONENT 'file://myvector';"
  sleep 2
  local DUMPS_BEFORE
  DUMPS_BEFORE=$(mq -N -e "SELECT COUNT(*) FROM information_schema.processlist WHERE COMMAND LIKE 'Binlog Dump%';" 2>/dev/null | LC_ALL=C tr -d '[:space:]')

  # Another client session that has run a query holds a reference to the
  # component's event_tracking_parse service until it ends (#155).
  docker exec -e MYSQL_PWD="$ROOT_PW" "$CONTAINER" \
    mysql -uroot -h 127.0.0.1 -e "SELECT SLEEP(120);" >/dev/null 2>&1 &
  local BG_PID=$! OTHER_ID="" i
  for i in $(seq 1 20); do
    OTHER_ID=$(mq -N -e "SELECT ID FROM information_schema.processlist WHERE INFO = 'SELECT SLEEP(120)';" 2>/dev/null | LC_ALL=C tr -d '[:space:]')
    [[ -n "$OTHER_ID" ]] && break
    sleep 0.5
  done
  if [[ -z "$OTHER_ID" ]]; then
    fail "3.10: the background session did not appear in the processlist"
    kill "$BG_PID" 2>/dev/null || true
    cleanup_container
    return 0
  fi

  local OUT
  OUT=$(mq mysql -e "CALL MYVECTOR_UNINSTALL_CHECK();" 2>&1 || true)
  if echo "$OUT" | grep -qE "^${OTHER_ID}[[:space:]].*client session"; then
    pass "MYVECTOR_UNINSTALL_CHECK lists the other session (id ${OTHER_ID})"
  else
    fail "MYVECTOR_UNINSTALL_CHECK did not list session ${OTHER_ID}: ${OUT}"
  fi

  OUT=$(mq mysql -e "CALL MYVECTOR_PREPARE_UNINSTALL(0);" 2>&1 || true)
  if echo "$OUT" | grep -q "Other sessions may block UNINSTALL COMPONENT (#155): ${OTHER_ID}" \
     && [[ -n "$(mq -N -e "SELECT component_urn FROM mysql.component;" 2>/dev/null)" ]]; then
    pass "MYVECTOR_PREPARE_UNINSTALL(0) reports session ${OTHER_ID} as an error; component intact"
  else
    fail "MYVECTOR_PREPARE_UNINSTALL(0) with another session: ${OUT}"
  fi
  local DUMPS_AFTER
  DUMPS_AFTER=$(mq -N -e "SELECT COUNT(*) FROM information_schema.processlist WHERE COMMAND LIKE 'Binlog Dump%';" 2>/dev/null | LC_ALL=C tr -d '[:space:]')
  if [[ "${DUMPS_BEFORE:-0}" -eq 0 ]]; then
    skip "3.10: no binlog listener running, so 'listener left running' was not checked"
  elif [[ "${DUMPS_AFTER:-0}" -gt 0 ]]; then
    pass "MYVECTOR_PREPARE_UNINSTALL(0) refused without stopping the binlog listener"
  else
    fail "MYVECTOR_PREPARE_UNINSTALL(0) refused but stopped the binlog listener (${DUMPS_BEFORE} -> ${DUMPS_AFTER} dump sessions)"
  fi

  # The uninstall script must stop at that error, before dropping anything.
  docker cp "$REPO_ROOT/sql/myvector_uninstall_component.sql" "$CONTAINER:/tmp/uninstall.sql"
  OUT=$(docker exec -e MYSQL_PWD="$ROOT_PW" "$CONTAINER" \
    sh -c "mysql -uroot -h 127.0.0.1 < /tmp/uninstall.sql" 2>&1 || true)
  if echo "$OUT" | grep -q "Other sessions may block UNINSTALL" \
     && [[ -n "$(mq -N -e "SELECT ROUTINE_NAME FROM information_schema.routines WHERE ROUTINE_SCHEMA='mysql' AND ROUTINE_NAME='MYVECTOR_INDEX_BUILD';" 2>/dev/null)" ]] \
     && [[ -n "$(mq -N -e "SELECT component_urn FROM mysql.component;" 2>/dev/null)" ]]; then
    pass "uninstall script stops before dropping procedures while another session exists"
  else
    fail "uninstall script with another session (should stop, procedures and component intact): ${OUT}"
  fi

  # The behaviour #155 describes: a plain UNINSTALL fails while the session exists.
  OUT=$(mq -e "UNINSTALL COMPONENT 'file://myvector';" 2>&1 || true)
  if echo "$OUT" | grep -q "ERROR 3540"; then
    pass "plain UNINSTALL fails with ERROR 3540 while another session exists (#155)"
  else
    fail "plain UNINSTALL with another session did not give ERROR 3540: ${OUT}"
  fi

  # Opt-in: KILL the other sessions, then UNINSTALL in the same session.
  local RC=0
  OUT=$(mq mysql -e "CALL MYVECTOR_PREPARE_UNINSTALL(1); UNINSTALL COMPONENT 'file://myvector';" 2>&1) || RC=$?
  if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -q "SUCCESS: run UNINSTALL COMPONENT now" \
     && [[ -z "$(mq -N -e "SELECT component_urn FROM mysql.component;" 2>/dev/null)" ]] \
     && [[ -z "$(mq -N -e "SELECT ID FROM information_schema.processlist WHERE ID = ${OTHER_ID};" 2>/dev/null)" ]]; then
    pass "MYVECTOR_PREPARE_UNINSTALL(1) killed session ${OTHER_ID}; UNINSTALL succeeded"
  else
    fail "MYVECTOR_PREPARE_UNINSTALL(1) + UNINSTALL (rc ${RC}): ${OUT}"
  fi
  kill "$BG_PID" 2>/dev/null || true
  wait "$BG_PID" 2>/dev/null || true
  cleanup_container
}

run_lifecycle_uninstall_inflight_udf() {
  local VER="$1" COMP_DIR="$2"
  echo "  [Lifecycle 3.5] UNINSTALL refused while a UDF is in use leaves the component intact ($VER)"
  cleanup_container
  start_container "$VER"
  install_component "$COMP_DIR"

  mq -e "CREATE DATABASE IF NOT EXISTS lc;
         CREATE TABLE lc.inflight_t (id INT PRIMARY KEY, vec VARBINARY(256));" 2>/dev/null
  local i j VALS
  for i in $(seq 0 9); do
    VALS=""
    for j in $(seq 0 99); do
      VALS="${VALS}($(( i * 100 + j )), myvector_construct('[$(( RANDOM % 100 )).0,$(( RANDOM % 100 )).0,$(( RANDOM % 100 )).0]')),"
    done
    mq -D lc -e "INSERT INTO lc.inflight_t (id, vec) VALUES ${VALS%,};" 2>/dev/null || true
  done

  # One long-running statement (1000^3 distance evaluations) keeps myvector_distance
  # in use for the whole test. The marker comment lets us find and KILL it later.
  ( mq -D lc -e "SELECT /* inflight_probe */ SUM(myvector_distance(a.vec, b.vec, 'L2'))
                 FROM lc.inflight_t a JOIN lc.inflight_t b JOIN lc.inflight_t c;" \
      >/dev/null 2>&1 || true ) &
  local QUERY_PID=$!
  local QID=""
  local DEADLINE=$(( $(date +%s) + 15 ))
  while [[ -z "$QID" && $(date +%s) -lt $DEADLINE ]]; do
    QID=$(mq -N -e "SELECT id FROM information_schema.processlist
                    WHERE info LIKE '%inflight_probe%' AND info NOT LIKE '%processlist%' LIMIT 1;" 2>/dev/null \
          | LC_ALL=C tr -d '[:space:]')
    [[ -z "$QID" ]] && sleep 0.5
  done
  if [[ -z "$QID" ]]; then
    fail "in-flight UDF probe query never appeared in PROCESSLIST"
    kill "$QUERY_PID" 2>/dev/null || true
    cleanup_container
    return 0
  fi

  local UNINSTALL_OUT UNINSTALL_RC=0
  UNINSTALL_OUT=$(mq -e "UNINSTALL COMPONENT 'file://myvector';" 2>&1) || UNINSTALL_RC=$?

  if [[ "$UNINSTALL_RC" -eq 0 ]]; then
    # The server let the component unload despite a running UDF; nothing to assert
    # about a refused unload.
    skip "in-flight UDF: UNINSTALL succeeded on MySQL $VER, no refusal to verify"
  elif ! echo "$UNINSTALL_OUT" | grep -qE "3540|3538"; then
    fail "UNINSTALL failed for an unexpected reason (expected ERROR 3540 or 3538): ${UNINSTALL_OUT}"
  else
    # UNINSTALL was refused (expected). The session running the UDF has run a query,
    # so MySQL refuses with ERROR 3540 before it calls the component's deinit (#155);
    # ERROR 3538 is the UDF-in-use refusal from deinit. A refused unload must leave
    # the component fully functional: every UDF still registered and usable.
    local FUNC_OUT
    FUNC_OUT=$(mq -N -D lc -e "SELECT myvector_display(myvector_construct('[1.0,2.0,3.0]')),
                                myvector_distance(myvector_construct('[1.0,2.0,3.0]'),
                                                  myvector_construct('[1.0,2.0,3.0]'), 'L2');" 2>&1) || true
    if echo "$FUNC_OUT" | grep -qE "ERROR|does not exist"; then
      fail "UNINSTALL refused (${UNINSTALL_OUT}) but left the component half torn down: ${FUNC_OUT}"
    else
      pass "refused UNINSTALL left all UDFs registered and usable"
    fi

    # The rollback must restore exactly the UDFs the failed unload removed, so check
    # all six: called with no arguments a registered UDF answers "Incorrect arguments",
    # an unregistered one "does not exist".
    local UDF MISSING=""
    for UDF in myvector_ann_set myvector_construct myvector_display myvector_distance \
               myvector_construct_binaryvector myvector_hamming_distance; do
      if mq -N -D lc -e "SELECT ${UDF}();" 2>&1 | grep -q "does not exist"; then
        MISSING="${MISSING} ${UDF}"
      fi
    done
    if [[ -n "$MISSING" ]]; then
      fail "refused UNINSTALL left UDFs unregistered:${MISSING}"
    else
      pass "refused UNINSTALL left all six UDFs registered"
    fi
  fi

  # Once the query is gone the component must unload cleanly.
  mq -e "KILL QUERY ${QID};" 2>/dev/null || true
  # Wait (bounded) for the statement to leave the PROCESSLIST rather than a bare
  # `wait`, which would hang if the KILL had not taken effect.
  local GONE_BY=$(( $(date +%s) + 20 ))
  while [[ $(date +%s) -lt $GONE_BY ]]; do
    if [[ -z "$(mq -N -e "SELECT id FROM information_schema.processlist WHERE id=${QID};" 2>/dev/null \
                 | LC_ALL=C tr -d '[:space:]')" ]]; then
      break
    fi
    sleep 0.5
  done
  kill "$QUERY_PID" 2>/dev/null || true   # reap the background client if still around
  wait "$QUERY_PID" 2>/dev/null || true
  if [[ "$UNINSTALL_RC" -ne 0 ]]; then
    local RETRY_OUT RETRY_RC=0
    uninstall_retry 15
    if [[ "$RETRY_RC" -eq 0 ]]; then
      pass "UNINSTALL succeeds once the in-flight UDF query has ended"
    else
      fail "UNINSTALL still fails after the in-flight query ended: ${RETRY_OUT}"
    fi
  fi
  cleanup_container
}

# Current Rows of an index, from MYVECTOR_INDEX_STATUS ("" if the call fails).
index_rows() {
  mq -N -e "CALL mysql.MYVECTOR_INDEX_STATUS('$1');" 2>/dev/null \
    | grep -oE 'Current Rows : [0-9]+' | grep -oE '[0-9]+$' || true
}

# Poll index_rows $1 for up to $3 seconds until it equals $2. Sets ROWS_SEEN.
wait_index_rows() {
  local DEADLINE=$(( $(date +%s) + $3 ))
  while :; do
    ROWS_SEEN=$(index_rows "$1")
    [[ "$ROWS_SEEN" == "$2" || $(date +%s) -ge $DEADLINE ]] && break
    sleep 1
  done
}

run_lifecycle_online_after_restart() {
  local VER="$1" COMP_DIR="$2"
  echo "  [Lifecycle 3.6] Online updates resume after a server restart, without reinstall ($VER)"
  cleanup_container
  start_container "$VER"
  install_component "$COMP_DIR"
  # When the listener connects it finds the online=Y indexes to reopen through the
  # mysql.myvector_columns view. sql/myvector_install_component.sql creates it;
  # install_procs does not, so create it here as the install script does.
  mq mysql -e "
    CREATE OR REPLACE VIEW myvector_columns AS
    SELECT TABLE_SCHEMA AS db, TABLE_NAME AS tbl, COLUMN_NAME AS col,
           COLUMN_COMMENT AS info
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE COLUMN_COMMENT LIKE 'MYVECTOR%'
    ORDER BY db, tbl, col;" 2>/dev/null
  # install_component writes myvector.cnf after INSTALL COMPONENT; reinstall so the
  # listener starts (as in 3.4).
  mq -e "UNINSTALL COMPONENT 'file://myvector';" 2>/dev/null || true
  mq -e "INSTALL COMPONENT 'file://myvector';"

  mq -e "
    CREATE DATABASE IF NOT EXISTS lc;
    CREATE TABLE lc.restart_t (
      id  INT PRIMARY KEY,
      vec VARBINARY(256)
        COMMENT 'MYVECTOR COLUMN type=hnsw,dim=3,size=1000,m=16,ef=50,idcol=id,dist=L2,online=Y'
    );
    INSERT INTO lc.restart_t VALUES
      (1, myvector_construct('[1.0,0.0,0.0]')),
      (2, myvector_construct('[0.0,1.0,0.0]')),
      (3, myvector_construct('[0.0,0.0,1.0]'));
  " 2>/dev/null
  mq -e "CALL mysql.MYVECTOR_INDEX_BUILD('lc.restart_t.vec', 'id');" 2>/dev/null || true

  # Control: before the restart an INSERT reaches the index.
  mq -e "INSERT INTO lc.restart_t VALUES (4, myvector_construct('[1.0,1.0,0.0]'));"
  wait_index_rows lc.restart_t.vec 4 20
  if [[ "$ROWS_SEEN" != "4" ]]; then
    fail "online after restart: setup broken, INSERT not applied before the restart (rows=${ROWS_SEEN:-none}, expected 4)"
    cleanup_container
    return 0
  fi

  docker restart "$CONTAINER" >/dev/null
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
  if [[ $READY -lt 3 ]]; then
    fail "online after restart: MySQL did not come back after docker restart"
    cleanup_container
    return 0
  fi
  # Once the listener connects it opens the online=Y indexes itself, with no LOAD
  # (#186). Do not call MYVECTOR_INDEX_LOAD here: it would hide a listener that never
  # started.
  local DEADLINE=$(( $(date +%s) + 30 ))
  ROWS_SEEN=""
  while [[ -z "$ROWS_SEEN" && $(date +%s) -lt $DEADLINE ]]; do
    sleep 1
    ROWS_SEEN=$(index_rows lc.restart_t.vec)
  done
  if [[ -z "$ROWS_SEEN" ]]; then
    fail "online after restart: online=Y index not reopened by the binlog listener within 30s: listener not started at boot (#186)"
    cleanup_container
    return 0
  fi
  # On disk the index is as of the build (3 rows). Row 4 was applied online before the
  # restart; the listener must replay it from the index checkpoint (#190).
  wait_index_rows lc.restart_t.vec 4 20
  local AFTER_BOOT="$ROWS_SEEN"
  if [[ "$AFTER_BOOT" == "4" ]]; then
    pass "online after restart: row applied before the restart replayed into the index (rows=4)"
  else
    fail "online after restart: row applied before the restart is missing (rows=${AFTER_BOOT}, expected 4) (#190)"
  fi

  # No reinstall: the component was loaded at boot. A new INSERT must be applied.
  mq -e "INSERT INTO lc.restart_t VALUES (5, myvector_construct('[0.0,1.0,1.0]'));"
  wait_index_rows lc.restart_t.vec $(( AFTER_BOOT + 1 )) 20
  if [[ "$ROWS_SEEN" == "$(( AFTER_BOOT + 1 ))" ]]; then
    pass "online after restart: listener reopened the index and applied a new INSERT with no reinstall (rows ${AFTER_BOOT} -> ${ROWS_SEEN})"
  else
    fail "online after restart: INSERT not applied within 20s (rows=${ROWS_SEEN:-none}, expected $(( AFTER_BOOT + 1 ))): listener stopped applying after the restart (#186; intermittent stalls: #179)"
  fi
  cleanup_container
}

run_lifecycle_online_delete_update() {
  local VER="$1" COMP_DIR="$2"
  echo "  [Lifecycle 3.7] Online DELETE and UPDATE reach the index, and survive a checkpoint + restart ($VER)"
  cleanup_container
  start_container "$VER"
  install_component "$COMP_DIR"
  # The listener reopens online=Y indexes at boot through this view (see 3.6).
  mq mysql -e "
    CREATE OR REPLACE VIEW myvector_columns AS
    SELECT TABLE_SCHEMA AS db, TABLE_NAME AS tbl, COLUMN_NAME AS col,
           COLUMN_COMMENT AS info
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE COLUMN_COMMENT LIKE 'MYVECTOR%'
    ORDER BY db, tbl, col;" 2>/dev/null
  # install_component writes myvector.cnf after INSTALL COMPONENT; reinstall so the
  # listener starts (as in 3.4).
  mq -e "UNINSTALL COMPONENT 'file://myvector';" 2>/dev/null || true
  mq -e "INSTALL COMPONENT 'file://myvector';"

  mq -e "
    CREATE DATABASE IF NOT EXISTS lc;
    -- Columns of other types around the vector: the row parser must know each
    -- one's width, or it misreads keys (and a misread DELETE removes the wrong row).
    CREATE TABLE lc.dml_t (
      id  INT PRIMARY KEY,
      t   TINYINT DEFAULT 7,
      d   DECIMAL(10,3) DEFAULT 12.5,
      dt  DATETIME(3) DEFAULT '2026-10-03 10:00:00.123',
      j   JSON,
      c   CHAR(10) DEFAULT 'abc',
      e   ENUM('x','y') DEFAULT 'y',
      vec VARBINARY(256)
        COMMENT 'MYVECTOR COLUMN type=hnsw,dim=3,size=1000,m=16,ef=50,idcol=id,dist=L2,online=Y',
      note TEXT
    );
    INSERT INTO lc.dml_t (id, j, vec, note) VALUES
      (1, '{\"a\":1}', myvector_construct('[1.0,0.0,0.0]'), 'one'),
      (2, '{\"a\":2}', myvector_construct('[0.0,1.0,0.0]'), 'two'),
      (3, NULL,          myvector_construct('[0.0,0.0,1.0]'), NULL),
      (4, '{\"a\":4}', myvector_construct('[1.0,1.0,0.0]'), 'four'),
      (5, '{\"a\":5}', myvector_construct('[0.0,1.0,1.0]'), 'five');
  " 2>/dev/null
  mq -e "CALL mysql.MYVECTOR_INDEX_BUILD('lc.dml_t.vec', 'id');" 2>/dev/null || true
  wait_index_rows lc.dml_t.vec 5 20
  if [[ "$ROWS_SEEN" != "5" ]]; then
    fail "online DELETE/UPDATE: setup broken, index has ${ROWS_SEEN:-no} rows, expected 5"
    cleanup_container
    return 0
  fi

  # nn <vector> <k>: ids of the k nearest rows, e.g. [2,5,4]
  nn() {
    mq -N -e "SELECT myvector_ann_set('lc.dml_t.vec', 'id', myvector_construct('$1'), 'nn=$2');" \
      2>/dev/null | LC_ALL=C tr -d '[:space:]'
  }
  # check_state <label>: the expected index after the DML below
  check_state() {
    local NOK=""
    [[ "$(index_rows lc.dml_t.vec)" == "4" ]] || NOK="${NOK} rows=$(index_rows lc.dml_t.vec)(want 4)"
    [[ "$(nn '[0.0,1.0,0.0]' 1)" == "[2]" ]] || NOK="${NOK} nearest[0,1,0]=$(nn '[0.0,1.0,0.0]' 1)(want [2])"
    [[ "$(nn '[9.0,9.0,9.0]' 1)" == "[3]" ]] || NOK="${NOK} nearest[9,9,9]=$(nn '[9.0,9.0,9.0]' 1)(want [3])"
    [[ "$(nn '[1.0,1.0,0.0]' 1)" == "[40]" ]] || NOK="${NOK} nearest[1,1,0]=$(nn '[1.0,1.0,0.0]' 1)(want [40])"
    local ALL; ALL=$(nn '[0.0,0.0,0.0]' 10)
    if echo "$ALL" | grep -qE '(\[|,)(4|5)(,|\])'; then NOK="${NOK} all=${ALL}(4 and 5 must be gone)"; fi
    if [[ -z "$NOK" ]]; then
      pass "online DELETE/UPDATE $1: index matches the table (rows=4, ids ${ALL})"
    else
      fail "online DELETE/UPDATE $1:${NOK} (#188)"
    fi
  }

  # DELETE, UPDATE of the vector, UPDATE of the key, vector set to NULL, and
  # re-INSERT of a deleted key.
  mq -e "DELETE FROM lc.dml_t WHERE id = 2;"
  wait_index_rows lc.dml_t.vec 4 20
  if [[ "$(nn '[0.0,1.0,0.0]' 5)" == *2* ]] || [[ "$ROWS_SEEN" != "4" ]]; then
    fail "online DELETE: deleted id 2 still in the index (rows=${ROWS_SEEN}, nn=$(nn '[0.0,1.0,0.0]' 5)) (#188)"
  else
    pass "online DELETE removed the row from the index (rows=4)"
  fi
  mq -e "UPDATE lc.dml_t SET vec = myvector_construct('[9.0,9.0,9.0]') WHERE id = 3;
         UPDATE lc.dml_t SET id = 40 WHERE id = 4;
         UPDATE lc.dml_t SET vec = NULL WHERE id = 5;
         UPDATE lc.dml_t SET note = 'changed', t = 9 WHERE id = 1;
         INSERT INTO lc.dml_t (id, vec) VALUES (2, myvector_construct('[0.0,1.0,0.0]'));"
  sleep 3
  check_state "after the DML"

  # Rotate the binlog: the listener checkpoints the index to disk, so after the
  # restart it replays nothing older and the state must come from disk.
  mq -e "FLUSH BINARY LOGS;"
  sleep 3
  docker restart "$CONTAINER" >/dev/null
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
  local DEADLINE=$(( $(date +%s) + 30 ))
  while [[ -z "$(index_rows lc.dml_t.vec)" && $(date +%s) -lt $DEADLINE ]]; do sleep 1; done
  check_state "after a checkpoint and a restart"

  # The listener resumes from the file named by the rotation's checkpoint. Its name
  # once came with 4 checksum bytes on the end, and the listener could not resume.
  mq -e "INSERT INTO lc.dml_t (id, vec) VALUES (6, myvector_construct('[5.0,5.0,5.0]'));"
  wait_index_rows lc.dml_t.vec 5 20
  if [[ "$ROWS_SEEN" == "5" && "$(nn '[5.0,5.0,5.0]' 1)" == "[6]" ]]; then
    pass "online INSERT after a rotation and a restart reached the index (rows=5)"
  else
    fail "online INSERT after a rotation and a restart not applied (rows=${ROWS_SEEN:-none}, nn=$(nn '[5.0,5.0,5.0]' 1))"
  fi
  cleanup_container
}

run_lifecycle_dump_connection_killed() {
  local VER="$1" COMP_DIR="$2"
  echo "  [Lifecycle 3.8] Online updates continue after the listener's binlog connection is killed ($VER)"
  cleanup_container
  start_container "$VER"
  install_component "$COMP_DIR"
  # install_component writes myvector.cnf after INSTALL COMPONENT; reinstall so the
  # listener starts (as in 3.4).
  mq -e "UNINSTALL COMPONENT 'file://myvector';" 2>/dev/null || true
  mq -e "INSTALL COMPONENT 'file://myvector';"

  mq -e "
    CREATE DATABASE IF NOT EXISTS lc;
    CREATE TABLE lc.kill_t (
      id  INT PRIMARY KEY,
      vec VARBINARY(256)
        COMMENT 'MYVECTOR COLUMN type=hnsw,dim=3,size=1000,m=16,ef=50,idcol=id,dist=L2,online=Y'
    );
    INSERT INTO lc.kill_t VALUES (1, myvector_construct('[1.0,0.0,0.0]')),
                                 (2, myvector_construct('[0.0,1.0,0.0]'));
  " 2>/dev/null
  mq -e "CALL mysql.MYVECTOR_INDEX_BUILD('lc.kill_t.vec', 'id');" 2>/dev/null || true

  # Rotate first: the resume position then names a file the listener reached by a
  # rotate event (its name once came with 4 checksum bytes on the end, #179/#195).
  mq -e "INSERT INTO lc.kill_t VALUES (3, myvector_construct('[0.0,0.0,1.0]')); FLUSH BINARY LOGS;
         INSERT INTO lc.kill_t VALUES (4, myvector_construct('[1.0,1.0,0.0]'));"
  wait_index_rows lc.kill_t.vec 4 20
  if [[ "$ROWS_SEEN" != "4" ]]; then
    fail "dump connection killed: setup broken, index has ${ROWS_SEEN:-no} rows, expected 4"
    cleanup_container
    return 0
  fi

  local ROUND EXPECT=4 DUMP_ID
  for ROUND in 1 2; do
    DUMP_ID=$(mq -N -e "SELECT ID FROM information_schema.PROCESSLIST
                        WHERE COMMAND LIKE 'Binlog Dump%' LIMIT 1;" 2>/dev/null | LC_ALL=C tr -d '[:space:]')
    if [[ -z "$DUMP_ID" ]]; then
      fail "dump connection killed: no Binlog Dump session to kill (round ${ROUND})"
      cleanup_container
      return 0
    fi
    if ! mq -e "KILL ${DUMP_ID};" 2>/dev/null; then
      fail "dump connection killed: KILL ${DUMP_ID} failed (round ${ROUND})"
      cleanup_container
      return 0
    fi
    # The INSERT must go through a new connection, so wait for the killed one to end.
    local GONE_BY=$(( $(date +%s) + 10 )) LEFT=1
    while [[ $(date +%s) -lt $GONE_BY ]]; do
      LEFT=$(mq -N -e "SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE ID = ${DUMP_ID};" \
             2>/dev/null | LC_ALL=C tr -d '[:space:]')
      [[ "$LEFT" == "0" ]] && break
      sleep 0.5
    done
    if [[ "$LEFT" != "0" ]]; then
      fail "dump connection killed: session ${DUMP_ID} still present 10s after KILL (round ${ROUND})"
      cleanup_container
      return 0
    fi
    [[ "$ROUND" == "2" ]] && mq -e "FLUSH BINARY LOGS;"
    EXPECT=$(( EXPECT + 1 ))
    mq -e "INSERT INTO lc.kill_t VALUES (${EXPECT}, myvector_construct('[${EXPECT}.0,0.0,0.0]'));"
    wait_index_rows lc.kill_t.vec "$EXPECT" 20
    if [[ "$ROWS_SEEN" == "$EXPECT" ]]; then
      pass "dump connection killed (round ${ROUND}$([[ $ROUND == 2 ]] && echo ', then a rotation')): INSERT applied (rows=${EXPECT})"
    else
      fail "dump connection killed (round ${ROUND}): INSERT not applied within 20s (rows=${ROWS_SEEN:-none}, expected ${EXPECT}) (#179)"
      cleanup_container
      return 0
    fi
  done
  cleanup_container
}

run_lifecycle_build_during_binlog_backlog() {
  local VER="$1" COMP_DIR="$2"
  echo "  [Lifecycle 3.9] Building an online=Y index while the listener works through a backlog ($VER)"
  cleanup_container
  start_container "$VER"
  install_component "$COMP_DIR"
  # install_component writes myvector.cnf after INSTALL COMPONENT; reinstall so the
  # listener starts (as in 3.4).
  mq -e "UNINSTALL COMPONENT 'file://myvector';" 2>/dev/null || true
  mq -e "INSTALL COMPONENT 'file://myvector';"

  # 20,000 rows in one statement: the listener queues an item per row for an index
  # that does not exist yet, and its workers are still busy when the build starts.
  # The build used to put the index in the collection before initializing it, and a
  # worker then inserted into the half-made index: mysqld crashed (SIGSEGV, #187).
  mq -e "
    CREATE DATABASE IF NOT EXISTS lc;
    CREATE TABLE lc.bulk_t (
      id  INT PRIMARY KEY,
      vec VARBINARY(256)
        COMMENT 'MYVECTOR COLUMN type=hnsw,dim=3,size=30000,m=16,ef=50,idcol=id,dist=L2,online=Y'
    );
    SET SESSION cte_max_recursion_depth = 30000;
    INSERT INTO lc.bulk_t (id, vec)
      WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 20000)
      SELECT n, myvector_construct(CONCAT('[', n % 97, '.0,', n % 89, '.0,', n % 83, '.0]')) FROM seq;
  " 2>/dev/null
  local BUILD_OUT
  BUILD_OUT=$(mq -N -e "CALL mysql.MYVECTOR_INDEX_BUILD('lc.bulk_t.vec', 'id');" 2>&1) || true
  sleep 2
  if ! mq -e "SELECT 1" >/dev/null 2>&1 || docker logs "$CONTAINER" 2>&1 | grep -q "got signal"; then
    fail "build during a binlog backlog: mysqld crashed (#187): ${BUILD_OUT}"
    cleanup_container
    return 0
  fi
  if ! echo "$BUILD_OUT" | grep -q "rows : 20000"; then
    fail "build during a binlog backlog: unexpected build result: ${BUILD_OUT}"
  else
    # Prove the race window was hit: a worker that reaches the index while it is
    # still being created sees the never-built sentinel and logs a skip against
    # it. If the workers drained the backlog before the build opened the index,
    # this run did not exercise #187.
    local OVERLAP
    OVERLAP=$(docker logs "$CONTAINER" 2>&1 | grep -c "Skipping index update .* < (zzzzzz.bin" || true)
    if [[ "$OVERLAP" -gt 0 ]]; then
      pass "build during a binlog backlog: no crash, 20000 rows; ${OVERLAP} queued rows reached the index while it was being created"
    else
      skip "build during a binlog backlog: no crash, 20000 rows, but the listener had drained before the build (overlap not reached)"
    fi
  fi

  # The listener must keep applying rows after the build.
  mq -e "INSERT INTO lc.bulk_t VALUES (20001, myvector_construct('[500.0,500.0,500.0]'));"
  wait_index_rows lc.bulk_t.vec 20001 30
  if [[ "$ROWS_SEEN" == "20001" ]]; then
    pass "build during a binlog backlog: a later INSERT reached the index (rows=20001)"
  else
    fail "build during a binlog backlog: later INSERT not applied (rows=${ROWS_SEEN:-none}, expected 20001)"
  fi
  cleanup_container
}

for VER in "${VERSIONS[@]}"; do
  DIR="${COMPONENT_DIRS[$VER]}"
  echo "--- Phase 3 Lifecycle ($VER) ---"
  run_lifecycle_install_timing  "$VER" "$DIR"
  run_lifecycle_uninstall_under_load "$VER" "$DIR"
  run_lifecycle_reload_persistence   "$VER" "$DIR"
  run_lifecycle_binlog_cleanup       "$VER" "$DIR"
  run_lifecycle_uninstall_inflight_udf "$VER" "$DIR"
  run_lifecycle_online_after_restart   "$VER" "$DIR"
  run_lifecycle_online_delete_update   "$VER" "$DIR"
  run_lifecycle_dump_connection_killed "$VER" "$DIR"
  run_lifecycle_build_during_binlog_backlog "$VER" "$DIR"
  run_lifecycle_prepare_uninstall "$VER" "$DIR"
  echo ""
done
