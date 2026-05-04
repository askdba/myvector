# Release Notes - v1.26.5

Release date: 2026-05-04
Previous release: v1.26.3

## Summary

v1.26.5 introduces the MySQL **Component** build path for MySQL 8.4 LTS and
9.7 LTS, a unified logging abstraction, plugin stability fixes, and updates
the MySQL 9.x line from 9.6 to 9.7 LTS across all CI and Docker workflows.
No source-breaking changes; the plugin path for 8.0/8.4/9.0 remains stable.

## Added

- **MySQL Component build** (`src/component_src/`) — install via
  `INSTALL COMPONENT` on MySQL 8.4 and 9.7 LTS (#88).
- `myvector_log.h` — unified logging macros (`MYVEC_LOG_INFO`, `MYVEC_LOG_ERR`,
  etc.) shared by plugin and component, replacing scattered `#ifdef` blocks.
- Out-of-tree component build scripts: `scripts/build-component.sh`,
  `build-component-8.4-docker.sh`, `build-component-9.7-docker.sh`.
- `scripts/smoke-component.sh` — real-data component smoke test.
- CI jobs for component build and test (MySQL 8.4 and 9.7).
- Docker image tag `ghcr.io/askdba/myvector:mysql9.7` (LTS line).
- Release workflow produces component `.tar.gz` artifacts for 8.4 and 9.7.
- `docs/BUILD_MODES.md` and `docs/COMPONENT_MIGRATION_PLAN.md`.

## Changed

- MySQL 9.x target updated from 9.6 → **9.7 LTS** in all CI, release, and
  Docker publish workflows.
- `CMakeLists.txt` dual-mode: in-tree plugin or out-of-tree component via
  `MYSQL_SOURCE_DIR`.
- `hnswdisk.h`/`hnswdisk.i` now use `MYVEC_LOG_*` macros.

## Fixed

- Thread safety: `gmtime` → `gmtime_r`, `asctime` → `asctime_r` (#87).
- Null-guard in `myvector_ann_set` row function; sets `*length=0` on
  null-index return (#87).
- Concurrency fixes in plugin init/deinit (#87).
- `binary_log` namespace conflict on MySQL 8.4/9.7 component build.
- Config file reader: hardened permission and parse error handling.
- CMakeCache.txt guard in component build scripts.
- Quick Start `wget` URL: `insert50d.sql` → `insert50d.sql.gz` (issue #89).

## Documentation updates

- `README.md`: Docker tag table updated, plugin vs. component install note,
  Quick Start wget fix.
- `docs/BUILD_MODES.md`, `docs/COMPONENT_MIGRATION_PLAN.md` added.

## Upgrade / migration notes

- No schema or index migration required.
- **Docker tag change:** Switch from `:mysql9.6` to `:mysql9.7`.
- Component path is the forward path for MySQL 8.4+; plugin path remains
  stable for 8.0/8.4/9.0 through their respective EOL dates.
- Windows builds remain unsupported — use Linux container images.

## Known issues

- No release-blocking issues identified at RC1 cut.
- Final known-issues list validated at RC sign-off.

## Excluded from this release scope

- MySQL 8.0 component build (no plan; 8.0 EOL path is plugin only).
