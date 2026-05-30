# Release Notes - v1.26.5.2

Release date: 2026-05-30
Previous release: v1.26.5.1

## Summary

v1.26.5.2 delivers the complete benchmarking and reliability-testing
infrastructure described in RFC-004, two correctness fixes to the HNSW
plugin and component build paths, and the Phase 3 lifecycle regression gate
in the pre-release test suite.

## Added

- **ANN benchmarking pipeline** (`scripts/myvectorbench.py`, `myvectorbench-compare.py`):
  end-to-end benchmark harness covering index-build, insert-throughput,
  KNN-search, KNN-ANN (query-rewrite path), and recall. Configurable workload
  params and regression thresholds via `myvectorbench.yml`. Baseline comparison
  with per-metric threshold enforcement (exit 1 on breach).
- **`knn_ann` workload** in `myvectorbench.py`: measures `MYVECTOR_IS_ANN`
  query-rewrite QPS/latency separately from plain KNN; detects whether ANN
  rewrite is active on each cell.
- **`--artifact` flag** in `myvectorbench.py`: resolves component artifacts
  from `dist/` or downloads from GitHub Releases by key (`component-8.4`,
  `component-9.7`).
- **RFC-004 concurrent stress harness** (`scripts/bench-concurrent-stress.py`):
  three concurrent pools (KNN readers, online writers, ANN readers) with
  post-stress consistency checks; JSON output with `passed` verdict.
- **Phase 3 lifecycle regression gate** in `scripts/pre-release-test.sh`:
  5 subtests covering install timing, index reload persistence, binlog cleanup,
  concurrent reads during install, and DROP stability.
- **Benchmark baseline** recorded in `results/` for plugin-8.4, component-8.4,
  and component-9.7 (synthetic 10k rows, dim=128, local Docker).

## Fixed

- **HNSW type case-insensitivity** (closes #92): `VectorIndexCollection::open`
  now compares index type case-insensitively. Previously `HNSW` vs `hnsw` in
  the column COMMENT silently fell back to brute-force search.
- **`toupper` UB on signed-char platforms**: argument now cast to `unsigned char`.
- **`recall_at_10` metric**: replaced `None` stub with live brute-force vs
  `MYVECTOR_IS_ANN` top-10 overlap computation.
- **ERROR 3540 on `UNINSTALL COMPONENT`** (closes #93): `myvector_unload_notify`
  now drains the binlog thread before the component unloads, eliminating
  `ER_COMPONENTS_UNLOAD_CANT_DEINITIALIZE`.
- **`MYVECTOR_IS_ANN` DDL on MySQL 9.7** (closes #93): derived-table wrapper
  prevents `has_subquery()` rejection; fixes `ERROR 1210` on 9.7.
- **Binlog listener startup ordering**: `install_component` wrote `myvector.cnf`
  after `INSTALL COMPONENT`, preventing the binlog listener from reading its
  config. Pre-release lifecycle test and benchmark setup now UNINSTALL + INSTALL
  to guarantee the component reads the config file at startup.
- **Plugin re-install on GHCR images**: `install_plugin` detects `load_option=ON`
  (plugin pre-loaded via my.cnf) and skips UNINSTALL/INSTALL to avoid `ERROR 1125`.

## Documentation updates

- `CLAUDE.md`: ANN benchmark and concurrent stress test sections; Phase 3 gate
  description in pre-release section.
- `docs/RFC-004-RELIABILITY.md`: `bench-concurrent-stress.py` replaces sysbench
  Lua as primary recommended tooling; updated tooling table.
- `CHANGELOG.md`: full entries for all PRs (#94–#101) since v1.26.5.1.

## Upgrade / migration notes

- No schema or index migration required.
- No MySQL version or Docker tag changes.
- Plugin and component paths remain unchanged; no configuration file changes.
- The `myvector_max_vector_dim` sysvar introduced in v1.26.5.1 is unchanged.

## Known issues

- `recall_at_10` reports `0.000` / `null` on synthetic datasets with local
  Docker — expected, because the ANN rewrite path requires the full component
  stack (including query rewrite service) to be active. On published GHCR
  images the plugin path shows `ann_rewrite_active=true` and recall is
  measurable with real datasets (glove50, dbpedia-openai).
- Component cells show `ann_rewrite_active=false` on fresh benchmark containers
  because the query rewrite component is not separately installed in the
  benchmark setup. Use the GHCR plugin image or manually install the rewrite
  component for ANN recall measurements.
