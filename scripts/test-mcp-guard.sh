#!/usr/bin/env bash
# Lifecycle test for component_mcp_guard (RFC 0001, Phase 0) against an official MySQL image.
#
# Usage: scripts/test-mcp-guard.sh <mysql-image-tag> <artifact-dir>
#   e.g. scripts/test-mcp-guard.sh 9.7 dist/mcp-phase0-9.7
# Checks: INSTALL COMPONENT succeeds, MCP_USER / MCP_ADMIN become grantable dynamic
# privileges, the component survives a restart, and UNINSTALL COMPONENT removes them.
set -u

TAG=${1:?mysql image tag, e.g. 9.7}
ART=${2:?artifact dir containing component_mcp_guard.so}
SO=$ART/component_mcp_guard.so
[ -f "$SO" ] || { echo "missing $SO"; exit 1; }
CNAME=mcp-guard-test-$(printf '%s' "$TAG" | tr -c 'a-zA-Z0-9.-' '-')-$$
PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); printf '  PASS  %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf '  FAIL  %s  -- %s\n' "$1" "$2"; }
cleanup() { docker rm -f "$CNAME" >/dev/null 2>&1; }
trap cleanup EXIT

sql() { docker exec "$CNAME" mysql -uroot -ptestpass -N -e "$1" 2>&1 | grep -v "Using a password"; }
wait_ready() {
	for _ in $(seq 1 90); do
		docker exec "$CNAME" mysql -uroot -ptestpass -e 'select 1' >/dev/null 2>&1 && return 0
		sleep 2
	done
	return 1
}

echo "== mysql:$TAG"
docker run -d --name "$CNAME" -e MYSQL_ROOT_PASSWORD=testpass "mysql:$TAG" >/dev/null || exit 1
wait_ready || { echo "server did not start"; exit 1; }
echo "   server: $(sql 'select version()')"

PLUGIN_DIR=$(sql 'select @@plugin_dir')
docker cp "$SO" "$CNAME:$PLUGIN_DIR/component_mcp_guard.so" &&
	docker exec -u 0 "$CNAME" chmod 0644 "$PLUGIN_DIR/component_mcp_guard.so"

out=$(sql "INSTALL COMPONENT 'file://component_mcp_guard'")
if [ -z "$out" ]; then ok "INSTALL COMPONENT"; else bad "INSTALL COMPONENT" "$out"; fi
privs=$(sql "SHOW PRIVILEGES" | awk '$1=="MCP_USER" || $1=="MCP_ADMIN" {print $1}' | sort | tr '\n' ',')
[ "$privs" = "MCP_ADMIN,MCP_USER," ] && ok "SHOW PRIVILEGES lists MCP_ADMIN, MCP_USER" || bad "SHOW PRIVILEGES" "got '$privs'"
out=$(sql "CREATE USER mcp@'%' IDENTIFIED BY 'x'; GRANT MCP_USER ON *.* TO mcp@'%'; SHOW GRANTS FOR mcp@'%'")
case $out in *MCP_USER*) ok "GRANT MCP_USER to an account" ;; *) bad "GRANT MCP_USER" "$out" ;; esac
case $(sql "SELECT component_urn FROM mysql.component") in *component_mcp_guard*) ok "registered in mysql.component" ;; *) bad "mysql.component" "not listed" ;; esac

docker restart "$CNAME" >/dev/null && wait_ready
case $(sql "SHOW PRIVILEGES" | awk '{print $1}') in *MCP_USER*) ok "loaded again after restart" ;; *) bad "after restart" "MCP_USER not registered" ;; esac

out=$(sql "UNINSTALL COMPONENT 'file://component_mcp_guard'")
if [ -z "$out" ]; then ok "UNINSTALL COMPONENT"; else bad "UNINSTALL COMPONENT" "$out"; fi
out=$(sql "GRANT MCP_ADMIN ON *.* TO mcp@'%'")
case $out in *ERROR*) ok "MCP_ADMIN unknown after uninstall" ;; *) bad "after uninstall" "GRANT MCP_ADMIN still accepted" ;; esac

echo "== mysql:$TAG: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
