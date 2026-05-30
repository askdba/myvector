# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
