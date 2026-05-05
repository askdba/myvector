# Binlog Fix + RC3 Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Diagnose and fix the multi-column binlog INSERT failure in `smoke-component.sh`, then cut and validate v1.26.5-rc3.

**Architecture:** Add targeted `fprintf(stderr,...)` trace points across the binlog event path, build via Docker, run smoke, identify the breaking step, apply a one-to-few-line fix, remove traces, then execute the RC3 release sequence.

**Tech Stack:** C++ (OracleLinux 9 Docker build), Bash smoke scripts, git tags, GitHub Actions CI.

---

## Files to modify

| File | Change |
|------|--------|
| `src/component_src/myvector_binlog_service.cc` | Add traces T1–T4, then fix, then remove traces |
| `src/myvector.cc` | Add traces T5–T6, then fix if needed, then remove traces |
| `release/RC2_STATUS_v1.26.5.md` | Close out RC2 with FAIL verdict |
| `release/RC3_STATUS_v1.26.5.md` | New file — RC3 status skeleton |

---

## Background

**`mc_test` table** (in `smoke-component.sh`):
```sql
CREATE TABLE mc_test (
    id   INT PRIMARY KEY,
    tag  VARCHAR(64),
    vec1 VARBINARY(256) COMMENT 'MYVECTOR COLUMN type=hnsw,dim=3,size=1000,m=16,ef=50,idcol=id,dist=L2,online=Y',
    vec2 VARBINARY(256) COMMENT 'MYVECTOR COLUMN type=hnsw,dim=3,size=1000,m=16,ef=50,idcol=id,dist=cosine,online=Y'
);
```

Ordinal positions from INFORMATION_SCHEMA: id=1, tag=2, vec1=3, vec2=4.

After `BUILD(vec1)` and `BUILD(vec2)`, `g_OnlineVectorIndexes["vectordb.mc_test"]` should contain
`[{vec1, idcolpos=1, veccolpos=3}, {vec2, idcolpos=1, veccolpos=4}]`.

`myvector_table_op` constructs `vecid = dbname + "." + tbname + "." + cname` to look up the index in `g_indexes`.

The type check in `VectorIndexCollection::open` is case-sensitive (`type=HNSW`); the column comment uses `type=hnsw` (lowercase), so all indexes fall back to `KNNIndex`. `KNNIndex::supportsIncrUpdates()` always returns `true`, so both columns are registered in `g_OnlineVectorIndexes` after BUILD.

---

## Task 1: Review and keep the existing uncommitted changes

**Files:**
- Read: `src/component_src/myvector_binlog_service.cc` (136 lines of lazy-discovery code from prior session)

The lazy-discovery additions (peek_table_name, discoverOnlineColumns, g_NonOnlineTables, the lazy-discovery block in the event loop) are a **no-op** for the current test path because `g_OnlineVectorIndexes` is already populated by `BuildMyVectorIndexSQL` before the INSERT arrives. They do no harm. Keep them — they improve robustness for tables created after component startup.

- [ ] **Step 1: Verify the existing changes compile cleanly**

Run:
```bash
cd /Users/askdba/Documents/GitHub/myvector
git diff --stat HEAD
```

Expected output:
```
 release/RC2_STATUS_v1.26.5.md                |   4 +-
 src/component_src/myvector_binlog_service.cc | 136 +++++++++++++++++++++++++++
 2 files changed, 138 insertions(+), 2 deletions(-)
```

No action needed if output matches. The changes remain staged for the eventual fix commit.

---

## Task 2: Add diagnostic trace points

**Files:**
- Modify: `src/component_src/myvector_binlog_service.cc` (lines ~1767–1791)
- Modify: `src/myvector.cc` (lines ~1660, ~2411, ~2419)

Add six `fprintf(stderr, "[DBG] ...")` trace points. These are temporary — removed in Task 5.

### Trace T1 — after parseTableMapEvent

In `src/component_src/myvector_binlog_service.cc`, after line 1768 (`parseTableMapEvent(event_buf, event_len, tev);`):

```cpp
                if (type == kTableMapEvent) {
                    parseTableMapEvent(event_buf, event_len, tev);
                    fprintf(stderr, "[DBG] T1 TABLE_MAP: %s.%s nCols=%u\n",
                            tev.dbName.c_str(), tev.tableName.c_str(), tev.nColumns);
```

### Trace T2 — WRITE_ROWS table lookup result

In `src/component_src/myvector_binlog_service.cc`, in the `kWriteRowsEvent` branch, immediately after the `g_OnlineVectorIndexes.find(key)` call:

```cpp
                } else if (type == kWriteRowsEvent) {
                    std::string key = tev.dbName + "." + tev.tableName;
                    auto kit = g_OnlineVectorIndexes.find(key);
                    fprintf(stderr, "[DBG] T2 WRITE_ROWS: key=%s found=%d\n",
                            key.c_str(), kit != g_OnlineVectorIndexes.end());
                    if (kit == g_OnlineVectorIndexes.end())
                        continue;
```

### Trace T3 — after parseRowsEvent per column

In `src/component_src/myvector_binlog_service.cc`, inside the `for (const auto& ci : kit->second)` loop, after the `parseRowsEvent(...)` call:

```cpp
                        parseRowsEvent(event_buf, event_len, tev,
                                       static_cast<unsigned int>(ci.idColumnPosition - 1),
                                       static_cast<unsigned int>(ci.vecColumnPosition - 1),
                                       ci.vectorColumn,
                                       binlog_file, binlog_pos, updates);
                        fprintf(stderr, "[DBG] T3 parseRows col=%s updates=%zu idpos=%d vecpos=%d\n",
                                ci.vectorColumn.c_str(), updates.size(),
                                ci.idColumnPosition, ci.vecColumnPosition);
```

### Trace T4 — worker thread before myvector_table_op

In `src/component_src/myvector_binlog_service.cc`, inside the worker lambda, before the `myvector_table_op` call (~line 1660):

```cpp
                    item = gqueue_.dequeue();
                    if (!item) break;
                    fprintf(stderr, "[DBG] T4 worker: %s.%s.%s pkid=%u vec_bytes=%zu\n",
                            item->dbName_.c_str(), item->tableName_.c_str(),
                            item->columnName_.c_str(), item->pkid_, item->vec_.size());
                    myvector_table_op(item->dbName_,
```

### Trace T5 — g_indexes.get result in myvector_table_op

In `src/myvector.cc`, after line 2411 (`AbstractVectorIndex* vi = g_indexes.get(vecid);`):

```cpp
    string vecid = dbname + "." + tbname + "." + cname;
    AbstractVectorIndex* vi = g_indexes.get(vecid);
    fprintf(stderr, "[DBG] T5 table_op vecid=%s vi=%p\n", vecid.c_str(), (void*)vi);
```

### Trace T6 — isAfter result before insertVector

In `src/myvector.cc`, replace the `if (isAfter(...))` block at ~line 2419 with an instrumented version:

```cpp
        vi->getLastUpdateCoordinates(binlogfileold, binlogposold);
        bool after = isAfter(binlogfile, binlogpos, binlogfileold, binlogposold);
        fprintf(stderr, "[DBG] T6 isAfter(%s,%zu > %s,%zu) = %d\n",
                binlogfile.c_str(), binlogpos,
                binlogfileold.c_str(), binlogposold, after);
        if (after) {
            vi->insertVector(vec.data(), vi->getDimension(), pkid);
        } else {
```

- [ ] **Step 2: Apply all six traces to the two files**

Edit `src/component_src/myvector_binlog_service.cc` to add T1, T2, T3, T4 at the locations above.
Edit `src/myvector.cc` to add T5, T6 at the locations above.

- [ ] **Step 3: Verify no syntax errors (dry check)**

Run:
```bash
grep -c "\[DBG\]" src/component_src/myvector_binlog_service.cc src/myvector.cc
```

Expected: at least 4 matches in the binlog file, at least 2 in myvector.cc.

---

## Task 3: Build and run the diagnostic

**Files:**
- Read-only: `scripts/build-component-8.4-docker.sh`

- [ ] **Step 1: Build the component for MySQL 8.4**

Run:
```bash
./scripts/build-component-8.4-docker.sh mysql-8.4.8
```

Expected: ends with something like:
```
[100%] Linking CXX shared library myvector_component.so
[100%] Built target myvector_component
```

If it fails, check the error — most likely a syntax error from the trace edits.

- [ ] **Step 2: Run the smoke test and capture output**

Run:
```bash
bash scripts/smoke-component.sh 8.4 2>&1 | tee /tmp/smoke-diag.txt
```

Let it run to completion (takes ~2–3 minutes including the 6-second sleep).

- [ ] **Step 3: Inspect the diagnostic output**

Run:
```bash
grep "\[DBG\]\|FAIL\|PASS\|rows" /tmp/smoke-diag.txt
```

**What to look for:**

| Scenario | Symptom | Root cause |
|----------|---------|------------|
| T1 shows `nCols=0` for mc_test | `parseTableMapEvent` returned early — mc_test not yet in `g_OnlineVectorIndexes` when TABLE_MAP_EVENT arrived | Timing: the binlog listener processed the INSERT's TABLE_MAP_EVENT before BUILD registered mc_test — **but tev is re-parsed each TABLE_MAP_EVENT**, so if the INSERT's TABLE_MAP_EVENT arrives after BUILD the tev will be correct |
| T2 shows `found=0` | mc_test NOT in `g_OnlineVectorIndexes` at WRITE_ROWS time | BUILD failed to populate the map |
| T3 shows `updates=0` | `parseRowsEvent` extracted no data | Column offset or type parsing bug |
| T4 never printed | Updates not enqueued, or worker not running | Queue or worker issue |
| T5 shows `vi=0x0` | `g_indexes.get("vectordb.mc_test.vec1")` returned null | Index not in `g_indexes` after BUILD |
| T6 shows `isAfter=0` | Binlog coordinates check failed | Coordinate comparison bug |

---

## Task 4: Apply the fix

Based on the diagnostic output from Task 3, apply ONE of the following fixes. Only apply the fix matching the observed symptom.

**Files:**
- Modify: `src/component_src/myvector_binlog_service.cc` (if fix is A or B)
- Modify: `src/myvector.cc` (if fix is C)

### Fix A — T2 `found=0`: BUILD doesn't register mc_test (supportsIncrUpdates gate)

`BuildMyVectorIndexSQL` only adds to `g_OnlineVectorIndexes` if `vi->supportsIncrUpdates()` is true. For KNN (which `type=hnsw` lowercase falls back to), `supportsIncrUpdates()` returns `true` unconditionally. So this path should not be the issue — but if T2 shows `found=0`, add a stderr print inside `BuildMyVectorIndexSQL` at line ~1324 to confirm whether `supportsIncr` is true.

In `src/component_src/myvector_binlog_service.cc` around line 1324:
```cpp
    supportsIncr = vi->supportsIncrUpdates();
    fprintf(stderr, "[DBG] BuildSQL supportsIncr=%d for %s.%s.%s\n",
            supportsIncr, db, table, veccol);
```

Re-run smoke. If `supportsIncr=0`, the `type=hnsw` (lowercase) check is not falling back to KNN as expected. Fix: change the `g_OnlineVectorIndexes` registration in `BuildMyVectorIndexSQL` to not gate on `supportsIncrUpdates()`:

```cpp
    // Always register the column for online binlog updates when online=Y.
    {
        std::lock_guard<std::mutex> binlogMutex(binlog_stream_mutex_);
        ...
        auto& cols = g_OnlineVectorIndexes[key];
        auto it = std::find_if(cols.begin(), cols.end(),
            [&](const VectorIndexColumnInfo& c) {
                return c.vectorColumn == vc.vectorColumn;
            });
        if (it != cols.end())
            *it = vc;
        else
            cols.push_back(vc);
    }
```

(Remove the `if (supportsIncr)` guard around the `g_OnlineVectorIndexes` update at lines 1342–1353.)

### Fix B — T3 `updates=0`: parseRowsEvent extracts no data

The `tag VARCHAR(64)` column has `metadata = max_byte_len`. In MySQL 8.x with default utf8mb4 charset, `VARCHAR(64)` has `max_byte_len = 256` (64 chars × 4 bytes). With `metadata = 256 >= 256`, `parseRowsEvent` uses a 2-byte length prefix for tag. If the actual row-format stores tag with a 1-byte prefix (because the column uses latin1 or utf8mb3 inside), the parser would read wrong bytes and all subsequent column offsets would be off, causing the vector column data to be misidentified.

Fix in `src/component_src/myvector_binlog_service.cc` in `parseRowsEvent`, `MYSQL_TYPE_VARCHAR` case: add a trace to log `tev.columnMetadata[i]` and `clen` for each column:

```cpp
case MYSQL_TYPE_VARCHAR: {
    unsigned int clen = 0;
    if (tev.columnMetadata[i] < 256) {
        ...
        clen = (unsigned int)event_buf[index]; index++; remaining -= 1;
    } else {
        ...
        memcpy(&clen, &event_buf[index], 2); index += 2; remaining -= 2;
    }
    fprintf(stderr, "[DBG] VARCHAR col=%u meta=%u clen=%u\n",
            i, tev.columnMetadata[i], clen);
```

If the trace shows `clen` blowing up (e.g., 0x6400 = 25600 for tag when it should be 1), the metadata is being misread. The real fix in that case is to ensure the metadata comparison threshold is correct for the actual column encoding in use.

### Fix C — T5 `vi=0x0`: g_indexes lookup fails

The `vecid` key in `myvector_table_op` is constructed as:
```cpp
string vecid = dbname + "." + tbname + "." + cname;
```

The index was opened with key `"vectordb.mc_test.vec1"`. If `dbname`, `tbname`, or `cname` differ from what was passed to `g_indexes.open()`, the lookup fails.

Add a trace to `BuildMyVectorIndexSQL` at line 1144 to print the key used:
```cpp
snprintf(vecid, sizeof(vecid), "%s.%s.%s", dbname, tbl, col);
fprintf(stderr, "[DBG] BUILD open key='%s'\n", vecid);
myvector_open_index_impl(vecid, info, empty, action, empty, empty);
```

If the key printed differs from what T5 logs as `vecid=`, fix the construction in `myvector_table_op` to match.

- [ ] **Step 1: Apply the fix matching the T2/T3/T5 symptom observed in Task 3**
- [ ] **Step 2: Re-build**

```bash
./scripts/build-component-8.4-docker.sh mysql-8.4.8
```

- [ ] **Step 3: Re-run smoke for 8.4 — verify the mc_test INSERT updates both indexes**

```bash
bash scripts/smoke-component.sh 8.4 2>&1 | tee /tmp/smoke-fixed.txt
grep "\[DBG\]\|FAIL\|PASS\|rows" /tmp/smoke-fixed.txt
```

Expected in output:
```
Index row counts after INSERT: vec1=4 vec2=4
```

If FAIL persists, re-read the new trace output and iterate.

---

## Task 5: Remove traces and verify clean

**Files:**
- Modify: `src/component_src/myvector_binlog_service.cc` (remove all `[DBG]` fprintf lines)
- Modify: `src/myvector.cc` (remove T5, T6 fprintf lines)

- [ ] **Step 1: Remove all diagnostic traces**

Remove every line added in Tasks 2 and 4 that contains `[DBG]`. Verify:

```bash
grep "\[DBG\]" src/component_src/myvector_binlog_service.cc src/myvector.cc
```

Expected: no output.

- [ ] **Step 2: Build clean for 8.4**

```bash
./scripts/build-component-8.4-docker.sh mysql-8.4.8
```

- [ ] **Step 3: Build clean for 9.7**

```bash
./scripts/build-component-9.7-docker.sh mysql-9.7.0
```

- [ ] **Step 4: Run smoke for 8.4 — must pass the mc_test section**

```bash
bash scripts/smoke-component.sh 8.4 2>&1 | tee /tmp/smoke-8.4-clean.txt
grep -E "FAIL|PASS|row count" /tmp/smoke-8.4-clean.txt
```

Expected in output:
```
Index row counts after INSERT: vec1=4 vec2=4
```

- [ ] **Step 5: Run smoke for 9.7 — must pass the mc_test section**

```bash
bash scripts/smoke-component.sh 9.7 2>&1 | tee /tmp/smoke-9.7-clean.txt
grep -E "FAIL|PASS|row count" /tmp/smoke-9.7-clean.txt
```

Expected in output:
```
Index row counts after INSERT: vec1=4 vec2=4
```

- [ ] **Step 6: Commit the fix**

```bash
git add src/component_src/myvector_binlog_service.cc src/myvector.cc
git commit -m "fix(component): fix binlog WRITE_ROWS handler for multi-column tables"
```

---

## Task 6: Close out RC2 status and create RC3 skeleton

**Files:**
- Modify: `release/RC2_STATUS_v1.26.5.md`
- Create: `release/RC3_STATUS_v1.26.5.md`

- [ ] **Step 1: Update RC2 status — Section 6 (component smoke results)**

Edit `release/RC2_STATUS_v1.26.5.md`. Replace the TBD rows in Section 6:

```markdown
## 6) Component smoke results

Run 2026-05-05 against RC2 images (pre-fix).

| Tag | Result |
| :-- | :-- |
| `mysql8.4` | **FAIL** — binlog INSERT not reflected in multi-column mc_test (vec1=3, expected 4) |
| `mysql9.7` | **FAIL** — same root cause |
```

- [ ] **Step 2: Update RC2 status — Section 7 (Go/No-Go)**

Edit `release/RC2_STATUS_v1.26.5.md`. Replace Section 7:

```markdown
## 7) Go/No-Go

- Decision: **No-Go** — binlog WRITE_ROWS handler does not update multi-column mc_test indexes.
- Blocker: Fixed in commit `<sha-of-the-fix-commit>` on `main`. Proceeding to RC3.
```

(Replace `<sha-of-the-fix-commit>` with the actual short SHA from Task 5 Step 6.)

- [ ] **Step 3: Create RC3 status skeleton**

Create `release/RC3_STATUS_v1.26.5.md`:

```markdown
# RC3 Status - v1.26.5

## 1) Release metadata

- Release version: `v1.26.5`
- Candidate tag: `v1.26.5-rc3`
- Previous candidate: `v1.26.5-rc2`
- Release manager: `askdba`
- Date opened: `2026-05-05`
- Last updated: `2026-05-05`

## 2) Candidate commit and branch

- Working branch: `main`
- **RC3 validation baseline commit:** `<sha-of-the-fix-commit>`
- Scope range: `v1.26.5-rc2..HEAD` on `main`.

## 3) RC2 → RC3 delta

| Commit | Description |
| :----- | :---------- |
| `<sha>` | fix(component): fix binlog WRITE_ROWS handler for multi-column tables |

**Root cause of RC2 component smoke failure:** [Fill in after Task 4 diagnosis]

## 4) Validation status

### Build and CI

- CI status: pending.
- Release workflow (`release.yml`): pending.
- Docker publish (`docker-publish.yml`): pending.
- Lint status: Green on main.

### Functional checks

- Plugin smoke (`smoke-published-images.sh`): pending.
- Component smoke (`smoke-component.sh`): PASS (local build, Tasks 3–5 above).
- Online index flow: optional for RC3.
- Regression: CI coverage pre-tag is green.

### Performance smoke

- Startup sanity: pending.
- Query latency: pending (README smoke).
- Memory: not measured for RC3.

## 5) GHCR images smoke-tested

Fill in after `docker-publish.yml` completes.

| Tag | Image digest (pulled) |
| :-- | :-- |
| `mysql8.0` | TBD |
| `mysql8.4` | TBD |
| `mysql9.7` | TBD |

## 6) Component smoke results (published images)

Fill in after `./scripts/smoke-component.sh` runs against RC3 GHCR images.

| Tag | Result |
| :-- | :-- |
| `mysql8.4` | TBD |
| `mysql9.7` | TBD |

## 7) Go/No-Go

- Decision: Pending smoke results.
- Blockers: None identified at RC3 cut time.
```

- [ ] **Step 4: Commit the release docs**

```bash
git add release/RC2_STATUS_v1.26.5.md release/RC3_STATUS_v1.26.5.md
git commit -m "docs(release): close RC2 as No-Go, add RC3 status skeleton for v1.26.5"
```

---

## Task 7: Tag RC3 and validate published images

- [ ] **Step 1: Tag RC3**

```bash
git tag v1.26.5-rc3
git push origin main v1.26.5-rc3
```

Expected: GitHub Actions triggers `release.yml` and (after it completes) `docker-publish.yml`.

- [ ] **Step 2: Monitor CI**

Watch the Actions tab for:
- `release.yml` → must succeed (creates GitHub release, attaches artifacts)
- `docker-publish.yml` → must succeed for all 3 matrix jobs (mysql8.0, mysql8.4, mysql9.7)

Both runs should show green within ~25 minutes.

- [ ] **Step 3: Pull images and smoke-test**

```bash
./scripts/smoke-published-images.sh
```

Expected output ends with:
```
=== All smokes completed OK ===
```

Record the image digests:

```bash
docker inspect --format='{{index .RepoDigests 0}}' ghcr.io/askdba/myvector:mysql8.0
docker inspect --format='{{index .RepoDigests 0}}' ghcr.io/askdba/myvector:mysql8.4
docker inspect --format='{{index .RepoDigests 0}}' ghcr.io/askdba/myvector:mysql9.7
```

- [ ] **Step 4: Run component smoke against published RC3 images**

```bash
bash scripts/smoke-component.sh 8.4 2>&1 | grep -E "FAIL|PASS|row count"
bash scripts/smoke-component.sh 9.7 2>&1 | grep -E "FAIL|PASS|row count"
```

Expected for both: `Index row counts after INSERT: vec1=4 vec2=4`

- [ ] **Step 5: Fill in RC3 status doc**

Edit `release/RC3_STATUS_v1.26.5.md`:
- Section 4: update CI run IDs and statuses to green/Success
- Section 5: fill in GHCR image digests from Step 3
- Section 6: fill in component smoke results from Step 4
- Section 7: set Decision to **Go** if all checks pass

- [ ] **Step 6: Commit and push final RC3 status**

```bash
git add release/RC3_STATUS_v1.26.5.md
git commit -m "docs(release): record RC3 CI results, image digests, and Go/No-Go for v1.26.5"
git push origin main
```
