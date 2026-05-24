# myvectorbench Design

**Date:** 2026-05-24
**Issue:** #85
**Target:** v1.27.0

---

## Goal

A repeatable, release-gated benchmarking pipeline for MyVector that captures comparable numbers on each release, stores them on a `benchmarks/` branch, and flags performance regressions before they ship.

---

## Scope

- Plugin and component build paths, MySQL 8.4 and 9.7.
- Synthetic dataset by default; configurable to GloVe 50d, GloVe 300d, or a custom TSV.
- Four workloads: index build, insert throughput, KNN search, recall.
- Per-metric configurable thresholds; conservative defaults tolerate GitHub Actions runner noise.
- Result history on a `benchmarks/` branch; baseline promoted manually.
- All delivered in a single PR.

### Not in scope (v1)

- Dimension scaling sweep.
- Distance metric breakdown (L2 vs Cosine vs IP).
- Nightly runs on `main`.
- Self-hosted runner pinning.

---

## Architecture

Three components:

| File | Role |
|---|---|
| `scripts/myvectorbench.py` | Runner: Docker orchestration, workload execution, JSON output |
| `scripts/myvectorbench-compare.py` | Comparator: reads baseline + current JSON, checks thresholds, prints delta table |
| `myvectorbench.yml` | Config: workload params, matrix, thresholds |
| `.github/workflows/myvectorbench.yml` | CI: trigger, artifact upload, benchmarks-branch commit, job summary |

---

## Config file (`myvectorbench.yml`)

```yaml
matrix:
  mysql_versions: [8.4, 9.7]
  build_paths: [plugin, component]
  # A cell is skipped (not failed) if its build artifact is absent.
  # e.g. plugin path is only built for 8.4 in CI; 9.7 plugin cell is skipped.

workload:
  dataset: synthetic        # synthetic | glove50 | glove300 | /path/to/custom.tsv
  rows: 10000
  dim: 128
  M: 16
  ef_construction: 200
  ef_search: 50
  knn_queries: 200

thresholds:
  index_build_time_s: +25%
  insert_qps: -25%
  knn_qps: -25%
  knn_p99_ms: +30%
  recall_at_10: -0.05        # absolute delta, not percent
```

---

## Workload suite

All four workloads run sequentially on a single Docker container per matrix cell.

| Workload | What it measures | Metrics emitted |
|---|---|---|
| `index_build` | `MYVECTOR_INDEX_BUILD` wall time on N rows | `index_build_time_s` |
| `insert_throughput` | Bulk INSERT rate into online=Y indexed table | `insert_qps` |
| `knn_search` | 200 KNN queries timed | `knn_qps`, `knn_p50_ms`, `knn_p99_ms` |
| `recall` | HNSW ANN vs brute-force KNN overlap | `recall_at_10` |

Expected runtime on GitHub Actions (synthetic, 10k rows, 128d): **3–5 min per matrix cell.**

---

## JSON result schema

One file per matrix cell per run:

```json
{
  "git_ref": "v1.26.5.1",
  "mysql_version": "8.4",
  "build_path": "component",
  "timestamp": "2026-05-24T08:30:00Z",
  "runner": "github-ubuntu-22.04",
  "dataset": "synthetic",
  "workload_params": {
    "rows": 10000,
    "dim": 128,
    "M": 16,
    "ef_construction": 200,
    "ef_search": 50,
    "knn_queries": 200
  },
  "metrics": {
    "index_build_time_s": 12.3,
    "insert_qps": 440,
    "knn_qps": 810,
    "knn_p50_ms": 1.2,
    "knn_p99_ms": 3.1,
    "recall_at_10": 0.942
  }
}
```

---

## Result storage (`benchmarks/` branch)

```
benchmarks/
  8.4/
    plugin/
      v1.26.5.1-20260524T083000.json
      latest.json      ← copy of most recent run
      baseline.json    ← manually promoted
    component/
      ...
  9.7/
    component/
      ...
  README.md            ← explains branch layout and baseline promotion
```

**Baseline promotion** — manual, run after a release passes the pre-release gate:

```bash
./scripts/myvectorbench.py --promote <git-ref>
```

Copies that ref's JSON files to `baseline.json` in each matrix cell directory on the `benchmarks/` branch. If no baseline exists for a cell, comparison is skipped and results are uploaded with a `NO_BASELINE` annotation.

---

## Regression detection

`myvectorbench-compare.py baseline.json current.json --config myvectorbench.yml`

- Reads thresholds from config.
- Percent thresholds (e.g., `-25%`) apply to QPS/throughput metrics; absolute thresholds (e.g., `-0.05`) apply to recall.
- Prints a markdown delta table to stdout.
- Exits 1 if any metric exceeds its threshold; exits 0 otherwise.
- A missing baseline produces exit 0 with a warning (never blocks first run).

---

## CI workflow

**Trigger:** `v*` tags and `workflow_dispatch` (with optional `mysql_version` and `build_path` inputs).

**Job graph:**

```
build-artifacts (matrix: mysql 8.4, 9.7)
  └── benchmark (matrix: version × build_path)
        1. Start MySQL container, install plugin or component
        2. Run myvectorbench.py → result.json
        3. Upload result.json as workflow artifact
        4. Checkout benchmarks/ branch
        5. Run myvectorbench-compare.py (exit 1 on threshold breach)
        6. Commit result.json + update latest.json to benchmarks/ branch
        7. Post GitHub job summary (markdown delta table)
```

Threshold breach fails the `benchmark` job for that matrix cell. It does **not** block the release workflow — the two workflows are independent.

**Job summary example:**

```
## myvectorbench — v1.26.5.1 · mysql:8.4 · component

| Metric           | Baseline | Current | Delta   | Status |
|------------------|----------|---------|---------|--------|
| index_build_time_s | 12.1   | 12.8    | +5.8%   | ✅    |
| insert_qps       | 440      | 435     | -1.1%   | ✅    |
| knn_qps          | 810      | 790     | -2.5%   | ✅    |
| knn_p99_ms       | 3.2      | 3.4     | +6.3%   | ✅    |
| recall_at_10     | 0.942    | 0.940   | -0.002  | ✅    |
```

---

## Files created

| File | Status |
|---|---|
| `scripts/myvectorbench.py` | New |
| `scripts/myvectorbench-compare.py` | New |
| `myvectorbench.yml` | New |
| `.github/workflows/myvectorbench.yml` | New |
| `benchmarks/README.md` | New (on `benchmarks/` branch, created by first CI run) |

No existing files modified.
