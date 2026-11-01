#!/bin/bash
# Health check of the mysql-mcp image: is mysqld up and done with first-start setup?
#
# - Not ready while PID 1 is still the entrypoint (first-start setup runs a temporary
#   server, which does not listen on TCP either).
# - The port: mysqld itself resolves it from its own arguments and option files
#   (`mysqld <args> --verbose --help`), so command line, option groups ([mysqld], [server],
#   [mysqld-<version>]), --defaults-file and --no-defaults apply exactly as for the server.
# - As a user that does not exist, with no password: ping reports a running server even
#   when access is denied, and a nonexistent account cannot be locked by failed logins
#   (probing as root could lock out administrators with FAILED_LOGIN_ATTEMPTS).
mapfile -d '' args </proc/1/cmdline 2>/dev/null || exit 1
[ "${args[0]##*/}" = mysqld ] || exit 1
# A throwaway binary log index, as the official entrypoint uses for the same lookup, so the
# live index is never touched.
port=$("${args[0]}" "${args[@]:1}" --verbose --help --log-bin-index="$(mktemp -u)" 2>/dev/null |
	awk '$1 == "port" { print $2; exit }')
exec mysqladmin ping --protocol=TCP -h127.0.0.1 -P"${port:-3306}" \
	-umysql-mcp-healthcheck --connect-timeout=3 --silent
