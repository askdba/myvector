#!/usr/bin/env bash
# End-to-end test of component_mcp (RFC 0001 Phase 4 spike) with component_mcp_guard, on the
# official MySQL 26.10 image: bearer auth, the MCP handshake, both tools, and the Phase 4 exit
# criterion (the guard's policy reaches run_query in the impersonated sessions).
#
# Usage: scripts/test-mcp-endpoint.sh <mysql-image> <artifact-dir>
#   e.g. scripts/test-mcp-endpoint.sh mysql:26.10.0 dist/mcp-26.10.0
# The artifact dir holds component_mcp.so and component_mcp_guard.so (both for that release).
# Requires Docker. bash 3.2+.
set -u
IMAGE=${1:?mysql image, e.g. mysql:26.10.0}; ART=${2:?artifact dir}
REPO=$(cd "$(dirname "$0")/.." && pwd)
PACK=$REPO/sql/mcp/mysql_mcp.sql
CURL_IMAGE=curlimages/curl:8.11.1
# The token is hashed outside MySQL, as the docs say, so it never appears in statement text.
TOKEN_HEX=$(printf '%s' tok-123 | sha256sum | cut -d' ' -f1)
C=mcp-endpoint-test-$$
PASS=0; FAIL=0
ok() { PASS=$((PASS + 1)); printf '  PASS  %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf '  FAIL  %s  -- %s\n' "$1" "$2"; }
expect() { if printf '%s\n' "$2" | grep -Eq -- "$3"; then ok "$1"; else bad "$1" "got: $(printf '%s' "$2" | head -c 300)"; fi; }
trap 'docker rm -f -v "$C" >/dev/null 2>&1' EXIT
sql() { docker exec -i "$C" mysql -h127.0.0.1 -uroot -ptestpass -N -e "$1" 2>&1 | grep -v "Using a password"; }
# rpc TOKEN JSON: POST to the endpoint from a sidecar in the server's network namespace.
rpc() { docker run --rm --network "container:$C" "$CURL_IMAGE" -s -m 20 -w '\n%{http_code}' -X POST \
	http://127.0.0.1:33480/mcp -H 'Content-Type: application/json' ${1:+-H "Authorization: Bearer $1"} -d "$2"; }
call() { rpc tok-123 "{\"jsonrpc\":\"2.0\",\"id\":9,\"method\":\"tools/call\",\"params\":{\"name\":\"$1\",\"arguments\":$2}}"; }
alive() { docker ps -q -f "name=$C" | grep -q . && echo up || echo DOWN; }

echo "== $IMAGE"
docker run -d --name "$C" -e MYSQL_ROOT_PASSWORD=testpass "$IMAGE" >/dev/null || exit 1
for _ in $(seq 1 90); do sql 'select 1' >/dev/null 2>&1 && sql 'select 1' | grep -q '^1$' && break; sleep 2; done
PD=$(sql 'select @@plugin_dir')
for f in component_mcp_guard.so component_mcp.so; do docker cp "$ART/$f" "$C:$PD/$f" && docker exec -u 0 "$C" chmod 0644 "$PD/$f"; done
docker exec -i "$C" mysql -h127.0.0.1 -uroot -ptestpass <"$PACK" 2>&1 | grep -v "Using a password"
expect "install refused without component_mcp_guard" "$(sql "INSTALL COMPONENT 'file://component_mcp' SET GLOBAL mcp_http.account = 'mcp@%', GLOBAL mcp_http.token_sha256 = '$TOKEN_HEX'")" "ERROR"
sql "INSTALL COMPONENT 'file://component_mcp_guard'; GRANT MCP_ADMIN ON *.* TO root@'%';
CREATE DATABASE d1; CREATE TABLE d1.t (i INT); INSERT INTO d1.t VALUES (1);
CREATE TABLE d1.big (n INT); INSERT INTO d1.big WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM s WHERE n < 999) SELECT n FROM s;
INSERT INTO d1.big SELECT n + 1000 FROM d1.big WHERE n <= 500;
CREATE TABLE d1.dbl (d DOUBLE(255,0)); INSERT INTO d1.dbl VALUES (1e250);
CREATE USER mcp@'%' IDENTIFIED BY 'p'; GRANT SELECT, INSERT ON d1.* TO mcp@'%'; GRANT SELECT ON mysql.* TO mcp@'%';
GRANT EXECUTE ON mysql_mcp.* TO mcp@'%'; GRANT MCP_USER ON *.* TO mcp@'%';
CREATE USER app@'%' IDENTIFIED BY 'p'; GRANT SELECT, INSERT ON d1.* TO app@'%'; GRANT EXECUTE ON mysql_mcp.* TO app@'%';"
expect "install component_mcp" "$(sql "INSTALL COMPONENT 'file://component_mcp' SET GLOBAL mcp_http.port = 33480, GLOBAL mcp_http.account = 'mcp@%', GLOBAL mcp_http.token_sha256 = '$TOKEN_HEX'")" "^$"

expect "the token hash is not readable by other accounts" "$(docker exec -i "$C" mysql -h127.0.0.1 -umcp -pp -N -e "SELECT @@mcp_http.token_sha256" 2>&1 | grep -v "Using a password")" "ERROR.*SENSITIVE_VARIABLES_OBSERVER"
expect "no token: 401" "$(rpc '' '{"jsonrpc":"2.0","id":1,"method":"ping"}')" "^401$"
expect "wrong token: 401" "$(rpc nope '{"jsonrpc":"2.0","id":1,"method":"ping"}')" "^401$"
expect "initialize" "$(rpc tok-123 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}')" '"serverInfo":\{"name":"component_mcp"'
expect "notification: 202" "$(rpc tok-123 '{"jsonrpc":"2.0","method":"notifications/initialized"}')" "^202$"
expect "tools/list" "$(rpc tok-123 '{"jsonrpc":"2.0","id":2,"method":"tools/list"}')" '"name":"list_databases".*"name":"run_query"'
expect "unknown method" "$(rpc tok-123 '{"jsonrpc":"2.0","id":3,"method":"nope"}')" '"code":-32601'
expect "list_databases via the tool pack" "$(call list_databases '{}')" 'SCHEMA_NAME.*d1.*"isError":false'
expect "large result truncated at 1000 rows" "$(call run_query '{"sql":"SELECT n FROM d1.big"}' | grep -o '\\"n\\":' | wc -l) $(call run_query '{"sql":"SELECT n FROM d1.big"}' | grep -o 'truncated\\":true' | head -1)" "^1000 truncated"
explicit_limit_reply=$(call run_query '{"sql":"SELECT n FROM d1.big LIMIT 5000"}')
expect "explicit LIMIT: the reply is still bounded" "$(printf '%s' "$explicit_limit_reply" | grep -o '\\"n\\":' | wc -l) $(printf '%s' "$explicit_limit_reply" | grep -o 'truncated\\":true' | head -1)" "^1000 truncated"
expect "escaped values count against the bound (no HTTP 500)" "$(call run_query '{"sql":"SELECT REPEAT(CHAR(1), 3000000) AS v"}' | head -c 300)" 'rows\\":\[\],\\"truncated\\":true'
expect "bytes that are not UTF-8 are returned as base64" "$(call run_query '{"sql":"SELECT X'"'"'FF'"'"' AS b"}')" 'b\\":\{\\"base64\\":\\"/w==\\"\}'
expect "valid UTF-8 stays a string" "$(call run_query '{"sql":"SELECT CONVERT(X'"'"'C3A9'"'"' USING utf8mb4) AS s"}')" 's\\":\\"é\\"' 
expect "a value over the byte bound is not sent" "$(call run_query '{"sql":"SELECT REPEAT(\"x\", 5000000) AS v"}' | head -c 300)" 'rows\\":\[\],\\"truncated\\":true'
# The bounded consumer: the server keeps at most 1001 rows and 4 MiB of values while a
# statement runs, whatever its LIMIT. mysqld is PID 1; its peak RSS (VmHWM) must grow by under
# 32 MiB, not by the size of the full result (about 100 MB for each; without the bounded
# consumer it grew by 61 and 144 MiB). The wide case runs first, as the peak only rises.
hwm_kb() { docker exec "$C" awk '/^VmHWM/ {print $2}' /proc/1/status; }
hwm0=$(hwm_kb)
wide_reply=$(call run_query '{"sql":"SELECT REPEAT(\"x\", 1000000) AS v FROM d1.big LIMIT 100"}')
expect "wide rows: the reply is bounded" "$(printf '%s' "$wide_reply" | grep -o '\\"v\\":' | wc -l) $(printf '%s' "$wide_reply" | grep -o 'truncated\\":true' | head -1)" "^[1-4] truncated"
expect "wide rows: bytes past the bound are not retained" "$(( ($(hwm_kb) - hwm0) / 1024 )) MiB" "^([0-9]|[12][0-9]|3[01]) MiB$"
hwm0=$(hwm_kb)
long_reply=$(call run_query '{"sql":"SELECT REPEAT(\"x\", 100) AS v FROM d1.big a, d1.big b LIMIT 1000000"}')
expect "LIMIT 1000000: the reply is bounded" "$(printf '%s' "$long_reply" | grep -o '\\"v\\":' | wc -l) $(printf '%s' "$long_reply" | grep -o 'truncated\\":true' | head -1)" "^1000 truncated"
expect "LIMIT 1000000: rows past the bound are not retained" "$(( ($(hwm_kb) - hwm0) / 1024 )) MiB" "^([0-9]|[12][0-9]|3[01]) MiB$"
expect "a NULL-only result is not cut" "$(call run_query '{"sql":"SELECT NULL AS z FROM d1.big LIMIT 3"}')" 'z\\":null\},\{\\"z\\":null\},\{\\"z\\":null\}\],\\"truncated\\":false'
# NULL cells count against the byte budget too (the DOM consumer keeps a pointer per cell): 1001
# rows of 8000 NULLs would hold 64 MB. The reply's own 4 MB cut comes first either way, so the
# check is on mysqld's peak RSS. Building the reply for 8000 columns takes memory of its own, so
# the same query with LIMIT 60 (under the budget, so retained the same way either way) runs first:
# the large LIMIT may then raise the peak only by what the consumer retains past it: under 16 MiB
# (measured +3 MiB; without the NULL accounting, +37 MiB).
nulls=$(seq -s, -f 'NULL AS n%g' 1 8000)
call run_query "{\"sql\":\"SELECT $nulls FROM d1.big LIMIT 60\"}" >/dev/null
hwm0=$(hwm_kb)
null_reply=$(call run_query "{\"sql\":\"SELECT $nulls FROM d1.big a, d1.big b LIMIT 5000\"}")
expect "8000 NULL columns: the reply is bounded and truncated" "$(printf '%s' "$null_reply" | grep -o 'n8000\\":null' | head -1) $(printf '%s' "$null_reply" | grep -o 'truncated\\":true' | head -1)" 'n8000\\":null truncated'
expect "8000 NULL columns: NULL cells past the bound are not retained" "$(( ($(hwm_kb) - hwm0) / 1024 )) MiB" "^([0-9]|1[0-5]) MiB$"
# DOUBLE values are charged their formatted length: 500 rows of 100 short DOUBLEs fit.
dbls=$(seq -s, -f 'n * 1.5e0 AS d%g' 1 100)
expect "500 rows of 100 DOUBLE columns are not cut" "$(call run_query "{\"sql\":\"SELECT $dbls FROM d1.big LIMIT 500\"}" | grep -o 'd100\\":' | wc -l | tr -d ' ')" "^500$"
expect "a fixed-decimal DOUBLE (251 digits) is returned in full" "$(call run_query '{"sql":"SELECT d AS big FROM d1.dbl"}' | grep -o 'big\\":\\"1[0-9]*' | wc -c | tr -d ' ')" "^260$"
expect "an error after some rows is reported, not a partial result" "$(call run_query '{"sql":"SELECT n, IF(n = 500, (SELECT 1 UNION SELECT 2), n) AS x FROM d1.big"}')" 'Subquery returns more than 1 row.*"isError":true'
expect "an error past the row bound is reported" "$(call run_query '{"sql":"SELECT n, IF(n = 1500, (SELECT 1 UNION SELECT 2), n) AS x FROM d1.big a LIMIT 1000000"}')" 'Subquery returns more than 1 row.*"isError":true'
expect "run_query read as the mapped account" "$(call run_query '{"sql":"SELECT i, CURRENT_USER() AS who FROM d1.t"}')" 'i\\":\\"1\\",\\"who\\":\\"mcp@%'
# Phase 4 exit criterion: the guard's policy reaches run_query in impersonated sessions.
expect "guard: an MCP account's write is refused" "$(call run_query '{"sql":"INSERT INTO d1.t VALUES (2)"}')" 'Error 1290: [^"]*mcp.read_only[^"]*"}],"isError":true'
expect "guard: mysql schema refused" "$(call run_query '{"sql":"SELECT COUNT(*) FROM mysql.user"}')" 'Error 1290: [^"]*mcp.deny_system_schemas[^"]*"}],"isError":true'
expect "multi-statement is a parse error" "$(call run_query '{"sql":"SELECT 1; INSERT INTO d1.t VALUES (3)"}')" 'Error 1064: .*"isError":true'
expect "server alive" "$(alive)" "^up$"
expect "nothing written" "$(sql "SELECT GROUP_CONCAT(i) FROM d1.t")" "^1$"
expect "guard counted the endpoint's statements" "$(sql "SHOW GLOBAL STATUS LIKE 'mcp.statements_denied'")" "mcp.statements_denied	[1-9]"
# Impersonation skips the login checks: the endpoint refuses a locked account itself.
sql "ALTER USER mcp@'%' ACCOUNT LOCK"
expect "a locked account is refused" "$(call run_query '{"sql":"SELECT 1 AS one"}')" "Error 3118: Access denied for user 'mcp'@'%'. Account is locked.*\"isError\":true"
expect "a locked account is refused for tool pack calls too" "$(call list_databases '{}')" 'Error 3118: .*"isError":true'
sql "ALTER USER mcp@'%' ACCOUNT UNLOCK"
expect "the account works again once unlocked" "$(call run_query '{"sql":"SELECT 1 AS one"}')" 'one\\":\\"1\\".*"isError":false'
expect "no MCP session left" "$(sql "SHOW GLOBAL STATUS LIKE 'mcp.sessions_connected'")" "	0$"

# An account without MCP_USER is not treated as an MCP session (the token maps to app).
sql "UNINSTALL COMPONENT 'file://component_mcp'" >/dev/null
expect "reinstall mapped to app" "$(sql "INSTALL COMPONENT 'file://component_mcp' SET GLOBAL mcp_http.port = 33480, GLOBAL mcp_http.account = 'app@%', GLOBAL mcp_http.token_sha256 = '$TOKEN_HEX'")" "^$"
expect "guard: an account without MCP_USER is not restricted" "$(call run_query '{"sql":"INSERT INTO d1.t VALUES (7)"}')" '"isError":false'
expect "the guard cannot be uninstalled while the endpoint is installed" "$(sql "UNINSTALL COMPONENT 'file://component_mcp_guard'")" "ERROR"
expect "uninstall component_mcp" "$(sql "UNINSTALL COMPONENT 'file://component_mcp'")" "^$"
expect "then the guard can be uninstalled" "$(sql "UNINSTALL COMPONENT 'file://component_mcp_guard'")" "^$"
expect "server alive at the end" "$(alive)" "^up$"
echo "== $IMAGE: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
