#!/usr/bin/env bash
# Fuzzing and soak run of component_mcp under AddressSanitizer (RFC 0001 §10 go/no-go: "zero
# mysqld crashes over 72h of fuzzing plus a soak test under ASAN").
#
# Starts the ASAN server built by scripts/build-mcp-asan-26.10-docker.sh in an oraclelinux:9
# container, sets up component_mcp_guard, the tool pack, the token table and the accounts every
# refusal path needs, installs component_mcp, then runs scripts/mcp-fuzz/fuzz.py from a sidecar
# for SECONDS: HTTP and JSON-RPC fuzzing, SQL fuzzing through run_query, checked sessions in both
# protocol eras, and non-MCP traffic. mysqld's resident memory is sampled every minute.
#
# Fails (exit 1) on: a mysqld crash or exit, any AddressSanitizer report, an error-log signal or
# assertion, an unclean shutdown, or any invariant fuzz.py checks (server unresponsive, a write
# let through, a token hash in a reply, a wrong answer).
#
# Usage: scripts/fuzz-mcp-endpoint.sh <mysql-asan.tar.gz> <seconds> <out-dir> [seed]
# Requires Docker. Artifacts in out-dir: summary.json, fuzz.log, mysqld.err, rss.csv, asan.*,
# verdict.txt.
set -uo pipefail
TARBALL=${1:?mysql-asan.tar.gz}; SECONDS_TO_RUN=${2:?seconds}; OUT=${3:?out dir}; SEED=${4:-1}
REPO=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$OUT"; OUT=$(cd "$OUT" && pwd)
WORK=$(mktemp -d); C=mcp-fuzz-$$
trap 'docker rm -f -v "$C" "$C-drv" >/dev/null 2>&1; rm -rf "$WORK"' EXIT
fail() { echo "FAIL: $*" | tee -a "$OUT/verdict.txt"; FAILED=1; }
FAILED=0
: >"$OUT/verdict.txt"

tar -C "$WORK" -xzf "$TARBALL"
B="$WORK/mysql-asan"
h() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }
chmod 0777 "$OUT"

# ASAN: stop at the first error and write it to OUT/asan.<pid>; leaks are not part of the gate
# (mysqld frees little at exit), memory growth is watched through rss.csv instead.
ASAN="halt_on_error=1:abort_on_error=0:handle_abort=1:detect_leaks=0:print_stacktrace=1:disable_coredump=1:log_path=/out/asan"
MYSQLD_ARGS="--basedir=/m --datadir=/data --user=root --plugin-dir=/m/lib/plugin \
  --lc-messages-dir=/m/share --character-sets-dir=/m/share/charsets --bind-address=127.0.0.1 \
  --port=3306 --mysqlx=OFF --log-error=/out/mysqld.err --max-connections=300"
# UMASK: mysqld creates its files 0640 by default; the logs in OUT must be readable by the runner.
docker run -d --name "$C" -v "$B:/m:ro" -v "$OUT:/out" -e LD_LIBRARY_PATH=/m/lib -e UMASK=0644 -e UMASK_DIR=0755 \
  -e ASAN_OPTIONS="$ASAN" oraclelinux:9 bash -c "
    /m/bin/mysqld $MYSQLD_ARGS --initialize-insecure && exec /m/bin/mysqld $MYSQLD_ARGS" >/dev/null || exit 1
sql() { docker exec -i "$C" /m/bin/mysql -uroot -h127.0.0.1 -N "$@"; }
# Under ASAN, --initialize alone takes minutes.
for _ in $(seq 1 600); do sql -e 'select 1' >/dev/null 2>&1 && break; sleep 3; done
sql -e 'select version()' || { fail "server did not start"; tail -50 "$OUT/mysqld.err"; exit 1; }

sql <"$REPO/sql/mcp/mysql_mcp.sql" || fail "tool pack install"
sql <"$REPO/src/mcp/endpoint/mcp_tokens.sql" || fail "token table install"
sql <<SQL || fail "setup"
INSTALL COMPONENT 'file://component_mcp_guard';
CREATE DATABASE d1;
CREATE TABLE d1.t (i INT); INSERT INTO d1.t VALUES (1), (2), (3);
SET SESSION cte_max_recursion_depth = 2000;
CREATE TABLE d1.big (n INT PRIMARY KEY);
INSERT INTO d1.big WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM s WHERE n < 1500) SELECT n FROM s;
CREATE TABLE d1.masked (secret_col VARCHAR(20), ok VARCHAR(5)); INSERT INTO d1.masked VALUES ('topsecret', 'x');
CREATE TABLE d1.dbl (d DOUBLE); INSERT INTO d1.dbl VALUES (1e300), (-0.0), (NULL);
CREATE TABLE d1.app_log (id BIGINT AUTO_INCREMENT PRIMARY KEY, v INT);
-- MCP accounts: grants wider than the guard allows, so the guard is what refuses.
CREATE USER mcp@'%', mcp2@'%';
GRANT SELECT, INSERT, UPDATE, DELETE, DROP, CREATE ON d1.* TO mcp@'%', mcp2@'%';
GRANT SELECT ON mysql.* TO mcp@'%', mcp2@'%';
GRANT EXECUTE ON mysql_mcp.* TO mcp@'%', mcp2@'%';
GRANT MCP_USER ON *.* TO mcp@'%', mcp2@'%';
CREATE USER lockd@'%' ACCOUNT LOCK; GRANT SELECT ON d1.* TO lockd@'%'; GRANT MCP_USER ON *.* TO lockd@'%';
CREATE USER adm@'%'; GRANT SELECT ON d1.* TO adm@'%'; GRANT MCP_ADMIN ON *.* TO adm@'%';
-- Non-MCP traffic and the monitor.
CREATE USER app@'%' IDENTIFIED BY 'app-pw'; GRANT SELECT ON d1.big TO app@'%'; GRANT INSERT ON d1.app_log TO app@'%';
CREATE USER fuzzmon@'%'; GRANT SELECT ON d1.t TO fuzzmon@'%';
INSERT INTO mysql.mcp_tokens (token_sha256, account, expires_at) VALUES
  ('$(h fuzz-token-table)', 'mcp2@%', NULL),
  ('$(h fuzz-token-locked)', 'lockd@%', NULL),
  ('$(h fuzz-token-admin)', 'adm@%', NULL),
  ('$(h fuzz-token-expired)', 'mcp@%', NOW() - INTERVAL 1 DAY);
INSTALL COMPONENT 'file://component_mcp' SET GLOBAL mcp_http.port = 33480,
  GLOBAL mcp_http.account = 'mcp@%', GLOBAL mcp_http.token_sha256 = '$(h fuzz-token-main)',
  GLOBAL mcp_http.mask_columns = 'secret', GLOBAL mcp_http.allowed_origins = 'http://localhost:6274';
SQL
[ "$FAILED" = 0 ] || { tail -30 "$OUT/mysqld.err"; exit 1; }
# Preflight: the guard must be active (it is inert if event tracking reaches no component, e.g.
# without component_reference_cache), or the run would measure an unguarded server.
pre=$(docker exec -i "$C" /m/bin/mysql -umcp -h127.0.0.1 -N -e 'DELETE FROM d1.t' 2>&1)
printf '%s\n' "$pre" | grep -q "ERROR 1290" || { fail "guard inactive: an MCP DELETE was not refused ($pre)"; exit 1; }

cp "$REPO/scripts/mcp-fuzz/fuzz.py" "$WORK/fuzz.py"

echo "==> fuzzing for ${SECONDS_TO_RUN}s (seed $SEED)"
docker run -d --name "$C-drv" --network "container:$C" -v "$WORK:/w:ro" -v "$OUT:/out" python:3.12-slim \
  sh -c "pip install -q --disable-pip-version-check --root-user-action=ignore mysql-connector-python==26.7.0 \
    && python -u /w/fuzz.py --seconds $SECONDS_TO_RUN --seed $SEED --out /out" >/dev/null || exit 1
echo "elapsed_s,rss_kb" >"$OUT/rss.csv"
start=$(date +%s)
while [ "$(docker inspect -f '{{.State.Running}}' "$C-drv" 2>/dev/null)" = true ]; do
  if [ "$(docker inspect -f '{{.State.Running}}' "$C")" != true ]; then
    fail "mysqld exited during the run (exit code $(docker inspect -f '{{.State.ExitCode}}' "$C"))"
    break
  fi
  rss=$(docker exec "$C" awk '/VmRSS/{print $2}' /proc/1/status 2>/dev/null)
  echo "$(( $(date +%s) - start )),${rss:-}" >>"$OUT/rss.csv"
  sleep 60
done
docker logs "$C-drv" >"$OUT/fuzz.log" 2>&1
drv_rc=$(docker wait "$C-drv" 2>/dev/null || echo 1)
tail -25 "$OUT/fuzz.log"
[ "$drv_rc" = 0 ] || fail "fuzz.py reported invariant violations or failed (rc=$drv_rc); see summary.json"

if [ "$(docker inspect -f '{{.State.Running}}' "$C")" = true ]; then
  sql -e 'SELECT 1' >/dev/null || fail "server not answering SQL after the run"
  # The endpoint may stall while the fuzzer holds its I/O threads (#269); once the fuzzer has
  # stopped it must answer again (a stall that outlives the load would be a deadlock).
  recovered=no
  for _ in $(seq 1 36); do
    code=$(docker run --rm --network "container:$C" curlimages/curl:8.11.1 -s -m 10 -o /dev/null -w '%{http_code}' \
      -X POST http://127.0.0.1:33480/mcp -H 'Authorization: Bearer fuzz-token-main' -H 'Content-Type: application/json' \
      -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' 2>/dev/null)
    [ "$code" = 200 ] && { recovered=yes; break; }
    sleep 5
  done
  [ "$recovered" = yes ] || fail "the endpoint did not answer within 3 minutes after the run"
  sql -e 'SHUTDOWN' >/dev/null 2>&1
  rc=$(timeout 600 docker wait "$C" || echo timeout)
  [ "$rc" = 0 ] || fail "unclean shutdown (exit $rc)"
fi
if ls "$OUT"/asan.* >/dev/null 2>&1; then
  fail "AddressSanitizer reports: $(ls "$OUT"/asan.* | tr '\n' ' ')"
  head -60 "$OUT"/asan.*
fi
if grep -nE "AddressSanitizer|got signal|Assertion failure|stack_bottom" "$OUT/mysqld.err" >"$OUT/err-matches.txt"; then
  fail "error log: $(head -5 "$OUT/err-matches.txt" | tr '\n' ' ')"
fi
awk -F, 'NR==2{a=$2} NR>1&&$2!=""{b=$2} END{if (a) printf "rss: %d MB at start, %d MB at end\n", a/1024, b/1024}' "$OUT/rss.csv" | tee -a "$OUT/verdict.txt"
if [ "$FAILED" = 0 ]; then
  echo "PASS: ${SECONDS_TO_RUN}s, seed $SEED, no crash, no sanitizer report, no invariant violation" | tee -a "$OUT/verdict.txt"
fi
exit "$FAILED"
