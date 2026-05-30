# RC1 Status - v1.26.5.2

RC tag: `v1.26.5.2-rc1` (pending)
HEAD SHA at RC cut: (to be recorded)
Date: 2026-05-30

## CI

| Job | Status |
|-----|--------|
| build (8.0) | pending |
| build (8.4) | pending |
| build (9.0) | pending |
| build-component (8.4) | pending |
| build-component-9-7 | pending |
| test (8.0) | pending |
| test (8.4) | pending |
| test (9.0) | pending |
| test-component (8.4) | pending |
| test-component-9-7 | pending |
| lint | pending |

## Pre-release gate (`./scripts/pre-release-test.sh`)

| Phase | MySQL 8.4 | MySQL 9.7 |
|-------|-----------|-----------|
| Phase 1 (smoke) | pending | pending |
| Phase 2 (online updates) | pending | pending |
| Phase 3.1 (install timing) | pending | pending |
| Phase 3.2 (reload persistence) | pending | pending |
| Phase 3.3 (binlog cleanup) | pending | pending |
| Phase 3.4 (concurrent reads) | pending | pending |
| Phase 3.5 (DROP stability) | pending | pending |

## Smoke tests

| Image | smoke-readme.sh | smoke-published-images.sh |
|-------|----------------|--------------------------|
| mysql8.0 | pending | pending |
| mysql8.4 | pending | pending |
| mysql9.7 | pending | pending |

## Benchmark comparison (vs v1.26.5.2 baseline)

| Cell | Result | Notes |
|------|--------|-------|
| plugin-8.4 | pending | |
| component-8.4 | pending | |
| component-9.7 | pending | |

## Concurrent stress

| Cell | passed | Notes |
|------|--------|-------|
| component-8.4 | pending | |

## Decision

- Blocker count: 0
- Go/No-Go: pending
