# Release Notes - v1.26.9

Release date: 2026-09-22
Previous release: v1.26.5.2

## Summary

v1.26.9 adds support for the MySQL 26.7 Innovation release (component-only)
and turns Docker publishing into a proper component pipeline, so the
`INSTALL COMPONENT` build is published alongside the legacy plugin images.
It also completes the component install/uninstall SQL and fixes
`UNINSTALL COMPONENT` failing with ERROR 3538.

This release went through three release candidates (`v1.26.9-rc1`, `-rc2`, `-rc3`); see
`release/RC1_STATUS_v1.26.9.md` through `RC3_STATUS_v1.26.9.md` for that history. rc1's tests
silently exercised KNN instead of HNSW on the component build (fixed in rc2, see Fixed below);
do not use rc1 for HNSW on components.

## Added

- **Pre-release gate in CI** (PR #125, closes #116): `pre-release-gate.yml` builds the
  component for 8.4 / 9.7 / 26.7 and runs `pre-release-test.sh` on PRs and `main`
  (path-filtered, non-blocking for now). New gate checks: index type is HNSW for both comment
  formats, a failed save keeps the server up and reports an error, 3.3 asserts the index is
  reloaded from disk (PR #121, closes #112), 3.5 asserts all six UDFs survive a refused unload.
- **Benchmark baselines** (closes #114): promoted on the `benchmarks` branch from a run with
  the fixed harness (plugin 8.4, component 8.4 / 9.7 / 26.7).
- `tests/test_myvector_options.cc` (parser and index-path unit test) and
  `tests/test_myvectorbench_build.py`.
- **MySQL 26.7 Innovation support** (component only): new
  `scripts/build-component-26.7-docker.sh`, CI jobs `build-component-26-7` /
  `test-component-26-7`, and wiring in `release.yml`, `myvectorbench.yml`,
  `pre-release-test.sh` (opt-in: `./scripts/pre-release-test.sh 26.7`) and
  `smoke-published-images.sh`.
- **Component Docker images**: new `Dockerfile.component` and a
  `build-and-publish-component` job in `docker-publish.yml` publishing
  `mysql8.4-component`, `mysql9.7-component` and `mysql26.7`. Existing plugin
  tags (`mysql8.0`, `mysql8.4`, `mysql9.7`, `latest`) are unchanged.
- **`sql/myvector_install_component.sql` / `sql/myvector_uninstall_component.sql`**:
  complete component install (supplemental UDFs, `MYVECTOR_INDEX_*`
  procedures, `myvector_columns` view) and uninstall scripts. Release
  artifacts now bundle the component SQL instead of the plugin SQL.
- **`DOCKER_PLATFORM`** option on the 8.4 / 9.7 / 26.7 component build scripts
  for multi-arch builds.
- **Docs site** (MkDocs Material, myvector.online) and `docs/CONTRIBUTING.md`.

## Changed

- **Docker publish is started by the release workflow** (PR #122, closes #109): the
  `workflow_run` trigger never fired, so `release.yml` now dispatches `docker-publish.yml` on
  the tag after the release is created. Manual dispatch remains as a fallback.
- **Test scripts remove containers with `-v`** (PR #120, closes #110): they no longer leak one
  Docker volume per test container.
- Component build scripts fall back to the MySQL CDN archive for pinned point
  releases that are no longer on the primary download path.
- Docker publish jobs apply a `v*` version-tag guard to both the plugin and
  component publish paths; `dry_run` input added to exercise the component
  publish without pushing.

## Fixed

- **Index save/checkpoint failures are now surfaced instead of lost silently** (PR #139): the
  bulk HNSW write path could fail (disk full) without throwing, so a build could report
  `SUCCESS` with a truncated index file on disk; a failed batch flush was never cleared,
  growing on every later checkpoint retry; the binlog listener's periodic checkpoint advanced
  its tracked position before checking whether the save actually succeeded; two online-build
  paths formatted a `SUCCESS` message before calling save, ignoring its result. All five now
  check and report the real outcome. Also fixes an fd leak on a failed write and restores a
  lock's atomicity in the component's online-build path (a race introduced while fixing an
  unrelated compile error in the same area).
- **HNSW index builds no longer crash mysqld on the component build** (PR #117, closes #111
  and #118). `MYVECTOR COLUMN type=hnsw,...` (no `|` marker) parsed with an empty type and
  silently fell back to KNN. Making it parse exposed two crashes in the real HNSW path:
  a SIGSEGV (the type was kept lower case while the distance-space lookup matches `HNSW`
  exactly) and an abort (the component's index directory was empty, so files were written to
  `/<name>`, and hnswlib's exception escaped into mysqld). The type is now upper-cased, an
  empty index directory is relative to the datadir, and a failed save is reported as an
  `ERROR` (build and explicit `save`) instead of aborting the server.
- **Benchmark harness never built a plugin index** (PR #123, closes #113): the plugin path did
  not write `myvector.cnf`, so `MYVECTOR_INDEX_BUILD` could not connect back to the server,
  the failure was ignored, and `recall_at_10` read 0.0. The harness is configured and now
  fails when the build does not report `SUCCESS` (plugin recall@10 is now 0.978).
- **Deinit rollback is exact** (PR #126, closes #115): only the UDFs a refused unload removed
  are restored, and a failed binlog stop restores them too.
- **`UNINSTALL COMPONENT` ERROR 3538**: the binlog service's stop path
  returned non-zero on success, which `myvector_component_deinit()`
  propagated as a failure whenever the binlog thread had been running.
  It now returns 0.
- **`UNINSTALL COMPONENT` under load left the component half torn down** (PR #107):
  deinit stopped the binlog thread and then unregistered the UDFs; MySQL refuses
  to unregister a UDF a running statement is using, so it failed with ERROR 3538
  after removing the other UDFs and stopping the binlog listener, and every later
  `UNINSTALL` failed too. Deinit now unregisters the UDFs first and, if refused,
  restores them and returns before touching the binlog thread, so a refused
  unload leaves the component fully functional and a retry succeeds.
- **Pre-release Phase 3 could never complete** (PR #107): three test-script bugs
  (3.3 malformed row literal and missing `MYVECTOR_INDEX_LOAD`; 3.4 `grep`
  no-match aborting the suite under `set -e`) meant subtests after 3.4 and Phase 3
  on later MySQL versions never ran. New subtest 3.5 covers refused unload.
- **Component images missing SQL surface**: published `*-component` images
  previously exposed only auto-registered core UDFs; HNSW index build and ANN
  workflows failed. Fixed by the expanded install SQL above.
- **Uninstallable release artifact**: `release.yml` packaged the plugin SQL
  with a component `.so`.
- Two publish-workflow failures exposed by the dry run.

## Documentation updates

- `README.md`, `docs/DOCKER_IMAGES.md`, `docs/quickstart.md`,
  `docs/COMPONENT_MIGRATION_PLAN.md`: corrected tag tables, plugin-vs-component
  image contents, and a pre-existing 9.0 / 9.7 contradiction.
- New docs site pages: index, quickstart, usage.

## Upgrade / migration notes

- **Behaviour change:** a column comment written as `MYVECTOR COLUMN type=hnsw,...` (no `|`
  marker) used to fall back to a KNN index silently. It now builds an HNSW index, as the
  `type` says. Indexes on such columns will be HNSW after they are rebuilt.
- No other migration action required for existing plugin or component users.
- MySQL 26.7 is component-only; there is no plugin build for 26.7.
- Component installs require `myvector.cnf` to be present before
  `INSTALL COMPONENT` (the binlog listener reads it at startup); see the
  header of `sql/myvector_install_component.sql`.

## Known issues

The full list of limitations is in [`docs/LIMITATIONS.md`](../docs/LIMITATIONS.md). The ones
most likely to matter for this release:

- MySQL 26.7 is a new Innovation release with a short track record. Its
  `pre-release-test.sh` run is opt-in by default but is a **blocking** gate
  for this release.
- `dist=cosine` (lower case) silently becomes L2 for HNSW indexes; use `dist=Cosine` (#119).
- Benchmark QPS/latency are dominated by `docker exec` overhead (~67 ms per query), so they
  are weak regression signals; recall and build time are meaningful (#124).
- `MYVECTOR_IS_ANN(...)` is plugin-only; the component builds have no ANN query path.
- There is no filtered search; extra `WHERE` predicates apply after the k ANN candidates.
- The `MYVECTOR(...)` DDL form is matched literally (upper case, no space); a
  `COMMENT 'MYVECTOR(...)'` string has been seen to fail on the plugin image (#130).
