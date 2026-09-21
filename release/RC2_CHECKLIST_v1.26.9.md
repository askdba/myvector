# RC2 Checklist - v1.26.9

Target RC: `v1.26.9-rc2`
Previous RC: `v1.26.9-rc1` (see `RC1_STATUS_v1.26.9.md`)
Policy: fixes only. rc2 exists because rc1's gate, smoke tests and benchmarks were exercising
KNN, not HNSW, and a real HNSW index build crashed mysqld on the component build.
MySQL 26.7 remains a **blocking** gate.

## 1) What changed since rc1

- [x] #117 `type=hnsw` comments now build HNSW (parser, upper-cased type, empty component index dir,
      failed saves no longer abort mysqld). Closes #111, #118.
- [x] #120 test scripts remove containers with `-v` (Docker volume leak, #110).
- [x] #121 Phase 3.3 proves the persisted index is reloaded (#112).
- [x] #122 `release.yml` dispatches the Docker publish (#109).
- [x] #123 benchmark configures the plugin and fails when the index build fails (#113).
- [x] #125 pre-release gate runs in CI, non-blocking (#116).
- [x] #126 exact deinit rollback (#115).
- [x] CI benchmark baselines promoted on the `benchmarks` branch (#114).

## 2) Pre-release gate on the merged commit (blocking)

```bash
./scripts/build-component-8.4-docker.sh  mysql-8.4.8  dist/component-8.4
./scripts/build-component-9.7-docker.sh  mysql-9.7.0  dist/component-9.7
./scripts/build-component-26.7-docker.sh mysql-26.7.0 dist/component-26.7
./scripts/pre-release-test.sh          # 8.4 + 9.7
./scripts/pre-release-test.sh 26.7     # 26.7 (BLOCKING)
```

- [ ] Exit 0 on both invocations.
- [ ] Phase 1 smoke: the HNSW index build takes seconds (rc1's KNN fallback took ~1 s).
- [ ] Phase 2 new checks pass on all three versions: index type HNSW for both comment formats,
      server survives a failed save, failed save and explicit `save` report an error.
- [ ] Phase 3 3.1-3.5 pass on all three versions (3.3 asserts `Current Rows`, 3.5 asserts all six UDFs).
- [ ] The CI `Pre-release gate` workflow is green on the merge commit (non-blocking).

## 3) Tag and automation

- [ ] Create and push the RC tag on the merged `main` commit:
  ```bash
  git tag -a v1.26.9-rc2 -m "Release candidate 2 for v1.26.9"
  git push origin v1.26.9-rc2
  ```
- [ ] `release.yml` passes (incl. `Create GitHub Release`).
- [ ] **`Dispatch Docker image publish` job starts `docker-publish.yml` on the tag by itself**
      (first real test of #122). If not, run `gh workflow run docker-publish.yml --ref v1.26.9-rc2`
      and record the failure.
- [ ] `docker-publish.yml` publishes `mysql8.0`, `mysql8.4`, `mysql9.7`, `mysql8.4-component`,
      `mysql9.7-component`, `mysql26.7` (this overwrites the mutable plugin tags and `latest`).

## 4) Published-image validation

- [ ] `./scripts/smoke-published-images.sh` passes for all six tags.
- [ ] `smoke-readme.sh` on `ghcr.io/askdba/myvector:mysql26.7` passes (**blocking**).
- [ ] **HNSW on a published component image** (the smoke test does not cover it; rc1 crashed here):
      with a working `myvector.cnf`, build an index on a `type=hnsw` column, check
      `MYVECTOR_INDEX_STATUS` reports `Type : HNSW` and the server stays up. Do it on
      `mysql8.4-component` and `mysql26.7`.

## 5) Benchmarks

- [ ] `myvectorbench` run on the tag compares against the promoted baselines (no `NO_BASELINE`).
- [ ] Plugin 8.4 `recall_at_10` >= 0.9 (baseline 0.978).
- [ ] No cell breaches its thresholds. QPS/latency are dominated by `docker exec` overhead (#124)
      and are weak signals.

## 6) Documentation

- [ ] `RELEASE_NOTES_v1.26.9.md` and `CHANGELOG.md` `[1.26.9]` reviewed.
- [ ] Record everything in `RC2_STATUS_v1.26.9.md`.

## 7) RC2 decision

- [ ] Blocker count = 0. Go/No-Go recorded.

## 8) Post-RC: final tag

After the checks above pass:
```bash
git tag -a v1.26.9 -m "Release v1.26.9"
git push origin v1.26.9
```
