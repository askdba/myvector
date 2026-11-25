# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **The MCP components moved to [askdba/mysql-mcp-components](https://github.com/askdba/mysql-mcp-components)**, with their history: `component_mcp_guard`, `component_mcp`, the `mysql_mcp` SQL tool pack (`sql/mcp/`), the `ghcr.io/askdba/mysql-mcp` images and their tests and workflows. `MYVECTOR_BUILD_MCP` and `MYVECTOR_BUILD_MCP_ENDPOINT` are gone from the build scripts; `docs/MCP_COMPONENTS.md` points to the new repository. Their unreleased changes are listed in its changelog.

### Fixed
- **`scripts/test-online-updates-idle.py` no longer fails on a single idle checkpoint** ([#255](https://github.com/askdba/myvector/issues/255)). Its "at most one checkpoint in a 10 s idle window" check counted log lines, and one checkpoint writes five, so one listener reconnection in the window (which checkpoints the rows just applied) failed the run. It now counts checkpoints.
- **More coverage for online indexes stored in InnoDB** (RFC-005 Phase 1b follow-up). `scripts/test-innodb-online.py` gains a part C: statements that change two tables, `max_allowed_packet` lowered below the index's chunk size and raised again, index rows that name another server as their writer, a purged saved start file, and a compressed transaction followed by a rebuild.
- **A search while an HNSW index is being rebuilt no longer crashes `mysqld`** ([#322](https://github.com/askdba/myvector/issues/322)). A search read the graph's element count, entry point and top level without the lock an insert changes them under. While the first row of a rebuilt (empty) graph was being inserted, a search could see one element and no entry point, and read far outside the graph's memory (SIGSEGV); when a new node raised the top level, it could pair the old entry point with the new level. Searches and updates now read the entry point and the top level as one pair under that lock, and a search on a graph with nothing linked yet returns no rows. Eight sessions searching through repeated rebuilds killed the 8.4 plugin within 14 rounds in 6 of 6 runs before (on `main` and on the commit before RFC-005 Phase 1b) and survived 40 rounds in 3 of 3 runs after; search throughput is unchanged (1, 8 and 16 sessions). `scripts/test-unloaded-index.py` gains step J for it.
- **A file-backed online index is no longer corrupted by the first checkpoint after a restart** ([#325](https://github.com/askdba/myvector/issues/325)). The checkpoint writes a node's upper-level links in place if the node is already in the `.links` files and at the end otherwise, and decided that by the node's offset being non-zero. After a load, the first node of the data file is at offset 0: when its links changed, a second copy was appended with no directory entry, and every upper-level node added afterwards was read from the wrong place at the next load. Inserts then failed with `cand error` and the index stayed unusable until rebuilt (plugin and component; reproduced every time with 5,000 rows plus 3,000 per restart cycle). Now:
  - the checkpoint and crash recovery go by whether the node is in the offsets map;
  - a build's full save records the offsets, so the first checkpoint after a build no longer stores changed nodes a second time;
  - the file loader checks every neighbor list, as the InnoDB loader does. **A file damaged by this bug is refused** at load (`Index seems to be corrupted ...; Rebuild it with MYVECTOR_INDEX_BUILD`) instead of being used; a rebuild repairs it.
  - the loader also refuses a links data file that is longer than its directory accounts for, which is what the bug always left behind, even where the shifted lists happen to look valid.
- **A file-backed online index keeps following the binlog after a crash during a checkpoint** ([#327](https://github.com/askdba/myvector/issues/327)). The three recovery paths of the file loader (first pass rolled back, second pass replayed, completed but not marked) ended with the checkpoint id reset, so the index was loaded with no binlog position and every later row change was skipped as older, silently, until the next restart. They now keep the id of the checkpoint the index stands at.
  - New `scripts/test-online-restart-cycles.py` (three restart cycles, a links data file with unaccounted bytes, an interrupted checkpoint; plugin 8.4 and components 8.4, 9.7, 26.10) runs in the `integration-scripts` CI job.
### Added
- **Chunk sizes of indexes stored in InnoDB can be set** (RFC-005 Phase 3, D5). `myvector_index_vec_chunk_bytes` (default 4194304) and `myvector_index_lnk_chunk_bytes` (default 262144) in `myvector.cnf`, read when the component starts, set the target size of the vector and level-0 link chunks of indexes built afterwards (4096 to 268435456; still limited by `max_allowed_packet`). A stored index keeps the sizes it was built with, and a load now sizes its reads by them. Every save and every checkpoint that writes rows is logged with its duration and what it wrote (vector chunks, link chunks, upper rows, bytes). `scripts/test-innodb-storage.py` checks the settings, the log line and the fallback for out-of-range values.
- **Online indexes can be stored in InnoDB** (RFC-005 Phase 1b, [docs/RFC-005-INNODB-INDEX-STORAGE.md](docs/RFC-005-INNODB-INDEX-STORAGE.md)). `online=Y` now works with `storage=innodb` (component build).
  - **Following the binlog by GTID.** Such an index records the set of transactions its graph includes: the server's executed set at the build, read under the build's table lock. The binlog listener reads each transaction's GTID (tagged GTIDs of MySQL 8.4+ included) and the index skips row changes of transactions in its set and applies the rest. File-backed online indexes keep using binlog file and position.
  - **Checkpoints on a timer.** Every `myvector_checkpoint_interval` seconds (a new `myvector.cnf` setting, default 300) the listener, between two transactions and with its queue empty, writes new versions of the chunks and upper-level rows that hold changed nodes, then publishes them and the new GTID set with one compare-and-set on the index's header row, and deletes the versions it replaced. An index nothing changed in is not rewritten; when only other tables changed, only its stored set moves. A crash before the publish leaves rows no load reads, which the next checkpoint deletes. Binlog rotation still checkpoints file-backed indexes only.
  - **Restart.** The index is loaded from its last checkpoint, and the listener starts reading at the newest binlog file whose `Previous_gtids` set is contained in the index's set, so every later transaction is applied once. If no such file is left (binary logs purged, or index rows restored without them), the index is not loaded and reports that it must be rebuilt; other indexes keep working. An explicit `MYVECTOR_INDEX_LOAD` of an index whose checkpoint is behind the running listener is refused the same way.
  - **Other changes.** `binlog_state.json` now also records the GTID set at the saved position. When the listener starts from a position whose set it doesn't know, it starts from the beginning of that binlog file instead; indexes skip what they already include. A `DROP TABLE` in the binlog drops an InnoDB-stored index unless the index's set includes the drop. A load makes the loading server the stored index's writer (`writer_uuid`), so rows that came with a restore or a copied datadir can be checkpointed. The listener keeps table maps by table id and across reconnections, so the rows of a statement that changes several tables, and row events it resumes at, are read with their own map. `online=Y` together with `track=` is refused for `storage=innodb`: a `track=` build reads only the rows its tracking column selects.
  - **Limits.** The listener can't read compressed transactions (`binlog_transaction_compression`), as before; it now says so once in the error log and never records such a transaction as applied, so an InnoDB-stored index does not resume past one. While it writes a checkpoint the listener applies no updates. There is no checkpoint at shutdown: a restart replays from the last timer checkpoint.
  - **Tests.** `tests/test_myvector_gtid.cc` covers GTID sets and the event decoders with event bytes from 8.4, 9.7 and 26.10 binlogs (`unit-tests` CI job). `scripts/test-innodb-online.py` (components 8.4, 9.7, 26.10; `integration-scripts` CI job) compares the index with its table after online changes, a 10,000-row transaction, a build under concurrent inserts, `kill -9` with and without a checkpoint, in the middle of one and under a write load, and after a binlog purge.
- **HNSW indexes can be stored in InnoDB** (RFC-005 Phase 1a, [docs/RFC-005-INNODB-INDEX-STORAGE.md](docs/RFC-005-INNODB-INDEX-STORAGE.md)). With `storage=innodb` in a column's options, the component keeps the index in tables in a new `myvector` schema (`index_meta`, `index_vectors`, `index_links`, `index_upper`) instead of files in `myvector_index_dir`, so it is crash-safe through InnoDB and included in backups. A build writes a new generation in bounded transactions and publishes it with a compare-and-set; a load reads it in one consistent snapshot and checks CRCs, the header and every neighbor list before the graph is used; a drop leaves a tombstone so generation numbers never reset. Writes stay out of the binlog (`sql_log_bin = 0`): each server keeps its own rows. The component runs this SQL on its own thread, as the locked account `'mysql.myvector'@'localhost'` the install script creates. Refused with a clear error: the plugin build, `gtid_mode` off, `read_only` / `super_read_only` on, KNN indexes, and a `max_allowed_packet` too small for one node. `scripts/test-innodb-storage.py` (component 8.4, 9.7, 26.10; plugin refusal) runs in the `integration-scripts` CI job.
  - The file loader's header checks moved into `validateHeader()` and helpers shared with the InnoDB loader; file-loading behavior is unchanged (`scripts/test-hnsw-loader-hardening.py`).
- **MySQL 26.10 component build script** (`scripts/build-component-26.10-docker.sh`). It configures the server tree with `-DWITH_AUTHENTICATION_WEBAUTHN=OFF`: 26.10 enables WebAuthn by default, which needs `libudev`/`libfido2` headers the build container does not have, and the component build only needs generated headers.
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
- **A loaded HNSW index searched with ef = 10 instead of its own `ef` / `ef_search`** ([#291](https://github.com/askdba/myvector/issues/291)). The option was applied only when an index was built; the file loader left hnswlib's default search effort, so after `MYVECTOR_INDEX_LOAD`, and after every restart, searches used ef 10 and recall dropped until the index was rebuilt (60 of 60 test queries returned different rows after a load). The loaded graph now gets the index's ef, as a built one does. Searches on reloaded indexes with a larger ef do more work per query and return better results, as they did before the reload. `scripts/test-ef-search.py` reloads the index and checks that default queries return the same rows.
- **Reloading or rebuilding an index while it was searched could use freed memory; an explicit load with no saved files reported SUCCESS** (RFC-005 Phase 0, follow-up to [#265](https://github.com/askdba/myvector/issues/265)).
  - `load` and `build` replaced the HNSW graph under the index's shared lock, so a search running at the same time could read the graph as it was deleted. The graph now has its own lock: `load`, `build` and `drop` take it exclusively while they replace or delete the graph, and searches, online updates, saves and `MYVECTOR_INDEX_STATUS` take it shared, so they wait while the graph is replaced instead (a build then fills the new graph while searches run, so they can see partial results until it finishes, as before). (Upgrading the index lock itself deadlocks against `get()`, which holds the collection mutex while it waits for that lock.)
  - `MYVECTOR_INDEX_LOAD` on an index with no saved files now returns "ERROR: no saved index for … Build it with MYVECTOR_INDEX_BUILD." and leaves the in-memory index as it is. The startup loaders still start such an online index empty.
  - `scripts/test-unloaded-index.py` adds both: an explicit load with no files, and 40 reloads while four sessions search.
- **An insert past an HNSW index's `size=` crashed mysqld** ([#285](https://github.com/askdba/myvector/issues/285)). hnswlib throws once `size=` rows are in the graph, and nothing caught it, so the next row terminated the server: from `MYVECTOR_INDEX_BUILD` on a table with more rows than `size=`, and from the binlog worker applying an `online=Y` INSERT.
  - Rows the graph can't take are now rejected and counted. `MYVECTOR_INDEX_BUILD` reports an error naming the index ("is full: N rows were not added ... Rebuild it with a larger size") instead of SUCCESS, and `MYVECTOR_INDEX_STATUS` shows `Rows rejected (index full)`.
  - Online INSERTs past the capacity are skipped and counted with the other skipped row events (#205), under the new reason "the index is full". An UPDATE of a row already in the index still applies.
  - Only a capacity refusal (raised before hnswlib changes the graph) counts as a rejected row. Any other insert failure, such as an allocation failure, may leave a half-inserted node, so the index is marked unusable: it takes no more rows, it is never saved (a later load would make it searchable again), searches fail with "not loaded", and a build that hits this reports "could not be built", until `MYVECTOR_INDEX_BUILD`.
- **`MYVECTOR_INDEX_BUILD` reused a stale index after the table or its COMMENT changed** ([#283](https://github.com/askdba/myvector/issues/283)). An index stays open in memory after its table is dropped, and a rebuild reused it with the options it was created with: a table recreated as `dist=Cosine` was searched through the old L2 graph, and new `M`, `ef`, `dim` or `size` values were ignored, with SUCCESS reported.
  - A build now compares the open index's options with the column's current COMMENT, and when they differ it drops the old index and creates a new one from the COMMENT.
  - Dropping an index (`MYVECTOR_INDEX_DROP`, a changed COMMENT, or a DROP seen in the binlog) now takes it out of the collection first, then waits until no search or binlog worker is using it before freeing the graph. Until the old index's files are deleted, opening an index of the same name waits, so a replacement can't save files that cleanup then deletes; and a DROP seen in the binlog leaves alone an index a `MYVECTOR_INDEX_BUILD` is working on. Before, `MYVECTOR_INDEX_DROP` freed the graph while other threads could still be searching it, and two concurrent drops of the same index could deadlock.
  - The binlog apply path ignores indexes that are not `online=Y`, so a registration left from before a column's COMMENT changed to non-online no longer updates the rebuilt index.
  - Component: the binlog listener now acts on `DROP TABLE` and `DROP DATABASE` / `DROP SCHEMA` (backtick or ANSI double-quoted names). It removes the online indexes of the dropped tables (built before the DROP) from memory and disk and forgets their online registrations column by column, so a table recreated under the same name no longer feeds the old graph. Indexes built after the DROP are kept, so replaying the binlog after a restart is safe.
  - `scripts/test-stale-index-overflow.py` checks both issues (plugin and component; the DROP handling is component only) and runs in the `integration-scripts` CI job.
- **A search on an HNSW index with no graph crashed mysqld; a failed load reported SUCCESS** ([#265](https://github.com/askdba/myvector/issues/265)). An index can be registered without a graph, for example by `MYVECTOR_INDEX_STATUS` on an index that was never built or loaded; a search on it dereferenced a null pointer (SIGSEGV). And `MYVECTOR_INDEX_LOAD` on corrupt index files fell back to an empty index and reported SUCCESS, so searches silently returned nothing.
  - `myvector_ann_set` now refuses an index that isn't loaded, with "Vector index is not loaded. Run MYVECTOR_INDEX_LOAD or MYVECTOR_INDEX_BUILD first.", in the plugin and the component.
  - `MYVECTOR_INDEX_LOAD` on files that exist but can't be loaded returns an error and leaves the index unloaded; the startup loaders then skip it. Missing files still start an empty index, as before.
  - `scripts/test-unloaded-index.py` covers both cases and runs in the integration-scripts workflow.
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
