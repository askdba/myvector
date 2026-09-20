# RC1 Status - v1.26.9

RC tag: `v1.26.9-rc1`
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

## Release workflow (run _TBD after tag_)

_TBD_

## Docker publish (run _TBD after tag_)

| Image | Status |
|-------|--------|
| ghcr.io/askdba/myvector:mysql8.0 | _TBD_ |
| ghcr.io/askdba/myvector:mysql8.4 | _TBD_ |
| ghcr.io/askdba/myvector:mysql9.7 | _TBD_ |
| ghcr.io/askdba/myvector:mysql8.4-component | _TBD_ |
| ghcr.io/askdba/myvector:mysql9.7-component | _TBD_ |
| ghcr.io/askdba/myvector:mysql26.7 | _TBD_ |

## Published-image smoke (`smoke-published-images.sh`)

_TBD after publish_

## Benchmark / stress

Not run for RC1 (26.7 baseline to be established post-RC).

## Known issues / follow-ups

- Server log prints `unknown index type ... using KNN` for a `type=hnsw` column on the
  component build; may mean the #92 case-insensitivity fix is missing from the component path. Undiagnosed.
- Test 3.3 compares brute-force KNN before/after reload, so it does not prove the
  on-disk index was reloaded.
- `pre-release-test.sh` / smoke scripts leak anonymous Docker volumes (`docker rm -f`
  without `-v`); can fill the disk over repeated runs.
- Deinit rollback ignores `register_udfs()` return value; binlog-stop failure path unreachable today.

## Blockers

None.

## Go / No-Go

**GO** for `v1.26.9-rc1` on gate results above; published-image smoke pending tag.
