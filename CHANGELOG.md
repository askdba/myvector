# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`component_mcp_guard` tests for views, multi-statement packets and parity** (RFC 0001 §5.2 item 7, Q3). `scripts/test-mcp-guard.sh` now has 102 checks on 8.4.11, 9.7.2 and 26.10.0.
  - **Views:** a view over another schema or over `mysql` is refused, nested views included (the rules see base tables).
  - **Multi-statement packets:** a write or disallowed read in a later statement of one packet is refused, and nothing is written.
  - **Parity table:** `sql/mcp/guard-parity.tsv` (34 statements) has the shared verdicts of mysql-mcp-server's `run_query` validator and the guard, with the reason for each difference. The guard column is checked here; mysql-mcp-server checks the other.
  - **Known limitation, documented:** reads inside stored functions and triggers aren't seen by the schema rules, because the server raises no table access events for tables the calling statement locked. Writes inside them are still refused. `docs/MCP_COMPONENTS.md` (Limits) gives the mitigation (grants), and a test check shows the behavior ([#247](https://github.com/askdba/myvector/issues/247)).
- **`mysql-mcp` Docker images** (Phase 3 of RFC 0001). `ghcr.io/askdba/mysql-mcp:<8.4|9.7|26.10>` (also tagged with the exact release) is the official MySQL image of the release the component is built for, with `component_mcp_guard` and the `mysql_mcp` tool pack installed on first start. `MYSQL_MCP_USER`/`MYSQL_MCP_PASSWORD` create an MCP account (`MCP_USER`, tool pack, read access to `MYSQL_MCP_DATABASES`), and `MYSQL_MCP_ALLOWED_SCHEMAS`/`MYSQL_MCP_MAX_EXECUTION_TIME_MS` persist guard settings. The images are a separate family from the MyVector images. They are published by `docker-publish.yml` with releases and tested in CI by `scripts/test-mysql-mcp-image.sh` (22 checks on 8.4.11, 9.7.2 and 26.10.0).
- **`component_mcp_guard` tool calls and audit messages** (Phase 3 of RFC 0001, server side).
  - **`performance_schema.mcp_tool_calls`:** one row per mysql-mcp-server tool call (account, tool, request id, statements, errors, refusals, latency, first and last seen). The guard groups the MCP session statements that start with the label `/* mcp:tool=... req=... */`, keeping the last `mcp.tool_calls_history_size` calls (new read-only variable, default 1000).
  - **Audit messages:** each refusal is emitted through `mysql_audit_api_message` (component `mcp_guard`, message `refused`, with the option, connection id, redacted statement and the tool call), so audit log consumers record refusals.
  - **Tests:** `scripts/test-mcp-guard.sh` now has 90 checks, passing on MySQL 8.4.11, 9.7.2 and 26.10.0. The label parser is unit-tested.
- **`component_mcp_guard` time limit and refusal log** (Phase 2 of RFC 0001, completed).
  - **`mcp.max_execution_time_ms`:** sets an MCP session's `max_execution_time` at connect. Attempts to raise it are refused with error 1290: via `SET`, or via the `MAX_EXECUTION_TIME(n)` or `SET_VAR(max_execution_time=n)` hints. Lowering it is allowed.
  - **`mcp.log_denials` (default ON):** writes each refusal to the error log as `[MY-011071] [mcp_guard]`, with string literals redacted, at most 10 lines a minute.
  - **Tests:** `scripts/test-mcp-guard.sh` now has 80 checks, including a 5 s `SELECT` stopped by a 1 s limit, a tightened limit reaching a connected session, literal redaction in the log and the log rate limit on a fresh server, passing on MySQL 8.4.11, 9.7.2 and 26.10.0.
- **`component_mcp_guard` enforcement** (Phase 2 of [RFC 0001](https://github.com/askdba/mysql-mcp-server/blob/main/docs/rfc/0001-mysql-component-architecture.md)).
  - **MCP sessions:** sessions of accounts holding `MCP_USER` (and not `MCP_ADMIN`) are checked against `mcp.*` settings inside `mysqld`. The checks run on the server's query, table-access and connection events.
  - **`mcp.read_only` (default ON):** only reads, even with write grants. It uses an allowlist of `sql_command` classes and also refuses `SELECT … INTO OUTFILE`, locking reads, `SET GLOBAL` and `CALL` outside `mysql_mcp`.
  - **Schema settings:** `mcp.deny_system_schemas` (default ON) blocks table access in `mysql` and `sys`. `mcp.allowed_schemas` limits table access to listed schemas.
  - **`mcp.enabled`:** a kill switch.
  - **Errors and administration:** denials fail with error 1290. Changing `mcp.*` needs `MCP_ADMIN`.
  - **Status variables:** `mcp.sessions_connected`, `mcp.statements_total`, `mcp.statements_denied` and `mcp.table_access_denied`.
  - **Tests:** `scripts/test-mcp-guard.sh` grew from a lifecycle test to 65 checks (enforcement, administration, persistence, restart, uninstall, `lower_case_table_names=1`), passing on MySQL 8.4.11, 9.7.2 and 26.10.0. `src/mcp/guard/test_mcp_guard_policy.cc` unit-tests the classification.
  - **Docs:** `docs/MCP_COMPONENTS.md`.
- **`component_mcp_guard` skeleton** (Phase 0 of [RFC 0001](https://github.com/askdba/mysql-mcp-server/blob/main/docs/rfc/0001-mysql-component-architecture.md) in mysql-mcp-server). A separate component for mysql-mcp-server sessions. In Phase 0 it only registered the `MCP_USER` and `MCP_ADMIN` dynamic privileges; enforcement arrived in Phase 2 (entry above). It is opt-in and does not change the MyVector build: `MYVECTOR_BUILD_MCP=ON ./scripts/build-component-<ver>-docker.sh` also builds and packages `component_mcp_guard.so`. `scripts/test-mcp-guard.sh <ver> <artifact-dir>` checks install, privilege registration, grants, restart and uninstall against the official `mysql:<ver>` image. Verified on MySQL 8.4.11, 9.7.2 and 26.10.0.
- **MySQL 26.10 component build script** (`scripts/build-component-26.10-docker.sh`). It configures the server tree with `-DWITH_AUTHENTICATION_WEBAUTHN=OFF`: 26.10 enables WebAuthn by default, which needs `libudev`/`libfido2` headers the build container does not have, and the component build only needs generated headers.
- **`mysql_mcp` SQL tool pack 1.0.0** (`sql/mcp/mysql_mcp.sql`, Phase 1 of RFC 0001). One `SQL SECURITY INVOKER` procedure per catalog tool of mysql-mcp-server (18 tools), each returning exactly the result set of the Go server's own query. Each runs fixed statement text as a prepared statement with bound arguments, so names compare exactly as they do for the Go server on MySQL 8.0 through 26.10 and MariaDB 11.4 and 12.3. Also includes `CALL mysql_mcp.version()` (a procedure, so installing needs no SUPER or `log_bin_trust_function_creators` with binary logging on) and `sql/mcp/uninstall.sql`. `scripts/test-mcp-tool-pack.sh <image>` checks install, upgrade, parity (as root and as an `EXECUTE`-only account) and the safety rules; the new `mcp-tool-pack` CI job runs it on all six servers.
- **`sql/mcp/`**: home of the `mysql_mcp` SQL tool pack for mysql-mcp-server, licensed **Apache-2.0** (`sql/mcp/LICENSE`, noted in `NOTICE`), unlike the rest of the repository.
- **`myvectorbench.py --server host`** (issue #133). Benchmarks against a real `mysqld` on
  the host instead of Docker. It downloads Oracle's generic tarball for the matching patch
  release once, starts an isolated server from it, and times every query client-side over one
  persistent connection (`SELECT 1` round trip about 0.1 ms), instead of server-side `NOW(6)`
  markers around a `mysql` client inside a container. With nothing else in the way,
  brute-force KNN on the component (about 150 ms p50 at 10k rows) turns out roughly 15x
  slower than on the plugin (about 10 ms). `--all-cells` runs plugin 8.4 and components 8.4,
  9.7 and 26.7 in one command. Every result now records the machine (CPU, cores, memory,
  kernel, a `machine_key`), the server mode and the `SELECT 1` round trip, and
  `myvectorbench-compare.py` warns when a baseline comes from another machine type or mode.
  Docker stays the default and CI is unchanged.
- **Skipped online updates are visible** (issue #205). When the binlog listener has to skip
  a table's row events (`binlog_row_image` not FULL, a key column that is not an integer,
  a column type it cannot read), it now logs one warning per table and reason (again
  after the index is rebuilt or reloaded),
  `Online updates for db.t: skipping row events (...)`, and `myvector_index_status` shows
  `Online events skipped : N (reason: n, ...)`. Before, the events were dropped silently, so
  an index that stopped updating gave no hint why. Plugin and component.
- **Movie Finder demo app** (`examples/movie-finder/`). Semantic search over about a million
  TMDB movies, with the vector search in MySQL through MyVector: describe a movie, filter by
  genre, year, rating and language (key-list or `MYVECTOR_ANN_FILTERED` path, chosen and
  explained per query), "more like this", HNSW vs exact search with recall, and adding a
  movie that is searchable at once (`online=Y`). Every panel shows its SQL, and an "Under
  the hood" panel shows the live index, each query's time per step, and recall against
  latency over `ef_search`. One
  `docker compose up`; the TMDB data is downloaded on the user's machine, never shipped.
  Profiles of 10k, 100k and all 1,035,695 movies; `smoke.py` checks it end to end.
- **`MYVECTOR_UNINSTALL_CHECK()` and `MYVECTOR_PREPARE_UNINSTALL(kill_others)`** (component,
  issue #155). `UNINSTALL COMPONENT` fails with ERROR 3540 while any other session that has
  run a query since the install is connected. This is MySQL behaviour: each session holds a
  reference to the component's `event_tracking_parse` service until it disconnects.
  - `MYVECTOR_UNINSTALL_CHECK()` lists those sessions.
  - `MYVECTOR_PREPARE_UNINSTALL(0)` raises an error naming them, before it stops anything.
    If there are none, it stops the binlog listener.
  - `MYVECTOR_PREPARE_UNINSTALL(1)` KILLs them.
  - `sql/myvector_uninstall_component.sql` now calls `MYVECTOR_PREPARE_UNINSTALL(0)`, so it
    stops before dropping anything when other sessions are connected, instead of failing
    at `UNINSTALL` after the procedures are gone.
- **Filtered vector search** (PR #157). `MYVECTOR_IS_ANN` and `myvector_ann_set` take an
  optional fifth argument: the keys of the rows that may be returned. The search returns
  the `k` nearest rows among them, so it returns `k` rows whenever at least `k` rows match.
  Before, a filter written next to `MYVECTOR_IS_ANN` was applied to the `k` results
  afterwards, and could return fewer rows or none.
  ```sql
  SELECT id FROM docs
  WHERE MYVECTOR_IS_ANN('db.docs.embedding', 'id', @q, 10,
        (SELECT JSON_ARRAYAGG(id) FROM docs WHERE category = 'books'));
  ```
  HNSW indexes compute exact distances when 10,000 or fewer keys are allowed, and walk the
  graph with the filter above that. Component builds call `myvector_ann_set(...)` with the
  same fifth argument. New test: `scripts/test-filtered-ann.py` (plugin and component).
- **Filtered search for broad filters.** A new stored procedure,
  `mysql.MYVECTOR_ANN_FILTERED(index, key column, query vector, k, predicate)`, on plugin
  and component builds. It takes the filter as a `WHERE` predicate instead of a key list,
  so a filter that matches most of a large table no longer needs a `JSON_ARRAYAGG` of
  millions of keys. It asks the index for candidates, keeps those that pass the predicate,
  and asks for more until `k` pass. After 10,000 candidates it falls back to the key list,
  so it still returns `k` rows whenever at least `k` indexed rows match.
  ```sql
  CALL mysql.MYVECTOR_ANN_FILTERED('db.docs.embedding', 'id', @q, 10, 'archived = 0');
  ```
  The procedure is `SQL SECURITY INVOKER`: the predicate runs with the caller's
  privileges. New test: `scripts/test-filtered-ann-broad.py`, which has a `--bench` mode.
- **Query rewrite on component builds** (PR #156, issue #144). Component builds
  (`INSTALL COMPONENT`) now support the inline `MYVECTOR(...)` column type and
  `WHERE MYVECTOR_IS_ANN(...)`, as plugin builds always have. The rewrite now uses MySQL's
  Event Tracking Parse service. Before, the component's rewrite code depended on a
  `query_rewrite.h` header that no supported MySQL version has, so it was never compiled
  and component builds had no rewrite. Verified on MySQL 8.4.8, 8.4.11, 9.7.0 and 26.7.0
  (PR #177). The `MYVECTOR COLUMN` comment and `myvector_ann_set()` still work on every
  build.
- **Docs:** a "Declaring a Vector Column" section in `docs/usage.md` covering the plugin and
  comment forms, index types, type errors and multi-line comments. There are worked
  filtered-search examples, and a troubleshooting entry for online indexes that fail to
  load (PR #161).
- **CI:** a `unit-tests` job runs the option-parser tests
  (`tests/test_myvector_options.cc`) (PR #159). The plugin `test` job checks that
  `MYVECTOR(type=<unknown>)` fails at `CREATE TABLE` (PR #161). A new, blocking
  `integration-scripts.yml` workflow runs the Docker-based test scripts
  (`test-filtered-ann.py`, `test-filtered-ann-broad.py`, `test-ef-search.py`,
  `test-online-updates-idle.py`). It covers the plugin (8.4) and the components (8.4, 9.7,
  26.7); before, these scripts ran only by hand.

### Changed
- **Behaviour change: a missing or unknown index `type` is now an error** (PR #160).
  Earlier versions built a brute-force KNN index for any type they did not recognise and
  returned `SUCCESS`, so a typo such as `type=hnws` silently gave exact search with none
  of the HNSW speed.
  - `MYVECTOR_INDEX_BUILD` and `MYVECTOR_INDEX_STATUS` return
    `ERROR: unknown index type 'hnws' for db.t.v. Use type=KNN, HNSW or HNSW_BV`
    (or `ERROR: missing index type for db.t.v. ...`) and build nothing.
  - The plugin's `MYVECTOR(type=<unknown>, ...)` DDL fails at `CREATE TABLE` with
    `MYVECTOR column type invalid`. `MYVECTOR(...)` with no type still defaults to KNN.
  - An `online=Y` column with a bad type is not loaded at startup. The server log shows
    `Online index <name> not loaded: ERROR: ...`, and searches on it fail.
  - **Action on upgrade:** a hand-written column comment with no `type=` must now say
    `type=KNN` explicitly. Correct any bad type with `ALTER TABLE ... MODIFY ... COMMENT`
    and rebuild the index. See [Declaring a Vector Column](docs/usage.md#declaring-a-vector-column).

### Fixed
- **`component_mcp_guard`: sessions opened by components were never guarded** (RFC 0001 Phase 4, open question 4). A component's `"local"` command-service session that impersonates an MCP account raises no connection event, so the guard never marked it as an MCP session. On 9.7.2 such a session could write, read `mysql.user` and run without the time limit.
  - The guard now classifies an unclassified session from its privileges at its first statement. A "not MCP" marker keeps the cost at one grant check per session.
  - `mcp.sessions_connected` is decremented when the session's THD ends, which also covers sessions without a disconnect event.
  - These sessions also start **unaudited** (no events at all), so a component must emit connection audit events for them. This is documented in `docs/MCP_COMPONENTS.md`, and `component_mcp` will do it.
  - `scripts/test-mcp-guard.sh`: 120 checks on 8.4.11, 9.7.2 and 26.10.0, with the test-only `component_mcp_session_probe`. The unfixed guard fails the local-session checks.
- **`component_mcp_guard`: table reads the schema rules could not see** (found reviewing the code behind #247). The server raises table access events only for `SELECT`, data changes, `ANALYZE` and `HANDLER READ`. An MCP session could therefore read any table, `mysql.user` included, past `mcp.allowed_schemas` and `mcp.deny_system_schemas`:
  - directly, with a subquery in `SET` or `DO` (`SET @x = (SELECT … FROM mysql.user); SELECT @x`), also through `PREPARE`/`EXECUTE`;
  - as a yes/no oracle, with a subquery in `SHOW … WHERE` or in a `CALL` argument;
  - checksums only, with `CHECKSUM TABLE` / `CHECK TABLE`.

  These statements are now refused while a schema rule is in effect. System schema names compare case-insensitively under `lower_case_table_names` 1 or 2. `scripts/test-mcp-guard.sh` has 110 checks. The unfixed guard fails all 8 new ones. Three parity rows were added.
- **Post-merge review of #244, #245, #246, #248** (#249; these were merged after rate-limited, not completed, CodeRabbit runs). Fixes:
  - **`mysql-mcp` image:** the settings are checked by an entrypoint wrapper before the data directory is initialized. `MYSQL_MCP_USER` equal to `MYSQL_USER` (or the user in `MYSQL_USER_FILE`) is refused. So is a `MYSQL_USER` with a backslash or quote, which the official entrypoint passes to SQL unescaped (`app\q` creates `appq`), or with leading or trailing spaces, which MySQL trims. The checks run only before initialization, so an initialized volume keeps starting. `MYSQL_MCP_USER` must be a plain name. Before, `CREATE USER` failed in first-start setup and a restart skipped the setup. The health check now asks mysqld for its port (`mysqld <args> --verbose --help`, with a throwaway `--log-bin-index` as the official entrypoint uses), so option files, option groups, `--defaults-file` and `--no-defaults` apply exactly as for the server. It reports not ready while first-start setup runs.
  - **`scripts/build-docker-local.sh`:** builds 9.7 with GCC 11 (9.7.4 needs 11+), 8.0/8.4 with GCC 10, as `docker-publish.yml` does.
  - **`scripts/test-mysql-mcp-image.sh`:** option-file port (`[server]` group), `MYSQL_USER` and `MYSQL_USER_FILE` clash cases, plain-name checks, restart of an initialized volume (34 checks). It uses `mktemp` and a portable wait.
- **Plugin images pinned to their build release.** The plugin images were built for 8.0.45, 8.4.8 and 9.7.0 but based on the floating `mysql:8.0`/`mysql:8.4`/`mysql:9.7` (now 8.0.46, 8.4.13, 9.7.4), and a library built for one MySQL release can crash another. `docker-publish.yml` now builds them for **8.0.46, 8.4.13 and 9.7.4**, bases each image on `mysql:<that release>` and also tags it with the release (`mysql8.0.46`, `mysql8.4.13`, `mysql9.7.4`). The plugin job also runs on a `dry_run` dispatch, building and testing without pushing. `Dockerfile.oraclelinux8`/`9` now require `MYSQL_VERSION`. `scripts/build-docker-local.sh` builds 8.0, 8.4 or 9.7 on the same releases and rejects other versions (it used to map 9.0 to 9.0.0).
- **Component images pinned to their build release.** A component works only on the exact MySQL release it was built against: in a dry run, a `component_mcp_guard` built for 8.4.8 crashed `mysql:8.4` (8.4.11). The images were built for 8.4.8 and 9.7.0 but ran on the floating `mysql:8.4`/`mysql:9.7`. `docker-publish.yml` now builds the component images for **8.4.13, 9.7.4 and 26.10.0** (the releases the floating tags now point to) and bases each one on `mysql:<that release>`. It also tags each image with its release (`mysql8.4.13-component`, `mysql9.7.4-component`, `mysql26.10.0`), and the artifact smoke test runs on the same release. `Dockerfile.component` now requires `MYSQL_VERSION` (no default).
- **Component build scripts reconfigure the server tree after a failed configure.** A `.configure-ok` marker is written only after `cmake` succeeds; before, the scripts trusted `CMakeCache.txt`, which appears before configuration finishes, so a failed configure was skipped on the next run.
- **Component build scripts no longer re-clone the MySQL source on every run.** Inside the build container, git refused the host-owned tree ("dubious ownership"), so the tag check always failed and the tree was deleted and cloned again, which also defeated the CI cache of the source tree. The scripts now mark the tree as a git safe directory.
- **Component: `myvector_construct()` with a constant argument is built once, not per row**
  (issue #214). The component re-parsed a constant literal on every row, so a brute-force
  `ORDER BY myvector_distance(vec, myvector_construct('[...]'), 'L2')` over 10k rows took
  ~157 ms versus ~10 ms on the plugin. The component's `myvector_construct_init` now caches
  the converted vector when every argument is constant (the plugin's #79 behaviour), so KNN
  is ~15x faster and matches the plugin. Per-row inputs (a column, or per-row options) are
  still converted each row.
- **Online updates for `BIGINT` keys** (issue #204). The binlog listener read only `INT`
  keys: for a table keyed by `BIGINT` (or `SMALLINT`, `MEDIUMINT`, `TINYINT`), every
  INSERT, UPDATE and DELETE was skipped and the index never changed, with nothing logged.
  Integer keys of any width are now read, as 64-bit values, the same way the index build
  converts them. Plugin and component. `scripts/test-online-dml.py --key-type bigint` uses
  keys above 2^32; it and `test-distance-udf.py` now run in the blocking integration CI.
- **`myvector_distance()`: a NULL vector no longer turns every later row's distance into
  NULL** (issue #170). A NULL input (vector or metric) set the UDF's error flag, which
  makes MySQL return NULL for that row and every later row of the statement, so
  `ORDER BY myvector_distance(...) LIMIT k` could return wrong results. A NULL input now
  gives NULL for that row only; `myvector_display(NULL)` likewise. An unknown metric now
  fails the statement with a message instead of returning NULL: a constant metric is
  checked once when the statement starts (and its function cached), a metric from a
  column when the row is read (`ER_UDF_ERROR`). Plugin and component.
- **`myvector_distance()`: vectors of different dimensions are an error** (issue #171).
  The plugin computed the distance over the shorter length, which could return 0 (a
  "perfect match"); the component returned NULL. Both now fail the statement with
  "vectors have different dimensions (N and M)". New test:
  `scripts/test-distance-udf.py` (plugin and component), also run by the pre-release gate.
- **A column comment that starts with whitespace or a line break** is accepted by the
  `MYVECTOR_INDEX_*` procedures and `MYVECTOR_ANN_FILTERED`, as it already was by the option
  parser (#159). They used to reject it with "not a MYVECTOR column". The prefix check
  ignores leading whitespace and case, like the parser.
- **Building an `online=Y` index while the binlog listener has a backlog no longer crashes
  `mysqld`** (issue #187). `VectorIndexCollection::open()` puts a new index in the collection
  before `initIndex()` creates it, and its last-applied binlog position started empty (the
  HNSW position was uninitialized). A listener worker applying queued rows for that table
  took every row as new and inserted into the index before its HNSW graph existed: SIGSEGV
  at address 0. Indexes now start at the "never built" position, so workers skip them
  until a build or load sets the real one; the position is read and written under a lock;
  and an HNSW insert before the graph exists is refused. New test: Lifecycle 3.9 in
  `scripts/pre-release-test.sh` (20,000-row load, then an immediate build).
- **Component: online updates continue after the listener's binlog connection is killed**
  (issue #179). After a binlog rotation, the listener's resume position named a file with
  4 checksum bytes appended, so when its connection dropped (a `KILL`, a network error) it
  could not resume, and the index stopped updating. Fixed by the rotation change in #195;
  the new Lifecycle test 3.8 in `scripts/pre-release-test.sh` kills the connection twice
  (once followed by a rotation) and checks that INSERTs still reach the index.
- **Component: online DELETE and UPDATE now reach `online=Y` indexes** (issue #188). The
  binlog listener applied only INSERTs: after a DELETE, searches still returned the row;
  after an UPDATE, they ranked it by its old vector. It now handles UPDATE_ROWS and
  DELETE_ROWS events: a DELETE removes the key, an UPDATE that changes the key or the vector
  replaces the old entry, and setting the vector to NULL removes it. Indexes gained a
  delete operation (HNSW marks the node deleted; KNN drops it), and HNSW checkpoints now
  write delete marks and updated vectors, so they survive a restart. Each key's changes
  are applied in binlog order (one queue per worker, chosen by key). The row parser now
  honours NULL columns. `Current Rows` in `MYVECTOR_INDEX_STATUS` excludes deleted rows.
  New test: Phase 3 test 3.7 in `scripts/pre-release-test.sh`.
- **Plugin: online DELETE and UPDATE now reach `online=Y` indexes** (issue #194). The plugin's
  binlog listener had the same INSERT-only gap as the component (#188). It now uses the same
  approach: UPDATE_ROWS and DELETE_ROWS events, a row parser that honours NULL columns and
  knows the width of common column types (events it cannot read are skipped, not
  misapplied), a CRC32 trailer stripped only when it matches, and one queue per worker
  chosen by key, so each row's changes apply in order. Its queue list was `static`, so
  instances could not be separated; it is now per queue. New test:
  `scripts/test-online-dml.py` (plugin and component).
- **Plugin: online indexes now keep updating after a restart** (found with #194). At startup the
  plugin looks up its `online=Y` columns in the `myvector_columns` view, but queried
  `test.myvector_columns`; `sql/myvectorplugin.sql` creates it as `mysql.myvector_columns`.
  The query failed silently, so after a restart or reinstall no online index was registered
  again, and every later INSERT, UPDATE and DELETE was ignored until the index was rebuilt.
  It now reads `mysql.myvector_columns`, and logs an error if it cannot.
- **Component: `UNINSTALL COMPONENT` failed with ERROR 3540 while the binlog listener ran**
  (issue #189). The listener's server session holds a reference to the component's
  `event_tracking_parse` service, and MySQL checks for references before it calls the
  component's deinit, so the component could never stop its own listener in time (see
  #155). New: `myvector_binlog_stop()` stops the listener and returns its binlog
  connection ids, and `mysql.MYVECTOR_BINLOG_STOP()` calls it and waits until those
  sessions have ended. `sql/myvector_uninstall_component.sql` runs it before
  `UNINSTALL COMPONENT`. The pre-release gate failed at this step on every version since
  #156, so Phases 2 and 3 never ran; the smoke test and lifecycle tests 3.2, 3.4 and 3.5
  now stop the listener first or accept the documented ERROR 3540 refusal.
- **Plugin: online indexes stopped updating about a second after the server went idle**
  (issue #166). The plugin's binlog listener reads with a 1-second timeout. When the server
  had no new binlog events for a second, the read timed out and the listener logged
  `Binlog fetch failed:` (with no error text) and exited, so `online=Y` indexes stopped
  receiving changes, usually within seconds of `INSTALL PLUGIN`. Building a non-online
  index was not the cause. The listener now asks the server for a heartbeat every 0.5 s,
  so an idle connection stays open. On any other read error it reconnects and resumes
  after the last event it processed instead of exiting. Rotate events are read according
  to whether the stream carries checksums, so the tracked binlog file name stays correct
  across reconnects and rotations. Component builds already reconnected and are unchanged
  by this fix. New test: `scripts/test-online-updates-idle.py` (plugin and
  component).
- **Component: online updates now work after a server restart** (issue #186). The
  component checked its binlog connection once, while MySQL loaded it. At startup MySQL
  does not accept connections yet, so the check failed and the binlog listener never
  started: after every restart, and in the Docker images from the first start, `online=Y`
  indexes stopped applying INSERTs until the component was reinstalled. Nothing was
  logged. The listener now waits until the server accepts connections. It stays off only
  when `myvector.cnf` has no user, or the login is refused, and says so in the error log.
  New test: Phase 3 test 3.6 in `scripts/pre-release-test.sh`.
- **Component: rows applied online are no longer lost across a restart** (issue #190).
  An `online=Y` index is saved to disk at build and at each binlog rotation, not when
  the listener stops, but the listener resumed from where it had stopped. Rows it had
  applied since the index was last saved were skipped, so they were missing from the
  index after the restart (whether this happened varied from run to run). The listener
  now starts from the oldest index checkpoint when that is earlier, and replays those
  rows. Test 3.6 checks it.
- **`ef_search` in a search's options now applies to that query only** (PR #167, issue #165).
  Before, `myvector_ann_set(..., 'nn=10,ef_search=N')` stored `N` on the shared index:
  every later query from any session searched with `N` instead of the index setting, and
  the write raced with concurrent searches (undefined behaviour). The effort is now passed
  per call, and the query path changes no index state.
- **`myvector_ann_set()` result buffer.** It is now sized for 10,000 keys of any length.
  The 128,000-byte buffer was too small for 10,000 keys of 12 digits or more. The plugin
  wrote past its end: `nn=10000` over 13-digit keys crashed `mysqld` (SIGSEGV) on 8.4. The
  component cut the JSON short.
- **A line break or tab after `MYVECTOR COLUMN`** in a column comment (a multi-line
  `COMMENT`) silently built a KNN index instead of the requested type. It is now read like
  a space, on plugin and component builds (PR #159, issue #158). If you built an index from
  such a comment on an earlier version, check `Type :` in `MYVECTOR_INDEX_STATUS` and
  rebuild it.

## [1.26.9] - 2026-09-22

### Added
- `docs/LIMITATIONS.md`: known limitations, linked from the README, the docs nav and the
  release notes (PR #134).
- Pre-release gate in CI (`pre-release-gate.yml`, non-blocking) and stronger gate checks
  (PRs #121, #125); benchmark baselines on the `benchmarks` branch.
- **MySQL 26.7 Innovation support** (component only) — build script, CI jobs,
  release/benchmark wiring, and opt-in `pre-release-test.sh 26.7` (PR #104).
- **Component Docker images** — `Dockerfile.component` and a
  `build-and-publish-component` job publishing `mysql8.4-component`,
  `mysql9.7-component` and `mysql26.7` (PR #104).
- **`sql/myvector_install_component.sql` / `myvector_uninstall_component.sql`** —
  full component install (supplemental UDFs, `MYVECTOR_INDEX_*` procedures,
  `myvector_columns` view) and uninstall (PR #104, #105).
- **`DOCKER_PLATFORM`** option on component build scripts for multi-arch builds.
- **Docs site** (MkDocs Material) and `docs/CONTRIBUTING.md` (PR #102).

### Changed
- `release.yml` dispatches the Docker publish (the `workflow_run` trigger never fired) (PR #122).
- Test scripts remove containers with `-v` to stop leaking Docker volumes (PR #120).
- Behaviour change: `MYVECTOR COLUMN type=hnsw,...` without the `|` marker now builds HNSW
  (it used to fall back to KNN silently).
- Component build scripts fall back to the MySQL CDN archive for pinned point
  releases.
- Publish workflow: `v*` version-tag guard on plugin and component jobs;
  `dry_run` input for the component publish.

### Fixed
- Index save/checkpoint failures are now surfaced instead of lost silently: the bulk HNSW
  write path could fail without throwing (truncated file reported as `SUCCESS`); a failed
  batch flush was never cleared, growing on every later checkpoint; the binlog listener's
  checkpoint advanced its tracked position before checking the save result; two online-build
  paths ignored it entirely. Also fixes an fd leak on a failed write and a lock-atomicity
  regression in the component's online-build path (PR #139).
- The RFC-004 stress harness, the Stanford 50d smoke and the online-updates test now run
  (a constant re-assigned inside `run_stress`, a `gzip | head` SIGPIPE under `pipefail`, a
  plugin variable unknown at `--initialize`, and a demo `create.sql` reformatted so the DDL
  rewrite no longer matched it) (PR #129).
- `build-docker-local.sh` targets arm64 on Linux `aarch64` (PR #135).
- HNSW index builds no longer crash mysqld on the component build; `type=hnsw` comments without
  the `|` marker now build HNSW instead of silently using KNN (PR #117).
- Benchmark harness configures the plugin and fails when the index build fails, so
  `recall_at_10` is meaningful (0.978 on plugin 8.4) (PR #123).
- Deinit restores exactly the UDFs a refused unload removed (PR #126).
- `UNINSTALL COMPONENT` no longer fails with ERROR 3538 (binlog service stop
  path returned non-zero on success).
- `UNINSTALL COMPONENT` refused while a UDF is in use (ERROR 3538) now leaves the
  component fully functional instead of half torn down, and a retry succeeds (PR #107).
- Pre-release Phase 3 lifecycle tests can now run to completion (3.3/3.4 script
  bugs fixed; new 3.5 refused-unload test) (PR #107).
- Published `*-component` images now expose the full UDF/procedure surface.
- `release.yml` bundles the component install SQL with the component artifact.

## [1.26.5.2] - 2026-05-30

### Added
- **`scripts/myvectorbench.py`** — full ANN benchmarking pipeline (PR #94).
  Runs index-build, insert-throughput, KNN-search, KNN-ANN, and recall
  workloads against a Dockerized MySQL instance; emits structured JSON
  results. Configurable via `myvectorbench.yml` (dataset, workload params,
  regression thresholds).
- **`scripts/myvectorbench-compare.py`** — baseline comparison tool; reads
  two result JSON files and checks all metrics against configured thresholds
  (`+25%`, `-25%`, `±0.05 recall`). Exit 0 = no breach, exit 1 = breach.
- **`scripts/myvectorbench.py`: `knn_ann` workload** (PR #96) — measures QPS
  and latency for `MYVECTOR_IS_ANN` query-rewrite path; emits
  `knn_ann_qps`, `knn_ann_p50_ms`, `knn_ann_p99_ms`, and
  `ann_rewrite_active` fields.
- **`scripts/myvectorbench.py`: `--artifact` flag** (PR #99) —
  `_resolve_artifact_dir` auto-resolves component artifacts from `dist/`
  or downloads from GitHub Releases by key (e.g. `component-8.4`). Handles
  `gh` CLI not-found and release asset naming mismatches with actionable
  error messages.
- **`scripts/bench-concurrent-stress.py`** — RFC-004 concurrent stress
  harness (PR #101). Runs KNN readers, online writers, and ANN readers
  simultaneously; validates post-stress consistency (index row-count,
  KNN top-1, InnoDB deadlock counter, clean thread exit); emits JSON with
  per-pool QPS, p50/p99, error counts, and `passed` verdict.
  Unit tests: `tests/test_bench_concurrent_stress.py`.
- **`scripts/pre-release-test.sh`: Phase 3 lifecycle regression gate**
  (PR #100) — 5 subtests: install timing, index reload persistence after
  uninstall/reinstall, binlog cleanup on component removal, concurrent reads
  during install, DROP INDEX stability under load.
- **`results/`** — v1.26.5.2 benchmark baseline (synthetic 10k rows,
  dim=128, local Docker): plugin-8.4, component-8.4, component-9.7.

### Fixed
- **HNSW type dispatch case-insensitivity** (PR #98, closes #92) —
  `VectorIndexCollection::open` and `rewriteMyVectorColumnDef` now compare
  index type upper-cased; previously `HNSW` vs `hnsw` in the COMMENT string
  could silently produce a brute-force index instead of HNSW.
- **`cast toupper` UB on signed-char platforms** (PR #98) — `toupper` argument
  now cast to `unsigned char` to avoid undefined behaviour.
- **recall_at_10 metric** (PR #98) — now computes live brute-force vs
  `MYVECTOR_IS_ANN` top-10 overlap; was a `None` stub.
- **ERROR 3540 on `UNINSTALL COMPONENT`** (PR #97, closes #93) —
  `myvector_unload_notify` service drains the binlog thread before the
  component unloads; prevents `ER_COMPONENTS_UNLOAD_CANT_DEINITIALIZE`.
- **DDL dim enforcement on MySQL 9.7** (PR #97) — `MYVECTOR_IS_ANN`
  query-rewrite service now uses a derived-table wrapper to avoid
  `has_subquery()` rejection; fixes `ERROR 1210` on 9.7.
- **`scripts/myvectorbench.py` runtime bugs** (PR #95) — fixed container
  startup race, SQL escaping issues, and column reference errors found
  during first live run.
- **`scripts/myvectorbench.py`: libstdc++ probe** (PR #99) — now uses the
  custom `--image` when provided so the probe matches the actual runtime lib.
- **`scripts/pre-release-test.sh`: binlog listener startup** (PR #100) —
  `install_component` wrote `myvector.cnf` *after* `INSTALL COMPONENT`, so
  the binlog listener started without its config. Lifecycle binlog-cleanup
  test now performs UNINSTALL + INSTALL after the initial install.
- **`scripts/myvectorbench.py`: permanent plugin re-install** — `install_plugin`
  now detects `load_option=ON` (plugin-load-add in my.cnf, as used in GHCR
  images) and skips UNINSTALL/INSTALL to avoid `ER_UDF_EXISTS (1125)`.

### Documentation
- `CLAUDE.md`: added ANN benchmark and RFC-004 stress test invocation examples;
  Phase 3 lifecycle gate description in pre-release section.
- `docs/RFC-004-RELIABILITY.md`: `bench-concurrent-stress.py` listed as
  primary recommended tooling, replacing sysbench Lua script placeholder.

## [1.26.5.1] - 2026-05-20

### Fixed
- Zero-magnitude vector inserts on cosine-metric indexes now return
  `ER_MYVECTOR_INVALID_VECTOR` to the client (UDF path) or log a warning
  and skip the index update (binlog/online-index path) instead of silently
  inserting max-distance entries.
- Added `DBUG_EXECUTE_IF("simulate_vector_crash", abort())` in
  `hnswdisk.i` checkpoint flush path for crash-recovery testing.
  No-op in release builds; requires `-DWITH_DEBUG=1`.

### Added
- New sysvar `myvector_max_vector_dim` (read-only, default 4096, max 16383).
  Allows indexes with dimensions up to MySQL's native VECTOR type limit.
  Set at server start: `--myvector-max-vector-dim=8192`.

### Documentation
- Added `docs/RFC-004-RELIABILITY.md` — corrected on-disk version of RFC-004
  (max dimension, persistence model, rebuild-on-start scope, error codes).
- Closes #77.

## [1.26.5] - 2026-05-08

### Added (1.26.5)

- **MySQL Component build path** for MySQL 8.4 LTS and 9.7 LTS (`src/component_src/`). Installs via `INSTALL COMPONENT` alongside the existing plugin path (#88).
- `myvector_log.h` unified logging abstraction — shared by plugin and component build, replacing per-TU `#ifdef` chains.
- `scripts/build-component.sh`, `build-component-8.4-docker.sh`, `build-component-9.7-docker.sh` — out-of-tree component build helpers.
- `scripts/smoke-component.sh` — real-data smoke test for component build.
- CI jobs: `build-component (8.4)`, `build-component-9-7`, `test-component (8.4)`, `test-component-9-7`.
- Release workflow now produces component artifacts for 8.4 and 9.7 alongside plugin artifacts.
- Docker image tag `mysql9.7` (replaces `mysql9.6`; MySQL 9.7 is the LTS line).

### Changed (1.26.5)

- MySQL 9.x image updated from 9.6 → 9.7 LTS across CI, release, and Docker publish workflows.
- `CMakeLists.txt` supports dual build mode: in-tree plugin (`MYSQL_ADD_PLUGIN`) and out-of-tree component (`MYSQL_SOURCE_DIR`).
- `hnswdisk.h`/`hnswdisk.i` logging updated to use `MYVEC_LOG_*` macros.

### Fixed (1.26.5)

- Thread safety: replaced `gmtime`/`asctime` with `gmtime_r`/`asctime_r` throughout (#87).
- Null-guard in `myvector_ann_set` row function; `*length=0` on null-index return (#87).
- Concurrency corrections in plugin init/deinit path (#87).
- `binary_log` namespace conflict on MySQL 8.4 component build — guarded for 9.7+ which declares it in `event_reader.h`.
- Config file reader hardened against permission errors and malformed input.
- CMakeCache.txt guard in component build scripts to prevent stale cache collisions.
- Quick Start `wget` URL fixed: `insert50d.sql` → `insert50d.sql.gz` (issue #89).
- **Binlog WRITE_ROWS handler for multi-column online index tables** (component): three bugs fixed together:
  - FORMAT_DESCRIPTION_EVENT (type=15) sent at binlog reconnect had a non-zero `next_log_pos` pointing past EOF, causing an infinite reconnect crash loop.
  - `KNNIndex` (brute-force fallback) did not implement `setLastUpdateCoordinates`/`getLastUpdateCoordinates`, so `isAfter()` always returned true and already-indexed rows were double-inserted on reconnect.
  - `BuildMyVectorIndexSQL` saved the listener's stale reconnect position instead of the actual DB binlog position (`SHOW BINARY LOG STATUS`), causing re-replay of pre-build INSERT events.

### Documentation updates (1.26.5)

- `docs/BUILD_MODES.md` — plugin vs. component build comparison.
- `docs/COMPONENT_MIGRATION_PLAN.md` — migration roadmap from plugin to component path.
- `README.md` — Docker tag table updated (`mysql9.7`), install path note for plugin vs. component, insert50d fix.

### Upgrade / migration notes (1.26.5)

- No schema or index migration required.
- **Docker tag change:** If you pull `ghcr.io/askdba/myvector:mysql9.6`, switch to `:mysql9.7`.
- The component path (`INSTALL COMPONENT`) is the forward path for MySQL 8.4+ but is not yet the default. The plugin path remains stable for 8.0/8.4/9.0.
- Windows builds remain unsupported.

## [1.26.3] - 2026-03-19

### Added (1.26.3)

- Runtime config file permission checks: refuse to load `myvector_config_file`
  if it has insecure permissions (group/world readable) or wrong ownership on
  Unix (#36).
- `docs/CONFIGURATION.md`: Configuration file reference with options,
  defaults, validation rules, security requirements, and examples (#36).
- `docs/ONLINE_INDEX_UPDATES.md`: Documentation for creating and configuring
  online (real-time) index updates via MySQL binlogs (#81).
- Docker test scripts and online index update assertion guidance.
- Benchmark workflow and baseline documentation for issue #79.
- Expanded licensing documentation and compatibility notes.
- MySQL 9.x setup example and updated macOS build/testing observations.

### Changed (1.26.3)

- Minor release includes accumulated merged changes since `v1.26.1`.
- Binlog config loading made thread-safe and deduplicated.
- Local Docker smoke workflow stabilized for release validation.
- Documentation expanded across Docker images, configuration, and testing flows.
- **Platform:** Microsoft Windows is **not a supported build target at this time**
  (Unix-like systems only: Linux and macOS).

### Removed (1.26.3)

- Win32-specific plugin code paths (Windows config handling, export macros, and
  related shims). Builds and releases target Linux/macOS only until Windows
  support is explicitly restored.

### Fixed (1.26.3)

- Fail-fast behavior for binlog config rejection paths.
- Lint violations across scripts, docs, and workflow files.
- `myvector_construct` caching optimization when first argument is constant.
- Forward declaration issue for `myvector_construct_bv`.

### Documentation updates

- Updated `README.md` (supported platforms; Windows not supported at this time),
  `docs/CONFIGURATION.md`, `docs/DOCKER_IMAGES.md`,
  `docs/ONLINE_INDEX_UPDATES.md`, and `docs/BUILDING_MACOS.md`.
- Added/updated licensing docs under `licenses/`.
- Added benchmark baseline documentation for issue #79.

### Upgrade / migration notes

- No schema migration required.
- Component PRs are excluded from this release scope by RC policy.
- If you previously built the plugin on Windows, that workflow is unsupported
  at this time; use Linux (including Docker images) or macOS.

## [1.0.2] - 2026-01-29

### Added (1.0.2)

- `NOTICE` file documenting third-party attributions (HNSWlib, Boost).
- `licenses/` directory with full license texts (Apache-2.0, Boost-1.0) and
  compatibility documentation.

### Changed (1.0.2)

- Preparation for 1.0.2 (was RC3)
- Update `README.md` with licensing information.

### Fixed (1.0.2)

- Re-added `mysql_close(binlog_conn)` in `myvector_index_build` just before
  `myvector::log_index_build_request` to ensure a fresh connection for binary
  logging.
- Fixed improper use of `get_row_count()` by adding `row_count` member to
  `IndexBuilder` to track progress correctly.
- Enhanced `IndexValidationTest` to execute `myvector_index_check` on the
  built table, ensuring index integrity after construction.
- Resolved build failures on newer MySQL versions (8.4+) by conditionally
  including `<mysqld_error.h>` or `<errmsg.h>` based on `MYSQL_VERSION_ID`.
- Fixed "Unknown system variable 'myvector_nprobes'" error in search functions
  by using the correct system variable name `myvector_vector_nprobes` and
  ensuring it is properly registered and accessible.
- Fixed `myvector_search` returning 0 results by ensuring the index is properly
  loaded and queried using the HNSW algorithm.
- Addressed compilation errors related to `String::c_ptr_safe()` by updating
  code to follow modern MySQL string handling practices.

### Changed (1.0.2 docs)

- Improved `README.md` documentation for installation and configuration.

## [1.0.1] - 2026-01-26

(No changes recorded for this version yet)

## [1.0.0] - 2026-01-18

### Changed (1.0.0)

- Removed `using namespace std` from all headers to prevent namespace pollution.

### Added (1.0.0)

- Automated test suite using the MySQL Test Run (MTR) framework.
  - Added `mysql-test/suite/myvector/t/vector_construct.test`: Tests for
    `myvector_construct()` UDF.
  - Added `mysql-test/suite/myvector/t/distance_functions.test`: Tests for
    distance calculation UDFs (`myvector_distance_l2`,
    `myvector_distance_cosine`).
  - Added `mysql-test/suite/myvector/t/index_build.test`: Tests for
    `myvector_index_build` stored procedure.
  - Added `mysql-test/suite/myvector/t/search.test`: Tests for vector search
    functionality.
  - Configured `mysql-test/suite/myvector/suite.opt` for plugin loading.

### SQL Functions

- `myvector_construct()` - Constructs a vector string from a standard
  comma-separated list
- `myvector_distance_l2()` - L2 (Euclidean) distance
- `myvector_distance_cosine()` - Cosine distance
- `myvector_add_document()` - Adds a document to the index (internal helper)
- `myvector_search()` - Search for nearest neighbors

### Stored Procedures

- `mysql.myvector_index_build(table_name, vector_column, metric_type)` -
  Builds an HNSW index on a table
- `mysql.myvector_index_check(table_name, vector_column)` - Checks index integrity

### System Variables

- `myvector_vector_limit` (default: 100)
- `myvector_vector_nprobes` (default: 5)

### Documentation

- README.md with build and usage instructions

## [0.0.1] - 2025-06-21

### Added (0.0.1)

- FOSDEM'25 MySQL Devroom presentation ("Vector Search in MySQL").
- Initial proof-of-concept implementation.

| Version | Date       | Comment                  |
| :------ | :--------- | :----------------------- |
| 0.0.1   | 2025-06-21 | Initial proof of concept |

[1.0.2]: https://github.com/askdba/myvector/compare/v1.0.1...v1.0.2
[1.26.3]: https://github.com/askdba/myvector/compare/v1.26.1...v1.26.3
[1.0.1]: https://github.com/askdba/myvector/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/askdba/myvector/compare/v0.0.1...v1.0.0
[0.0.1]: https://github.com/askdba/myvector/releases/tag/v0.0.1
