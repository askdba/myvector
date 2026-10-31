#!/usr/bin/env bash
# Tests a mysql-mcp image (docker/mysql-mcp/Dockerfile): first-start setup of
# component_mcp_guard, the mysql_mcp tool pack and the MYSQL_MCP_* account and settings,
# and that they survive a restart.
#
# Usage: scripts/test-mysql-mcp-image.sh <image>
#   e.g. scripts/test-mysql-mcp-image.sh mysql-mcp-smoke:9.7
# Requires Docker. bash 3.2+.
set -u

IMAGE=${1:?image, e.g. mysql-mcp-smoke:9.7}
CNAME=mysql-mcp-image-test-$$
CNAME_PLAIN=$CNAME-plain
PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); printf '  PASS  %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf '  FAIL  %s  -- %s\n' "$1" "$2"; }
cleanup() { docker rm -f -v "$CNAME" "$CNAME_PLAIN" >/dev/null 2>&1; }
trap cleanup EXIT
expect() {
	if printf '%s\n' "$2" | grep -Eq -- "$3"; then ok "$1"; else bad "$1" "got: $(printf '%s' "$2" | head -c 300)"; fi
}

# as CONTAINER USER PASS SQL
as() { docker exec -i "$1" mysql -h127.0.0.1 -u"$2" -p"$3" -N --batch -e "$4" 2>&1 | grep -v "Using a password"; }
wait_ready() {
	for _ in $(seq 1 120); do
		docker exec "$1" mysql -h127.0.0.1 -uroot -p"$2" -e 'select 1' >/dev/null 2>&1 && return 0
		sleep 2
	done
	return 1
}

echo "== $IMAGE"
# A database name with _ and a password with quotes check the init script's quoting.
docker run -d --name "$CNAME" -e MYSQL_ROOT_PASSWORD=testpass -e MYSQL_DATABASE=app_db \
	-e MYSQL_MCP_USER=mcp -e "MYSQL_MCP_PASSWORD=it's\"a\\pass" \
	-e MYSQL_MCP_ALLOWED_SCHEMAS=app_db -e MYSQL_MCP_MAX_EXECUTION_TIME_MS=5000 "$IMAGE" >/dev/null || exit 1
wait_ready "$CNAME" testpass || { docker logs "$CNAME" 2>&1 | tail -30; echo "server did not start"; exit 1; }
root() { as "$CNAME" root testpass "$1"; }
mcp() { as "$CNAME" mcp "it's\"a\\pass" "$1"; }
echo "   server: $(root 'select version()')"

expect "init script ran without errors" "$(docker logs "$CNAME" 2>&1 | grep -E 'mysql-mcp:|ERROR' | tr '\n' ' ')" "mysql-mcp: creating MCP account mcp"
expect "no init errors" "$(docker logs "$CNAME" 2>&1 | grep -c '\[ERROR\] \[Entrypoint\]')" "^0$"
expect "component_mcp_guard installed" "$(root "SELECT component_urn FROM mysql.component")" "component_mcp_guard"
expect "mysql_mcp tool pack installed" "$(root "CALL mysql_mcp.version()")" "^[0-9]+\.[0-9]+\.[0-9]+$"
expect "root has MCP_ADMIN" "$(root "SHOW GRANTS FOR root@'%'")" "MCP_ADMIN"
expect "settings persisted" "$(root "SELECT CONCAT_WS(',', @@mcp.allowed_schemas, @@mcp.max_execution_time_ms, @@mcp.read_only)")" "^app_db,5000,1$"
expect "MCP account grants" "$(root "SHOW GRANTS FOR mcp@'%'" | tr '\n' ' ')" "MCP_USER.*SELECT, SHOW VIEW ON \`app[\\]+_db\`.*EXECUTE ON \`mysql_mcp\`" # --batch doubles the backslash
root "CREATE TABLE app_db.t (i INT); INSERT INTO app_db.t VALUES (1), (2)" >/dev/null
expect "MCP account reads its database" "$(mcp "SELECT COUNT(*) FROM app_db.t")" "^2$"
expect "MCP account uses the tool pack" "$(mcp "CALL mysql_mcp.list_tables('app_db')")" "^t"
expect "MCP account gets the time limit" "$(mcp "SELECT @@max_execution_time")" "^5000$"
expect "guard applies to the MCP account" "$(mcp "SELECT * FROM app_db.t FOR UPDATE")" "ERROR 1290 .*mcp.read_only"
expect "root can change mcp.* settings" "$(root "SET GLOBAL mcp.read_only = ON")" "^$"

docker restart "$CNAME" >/dev/null && wait_ready "$CNAME" testpass
expect "after restart: guard loaded, settings kept" "$(root "SELECT CONCAT_WS(',', @@mcp.allowed_schemas, @@mcp.max_execution_time_ms)")" "^app_db,5000$"
expect "after restart: setup not run again" "$(docker logs "$CNAME" 2>&1 | grep -c 'mysql-mcp: installing component_mcp_guard')" "^1$"

# Without MYSQL_MCP_* settings: component and tool pack only.
docker run -d --name "$CNAME_PLAIN" -e MYSQL_ROOT_PASSWORD=testpass "$IMAGE" >/dev/null
if wait_ready "$CNAME_PLAIN" testpass; then
	expect "plain: component and tool pack" "$(as "$CNAME_PLAIN" root testpass "SELECT COUNT(*) FROM mysql.component WHERE component_urn LIKE '%mcp_guard'; CALL mysql_mcp.version()" | tr '\n' ' ')" "^1 [0-9]+\.[0-9]+\.[0-9]+ $"
	expect "plain: default settings" "$(as "$CNAME_PLAIN" root testpass "SELECT CONCAT_WS(',', @@mcp.enabled, @@mcp.read_only, IFNULL(@@mcp.allowed_schemas, ''))")" "^1,1,$"
	expect "plain: no MCP account" "$(as "$CNAME_PLAIN" root testpass "SELECT COUNT(*) FROM mysql.user WHERE user = 'mcp'")" "^0$"
else
	bad "plain container" "did not start"
fi

echo "== $IMAGE: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
