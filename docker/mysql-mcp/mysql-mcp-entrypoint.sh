#!/bin/bash
# Entrypoint of the mysql-mcp image: checks the MYSQL_MCP_* settings, then runs the official
# entrypoint. The checks run before the data directory is initialized, because a first-start
# script that fails leaves an initialized data directory whose setup a restart skips.
set -eo pipefail

mcp_error() {
	printf '%s [ERROR] [Entrypoint]: mysql-mcp: %s\n' "$(date --rfc-3339=seconds)" "$*" >&2
	exit 1
}

if [ -n "${MYSQL_MCP_USER:-}" ]; then
	[ -n "${MYSQL_MCP_PASSWORD:-}" ] || mcp_error "MYSQL_MCP_USER is set but MYSQL_MCP_PASSWORD is not"
	[ "$MYSQL_MCP_USER" != root ] || mcp_error "MYSQL_MCP_USER cannot be root"
	# The official entrypoint creates MYSQL_USER with ALL on MYSQL_DATABASE; the MCP account
	# must be a separate, read-only one. MYSQL_USER may come from MYSQL_USER_FILE (when both
	# are set, the official entrypoint refuses them itself).
	app_user=${MYSQL_USER:-}
	if [ -z "$app_user" ] && [ -n "${MYSQL_USER_FILE:-}" ] && [ -r "$MYSQL_USER_FILE" ]; then
		app_user=$(<"$MYSQL_USER_FILE")
	fi
	[ "$MYSQL_MCP_USER" != "$app_user" ] ||
		mcp_error "MYSQL_MCP_USER must differ from MYSQL_USER (that account gets all privileges on MYSQL_DATABASE)"
fi
case "${MYSQL_MCP_MAX_EXECUTION_TIME_MS:-}" in
*[!0-9]*) mcp_error "MYSQL_MCP_MAX_EXECUTION_TIME_MS must be a number of milliseconds" ;;
esac

exec docker-entrypoint.sh "$@"
