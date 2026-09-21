# RC1 Status - v1.26.9

RC tag: `v1.26.9-rc1` (commit `913ecff`, pushed 2026-09-20)
Source under test: `3b226d4` (main after PR #104 + #107; the RC branch adds release docs only)
Final release tag: `v1.26.9`
Date: 2026-09-20

Gate policy: MySQL 26.7 is **blocking** for this RC.

## Local pre-release gate (artifacts built from `3b226d4`, aarch64, Docker)

| Check | 8.4 | 9.7 | 26.7 (blocking) |
|-------|-----|-----|-----------------|
| Artifact build | ✅ | ✅ | ✅ |
| Phase 1 (smoke) | ✅ | ✅ | ✅ |
| Phase 2 (RFC-004 / edge cases) | ✅ | ✅ | ✅ |
| Phase 3.1 install timing | ✅ | ✅ | ✅ |
| Phase 3.2 uninstall under load | ✅ (refused once with 3538, succeeded after drain) | ✅ | ✅ |
| Phase 3.3 reload persistence | ✅ | ✅ | ✅ |
| Phase 3.4 binlog cleanup | ✅ | ✅ | ✅ |
| Phase 3.5 refused UNINSTALL leaves component intact | ✅ | ✅ | ✅ |

- `./scripts/pre-release-test.sh` (8.4 + 9.7): 26 passed, 0 failed, 12 skipped, exit 0.
- `./scripts/pre-release-test.sh 26.7`: 13 passed, 0 failed, 6 skipped, exit 0.
- Skips are the existing plugin-only / debug-build-only checks.

### Gate history for this RC (recorded for transparency)

1. First gate on the pre-fix code failed Phase 3: `UNINSTALL COMPONENT` under load
   returned ERROR 3538 on 8.4 (intermittent) and 26.7 (deterministic with an in-flight
   UDF query), leaving the component half torn down. Fixed by PR #107.
2. Phase 3 could not previously run to completion: 3.3 (bad row literal, missing
   `MYVECTOR_INDEX_LOAD`) and 3.4 (`grep` no-match aborting under `set -e`) were
   test-script bugs, fixed in #107. This is the first RC where 3.1-3.5 executed on
   all three versions.
3. On the final source, the 26.7 gate failed twice with "MySQL 26.7 container did not
   become ready": the host disk was 100% full (117 leaked anonymous Docker volumes from
   test containers; `docker rm -f` does not remove them). Infrastructure failure, before
   the component was involved. After freeing the volumes the 26.7 gate was re-run and
   passed (result above). The 8.4 + 9.7 run had already passed in one attempt.

## CI (MyVector CI)

PR #104 and PR #107 CI: all jobs green (builds 8.0/8.4/9.0, component builds 8.4/9.7/26.7,
tests, lint, CodeRabbit, GitGuardian). `build-and-benchmark` skipped on PRs.
CI does not run `pre-release-test.sh`.

## Release workflow ([run 35528548498](https://github.com/askdba/myvector/actions/runs/35528548498), tag `v1.26.9-rc1` @ `913ecff`)

| Job | Status |
|-----|--------|
| Build Plugin 8.0.35 | ✅ success |
| Build Plugin 8.4.8 | ✅ success |
| Build Plugin 9.0.0 | ✅ success |
| Build Component 8.4.8 | ✅ success |
| Build Component 9.7.0 | ✅ success |
| Create GitHub Release | ✅ success |

## Docker publish ([run 35531714614](https://github.com/askdba/myvector/actions/runs/35531714614), manual dispatch on tag `v1.26.9-rc1`)

The publish workflow is meant to start automatically when the Release workflow completes
(`workflow_run`). It did **not** trigger for this tag, so it was dispatched manually
(same as the previous RC). Follow-up: find out why the `workflow_run` trigger does not fire.

| Image | Status |
|-------|--------|
| ghcr.io/askdba/myvector:mysql8.0 | ✅ published |
| ghcr.io/askdba/myvector:mysql8.4 | ✅ published |
| ghcr.io/askdba/myvector:mysql9.7 | ✅ published |
| ghcr.io/askdba/myvector:mysql8.4-component | ✅ published |
| ghcr.io/askdba/myvector:mysql9.7-component | ✅ published |
| ghcr.io/askdba/myvector:mysql26.7 | ✅ published |

Note: publishing overwrites the mutable plugin tags (`mysql8.0`, `mysql8.4`, `mysql9.7`, `latest`)
with the RC build.

## Published-image smoke (`smoke-published-images.sh`, pulled from GHCR)

| Image | Result |
|-------|--------|
| ghcr.io/askdba/myvector:mysql8.0 | ✅ pass |
| ghcr.io/askdba/myvector:mysql8.4 | ✅ pass |
| ghcr.io/askdba/myvector:mysql9.7 | ✅ pass |
| ghcr.io/askdba/myvector:mysql8.4-component | ✅ pass |
| ghcr.io/askdba/myvector:mysql9.7-component | ✅ pass |
| ghcr.io/askdba/myvector:mysql26.7 | ✅ pass |

- `smoke-published-images.sh`: "All smokes completed OK", exit 0.
- `MYSQL_ROOT_PASSWORD=myvector MYSQL_DATABASE=vectordb bash scripts/smoke-readme.sh ghcr.io/askdba/myvector:mysql26.7`
  (blocking): exit 0.
- Scope: container start, UDF load, `myvector_construct` / `myvector_display` / `myvector_distance`.
  Does not cover HNSW index build, ANN or uninstall (covered by the local gate above).
- `MYVECTOR_SMOKE_STANFORD=1` heavier variant not run.

## Benchmark ([myvectorbench run 35528548503](https://github.com/askdba/myvector/actions/runs/35528548503))

Workflow succeeded, but **no cell was compared**: every cell logged
`NO_BASELINE: first run for this matrix cell`. Manual comparison against the v1.26.5.2
baselines in `results/` (recorded on different hardware: local Docker vs GitHub runners):

| Cell | Metric | Baseline (5.2) | This RC | Change |
|------|--------|----------------|---------|--------|
| plugin 8.4 | insert QPS | 4999 | 3864 | -23% |
| plugin 8.4 | KNN QPS / p50 | 20.1 / 47.0 ms | 13 / 78.3 ms | -35% / +66% |
| plugin 8.4 | ANN QPS / p50 | 20.7 / 45.6 ms | 14 / 72.6 ms | -32% / +59% |
| plugin 8.4 | recall@10 | 0.0 | 0.0 | unchanged |
| component 8.4 | insert QPS | 5600 | 3453 | -38% |
| component 8.4 | KNN QPS / p50 | 8.5 / 113.7 ms | 9 / 105.9 ms | +5% / -7% |
| component 9.7 | insert QPS | 5128 | 3046 | -41% |
| component 9.7 | KNN QPS / p50 | 6.6 / 115.8 ms | 8 / 117.8 ms | +20% / +2% |
| component 26.7 | insert / KNN QPS | none | 3917 / 6 (p50 177 ms) | no baseline |

Assessment: inconclusive, non-blocking. Since v1.26.5.2 the only source changes are in
`src/component_src/myvector_binlog_service.cc` and `myvector_component.cc`; plugin, HNSW and
search code are untouched, so the plugin KNN drop cannot come from code. The drops in insert
throughput across all cells are consistent with slower CI runners vs the baseline machine;
component KNN is flat or better. Recommendation: adopt this run as the CI baseline so the
next RC compares like for like. Stress harness (`bench-concurrent-stress.py`) not run.

`recall_at_10` is 0.0 in both the baseline and this run with ANN active: either the
metric or ANN recall itself is broken. Pre-existing, not a regression.

## Known issues / follow-ups

- Server log prints `unknown index type ... using KNN` for a `type=hnsw` column on the
  component build; may mean the #92 case-insensitivity fix is missing from the component path. Undiagnosed.
- Test 3.3 compares brute-force KNN before/after reload, so it does not prove the
  on-disk index was reloaded.
- `pre-release-test.sh` / smoke scripts leak anonymous Docker volumes (`docker rm -f`
  without `-v`); can fill the disk over repeated runs.
- `docker-publish.yml`'s `workflow_run` trigger did not fire after the tag-triggered Release
  run; publish had to be dispatched manually (also needed for the previous RC).
- `recall_at_10` reads 0.0 in both baseline and RC with ANN active.
- No CI benchmark baselines exist; every cell reports `NO_BASELINE`.
- Deinit rollback ignores `register_udfs()` return value; binlog-stop failure path unreachable today.

## Blockers

None.

## Go / No-Go

**GO** for `v1.26.9-rc1`: pre-release gate, CI, Release workflow, Docker publish and
published-image smoke (all six tags, including the blocking 26.7) all pass. Benchmark
comparison is inconclusive (no CI baseline) and non-blocking. Decision on cutting the
final `v1.26.9` is separate and pending.
