#!/bin/bash
# First-start setup of the mysql-mcp image. Sourced by the official entrypoint (it is not
# executable), so docker_process_sql and mysql_note are available.
#
#   - installs component_mcp_guard and gives root MCP_ADMIN (to change mcp.* settings);
#   - installs the mysql_mcp SQL tool pack;
#   - MYSQL_MCP_USER / MYSQL_MCP_PASSWORD: creates an account for mysql-mcp-server that
#     holds MCP_USER (so the guard applies to it), EXECUTE on mysql_mcp and SELECT, SHOW
#     VIEW on MYSQL_MCP_DATABASES (comma-separated; default MYSQL_DATABASE);
#   - MYSQL_MCP_ALLOWED_SCHEMAS, MYSQL_MCP_MAX_EXECUTION_TIME_MS: persisted as
#     mcp.allowed_schemas and mcp.max_execution_time_ms.

# Batches with literals start with this, so _mcp_sql_literal's escaping holds even when
# the server runs with NO_BACKSLASH_ESCAPES.
_mcp_backslash_escapes="SET SESSION sql_mode = REPLACE(@@SESSION.sql_mode, 'NO_BACKSLASH_ESCAPES', '');"
_mcp_sql_literal() { # a single-quoted SQL string literal (with backslash escapes)
	local s=${1//\\/\\\\}
	printf "'%s'" "${s//\'/\'\'}"
}
_mcp_sql_ident() { # a backquoted identifier
	printf '`%s`' "${1//\`/\`\`}"
}

mysql_note "mysql-mcp: installing component_mcp_guard"
docker_process_sql --database=mysql <<<"INSTALL COMPONENT 'file://component_mcp_guard'"
# Dynamic privileges registered after root was created: grant them to every root account.
_mcp_roots=$(docker_process_sql --database=mysql -N -B <<<"SELECT CONCAT(QUOTE(user), '@', QUOTE(host)) FROM mysql.user WHERE user = 'root'")
for _mcp_root in $_mcp_roots; do
	docker_process_sql --database=mysql <<<"GRANT MCP_ADMIN ON *.* TO $_mcp_root"
done

mysql_note "mysql-mcp: installing the mysql_mcp tool pack"
docker_process_sql --database=mysql </usr/share/mysql-mcp/mysql_mcp.sql

_mcp_settings=$_mcp_backslash_escapes
if [ -n "${MYSQL_MCP_ALLOWED_SCHEMAS:-}" ]; then
	_mcp_settings+="SET PERSIST mcp.allowed_schemas = $(_mcp_sql_literal "$MYSQL_MCP_ALLOWED_SCHEMAS");"
fi
if [ -n "${MYSQL_MCP_MAX_EXECUTION_TIME_MS:-}" ]; then
	case "$MYSQL_MCP_MAX_EXECUTION_TIME_MS" in
	*[!0-9]*) mysql_error "MYSQL_MCP_MAX_EXECUTION_TIME_MS must be a number of milliseconds" ;;
	esac
	_mcp_settings+="SET PERSIST mcp.max_execution_time_ms = $MYSQL_MCP_MAX_EXECUTION_TIME_MS;"
fi
[ "$_mcp_settings" = "$_mcp_backslash_escapes" ] || docker_process_sql --database=mysql <<<"$_mcp_settings"

if [ -n "${MYSQL_MCP_USER:-}" ]; then
	[ -n "${MYSQL_MCP_PASSWORD:-}" ] || mysql_error "MYSQL_MCP_USER is set but MYSQL_MCP_PASSWORD is not"
	[ "$MYSQL_MCP_USER" != root ] || mysql_error "MYSQL_MCP_USER cannot be root"
	_mcp_account="$(_mcp_sql_literal "$MYSQL_MCP_USER")@'%'"
	mysql_note "mysql-mcp: creating MCP account $MYSQL_MCP_USER"
	_mcp_grants="$_mcp_backslash_escapes
CREATE USER $_mcp_account IDENTIFIED BY $(_mcp_sql_literal "$MYSQL_MCP_PASSWORD");
GRANT MCP_USER ON *.* TO $_mcp_account;
GRANT EXECUTE ON mysql_mcp.* TO $_mcp_account;"
	IFS=',' read -r -a _mcp_dbs <<<"${MYSQL_MCP_DATABASES:-${MYSQL_DATABASE:-}}"
	for _mcp_db in "${_mcp_dbs[@]}"; do
		_mcp_db=${_mcp_db#"${_mcp_db%%[![:space:]]*}"} # trim
		_mcp_db=${_mcp_db%"${_mcp_db##*[![:space:]]}"}
		[ -n "$_mcp_db" ] || continue
		# Escape _ and % so the grant names exactly this schema (as the entrypoint does).
		_mcp_pattern=${_mcp_db//\\/\\\\}
		_mcp_pattern=${_mcp_pattern//_/\\_}
		_mcp_pattern=${_mcp_pattern//%/\\%}
		_mcp_grants+="
GRANT SELECT, SHOW VIEW ON $(_mcp_sql_ident "$_mcp_pattern").* TO $_mcp_account;"
	done
	docker_process_sql --database=mysql <<<"$_mcp_grants"
fi
unset _mcp_backslash_escapes _mcp_roots _mcp_root _mcp_settings _mcp_account _mcp_grants _mcp_dbs _mcp_db _mcp_pattern
