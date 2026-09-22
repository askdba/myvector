# Release Status - v1.26.9 (final)

Release tag: `v1.26.9` (not yet pushed)
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

## Release workflow

_TBD after tag_

## Docker publish

_TBD after tag_

| Image | Status |
|-------|--------|
| ghcr.io/askdba/myvector:mysql8.0 | _TBD_ |
| ghcr.io/askdba/myvector:mysql8.4 | _TBD_ |
| ghcr.io/askdba/myvector:mysql9.7 | _TBD_ |
| ghcr.io/askdba/myvector:mysql8.4-component | _TBD_ |
| ghcr.io/askdba/myvector:mysql9.7-component | _TBD_ |
| ghcr.io/askdba/myvector:mysql26.7 | _TBD_ |

## Published-image smoke

_TBD_

## HNSW on published component images

_TBD_

## Benchmark (compared against the promoted baselines)

_TBD_

## Known issues / follow-ups

Full list: `docs/LIMITATIONS.md`. Open: #119 (option matching), #124 / #131 / #133
(benchmark), #130 (DDL rewrite in COMMENT), #138 (QEMU emulation flake in Docker publish),
#80 (Windows).

## Blockers

_None recorded._

## Go / No-Go

_TBD_
