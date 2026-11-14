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
# rpc_origin ORIGIN JSON: as rpc with the token, sending an Origin header (as a browser does).
rpc_origin() { docker run --rm --network "container:$C" "$CURL_IMAGE" -s -m 20 -w '\n%{http_code}' -X POST \
	http://127.0.0.1:33480/mcp -H 'Content-Type: application/json' -H 'Authorization: Bearer tok-123' \
	-H "Origin: $1" -d "$2"; }
# curl_i ARGS...: a request from a sidecar, printing the status line and headers (CR stripped).
curl_i() { docker run --rm --network "container:$C" "$CURL_IMAGE" -s -m 20 -o /dev/null -D - "$@" \
	http://127.0.0.1:33480/mcp | tr -d '\r'; }
call() { rpc tok-123 "{\"jsonrpc\":\"2.0\",\"id\":9,\"method\":\"tools/call\",\"params\":{\"name\":\"$1\",\"arguments\":$2}}"; }
# par N SQL: N concurrent run_query calls from one sidecar; prints the replies (they can
# interleave, so count matches with grep -o), then the wall time in seconds as "elapsed=S".
par() { docker run --rm --network "container:$C" --entrypoint sh "$CURL_IMAGE" -c '
	n=$0; body=$1; start=$(date +%s); i=0
	while [ "$i" -lt "$n" ]; do
		curl -s -m 30 -w "\n" -X POST http://127.0.0.1:33480/mcp -H "Content-Type: application/json" \
			-H "Authorization: Bearer tok-123" -d "$body" & i=$((i + 1))
	done; wait; echo "elapsed=$(( $(date +%s) - start ))"' "$1" \
	"{\"jsonrpc\":\"2.0\",\"id\":9,\"method\":\"tools/call\",\"params\":{\"name\":\"run_query\",\"arguments\":{\"sql\":\"$2\"}}}"; }
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
# DNS rebinding: a browser request carries its page's Origin; none is allowed by default.
expect "a browser Origin is refused: 403, even with the token" "$(rpc_origin http://evil.example '{"jsonrpc":"2.0","id":1,"method":"ping"}')" "^403$"
expect "a localhost Origin is refused too unless allowed" "$(rpc_origin http://localhost:6274 '{"jsonrpc":"2.0","id":1,"method":"ping"}')" "^403$"
expect "initialize" "$(rpc tok-123 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}')" '"serverInfo":\{"name":"component_mcp"'
expect "notification: 202" "$(rpc tok-123 '{"jsonrpc":"2.0","method":"notifications/initialized"}')" "^202$"
expect "tools/list: the six core tools" "$(rpc tok-123 '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | grep -o '"name":"[a-z_]*"' | tr '\n' ' ')" '^"name":"list_databases" "name":"list_tables" "name":"describe_table" "name":"run_query" "name":"ping" "name":"server_info" $'
expect "tools/list: catalog and status tools are annotated read-only, run_query is not" "$(rpc tok-123 '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | grep -oE '"name":"[a-z_]*","description":"[^"]*",("annotations":\{"readOnlyHint":true)?' | sed 's/"description":"[^"]*",//; s/"annotations":{"readOnlyHint":true/ro/' | tr '\n' ' ')" '^"name":"list_databases",ro "name":"list_tables",ro "name":"describe_table",ro "name":"run_query", "name":"ping",ro "name":"server_info",ro $'
expect "unknown method" "$(rpc tok-123 '{"jsonrpc":"2.0","id":3,"method":"nope"}')" '"code":-32601'
expect "list_databases via the tool pack" "$(call list_databases '{}')" 'SCHEMA_NAME.*d1.*"isError":false'
expect "list_tables via the tool pack" "$(call list_tables '{"database":"d1"}')" 'TABLE_NAME\\":\\"big\\".*TABLE_NAME\\":\\"t\\".*"isError":false'
expect "describe_table via the tool pack" "$(call describe_table '{"database":"d1","table":"t"}')" 'COLUMN_NAME\\":\\"i\\",\\"COLUMN_TYPE\\":\\"int\\".*"isError":false'
# A table named with quotes, backslashes and SQL (two backslashes, a quote, "); SELECT 1; -- x"):
# describe_table must find it and return its columns, so the argument bytes arrive exactly.
docker exec -i "$C" mysql -h127.0.0.1 -uroot -ptestpass 2>&1 <<'SQL' | grep -v "Using a password"
CREATE TABLE d1.`t\\'); SELECT 1; -- x` (i INT, j INT);
SQL
expect "arguments are literals, kept byte for byte (quotes, backslashes, SQL)" "$(call describe_table '{"database":"d1","table":"t\\\\'"'"'); SELECT 1; -- x"}')" 'COLUMN_NAME\\":\\"i\\".*COLUMN_NAME\\":\\"j\\".*"isError":false'
expect "describe_table without table is a tool error" "$(call describe_table '{"database":"d1"}')" 'needs string arguments database and table.*"isError":true'
expect "list_tables with a non-string database is a tool error" "$(call list_tables '{"database":1}')" 'needs a string argument database.*"isError":true'
expect "ping" "$(call ping '{}')" 'success\\":true,\\"latency_ms\\":[0-9]+,\\"message\\":\\"pong\\".*"isError":false'
expect "server_info, as the mapped account (one object, not rows)" "$(call server_info '{}')" '"text":"\{\\"version\\":\\"26\.10\.[0-9]+\\",\\"server_engine\\":\\"mysql\\".*current_user\\":\\"mcp@%\\",\\"current_database\\":\\"\\".*max_connections\\":\\"[0-9]+\\".*"isError":false'
expect "large result truncated at 1000 rows" "$(call run_query '{"sql":"SELECT n FROM d1.big"}' | grep -o '\\"n\\":' | wc -l) $(call run_query '{"sql":"SELECT n FROM d1.big"}' | grep -o 'truncated\\":true' | head -1)" "^1000 truncated"
explicit_limit_reply=$(call run_query '{"sql":"SELECT n FROM d1.big LIMIT 5000"}')
expect "explicit LIMIT: the reply is still bounded" "$(printf '%s' "$explicit_limit_reply" | grep -o '\\"n\\":' | wc -l) $(printf '%s' "$explicit_limit_reply" | grep -o 'truncated\\":true' | head -1)" "^1000 truncated"
expect "escaped values count against the bound (no HTTP 500)" "$(call run_query '{"sql":"SELECT REPEAT(CHAR(1), 3000000) AS v"}' | head -c 300)" 'rows\\":\[\],\\"truncated\\":true'
expect "bytes that are not UTF-8 are returned as base64" "$(call run_query '{"sql":"SELECT X'"'"'FF'"'"' AS b"}')" 'b\\":\{\\"base64\\":\\"/w==\\"\}'
expect "valid UTF-8 stays a string" "$(call run_query '{"sql":"SELECT CONVERT(X'"'"'C3A9'"'"' USING utf8mb4) AS s"}')" 's\\":\\"é\\"' 
expect "a value over the byte bound is not sent" "$(call run_query '{"sql":"SELECT REPEAT(\"x\", 5000000) AS v"}' | head -c 300)" 'rows\\":\[\],\\"truncated\\":true'
# The bounded consumer: the server keeps at most 1001 rows and 4 MiB (values plus per-row and
# per-cell overhead) while a statement runs, whatever its LIMIT. mcp_http.last_rows_retained and
# last_bytes_retained report what it kept for the last statement (exact, unlike process memory):
# a row is charged its values, 8 bytes per cell and 32 per row.
# Prints "rows bytes stored": stored is the row count the DOM consumer itself handed over, an
# independent observation, so a row the budget drops but the DOM consumer keeps shows up there.
retained() { sql "SHOW GLOBAL STATUS LIKE 'mcp_http.last_%'" | awk '{v[$1] = $2} END {print v["mcp_http.last_rows_retained"], v["mcp_http.last_bytes_retained"], v["mcp_http.last_rows_stored"]}'; }
wide_reply=$(call run_query '{"sql":"SELECT REPEAT(\"x\", 1000000) AS v FROM d1.big LIMIT 100"}')
expect "wide rows: the reply is bounded" "$(printf '%s' "$wide_reply" | grep -o '\\"v\\":' | wc -l) $(printf '%s' "$wide_reply" | grep -o 'truncated\\":true' | head -1)" "^[1-4] truncated"
# 1 MB values are charged 1,000,040 bytes each: the fifth row crosses 4 MiB and is not kept.
expect "wide rows: 4 rows retained (4 x 1,000,040 bytes)" "$(retained)" "^4 4000160 4$"
long_reply=$(call run_query '{"sql":"SELECT REPEAT(\"x\", 100) AS v FROM d1.big a, d1.big b LIMIT 1000000"}')
expect "LIMIT 1000000: the reply is bounded" "$(printf '%s' "$long_reply" | grep -o '\\"v\\":' | wc -l) $(printf '%s' "$long_reply" | grep -o 'truncated\\":true' | head -1)" "^1000 truncated"
expect "LIMIT 1000000: 1001 rows retained (x 140 bytes), not 1000000" "$(retained)" "^1001 140140 1001$"
expect "a NULL-only result is not cut" "$(call run_query '{"sql":"SELECT NULL AS z FROM d1.big LIMIT 3"}')" 'z\\":null\},\{\\"z\\":null\},\{\\"z\\":null\}\],\\"truncated\\":false'
# NULL cells count against the byte budget too (the DOM consumer keeps a pointer per cell): a row
# of 8000 NULLs is charged 64,032 bytes, so 65 rows fit in 4 MiB. Without NULL accounting all
# 1001 rows (64 MB) were kept. The reply's own 4 MB cut comes first either way.
nulls=$(seq -s, -f 'NULL AS n%g' 1 8000)
null_reply=$(call run_query "{\"sql\":\"SELECT $nulls FROM d1.big a, d1.big b LIMIT 5000\"}")
expect "8000 NULL columns: the reply is bounded and truncated" "$(printf '%s' "$null_reply" | grep -o 'n8000\\":null' | head -1) $(printf '%s' "$null_reply" | grep -o 'truncated\\":true' | head -1)" 'n8000\\":null truncated'
expect "8000 NULL columns: 65 rows retained (x 64,032 bytes)" "$(retained)" "^65 4162080 65$"
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
# With mcp.read_only off for the check, a write shows whether anything ran before the refusal.
sql "SET GLOBAL mcp.read_only = OFF"
sql "ALTER USER mcp@'%' ACCOUNT LOCK"
expect "a locked account is refused before execution" "$(call run_query '{"sql":"INSERT INTO d1.t VALUES (2)"}')" "Error 3118: Access denied for user 'mcp'@'%'. Account is locked.*\"isError\":true"
expect "a locked account does not write" "$(sql "SELECT GROUP_CONCAT(i ORDER BY i) FROM d1.t")" "^1$"
expect "a locked account is refused for tool pack calls too" "$(call list_databases '{}')" 'Error 3118: .*"isError":true'
expect "a failed ping is a result (success false), not a tool error" "$(call ping '{}')" 'success\\":false,.*Error 3118.*"isError":false'
sql "SET GLOBAL mcp.read_only = ON"
sql "ALTER USER mcp@'%' ACCOUNT UNLOCK"
expect "the account works again once unlocked" "$(call run_query '{"sql":"SELECT 1 AS one"}')" 'one\\":\\"1\\".*"isError":false'
expect "no MCP session left" "$(sql "SHOW GLOBAL STATUS LIKE 'mcp.sessions_connected'")" "	0$"

# Concurrency: mcp_http.threads (default 4) I/O workers serve calls at once.
expect "threads and session cap defaults" "$(sql "SELECT @@mcp_http.threads, @@mcp_http.max_sessions_per_account")" "^4	4$"
four=$(par 4 "SELECT SLEEP(2) AS s")
expect "4 concurrent calls all succeed" "$(printf '%s\n' "$four" | grep -o '"isError":false' | wc -l | tr -d ' ')" "^4$"
expect "4 concurrent calls overlap (4 x 2 s in under 4 s)" "$(printf '%s\n' "$four" | grep elapsed=)" "^elapsed=[0-3]$"
# max_user_connections does not apply to these sessions; mcp_http.max_sessions_per_account does.
sql "UNINSTALL COMPONENT 'file://component_mcp'" >/dev/null
expect "reinstall with a session cap of 2" "$(sql "INSTALL COMPONENT 'file://component_mcp' SET GLOBAL mcp_http.port = 33480, GLOBAL mcp_http.max_sessions_per_account = 2, GLOBAL mcp_http.account = 'mcp@%', GLOBAL mcp_http.token_sha256 = '$TOKEN_HEX'")" "^$"
capped=$(par 4 "SELECT SLEEP(2) AS s")
expect "cap 2: two of four concurrent calls succeed" "$(printf '%s\n' "$capped" | grep -o '"isError":false' | wc -l | tr -d ' ')" "^2$"
expect "cap 2: the others are refused (1203)" "$(printf '%s\n' "$capped" | grep -o "Error 1203: User mcp already has 2 active endpoint sessions (mcp_http.max_sessions_per_account)" | wc -l | tr -d ' ')" "^2$"
expect "cap 2: a session slot is free again afterwards" "$(call run_query '{"sql":"SELECT 1 AS one"}')" '"isError":false'
# The sessions count against max_connections and, unlike the command service's default, are
# subject to it: one idle client fills max_connections = 1.
sql "SET GLOBAL max_connections = 1"
sql "SELECT SLEEP(4)" >/dev/null &
sleep 1.5
expect "max_connections applies to endpoint sessions" "$(call run_query '{"sql":"SELECT 1 AS one"}')" 'Error 1040: Too many connections.*"isError":true'
wait
sql "SET GLOBAL max_connections = 151"
expect "a call works again below max_connections" "$(call run_query '{"sql":"SELECT 1 AS one"}')" '"isError":false'
expect "no MCP session left after concurrent calls" "$(sql "SHOW GLOBAL STATUS LIKE 'mcp.sessions_connected'")" "	0$"

# Token table (mcp_tokens.sql): bearer token hashes mapped to accounts, read through the locked
# account mysql.mcp@localhost. mcp_http.token_sha256 (tok-123 -> mcp@%) keeps working.
callas() { rpc "$1" "{\"jsonrpc\":\"2.0\",\"id\":9,\"method\":\"tools/call\",\"params\":{\"name\":\"$2\",\"arguments\":$3}}"; }
h() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }
expect "no token table yet: an unknown token gets 401" "$(callas tbl-mcp run_query '{"sql":"SELECT 1"}' | tail -1)" "^401$"
# A pre-existing, unlocked reader account with a password and other grants, granted as a role and
# as a proxy to another account: setup drops and recreates it, so none of that survives (run
# twice: the script is safe to run again; each run must succeed).
sql "CREATE USER 'mysql.mcp'@'localhost' IDENTIFIED BY 'guess'; GRANT SELECT ON d1.* TO 'mysql.mcp'@'localhost';
CREATE USER heir@'%'; GRANT 'mysql.mcp'@'localhost' TO heir@'%'"
# Only root@localhost holds PROXY ... WITH GRANT OPTION, so this grant goes over the socket.
docker exec "$C" mysql -uroot -ptestpass -e "GRANT PROXY ON 'mysql.mcp'@'localhost' TO heir@'%'" 2>&1 | grep -v "Using a password"
expect "fixture: the old reader is granted as a role" "$(sql "SELECT COUNT(*) FROM mysql.role_edges WHERE FROM_USER = 'mysql.mcp' AND TO_USER = 'heir'")" "^1$"
expect "fixture: and as a proxy" "$(sql "SELECT COUNT(*) FROM mysql.proxies_priv WHERE Proxied_user = 'mysql.mcp' AND User = 'heir'")" "^1$"
for run in 1 2; do
	setup_out=$(docker exec -i "$C" mysql -h127.0.0.1 -uroot -ptestpass <"$REPO/src/mcp/endpoint/mcp_tokens.sql" 2>&1)
	expect "token setup run $run succeeds" "$? $(printf '%s' "$setup_out" | grep -v 'Using a password')" "^0 ?$"
done
expect "setup locks the reader account" "$(sql "SELECT account_locked FROM mysql.user WHERE user = 'mysql.mcp'")" "^Y$"
expect "setup leaves the reader only SELECT on the table" "$(sql "SHOW GRANTS FOR 'mysql.mcp'@'localhost'" | tr -d '`' | tr '\n' ';')" "^GRANT USAGE ON [*][.][*] TO mysql.mcp@localhost;GRANT SELECT ON mysql.mcp_tokens TO mysql.mcp@localhost;$"
expect "no account inherits the reader as a role" "$(sql "SELECT COUNT(*) FROM mysql.role_edges WHERE FROM_USER = 'mysql.mcp'")" "^0$"
expect "no account can proxy as the reader (DROP USER leaves proxy grants)" "$(sql "SELECT COUNT(*) FROM mysql.proxies_priv WHERE Proxied_user = 'mysql.mcp'")" "^0$"
sql "INSERT INTO mysql.mcp_tokens (token_sha256, account, expires_at, comment) VALUES
  ('$(h tbl-mcp)', 'mcp@%', NULL, 'test'), ('$(h tbl-app)', 'app@%', NOW() + INTERVAL 1 DAY, 'test'),
  ('$(h tbl-old)', 'mcp@%', NOW() - INTERVAL 1 SECOND, 'expired')"
expect "a table token runs as its account" "$(callas tbl-mcp run_query '{"sql":"SELECT CURRENT_USER() AS who"}')" 'who\\":\\"mcp@%\\".*"isError":false'
expect "another table token, another account" "$(callas tbl-app run_query '{"sql":"SELECT CURRENT_USER() AS who"}')" 'who\\":\\"app@%\\".*"isError":false'
expect "the mapped account's policy applies (guard, MCP_USER)" "$(callas tbl-mcp run_query '{"sql":"INSERT INTO d1.t VALUES (5)"}')" 'Error 1290: [^"]*mcp.read_only.*"isError":true'
expect "an expired token gets 401" "$(callas tbl-old run_query '{"sql":"SELECT 1"}' | tail -1)" "^401$"
expect "an unknown token gets 401" "$(callas tbl-none run_query '{"sql":"SELECT 1"}' | tail -1)" "^401$"
expect "the single token still maps to mcp_http.account" "$(call run_query '{"sql":"SELECT CURRENT_USER() AS who"}')" 'who\\":\\"mcp@%\\"'
expect "MCP sessions cannot read the token table" "$(callas tbl-mcp run_query '{"sql":"SELECT COUNT(*) FROM mysql.mcp_tokens"}')" 'Error 1290: [^"]*mcp.deny_system_schemas.*"isError":true'
expect "nobody can log in as the reader account, not even with its old password" "$(docker exec "$C" mysql -umysql.mcp -pguess -N -e 'SELECT 1' 2>&1 | grep -v 'Using a password')" "ERROR (1045|3118)"
sql "DELETE FROM mysql.mcp_tokens WHERE token_sha256 = '$(h tbl-mcp)'"
expect "a deleted token gets 401 on the next request" "$(callas tbl-mcp run_query '{"sql":"SELECT 1"}' | tail -1)" "^401$"
# RFC 0001 §7.4: a token never runs as an account with SUPER, SYSTEM_USER or MCP_ADMIN, whatever
# maps it (checked on every call; these accounts would not be restricted by the guard).
sql "CREATE USER sysu@'%'; GRANT SELECT, INSERT ON d1.* TO sysu@'%'; GRANT SYSTEM_USER ON *.* TO sysu@'%';
CREATE USER adm@'%'; GRANT SELECT, INSERT ON d1.* TO adm@'%'; GRANT MCP_ADMIN ON *.* TO adm@'%';
INSERT INTO mysql.mcp_tokens (token_sha256, account) VALUES
  ('$(h tbl-root)', 'root@%'), ('$(h tbl-sysu)', 'sysu@%'), ('$(h tbl-adm)', 'adm@%')"
expect "a token mapped to root is refused (SUPER)" "$(callas tbl-root run_query '{"sql":"INSERT INTO d1.t VALUES (8)"}')" 'Error 1227: [^"]*does not run as an account with SUPER, SYSTEM_USER or MCP_ADMIN \(it has SUPER\).*"isError":true'
expect "a token mapped to a SYSTEM_USER account is refused" "$(callas tbl-sysu run_query '{"sql":"INSERT INTO d1.t VALUES (8)"}')" 'Error 1227: [^"]*\(it has SYSTEM_USER\).*"isError":true'
expect "a token mapped to an MCP_ADMIN account is refused" "$(callas tbl-adm run_query '{"sql":"INSERT INTO d1.t VALUES (8)"}')" 'Error 1227: [^"]*\(it has MCP_ADMIN\).*"isError":true'
expect "nothing written by table tokens" "$(sql "SELECT GROUP_CONCAT(i) FROM d1.t")" "^1$"

# Streamable HTTP (MCP transport spec): protocol version negotiation and header, Mcp-Session-Id
# sessions bound to the token's account, DELETE, and the request header checks.
# req TOKEN METHOD [curl args...]: status line, headers and body of one request.
req() { t=$1; m=$2; shift 2; docker run --rm --network "container:$C" "$CURL_IMAGE" -s -m 20 -i -X "$m" \
	http://127.0.0.1:33480/mcp -H "Authorization: Bearer $t" "$@" | tr -d '\r'; }
INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25"}}'
CALL='{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"run_query","arguments":{"sql":"SELECT 1 AS one"}}}'
J=(-H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream')
init=$(req tok-123 POST "${J[@]}" -d "$INIT")
SID=$(printf '%s\n' "$init" | awk -F': ' 'tolower($1) == "mcp-session-id" {print $2}')
expect "initialize issues an Mcp-Session-Id" "$SID" "^[0-9a-f]{32}$"
expect "initialize keeps a supported version (2025-11-25)" "$init" '"protocolVersion":"2025-11-25"'
expect "initialize answers an unsupported version with the newest supported" "$(req tok-123 POST "${J[@]}" -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"1999-01-01"}}')" '"protocolVersion":"2025-11-25"'
expect "initialize keeps 2024-11-05" "$(req tok-123 POST "${J[@]}" -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}')" '"protocolVersion":"2024-11-05"'
expect "a call in the session" "$(req tok-123 POST "${J[@]}" -H "Mcp-Session-Id: $SID" -H 'MCP-Protocol-Version: 2025-11-25' -d "$CALL")" 'one\\":\\"1\\".*"isError":false'
badver=$(req tok-123 POST "${J[@]}" -H "Mcp-Session-Id: $SID" -H 'MCP-Protocol-Version: 1999-01-01' -d "$CALL")
expect "an unsupported MCP-Protocol-Version: 400" "$(printf '%s\n' "$badver" | head -1)" "^HTTP/1.1 400"
expect "the 400 is JSON-RPC -32022 listing the supported versions (no id: the body is not read)" "$(printf '%s\n' "$badver" | tail -1)" '^\{"jsonrpc":"2.0","error":\{"code":-32022,"message":"Unsupported protocol version \(supported versions: 2026-07-28, 2025-11-25, 2025-06-18, 2025-03-26, 2024-11-05\)","data":\{"supported":\["2026-07-28","2025-11-25","2025-06-18","2025-03-26","2024-11-05"\],"requested":"1999-01-01"\}\}\}$'
expect "an unknown session: 404" "$(req tok-123 POST "${J[@]}" -H 'Mcp-Session-Id: 00000000000000000000000000000000' -d "$CALL" | head -1)" "^HTTP/1.1 404"
expect "another account's token on the session: 403" "$(req tbl-app POST "${J[@]}" -H "Mcp-Session-Id: $SID" -d "$CALL" | head -1)" "^HTTP/1.1 403"
expect "a request without a session id is still served" "$(req tok-123 POST "${J[@]}" -d "$CALL")" '"isError":false'
expect "DELETE without a session id: 400" "$(req tok-123 DELETE | head -1)" "^HTTP/1.1 400"
expect "DELETE by another account: 403" "$(req tbl-app DELETE -H "Mcp-Session-Id: $SID" | head -1)" "^HTTP/1.1 403"
expect "DELETE ends the session: 204" "$(req tok-123 DELETE -H "Mcp-Session-Id: $SID" | head -1)" "^HTTP/1.1 204"
expect "the ended session: 404" "$(req tok-123 POST "${J[@]}" -H "Mcp-Session-Id: $SID" -d "$CALL" | head -1)" "^HTTP/1.1 404"
expect "DELETE needs the token: 401" "$(req nope DELETE -H "Mcp-Session-Id: $SID" | head -1)" "^HTTP/1.1 401"
expect "a non-JSON Content-Type: 415" "$(req tok-123 POST -H 'Content-Type: text/plain' -H 'Accept: */*' -d "$CALL" | head -1)" "^HTTP/1.1 415"
expect "Accept without text/event-stream: 400" "$(req tok-123 POST -H 'Content-Type: application/json' -H 'Accept: application/json' -d "$CALL" | head -1)" "^HTTP/1.1 400"
expect "Accept wildcards are accepted" "$(req tok-123 POST -H 'Content-Type: application/json; charset=utf-8' -H 'Accept: application/*;q=0.9, text/*' -d "$CALL")" '"isError":false'
expect "GET (no SSE stream): 405, Allow: POST, DELETE" "$(req tok-123 GET -H 'Accept: text/event-stream' | grep -iE '^HTTP|^allow' | tr '\n' ' ')" "^HTTP/1.1 405.*[Aa]llow: POST, DELETE"
# Sessions per account: at most 64; a new one replaces that account's least recently used, and
# never another account's session (repeated initialize must not evict other accounts).
OTHER=$(req tbl-app POST "${J[@]}" -d "$INIT" | awk -F': ' 'tolower($1) == "mcp-session-id" {print $2}')
many=$(docker run --rm --network "container:$C" --entrypoint sh "$CURL_IMAGE" -c '
	i=0; while [ $i -lt 70 ]; do
		curl -s -m 10 -D - -o /dev/null -X POST http://127.0.0.1:33480/mcp -H "Authorization: Bearer tok-123" \
			-H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d "$0" |
			tr -d "\r" | awk -F": " "tolower(\$1) == \"mcp-session-id\" {print \$2}"
		i=$((i + 1))
	done' "$INIT")
FIRST=$(printf '%s\n' "$many" | head -1); LAST=$(printf '%s\n' "$many" | tail -1)
expect "70 initializes issue 70 session ids" "$(printf '%s\n' "$many" | grep -c .)" "^70$"
expect "the account's oldest sessions were replaced (404)" "$(req tok-123 POST "${J[@]}" -H "Mcp-Session-Id: $FIRST" -d "$CALL" | head -1)" "^HTTP/1.1 404"
expect "its newest session works" "$(req tok-123 POST "${J[@]}" -H "Mcp-Session-Id: $LAST" -d "$CALL" | head -1)" "^HTTP/1.1 200"
expect "another account's session survives them" "$(req tbl-app POST "${J[@]}" -H "Mcp-Session-Id: $OTHER" -d "$CALL" | head -1)" "^HTTP/1.1 200"
# Deeply nested JSON: rapidjson is recursive, so the endpoint refuses deep nesting before parsing
# (a body of half a million '[' crashed mysqld before the depth check).
deep=$(docker run --rm --network "container:$C" --entrypoint sh "$CURL_IMAGE" -c '
	printf "%0500000d" 0 | tr 0 "[" >/tmp/deep.json
	curl -s -m 30 -w "\n%{http_code}" -X POST http://127.0.0.1:33480/mcp -H "Content-Type: application/json" \
		-H "Accept: application/json, text/event-stream" -H "Authorization: Bearer tok-123" --data-binary @/tmp/deep.json')
expect "500 000 nested arrays: a parse error, not a crash" "$(printf '%s\n' "$deep" | tr '\n' ' ')" '"code":-32700,"message":"parse error: JSON nested too deeply".* 200 $'
expect "the server is still up" "$(alive)" "^up$"
expect "64 levels are accepted, 65 refused" "$(rpc tok-123 "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\",\"params\":{\"x\":$(printf '%062d' 0 | tr 0 '[')$(printf '%062d' 0 | tr 0 ']')}}" | head -1) $(rpc tok-123 "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\",\"params\":{\"x\":$(printf '%063d' 0 | tr 0 '[')$(printf '%063d' 0 | tr 0 ']')}}" | head -1)" '^\{"jsonrpc":"2.0","id":1,"result":\{"tools".* \{"jsonrpc":"2.0","id":null,"error":\{"code":-32700'
expect "brackets inside strings do not count" "$(call run_query "{\"sql\":\"SELECT '$(printf '%0200d' 0 | tr 0 '[')' AS b\"}" | head -1)" '"isError":false'
expect "session timeout default" "$(sql "SELECT @@mcp_http.session_timeout_minutes")" "^30$"

# The stateless revision 2026-07-28 (SEP-2575), on the same endpoint: no handshake or session;
# every request carries its version and client capabilities in _meta, and the MCP-Protocol-Version,
# Mcp-Method and Mcp-Name headers mirror the body.
# sl METHOD PARAMS [curl args...]: one stateless-era request with the usual headers and _meta.
META='"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{},"io.modelcontextprotocol/clientInfo":{"name":"test","version":"1"}}'
sl() { m=$1; p=$2; shift 2; req tok-123 POST "${J[@]}" -H 'MCP-Protocol-Version: 2026-07-28' -H "Mcp-Method: $m" "$@" \
	-d "{\"jsonrpc\":\"2.0\",\"id\":7,\"method\":\"$m\",\"params\":{$META${p:+,$p}}}"; }
SLQ='"name":"run_query","arguments":{"sql":"SELECT 1 AS one"}'
disc=$(sl server/discover '')
expect "server/discover: 200, without an Mcp-Session-Id" "$(printf '%s\n' "$disc" | grep -ciE '^HTTP/1.1 200|^mcp-session-id' | tr -d ' ')" "^1$"
expect "server/discover: every supported version, newest first" "$disc" '"supportedVersions":\["2026-07-28","2025-11-25","2025-06-18","2025-03-26","2024-11-05"\],"capabilities":\{"tools":\{\}\}'
expect "server/discover: resultType, cache hints, serverInfo in _meta" "$disc" '"resultType":"complete","ttlMs":3600000,"cacheScope":"public","_meta":\{"io.modelcontextprotocol/serverInfo":\{"name":"component_mcp","version":"[^"]+"\}\}'
expect "tools/list: the six tools, with cache hints" "$(sl tools/list '' | tail -1 | grep -o '"name":"[a-z_]*"\|"ttlMs":[0-9]*\|"cacheScope":"[a-z]*"' | tr '\n' ' ')" '^"name":"list_databases" "name":"list_tables" "name":"describe_table" "name":"run_query" "name":"ping" "name":"server_info" "ttlMs":3600000 "cacheScope":"public" "name":"component_mcp" $'
slcall=$(sl tools/call "$SLQ" -H 'Mcp-Name: run_query')
expect "tools/call: served, resultType complete" "$slcall" 'one\\":\\"1\\".*"isError":false,"resultType":"complete","_meta":\{"io.modelcontextprotocol/serverInfo"'
expect "tools/call: no ttlMs on a call result, no Mcp-Session-Id" "$(printf '%s\n' "$slcall" | grep -ciE 'ttlMs|^mcp-session-id' | tr -d ' ')" "^0$"
expect "tools/call: the guard still applies" "$(sl tools/call '"name":"run_query","arguments":{"sql":"INSERT INTO d1.t VALUES (9)"}' -H 'Mcp-Name: run_query')" '"isError":true,"resultType":"complete"'
expect "an Mcp-Session-Id is ignored, not looked up" "$(sl tools/call "$SLQ" -H 'Mcp-Name: run_query' -H 'Mcp-Session-Id: 00000000000000000000000000000000' | head -1)" "^HTTP/1.1 200"
expect "an unknown tool: -32602" "$(sl tools/call '"name":"nope","arguments":{}' -H 'Mcp-Name: nope')" '"error":\{"code":-32602,"message":"Unknown tool: nope"\}'
expect "ping is not a 2026-07-28 method: 404, -32601" "$(sl ping '' | sed -n '1p;$p' | tr '\n' ' ')" '^HTTP/1.1 404.*"code":-32601'
expect "neither is initialize: 404, -32601" "$(sl initialize '' | sed -n '1p;$p' | tr '\n' ' ')" '^HTTP/1.1 404.*"code":-32601'
expect "no MCP-Protocol-Version header: 400, -32020" "$(req tok-123 POST "${J[@]}" -H 'Mcp-Method: tools/list' -d "{\"jsonrpc\":\"2.0\",\"id\":7,\"method\":\"tools/list\",\"params\":{$META}}" | sed -n '1p;$p' | tr '\n' ' ')" '^HTTP/1.1 400.*"id":7,"error":\{"code":-32020'
expect "a 2026-07-28 header without _meta: 400, -32602" "$(req tok-123 POST "${J[@]}" -H 'MCP-Protocol-Version: 2026-07-28' -H 'Mcp-Method: tools/list' -d '{"jsonrpc":"2.0","id":7,"method":"tools/list"}' | sed -n '1p;$p' | tr '\n' ' ')" '^HTTP/1.1 400.*"code":-32602,"message":"missing or invalid _meta field io.modelcontextprotocol/protocolVersion"'
expect "header and _meta versions differ: 400, -32020" "$(req tok-123 POST "${J[@]}" -H 'MCP-Protocol-Version: 2025-11-25' -H 'Mcp-Method: tools/list' -d "{\"jsonrpc\":\"2.0\",\"id\":7,\"method\":\"tools/list\",\"params\":{$META}}" | sed -n '1p;$p' | tr '\n' ' ')" '^HTTP/1.1 400.*"code":-32020'
expect "an unsupported newer version: 400, -32022 naming the supported ones, before the body" "$(req tok-123 POST "${J[@]}" -H 'MCP-Protocol-Version: 2099-01-01' -H 'Mcp-Method: server/discover' -d '{"jsonrpc":"2.0","id":7,"method":"server/discover","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2099-01-01","io.modelcontextprotocol/clientCapabilities":{}}}}' | sed -n '1p;$p' | tr '\n' ' ')" '^HTTP/1.1 400.*"code":-32022,.*"data":\{"supported":\["2026-07-28","2025-11-25","2025-06-18","2025-03-26","2024-11-05"\],"requested":"2099-01-01"\}'
expect "no clientCapabilities: 400, -32602" "$(req tok-123 POST "${J[@]}" -H 'MCP-Protocol-Version: 2026-07-28' -H 'Mcp-Method: tools/list' -d '{"jsonrpc":"2.0","id":7,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28"}}}' | sed -n '1p;$p' | tr '\n' ' ')" '^HTTP/1.1 400.*"code":-32602,"message":"missing or invalid _meta field io.modelcontextprotocol/clientCapabilities"'
expect "a malformed clientInfo: 400, -32602" "$(req tok-123 POST "${J[@]}" -H 'MCP-Protocol-Version: 2026-07-28' -H 'Mcp-Method: tools/list' -d '{"jsonrpc":"2.0","id":7,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{},"io.modelcontextprotocol/clientInfo":"x"}}}' | sed -n '1p;$p' | tr '\n' ' ')" '^HTTP/1.1 400.*"code":-32602'
expect "no Mcp-Method header: 400, -32020" "$(req tok-123 POST "${J[@]}" -H 'MCP-Protocol-Version: 2026-07-28' -d "{\"jsonrpc\":\"2.0\",\"id\":7,\"method\":\"tools/list\",\"params\":{$META}}" | sed -n '1p;$p' | tr '\n' ' ')" '^HTTP/1.1 400.*"code":-32020,"message":"Mcp-Method header is required"'
expect "Mcp-Method differs from the method: 400, -32020" "$(sl tools/list '' -H 'Mcp-Method: tools/call' | sed -n '1p;$p' | tr '\n' ' ')" '^HTTP/1.1 400.*"code":-32020'
expect "tools/call without Mcp-Name: 400, -32020" "$(sl tools/call "$SLQ" | sed -n '1p;$p' | tr '\n' ' ')" '^HTTP/1.1 400.*"code":-32020,"message":"Mcp-Name header is required for tools/call"'
expect "Mcp-Name differs from the tool: 400, -32020" "$(sl tools/call "$SLQ" -H 'Mcp-Name: ping' | sed -n '1p;$p' | tr '\n' ' ')" '^HTTP/1.1 400.*"code":-32020'
expect "jsonrpc other than 2.0: 400, -32600" "$(req tok-123 POST "${J[@]}" -H 'MCP-Protocol-Version: 2026-07-28' -H 'Mcp-Method: tools/call' -H 'Mcp-Name: run_query' -d "{\"jsonrpc\":\"1.0\",\"id\":7,\"method\":\"tools/call\",\"params\":{$META,$SLQ}}" | sed -n '1p;$p' | tr '\n' ' ')" '^HTTP/1.1 400.*"code":-32600,"message":"jsonrpc must be \\"2.0\\""'
SID2=$(req tok-123 POST "${J[@]}" -d "$INIT" | awk -F': ' 'tolower($1) == "mcp-session-id" {print $2}')
expect "a stateless DELETE: 405" "$(req tok-123 DELETE -H 'MCP-Protocol-Version: 2026-07-28' -H "Mcp-Session-Id: $SID2" | head -1)" "^HTTP/1.1 405"
expect "... and the session-era session it named is untouched" "$(req tok-123 POST "${J[@]}" -H "Mcp-Session-Id: $SID2" -d "$CALL" | head -1)" "^HTTP/1.1 200"
expect "a large unsigned integer id is served" "$(req tok-123 POST "${J[@]}" -H 'MCP-Protocol-Version: 2026-07-28' -H 'Mcp-Method: tools/list' -d "{\"jsonrpc\":\"2.0\",\"id\":18446744073709551615,\"method\":\"tools/list\",\"params\":{$META}}" | sed -n '1p;$p' | tr '\n' ' ')" '^HTTP/1.1 200 .*"id":18446744073709551615,"result"'
expect "a fractional id: 400, -32600" "$(req tok-123 POST "${J[@]}" -H 'MCP-Protocol-Version: 2026-07-28' -H 'Mcp-Method: tools/list' -d "{\"jsonrpc\":\"2.0\",\"id\":1.5,\"method\":\"tools/list\",\"params\":{$META}}" | sed -n '1p;$p' | tr '\n' ' ')" '^HTTP/1.1 400.*"code":-32600'
expect "a null request id: 400, -32600" "$(req tok-123 POST "${J[@]}" -H 'MCP-Protocol-Version: 2026-07-28' -H 'Mcp-Method: tools/list' -d "{\"jsonrpc\":\"2.0\",\"id\":null,\"method\":\"tools/list\",\"params\":{$META}}" | sed -n '1p;$p' | tr '\n' ' ')" '^HTTP/1.1 400.*"code":-32600'
expect "a stateless request needs the token: 401" "$(req nope POST "${J[@]}" -H 'MCP-Protocol-Version: 2026-07-28' -H 'Mcp-Method: tools/list' -d "{\"jsonrpc\":\"2.0\",\"id\":7,\"method\":\"tools/list\",\"params\":{$META}}" | head -1)" "^HTTP/1.1 401"
expect "a notification with the 2026-07-28 header: 202" "$(req tok-123 POST "${J[@]}" -H 'MCP-Protocol-Version: 2026-07-28' -d '{"jsonrpc":"2.0","method":"notifications/cancelled"}' | head -1)" "^HTTP/1.1 202"
expect "a session-era version in _meta and the header is served as that era" "$(req tok-123 POST "${J[@]}" -H 'MCP-Protocol-Version: 2025-11-25' -d '{"jsonrpc":"2.0","id":7,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2025-11-25"}}}' | tail -1)" '^\{"jsonrpc":"2.0","id":7,"result":\{"tools":.*\]\}\}$'

# An account without MCP_USER is not treated as an MCP session (the token maps to app).
sql "UNINSTALL COMPONENT 'file://component_mcp'" >/dev/null
expect "reinstall mapped to app, with allowed origins and masked columns" "$(sql "INSTALL COMPONENT 'file://component_mcp' SET GLOBAL mcp_http.port = 33480, GLOBAL mcp_http.account = 'app@%', GLOBAL mcp_http.token_sha256 = '$TOKEN_HEX', GLOBAL mcp_http.allowed_origins = 'http://localhost:6274, https://Inspector.example', GLOBAL mcp_http.mask_columns = ' secret, PASS ,, ÉMAIL,	пароль , token '")" "^$"
# Column masking, as mysql-mcp-server's MYSQL_MCP_MASK_COLUMNS: by column name, substring,
# case-insensitive; NULL stays NULL.
expect "masked columns: values replaced, NULL kept, others untouched" "$(call run_query '{"sql":"SELECT \"a\" AS user_password, \"b\" AS SecretKey, NULL AS secret_null, \"c\" AS ok"}')" 'user_password\\":\\"\*\*\*\*\*\*\*\*\\",\\"SecretKey\\":\\"\*\*\*\*\*\*\*\*\\",\\"secret_null\\":null,\\"ok\\":\\"c\\"'
expect "masked columns: binary values are masked too" "$(call run_query '{"sql":"SELECT X'"'"'FF'"'"' AS pass_hash"}')" 'pass_hash\\":\\"\*\*\*\*\*\*\*\*\\"\}'
# As Go's strings.ToLower / TrimSpace: Unicode case (ÉMAIL, пароль) and whitespace (a tab, and a
# no-break space U+00A0 after "token") in the patterns.
expect "masked columns: Unicode case and whitespace as in Go" "$(call run_query '{"sql":"SELECT \"a\" AS `émail`, \"b\" AS `ПАРОЛЬ`, \"c\" AS api_token, \"d\" AS ok"}')" 'émail\\":\\"\*{8}\\",\\"ПАРОЛЬ\\":\\"\*{8}\\",\\"api_token\\":\\"\*{8}\\",\\"ok\\":\\"d\\"'
expect "masked columns: lowercase that grows in UTF-8 (U+023E) still matches" "$(call run_query '{"sql":"SELECT \"a\" AS `Ⱦsecret`"}')" 'Ⱦsecret\\":\\"\*{8}\\"'
expect "an allowed Origin passes" "$(rpc_origin http://localhost:6274 '{"jsonrpc":"2.0","id":1,"method":"ping"}')" '"result":\{\}'
expect "allowed origins match case-insensitively, spaces trimmed" "$(rpc_origin https://inspector.example '{"jsonrpc":"2.0","id":1,"method":"ping"}')" '"result":\{\}'
# CORS for an allowed browser origin: the preflight is answered and replies name the origin.
preflight=$(curl_i -X OPTIONS -H 'Origin: http://localhost:6274' -H 'Access-Control-Request-Method: POST' -H 'Access-Control-Request-Headers: authorization, content-type')
expect "preflight from an allowed Origin: 204" "$(printf '%s\n' "$preflight" | head -1)" "^HTTP/1.1 204"
expect "preflight allows POST with Authorization" "$(printf '%s\n' "$preflight" | grep -i '^access-control-' | tr 'A-Z' 'a-z' | sort | tr '\n' ' ')" "access-control-allow-headers: authorization, content-type.*access-control-allow-methods: post, delete.*access-control-allow-origin: http://localhost:6274"
expect "a POST reply names the allowed Origin" "$(curl_i -X POST -H 'Origin: http://localhost:6274' -H 'Authorization: Bearer tok-123' -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"ping"}' | grep -i '^access-control-allow-origin')" "^[Aa]ccess-[Cc]ontrol-[Aa]llow-[Oo]rigin: http://localhost:6274$"
expect "replies to an allowed Origin expose Mcp-Session-Id" "$(curl_i -X POST -H 'Origin: http://localhost:6274' -H 'Authorization: Bearer tok-123' -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | grep -i '^access-control-expose-headers')" "^[Aa]ccess-[Cc]ontrol-[Ee]xpose-[Hh]eaders: Mcp-Session-Id$"
expect "a 401 for an allowed Origin is readable by the page" "$(curl_i -X POST -H 'Origin: http://localhost:6274' -H 'Content-Type: application/json' -d '{}' | grep -ci '^access-control-allow-origin: http://localhost:6274')" "^1$"
foreign=$(curl_i -X OPTIONS -H 'Origin: http://evil.example' -H 'Access-Control-Request-Method: POST')
expect "preflight from another Origin: 403" "$(printf '%s\n' "$foreign" | head -1)" "^HTTP/1.1 403"
expect "preflight from another Origin: no CORS headers" "$(printf '%s\n' "$foreign" | grep -ci '^access-control-')" "^0$"
expect "OPTIONS without Origin: 405" "$(curl_i -X OPTIONS | head -1)" "^HTTP/1.1 405"
expect "a prefix of an allowed Origin is refused" "$(rpc_origin http://localhost:62 '{"jsonrpc":"2.0","id":1,"method":"ping"}')" "^403$"
expect "another Origin is still refused" "$(rpc_origin http://evil.example '{"jsonrpc":"2.0","id":1,"method":"ping"}')" "^403$"
expect "guard: an account without MCP_USER is not restricted" "$(call run_query '{"sql":"INSERT INTO d1.t VALUES (7)"}')" '"isError":false'
expect "the guard cannot be uninstalled while the endpoint is installed" "$(sql "UNINSTALL COMPONENT 'file://component_mcp_guard'")" "ERROR"
expect "uninstall component_mcp" "$(sql "UNINSTALL COMPONENT 'file://component_mcp'")" "^$"

# TLS (RFC 0001 §5.3): plain HTTP only on loopback. The server's auto-generated certificate
# in the datadir serves as the endpoint's.
INSTALL_MCP="INSTALL COMPONENT 'file://component_mcp' SET GLOBAL mcp_http.port = 33480, GLOBAL mcp_http.account = 'mcp@%', GLOBAL mcp_http.token_sha256 = '$TOKEN_HEX'"
expect "a non-loopback address without TLS is refused" "$(sql "$INSTALL_MCP, GLOBAL mcp_http.bind_address = '0.0.0.0'")" "ERROR"
expect "a certificate without a key is refused" "$(sql "$INSTALL_MCP, GLOBAL mcp_http.ssl_cert = '/var/lib/mysql/server-cert.pem'")" "ERROR"
expect "localhost is loopback: installs without TLS" "$(sql "$INSTALL_MCP, GLOBAL mcp_http.bind_address = 'localhost'")" "^$"
expect "localhost binds 127.0.0.1" "$(rpc tok-123 '{"jsonrpc":"2.0","id":1,"method":"ping"}')" '"result":\{\}'
IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$C")
expect "a localhost install is not reachable from another host" "$(docker run --rm "$CURL_IMAGE" -s -m 5 -o /dev/null -w '%{http_code}' -X POST "http://$IP:33480/mcp" -H 'Authorization: Bearer tok-123' -d '{}' || true)" "^000$"
sql "UNINSTALL COMPONENT 'file://component_mcp'" >/dev/null
expect "install on 0.0.0.0 with TLS" "$(sql "$INSTALL_MCP, GLOBAL mcp_http.bind_address = '0.0.0.0', GLOBAL mcp_http.ssl_cert = '/var/lib/mysql/server-cert.pem', GLOBAL mcp_http.ssl_key = '/var/lib/mysql/server-key.pem'")" "^$"
https_ping() { docker run --rm ${2:+--network "$2"} "$CURL_IMAGE" -sk -m 20 -w '\n%{http_code}' -X POST "$1" \
	-H 'Content-Type: application/json' -H 'Authorization: Bearer tok-123' -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'; }
expect "HTTPS on loopback" "$(https_ping https://127.0.0.1:33480/mcp "container:$C")" '"result":\{\}'
expect "plain HTTP to the TLS port gets no reply" "$(https_ping http://127.0.0.1:33480/mcp "container:$C" | tail -1)" "^(000|400)$"
expect "HTTPS from another host (container $IP)" "$(https_ping "https://$IP:33480/mcp")" '"result":\{\}'
expect "uninstall the TLS endpoint" "$(sql "UNINSTALL COMPONENT 'file://component_mcp'")" "^$"
expect "then the guard can be uninstalled" "$(sql "UNINSTALL COMPONENT 'file://component_mcp_guard'")" "^$"
expect "server alive at the end" "$(alive)" "^up$"
echo "== $IMAGE: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
