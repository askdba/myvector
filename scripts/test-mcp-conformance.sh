#!/usr/bin/env bash
# MCP conformance of component_mcp (RFC 0001 §10 go/no-go: "MCP conformance against at least
# two clients"): the endpoint, with component_mcp_guard and the tool pack, on the official MySQL
# image, driven by two independent MCP clients over Streamable HTTP:
#   1. MCP Inspector's CLI (@modelcontextprotocol/inspector, Node);
#   2. the official Python SDK client (mcp), scripts/mcp-conformance/sdk_client.py, which also
#      checks the transport: session id and version headers, DELETE on close.
# Each client runs in both protocol eras: the session-based revisions (up to 2025-11-25) and the
# stateless revision 2026-07-28 (Inspector: --protocol-era modern, which does not fall back).
# Client versions are pinned below; each runs in its own container sharing the server's
# loopback (the endpoint listens on 127.0.0.1).
#
# Usage: scripts/test-mcp-conformance.sh <mysql-image> <artifact-dir>
#   e.g. scripts/test-mcp-conformance.sh mysql:26.10.0 dist/mcp-26.10.0
# The artifact dir holds component_mcp.so and component_mcp_guard.so. Requires Docker.
set -u
IMAGE=${1:?mysql image, e.g. mysql:26.10.0}; ART=${2:?artifact dir}
REPO=$(cd "$(dirname "$0")/.." && pwd)
INSPECTOR=@modelcontextprotocol/inspector@2.12.0
NODE_IMAGE=node:22-alpine
PY_SDK=mcp==2.3.0
PY_IMAGE=python:3.12-slim
TOKEN=conformance-token
TOKEN_HEX=$(printf '%s' "$TOKEN" | sha256sum | cut -d' ' -f1)
URL=http://127.0.0.1:33480/mcp
C=mcp-conformance-$$
PASS=0; FAIL=0
ok() { PASS=$((PASS + 1)); printf '  PASS  %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf '  FAIL  %s  -- %s\n' "$1" "$2"; }
expect() { if printf '%s\n' "$2" | grep -Eq -- "$3"; then ok "$1"; else bad "$1" "got: $(printf '%s' "$2" | head -c 300)"; fi; }
trap 'docker rm -f -v "$C" >/dev/null 2>&1' EXIT
sql() { docker exec -i "$C" mysql -h127.0.0.1 -uroot -ptestpass -N -e "$1" 2>&1 | grep -v "Using a password"; }
# inspector TOKEN ARGS...: one MCP Inspector CLI call; prints its output (pretty-printed JSON),
# then "rc=N". Checks spanning lines flatten it with tr.
inspector() { t=$1; shift; docker run --rm --network "container:$C" -e npm_config_update_notifier=false "$NODE_IMAGE" \
	npx -y "$INSPECTOR" --cli "$URL" --transport http --header "Authorization: Bearer $t" "$@" 2>&1; echo "rc=$?"; }

echo "== $IMAGE"
docker run -d --name "$C" -e MYSQL_ROOT_PASSWORD=testpass "$IMAGE" >/dev/null || exit 1
for _ in $(seq 1 90); do sql 'select 1' >/dev/null 2>&1 && sql 'select 1' | grep -q '^1$' && break; sleep 2; done
PD=$(sql 'select @@plugin_dir')
for f in component_mcp_guard.so component_mcp.so; do docker cp "$ART/$f" "$C:$PD/$f" && docker exec -u 0 "$C" chmod 0644 "$PD/$f"; done
docker exec -i "$C" mysql -h127.0.0.1 -uroot -ptestpass <"$REPO/sql/mcp/mysql_mcp.sql" 2>&1 | grep -v "Using a password"
sql "INSTALL COMPONENT 'file://component_mcp_guard';
CREATE DATABASE d1; CREATE TABLE d1.t (i INT); INSERT INTO d1.t VALUES (1);
CREATE USER mcp@'%'; GRANT SELECT, INSERT ON d1.* TO mcp@'%'; GRANT EXECUTE ON mysql_mcp.* TO mcp@'%';
GRANT MCP_USER ON *.* TO mcp@'%'"
expect "install component_mcp" "$(sql "INSTALL COMPONENT 'file://component_mcp' SET GLOBAL mcp_http.port = 33480, GLOBAL mcp_http.account = 'mcp@%', GLOBAL mcp_http.token_sha256 = '$TOKEN_HEX'")" "^$"

echo "-- client 1: MCP Inspector CLI ($INSPECTOR)"
expect "inspector: tools/list" "$(inspector "$TOKEN" --method tools/list | grep -o '"name": "[a-z_]*"' | tr '\n' ' ')" '"name": "list_databases" "name": "list_tables" "name": "describe_table" "name": "run_query" "name": "ping" "name": "server_info"'
expect "inspector: tools/call run_query" "$(inspector "$TOKEN" --method tools/call --tool-name run_query --tool-arg 'sql=SELECT i FROM d1.t' | tr '\n' ' ')" '"text": "\{\\"rows\\":\[\{\\"i\\":\\"1\\"\}\],\\"truncated\\":false\}".*"isError": false.*rc=0'
expect "inspector: tools/call describe_table" "$(inspector "$TOKEN" --method tools/call --tool-name describe_table --tool-arg database=d1 --tool-arg table=t | tr '\n' ' ')" 'COLUMN_NAME\\":\\"i\\".*"isError": false.*rc=0'
expect "inspector: a refused statement is a tool error" "$(inspector "$TOKEN" --method tools/call --tool-name run_query --tool-arg 'sql=INSERT INTO d1.t VALUES (2)' | tr '\n' ' ')" 'mcp.read_only.*"isError": true'
expect "inspector: a wrong token fails to connect" "$(inspector wrong-token --method tools/list | tail -1)" '^rc=[1-9]'
echo "-- client 1: MCP Inspector CLI, stateless revision 2026-07-28 (--protocol-era modern)"
expect "inspector 2026-07-28: tools/list" "$(inspector "$TOKEN" --protocol-era modern --method tools/list | grep -o '"name": "[a-z_]*"' | tr '\n' ' ')" '"name": "list_databases" "name": "list_tables" "name": "describe_table" "name": "run_query" "name": "ping" "name": "server_info"'
expect "inspector 2026-07-28: tools/call run_query" "$(inspector "$TOKEN" --protocol-era modern --method tools/call --tool-name run_query --tool-arg 'sql=SELECT i FROM d1.t' | tr '\n' ' ')" '"text": "\{\\"rows\\":\[\{\\"i\\":\\"1\\"\}\],\\"truncated\\":false\}".*"isError": false.*rc=0'
expect "inspector 2026-07-28: a refused statement is a tool error" "$(inspector "$TOKEN" --protocol-era modern --method tools/call --tool-name run_query --tool-arg 'sql=INSERT INTO d1.t VALUES (2)' | tr '\n' ' ')" 'mcp.read_only.*"isError": true'
expect "inspector --protocol-era auto: tools/call list_tables" "$(inspector "$TOKEN" --protocol-era auto --method tools/call --tool-name list_tables --tool-arg database=d1 | tr '\n' ' ')" 'TABLE_NAME\\":\\"t\\".*"isError": false.*rc=0'
expect "inspector 2026-07-28: a wrong token fails" "$(inspector wrong-token --protocol-era modern --method tools/list | tail -1)" '^rc=[1-9]'

echo "-- client 2: Python SDK ($PY_SDK)"
sdk_out=$(docker run --rm --network "container:$C" -v "$REPO/scripts/mcp-conformance:/conf:ro" "$PY_IMAGE" \
	sh -c "pip install -q --disable-pip-version-check --root-user-action=ignore '$PY_SDK' && python /conf/sdk_client.py '$URL' '$TOKEN'" 2>&1)
sdk_rc=$?
printf '%s\n' "$sdk_out" | grep -E '^(PASS|FAIL)  sdk:' | sed 's/^/  /'
PASS=$((PASS + $(printf '%s\n' "$sdk_out" | grep -c '^PASS  sdk:')))
FAIL=$((FAIL + $(printf '%s\n' "$sdk_out" | grep -c '^FAIL  sdk:')))
if [ "$sdk_rc" != 0 ] && ! printf '%s\n' "$sdk_out" | grep -q '^FAIL  sdk:'; then
	bad "sdk client ran" "rc=$sdk_rc: $(printf '%s' "$sdk_out" | tail -c 400)"
fi
expect "nothing written by either client" "$(sql "SELECT GROUP_CONCAT(i) FROM d1.t")" "^1$"
echo "== $IMAGE: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
