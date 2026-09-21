# RC3 Status - v1.26.9

RC tag: `v1.26.9-rc3` (not yet pushed)
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

## Planned checks

- [ ] Release workflow green; **`Dispatch Docker image publish` starts the publish automatically again** (second run of #122).
- [ ] Publish run green for all six tags.
- [ ] `smoke-published-images.sh` passes for all six tags; digests differ from rc2.
- [ ] `smoke-readme.sh` on `mysql26.7` (blocking).
- [ ] HNSW on published `mysql8.4-component` and `mysql26.7`: `Type : HNSW`, server stays up.
- [ ] `myvectorbench` on the tag compares within thresholds against the promoted baselines.
- [ ] The repaired optional checks on the published images: online updates (plugin image), Stanford smoke,
      RFC-004 stress (8.4 and 26.7 components).

## Results

_TBD after the tag_

## Known issues / follow-ups

Full list: `docs/LIMITATIONS.md`. Open: #119 (option matching), #124 / #131 / #133 (benchmark), #130 (DDL rewrite in COMMENT), #80 (Windows).

## Go / No-Go

_TBD_
