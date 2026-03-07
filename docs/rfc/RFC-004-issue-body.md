# RFC-004: Reliability, Concurrency, and Crash Consistency Model for MyVector Plugin

**Author:** MyVector Engineering  
**Status:** Draft  
**Target Version:** 8.0.41+  
**Last Updated:** 2026-02-18  

---

## 1. Abstract

This document defines the concurrency model, edge-case handling strategy, and crash recovery guarantees of the MyVector plugin for MySQL.

The goal is to formally describe:

- Thread-safety mechanisms in the HNSWlib wrapper
- Data validation rules for vector inputs
- Crash-consistency model for persisted .bin index files
- Required stress and failure test scenarios
- Recovery semantics on mysqld restart

This RFC establishes MyVector's reliability model and operational guarantees.

This document is **prescriptive** (target behavior); implementation status may lag and is tracked in the "Implementation Status" section.

---

## 2. Design Principles

MyVector follows these core principles:

1. **Table Data is the Source of Truth**
2. **Vector indexes are secondary, rebuildable structures**
3. **All mutations must respect MySQL transactional semantics**
4. **Index persistence must be atomic** (write temp → fsync → rename)
5. **Corruption must be detectable and recoverable**

---

## 3. Concurrency Model

### 3.1 Scope

Concurrency concerns apply to:

- HNSW graph insertions
- Concurrent KNN searches
- Index rebuilds
- Index persistence
- DROP INDEX / ALTER TABLE operations

### 3.2 Threading Context

MyVector executes within the MySQL server process:

- Each connection runs in a separate THD context
- Memory allocations are THD-bound
- No shared global mutable state is allowed without locking

### 3.3 Locking Strategy

Each vector index maintains:

- One read-write lock (RWLock)
- One exclusive persistence mutex

| Operation           | Lock Type           |
|---------------------|---------------------|
| KNN Search          | Shared (read)       |
| INSERT (vector update) | Exclusive (write) |
| Batch Load          | Exclusive           |
| Index Persistence   | Exclusive           |
| Index Rebuild       | Exclusive           |

**Lock Guarantees**

- Multiple concurrent readers allowed
- Writers are serialized
- Search operations never mutate graph state
- No lock-free mutation paths exist

### 3.4 Concurrency Stress Testing Requirements

Minimum stress scenarios:

| Scenario                  | Threads | Expected Behavior                    |
|---------------------------|--------|-------------------------------------|
| Pure SELECT KNN           | 200    | No deadlocks, stable latency        |
| 100 INSERT + 100 SELECT   | 200    | No corruption, no starvation       |
| Burst INSERT (10k rows)   | 64     | No graph resize race                |
| DROP INDEX during INSERT  | 50     | Clean teardown, no crash            |

Recommended tooling:

- sysbench custom Lua script
- MySQL debug build (`-DWITH_DEBUG=1`)
- ThreadSanitizer (TSAN)
- Valgrind Helgrind

---

## 4. Edge Case Data Handling

### 4.1 Zero Vectors

**For cosine similarity:**

`norm(vector) == 0 → reject`

Behavior:

- Return ER_MYVECTOR_INVALID_VECTOR (or ER_VECTOR_INVALID when aligned)
- Do not insert into index
- **Strict mode (default):** ER_VECTOR_INVALID causes the INSERT statement to fail (abort the statement/transaction); the row is not inserted.
- **Non-strict mode:** Emits a warning, inserts the row into the table, but omits it from the vector index.
- "Transaction continues normally" means: in non-strict mode the transaction commits; in strict mode the statement is rolled back.

**For L2 distance:**

- Zero vector permitted
- No normalization performed

### 4.2 Dimension Mismatch

At index creation: `CREATE VECTOR INDEX ... DIM = N`

On INSERT: `vector_length == N * sizeof(float)`

If mismatch:

- Reject row
- Do not modify index

### 4.3 Extremely High Dimensions

Constraints:

- Configurable maximum dimension (default: 4096)
- Pre-allocation memory validation
- Reject index creation exceeding server memory limits

Memory complexity: `O(N × M × dim)` where N = number of nodes, M = graph connectivity, dim = vector dimension.

### 4.4 Malformed Binary Data

Validation steps before deserialization:

1. Length check
2. Alignment verification
3. Float boundary safety
4. Optional checksum (if enabled)

Unsafe casts are prohibited. Failure: reject INSERT, log warning, no index mutation.

---

## 5. Crash Consistency Model

### 5.1 Write Ordering

Index update occurs only after transaction commit.

Two supported models:

- **Model A (Post-Commit Hook):** INSERT row → InnoDB commit durable → Post-commit hook triggers HNSW insert
- **Model B (Deferred Batch Sync):** INSERT row → Commit → Buffered index update → Periodic checkpoint flush

**Implemented model:** Model B (Deferred Batch Sync). "Buffered" means buffered committed rows only. A commit-aware binlog listener (or transaction-visible flush boundary) filters out uncommitted transactions. The checkpoint/flush policy guarantees eventual index convergence while maintaining the invariant below.

Under no condition may an uncommitted row enter the index.

#### 5.1.1 Model B (Deferred Batch Sync) Consistency Implications

**Expected query staleness:** A recently committed INSERT may not appear in KNN results until the next checkpoint. Staleness is bounded by the checkpoint interval or max delay (e.g., seconds to minutes depending on configuration).

**User-visible behavior:** A user who commits an INSERT and immediately runs a KNN query may not see their newly inserted vector in the results until the next checkpoint flush. This is eventual consistency for the index with respect to committed table data.

**Operational guidance:** Applications should expect eventual consistency for Model B. This is acceptable for use cases such as recommendation systems, semantic search, and analytics where near-real-time (within checkpoint interval) visibility is sufficient. For read-your-writes semantics, operators should either enable sync-on-commit (if available) or tune checkpoint frequency to reduce the visibility window.

**Mechanisms to force synchronization:** (1) Explicit flush/checkpoint API—if exposed—triggers immediate index update; (2) sync-on-commit config—when enabled, each commit triggers index update before returning; (3) manual rebuild—`myvector("build")` or `ALTER TABLE ... ADD VECTOR INDEX` after `DROP VECTOR INDEX` forces full rebuild from table data. Operators should note this in the Production Readiness Checklist: tune checkpoint frequency or enable sync-on-commit when read-your-writes is required.

### 5.2 Index Persistence Strategy

When saving .bin index files, the target behavior is:

```
write → temp_file
fsync(temp_file)
fsync(directory)
rename(temp_file, final_file)
```

Properties: POSIX rename is atomic; either old or new index survives; partial writes are never active. **Implementation:** Component state file and main HNSW index (.bin) in `hnswdisk.i` both use write-to-temp, fsync, fsync-directory, then atomic rename.

### 5.3 Crash During Update

If mysqld is terminated during HNSW insertion, graph resize, or file flush:

On restart:

1. Load .bin
2. Validate header
3. Verify dimension match
4. Validate graph node count
5. Compare metadata row count

If validation fails: mark index invalid, trigger rebuild.

---

## 6. Crash Simulation Testing

### 6.1 Debug Fault Injection (Planned)

When implemented, MySQL DBUG can be used: `DBUG_EXECUTE_IF("simulate_vector_crash", abort(););` with `SET debug_dbug="+d,simulate_vector_crash";`. Test procedure: Insert large batch → Trigger injected crash → Restart mysqld → Verify no corrupted index load, search consistency, index count == committed row count.

**Current status:** DBUG fault injection is not yet implemented. Use the external-kill procedure (§6.2) and deterministic integration tests that validate recovery behavior (e.g., batch insert → kill → restart → verify index load or rebuild) until DBUG hooks are added.

### 6.2 External Kill Testing

During batch insert: `kill -9 <mysqld_pid>`. Restart and verify index loads cleanly OR automatic rebuild triggered. This is the primary crash simulation method until DBUG fault injection is implemented.

---

## 7. Recovery Model

### 7.1 Corruption Handling

If .bin invalid:

- Index marked unusable
- Server continues running
- Automatic rebuild allowed if configured

Config: `myvector_rebuild_on_start = ON`

Manual recovery: `ALTER TABLE t DROP VECTOR INDEX idx;` then `ALTER TABLE t ADD VECTOR INDEX idx(...);`

---

## 8. Invariants

The following invariants must always hold:

1. No uncommitted row exists in index; committed rows may be temporarily absent from index until next checkpoint (Model B only)
2. No corrupted .bin file is loaded silently
3. Index can always be rebuilt from table data
4. Concurrency does not produce graph corruption
5. Persistence is atomic

---

## 9. Limitations

- HNSWlib itself does not provide WAL
- Recovery relies on rebuildability
- Memory footprint grows with graph size
- Insert-heavy workloads may require batching

---

## 10. Security Considerations

- Strict binary validation prevents memory corruption
- No unbounded memory allocations
- No unsafe pointer arithmetic
- Index files validated before load

---

## 11. Production Readiness Checklist

This checklist reflects target state for GA; some items are not yet implemented (see Planned/Blockers below).

**Implemented / Verified**

- [ ] 200-thread stress test stable for 24h
- [ ] Kill -9 recovery test passes
- [ ] TSAN/Helgrind clean
- [ ] Dimension bounds enforced

**Planned / Blockers**

- [ ] Crash injection test passes — *DBUG fault injection not implemented; use external kill or deterministic integration tests until DBUG hooks are added*
- [ ] Zero-vector validation verified — *Zero-vector rejection for cosine missing; implement per RFC §4.1*
- [ ] Corrupted file detection confirmed — *myvector_rebuild_on_start wiring to rebuild on load failure: TBD*

---

## 12. Conclusion

MyVector's reliability model is based on a critical architectural decision: **the vector index is a secondary, derived, and rebuildable structure.**

- Durability is guaranteed by MySQL's transactional engine.
- Integrity is guaranteed through strict validation and atomic persistence.
- Recoverability is guaranteed via deterministic rebuild.

This model aligns with MySQL's secondary index philosophy and enables production-safe ANN inside the database engine.

---

## Implementation Status / Open Work Items

| Item | Current Status | Proposed Action | Issue/Ticket | Priority |
|------|----------------|-----------------|--------------|----------|
| Zero-vector rejection for cosine (ER_MYVECTOR_INVALID_VECTOR, computeCosineDistance) | Missing | Reject norm==0, return error; implement strict/non-strict mode | TBD | Blocker |
| Temp-file-then-rename for main .bin (hnswdisk.i saveIndex) | Implemented | Main HNSW index now uses write→temp→fsync→rename; .links and .links.data also use atomic rename | — | — |
| DBUG fault injection (DBUG_EXECUTE_IF, simulate_vector_crash) | Not implemented | Add DBUG_EXECUTE_IF for crash simulation tests | TBD | Blocker |
| myvector_rebuild_on_start sysvar | Implemented | Boolean sysvar (default OFF) in plugin; static var in component. Rebuild-on-fail wiring when load fails: TBD | — | Critical |

---

## Context verification (vs. current codebase)

Verification of RFC claims against the repository (as of 2026-02-18):

| RFC claim | Codebase status |
|-----------|-----------------|
| **Concurrency: one RWLock per index** | **Verified.** `AbstractVectorIndex` uses `std::shared_mutex`; `KNNIndex`/`HNSWMemoryIndex` use `search_insert_mutex_` with shared lock for search, exclusive for insert (`src/myvector.cc`, `include/myvector.h`). |
| **KNN Search = shared lock, INSERT = exclusive** | **Verified.** `searchVectorNN` uses `std::shared_lock`, `insertVector` uses `std::unique_lock` on `search_insert_mutex_`. |
| **HNSW internal locking** | **Verified.** `hnswalg.h` / `hnswdisk.h` use per-node/label mutexes; `hnswdisk.i` uses partitioned flush-list mutexes. |
| **Error code for invalid vector** | **Partial.** RFC says "ER_VECTOR_INVALID"; code uses `ER_MYVECTOR_INVALID_VECTOR` ("Invalid vector format or checksum mismatch") in `include/myvector_errors.h`. Consider aligning names. |
| **Zero vector rejection for cosine** | **Gap.** `computeCosineDistance` avoids division by zero (`if (t) dist = ...`) but does not reject insert; RFC requires explicit reject and ER_VECTOR_INVALID for `norm == 0`. |
| **Max dimension default 4096** | **Verified.** RFC default aligned with code: `MYVECTOR_MAX_VECTOR_DIM = 4096` in `src/myvector.cc`. |
| **Atomic persistence (write temp → fsync → rename)** | **Verified.** Component state file uses `std::rename(tmp_path, path)` after write (`myvector_binlog_service.cc`). Main HNSW index save in `hnswdisk.i` now uses write-to-temp, fsync, fsync-directory, then atomic rename for main .bin, .links, and .links.data. |
| **Crash recovery / checkpoint** | **Verified.** `hnswdisk.i` has `doCheckPoint`, `WriteCheckPointStatus`, `makeIndexConsistent`, and rollback on interrupted flush; corrupted/unsupported index throws in load. |
| **DBUG fault injection** | **Not implemented.** No `DBUG_EXECUTE_IF` or `simulate_vector_crash` in repo; RFC describes desired testing approach. |
| **myvector_rebuild_on_start** | **Implemented.** Boolean sysvar in `myvector_plugin.cc` (plugin); static var in `myvector_component_config.cc` (component). Default OFF. Wiring to rebuild on load failure: TBD. |

**Summary:** Core concurrency and recovery behavior matches the RFC. Atomic persistence for main HNSW index and `myvector_rebuild_on_start` sysvar are implemented. Gaps: explicit zero-vector rejection for cosine, DBUG fault injection, and myvector_rebuild_on_start wiring to rebuild on load failure can be tracked as follow-ups.
