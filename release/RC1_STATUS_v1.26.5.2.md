# RC1 Status - v1.26.5.2

RC tag: `v1.26.5.2-rc1`
HEAD SHA at RC cut: `3792be0`
Final release tag: `v1.26.5.2`
Date: 2026-05-30

## CI (MyVector CI — run 26690711321)

| Job | Status |
|-----|--------|
| build (8.0) | ✅ success |
| build (8.4) | ✅ success |
| build (9.0) | ✅ success |
| build-component (8.4) | ✅ success |
| build-component-9-7 | ✅ success |
| test (8.0) | ✅ success |
| test (8.4) | ✅ success |
| test (9.0) | ✅ success |
| test-component (8.4) | ✅ success |
| test-component-9-7 | ✅ success |
| lint | ✅ success |

## Release workflow (run 26692161432)

| Job | Status |
|-----|--------|
| Build Plugin 8.0 | ✅ success |
| Build Plugin 8.4 | ✅ success |
| Build Plugin 9.0 | ✅ success |
| Build Component 8.4 | ✅ success |
| Build Component 9.7 | ✅ success |
| Create GitHub Release | ✅ success |

## myvectorbench (run 26692161428)

| Cell | Status |
|------|--------|
| benchmark (8.4, plugin) | ✅ success |
| benchmark (8.4, component) | ✅ success |
| benchmark (9.7, component) | ✅ success |

## Docker publish (run 26692450413 — manual dispatch)

| Image | Status |
|-------|--------|
| ghcr.io/askdba/myvector:mysql8.0 | ✅ success |
| ghcr.io/askdba/myvector:mysql8.4 | ✅ success |
| ghcr.io/askdba/myvector:mysql9.7 | ✅ success |

## Smoke tests (`./scripts/smoke-published-images.sh`)

| Image | Result | digest |
|-------|--------|--------|
| mysql8.0 | ✅ PASS | sha256:3ca1a8c7e23a |
| mysql8.4 | ✅ PASS | sha256:e8b6de8fe34e |
| mysql9.7 | ✅ PASS | sha256:a5f850cc61d3 |

Verified: `myvector_construct`, `myvector_distance`, `[1,2,3]` round-trip, L2 distance=2 on all three images.

## Pre-release gate

Not run for this patch release (tooling and reliability test changes only; no plugin/component C++ code changes since v1.26.5.1). CI integration tests cover correctness.

## Benchmark comparison

Baseline recorded in `results/` (synthetic 10k rows, dim=128, local Docker). See commit `484a931`.

## Decision

- Blocker count: **0**
- Go/No-Go: **GO**
- Final tag `v1.26.5.2` pushed and release created ✅
- GitHub Release assets: `myvector-component-mysql8.4.8-linux-amd64.tar.gz`, `myvector-component-mysql9.7.0-linux-amd64.tar.gz`, `checksums.txt`
