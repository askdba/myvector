#!/bin/sh
# Health check of the mysql-mcp image: is mysqld up and done with first-start setup?
#
# - TCP: the entrypoint's temporary setup server does not listen on TCP, so the check
#   passes only once the real server runs.
# - The port mysqld uses: --port=N or --port N on its command line, else port= in its option
#   files (read with my_print_defaults, honoring --defaults-file / --defaults-extra-file),
#   else 3306.
# - As a user that does not exist, with no password: ping reports a running server even
#   when access is denied, and a nonexistent account cannot be locked by failed logins
#   (probing as root could lock out administrators with FAILED_LOGIN_ATTEMPTS).
args=$(tr '\0' '\n' </proc/1/cmdline 2>/dev/null)
port=$(printf '%s\n' "$args" | awk '
	prev == "--port" || prev == "-P" { p = $0 }
	/^--port=/ { p = substr($0, 8) }
	{ prev = $0 }
	END { print p }')
if [ -z "$port" ]; then
	defaults=$(printf '%s\n' "$args" | grep -E '^--defaults-(extra-)?file=' | head -n 1)
	port=$(my_print_defaults $defaults mysqld 2>/dev/null | sed -n 's/^--port=//p' | tail -n 1)
fi
exec mysqladmin ping --protocol=TCP -h127.0.0.1 -P"${port:-3306}" \
	-umysql-mcp-healthcheck --connect-timeout=3 --silent
