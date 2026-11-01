#!/usr/bin/env bash
# Tests component_mcp_guard (RFC 0001 in askdba/mysql-mcp-server, Phases 0 and 2) against an
# official MySQL image: lifecycle (install, restart, uninstall), MCP session identity,
# read-only and schema enforcement, the mcp.* system variables and status variables.
#
# Usage: scripts/test-mcp-guard.sh <mysql-image-tag> <artifact-dir>
#   e.g. scripts/test-mcp-guard.sh 9.7 dist/mcp-guard-9.7.2
# The artifact dir holds component_mcp_guard.so and the test-only component_mcp_session_probe.so
# (both built with MYVECTOR_BUILD_MCP=ON).
# Requires Docker. bash 3.2+.
set -u

TAG=${1:?mysql image tag, e.g. 9.7}
ART=${2:?artifact dir containing component_mcp_guard.so}
SO=$ART/component_mcp_guard.so
[ -f "$SO" ] || { echo "missing $SO"; exit 1; }
# Test-only component that opens command-service sessions as component_mcp does.
PROBE=$ART/component_mcp_session_probe.so
[ -f "$PROBE" ] || { echo "missing $PROBE"; exit 1; }
REPO=$(cd "$(dirname "$0")/.." && pwd)
CNAME=mcp-guard-test-$(printf '%s' "$TAG" | tr -c 'a-zA-Z0-9.-' '-')-$$
PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); printf '  PASS  %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf '  FAIL  %s  -- %s\n' "$1" "$2"; }
CNAME_LCTN=$CNAME-lctn
cleanup() { docker rm -f -v "$CNAME" "$CNAME_LCTN" >/dev/null 2>&1; }
trap cleanup EXIT

# as USER PASS SQL: run SQL over TCP; prints rows (tab-separated) or the client error.
as() { docker exec -i "$CNAME" mysql -h127.0.0.1 -u"$1" -p"$2" -N --batch -e "$3" 2>&1 | grep -v "Using a password"; }
sql() { as root testpass "$1"; }
# Over TCP, so readiness cannot be reported by the entrypoint's temporary init server.
wait_ready() {
	for _ in $(seq 1 90); do
		docker exec "$CNAME" mysql -h127.0.0.1 -uroot -ptestpass -e 'select 1' >/dev/null 2>&1 && return 0
		sleep 2
	done
	return 1
}
# expect LABEL OUTPUT PATTERN: PATTERN is a grep -E regex; "^$" means no output.
expect() {
	if printf '%s\n' "$2" | grep -Eq -- "$3"; then ok "$1"; else bad "$1" "got: $(printf '%s' "$2" | head -c 200)"; fi
}
denied() { expect "$1" "$2" "ERROR 1290 .*$3"; }

echo "== mysql:$TAG"
docker run -d --name "$CNAME" -e MYSQL_ROOT_PASSWORD=testpass "mysql:$TAG" >/dev/null || exit 1
wait_ready || { echo "server did not start"; exit 1; }
echo "   server: $(sql 'select version()')"

PLUGIN_DIR=$(sql 'select @@plugin_dir')
docker cp "$SO" "$CNAME:$PLUGIN_DIR/component_mcp_guard.so" &&
	docker exec -u 0 "$CNAME" chmod 0644 "$PLUGIN_DIR/component_mcp_guard.so"
docker cp "$PROBE" "$CNAME:$PLUGIN_DIR/component_mcp_session_probe.so" &&
	docker exec -u 0 "$CNAME" chmod 0644 "$PLUGIN_DIR/component_mcp_session_probe.so"
out=$(docker exec -i "$CNAME" mysql -h127.0.0.1 -uroot -ptestpass <"$REPO/sql/mcp/mysql_mcp.sql" 2>&1 | grep -v "Using a password")
[ -z "$out" ] && [ "$(sql "CALL mysql_mcp.version()")" = "1.0.0" ] || { echo "tool pack import failed: $out"; exit 1; }

# --- lifecycle -----------------------------------------------------------------------------
expect "INSTALL COMPONENT" "$(sql "INSTALL COMPONENT 'file://component_mcp_guard'")" "^$"
privs=$(sql "SHOW PRIVILEGES" | awk '$1=="MCP_USER" || $1=="MCP_ADMIN" {print $1}' | sort | tr '\n' ',')
expect "SHOW PRIVILEGES lists MCP_ADMIN, MCP_USER" "$privs" "^MCP_ADMIN,MCP_USER,$"
expect "registered in mysql.component" "$(sql "SELECT component_urn FROM mysql.component")" "component_mcp_guard"
expect "system variables with defaults" "$(sql "SELECT CONCAT_WS(',', @@mcp.enabled, @@mcp.read_only, @@mcp.deny_system_schemas, IFNULL(@@mcp.allowed_schemas, ''))")" "^1,1,1,$"

# --- fixtures ------------------------------------------------------------------------------
# mcp: an MCP account that (wrongly) also has write grants, so the guard must stop writes.
# dba: MCP_USER and MCP_ADMIN (as after GRANT ALL): never treated as an MCP session.
# app: no MCP_USER: unaffected.
out=$(sql "
CREATE DATABASE d1; CREATE DATABASE d2;
CREATE TABLE d1.t (i INT); CREATE TABLE d2.t (i INT); INSERT INTO d1.t VALUES (1);
CREATE USER mcp@'%' IDENTIFIED BY 'mcppass1';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, EXECUTE, LOCK TABLES ON d1.* TO mcp@'%';
GRANT SELECT ON d2.* TO mcp@'%'; GRANT SELECT ON mysql.* TO mcp@'%';
GRANT EXECUTE ON mysql_mcp.* TO mcp@'%'; GRANT MCP_USER ON *.* TO mcp@'%';
CREATE USER dba@'%' IDENTIFIED BY 'dbapass1'; GRANT ALL ON *.* TO dba@'%';
CREATE USER app@'%' IDENTIFIED BY 'apppass1'; GRANT ALL ON d1.* TO app@'%';")
out="$out$(printf 'DELIMITER $$\nCREATE FUNCTION d1.f_write() RETURNS INT DETERMINISTIC MODIFIES SQL DATA BEGIN INSERT INTO d1.t VALUES (99); RETURN 1; END$$\n' |
	docker exec -i "$CNAME" mysql -h127.0.0.1 -uroot -ptestpass 2>&1 | grep -v "Using a password")"
expect "fixtures" "$out" "^$"
expect "GRANT ALL after install includes MCP_USER and MCP_ADMIN" "$(sql "SHOW GRANTS FOR dba@'%'")" "MCP_ADMIN.*MCP_USER|MCP_USER.*MCP_ADMIN"
mcp() { as mcp mcppass1 "$1"; }

# --- MCP sessions: what is allowed --------------------------------------------------------
expect "mcp: SELECT" "$(mcp "SELECT COUNT(*) FROM d1.t")" "^1$"
expect "mcp: SHOW TABLES" "$(mcp "SHOW TABLES FROM d1")" "^t$"
expect "mcp: information_schema" "$(mcp "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA='d1'")" "^t$"
expect "mcp: performance_schema.global_status" "$(mcp "SELECT VARIABLE_NAME FROM performance_schema.global_status WHERE VARIABLE_NAME='Uptime'")" "^Uptime$"
expect "mcp: session SET" "$(mcp "SET SESSION sql_mode = ''; SELECT 1")" "^1$"
expect "mcp: CALL mysql_mcp tool pack" "$(mcp "CALL mysql_mcp.list_tables('d1')")" "^t	"
expect "mcp: EXPLAIN" "$(mcp "EXPLAIN SELECT * FROM d1.t")" "SIMPLE|Table scan"
expect "mcp: session counted while connected" "$(mcp "SHOW GLOBAL STATUS LIKE 'mcp.sessions_connected'")" "	1$"

# --- MCP sessions: what mcp.read_only denies, even with write grants -----------------------
denied "mcp: INSERT" "$(mcp "INSERT INTO d1.t VALUES (2)")" "mcp.read_only"
denied "mcp: UPDATE" "$(mcp "UPDATE d1.t SET i = 3")" "mcp.read_only"
denied "mcp: DELETE" "$(mcp "DELETE FROM d1.t")" "mcp.read_only"
denied "mcp: CREATE TABLE" "$(mcp "CREATE TABLE d1.x (i INT)")" "mcp.read_only"
denied "mcp: DROP TABLE" "$(mcp "DROP TABLE d1.t")" "mcp.read_only"
denied "mcp: TRUNCATE" "$(mcp "TRUNCATE d1.t")" "mcp.read_only"
denied "mcp: SELECT ... FOR UPDATE" "$(mcp "SELECT * FROM d1.t FOR UPDATE")" "mcp.read_only"
denied "mcp: FOR SHARE in an executable comment" "$(mcp "SELECT * FROM d1.t /*!80000 FOR SHARE */")" "mcp.read_only"
denied "mcp: SELECT ... INTO OUTFILE" "$(mcp "SELECT * FROM d1.t INTO OUTFILE '/tmp/x'")" "mcp.read_only"
denied "mcp: SET GLOBAL" "$(mcp "SET GLOBAL max_connections = 10")" "mcp.read_only"
denied "mcp: CALL outside mysql_mcp" "$(mcp "CALL d1.nope()")" "mcp.read_only"
denied "mcp: DELETE through PREPARE/EXECUTE" "$(mcp "PREPARE s FROM 'DELETE FROM d1.t'; EXECUTE s")" "mcp.read_only"
denied "mcp: write inside a stored function" "$(mcp "SELECT d1.f_write()")" "mcp.read_only"
denied "mcp: LOCK TABLES" "$(mcp "LOCK TABLES d1.t READ")" "mcp.read_only"
# With NO_BACKSLASH_ESCAPES, '\' is a complete string, so FOR UPDATE / INTO OUTFILE below are code.
denied "mcp: FOR UPDATE behind '\\' with NO_BACKSLASH_ESCAPES" "$(mcp "SET SESSION sql_mode = 'NO_BACKSLASH_ESCAPES'; SELECT '\\', 1 FROM d1.t FOR UPDATE -- '")" "mcp.read_only"
denied "mcp: INTO OUTFILE behind '\\' with NO_BACKSLASH_ESCAPES" "$(mcp "SET SESSION sql_mode = 'NO_BACKSLASH_ESCAPES'; SELECT '\\' INTO OUTFILE '/tmp/y' -- '")" "mcp.read_only"
expect "mcp: nothing was written" "$(sql "SELECT COUNT(*) FROM d1.t")" "^1$"

# --- schema rules ---------------------------------------------------------------------------
denied "mcp: mysql schema (deny_system_schemas)" "$(mcp "SELECT COUNT(*) FROM mysql.user")" "mcp.deny_system_schemas"

# --- other accounts are not affected ---------------------------------------------------------
expect "app (no MCP_USER) may write" "$(as app apppass1 "INSERT INTO d1.t VALUES (7); DELETE FROM d1.t WHERE i = 7; SELECT 'ok'")" "^ok$"
expect "dba (MCP_USER and MCP_ADMIN) may write" "$(as dba dbapass1 "INSERT INTO d2.t VALUES (7); DELETE FROM d2.t; SELECT 'ok'")" "^ok$"

# --- administration: MCP_ADMIN required ---------------------------------------------------------
expect "root without MCP_ADMIN cannot change mcp.*" "$(sql "SET GLOBAL mcp.read_only = OFF")" "ERROR 1227 .*MCP_ADMIN"
sql "GRANT MCP_ADMIN ON *.* TO root@'%'; GRANT MCP_ADMIN ON *.* TO root@localhost" >/dev/null 2>&1

expect "SET GLOBAL mcp.read_only = OFF" "$(sql "SET GLOBAL mcp.read_only = OFF")" "^$"
expect "mcp: INSERT with read_only OFF" "$(mcp "INSERT INTO d1.t VALUES (2); SELECT COUNT(*) FROM d1.t")" "^2$"
expect "SET GLOBAL mcp.read_only = ON" "$(sql "SET GLOBAL mcp.read_only = ON")" "^$"
expect "read_only is ON again" "$(sql "SELECT @@mcp.read_only")" "^1$"
sql "DELETE FROM d1.t WHERE i = 2" >/dev/null

expect "SET GLOBAL mcp.allowed_schemas" "$(sql "SET GLOBAL mcp.allowed_schemas = ' d1 , other'")" "^$"
denied "mcp: schema outside allowed_schemas" "$(mcp "SELECT * FROM d2.t")" "mcp.allowed_schemas"
expect "mcp: schema in allowed_schemas" "$(mcp "SELECT COUNT(*) FROM d1.t")" "^1$"
expect "mcp: catalog of other schemas still listed" "$(mcp "CALL mysql_mcp.list_tables('d2')")" "^t	"
sql "SET GLOBAL mcp.allowed_schemas = ' , '" >/dev/null
denied "mcp: allowed_schemas with no names allows none" "$(mcp "SELECT COUNT(*) FROM d1.t")" "mcp.allowed_schemas"
sql "SET GLOBAL mcp.allowed_schemas = ''" >/dev/null
expect "mcp: allowed_schemas cleared" "$(mcp "SELECT COUNT(*) FROM d2.t")" "^0$"

sql "SET GLOBAL mcp.deny_system_schemas = OFF" >/dev/null
expect "mcp: mysql schema with deny_system_schemas OFF" "$(mcp "SELECT COUNT(*) > 0 FROM mysql.user")" "^1$"
sql "SET GLOBAL mcp.deny_system_schemas = ON" >/dev/null

# --- views, stored functions, multi-statement packets (RFC 0001 §5.2 item 7, Q3) --------------
# The schema rules see the base tables of a statement, through views too. Statements of a
# stored function or trigger run on tables the calling statement already locked, which raise
# no table access events: their reads are not seen (known limitation, docs/MCP_COMPONENTS.md).
out=$(sql "
CREATE SQL SECURITY DEFINER VIEW d1.v_d2 AS SELECT i FROM d2.t;
CREATE SQL SECURITY DEFINER VIEW d1.v_nested AS SELECT i FROM d1.v_d2;
CREATE SQL SECURITY DEFINER VIEW d1.v_sys AS SELECT user FROM mysql.user;
CREATE SQL SECURITY DEFINER VIEW d2.v_d1 AS SELECT i FROM d1.t;
INSERT INTO d2.t VALUES (42);")
out="$out$(printf 'DELIMITER $$\nCREATE FUNCTION d1.f_read_d2() RETURNS INT READS SQL DATA SQL SECURITY DEFINER BEGIN RETURN (SELECT MAX(i) FROM d2.t); END$$\n' |
	docker exec -i "$CNAME" mysql -h127.0.0.1 -uroot -ptestpass 2>&1 | grep -v "Using a password")"
expect "view fixtures" "$out" "^$"
# packet SQL: run SQL as one COM_QUERY packet (a multi-statement packet if it has several).
packet() { printf 'DELIMITER ~~\n%s~~\n' "$1" | docker exec -i "$CNAME" mysql -h127.0.0.1 -umcp -pmcppass1 -N --batch 2>&1 | grep -v "Using a password"; }
denied "view in d1 over mysql.user (deny_system_schemas)" "$(mcp "SELECT COUNT(*) FROM d1.v_sys")" "mcp.deny_system_schemas"
sql "SET GLOBAL mcp.allowed_schemas = 'd1'" >/dev/null
denied "view in an allowed schema over another schema" "$(mcp "SELECT * FROM d1.v_d2")" "mcp.allowed_schemas"
denied "nested view over another schema" "$(mcp "SELECT * FROM d1.v_nested")" "mcp.allowed_schemas"
expect "view in another schema over allowed tables (rules apply to base tables)" "$(mcp "SELECT COUNT(*) FROM d2.v_d1")" "^[0-9]+$"
denied "multi-statement packet: disallowed read in the second statement" "$(packet "SELECT 1; SELECT * FROM d2.t")" "mcp.allowed_schemas"
expect "KNOWN LIMITATION: a stored function's reads are not seen by the schema rules" "$(mcp "SELECT d1.f_read_d2()")" "^42$"
# Statements whose table reads raise no table access events are refused under the rules.
denied "SET with a subquery over another schema" "$(mcp "SET @x = (SELECT MAX(i) FROM d2.t); SELECT @x")" "mcp.allowed_schemas"
denied "DO with a subquery" "$(mcp "DO @x := (SELECT MAX(i) FROM d2.t)")" "mcp.allowed_schemas"
denied "SHOW ... WHERE with a subquery" "$(mcp "SHOW DATABASES WHERE (SELECT MAX(i) FROM d2.t) = 42")" "mcp.allowed_schemas"
denied "CALL mysql_mcp with a subquery argument" "$(mcp "CALL mysql_mcp.list_tables((SELECT 'd2'))")" "mcp.allowed_schemas"
denied "SET with a subquery through PREPARE/EXECUTE" "$(mcp "PREPARE s FROM 'SET @p = (SELECT MAX(i) FROM d2.t)'; EXECUTE s; SELECT @p")" "mcp.allowed_schemas"
denied "CHECKSUM TABLE" "$(mcp "CHECKSUM TABLE d2.t")" "mcp.allowed_schemas"
expect "SET, SHOW and CALL without subqueries still work" "$(mcp "SET @x = 1; SHOW TABLES FROM d1 LIKE 't'; CALL mysql_mcp.version()" | tr '\n' ' ')" "^t [0-9]+\.[0-9]+\.[0-9]+ $"
sql "SET GLOBAL mcp.allowed_schemas = ''" >/dev/null
denied "SET with a subquery over mysql (deny_system_schemas)" "$(mcp "SET @x = (SELECT COUNT(*) FROM mysql.user); SELECT @x")" "mcp.deny_system_schemas"
denied "multi-statement packet: write in the second statement" "$(packet "SELECT 1; INSERT INTO d1.t VALUES (5)")" "mcp.read_only"
denied "multi-statement packet: write after a session SET" "$(packet "SET @a = 1; DELETE FROM d1.t")" "mcp.read_only"
expect "multi-statement packets wrote nothing" "$(sql "SELECT GROUP_CONCAT(i ORDER BY i) FROM d1.t")" "^1$"

# --- parity with mysql-mcp-server's run_query validator (sql/mcp/guard-parity.tsv) -----------
# The guard column of the shared table; mysql-mcp-server checks the go column. Each statement
# is sent as one packet under the default settings; "deny" means refused with error 1290.
sql "GRANT SELECT ON sys.* TO mcp@'%'; GRANT ALTER ON d1.* TO mcp@'%'" >/dev/null
parity_rows=0
parity_bad=""
[ -s "$REPO/sql/mcp/guard-parity.tsv" ] || parity_bad="cannot read $REPO/sql/mcp/guard-parity.tsv"
while IFS=$'\t' read -r _go guard stmt _note; do
	case "$_go" in '#'* | '') continue ;; esac
	parity_rows=$((parity_rows + 1))
	got=allow
	packet "$stmt" | grep -q "ERROR 1290" && got=deny
	[ "$got" = "$guard" ] || parity_bad="$parity_bad [$stmt: $got, table says $guard]"
done <"$REPO/sql/mcp/guard-parity.tsv"
[ "$parity_rows" -ge 30 ] || parity_bad="$parity_bad [only $parity_rows rows read]"
expect "parity table: guard verdicts ($parity_rows statements)" "${parity_bad:-ok}" "^ok$"
# d1.t still holds exactly its row (1) and one column, d1.x was not created, d1 has no new table.
d1_state() { sql "SELECT CONCAT_WS(',', (SELECT GROUP_CONCAT(i ORDER BY i) FROM d1.t),
	(SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = 'd1' AND TABLE_NAME = 't'),
	(SELECT GROUP_CONCAT(TABLE_NAME ORDER BY TABLE_NAME) FROM information_schema.TABLES WHERE TABLE_SCHEMA = 'd1'))"; }
expect "parity table: nothing was written" "$(d1_state)" "^1,1,t,v_d2,v_nested,v_sys$"

# --- sessions opened by components (command services), as component_mcp opens them -----------
# A component's "local" session that impersonates an MCP account raises no connection event;
# the guard classifies it at its first statement. The component must emit connection audit
# events for it (Srv_session starts unaudited), as component_mcp will.
expect "probe component installed" "$(sql "INSTALL COMPONENT 'file://component_mcp_session_probe'")" "^$"
local_session() { sql "SELECT mcp_test_local_session($1)"; }
# Refused by the guard: its message, with errno 1290 (8.4 reports errno 0 through the
# command service for an error raised by an event consumer; the message is the same).
denied_local() { expect "$1" "$2" "^err:query:(1290|0):The MySQL server is running with the mcp\."; }
denied_local "local MCP session: write refused" "$(local_session "'mcp', '%', 'INSERT INTO d1.t VALUES (2)'")"
denied_local "local MCP session: mysql schema refused" "$(local_session "'mcp', '%', 'SELECT COUNT(*) FROM mysql.user'")"
denied_local "local MCP session: SET subquery refused" "$(local_session "'mcp', '%', 'SET @x = (SELECT COUNT(*) FROM mysql.user)'")"
expect "local MCP session: reads allowed" "$(local_session "'mcp', '%', 'SELECT COUNT(*) FROM d1.t'")" "^ok:1$"
sql "SET GLOBAL mcp.max_execution_time_ms = 1500" >/dev/null
expect "local MCP session: time limit applied" "$(local_session "'mcp', '%', 'SELECT @@max_execution_time'")" "^ok:1500$"
sql "SET GLOBAL mcp.max_execution_time_ms = 0" >/dev/null
expect "local session of another account: not restricted" "$(local_session "'app', '%', 'INSERT INTO d1.t VALUES (9)'")" "^ok:no-result$"
sql "DELETE FROM d1.t WHERE i = 9" >/dev/null
# Without the connection event the session is unaudited: no event reaches the guard. This is
# why component_mcp must emit it (checked here so that a server change is noticed).
expect "REQUIREMENT: an unaudited local session is not seen (components must emit connection events)" "$(local_session "'mcp', '%', 'INSERT INTO d1.t VALUES (8)', 'unaudited'")" "^ok:no-result$"
sql "DELETE FROM d1.t WHERE i = 8" >/dev/null
expect "local sessions: nothing written, no MCP session left" "$(sql "SELECT GROUP_CONCAT(i ORDER BY i) FROM d1.t; SHOW GLOBAL STATUS LIKE 'mcp.sessions_connected'" | tr '\n' ' ')" "^1 mcp.sessions_connected	0 $"
expect "probe component uninstalled" "$(sql "UNINSTALL COMPONENT 'file://component_mcp_session_probe'")" "^$"

sql "SET GLOBAL mcp.enabled = OFF" >/dev/null
denied "mcp.enabled OFF refuses MCP connections" "$(mcp "SELECT 1")" "mcp.enabled"
expect "mcp.enabled OFF leaves other accounts alone" "$(as app apppass1 "SELECT 1")" "^1$"
sql "SET GLOBAL mcp.enabled = ON" >/dev/null
expect "mcp.enabled ON again" "$(mcp "SELECT 1")" "^1$"

expect "invalid value rejected" "$(sql "SET GLOBAL mcp.read_only = 'maybe'")" "ERROR"

# --- mcp.max_execution_time_ms: a time limit MCP sessions cannot raise ------------------------
expect "SET GLOBAL mcp.max_execution_time_ms = 1000" "$(sql "SET GLOBAL mcp.max_execution_time_ms = 1000")" "^$"
expect "mcp: session max_execution_time set at connect" "$(mcp "SELECT @@max_execution_time")" "^1000$"
expect "mcp: lowering it is allowed" "$(mcp "SET SESSION max_execution_time = 500; SELECT @@max_execution_time")" "^500$"
denied "mcp: SET max_execution_time = 0" "$(mcp "SET SESSION max_execution_time = 0")" "mcp.max_execution_time_ms"
denied "mcp: SET max_execution_time above the limit" "$(mcp "SET @@max_execution_time = 5000")" "mcp.max_execution_time_ms"
denied "mcp: MAX_EXECUTION_TIME(0) hint" "$(mcp "SELECT /*+ MAX_EXECUTION_TIME(0) */ COUNT(*) FROM d1.t")" "mcp.max_execution_time_ms"
denied "mcp: SET_VAR(max_execution_time=0) hint" "$(mcp "SELECT /*+ SET_VAR(max_execution_time=0) */ COUNT(*) FROM d1.t")" "mcp.max_execution_time_ms"
start=$(date +%s)
slept=$(mcp "SELECT SLEEP(5) FROM d1.t")
took=$(($(date +%s) - start))
# Interrupted by the time limit: SLEEP() alone returns 1; reading a table, as here, the
# statement fails with 3024 (ER_QUERY_TIMEOUT). Either way it must not run 5s.
if [ "$took" -le 3 ] && { [ "$slept" = 1 ] || printf '%s' "$slept" | grep -q '^ERROR 3024 '; }; then ok "mcp: a 5s SELECT is stopped by the 1s limit (${took}s)"; else bad "mcp: time limit enforced" "took ${took}s, result '$slept'"; fi
expect "app (no MCP_USER) keeps max_execution_time 0" "$(as app apppass1 "SELECT @@max_execution_time")" "^0$"
sql "SET GLOBAL mcp.max_execution_time_ms = 0" >/dev/null
expect "mcp: no limit once the floor is 0" "$(mcp "SELECT @@max_execution_time")" "^0$"
# A session already connected gets a new limit at its next statement.
mcp "SELECT @@max_execution_time; DO SLEEP(3); SELECT @@max_execution_time" >"/tmp/$CNAME-live" 2>&1 &
live=$!
sleep 1
sql "SET GLOBAL mcp.max_execution_time_ms = 1500" >/dev/null
wait "$live"
expect "mcp: an existing session gets a tightened limit" "$(tr '\n' ' ' <"/tmp/$CNAME-live")" "^0 1500 $"
rm -f "/tmp/$CNAME-live"
sql "SET GLOBAL mcp.max_execution_time_ms = 0" >/dev/null

# --- mcp.log_denials -----------------------------------------------------------------------------
expect "refusals are written to the error log" "$(docker logs "$CNAME" 2>&1 | grep 'mcp_guard: refused by' | tail -3)" '\[MY-011071\] \[mcp_guard\] mcp_guard: refused by mcp\.(read_only|max_execution_time_ms): MCP connection [0-9]+'
mcp "SELECT 'token=hunter2' FROM d1.t FOR UPDATE" >/dev/null
mcp "SET SESSION sql_mode = 'NO_BACKSLASH_ESCAPES'; SELECT 'a\\', 'token=hunter2' FROM d1.t FOR UPDATE" >/dev/null
expect "error log redacts string literals" "$(docker logs "$CNAME" 2>&1 | grep 'mcp_guard: refused by' | grep -c 'hunter2')" "^0$"
sql "SET GLOBAL mcp.log_denials = OFF" >/dev/null
before=$(docker logs "$CNAME" 2>&1 | grep -c 'mcp_guard: refused by')
mcp "INSERT INTO d1.t VALUES (8)" >/dev/null
after=$(docker logs "$CNAME" 2>&1 | grep -c 'mcp_guard: refused by')
expect "mcp.log_denials = OFF writes nothing" "$after" "^$before$"
sql "SET GLOBAL mcp.log_denials = ON" >/dev/null

# --- performance_schema.mcp_tool_calls -------------------------------------------------------------
# Statements labeled by mysql-mcp-server (/* mcp:tool=... req=... */) are grouped per tool call.
# --comments: older clients strip comments by default.
mcpc() { docker exec -i "$CNAME" mysql -h127.0.0.1 -umcp -pmcppass1 -N --batch --comments -e "$1" 2>&1 | grep -v "Using a password"; }
calls() { sql "SELECT CONCAT_WS(',', TOOL, ACCOUNT, STATEMENTS, ERRORS, DENIED) FROM performance_schema.mcp_tool_calls WHERE REQUEST_ID = '$1'"; }
expect "mcp.tool_calls_history_size default" "$(sql "SELECT @@mcp.tool_calls_history_size")" "^1000$"
expect "mcp_tool_calls empty before any labeled statement" "$(sql "SELECT COUNT(*) FROM performance_schema.mcp_tool_calls")" "^0$"
mcpc "/* mcp:tool=list_tables req=t-1 */ SELECT 1; /* mcp:tool=list_tables req=t-1 */ SELECT COUNT(*) FROM d1.t" >/dev/null
expect "a tool call with two statements" "$(calls t-1)" "^list_tables,mcp@%,2,0,0$"
mcpc "/* mcp:tool=run_query req=t-2 */ INSERT INTO d1.t VALUES (7)" >/dev/null
expect "a refused tool call counts denied and errors" "$(calls t-2)" "^run_query,mcp@%,1,1,1$"
mcpc "/* mcp:tool=run_query req=t-3 */ SELECT * FROM mysql.user" >/dev/null
expect "a table-level refusal is counted" "$(calls t-3)" "^run_query,mcp@%,1,1,1$"
mcpc "/* mcp:tool=run_query req=t-4 */ SELECT SLEEP(0.2)" >/dev/null
expect "latency is measured" "$(sql "SELECT LATENCY_US >= 200000 AND LAST_SEEN >= FIRST_SEEN FROM performance_schema.mcp_tool_calls WHERE REQUEST_ID = 't-4'")" "^1$"
mcpc "SELECT 1; /* not a label */ SELECT 2; /* mcp:tool=x */ SELECT 3" >/dev/null
docker exec -i "$CNAME" mysql -h127.0.0.1 -uapp -papppass1 --comments -e "/* mcp:tool=list_tables req=t-app */ SELECT 1" >/dev/null 2>&1
expect "unlabeled statements and other accounts add no rows" "$(sql "SELECT COUNT(*) FROM performance_schema.mcp_tool_calls")" "^4$"
expect "rows read in order and by a self-join" "$(sql "SELECT GROUP_CONCAT(REQUEST_ID ORDER BY REQUEST_ID DESC), (SELECT COUNT(*) FROM performance_schema.mcp_tool_calls a JOIN performance_schema.mcp_tool_calls b USING (REQUEST_ID)) FROM performance_schema.mcp_tool_calls")" "^t-4,t-3,t-2,t-1	4$"
expect "mcp_tool_calls is read-only" "$(sql "DELETE FROM performance_schema.mcp_tool_calls")" "ERROR"

status=$(sql "SHOW GLOBAL STATUS LIKE 'mcp.%'")
expect "status: statements_denied counted" "$status" "mcp.statements_denied	[1-9]"
expect "status: table_access_denied counted" "$status" "mcp.table_access_denied	[1-9]"
expect "status: statements_total counted" "$status" "mcp.statements_total	[1-9]"
expect "status: no MCP session left" "$status" "mcp.sessions_connected	0"

# --- persistence and restart -----------------------------------------------------------------------
sql "SET PERSIST mcp.allowed_schemas = 'd1'" >/dev/null
docker restart "$CNAME" >/dev/null && wait_ready
expect "loaded again after restart" "$(sql "SHOW PRIVILEGES" | awk '{print $1}')" "MCP_USER"
expect "persisted mcp.allowed_schemas" "$(sql "SELECT @@mcp.allowed_schemas")" "^d1$"
denied "persisted allowed_schemas enforced after restart" "$(mcp "SELECT * FROM d2.t")" "mcp.allowed_schemas"
sql "CREATE USER sva@'%' IDENTIFIED BY 'svapass1'; GRANT SYSTEM_VARIABLES_ADMIN ON *.* TO sva@'%'" >/dev/null
expect "SET PERSIST mcp.* needs MCP_ADMIN (SYSTEM_VARIABLES_ADMIN is not enough)" "$(as sva svapass1 "SET PERSIST mcp.read_only = OFF")" "ERROR 1227 .*MCP_ADMIN"
sql "RESET PERSIST mcp.allowed_schemas; SET GLOBAL mcp.allowed_schemas = ''" >/dev/null

# --- uninstall ---------------------------------------------------------------------------------
expect "UNINSTALL COMPONENT" "$(sql "UNINSTALL COMPONENT 'file://component_mcp_guard'")" "^$"
expect "MCP_ADMIN unknown after uninstall" "$(sql "GRANT MCP_ADMIN ON *.* TO app@'%'")" "ERROR"
expect "mcp.* variables gone after uninstall" "$(sql "SHOW VARIABLES LIKE 'mcp.%'")" "^$"
expect "performance_schema.mcp_tool_calls gone after uninstall" "$(sql "SELECT COUNT(*) FROM performance_schema.mcp_tool_calls")" "ERROR 1146"
expect "no enforcement after uninstall" "$(mcp "INSERT INTO d1.t VALUES (5); SELECT COUNT(*) FROM d1.t")" "^2$"
# A session connected before INSTALL COMPONENT is classified at its first statement after it.
# The client is connected and idle (no statement running) through the install; its INSERT is
# then sent over the same connection, through a FIFO.
early_dir=$(mktemp -d)
mkfifo "$early_dir/in"
docker exec -i "$CNAME" mysql -h127.0.0.1 -umcp -pmcppass1 -N <"$early_dir/in" >"$early_dir/out" 2>&1 &
early=$!
exec 3>"$early_dir/in"
idle_mcp() { sql "SELECT COUNT(*) FROM performance_schema.processlist WHERE USER = 'mcp' AND COMMAND = 'Sleep'"; }
for _ in $(seq 1 40); do [ "$(idle_mcp)" = 1 ] && break; sleep 0.25; done
expect "the MCP session is connected and idle before the install" "$(idle_mcp)" "^1$"
expect "reinstalled while an MCP session is connected" "$(sql "INSTALL COMPONENT 'file://component_mcp_guard'")" "^$"
printf 'INSERT INTO d1.t VALUES (6);\n' >&3
exec 3>&-
wait "$early"
denied "a session connected before INSTALL is guarded at its first statement after it" "$(grep -v "Using a password" "$early_dir/out")" "mcp.read_only"
rm -rf "$early_dir"

# --- lower_case_table_names = 1: allowed_schemas compares case-insensitively ---------------------
docker run -d --name "$CNAME_LCTN" -e MYSQL_ROOT_PASSWORD=testpass "mysql:$TAG" --lower-case-table-names=1 >/dev/null
CNAME_MAIN=$CNAME
CNAME=$CNAME_LCTN
if wait_ready; then
	docker cp "$SO" "$CNAME:$PLUGIN_DIR/component_mcp_guard.so" &&
		docker exec -u 0 "$CNAME" chmod 0644 "$PLUGIN_DIR/component_mcp_guard.so"
	sql "INSTALL COMPONENT 'file://component_mcp_guard'; GRANT MCP_ADMIN ON *.* TO root@'%'; GRANT MCP_ADMIN ON *.* TO root@localhost;
CREATE DATABASE Shop; CREATE TABLE Shop.t (i INT); CREATE DATABASE other; CREATE TABLE other.t (i INT);
CREATE USER mcp@'%' IDENTIFIED BY 'mcppass1'; GRANT SELECT ON *.* TO mcp@'%'; GRANT MCP_USER ON *.* TO mcp@'%'" >/dev/null
	expect "lower_case_table_names=1: SET GLOBAL mcp.allowed_schemas = 'Shop'" "$(sql "SET GLOBAL mcp.allowed_schemas = 'Shop'")" "^$"
	expect "lower_case_table_names=1: allowed_schemas 'Shop' allows shop" "$(mcp "SELECT COUNT(*) FROM Shop.t")" "^0$"
	denied "lower_case_table_names=1: other schemas still denied" "$(mcp "SELECT COUNT(*) FROM other.t")" "mcp.allowed_schemas"
	# A fresh server: its first minute of refusals is logged up to 10 lines, then held back.
	for _ in $(seq 1 15); do mcp "SELECT COUNT(*) FROM other.t" >/dev/null; done
	expect "error log rate limit: 16 refusals in a minute, 10 lines" "$(docker logs "$CNAME" 2>&1 | grep -c 'mcp_guard: refused by')" "^10$"
else
	bad "lower_case_table_names=1 server" "did not start"
fi
CNAME=$CNAME_MAIN

echo "== mysql:$TAG: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
