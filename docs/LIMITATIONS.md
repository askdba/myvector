# Known limitations

What MyVector does not do today, or does differently than you might expect. Each item links
to the issue tracking it. Items marked *(untested)* are not verified by this project's tests.

## Search

- **`MYVECTOR_IS_ANN(...)` on component builds needs a build that includes #156.** The
  annotation is a query rewrite. Component builds (`INSTALL COMPONENT`) from v1.26.9 and
  earlier releases don't have it ([#144](https://github.com/askdba/myvector/issues/144)); on
  those, call `myvector_ann_set()` directly. Components built from `main` after
  [#156](https://github.com/askdba/myvector/pull/156) have the rewrite, verified on MySQL
  8.4.8, 8.4.11, 9.7.0 and 26.7.0 with components built by the repo's build scripts on
  aarch64. [#174](https://github.com/askdba/myvector/issues/174) reports the rewrite not
  firing on 8.4.11 in one setup, which we could not reproduce. If `MYVECTOR(...)` or
  `MYVECTOR_IS_ANN` fails on your server, use the `MYVECTOR COLUMN` comment and
  `myvector_ann_set()`, which work on every build. Plugin builds have always had the rewrite.
- **Uninstalling a component that has the query rewrite fails with ERROR 3540** while
  any other session that has run a query since the install is still connected
  ([#155](https://github.com/askdba/myvector/issues/155)). Each such session holds a
  reference to the component's `event_tracking_parse` service until it disconnects, and
  MySQL won't unload a component whose services are still referenced. This is MySQL
  behaviour, which the component can't change. The component's own binlog listener is
  one such session ([#189](https://github.com/askdba/myvector/issues/189)), and so is
  every application connection or open `mysql` client. Before uninstalling, run
  `CALL mysql.MYVECTOR_UNINSTALL_CHECK()` to list the other sessions.
  `CALL mysql.MYVECTOR_PREPARE_UNINSTALL(0)` checks for other client sessions first. If
  there are any, it raises an error naming them and leaves the listener running. If there
  are none, it stops the listener. `(1)` stops the listener and KILLs the other sessions.
  See [Uninstalling the component](usage.md#uninstalling-the-component).
- **Filtered search takes an explicit key list.** Pass the allowed keys as the fifth
  argument, e.g. `MYVECTOR_IS_ANN(..., 10, (SELECT JSON_ARRAYAGG(id) FROM t WHERE ...))`
  (see [Usage](usage.md#vector-search)). A predicate written *next to*
  `MYVECTOR_IS_ANN(...)` in the same `WHERE` clause is still applied to the k results
  afterwards, so it can return fewer than k rows. Building the key list costs one scan of
  the matching rows, which is slow when the filter matches most of a large table. For
  those filters, use `CALL mysql.MYVECTOR_ANN_FILTERED(...)` (see
  [Usage](usage.md#vector-search)).
- **`MYVECTOR_ANN_FILTERED` returns a result set.** You cannot join it or use it inside
  another query. When the predicate is so selective that 10,000 candidates hold fewer than
  k matches, it builds the key list after all: it then does the key list's work plus the
  rounds of candidates before it. It uses the session variables `@_myvector_sql`,
  `@_myvector_js` and `@_myvector_n`, and sets them to NULL when it is done. It also uses
  the prepared statement name `_myvector_stmt`, which replaces a statement of the same
  name in the session. A row written between its last round and its final query can make
  it return fewer than k rows.
- **Approximate results.** HNSW is approximate: recall depends on `ef` / `ef_search`. The
  benchmark's recall@10 (0.978 on plugin 8.4) was measured on synthetic vectors at a single
  setting and is not comparable with published results (#131).

## Building and running an index

- **The index build connects back to the server.** `MYVECTOR_INDEX_BUILD` needs
  `myvector.cnf` (host, user, password, port). Without it the procedure returns an error
  row such as `Can't connect to local MySQL server through socket ''`, and no index is built.
  Check the returned text: it is not an SQL error.
- **Index files live in `myvector_index_dir`.** The plugin has this as a system variable
  (default `/mysqldata`, which must exist and be writable). The component has no such
  variable: index files are written relative to the server's data directory.
- **A failed save is reported, not fatal.** Since v1.26.9-rc2 a build or `save` that cannot
  write the index returns `ERROR: ...` and leaves the server running. On v1.26.9-rc1 the
  same failure aborted `mysqld`.

## Index options and DDL

- **Options are matched exactly, and unknown values fall back silently** (#119). `dist=cosine`
  (lower case) becomes L2; use `Cosine`, `CosineNorm` or `Angular`. Integer keys are
  case-sensitive too, so `m=64` is ignored and the default is used; write `M=64`.
  `MYVECTOR_INDEX_STATUS` prints the option as written, not the effective setting.
- **`type` is case-insensitive** since v1.26.9-rc2 (`hnsw` and `HNSW` both build HNSW).
  A column comment written without the `|` marker (`MYVECTOR COLUMN type=hnsw,...`) now
  builds HNSW; on earlier versions it silently built a brute-force KNN index.
- **A misspelled or missing `type` is an error.** `MYVECTOR_INDEX_BUILD` returns
  `ERROR: unknown index type 'hnws' ...` or `ERROR: missing index type ...` and builds nothing.
  Earlier versions built a brute-force KNN index and returned `SUCCESS`. A column comment
  with no `type=` must now say `type=KNN` explicitly. The plugin's `MYVECTOR(...)` DDL still
  defaults a missing type to KNN, and now rejects an unknown one at `CREATE TABLE`.
- **A line break or tab after `MYVECTOR COLUMN`** (a multi-line `COMMENT`) is read like a
  space (#158). Before this fix such a comment silently built a KNN index; if you built one
  on an earlier version, check `Type :` in `MYVECTOR_INDEX_STATUS`. Whitespace or a line
  break *before* `MYVECTOR COLUMN` is accepted too. Earlier, the `MYVECTOR_INDEX_*`
  procedures rejected it with "not a MYVECTOR column". `MYVECTOR` and `COLUMN` must
  still be separated by exactly one space.
- **The `MYVECTOR(...)` column type is matched literally.** The plugin's DDL rewrite looks for
  the upper-case text `MYVECTOR(` with no space. `myvector (type=...)` is a syntax error.
  A `COMMENT 'MYVECTOR(...)'` string has also been seen to fail on the plugin image (#130,
  observed once, not yet investigated). On MySQL 9.x use the native `VECTOR(n)` type with a
  `COMMENT`, as in [Docker images](DOCKER_IMAGES.md).

## Data model

- Row keys are integers (`TINYINT` to `BIGINT`, signed or unsigned; `KeyTypeInteger`)
  and must not be negative. Online updates (`online=Y`) take the same key types;
  before #204 they applied only `INT` keys and silently skipped the others.
- Vectors are 32-bit floats (`FP32`); the default maximum dimension is 4096
  (`myvector_max_vector_dim`, a plugin variable).
- Distance metrics: L2, Cosine and inner product.

## Platforms and versions

| | Plugin (`INSTALL PLUGIN`) | Component (`INSTALL COMPONENT`) |
|---|---|---|
| MySQL 8.0 | yes | no build planned |
| MySQL 8.4 | yes | yes |
| MySQL 9.x | 9.0 and 9.7 | 9.7 |
| MySQL 26.7 (Innovation) | no (ABI) | yes; new, short track record |

- **Windows is not supported** (#80).
- **MariaDB and Percona Server are not built or tested.**
- **macOS:** no prebuilt binaries; use the Docker images, or see
  [Building on macOS](BUILDING_MACOS.md). There is no Homebrew formula.

## Benchmarks

- Query QPS and latency are dominated by `docker exec` overhead (about 67 ms per query), so
  they are weak regression signals (#124). Recall, index build time and batched insert
  throughput are meaningful.
- The benchmark uses synthetic data and one `ef_search` setting (#131), and runs against a
  containerised server, not a bare-metal one (#133).
