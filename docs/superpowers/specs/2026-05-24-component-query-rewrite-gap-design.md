# Component Query-Rewrite Gap: Investigation & Fix Design

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Instrument the performance gap between plugin and component builds caused by the component's broken query-rewrite registration, then fix it.

**Architecture:** Two independent sub-problems: (1) add a `knn_ann` benchmark workload that uses `MYVECTOR_IS_ANN` syntax to quantify the gap, (2) wire the component's existing `QueryRewriterService` into MySQL's pre-parse hook and verify closure with the benchmark.

**Tech Stack:** Python (myvectorbench.py), C++ (myvector_component.cc, myvector_query_rewrite_service.cc), MySQL 8.4 / 9.7 component service API

---

## Background

### Observed gap

Benchmark results (10K rows, dim=128, synthetic, MySQL 8.4, Apple Silicon):

| Metric | Plugin | Component | Ratio |
|---|---|---|---|
| knn_qps | 22.4 | 9.3 | 2.4× |
| knn_p50_ms | 44 ms | 106 ms | 2.4× |
| knn_p99_ms | 71 ms | 133 ms | 1.9× |

The existing `knn_search` workload uses `ORDER BY myvector_distance(...) LIMIT 10` — a full table scan on both builds. The 2.4× gap there is a compiler difference (Clang for plugin vs GCC-12 for component).

### The more serious gap (this spec)

The plugin registers a `MYSQL_AUDIT_PARSE_PREPARSE` hook that rewrites:
- `WHERE MYVECTOR_IS_ANN('vec_col', 'id_col', query_vec)` → `JSON_TABLE(myvector_ann_set(...))` HNSW lookup (O(log N))
- `MYVECTOR_SEARCH[...]` → same HNSW lookup pattern

The component has `myvector_query_rewrite_service.cc` which defines a `QueryRewriterService` class but **`BEGIN_COMPONENT_PROVIDES` in `myvector_component.cc` is empty** — the service is never registered with MySQL's framework. Queries using `MYVECTOR_IS_ANN` or `MYVECTOR_SEARCH` syntax against the component either fail with "unknown function" or silently fall back to full table scans.

---

## Sub-problem 1: `knn_ann` benchmark workload

### What it does

`bench_knn_ann` runs after `bench_knn_search` against the same `bench.build_t` table (HNSW index already loaded by `bench_index_build`). It issues:

```sql
SELECT id, myvector_row_distance(id) AS dist
FROM bench.build_t
WHERE MYVECTOR_IS_ANN('vec', 'id', {binary_query_vec})
ORDER BY dist LIMIT 10;
```

With the plugin: audit hook rewrites this to an HNSW index lookup — O(log N) per query.

Without the component query rewrite: MySQL tries to call `MYVECTOR_IS_ANN` as a function, fails with SQL error.

### Graceful degradation

`bench_knn_ann` catches SQL errors:
- On error: emit `knn_ann_qps = 0.0`, `knn_ann_p50_ms = null`, `knn_ann_p99_ms = null`; print `⚠ MYVECTOR_IS_ANN not supported (query rewrite inactive)`
- On success: measure QPS, p50, p99 as normal

This makes the workload runnable on both builds — it documents the gap before the fix and enforces correctness after.

### New config fields

**`myvectorbench.yml`** — workload section:
```yaml
knn_ann_queries: 200
```

**`myvectorbench.yml`** — thresholds section:
```yaml
knn_ann_qps: -25%
knn_ann_p50_ms: +30%
knn_ann_p99_ms: +30%
```

### New JSON output fields

```json
{
  "knn_ann_qps": 45.2,
  "knn_ann_p50_ms": 21.3,
  "knn_ann_p99_ms": 38.1
}
```

`null` for p50/p99 when the query errored. The compare tool skips a metric if the baseline value is `null` or if `knn_ann_qps = 0.0` (the workload was broken when the baseline was recorded) — both cases produce an N/A row with no breach.

### Files touched

- `scripts/myvectorbench.py` — add `bench_knn_ann()`, wire into `run_workloads()`
- `myvectorbench.yml` — add `knn_ann_queries`, add thresholds
- `tests/test_myvectorbench_compare.py` — add tests for null/0 baseline handling

### Acceptance criterion

Running with `--build-path plugin` reports a nonzero `knn_ann_qps`. Running with `--build-path component` (before the fix) prints the warning and reports `knn_ann_qps = 0.0`. Both exit 0.

---

## Sub-problem 2: Fix component query-rewrite registration

### Investigation step (must precede code changes)

Before modifying `myvector_component.cc`, verify the following against MySQL 8.4 source headers in `mysql-server-mysql-8.4.8/`:

1. Confirm `include/mysql/components/services/query_rewrite.h` exists and defines the service interface that `QueryRewriterService` claims to implement
2. Confirm the virtual method signature `int rewrite_query(const Query_rewrite_request*, Query_rewrite_response*)` matches what MySQL's framework expects to call
3. Confirm that a component providing this service is called at `MYSQL_AUDIT_PARSE_PREPARSE` equivalent timing (pre-parse, before the query hits the optimizer)

If all three hold: wire `BEGIN_COMPONENT_PROVIDES` and test.

If the interface is wrong or the timing differs: fall back to the consumer-side pattern — `REQUIRES mysql_query_rewrite_inject` during `component_init`, registering a callback that calls `myvector_query_rewrite()` directly.

### Primary fix path (if interface is correct)

**`src/component_src/myvector_component.cc`** — add the service to `BEGIN_COMPONENT_PROVIDES`:

```cpp
BEGIN_COMPONENT_PROVIDES(myvector)
  PROVIDES_SERVICE(myvector, myvector_query_rewriter_service),
END_COMPONENT_PROVIDES();
```

Also confirm `myvector_component_init()` does not need to explicitly register the rewriter — providing the service should be sufficient for MySQL's framework to call it automatically.

### Fallback fix path (if `PROVIDES_SERVICE` is insufficient)

Use `REQUIRES_SERVICE(mysql_query_rewrite_inject)` in `BEGIN_COMPONENT_REQUIRES`, then in `myvector_component_init()`:

```cpp
mysql_service_mysql_query_rewrite_inject->add(
    &myvector_query_rewriter_callback);
```

and in `myvector_component_deinit()`:

```cpp
mysql_service_mysql_query_rewrite_inject->remove(
    &myvector_query_rewriter_callback);
```

where `myvector_query_rewriter_callback` wraps the existing `myvector_query_rewrite()`.

### Files touched

- `src/component_src/myvector_component.cc` — `BEGIN_COMPONENT_PROVIDES` and/or init/deinit
- `src/component_src/myvector_query_rewrite_service.cc` — adjust interface if fallback path chosen

### Acceptance criterion

After rebuilding the component and running `myvectorbench.py --build-path component`:
- `knn_ann_qps` is nonzero and within 25% of the plugin's `knn_ann_qps` on the same dataset/MySQL version
- All existing smoke tests pass (`scripts/pre-release-test.sh 8.4`)

---

## Sequencing

```
PR 1: knn_ann benchmark workload (Python only, no C++)
  → proves the gap on both 8.4 and 9.7
  → merged first, establishes baselines

PR 2: component query-rewrite fix (C++ only)
  → verified by knn_ann benchmark
  → merged second, baselines updated
```

Sub-problem 1 has no C++ dependency and can be implemented immediately. Sub-problem 2 requires inspecting MySQL source headers before writing code.
