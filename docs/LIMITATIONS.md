# Known limitations

What MyVector does not do today, or does differently than you might expect. Each item links
to the issue tracking it. Items marked *(untested)* are not verified by this project's tests.

## Search

- **`MYVECTOR_IS_ANN(...)` is available on the plugin builds only.** The annotation is
  implemented as a query rewrite, which the component builds (`INSTALL COMPONENT`) do not
  have: every component cell of the benchmark reports `ann_rewrite_active = False`, so there
  is no ANN query path and no recall number for components. The component registers the
  `myvector_ann_set()` function, but this release does not test ANN through SQL on components
  *(untested)*.
- **No filtered search.** There is no pre-filtering. Because the rewrite turns
  `MYVECTOR_IS_ANN(...)` into `id IN (<k nearest ids>)`, any other predicate in the same
  `WHERE` clause is applied to those k candidates afterwards, so a filtered query can return
  fewer than k rows.
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
- **The `MYVECTOR(...)` column type is matched literally.** The plugin's DDL rewrite looks for
  the upper-case text `MYVECTOR(` with no space. `myvector (type=...)` is a syntax error.
  A `COMMENT 'MYVECTOR(...)'` string has also been seen to fail on the plugin image (#130,
  observed once, not yet investigated). On MySQL 9.x use the native `VECTOR(n)` type with a
  `COMMENT`, as in [Docker images](DOCKER_IMAGES.md).

## Data model

- Row keys are `INT` / `BIGINT` only (`KeyTypeInteger`).
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
