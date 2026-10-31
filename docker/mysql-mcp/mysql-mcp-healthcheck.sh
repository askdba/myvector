#!/bin/sh
# Health check of the mysql-mcp image: is mysqld up and done with first-start setup?
#
# - TCP: the entrypoint's temporary setup server does not listen on TCP, so the check
#   passes only once the real server runs.
# - The port mysqld was started with (--port=N or --port N on its command line; else 3306).
# - As a user that does not exist, with no password: ping reports a running server even
#   when access is denied, and a nonexistent account cannot be locked by failed logins
#   (probing as root could lock out administrators with FAILED_LOGIN_ATTEMPTS).
port=$(tr '\0' '\n' </proc/1/cmdline 2>/dev/null | awk '
	prev == "--port" || prev == "-P" { p = $0 }
	/^--port=/ { p = substr($0, 8) }
	{ prev = $0 }
	END { print p }')
exec mysqladmin ping --protocol=TCP -h127.0.0.1 -P"${port:-3306}" \
	-umysql-mcp-healthcheck --connect-timeout=3 --silent
