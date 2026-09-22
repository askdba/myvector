# Release Status - v1.26.9 (final)

Release tag: `v1.26.9` (commit `384d9df`, pushed 2026-09-22)
Source under test: `ff97228` (main after PR #139; identical shipped-code tree to the
gate run below, checked with `git diff --stat` against the exact commit tested)
Previous release: `v1.26.5.2`
Previous RC: `v1.26.9-rc3` (`6ab4a83`) - superseded; #139 (save/checkpoint failure
handling, found in a post-rc3 self-review) merged after rc3 was tagged and is not in
rc3's published images.
Date: 2026-09-22

Gate policy: MySQL 26.7 is **blocking**.

## Local pre-release gate (components rebuilt from `0ef7537`, the branch tip squash-merged
## as `ff97228` with an identical tree; aarch64, Docker)

Run twice across the PR #139 review cycle, both on the same final source:

- `./scripts/pre-release-test.sh` (8.4 + 9.7): **40 passed, 0 failed, 12 skipped, exit 0**
  (both runs).
- `./scripts/pre-release-test.sh 26.7` (blocking): **20 passed, 0 failed, 6 skipped, exit 0**
  (both runs).
- Includes the fd-leak-specific check: 15 repeated forced write failures on a plugin built
  from this source, fd count flat throughout (34 -> 34).

## CI on `main` at `ff97228` (post-merge push, not just the PR)

| Workflow | Result |
|---|---|
| MyVector CI ([35714998932](https://github.com/askdba/myvector/actions/runs/35714998932)) | success |
| Pre-release gate ([35714998935](https://github.com/askdba/myvector/actions/runs/35714998935)) | success |

## Release workflow ([run 35716116776](https://github.com/askdba/myvector/actions/runs/35716116776))

All jobs success: Build Plugin 8.0.35 / 8.4.8 / 9.0.0, Build Component 8.4.8 / 9.7.0,
Create GitHub Release, Dispatch Docker image publish (**third automatic success** for #122).

## Docker publish ([run 35716492458](https://github.com/askdba/myvector/actions/runs/35716492458))

Started automatically. First attempt: 5 of 6 jobs succeeded; `build-and-publish
(mysql-8.0.45, 8.0, ...)` failed on the **known QEMU emulation flake** (#138,
`qemu: uncaught target signal 11` during an arm64 `apt-get`/`libc-bin` postinst) --
**second confirmed occurrence** of #138 (rc3 hit it on the 9.7 leg; this run hit it on
8.0), now noted on the issue as evidence it is systematic. Re-ran only the failed job:
success. All six tags are final.

| Image | Status |
|-------|--------|
| ghcr.io/askdba/myvector:mysql8.0 | published (after one retry, #138) |
| ghcr.io/askdba/myvector:mysql8.4 | published |
| ghcr.io/askdba/myvector:mysql9.7 | published |
| ghcr.io/askdba/myvector:mysql8.4-component | published |
| ghcr.io/askdba/myvector:mysql9.7-component | published |
| ghcr.io/askdba/myvector:mysql26.7 | published |

Publishing this tag updated the mutable plugin tags (`mysql8.0`, `mysql8.4`, `mysql9.7`,
`latest`) for real -- this is the GA release, not a candidate.

## Published-image smoke (`smoke-published-images.sh`, scripts from the tag)

| Image | Result | Digest (rc3 -> final) |
|-------|--------|------------------------|
| mysql8.0 | pass | e8fb3b632549 -> 7645cf173bd5 |
| mysql8.4 | pass | e90468089822 -> 219c74a5735c |
| mysql9.7 | pass | 66a3f3a56c8e -> 9472e0414c58 |
| mysql8.4-component | pass | d8b0e08499cd -> f1ab14a11abb |
| mysql9.7-component | pass | cb39bc064ea1 -> 1b3125c78286 |
| mysql26.7 | pass | e555097f2e3a -> d7793fcbffcf |

- "All smokes completed OK", exit 0. All six digests differ from rc3, confirming these
  are the final release images.
- `smoke-readme.sh ghcr.io/askdba/myvector:mysql26.7` (blocking): exit 0.

## HNSW on published component images

`mysql8.4-component` and `mysql26.7`, both comment forms (`type=hnsw` with and without
the `|` marker): build `SUCCESS`, `Type : HNSW`, `Current Rows : 3`, server stays up.

## Benchmark ([myvectorbench run 35716117079](https://github.com/askdba/myvector/actions/runs/35716117079))

All four cells: **PASS: all metrics within threshold**, and noticeably tighter than the
rc3 comparison (which ran on identical source and still showed up to +49%/-24% noise;
this run's largest delta is +7.2%).

| Cell | index build | insert QPS | KNN QPS | recall@10 |
|------|-------------|------------|---------|-----------|
| plugin 8.4 | 2.946 s (+7.2%) | 3707 (-3.5%) | 12.36 (-4.8%) | 0.9778 (unchanged) |
| component 8.4 | 2.667 s (-4.3%) | 3942 (+3.4%) | 5.40 (-8.7%) | n/a |
| component 9.7 | 2.636 s (-1.3%) | 3916 (+1.9%) | 5.67 (-0.0%) | n/a |
| component 26.7 | 2.527 s (+7.2%) | 4062 (-7.6%) | 6.05 (-5.9%) | n/a |

## Known issues / follow-ups

Full list: `docs/LIMITATIONS.md`. Open: #119 (option matching), #124 / #131 / #133
(benchmark), #130 (DDL rewrite in COMMENT), #138 (QEMU emulation flake in Docker publish),
#80 (Windows).

## Blockers

None.

## Go / No-Go

**GO. `v1.26.9` is released.** Pre-release gate (local and CI, all three versions, blocking 26.7), Release workflow, Docker publish (one retry for the known #138 flake), published-image smoke on all six tags, `smoke-readme` on 26.7, HNSW on published component images, and the benchmark comparison all pass.
