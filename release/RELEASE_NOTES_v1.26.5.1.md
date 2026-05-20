# Release Notes - v1.26.5.1

Release date: 2026-05-20
Previous release: v1.26.5

## Summary

v1.26.5.1 is a reliability patch that closes the RFC-004 gap items deferred
from v1.26.5: zero-magnitude vector rejection on cosine indexes, a read-only
`myvector_max_vector_dim` sysvar, a crash-injection debug hook for recovery
testing, and the full pre-release gate script that gates every future tag.

## Added

- **`myvector_max_vector_dim` sysvar** (read-only, default 4096, max 16383) —
  allows indexes with dimensions up to MySQL's native VECTOR type limit.
  Set at server start: `--myvector-max-vector-dim=8192`.
- **`scripts/pre-release-test.sh`** — full pre-release gate (Phase 1 component
  smoke + Phase 2 RFC-004 / edge-case tests) for MySQL 8.4 and 9.7. Exit 0 =
  safe to tag. Required before every future release tag.
- `docs/RFC-004-RELIABILITY.md` — on-disk record of RFC-004 gap analysis and
  design decisions (max dimension, persistence model, error codes).

## Fixed

- **Zero-magnitude vector rejection on cosine indexes**: UDF path now returns
  `ER_MYVECTOR_INVALID_VECTOR`; binlog/online-index path logs a warning and
  skips the update instead of silently inserting a max-distance entry.
- **Crash-recovery test hook**: `DBUG_EXECUTE_IF("simulate_vector_crash",
  abort())` added to `hnswdisk.i` checkpoint flush path. No-op in release
  builds; requires `-DWITH_DEBUG=1`.

## Documentation updates

- `CHANGELOG.md`: `[1.26.5.1]` entry added.
- `CLAUDE.md`: pre-release gate commands documented.

## Upgrade / migration notes

- No schema or index migration required.
- No Docker tag changes.
- `myvector_max_vector_dim` defaults to 4096 — no action needed unless indexes
  exceed 4096 dimensions, in which case set `--myvector-max-vector-dim=N` at
  server start.

## Known issues

- `MYVECTOR(...)` DDL annotation (dimension enforcement via query rewrite) is
  not available in the component build — `query_rewrite.h` is absent from
  MySQL's component services headers. The dimension limit is enforced at the
  C++ level (index operations clamp to `myvector_max_vector_dim`); DDL-time
  enforcement via the plugin path is unaffected.
- No other release-blocking issues. Gate passed: 14/14 tests on MySQL 8.4 + 9.7.
