#!/usr/bin/env bash
# Tests the mysql_mcp SQL tool pack (sql/mcp/mysql_mcp.sql, RFC 0001 Phase 1) against an
# official image: install and re-install, then parity. Every procedure must return exactly
# what mysql-mcp-server's own query returns, run as a server-side prepared statement with
# `?` placeholders (as the Go driver runs it), both as root and as an MCP account that
# has only EXECUTE on mysql_mcp plus its data grants.
#
# Usage: scripts/test-mcp-tool-pack.sh [image]
#   image   e.g. mysql:8.0, mysql:9.7, mysql:26.10, mariadb:11.4 (default: mysql:8.4)
# Requires Docker. bash 3.2+.
set -u

IMAGE=${1:-mysql:8.4}
REPO=$(cd "$(dirname "$0")/.." && pwd)
PACK=$REPO/sql/mcp/mysql_mcp.sql
CNAME=mcp-pack-test-$(printf '%s' "$IMAGE" | tr -c 'a-zA-Z0-9.-' '-')-$$
W=$(mktemp -d)
PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); printf '  PASS  %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf '  FAIL  %s  -- %s\n' "$1" "$2"; }
cleanup() {
	docker rm -f -v "$CNAME" >/dev/null 2>&1
	rm -rf "${W:?}"
}
trap cleanup EXIT

echo "== $IMAGE"
docker run -d --name "$CNAME" -e MYSQL_ROOT_PASSWORD=testpass -e MARIADB_ROOT_PASSWORD=testpass "$IMAGE" >/dev/null || exit 1
CLIENT=
ready=0
# Over TCP, so the probe cannot reach the entrypoint's temporary --skip-networking server.
for _ in $(seq 1 90); do
	[ -n "$CLIENT" ] || CLIENT=$(docker exec "$CNAME" sh -c 'command -v mysql || command -v mariadb' 2>/dev/null)
	[ -n "$CLIENT" ] && docker exec "$CNAME" "$CLIENT" -h127.0.0.1 -uroot -ptestpass -e 'select 1' >/dev/null 2>&1 && {
		ready=1
		break
	}
	sleep 2
done
[ "$ready" = 1 ] || { echo "server did not start"; exit 1; }

# as USER PASS [client args] < sql : batch output, errors folded into the output
as() {
	local u=$1 p=$2
	shift 2
	docker exec -i "$CNAME" "$CLIENT" -h127.0.0.1 -u"$u" -p"$p" --batch "$@" 2>&1 | grep -v 'password on the command line'
}
root() { as root testpass "$@"; }
VERSION=$(echo 'SELECT VERSION()' | root -N)
echo "   server: $VERSION"
MARIADB=0
case $VERSION in *MariaDB*) MARIADB=1 ;; esac

# --- install ---------------------------------------------------------------------------
out=$(root <"$PACK")
[ -z "$out" ] && ok "install" || bad "install" "$out"
out=$(root <"$PACK")
[ -z "$out" ] && ok "re-install (upgrade path)" || bad "re-install" "$out"
check_eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "got '$2', want '$3'"; fi; }
check_eq "version()" "$(echo 'SELECT mysql_mcp.version()' | root -N)" "1.0.0"
check_eq "routines are SQL SECURITY INVOKER" \
	"$(echo "SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA='mysql_mcp' AND SECURITY_TYPE<>'INVOKER'" | root -N)" 0

# --- fixture ---------------------------------------------------------------------------
VECTOR=0
echo "SELECT CAST(STRING_TO_VECTOR('[1,2]') AS BINARY) IS NOT NULL" | root -N 2>/dev/null | grep -qx 1 && VECTOR=1
root <<'SQL'
DROP DATABASE IF EXISTS CaseDB;
DROP DATABASE IF EXISTS casedb;
CREATE DATABASE CaseDB;
CREATE DATABASE casedb;
CREATE TABLE casedb.lower_only (id INT PRIMARY KEY);
CREATE TABLE CaseDB.Users (
  id INT AUTO_INCREMENT PRIMARY KEY COMMENT 'user id',
  name VARCHAR(50) NOT NULL DEFAULT 'x',
  email VARCHAR(100),
  UNIQUE KEY uk_email (email),
  KEY idx_name_email (name, email)
) COMMENT='people';
CREATE TABLE CaseDB.Orders (
  id INT PRIMARY KEY,
  user_id INT,
  total DECIMAL(10,2),
  CONSTRAINT fk_orders_user FOREIGN KEY (user_id) REFERENCES CaseDB.Users(id) ON DELETE CASCADE ON UPDATE SET NULL
);
CREATE TABLE `CaseDB`.`we``ird` (`col``q` INT, KEY `ix``q` (`col``q`));
CREATE TABLE CaseDB.Parts (id INT, d DATE)
  PARTITION BY RANGE (YEAR(d)) (PARTITION p2024 VALUES LESS THAN (2025), PARTITION pmax VALUES LESS THAN MAXVALUE);
INSERT INTO CaseDB.Users (name, email) VALUES ('ann','a@x'), ('bob','b@x'), ('cy','c@x');
INSERT INTO CaseDB.Orders VALUES (1,1,9.50), (2,2,20.00);
CREATE VIEW CaseDB.v_users AS SELECT id, name FROM CaseDB.Users;
CREATE VIEW CaseDB.a_view AS SELECT 1 AS one;
CREATE TRIGGER CaseDB.trg_users BEFORE INSERT ON CaseDB.Users FOR EACH ROW SET NEW.name = TRIM(NEW.name);
CREATE PROCEDURE CaseDB.p_one() SELECT 1;
CREATE FUNCTION CaseDB.f_one() RETURNS INT DETERMINISTIC NO SQL RETURN 1;
ANALYZE TABLE CaseDB.Users, CaseDB.Orders, CaseDB.Parts;
DROP USER IF EXISTS 'mcp'@'%';
CREATE USER 'mcp'@'%' IDENTIFIED BY 'mcppass1';
GRANT EXECUTE ON mysql_mcp.* TO 'mcp'@'%';
GRANT SELECT, SHOW VIEW, TRIGGER ON CaseDB.* TO 'mcp'@'%';
SQL
if [ "$VECTOR" = 1 ]; then
	root <<<"CREATE TABLE CaseDB.Docs (id INT PRIMARY KEY, emb VECTOR(3), emb2 VECTOR(8), KEY ix_emb2 (id));" >/dev/null
fi

# --- parity ----------------------------------------------------------------------------
# sqlstr VALUE -> a single-quoted SQL literal; NULL for the word NULL and a bare number
# for digits (LIMIT ? needs an integer, which is how the Go driver binds it)
sqlstr() {
	case $1 in
	NULL) printf 'NULL' ;;
	*[!0-9]* | '') printf "'%s'" "$(printf '%s' "$1" | sed "s/'/''/g")" ;;
	*) printf '%s' "$1" ;;
	esac
}

# errcode OUTPUT -> "ERROR <code>" when OUTPUT has a client error, else OUTPUT unchanged
errcode() {
	if printf '%s\n' "$1" | grep -q '^ERROR [0-9]'; then
		printf '%s\n' "$1" | grep -o '^ERROR [0-9]*' | head -1
	else
		printf '%s\n' "$1"
	fi
}

# same LABEL WHO "CALL ..." "reference SQL with ?" [args...]
#   WHO is root or mcp. The reference runs as a prepared statement with the args bound
#   through user variables, the way the Go driver binds `?`.
same() {
	local label=$1 who=$2 call=$3 ref=$4 sets="" using="" i=0 a got want
	shift 4
	for a in "$@"; do
		i=$((i + 1))
		sets="$sets SET @a$i = $(sqlstr "$a");"
		using="$using${using:+,} @a$i"
	done
	local refsql
	refsql="$sets PREPARE s FROM $(sqlstr "$ref"); EXECUTE s${using:+ USING$using}; DEALLOCATE PREPARE s;"
	if [ "$who" = root ]; then
		got=$(root <<<"$call")
		want=$(root <<<"$refsql")
	else
		got=$(as mcp mcppass1 <<<"$call")
		want=$(as mcp mcppass1 <<<"$refsql")
	fi
	# An error must match by code only: the client's message quotes the failing statement.
	got=$(errcode "$got")
	want=$(errcode "$want")
	if [ "$got" = "$want" ]; then
		case $got in
		"ERROR "*) ok "$label [$who] (both fail: $got)" ;;
		*) ok "$label [$who] ($(printf '%s\n' "$got" | sed -n '2,$p' | grep -c .) rows)" ;;
		esac
	else
		printf '%s\n' "$want" >"$W/want"
		printf '%s\n' "$got" >"$W/got"
		bad "$label [$who]" "$(diff "$W/want" "$W/got" | head -8 | tr '\n' ' ')"
	fi
}

SYS="'information_schema', 'performance_schema', 'mysql', 'sys'"
for who in root mcp; do
	same "list_databases" $who "CALL mysql_mcp.list_databases()" \
		"SELECT SCHEMA_NAME FROM information_schema.SCHEMATA ORDER BY SCHEMA_NAME, CAST(SCHEMA_NAME AS BINARY)"
	for db in CaseDB casedb CASEDB nosuch; do
		same "list_tables $db" $who "CALL mysql_mcp.list_tables($(sqlstr $db))" \
			"SELECT TABLE_NAME, ENGINE, TABLE_ROWS, TABLE_COMMENT FROM information_schema.TABLES WHERE TABLE_SCHEMA = ? ORDER BY TABLE_NAME, CAST(TABLE_NAME AS BINARY)" $db
	done
	for t in Users users 'we`ird' nosuch; do
		same "describe_table $t" $who "CALL mysql_mcp.describe_table('CaseDB', $(sqlstr "$t"))" \
			"SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, COLUMN_DEFAULT, EXTRA, COLUMN_COMMENT, COLLATION_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION" CaseDB "$t"
	done
	for t in Users Orders 'we`ird' v_users; do
		qt=$(printf '%s' "$t" | sed 's/`/``/g')
		same "show_create_table $t" $who "CALL mysql_mcp.show_create_table('CaseDB', $(sqlstr "$t"))" "SHOW CREATE TABLE \`CaseDB\`.\`$qt\`"
		same "list_indexes $t" $who "CALL mysql_mcp.list_indexes('CaseDB', $(sqlstr "$t"))" "SHOW INDEX FROM \`CaseDB\`.\`$qt\`"
	done
	same "list_views" $who "CALL mysql_mcp.list_views('CaseDB')" \
		"SELECT TABLE_NAME, DEFINER, SECURITY_TYPE, IS_UPDATABLE FROM information_schema.VIEWS WHERE TABLE_SCHEMA = ? ORDER BY TABLE_NAME" CaseDB
	same "list_triggers" $who "CALL mysql_mcp.list_triggers('CaseDB')" \
		"SELECT TRIGGER_NAME, EVENT_MANIPULATION, EVENT_OBJECT_TABLE, ACTION_TIMING, LEFT(ACTION_STATEMENT, 200) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = ? ORDER BY TRIGGER_NAME" CaseDB
	same "list_procedures" $who "CALL mysql_mcp.list_procedures('CaseDB')" \
		"SELECT ROUTINE_NAME, DEFINER, CREATED, LAST_ALTERED, IFNULL(PARAMETER_STYLE, '') FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = ? AND ROUTINE_TYPE = 'PROCEDURE' ORDER BY ROUTINE_NAME" CaseDB
	same "list_functions" $who "CALL mysql_mcp.list_functions('CaseDB')" \
		"SELECT ROUTINE_NAME, DEFINER, DTD_IDENTIFIER, CREATED FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = ? AND ROUTINE_TYPE = 'FUNCTION' ORDER BY ROUTINE_NAME" CaseDB
	same "list_partitions" $who "CALL mysql_mcp.list_partitions('CaseDB', 'Parts')" \
		"SELECT PARTITION_NAME, PARTITION_METHOD, PARTITION_EXPRESSION, PARTITION_DESCRIPTION, TABLE_ROWS, DATA_LENGTH FROM information_schema.PARTITIONS WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? AND PARTITION_NAME IS NOT NULL ORDER BY PARTITION_ORDINAL_POSITION, SUBPARTITION_ORDINAL_POSITION" CaseDB Parts
	DBSIZE="SELECT TABLE_SCHEMA, ROUND(SUM(DATA_LENGTH + INDEX_LENGTH) / 1024 / 1024, 2) as size_mb, ROUND(SUM(DATA_LENGTH) / 1024 / 1024, 2) as data_mb, ROUND(SUM(INDEX_LENGTH) / 1024 / 1024, 2) as index_mb, COUNT(*) as tables FROM information_schema.TABLES"
	same "database_size CaseDB" $who "CALL mysql_mcp.database_size('CaseDB')" "$DBSIZE WHERE TABLE_SCHEMA = ? GROUP BY TABLE_SCHEMA ORDER BY size_mb DESC, TABLE_SCHEMA, CAST(TABLE_SCHEMA AS BINARY)" CaseDB
	same "database_size all" $who "CALL mysql_mcp.database_size(NULL)" "$DBSIZE WHERE TABLE_SCHEMA NOT IN ($SYS) GROUP BY TABLE_SCHEMA ORDER BY size_mb DESC, TABLE_SCHEMA, CAST(TABLE_SCHEMA AS BINARY)"
	TSIZE="SELECT TABLE_NAME, TABLE_ROWS, ROUND(DATA_LENGTH / 1024 / 1024, 2) as data_mb, ROUND(INDEX_LENGTH / 1024 / 1024, 2) as index_mb, ROUND((DATA_LENGTH + INDEX_LENGTH) / 1024 / 1024, 2) as total_mb, ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = ?"
	same "table_size all" $who "CALL mysql_mcp.table_size('CaseDB', NULL)" "$TSIZE ORDER BY total_mb DESC, TABLE_NAME" CaseDB
	same "table_size Users" $who "CALL mysql_mcp.table_size('CaseDB', 'Users')" "$TSIZE AND TABLE_NAME = ? ORDER BY total_mb DESC, TABLE_NAME" CaseDB Users
	FK="SELECT CONSTRAINT_NAME, TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME, (SELECT UPDATE_RULE FROM information_schema.REFERENTIAL_CONSTRAINTS rc WHERE rc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA AND rc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME) as on_update, (SELECT DELETE_RULE FROM information_schema.REFERENTIAL_CONSTRAINTS rc WHERE rc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA AND rc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME) as on_delete FROM information_schema.KEY_COLUMN_USAGE kcu WHERE CONSTRAINT_SCHEMA = ? AND REFERENCED_TABLE_NAME IS NOT NULL"
	same "foreign_keys all" $who "CALL mysql_mcp.foreign_keys('CaseDB', NULL)" "$FK ORDER BY TABLE_NAME, CONSTRAINT_NAME, ORDINAL_POSITION" CaseDB
	same "foreign_keys Orders" $who "CALL mysql_mcp.foreign_keys('CaseDB', 'Orders')" "$FK AND TABLE_NAME = ? ORDER BY TABLE_NAME, CONSTRAINT_NAME, ORDINAL_POSITION" CaseDB Orders
	for pat in '%user%' 'USERS' '%'; do
		same "search_tables $pat in CaseDB" $who "CALL mysql_mcp.search_tables($(sqlstr "$pat"), 'CaseDB', 50)" \
			"SELECT TABLE_SCHEMA, TABLE_NAME FROM information_schema.TABLES WHERE TABLE_NAME LIKE ? AND TABLE_SCHEMA = ? ORDER BY TABLE_SCHEMA, CAST(TABLE_SCHEMA AS BINARY), TABLE_NAME, CAST(TABLE_NAME AS BINARY) LIMIT ?" "$pat" CaseDB 50
		same "search_tables $pat everywhere" $who "CALL mysql_mcp.search_tables($(sqlstr "$pat"), NULL, 3)" \
			"SELECT TABLE_SCHEMA, TABLE_NAME FROM information_schema.TABLES WHERE TABLE_NAME LIKE ? AND TABLE_SCHEMA NOT IN ($SYS) ORDER BY TABLE_SCHEMA, CAST(TABLE_SCHEMA AS BINARY), TABLE_NAME, CAST(TABLE_NAME AS BINARY) LIMIT ?" "$pat" 3
		same "search_columns $pat everywhere" $who "CALL mysql_mcp.search_columns($(sqlstr "$pat"), NULL, 5)" \
			"SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS WHERE COLUMN_NAME LIKE ? AND TABLE_SCHEMA NOT IN ($SYS) ORDER BY TABLE_SCHEMA, CAST(TABLE_SCHEMA AS BINARY), TABLE_NAME, CAST(TABLE_NAME AS BINARY), ORDINAL_POSITION LIMIT ?" "$pat" 5
	done
	same "search_columns id in CaseDB" $who "CALL mysql_mcp.search_columns('ID', 'CaseDB', 50)" \
		"SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS WHERE COLUMN_NAME LIKE ? AND TABLE_SCHEMA = ? ORDER BY TABLE_SCHEMA, CAST(TABLE_SCHEMA AS BINARY), TABLE_NAME, CAST(TABLE_NAME AS BINARY), ORDINAL_POSITION LIMIT ?" ID CaseDB 50
	same "vector_columns" $who "CALL mysql_mcp.vector_columns('CaseDB', NULL)" \
		"SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = ? AND COLUMN_TYPE LIKE 'vector%' ORDER BY TABLE_NAME, ORDINAL_POSITION" CaseDB
	same "vector_column_index" $who "CALL mysql_mcp.vector_column_index('CaseDB', 'Users', 'email')" \
		"SELECT INDEX_NAME, INDEX_TYPE FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? AND COLUMN_NAME = ? ORDER BY INDEX_NAME LIMIT 1" CaseDB Users email
done

# Status and variables change between two reads (Uptime, Questions, ...): compare stable
# rows exactly, and the full list by name only. Where performance_schema.global_status is
# missing or empty (MariaDB), both sides must agree.
for who in root mcp; do
	same "list_status pattern" $who "CALL mysql_mcp.list_status('Innodb_page_size')" \
		"SELECT VARIABLE_NAME, VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME LIKE ? ORDER BY VARIABLE_NAME" Innodb_page_size
	same "list_variables pattern" $who "CALL mysql_mcp.list_variables('MAX_CONN%')" "SHOW GLOBAL VARIABLES WHERE Variable_name LIKE ?" 'MAX_CONN%'
done
names() { errcode "$(root)" | cut -f1; }
check_eq "list_status all (names)" "$(names <<<'CALL mysql_mcp.list_status(NULL)' | md5sum)" \
	"$(names <<<'SELECT VARIABLE_NAME, VARIABLE_VALUE FROM performance_schema.global_status ORDER BY VARIABLE_NAME' | md5sum)"
check_eq "list_variables all (names)" "$(names <<<'CALL mysql_mcp.list_variables(NULL)' | md5sum)" \
	"$(names <<<'SHOW GLOBAL VARIABLES' | md5sum)"
check_eq "process_list columns" "$(root <<<'CALL mysql_mcp.process_list()' | head -1)" "$(root <<<'SHOW FULL PROCESSLIST' | head -1)"

if [ "$VECTOR" = 1 ]; then
	has_vec=$(root -N <<<"CALL mysql_mcp.vector_columns('CaseDB', 'Docs')" | cut -f2 | tr '\n' ' ')
	check_eq "vector_columns finds VECTOR columns" "$has_vec" "emb emb2 "
else
	ok "vector_columns: no VECTOR type on this server (skipped)"
fi

# --- safety ----------------------------------------------------------------------------
err_has() { # label output want
	if printf '%s' "$2" | grep -q -- "$3"; then ok "$1"; else bad "$1" "missing '$3' in: $(printf '%s' "$2" | head -c 200)"; fi
}
err_has "show_create_table: missing table -> 1146" "$(root <<<"CALL mysql_mcp.show_create_table('CaseDB', 'nosuch')")" "1146"
err_has "list_indexes: injection in name is just a missing table" \
	"$(root <<<"CALL mysql_mcp.list_indexes('CaseDB', 'Users\` ; DROP TABLE CaseDB.Orders ; --')")" "1146"
check_eq "  ... and Orders still exists" "$(root -N <<<"SELECT COUNT(*) FROM CaseDB.Orders")" 2
err_has "show_create_table: NULL names -> 1146" "$(root <<<"CALL mysql_mcp.show_create_table(NULL, NULL)")" "1146"
err_has "mcp cannot see tables it has no grant on" "$(as mcp mcppass1 <<<"CALL mysql_mcp.show_create_table('casedb', 'lower_only')")" "1146"
check_eq "mcp list_tables of an ungranted database is empty" "$(as mcp mcppass1 -N <<<"CALL mysql_mcp.list_tables('casedb')")" ""
root <<<"DROP USER IF EXISTS 'nopack'@'%'; CREATE USER 'nopack'@'%' IDENTIFIED BY 'nopack1'; GRANT SELECT ON CaseDB.* TO 'nopack'@'%';" >/dev/null
err_has "no EXECUTE grant -> CALL refused" "$(as nopack nopack1 <<<"CALL mysql_mcp.list_databases()")" "denied"
check_eq "session variable cleared after dynamic SHOW" "$(root -N <<<"CALL mysql_mcp.list_indexes('CaseDB', 'Users'); SELECT IFNULL(@mysql_mcp_stmt, 'unset');" | tail -1)" "unset"

echo "== $IMAGE ($VERSION): $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
