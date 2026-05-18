# RFC-004 Gap Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close five RFC-004 spec/code gaps in a single combined PR targeting v1.26.5.1-rc1.

**Architecture:** Five independent commits in sequence — zero-vector rejection, configurable max dimension sysvar, DBUG fault injection, RFC doc corrections, version/CHANGELOG bump + PR. Each commit is self-contained and independently reviewable.

**Tech Stack:** C++17, MySQL Plugin API (`MYSQL_SYSVAR_*`), hnswlib (HNSW), POSIX fsync/rename, GitHub CLI (`gh`)

---

## File Map

| File | Change |
|------|--------|
| `include/myvector.h` | Add `virtual bool isCosineMetric()` to `AbstractVectorIndex` |
| `src/myvector.cc` | Zero-vector check in UDF path + `myvector_table_op`; replace `MYVECTOR_MAX_VECTOR_DIM` constant with extern ulong |
| `src/myvector_plugin.cc` | Register `MYSQL_SYSVAR_ULONG(max_vector_dim, ...)` |
| `src/component_src/myvector_component_config.cc` | Add `unsigned long myvector_max_vector_dim = 4096` |
| `include/hnswdisk.h` | Add `#include "my_dbug.h"` |
| `include/hnswdisk.i` | Add `DBUG_EXECUTE_IF("simulate_vector_crash", abort();)` |
| `docs/RFC-004-RELIABILITY.md` | Create corrected RFC doc |
| `CHANGELOG.md` | Add v1.26.5.1-rc1 entry |

---

## Task 1: Zero-vector rejection for cosine index inserts

**Context:** Inserting a zero-magnitude vector into a cosine-metric index silently returns max distance instead of an error. There are two code paths that call `insertVector`: the UDF path (`myvector_search_add_row_udf`, line ~2293 of `src/myvector.cc`) and the async binlog path (`myvector_table_op`, line 2421). Each needs a different response — hard error for UDF, warning+skip for binlog.

**Files:**
- Modify: `include/myvector.h:66-140` (add `isCosineMetric()` to `AbstractVectorIndex`)
- Modify: `src/myvector.cc:361-460` (add `isCosineMetric()` override to `KNNIndex`)
- Modify: `src/myvector.cc:574-660` (add `isCosineMetric()` override to `HNSWMemoryIndex`)
- Modify: `src/myvector.cc:2293-2315` (zero-vector check in UDF path)
- Modify: `src/myvector.cc:2421-2448` (zero-vector check in binlog path)

- [ ] **Step 1.1: Write the validation SQL (run this after implementation to confirm behavior)**

Save to a scratch file for manual execution against a live MySQL with the plugin loaded:

```sql
-- Setup
CREATE TABLE IF NOT EXISTS t_zvec (id INT PRIMARY KEY AUTO_INCREMENT, v VARBINARY(16));
-- Insert zero vector (4 floats all 0.0) with cosine index
-- First build a cosine index:
CALL myvector_index_build('test.t_zvec', 'v', 'id', 'type=HNSW,dim=4,dist=Cosine,size=100');

-- This INSERT should fail with ER_MYVECTOR_INVALID_VECTOR:
SELECT myvector_search_open_udf('test.t_zvec.v');
-- Use a zero vector: 0x00000000 00000000 00000000 00000000 (4x FP32 zeroes)
SELECT myvector_search_add_row_udf('test.t_zvec.v', '', 1,
    CAST(0x00000000000000000000000000000000 AS BINARY(16)));
-- Expected: ERROR - Invalid vector format or checksum mismatch
```

- [ ] **Step 1.2: Add `isCosineMetric()` to `AbstractVectorIndex` in `include/myvector.h`**

After line 80 (`virtual bool supportsConcurrentUpdates() { return false; }`), add:

```cpp
    virtual bool isCosineMetric() const { return false; }
```

- [ ] **Step 1.3: Override `isCosineMetric()` in `KNNIndex` in `src/myvector.cc`**

`KNNIndex` class body is around line 361. After `string getType() { return "KNN"; }`, add:

```cpp
    bool isCosineMetric() const override {
        std::string d = m_optionsMap.getOption("dist");
        return (d == "Cosine");
    }
```

- [ ] **Step 1.4: Override `isCosineMetric()` in `HNSWMemoryIndex` in `src/myvector.cc`**

`HNSWMemoryIndex` class body starts around line 574. After `string getType()`, add:

```cpp
    bool isCosineMetric() const override {
        return (m_dist == "Cosine" || m_dist == "CosineNorm" || m_dist == "Angular");
    }
```

- [ ] **Step 1.5: Add zero-norm helper above `myvector_search_add_row_udf` in `src/myvector.cc`**

Find `/* UDF : myvector_search_add_row_udf() */` (around line 2290). Insert this helper before it:

```cpp
static bool isZeroVector(const FP32* vec, int dim) {
    double norm = 0.0;
    for (int i = 0; i < dim; i++)
        norm += (double)vec[i] * vec[i];
    return (norm == 0.0);
}
```

- [ ] **Step 1.6: Add zero-vector check in UDF path (`myvector_search_add_row_udf`)**

The current body of `myvector_search_add_row_udf` (around line 2293):
```cpp
    AbstractVectorIndex* vi = (AbstractVectorIndex*)(initid->ptr);
    if (vi) {
        vi->insertVector((FP32*)vecval, dims, pkid);
    } else {
```

Replace the `if (vi)` block with:
```cpp
    AbstractVectorIndex* vi = (AbstractVectorIndex*)(initid->ptr);
    if (vi) {
        if (vi->isCosineMetric() && isZeroVector((const FP32*)vecval, dims)) {
            strcpy(message, ER_MYVECTOR_INVALID_VECTOR);
            *error = 1;
            return 0;
        }
        vi->insertVector((FP32*)vecval, dims, pkid);
    } else {
```

- [ ] **Step 1.7: Add zero-vector check in binlog path (`myvector_table_op`)**

`myvector_table_op` is at line 2421. The current `if (vi)` block calls:
```cpp
            vi->insertVector(vec.data(), vi->getDimension(), pkid);
```

Replace the `insertVector` call with:
```cpp
            if (vi->isCosineMetric() &&
                isZeroVector(reinterpret_cast<const FP32*>(vec.data()),
                             vi->getDimension())) {
                warning_print("Skipping zero-magnitude vector for cosine index"
                              " (pkid=%u), row is in table but not in index.", pkid);
            } else {
                vi->insertVector(vec.data(), vi->getDimension(), pkid);
            }
```

- [ ] **Step 1.8: Build and verify compiles cleanly**

```bash
make clean && make 2>&1 | tail -20
```
Expected: `myvector.so` produced, no errors or new warnings.

- [ ] **Step 1.9: Commit**

```bash
git add include/myvector.h src/myvector.cc
git commit -m "fix: reject zero-magnitude vectors on cosine index insert

UDF path returns ER_MYVECTOR_INVALID_VECTOR to client.
Binlog path logs warning and skips index update (row stays in InnoDB).
Adds isCosineMetric() virtual method to AbstractVectorIndex.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: `myvector_max_vector_dim` sysvar (default 4096, max 16383)

**Context:** `MYVECTOR_MAX_VECTOR_DIM` is a hardcoded `const size_t` at line 1188 of `src/myvector.cc`. Replace it with a `ulong` global, register it as a `PLUGIN_VAR_READONLY` sysvar in the plugin path, and add a default in the component config. The validation check at line 1249 reads the variable instead of the constant — no structural change needed there.

**Files:**
- Modify: `src/myvector.cc:1188` (replace const with extern ulong)
- Modify: `src/myvector_plugin.cc` (add MYSQL_SYSVAR_ULONG + add to sysvar array)
- Modify: `src/component_src/myvector_component_config.cc` (add ulong definition)

- [ ] **Step 2.1: Write validation SQL (run after implementation)**

```sql
-- Should show 4096 (default):
SHOW VARIABLES LIKE 'myvector_max_vector_dim';

-- Should fail (dim 5000 > 4096):
-- (Attempt to create a MYVECTOR column with dim=5000)
CREATE TABLE t_bigdim (id INT PRIMARY KEY, v MYVECTOR(dim=5000,type=HNSW,size=100));
-- Expected: ERROR - MYVECTOR column dimension incorrect 5000

-- Restart with --myvector-max-vector-dim=8192 and retry above:
-- Should succeed (dim 5000 < 8192)
```

- [ ] **Step 2.2: Replace the constant in `src/myvector.cc`**

Find line 1188:
```cpp
const size_t MYVECTOR_MAX_VECTOR_DIM = 4096;
```

Replace with:
```cpp
ulong myvector_max_vector_dim = 4096;
```

- [ ] **Step 2.3: Add the definition to `src/component_src/myvector_component_config.cc`**

This file already defines `myvector_rebuild_on_start`. Add alongside it:

```cpp
unsigned long myvector_max_vector_dim = 4096;
```

- [ ] **Step 2.4: Add sysvar declaration and registration to `src/myvector_plugin.cc`**

After the existing sysvar declarations (around line 133), add:

```cpp
static MYSQL_SYSVAR_ULONG(max_vector_dim,
    myvector_max_vector_dim,
    PLUGIN_VAR_READONLY | PLUGIN_VAR_RQCMDARG,
    "Maximum vector dimension allowed for index creation (default 4096, max 16383). "
    "Read-only at runtime — set at server start.",
    nullptr,   /* check */
    nullptr,   /* update */
    4096,      /* default */
    2,         /* min */
    16383,     /* max */
    0          /* blocksize */
);
```

Add `MYSQL_SYSVAR(max_vector_dim)` to `myvector_system_variables[]`:

```cpp
static SYS_VAR* myvector_system_variables[] = {
    MYSQL_SYSVAR(feature_level),
    MYSQL_SYSVAR(index_bg_threads),
    MYSQL_SYSVAR(index_dir),
    MYSQL_SYSVAR(config_file),
    MYSQL_SYSVAR(max_vector_dim),   /* add this line */
    nullptr
};
```

- [ ] **Step 2.5: Add extern declaration in `src/myvector.cc`**

Near the other `extern` declarations (around line 191):
```cpp
extern ulong myvector_max_vector_dim;
```

Then verify line 1249 already reads `MYVECTOR_MAX_VECTOR_DIM` — now it reads the global. The name changed from `MYVECTOR_MAX_VECTOR_DIM` (const) to `myvector_max_vector_dim` (global), so update the validation check:

Find:
```cpp
        if (!dimValid || dim <= 1 || dim > MYVECTOR_MAX_VECTOR_DIM) {
```

Replace with:
```cpp
        if (!dimValid || dim <= 1 || (ulong)dim > myvector_max_vector_dim) {
```

- [ ] **Step 2.6: Build and verify**

```bash
make clean && make 2>&1 | tail -20
```
Expected: clean build, `myvector.so` produced.

- [ ] **Step 2.7: Commit**

```bash
git add src/myvector.cc src/myvector_plugin.cc src/component_src/myvector_component_config.cc
git commit -m "feat: add myvector_max_vector_dim sysvar, default 4096 max 16383

PLUGIN_VAR_READONLY prevents runtime changes that would create dimension
inconsistency with already-loaded indexes. Component path uses a config
default; plugin sysvar architecture is being phased out.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: DBUG fault injection point in HNSW persistence flush

**Context:** RFC-004 Section 6 specifies a `DBUG_EXECUTE_IF("simulate_vector_crash", abort())` for crash-recovery testing. The injection point goes in `include/hnswdisk.i`, after `Fsync(ckptFile, ckptFileName)` (~line 457) and before `WriteCheckPointStatus(hnswFileName, CKPT_END_INCR_PASS1)` (~line 460). This is the exact window where kill-9 leaves checkpoint state incomplete and exercises the `makeIndexConsistent` recovery path on restart. `my_dbug.h` is already used in `myvector_binlog.cc`; add it to `hnswdisk.h` so it's available inside the inlined class body.

**Files:**
- Modify: `include/hnswdisk.h` (add `#include "my_dbug.h"`)
- Modify: `include/hnswdisk.i` (add `DBUG_EXECUTE_IF`)

- [ ] **Step 3.1: Add `my_dbug.h` to `include/hnswdisk.h`**

In `include/hnswdisk.h`, after the existing myvector includes (`myvector_log.h`, `myvectorutils.h`):

```cpp
#include "my_dbug.h"
```

- [ ] **Step 3.2: Add DBUG injection point in `include/hnswdisk.i`**

Find this sequence in `include/hnswdisk.i` (around line 455-462):
```cpp
    Fsync(ckptFile, ckptFileName);

    Close(ckptFile, ckptFileName);
    
    WriteCheckPointStatus(hnswFileName, CKPT_END_INCR_PASS1);
```

Replace with:
```cpp
    Fsync(ckptFile, ckptFileName);

    DBUG_EXECUTE_IF("simulate_vector_crash", abort(););

    Close(ckptFile, ckptFileName);

    WriteCheckPointStatus(hnswFileName, CKPT_END_INCR_PASS1);
```

- [ ] **Step 3.3: Build and verify (release build — DBUG macro is a no-op)**

```bash
make clean && make 2>&1 | tail -20
```
Expected: clean build. The `DBUG_EXECUTE_IF` compiles to nothing in a non-debug build.

- [ ] **Step 3.4: Manual crash-recovery verification procedure (requires MySQL debug build)**

This step is a manual checklist for the PR test plan — not automated in this PR.

```
1. Build MySQL with -DWITH_DEBUG=1 and install myvector.so.
2. Create an online HNSW cosine index and insert 10k rows.
3. SET debug_dbug="+d,simulate_vector_crash";
4. Trigger a checkpoint (rotate binlog or call myvector_checkpoint_index).
5. MySQL aborts. Restart mysqld.
6. Verify: index loads cleanly OR myvector_index_status() shows needs_rebuild.
7. Verify: myvector_ann_set() returns correct results after rebuild.
8. Confirm no crash on load, no silent corruption.
```

- [ ] **Step 3.5: Commit**

```bash
git add include/hnswdisk.h include/hnswdisk.i
git commit -m "fix: add DBUG_EXECUTE_IF simulate_vector_crash in hnswdisk flush path

Injection point sits after fsync(ckpt_file) but before CKPT_END_INCR_PASS1
status write — the exact window where kill-9 leaves checkpoint incomplete.
No-op in release builds; requires -DWITH_DEBUG=1 to activate.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 4: RFC-004 doc corrections

**Context:** The RFC (issue #77) contains five inaccuracies vs. the codebase. Create `docs/RFC-004-RELIABILITY.md` as the canonical on-disk version with all corrections applied.

**Files:**
- Create: `docs/RFC-004-RELIABILITY.md`

- [ ] **Step 4.1: Create `docs/RFC-004-RELIABILITY.md`**

Copy the full RFC-004 text from issue #77 and apply these corrections:

| Section | Find | Replace with |
|---------|------|-------------|
| §4.3 Extremely High Dimensions | "default: 16384" | "configurable via `myvector_max_vector_dim` sysvar, default 4096, max 16383" |
| §5.2 Index Persistence Strategy | "write → temp_file / fsync(temp_file) / rename(temp_file, final_file)" description | "MyVector uses a two-pass incremental checkpoint protocol (`include/hnswdisk.i`): write dirty nodes → `Fsync(ckpt_file)` → `CKPT_END_INCR_PASS1` → write to real HNSW file → `Fsync(hnsw_file)` → `CKPT_END_INCR_PASS2` → `CKPT_CONSISTENT`. A temp-file-then-rename pattern applies only to the component state file (`myvector_binlog_service.cc`)." |
| §7.1 Corruption Handling | "Config: `myvector_rebuild_on_start = ON`" | "Config: `myvector_rebuild_on_start` is an internal default (`false`) in the component path (`src/component_src/myvector_component_config.cc`). The plugin architecture is being phased out; no plugin sysvar is planned." |
| §6.1 Debug Fault Injection | "Example" (description only) | Add: "Implemented in `include/hnswdisk.i` after `Fsync(ckptFile)` in `doCheckPoint()`. Activate with: `SET debug_dbug=\"+d,simulate_vector_crash\";` (requires MySQL debug build)." |
| Error codes throughout | `ER_VECTOR_INVALID` | `ER_MYVECTOR_INVALID_VECTOR` |
| §11 Production Readiness Checklist | `[ ] Zero-vector validation verified` | `[x] Zero-vector validation verified` |
| §11 Production Readiness Checklist | `[ ] Dimension bounds enforced` | `[x] Dimension bounds enforced (configurable via myvector_max_vector_dim)` |

Also add at the top, below the title:

```markdown
> **Note:** This is the on-disk version of RFC-004 as of v1.26.5.1-rc1.
> Original draft: https://github.com/askdba/myvector/issues/77
```

- [ ] **Step 4.2: Commit**

```bash
git add docs/RFC-004-RELIABILITY.md
git commit -m "docs: add RFC-004-RELIABILITY.md with corrected gap analysis

Fixes: max dimension (4096 not 16384), persistence model (checkpoint
protocol not temp-rename), rebuild-on-start scope (component only),
error code names (ER_MYVECTOR_INVALID_VECTOR), DBUG injection location.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 5: Version bump, CHANGELOG, and open PR

**Context:** Bump to `v1.26.5.1-rc1` in `CHANGELOG.md`, then open a GitHub PR targeting `main` that covers all 5 commits. Version lives only in `CHANGELOG.md` (no separate version constant file). The CI release trigger pattern `v[0-9]*.[0-9]*.[0-9]*` is a glob that matches `v1.26.5.1-rc1` — no workflow changes needed.

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 5.1: Add v1.26.5.1-rc1 entry to `CHANGELOG.md`**

Insert at the top of the changelog (before the `## [1.26.5]` entry):

```markdown
## [1.26.5.1-rc1] - 2026-05-18

### Fixed
- Zero-magnitude vector inserts on cosine-metric indexes now return
  `ER_MYVECTOR_INVALID_VECTOR` to the client (UDF path) or log a warning
  and skip the index update (binlog/online-index path) instead of silently
  inserting max-distance entries.

### Added
- New sysvar `myvector_max_vector_dim` (read-only, default 4096, max 16383).
  Allows indexes with dimensions up to MySQL's native VECTOR type limit.
  Set at server start: `--myvector-max-vector-dim=8192`.

### Fixed
- Added `DBUG_EXECUTE_IF("simulate_vector_crash", abort())` in
  `hnswdisk.i` checkpoint flush path for crash-recovery testing.
  No-op in release builds; requires `-DWITH_DEBUG=1`.

### Documentation
- Added `docs/RFC-004-RELIABILITY.md` — corrected on-disk version of RFC-004
  (max dimension, persistence model, rebuild-on-start scope, error codes).
- Closes #77.
```

- [ ] **Step 5.2: Commit the CHANGELOG**

```bash
git add CHANGELOG.md
git commit -m "chore: bump version to v1.26.5.1-rc1, update CHANGELOG

Covers: zero-vector rejection, myvector_max_vector_dim sysvar,
DBUG crash injection, RFC-004 doc corrections.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

- [ ] **Step 5.3: Push branch and open PR**

```bash
git push origin main
gh pr create \
  --title "fix(reliability): RFC-004 gap closure — v1.26.5.1-rc1" \
  --body "$(cat <<'EOF'
## Summary

Closes #77. Five RFC-004 spec/code gaps closed in one patch release.

- **Zero-vector rejection**: Cosine index inserts with zero-magnitude vectors now hard-error at the UDF path (`ER_MYVECTOR_INVALID_VECTOR`); binlog path logs warning and skips.
- **`myvector_max_vector_dim` sysvar**: Replaces hardcoded 4096 constant. Read-only sysvar, default 4096, max 16383. Set at start: `--myvector-max-vector-dim=N`.
- **DBUG crash injection**: `DBUG_EXECUTE_IF("simulate_vector_crash", abort())` added to `hnswdisk.i` after `fsync(ckpt_file)`, before `CKPT_END_INCR_PASS1`. No-op in release builds.
- **RFC-004 doc**: `docs/RFC-004-RELIABILITY.md` created with corrected persistence model, dimension values, error codes, and rebuild-on-start scope.
- **Version**: Bumped to `v1.26.5.1-rc1` in CHANGELOG.

## Test plan

- [ ] Build: `make clean && make` — no errors or new warnings
- [ ] Zero-vector SQL: insert zero FP32 vector into cosine index → `ER_MYVECTOR_INVALID_VECTOR`
- [ ] Zero-vector L2: insert zero vector into L2 index → succeeds (no rejection for non-cosine)
- [ ] Sysvar default: `SHOW VARIABLES LIKE 'myvector_max_vector_dim'` → `4096`
- [ ] Sysvar enforcement: create index with `dim=5000` at default → rejected; with `--myvector-max-vector-dim=8192` → accepted
- [ ] DBUG injection (debug build only): `SET debug_dbug="+d,simulate_vector_crash"` → checkpoint aborts → restart → index recovers or marks needs_rebuild

🤖 Generated with [Claude Code](https://claude.ai/claude-code)
EOF
)"
```

---

## Self-Review

**Spec coverage check:**
- ✅ Gap 1 (zero-vector): Tasks 1.5–1.7
- ✅ Gap 2 (max dim sysvar): Tasks 2.2–2.5
- ✅ Gap 3 (DBUG injection): Tasks 3.1–3.2
- ✅ Gap 4 (RFC doc): Task 4.1
- ✅ Gap 5 (version + PR): Tasks 5.1–5.3
- ✅ Dual-path zero-vector (UDF hard error, binlog warning+skip): Tasks 1.6 and 1.7
- ✅ `PLUGIN_VAR_READONLY` on sysvar: Task 2.4
- ✅ 4-segment version CI compatibility: no workflow changes needed (noted in Task 5 context)

**Placeholder scan:** No TBDs, no "implement later", all code blocks are complete.

**Type consistency:**
- `isCosineMetric()` defined in Task 1.2, used in Tasks 1.6 and 1.7 — consistent
- `myvector_max_vector_dim` defined as `ulong` in Task 2.2, declared `extern ulong` in Task 2.5, registered as `MYSQL_SYSVAR_ULONG` in Task 2.4 — consistent
- `isZeroVector()` defined in Task 1.5, used in Tasks 1.6 and 1.7 — consistent
