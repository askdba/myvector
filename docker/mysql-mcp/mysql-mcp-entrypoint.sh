#!/bin/bash
# Entrypoint of the mysql-mcp image: checks the MYSQL_MCP_* settings, then runs the official
# entrypoint. The checks run before the data directory is initialized, because a first-start
# script that fails leaves an initialized data directory whose setup a restart skips. Once the
# data directory exists the settings are not used again, so they are not checked (a volume
# created by an earlier image keeps starting).
set -eo pipefail

mcp_error() {
	printf '%s [ERROR] [Entrypoint]: mysql-mcp: %s\n' "$(date --rfc-3339=seconds)" "$*" >&2
	exit 1
}

check_settings() {
	if [ -n "${MYSQL_MCP_USER:-}" ]; then
		[ -n "${MYSQL_MCP_PASSWORD:-}" ] || mcp_error "MYSQL_MCP_USER is set but MYSQL_MCP_PASSWORD is not"
		[ "$MYSQL_MCP_USER" != root ] || mcp_error "MYSQL_MCP_USER cannot be root"
		[[ "$MYSQL_MCP_USER" =~ ^[A-Za-z0-9_.-]{1,32}$ ]] ||
			mcp_error "MYSQL_MCP_USER must be 1-32 characters from A-Z, a-z, 0-9, _ . -"
		# The official entrypoint creates MYSQL_USER with ALL on MYSQL_DATABASE; the MCP account
		# must be a separate, read-only one. MYSQL_USER may come from MYSQL_USER_FILE (when both
		# are set, the official entrypoint refuses them itself).
		local app_user=${MYSQL_USER:-}
		if [ -z "$app_user" ] && [ -n "${MYSQL_USER_FILE:-}" ] && [ -r "$MYSQL_USER_FILE" ]; then
			app_user=$(<"$MYSQL_USER_FILE")
		fi
		# The official entrypoint puts MYSQL_USER into SQL unescaped, so a backslash or quote in
		# it changes the account name (app\q creates appq), and MySQL trims leading and trailing
		# spaces (' app ' is app): such a name cannot be compared.
		case "$app_user" in
		*\\* | *\'* | *\"*) mcp_error "MYSQL_USER contains a backslash or quote; use a plain name with MYSQL_MCP_USER" ;;
		[[:space:]]* | *[[:space:]]) mcp_error "MYSQL_USER starts or ends with whitespace; use a plain name with MYSQL_MCP_USER" ;;
		esac
		[ "$MYSQL_MCP_USER" != "$app_user" ] ||
			mcp_error "MYSQL_MCP_USER must differ from MYSQL_USER (that account gets all privileges on MYSQL_DATABASE)"
	fi
	case "${MYSQL_MCP_MAX_EXECUTION_TIME_MS:-}" in
	*[!0-9]*) mcp_error "MYSQL_MCP_MAX_EXECUTION_TIME_MS must be a number of milliseconds" ;;
	esac
}

# As the official entrypoint: a command starting with an option runs mysqld.
if [ "${1:0:1}" = '-' ]; then
	set -- mysqld "$@"
fi
if [ "$1" = mysqld ]; then
	# The data directory as the official entrypoint finds it (mysql_get_config).
	datadir=$("$@" --verbose --help --log-bin-index="$(mktemp -u)" 2>/dev/null |
		awk '$1 == "datadir" && /^[^ \t]/ { sub(/^[^ \t]+[ \t]+/, ""); print; exit }')
	[ -d "$datadir/mysql" ] || check_settings
fi

exec docker-entrypoint.sh "$@"
