# MCP components

Server-side components for [mysql-mcp-server](https://github.com/askdba/mysql-mcp-server), designed in its [RFC 0001](https://github.com/askdba/mysql-mcp-server/blob/main/docs/rfc/0001-mysql-component-architecture.md). They are opt-in and separate from MyVector: `MYVECTOR_BUILD_MCP=ON ./scripts/build-component-<ver>-docker.sh` builds `component_mcp_guard.so` next to the MyVector component.

| Component | Status | Servers |
|---|---|---|
| `component_mcp_guard` | Phase 2: identity, read-only and schema enforcement, time limit, error log, status variables | MySQL 8.4, 9.7, 26.10 |
| `mysql_mcp` SQL tool pack ([`sql/mcp/`](https://github.com/askdba/myvector/tree/main/sql/mcp)) | 1.0.0 | MySQL 8.0 to 26.10, MariaDB 11.4, 12.3 |

## component_mcp_guard

Enforces a policy on the sessions of mysql-mcp-server, inside `mysqld`, so a client cannot widen it. Grants stay the primary control (MCP accounts should be `SELECT`-only); the guard covers what grants cannot express: a kill switch, read-only even when an account has write grants, and schema limits for accounts with global `SELECT`.

### Install

```sql
-- copy component_mcp_guard.so into @@plugin_dir first: the build for this server's
-- exact release (a build for another patch release, even 8.4.8 on 8.4.11, can crash it)
INSTALL COMPONENT 'file://component_mcp_guard';
GRANT MCP_ADMIN ON *.* TO 'dba'@'%';          -- who may change mcp.* settings
GRANT MCP_USER  ON *.* TO 'mcp'@'%';          -- the account mysql-mcp-server uses
GRANT EXECUTE   ON mysql_mcp.* TO 'mcp'@'%';  -- if the SQL tool pack is installed
```

`UNINSTALL COMPONENT 'file://component_mcp_guard'` removes the privileges, variables and status variables. Grants of `MCP_USER` and `MCP_ADMIN` stay in the grant tables until revoked.

### Docker images (mysql-mcp)

`ghcr.io/askdba/mysql-mcp:<8.4|9.7|26.10>` (amd64, arm64; also tagged with the exact release, such as `8.4.13`) is the official `mysql` image of the release the component was built for, with `component_mcp_guard` and the `mysql_mcp` SQL tool pack. It is separate from the MyVector images. On first start, with an empty data directory, it installs both, grants `MCP_ADMIN` to the root accounts, and reads these settings:

| Variable | Effect |
|---|---|
| `MYSQL_MCP_USER`, `MYSQL_MCP_PASSWORD` | Create this account (host `%`; a plain name of up to 32 characters from `A-Z a-z 0-9 _ . -`) for mysql-mcp-server, with `MCP_USER`, `EXECUTE` on `mysql_mcp` and `SELECT, SHOW VIEW` on the databases below. |
| `MYSQL_MCP_DATABASES` | Comma-separated databases the account may read. The default is `MYSQL_DATABASE`. |
| `MYSQL_MCP_ALLOWED_SCHEMAS` | Persisted as `mcp.allowed_schemas`. |
| `MYSQL_MCP_MAX_EXECUTION_TIME_MS` | Persisted as `mcp.max_execution_time_ms`. |

The other settings keep their defaults (read-only on). The settings are checked before the data directory is initialized, and a container with an invalid combination does not start. Once the data directory exists they are not used or checked again. For example, `MYSQL_MCP_USER` must differ from `MYSQL_USER` (or the user in `MYSQL_USER_FILE`), because the official entrypoint gives that account all privileges on `MYSQL_DATABASE`. The health check asks mysqld for the port it uses (command line and option files, as mysqld resolves them) and pings it without logging in as root. The official image's own variables (`MYSQL_ROOT_PASSWORD`, `MYSQL_DATABASE`, ...) work as usual.

```sh
docker run -d -p 3306:3306 -e MYSQL_ROOT_PASSWORD=secret -e MYSQL_DATABASE=shop \
  -e MYSQL_MCP_USER=mcp -e MYSQL_MCP_PASSWORD=mcp-secret ghcr.io/askdba/mysql-mcp:9.7
MYSQL_DSN='mcp:mcp-secret@tcp(127.0.0.1:3306)/shop' mysql-mcp-server
```

The images are built and published with MyVector releases (`docker-publish.yml`). `scripts/test-mysql-mcp-image.sh <image>` tests the first-start setup, and CI (`integration-scripts.yml`, job `mcp-guard`) runs it on each version.

### MCP sessions

A session is an **MCP session** when its account holds `MCP_USER` and not `MCP_ADMIN`. This is decided at connect and at `COM_CHANGE_USER`, from privileges only; connection attributes and statement comments cannot change it.

- `MCP_ADMIN` holders are never guarded. After the component is installed, `GRANT ALL ON *.*` includes both privileges, so an administrator granted that way is not put under the guard.
- **Privilege changes take effect at the next connection.** Sessions already connected when the component is installed, or when `MCP_USER` / `MCP_ADMIN` is granted or revoked, keep their previous status. Disconnect them (`KILL CONNECTION`) or have the client reconnect after such a change.
- Grant `MCP_USER` to the account itself; a role activated later with `SET ROLE` is not considered.
- **Sessions opened by components.** A component can run SQL as an account through the command services: a `"local"` session that impersonates the account, as `component_mcp` (RFC 0001 Phase 4) and `component_mysql_rest_service` do. Such a session raises no connection event, so the guard classifies it from its privileges at its first statement.
  - The server starts these sessions **unaudited**: no statement or connection event reaches any audit consumer, the guard included.
  - A component that runs SQL for MCP accounts must therefore emit `MYSQL_AUDIT_CONNECTION_CONNECT` for the session's THD after connecting (`mysql_audit_api_connection`), and `..._DISCONNECT` before closing. The CONNECT event turns auditing on.
  - `component_mcp` does this. A component that doesn't runs its SQL unseen by the guard and by audit log plugins.
  - `scripts/test-mcp-guard.sh` checks both cases with a test-only component (`src/mcp/guard/test/component_mcp_session_probe.cc`).

### Settings

| Variable | Default | Effect on MCP sessions |
|---|---|---|
| `mcp.enabled` | `ON` | `OFF` refuses new MCP connections and every statement of existing ones (kill switch). |
| `mcp.read_only` | `ON` | Only reads, whatever the account's grants (below). |
| `mcp.deny_system_schemas` | `ON` | No table access in `mysql` and `sys`. `information_schema` and `performance_schema` stay readable (the tools use them; grants govern them). |
| `mcp.allowed_schemas` | empty | Comma-separated schemas whose tables MCP sessions may access; blank means all. A value with no names in it, such as `,`, allows none. Names are compared exactly, or case-insensitively when `lower_case_table_names` is 1 or 2. The system schemas above are governed by `mcp.deny_system_schemas`, and `mysql_mcp` is always allowed. |
| `mcp.max_execution_time_ms` | 0 | Time limit for their `SELECT` statements, which they cannot raise; 0 turns the guard's limit off (the server's own `max_execution_time` default still applies) (below). |
| `mcp.log_denials` | `ON` | Write refusals to the error log, at most 10 lines a minute (below). |
| `mcp.tool_calls_history_size` | 1000 | Tool calls kept in `performance_schema.mcp_tool_calls` (below); 0 keeps none. Read-only: set it at startup or in `INSTALL COMPONENT ... SET`. |

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

### Time limit (mcp.max_execution_time_ms)

When an MCP session connects, the guard sets its session `max_execution_time` to `mcp.max_execution_time_ms`, so its `SELECT` statements stop after that many milliseconds (error 3024). The session may lower it, but not raise it. These are refused with error 1290 naming `mcp.max_execution_time_ms`:

- `SET [SESSION] max_execution_time` (or `@@max_execution_time`) to 0, above the limit, or to anything but a literal (`DEFAULT`, an expression, a variable).
- The optimizer hints `MAX_EXECUTION_TIME(n)` and `SET_VAR(max_execution_time = n)` with such a value.

A changed limit reaches connected MCP sessions at their next statement, and the limit is applied again after `COM_CHANGE_USER` and `COM_RESET_CONNECTION`, which reset session variables. Setting it back to 0 leaves their current session value as it is.

As with the server's own `max_execution_time`, the limit covers top-level read-only `SELECT` statements. MySQL ignores it inside stored programs, so **`CALL mysql_mcp.*` tool-pack calls are not limited**. Those run catalog queries on `information_schema`. If they must be limited too, revoke `EXECUTE` on `mysql_mcp` from the MCP account: mysql-mcp-server then falls back to its own SQL, whose `SELECT`s the limit covers.

### Error log (mcp.log_denials)

Each refusal is written as a warning, with the setting, the connection id and the start of the statement (up to 256 bytes, string literals replaced by `'?'`, line breaks by spaces) or the table:

```
[Warning] [MY-011071] [mcp_guard] mcp_guard: refused by mcp.read_only: MCP connection 11: INSERT INTO d.t VALUES (1)
```

At most 10 lines are written per minute; the number left out is reported with the next line after the minute. String literals are redacted, but numbers and identifiers are kept; turn `mcp.log_denials` off if even those must not reach the error log.

### Audit log

Each refusal is also emitted as an audit message (the `mysql_audit_api_message` service, subclass `MYSQL_AUDIT_MESSAGE_INTERNAL`), so an audit log plugin or component that records message events records it, whatever `mcp.log_denials` says. The component is `mcp_guard`, the producer `component_mcp_guard` and the message `refused`, with these keys:

| Key | Value |
|---|---|
| `option` | the setting that refused it, as in the error |
| `connection_id` | the connection (a number) |
| `statement` | the statement with string literals redacted as in the error log, or `read db.table` / `write db.table` |
| `tool`, `request_id` | the tool call, when the statement is labeled (next section) |

### performance_schema.mcp_tool_calls

mysql-mcp-server starts every statement of a tool call with a label, `/* mcp:tool=<tool> req=<request id> */`. For MCP sessions the guard groups labeled statements into one row per tool call (one request id on one connection), keeping the latest `mcp.tool_calls_history_size` calls:

| Column | Meaning |
|---|---|
| `CONNECTION_ID` | the connection |
| `ACCOUNT` | the session's account, `user@host` as granted |
| `TOOL`, `REQUEST_ID` | from the label |
| `STATEMENTS` | labeled top-level statements of the call |
| `ERRORS` | those that failed, refusals included |
| `DENIED` | refusals by the guard |
| `LATENCY_US` | total time of the statements, in microseconds |
| `FIRST_SEEN`, `LAST_SEEN` | first statement start and last statement end |

```sql
SELECT TOOL, COUNT(*) AS calls, SUM(DENIED) AS denied, SUM(LATENCY_US) / 1000 AS ms
FROM performance_schema.mcp_tool_calls GROUP BY TOOL ORDER BY ms DESC;
```

The table is read-only and lives in memory: it is empty after a restart or reinstall. The label only attributes statements; who is an MCP session is decided by `MCP_USER`, so labels from other accounts are ignored, and a session can write any label it likes (the account column is the trusted one). The label must be at the very start of the statement, which the `mysql` client keeps only with `--comments`.

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
- The schema rules apply to a statement's **base tables**. A view in an allowed schema over a table in another schema (or in `mysql`) is denied, nested views included. A view in another schema whose base tables are all allowed is allowed.
- **Stored functions and triggers: their reads are not checked by the schema rules.** Their statements run on tables the calling statement has already locked, so the server raises no table access event for them. For example, with `mcp.allowed_schemas = 'd1'`, `SELECT d1.f()` returns data from `d2` if `d1.f()` reads `d2`. Writes inside them are still refused by `mcp.read_only` (a nested statement check), and stored procedures are not affected (`CALL` is limited to `mysql_mcp` under `mcp.read_only`, and a procedure's statements raise their own events). Control this with grants: don't give MCP accounts `EXECUTE` on schemas with functions that read data they must not see. A `SQL SECURITY INVOKER` function runs with the account's own grants, but `mcp.allowed_schemas` doesn't limit it. Triggers need no `EXECUTE`: they run, with their definer's privileges, when a statement writes their table. Under `mcp.read_only` (the default) MCP sessions cannot write, so no trigger runs for them. If you turn `mcp.read_only` off, don't give MCP accounts write grants on tables whose triggers read data outside the account's allowed schemas, and review trigger definers. `scripts/test-mcp-guard.sh` keeps a check that shows this, so a server change is noticed ([#247](https://github.com/askdba/myvector/issues/247)).
- A multi-statement packet is checked statement by statement: each statement in it raises its own events.
- **Statements whose table reads raise no table access events are refused** while `mcp.deny_system_schemas` is on (the default) or `mcp.allowed_schemas` is set. The server raises table access events only for `SELECT`, data changes, `ANALYZE` and `HANDLER READ`, so these statements would otherwise read any table unseen: a subquery in `SET`, `DO`, a `CALL` argument or `SHOW ... WHERE`, and `CHECK TABLE` / `CHECKSUM TABLE`. Read through `SELECT` instead (`SELECT ... INTO @x`). The refusal names the schema rule in effect. `SET`, `SHOW` and `CALL` without subqueries are unaffected.
- With `lower_case_table_names` 1 or 2, the system schema names are compared case-insensitively, as the server compares them.

### Tests

`scripts/test-mcp-guard.sh <mysql-tag> <artifact-dir>` runs install, restart, persistence, uninstall and the enforcement cases, the tool-call table and its labels, views, multi-statement packets, statements without table access events, sessions opened by components and the parity table below against the official image (120 checks, including a `lower_case_table_names=1` server), and `src/mcp/guard/test_mcp_guard_policy.cc` unit-tests the classification and the SQL scanner. CI (`integration-scripts.yml`, job `mcp-guard`) runs both on MySQL 8.4.11, 9.7.2 and 26.10.0.

**Parity with mysql-mcp-server** (RFC 0001 §5.2 item 7): [`sql/mcp/guard-parity.tsv`](https://github.com/askdba/myvector/blob/main/sql/mcp/guard-parity.tsv) lists statements a `run_query` call might send, with the verdict of mysql-mcp-server's validator (`go`) and of the guard under the default settings (`guard`). Every row where they differ says why. The test checks the guard column, sending each statement as one packet. mysql-mcp-server vendors the file and checks the go column. Both reject every write and locking read. The guard allows a few things the validator does not (session `SET`, `information_schema`, `CALL mysql_mcp.*`) and catches what text checks cannot (a write inside a function called from `SELECT`).
