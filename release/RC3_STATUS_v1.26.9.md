# RC3 Status - v1.26.9

RC tag: `v1.26.9-rc3` (commit `6ab4a83`, pushed 2026-09-21)
Source: identical to rc2 (`d0c0679` plus release docs); rc3 adds only documentation, test scripts, a build script and a demo file.
Previous RC: `v1.26.9-rc2` (see `RC2_STATUS_v1.26.9.md`, GO)
Final release tag: `v1.26.9`
Date: 2026-09-21

## What changed since rc2 (`586f746`)

No file under `src/`, `include/`, `sql/`, `Dockerfile*`, `.github/`, `CMakeLists.txt` or `Makefile`
changed (checked with `git diff --name-only v1.26.9-rc2 origin/main -- <those paths>`, empty).

- #129 stress harness, Stanford 50d smoke and online-updates test scripts repaired; demo `create.sql` restored.
- #134 `docs/LIMITATIONS.md` (README, docs nav, release notes).
- #135 `build-docker-local.sh` targets arm64 on Linux `aarch64`.

Because no shipped code changed, rc2's pre-release gate (40 + 20 passed on `d0c0679`) and benchmark
evidence apply to rc3's binaries; the local gate was not re-run. rc3 re-verifies the published artifacts.

## Results

- **Release workflow** ([35655488916](https://github.com/askdba/myvector/actions/runs/35655488916)): all jobs success, incl. `Create GitHub Release` and `Dispatch Docker image publish`.
- **Docker publish** ([35655853164](https://github.com/askdba/myvector/actions/runs/35655853164)): **started automatically again** (second run of #122, so the dispatch is repeatable).
  - First attempt: 5 of 6 jobs succeeded; `build-and-publish (mysql 9.7 plugin)` failed. Cause: a QEMU emulation
    flake while building the arm64 image (`qemu: uncaught target signal 11` during the `libc-bin` post-install
    of an `apt` install); not our source (the same source built fine for rc2).
  - Re-ran only the failed job: success. All six tags are now rc3 (for a short time `mysql9.7` was still rc2).
- **Published-image smoke** (`smoke-published-images.sh`, scripts from the tag): all six tags pass, exit 0.
  `smoke-readme.sh` on `mysql26.7` (blocking): exit 0.

  | Image | Digest (rc2 -> rc3) |
  |-------|---------------------|
  | mysql8.0 | 68877004b483 -> e8fb3b632549 |
  | mysql8.4 | 658110140b55 -> e90468089822 |
  | mysql9.7 | 2f65f7bfe0a7 -> 66a3f3a56c8e |
  | mysql8.4-component | e0f5f6b6727f -> d8b0e08499cd |
  | mysql9.7-component | 84d28f1d0dff -> cb39bc064ea1 |
  | mysql26.7 | 58db4707c652 -> e555097f2e3a |

- **HNSW on published component images** (`mysql8.4-component`, `mysql26.7`): both comment forms pass
  (build `SUCCESS`, `Type : HNSW`, `Current Rows : 3`, server up).
- **Repaired optional checks** (first run of the fixed scripts on a tagged RC):
  online updates on the published plugin image `mysql8.4`: pass; Stanford 50d smoke on all six images: exit 0;
  RFC-004 stress on the 8.4 and 26.7 components (50 KNN / 50 write / 20 ANN, 60 s): `passed=True` on both
  (row count stable, KNN result stable, no deadlock, clean exit). The stress runs used the rc2-built
  component artifacts (identical source).
- **Benchmark** ([35655488841](https://github.com/askdba/myvector/actions/runs/35655488841)): all four cells
  "PASS: all metrics within threshold" against the promoted baselines.

  | Cell | index build | insert QPS | KNN QPS | recall@10 |
  |------|-------------|------------|---------|-----------|
  | plugin 8.4 | 2.311 s (-15.9%) | 4275 (+11.3%) | 19.37 (**+49.2%**) | 0.9778 (unchanged) |
  | component 8.4 | 2.663 s (-4.4%) | 3973 (+4.2%) | 5.42 (-8.3%) | n/a |
  | component 9.7 | 2.945 s (+10.3%) | 3789 (-1.4%) | 5.95 (+4.9%) | n/a |
  | component 26.7 | 2.275 s (-3.5%) | 3341 (**-24.0%**) | 7.86 (+22.2%) | n/a |

  **Noise floor:** rc3 has the same source as the baseline run, so every delta above is runner variance.
  It reaches +49% (plugin KNN QPS) and -24% (26.7 insert QPS, almost the 25% threshold). A single-run
  baseline with 25% thresholds on these QPS metrics is therefore both noisy and coarse. Recall and index
  build time are the steadier signals (#124).

## Known issues / follow-ups

Full list: `docs/LIMITATIONS.md`. Open: #119 (option matching), #124 / #131 / #133 (benchmark), #130 (DDL rewrite in COMMENT), #80 (Windows).

## Go / No-Go

**GO** for `v1.26.9-rc3`: all published-image, HNSW, repaired-optional-check and benchmark results pass on the rc3 images, which are built from the same source as rc2. Cutting the final `v1.26.9` is a separate decision.
