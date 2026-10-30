/*
   This program is free software; you can redistribute it and/or modify
   it under the terms of the GNU General Public License, version 2.0,
   as published by the Free Software Foundation.

   This program is distributed in the hope that it will be useful,
   but WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
   GNU General Public License, version 2.0, for more details.
*/

// Unit test for mcp_guard_policy.h (no server needed):
//   g++ -std=c++17 -Wall -Wextra -o /tmp/t src/mcp/guard/test_mcp_guard_policy.cc && /tmp/t

#include "mcp_guard_policy.h"

#include <cstdio>
#include <cstring>

using namespace mcp_guard;

static int failures = 0;

static void expect_ro(const char *cmd, const char *sql, bool denied) {
  const char *why = read_only_violation(cmd, sql, std::strlen(sql));
  if ((why != nullptr) != denied) {
    std::printf("FAIL read_only %-10s %-60s want %s\n", cmd, sql, denied ? "deny" : "allow");
    ++failures;
  }
}

static void expect_table(const TablePolicy &p, const char *db, bool write, const char *want) {
  const char *got = table_violation(p, db, write);
  if ((got == nullptr) != (want == nullptr) || (got && std::strcmp(got, want) != 0)) {
    std::printf("FAIL table %s write=%d got %s want %s\n", db, write, got ? got : "allow",
                want ? want : "allow");
    ++failures;
  }
}

int main() {
  // Allowed reads.
  expect_ro("select", "SELECT * FROM t WHERE note = 'FOR UPDATE'", false);
  expect_ro("select", "SELECT 1 -- FOR UPDATE", false);
  expect_ro("select", "SELECT 1 /* INTO OUTFILE '/tmp/x' */", false);
  expect_ro("select", "SELECT `for`, `update` FROM t", false);
  expect_ro("select", "SELECT COUNT(*) INTO @n FROM t", false);
  expect_ro("select", "SELECT * FROM JSON_TABLE('[]', '$[*]' COLUMNS (i FOR ORDINALITY)) j", false);
  expect_ro("show_tables", "SHOW TABLES", false);
  expect_ro("show_create", "SHOW CREATE TABLE t", false);
  expect_ro("set_option", "SET @mysql_mcp_1 = 'GLOBAL'", false);
  expect_ro("set_option", "SET SESSION max_execution_time = 5", false);
  expect_ro("set_option", "SET NAMES utf8mb4", false);
  expect_ro("call", "CALL mysql_mcp.list_tables('d')", false);
  expect_ro("call", "/* mcp:tool=list_tables */ CALL `mysql_mcp`.`list_tables`(?)", false);
  expect_ro("call", "call MYSQL_MCP . list_databases()", false);
  expect_ro("execute", "EXECUTE s", false);
  expect_ro("change_db", "USE d", false);

  // Denied.
  expect_ro("insert", "INSERT INTO t VALUES (1)", true);
  expect_ro("delete", "DELETE FROM t", true);
  expect_ro("create_table", "CREATE TABLE x (i int)", true);
  expect_ro("drop_table", "DROP TABLE t", true);
  expect_ro("truncate", "TRUNCATE t", true);
  expect_ro("load", "LOAD DATA INFILE 'x' INTO TABLE t", true);
  expect_ro("lock_tables", "LOCK TABLES t READ", true);
  expect_ro("flush", "FLUSH TABLES", true);
  expect_ro("grant", "GRANT SELECT ON *.* TO u", true);
  expect_ro("analyze", "ANALYZE TABLE t", true);
  expect_ro("a_future_command", "WHATEVER", true);
  expect_ro("select", "SELECT * FROM t INTO OUTFILE '/tmp/x'", true);
  expect_ro("select", "select * into dumpfile '/tmp/x' from t", true);
  expect_ro("select", "SELECT * FROM t FOR UPDATE", true);
  expect_ro("select", "SELECT * FROM t FOR SHARE SKIP LOCKED", true);
  expect_ro("select", "SELECT * FROM t LOCK IN SHARE MODE", true);
  expect_ro("select", "SELECT * FROM t /*!80000 FOR UPDATE */", true);
  expect_ro("select", "SELECT * FROM t /*M! FOR UPDATE */", true);
  expect_ro("set_option", "SET GLOBAL read_only = 0", true);
  expect_ro("set_option", "SET PERSIST max_connections = 10", true);
  expect_ro("set_option", "SET @@global.read_only = 0", true);
  expect_ro("set_option", "SET @@persist_only.x = 1", true);
  expect_ro("call", "CALL shop.cleanup()", true);
  expect_ro("call", "CALL list_tables('d')", true);
  expect_ro("call", "CALL `mysql_mcp_evil`.x()", true);

  // NO_BACKSLASH_ESCAPES: '\' is a whole string there, so FOR UPDATE below is code.
  // With backslash escapes the same text hides it inside a string; both are checked.
  expect_ro("select", "SELECT '\\', 1 FROM t FOR UPDATE -- '", true);
  expect_ro("select", "SELECT '\\' INTO OUTFILE '/tmp/x' -- '", true);
  expect_ro("set_option", "SET @a = '\\', GLOBAL max_connections = 1 -- '", true);
  // ...and the other way round: a quote escaped by a backslash hides nothing.
  expect_ro("select", "SELECT 'it\\'s FOR UPDATE'", true);  // denied under one reading
  expect_ro("select", "SELECT 'a\\\\b'", false);
  // ANSI_QUOTES: "..." is an identifier; scanning it as a string hides no keywords.
  expect_ro("select", "SELECT \"for\" FROM t", false);
  expect_ro("call", "CALL mysql_mcp.list_tables('\\') ", false);

  // mcp.max_execution_time_ms
  struct TimeCase {
    const char *cmd, *sql;
    bool denied;
  };
  const TimeCase time_cases[] = {
      {"set_option", "SET SESSION max_execution_time = 500", false},
      {"set_option", "SET max_execution_time=1000", false},
      {"set_option", "SET @@session.max_execution_time = 999, sql_mode = ''", false},
      {"set_option", "SET @max_execution_time = 0", false},  // a user variable
      {"set_option", "SET sql_mode = ''", false},
      {"set_option", "SET SESSION max_execution_time = 0", true},
      {"set_option", "SET max_execution_time = 5000", true},
      {"set_option", "SET @@max_execution_time := 2000", true},
      {"set_option", "SET max_execution_time = DEFAULT", true},
      {"set_option", "SET max_execution_time = 500 * 100", true},
      {"set_option", "SET max_execution_time = @x", true},
      {"set_option", "SET a = 1, max_execution_time = 0", true},
      {"select", "SELECT /*+ MAX_EXECUTION_TIME(500) */ * FROM t", false},
      {"select", "SELECT /*+ MAX_EXECUTION_TIME(0) */ * FROM t", true},
      {"select", "SELECT /*+ max_execution_time( 5000 ) */ * FROM t", true},
      {"select", "SELECT /*+ SET_VAR(max_execution_time=0) */ * FROM t", true},
      {"select", "SELECT /*+ SET_VAR(max_execution_time = 800) */ * FROM t", false},
      {"select", "SELECT /*+ BKA(t) */ * FROM t", false},
      {"select", "SELECT 'max_execution_time = 0' FROM t", false},
  };
  for (const TimeCase &c : time_cases) {
    const char *why = time_limit_violation(c.cmd, c.sql, std::strlen(c.sql), 1000);
    if ((why != nullptr) != c.denied) {
      std::printf("FAIL time limit %-60s want %s\n", c.sql, c.denied ? "deny" : "allow");
      ++failures;
    }
  }
  if (time_limit_violation("set_option", "SET max_execution_time = 0", 26, 0) != nullptr) {
    std::printf("FAIL time limit floor 0 must not check\n");
    ++failures;
  }

  // Tokenizer details.
  {
    const char *q = "SELECT 'it''s' , \"a\\\"b\" FROM `we``ird`";
    std::vector<Token> t = tokenize(q, std::strlen(q));
    if (t.size() != 4 || t[3].kind != Token::kQuotedIdent || t[3].text != "we`ird") {
      std::printf("FAIL tokenize quoting (%zu tokens)\n", t.size());
      ++failures;
    }
  }
  if (parse_schema_list(" shop , ,crm,").size() != 2) {
    std::printf("FAIL parse_schema_list\n");
    ++failures;
  }

  if (restricts_schemas(" , ") != true || restricts_schemas("  ") != false) {
    std::printf("FAIL restricts_schemas\n");
    ++failures;
  }
  std::vector<std::string> allowed = {"shop", "crm"};
  std::vector<std::string> none;
  TablePolicy p;
  expect_table(p, "shop", false, nullptr);
  expect_table(p, "shop", true, "mcp.read_only");
  expect_table(p, "mysql", false, "mcp.deny_system_schemas");
  expect_table(p, "sys", false, "mcp.deny_system_schemas");
  expect_table(p, "performance_schema", false, nullptr);
  p.restrict_schemas = true;
  p.allowed_schemas = &none;  // mcp.allowed_schemas = ',': no schema
  expect_table(p, "shop", false, "mcp.allowed_schemas");
  expect_table(p, "information_schema", false, nullptr);
  p.allowed_schemas = &allowed;
  expect_table(p, "crm", false, nullptr);
  expect_table(p, "CRM", false, "mcp.allowed_schemas");  // lower_case_table_names = 0
  p.case_insensitive_names = true;                         // 1 or 2
  expect_table(p, "CRM", false, nullptr);
  p.case_insensitive_names = false;
  expect_table(p, "hr", false, "mcp.allowed_schemas");
  expect_table(p, "mysql_mcp", false, nullptr);
  expect_table(p, "information_schema", false, nullptr);
  p.deny_system_schemas = false;
  p.read_only = false;
  expect_table(p, "mysql", false, nullptr);
  expect_table(p, "shop", true, nullptr);
  expect_table(p, "hr", true, "mcp.allowed_schemas");

  std::printf("%s (%d failures)\n", failures ? "FAILED" : "ok", failures);
  return failures ? 1 : 0;
}
