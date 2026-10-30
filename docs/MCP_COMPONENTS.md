# MCP components

Server-side components for [mysql-mcp-server](https://github.com/askdba/mysql-mcp-server), designed in its [RFC 0001](https://github.com/askdba/mysql-mcp-server/blob/main/docs/rfc/0001-mysql-component-architecture.md). They are opt-in and separate from MyVector: `MYVECTOR_BUILD_MCP=ON ./scripts/build-component-<ver>-docker.sh` builds `component_mcp_guard.so` next to the MyVector component.

| Component | Status | Servers |
|---|---|---|
| `component_mcp_guard` | Phase 2: identity, read-only and schema enforcement, status variables | MySQL 8.4, 9.7, 26.10 |
| `mysql_mcp` SQL tool pack ([`sql/mcp/`](https://github.com/askdba/myvector/tree/main/sql/mcp)) | 1.0.0 | MySQL 8.0 to 26.10, MariaDB 11.4, 12.3 |

## component_mcp_guard

Enforces a policy on the sessions of mysql-mcp-server, inside `mysqld`, so a client cannot widen it. Grants stay the primary control (MCP accounts should be `SELECT`-only); the guard covers what grants cannot express: a kill switch, read-only even when an account has write grants, and schema limits for accounts with global `SELECT`.

### Install

```sql
-- copy component_mcp_guard.so into @@plugin_dir first
INSTALL COMPONENT 'file://component_mcp_guard';
GRANT MCP_ADMIN ON *.* TO 'dba'@'%';          -- who may change mcp.* settings
GRANT MCP_USER  ON *.* TO 'mcp'@'%';          -- the account mysql-mcp-server uses
GRANT EXECUTE   ON mysql_mcp.* TO 'mcp'@'%';  -- if the SQL tool pack is installed
```

`UNINSTALL COMPONENT 'file://component_mcp_guard'` removes the privileges, variables and status variables. Grants of `MCP_USER` and `MCP_ADMIN` stay in the grant tables until revoked.

### MCP sessions

A session is an **MCP session** when its account holds `MCP_USER` and not `MCP_ADMIN`. This is decided at connect and at `COM_CHANGE_USER`, from privileges only; connection attributes and statement comments cannot change it.

- `MCP_ADMIN` holders are never guarded. After the component is installed, `GRANT ALL ON *.*` includes both privileges, so an administrator granted that way is not put under the guard.
- **Privilege changes take effect at the next connection.** Sessions already connected when the component is installed, or when `MCP_USER` / `MCP_ADMIN` is granted or revoked, keep their previous status. Disconnect them (`KILL CONNECTION`) or have the client reconnect after such a change.
- Grant `MCP_USER` to the account itself; a role activated later with `SET ROLE` is not considered.

### Settings

| Variable | Default | Effect on MCP sessions |
|---|---|---|
| `mcp.enabled` | `ON` | `OFF` refuses new MCP connections and every statement of existing ones (kill switch). |
| `mcp.read_only` | `ON` | Only reads, whatever the account's grants (below). |
| `mcp.deny_system_schemas` | `ON` | No table access in `mysql` and `sys`. `information_schema` and `performance_schema` stay readable (the tools use them; grants govern them). |
| `mcp.allowed_schemas` | empty | Comma-separated schemas whose tables MCP sessions may access; blank means all. A value with no names in it, such as `,`, allows none. The system schemas above are governed by `mcp.deny_system_schemas`, and `mysql_mcp` is always allowed. |

`SET GLOBAL` and `SET PERSIST` of `mcp.*` need `MCP_ADMIN` on top of `SYSTEM_VARIABLES_ADMIN`. Persisted values load at startup as usual. Values given in `INSTALL COMPONENT ... SET` are taken from the installer.

### What mcp.read_only allows

Statements are classified by the server's `sql_command`, so the decision does not depend on parsing. Allowed:

- `SELECT` (including `EXPLAIN`), every `SHOW`, `DO`, `USE`, `HELP`, `CHECK`, `CHECKSUM`, `HANDLER` reads, `KILL`, transaction control (`BEGIN`, `COMMIT`, `ROLLBACK`, savepoints), `SIGNAL`/`RESIGNAL`, `GET DIAGNOSTICS`, `UNLOCK TABLES`.
- `SET` of session and user variables.
- `CALL` into the tool pack: `CALL mysql_mcp.<routine>(...)`, schema-qualified.
- `PREPARE`, `EXECUTE`, `DEALLOCATE PREPARE`: the executed statement raises its own event and is checked as itself. Statements sent as binary-protocol prepared statements (as the Go driver does) are checked the same way.

Denied: everything else, including any `sql_command` a future server adds. Four cases also look at the statement text, after string literals and comments are removed (executable comments `/*! ... */` and `/*M! ... */` count as code). Where a string ends depends on `sql_mode` (`NO_BACKSLASH_ESCAPES`), which a session may change, so the text is read both with and without backslash escapes, and a statement is denied if either reading finds one of these:

- `SELECT ... INTO OUTFILE` / `INTO DUMPFILE`.
- Locking reads: `FOR UPDATE`, `FOR SHARE`, `LOCK IN SHARE MODE`.
- `SET GLOBAL`, `SET PERSIST`, `SET PERSIST_ONLY`, `@@global.`, `@@persist.`.
- `CALL` of anything outside `mysql_mcp`, or an unqualified `CALL`.

Table access events catch writes the statement class cannot see, for example a stored function that inserts, called from a `SELECT`.

Because of the double reading, a `SELECT` whose string literal contains a backslash-escaped quote followed by text such as `FOR UPDATE` (`'it\'s FOR UPDATE'`) is denied even though it only reads. Write such a literal with a doubled quote (`'it''s FOR UPDATE'`) instead.

`CALL` is allowed for any routine in the `mysql_mcp` schema, not only the tool pack's. Keep only the tool pack in `mysql_mcp` (it is installed and dropped as a whole).

### Errors

A denial fails the statement with error **1290** (`ER_OPTION_PREVENTS_STATEMENT`), the server's own `read_only` error, naming the setting:

```
ERROR 1290 (HY000): The MySQL server is running with the mcp.read_only option so it cannot execute this statement
```

The option is `mcp.read_only`, `mcp.deny_system_schemas`, `mcp.allowed_schemas` or `mcp.enabled`. A `SET` of `mcp.*` without `MCP_ADMIN` fails with error 1227 (`ER_SPECIFIC_ACCESS_DENIED_ERROR`).

### Status variables

| Variable | Meaning |
|---|---|
| `mcp.sessions_connected` | MCP sessions now connected |
| `mcp.statements_total` | Statements started by MCP sessions (nested statements of stored programs not counted) |
| `mcp.statements_denied` | Statements refused by the statement checks (`mcp.read_only`, `mcp.enabled`), including refused connections |
| `mcp.table_access_denied` | Statements refused by a table access (`mcp.allowed_schemas`, `mcp.deny_system_schemas`, a write under `mcp.read_only` seen only at table level). Add both counters for all refusals |

### Limits

- `mcp.allowed_schemas` limits **table access**, not catalog metadata: `information_schema` and `SHOW` still list what the account's grants allow.
- With `mcp.deny_system_schemas = ON`, mysql-mcp-server's opt-in `slow_query_log` tool (which reads `mysql.slow_log`) is denied.
- Planned (RFC 0001): a `mcp.max_execution_time_ms` floor, denials in the error log and audit log, `performance_schema.mcp_tool_calls`.

### Tests

`scripts/test-mcp-guard.sh <mysql-tag> <artifact-dir>` runs install, restart, persistence, uninstall and the enforcement cases against the official image (57 checks), and `src/mcp/guard/test_mcp_guard_policy.cc` unit-tests the classification and the SQL scanner. CI (`integration-scripts.yml`, job `mcp-guard`) runs both on MySQL 8.4.11, 9.7.2 and 26.10.0.
