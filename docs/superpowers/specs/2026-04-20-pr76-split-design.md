# PR #76 Split Design

**Date:** 2026-04-20
**Status:** Approved
**Author:** Alkin Tezuysal

---

## Context

PR #76 (`feature/component-migration-8.4-9.6`) introduced two distinct bodies of work in a single branch: bug fixes and CI improvements to the existing plugin, and a full MySQL Component build targeting MySQL 8.4+. After 41 commits of CI-chasing the branch has accumulated test failures (lint, integration tests, 9.6 component build) and inherited debt that makes it unready to merge as-is.

The strategic direction is clear: the MySQL Component architecture is the forward path for MySQL 8.4 and 9.6 (LTS); the legacy plugin will be deprecated once the component ships. MySQL 8.0 reaches EOL and will not receive component support.

This design splits the work into two clean deliverables and closes PR #76.

---

## Part 1 — Plugin Stability and Workflow Fixes

### Goal

Land the self-contained bug fixes and workflow improvements from PR #76 independently, without any component build machinery.

### Branch

`fix/plugin-stability-and-workflow` off `main` (current HEAD after v1.26.3 release).

### Scope — files cherry-picked from the feature branch

| File | Change |
|---|---|
| `src/myvector.cc` | Format specifier fixes (`%zu`), NULL check on `myvector_construct` first arg, `gmtime_r`/`asctime_r` thread-safety (replaces non-thread-safe `gmtime`), `escape_identifier` buffer overflow fix, concurrency fixes: `SharedLockGuard` release-only semantics, `VectorIndexCollection::open()` acquires shared lock before return |
| `src/myvector_plugin.cc` | Minor safety additions |
| `include/hnswdisk.i` | Atomic persistence improvements (checkpoint/fsync path) |
| `include/hnswdisk.h` | Minor include fix |
| `.github/workflows/release.yml` | Bump `softprops/action-gh-release` v1 → v2 |
| `.github/workflows/docker-publish.yml` | Gate push on `workflow_run`; only push images when triggered by Release workflow, not on PRs |
| `.github/actionlint.yaml` | Actionlint suppression rules |
| `.markdownlint.yaml` | Markdown lint config |
| `examples/stanford50d/create-basic.sql` | Basic BLOB schema SQL for examples |

**Explicitly excluded:** `CMakeLists.txt` dual-mode, all of `src/component_src/`, component build scripts, component CI jobs, component-specific headers (`mysql_version.h`, `my_config.h`, `mysql_component_service_base.h`), PR76 status/review docs.

### Verification

- All existing CI checks pass (build 8.0/8.4/9.0, lint, integration tests).
- No component CI jobs added.
- Docker publish workflow tested: images only pushed on release tag, not on PR.

---

## Part 2 — MySQL Component (fresh PR)

### Goal

Ship a clean MySQL Component build of MyVector targeting MySQL 8.4 and 9.6 (LTS), on a branch with no inherited CI debt from the feature branch.

### Branch

`feature/myvector-component` off `main` after Part 1 merges.

### Scope — ported from the feature branch

**Component sources:**
- `src/component_src/myvector_component.cc` — lifecycle init/deinit
- `src/component_src/myvector_udf_service.cc/.h` — UDF registration via `mysql_udf_metadata`
- `src/component_src/myvector_binlog_service.cc/.h` — binlog monitoring thread, `binlog_state.json` persistence
- `src/component_src/myvector_query_rewrite_service.cc` — query rewriter (MySQL 9.0+ only, optional)
- `src/component_src/myvector_component_config.cc` — config loading
- `src/component_src/myvector.json` — component manifest

**Build system:**
- `CMakeLists.txt` — dual-mode: `MYSQL_ADD_PLUGIN` path preserved; new `MYSQL_SOURCE_DIR`-based component path added
- `scripts/build-component.sh` — component build script (out-of-tree, tarball download)
- `scripts/build-component-9.6-docker.sh` — 9.6-specific Docker build (OracleLinux 9 + MySQL 9.6 libs)
- `scripts/pre-pr.sh` — pre-PR local checks

**Headers (component build stubs):**
- `include/mysql_component_service_base.h`
- `include/mysql_version.h` (version-specific stub, written by build script)
- `include/my_config.h`

**Tests and docs:**
- `mysql-test/suite/myvector/` — component MTR tests
- `docs/COMPONENT_MIGRATION_PLAN.md`
- `docs/BUILD_MODES.md`

### CI matrix

| Job type | MySQL versions |
|---|---|
| Plugin build + test (existing) | 8.0, 8.4, 9.0 — unchanged |
| Component build | 8.4, 9.6 only |
| Component test | 8.4, 9.6 only |

8.0 and 9.0 component jobs from the feature branch are dropped entirely.

### Deprecation notice

`README.md` and `docs/` updated to state:
- The plugin (`.so` + `INSTALL PLUGIN`) is the current stable installation path.
- The component (`INSTALL COMPONENT`) is the forward path for MySQL 8.4+.
- MySQL 8.0 plugin support will be maintained until MySQL 8.0 EOL; no component build is planned for 8.0.

### Verification

- Component builds on 8.4 and 9.6.
- `INSTALL COMPONENT 'file://myvector'` succeeds on both versions.
- UDF smoke tests pass (construct, display, distance, ann_set).
- `UNINSTALL COMPONENT` cleanly deregisters UDFs.
- Plugin CI (8.0/8.4/9.0) unaffected.

---

## Execution sequence

1. Create `fix/plugin-stability-and-workflow` off `main`, cherry-pick Part 1 file diffs, open PR.
2. Merge Part 1 PR after CI green.
3. Close PR #76 with explanation comment.
4. Create `feature/myvector-component` off updated `main`, port component files, narrow CI matrix to 8.4 + 9.6, open new PR.
