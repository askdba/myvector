# RFC-004 Gap Closure Design
# Reliability, Concurrency, and Crash Consistency — v1.26.5.1-rc1

**Date:** 2026-05-18
**Author:** MyVector Engineering
**Target version:** v1.26.5.1-rc1
**Issue:** #77
**Delivery:** Single combined PR, 5 sequential commits (Option A)

---

## Summary

Five gaps exist between RFC-004 (as written in issue #77) and the current codebase. This spec closes all five in one patch release. Changes are independent and land as separate commits for reviewability.

---

## Gap 1 — Zero-vector rejection for cosine distance

**File:** `src/myvector.cc`

**Problem:** `insertVector()` with a cosine-metric index silently accepts zero-magnitude vectors. `computeCosineDistance` guards against division by zero with `if (t)` but returns `1.0` (max distance) instead of rejecting the insert.

**Design:** At the top of the insert path, before adding to the HNSW graph, when the index metric is `COSINE`:
1. Compute the L2 norm of the input vector.
2. If `norm == 0.0`, set UDF error message to `ER_MYVECTOR_INVALID_VECTOR` and return early.
3. The InnoDB row commit is unaffected — only the index update is skipped and the error surfaced to the client.

**Scope:** Insert path only. `computeCosineDistance` is unchanged — zero query vectors in search return max-distance results, not errors.

---

## Gap 2 — Configurable max vector dimension

**Files:** `src/myvector.cc`, `src/myvector_plugin.cc`, `src/component_src/myvector_component_config.cc`

**Problem:** `MYVECTOR_MAX_VECTOR_DIM` is hardcoded at `4096`. RFC-004 states 16384 (incorrect). MySQL's native VECTOR type supports up to 16383 dimensions.

**Design:**
- Replace `const size_t MYVECTOR_MAX_VECTOR_DIM = 4096` with a global `ulong myvector_max_vector_dim`.
- **Plugin path** (`myvector_plugin.cc`): register `MYSQL_SYSVAR_ULONG(max_vector_dim, myvector_max_vector_dim, PLUGIN_VAR_RQCMDARG, ...)` with min=2, max=16383, default=4096. Add to `myvector_system_variables[]`.
- **Component path** (`myvector_component_config.cc`): add `unsigned long myvector_max_vector_dim = 4096` alongside the existing `myvector_rebuild_on_start` bool. No component sysvar registration (plugin sysvars are being phased out with the plugin architecture).
- The existing validation `dim <= 1 || dim > MYVECTOR_MAX_VECTOR_DIM` reads the variable instead of the constant — no structural change.

---

## Gap 3 — DBUG fault injection point

**File:** `include/hnswdisk.i`

**Problem:** RFC-004 Section 6 specifies `DBUG_EXECUTE_IF("simulate_vector_crash", abort())` for crash-recovery testing. Nothing is implemented.

**Design:** Add one injection point in the persistence flush path — after the `fsync()` that durably writes index data, before the checkpoint status file is updated. This is the exact window where kill-9 leaves the index inconsistent and exercises the recovery path on restart.

```cpp
DBUG_EXECUTE_IF("simulate_vector_crash", abort(););
```

Requires `#include "my_dbug.h"`. Compiles to a no-op in release builds (`-DWITH_DEBUG=1` required to activate). No new test file in this PR — manual verification procedure is in RFC-004 Section 6 and the PR test plan.

---

## Gap 4 — RFC-004 doc corrections

**File:** `docs/` — create `docs/RFC-004-RELIABILITY.md` from issue #77 body with corrections applied.

Corrections:
| Section | Old | New |
|---------|-----|-----|
| Max dimension | 16384 | Configurable via `myvector_max_vector_dim`, default 4096, max 16383 |
| Atomic persistence | write → fsync → rename | Checkpoint protocol: write → fsync → checkpoint status file → fsync → mark consistent (`hnswdisk.i`) |
| `myvector_rebuild_on_start` | Config knob (implied both paths) | Component-internal default (`false`); plugin architecture being phased out, no sysvar planned |
| Error code | `ER_VECTOR_INVALID` | `ER_MYVECTOR_INVALID_VECTOR` |
| Production checklist | All unchecked | Check "Zero-vector validation verified" and "Dimension bounds enforced" on merge |

---

## Gap 5 — Version bump + CHANGELOG + PR

**Files:** `CHANGELOG.md`, version constant (wherever defined)

- Bump version to `v1.26.5.1-rc1`.
- CHANGELOG entry covering all 5 items.
- Open GitHub PR targeting `main`, referencing issue #77, with the RFC Section 6 manual test procedure as the PR test plan.

---

## Commit sequence (Option A)

| # | Commit message | Files touched |
|---|----------------|---------------|
| 1 | `fix: reject zero-magnitude vectors on cosine index insert` | `src/myvector.cc` |
| 2 | `feat: add myvector_max_vector_dim sysvar, default 4096 max 16383` | `src/myvector.cc`, `src/myvector_plugin.cc`, `src/component_src/myvector_component_config.cc` |
| 3 | `fix: add DBUG_EXECUTE_IF simulate_vector_crash in hnswdisk flush path` | `include/hnswdisk.i` |
| 4 | `docs: add RFC-004-RELIABILITY.md with corrected gap analysis` | `docs/RFC-004-RELIABILITY.md` |
| 5 | `chore: bump version to v1.26.5.1-rc1, update CHANGELOG` | `CHANGELOG.md`, version file |

---

## Non-goals

- `myvector_rebuild_on_start` plugin sysvar — plugin architecture expiring, document as component-internal only.
- Sysbench stress harness or TSAN/Helgrind CI jobs — deferred to a future reliability milestone.
- Temp-file-then-rename for main `.bin` — the existing checkpoint protocol already provides equivalent crash safety; rename pattern only applies to the component state file.
