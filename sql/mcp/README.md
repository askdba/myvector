# `mysql_mcp` SQL tool pack

Stored procedures and views that implement the tool logic of
[mysql-mcp-server](https://github.com/askdba/mysql-mcp-server) once, in SQL, for
MySQL 8.0 through 26.10. The Go server and `component_mcp` will call it. In Phase 2,
`component_mcp_guard` will exempt its routines from the guard's `CALL` restriction (in
Phase 0 the guard only registers the `MCP_USER` and `MCP_ADMIN` privileges). See [RFC 0001](https://github.com/askdba/mysql-mcp-server/blob/main/docs/rfc/0001-mysql-component-architecture.md), §5.1.

**License:** everything in this directory is licensed under the Apache License 2.0
(see [`LICENSE`](LICENSE)), unlike the rest of this repository (GPLv2). The SQL files
are standalone scripts that are not linked into any component, and mysql-mcp-server
(Apache-2.0) vendors them.

**Reserved names:** `component_mcp_guard` registers the dynamic privileges `MCP_USER` and
`MCP_ADMIN` and unregisters them on `UNINSTALL COMPONENT`. MySQL's privilege registry has
no owner, so other components must not register these names.

## Version 1.0.0 (Phase 1)

[`mysql_mcp.sql`](mysql_mcp.sql) installs the schema `mysql_mcp`. It has one procedure per
catalog tool of mysql-mcp-server, and each returns exactly the result set of the query the
Go server runs without the pack: same columns, order and rows. The Go server keeps input
checks, access control (`MYSQL_MCP_ALLOWED_DATABASES`), row limits and error messages.

| Procedure | Tool |
|---|---|
| `list_databases()` | `list_databases` |
| `list_tables(db)` | `list_tables` |
| `describe_table(db, table)` | `describe_table` |
| `show_create_table(db, table)` | `show_create_table` |
| `list_indexes(db, table)` | `list_indexes` |
| `list_views(db)`, `list_triggers(db)`, `list_procedures(db)`, `list_functions(db)` | same names |
| `list_partitions(db, table)` | `list_partitions` |
| `database_size(db or NULL)` | `database_size` (NULL: all but system schemas) |
| `table_size(db, table or NULL)`, `foreign_keys(db, table or NULL)` | same names |
| `search_tables(pattern, db or NULL, limit)`, `search_columns(pattern, db or NULL, limit)` | `search_schema` |
| `list_status(pattern or NULL)` | `list_status` (`performance_schema.global_status`) |
| `list_variables(pattern or NULL)` | `list_variables` (`SHOW GLOBAL VARIABLES`) |
| `process_list()` | `process_list` |
| `vector_columns(db, table or NULL)`, `vector_column_index(db, table, column)` | `vector_info` |
| `version()` (function) | pack version, `1.0.0` |

Not in SQL, by design ([RFC 0001](https://github.com/askdba/mysql-mcp-server/blob/main/docs/rfc/0001-mysql-component-architecture.md) §5.1): `run_query` and `explain_query`, which take caller SQL; `schema_diff` across servers; and the connection tools.

### Install, upgrade, remove

```sql
-- as an account with CREATE, CREATE ROUTINE and DROP on mysql_mcp
SOURCE mysql_mcp.sql;          -- re-run to upgrade: every routine is dropped and recreated
GRANT EXECUTE ON mysql_mcp.* TO 'mcp'@'%';
SOURCE uninstall.sql;          -- DROP DATABASE mysql_mcp
```

mysql-mcp-server vendors these files; `mysql-mcp-server install-schema` runs the same script.

### Design

- **`SQL SECURITY INVOKER`** everywhere: a caller sees only what its own grants allow. MCP
  accounts need only `EXECUTE` on `mysql_mcp` plus their usual data grants.
- **Prepared statements inside.** Each procedure runs a fixed statement text with its
  arguments bound to `?`, as the Go driver does. A routine parameter compared directly would
  bring its own collation: case-insensitive name matching on MySQL, and "Illegal mix of
  collations" on MariaDB 11.5+ (`information_schema` uses `utf8mb3_uca1400_ai_ci`). A bound
  `?` takes the column's collation on every server. Arguments pass through the session
  variables `@mysql_mcp_1`..`@mysql_mcp_3`, which are reset afterwards, also on error.
- **No SQL arguments.** For `SHOW INDEX` and `SHOW CREATE TABLE`, the names are first looked
  up in `information_schema.TABLES` (error 1146 if not visible), then quoted.
- **Deterministic order.** Every result is fully ordered, with a binary tiebreak where names
  can differ only in case, so row limits always cut at the same place.

### Supported servers

| Server | Status |
|---|---|
| MySQL 8.0, 8.4, 9.7, 26.10 | ✅ |
| MariaDB 11.4, 12.3 | ✅ |
| Managed services | where `CREATE ROUTINE` is allowed; otherwise the Go server keeps its embedded SQL |

### Tests

`scripts/test-mcp-tool-pack.sh <image>` installs the pack twice. It then compares every
procedure with the Go server's query, run as a prepared statement, as root and as an
`EXECUTE`-only account. The fixture has mixed-case schemas, a table name with a backtick,
views, triggers, routines, partitions, foreign keys and (MySQL 9+) `VECTOR` columns. It also
checks the safety rules. CI (`integration-scripts.yml`, job `mcp-tool-pack`) runs it on
MySQL 8.0, 8.4, 9.7 and 26.10 and MariaDB 11.4 and 12.3.
