# RC2 Status - v1.26.9

RC tag: `v1.26.9-rc2` (commit `586f746`, pushed 2026-09-21)
Source under test: `d0c0679` (main after PRs #117, #120-#123, #125, #126; the RC commit adds release docs only)
Final release tag: `v1.26.9`
Date: 2026-09-21

Gate policy: MySQL 26.7 is **blocking** for this RC.

Why rc2: the rc1 gate, smoke tests and benchmarks silently exercised KNN (the scripts' comment
format never parsed as HNSW), and a real HNSW build crashed mysqld on the component build.
The published rc1 component images predate the fix.

## Local pre-release gate (artifacts built from `d0c0679`, aarch64, Docker)

- `./scripts/pre-release-test.sh` (8.4 + 9.7): **40 passed, 0 failed, 12 skipped, exit 0**.
- `./scripts/pre-release-test.sh 26.7` (blocking): **20 passed, 0 failed, 6 skipped, exit 0**.
- On all three versions: Phase 1 HNSW index build ~9 s (real HNSW); Phase 2 index type is HNSW for
  both comment formats, the server survives a failed save, a failed build save and a failed explicit
  `save` report an error; Phase 3.1-3.5 pass (3.3 asserts `Current Rows`, 3.5 asserts all six UDFs).
- Skips are the existing plugin-only / debug-build-only checks.

## CI

- PR CI for #117, #120-#123, #125, #126, #127: all jobs green.
- `Pre-release gate` workflow (new, #125, non-blocking) on `main` @ `d0c0679`: success on x86_64
  runners (about 6-7 min per version, incl. the component build).
- #127 had one failed check, `Lint Code Base`, caused by a transient actionlint download failure
  (a 92-byte error body instead of the tarball). Re-running that job passed; not a lint finding.

## Release workflow ([run 35616214208](https://github.com/askdba/myvector/actions/runs/35616214208), tag `v1.26.9-rc2` @ `586f746`)

All jobs success: Build Plugin 8.0.35 / 8.4.8 / 9.0.0, Build Component 8.4.8 / 9.7.0,
Create GitHub Release, **Dispatch Docker image publish**.

## Docker publish ([run 35616658559](https://github.com/askdba/myvector/actions/runs/35616658559))

**Started automatically** by the `Dispatch Docker image publish` job (first real test of #122;
no manual dispatch needed). All 6 jobs success.

| Image | Status |
|-------|--------|
| ghcr.io/askdba/myvector:mysql8.0 | published |
| ghcr.io/askdba/myvector:mysql8.4 | published |
| ghcr.io/askdba/myvector:mysql9.7 | published |
| ghcr.io/askdba/myvector:mysql8.4-component | published |
| ghcr.io/askdba/myvector:mysql9.7-component | published |
| ghcr.io/askdba/myvector:mysql26.7 | published |

Note: publishing overwrites the mutable plugin tags (`mysql8.0`, `mysql8.4`, `mysql9.7`, `latest`).

## Published-image smoke (`smoke-published-images.sh`, scripts from the tag, pulled from GHCR)

| Image | Result | Digest (rc1 -> rc2) |
|-------|--------|---------------------|
| mysql8.0 | pass | d23d91fb5300 -> 68877004b483 |
| mysql8.4 | pass | 19f834977d14 -> 658110140b55 |
| mysql9.7 | pass | 26691f9ebe15 -> 2f65f7bfe0a7 |
| mysql8.4-component | pass | 0cfac624972a -> e0f5f6b6727f |
| mysql9.7-component | pass | 85456db5accb -> 84d28f1d0dff |
| mysql26.7 | pass | c073a47ae34e -> 58db4707c652 |

- `smoke-published-images.sh`: "All smokes completed OK", exit 0. All six digests differ from rc1,
  so these are the rc2 images and not stale ones.
- `smoke-readme.sh ghcr.io/askdba/myvector:mysql26.7` (blocking): exit 0.
- Scope: container start, UDF load, `myvector_construct` / `display` / `distance`.

## HNSW on published component images (the smoke test does not cover this; rc1 crashed here)

With a working `myvector.cnf`, building an index on both comment forms
(`MYVECTOR COLUMN type=hnsw,...` and `MYVECTOR Column |type=HNSW,...`):

| Image | no-pipe form | pipe form |
|-------|--------------|-----------|
| mysql8.4-component | pass | pass |
| mysql26.7 | pass | pass |

Each: build `SUCCESS`, `Type : HNSW`, `Current Rows : 3`, server still running.
`mysql9.7-component` not separately checked (same code path as the two above; covered by the gate).

## Benchmark ([myvectorbench run 35616214244](https://github.com/askdba/myvector/actions/runs/35616214244))

First tag run compared against the promoted baselines (no `NO_BASELINE`). All four cells:
**PASS: all metrics within threshold.** Baselines and how to read them:
[`BENCHMARK_v1.26.9.md`](BENCHMARK_v1.26.9.md).

| Cell | index build | insert QPS | KNN QPS | recall@10 |
|------|-------------|------------|---------|-----------|
| plugin 8.4 | 2.739 s (-0.3%) | 3885 (+1.2%) | 13.2 (+1.7%) | 0.9778 (unchanged) |
| component 8.4 | 2.888 s (+3.7%) | 3792 (-0.5%) | 5.81 (-1.7%) | n/a |
| component 9.7 | 2.673 s (+0.1%) | 3872 (+0.7%) | 5.69 (+0.3%) | n/a |
| component 26.7 | 2.708 s (+14.9%) | 3878 (-11.8%) | 5.65 (-12.1%) | n/a |

The 26.7 deltas are larger than the others but within threshold; the 26.7 baseline is a single run and
runner variance on shared runners is likely, not a confirmed regression. Component cells have no ANN or
recall (query rewrite unavailable). QPS/latency are dominated by `docker exec` overhead (#124).

## Known issues / follow-ups

- #119 `dist=cosine` (lower case) silently becomes L2.
- #124 benchmark QPS/latency are dominated by `docker exec` overhead.
- `Lint Code Base` can fail on a transient actionlint download; unpinned install script, no retry.
- The published-image smoke test does not build an HNSW index (checked separately above).

## Blockers

None.

## Go / No-Go

**GO** for `v1.26.9-rc2`: pre-release gate (incl. blocking 26.7), CI, Release workflow, automatic Docker
publish, published-image smoke on all six tags, `smoke-readme` on 26.7, HNSW on published component
images, and the benchmark comparison all pass. The decision to cut the final `v1.26.9` is separate and pending.
