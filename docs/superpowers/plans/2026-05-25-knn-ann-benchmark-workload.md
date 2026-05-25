# knn_ann Benchmark Workload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `knn_ann` workload to myvectorbench that issues `MYVECTOR_IS_ANN` queries, gracefully reports `0.0` when the component's query rewrite is inactive, and becomes the regression gate for the component fix in the follow-on PR.

**Architecture:** Python-only change. One new `bench_knn_ann()` function added to `scripts/myvectorbench.py`, wired into `run_workloads()`. Config and tests updated to match. No C++ changes.

**Tech Stack:** Python 3, pytest, PyYAML, Docker (MySQL 8.4 / 9.7 containers)

**Prerequisite:** PR #95 (`fix/bench-runtime-bugs`) must be merged before implementing this plan. All file paths below are repo-relative from that merged state.

---

## File Map

| File | Change |
|---|---|
| `scripts/myvectorbench.py` | Add `bench_knn_ann()` after `bench_knn_search`; wire into `run_workloads()`; add `knn_ann_queries` to `workload_params` JSON |
| `myvectorbench.yml` | Add `knn_ann_queries: 200` under `workload`; add 3 thresholds under `thresholds` |
| `tests/test_myvectorbench_compare.py` | Add 3 tests covering `knn_ann_qps=0.0` baseline, `knn_ann_p50_ms=null` baseline, and `knn_ann_qps` breach |

`scripts/myvectorbench-compare.py` requires **no changes** — the existing null/zero guards already handle the new metric shapes correctly.

---

## Task 1: Add compare tests for knn_ann metric handling

**Files:**
- Modify: `tests/test_myvectorbench_compare.py`

- [ ] **Step 1: Write three failing tests**

Open `tests/test_myvectorbench_compare.py`. Append these three tests after `test_compare_no_baseline`:

```python
def test_compare_knn_ann_zero_baseline_skips(capsys):
    """knn_ann_qps=0.0 baseline → N/A row, no breach (workload was broken at baseline time)."""
    with tempfile.TemporaryDirectory() as tmp:
        baseline = _make_json(tmp, 'baseline.json', {'knn_ann_qps': 0.0})
        current = _make_json(tmp, 'current.json', {'knn_ann_qps': 45.2})
        cfg = _make_config(tmp, "  knn_ann_qps: -25%\n")
        rc = compare(baseline, current, cfg)
        captured = capsys.readouterr()
    assert rc == 0
    assert 'N/A' in captured.out


def test_compare_knn_ann_null_latency_skips(capsys):
    """knn_ann_p50_ms=null in baseline → N/A row, no breach."""
    with tempfile.TemporaryDirectory() as tmp:
        baseline = _make_json(tmp, 'baseline.json', {'knn_ann_p50_ms': None})
        current = _make_json(tmp, 'current.json', {'knn_ann_p50_ms': 21.3})
        cfg = _make_config(tmp, "  knn_ann_p50_ms: +30%\n")
        rc = compare(baseline, current, cfg)
        captured = capsys.readouterr()
    assert rc == 0
    assert 'N/A' in captured.out


def test_compare_knn_ann_qps_breach(capsys):
    """knn_ann_qps drops >25% after fix → breach detected."""
    with tempfile.TemporaryDirectory() as tmp:
        baseline = _make_json(tmp, 'baseline.json', {'knn_ann_qps': 45.0})
        current = _make_json(tmp, 'current.json', {'knn_ann_qps': 32.0})  # -28.9%
        cfg = _make_config(tmp, "  knn_ann_qps: -25%\n")
        rc = compare(baseline, current, cfg)
        captured = capsys.readouterr()
    assert rc == 1
    assert 'FAIL' in captured.out
```

- [ ] **Step 2: Run the tests to verify they pass**

```bash
cd <repo-root>
pytest tests/test_myvectorbench_compare.py -v
```

Expected: all 16 tests PASS. The three new tests rely only on existing compare logic; no implementation changes needed first.

- [ ] **Step 3: Commit**

```bash
git add tests/test_myvectorbench_compare.py
git commit -m "test: add compare tests for knn_ann null/zero baseline handling"
```

---

## Task 2: Add bench_knn_ann() to myvectorbench.py

**Files:**
- Modify: `scripts/myvectorbench.py`

- [ ] **Step 1: Add `bench_knn_ann` function after `bench_knn_search`**

In `scripts/myvectorbench.py`, locate line ~494 (the blank line after `bench_knn_search` returns). Insert the following function:

```python
def bench_knn_ann(container: Container, vectors: list, wp: dict) -> dict:
    """Run MYVECTOR_IS_ANN queries using the HNSW index.

    Returns knn_ann_qps=0.0 and null latencies when query rewrite is inactive
    (component build before the QueryRewriterService fix).
    """
    n_queries = wp.get('knn_ann_queries', 200)
    print(f"  [knn_ann] {n_queries} queries, dim={wp['dim']}")

    rng = random.Random(77)
    query_vectors = [vectors[rng.randint(0, len(vectors) - 1)] for _ in range(n_queries)]

    latencies_ms = []
    for q in query_vectors:
        sql = (
            f"SELECT id, myvector_row_distance(id) AS dist"
            f" FROM bench.build_t"
            f" WHERE MYVECTOR_IS_ANN('vec', 'id', {_vec_literal(q)})"
            f" ORDER BY dist LIMIT 10;"
        )
        t0 = time.time()
        try:
            container.sql(sql)
        except RuntimeError:
            print("    ⚠ MYVECTOR_IS_ANN not supported (query rewrite inactive)")
            return {"knn_ann_qps": 0.0, "knn_ann_p50_ms": None, "knn_ann_p99_ms": None}
        latencies_ms.append((time.time() - t0) * 1000)

    latencies_ms.sort()
    p50 = statistics.median(latencies_ms)
    p99 = latencies_ms[max(0, math.ceil(len(latencies_ms) * 0.99) - 1)]
    qps = n_queries / (sum(latencies_ms) / 1000) if latencies_ms else 0.0
    print(f"    knn_ann_qps={qps:.0f}  p50={p50:.1f}ms  p99={p99:.1f}ms")
    return {"knn_ann_qps": qps, "knn_ann_p50_ms": p50, "knn_ann_p99_ms": p99}
```

- [ ] **Step 2: Wire bench_knn_ann into run_workloads**

Find `run_workloads` (~line 496). It currently ends with:

```python
    metrics.update(bench_knn_search(container, vectors, wp))
    metrics["recall_at_10"] = None  # requires ANN query API not available in v1
    return metrics
```

Change it to:

```python
    metrics.update(bench_knn_search(container, vectors, wp))
    metrics.update(bench_knn_ann(container, vectors, wp))
    metrics["recall_at_10"] = None  # requires ANN query API not available in v1
    return metrics
```

- [ ] **Step 3: Add knn_ann_queries to workload_params in the JSON output**

Find the `workload_params` dict in `run_benchmark` (~line 632). It currently is:

```python
        "workload_params": {
            "rows": wp.get("rows", 10000),
            "dim": wp.get("dim", 128),
            "M": wp.get("M", 16),
            "ef_construction": wp.get("ef_construction", 200),
            "knn_queries": wp.get("knn_queries", 200),
        },
```

Add one line:

```python
        "workload_params": {
            "rows": wp.get("rows", 10000),
            "dim": wp.get("dim", 128),
            "M": wp.get("M", 16),
            "ef_construction": wp.get("ef_construction", 200),
            "knn_queries": wp.get("knn_queries", 200),
            "knn_ann_queries": wp.get("knn_ann_queries", 200),
        },
```

- [ ] **Step 4: Run compare tests to verify nothing regressed**

```bash
pytest tests/test_myvectorbench_compare.py -v
```

Expected: all 16 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/myvectorbench.py
git commit -m "feat: add bench_knn_ann workload (MYVECTOR_IS_ANN, graceful degradation)"
```

---

## Task 3: Update myvectorbench.yml config

**Files:**
- Modify: `myvectorbench.yml`

- [ ] **Step 1: Add knn_ann_queries to the workload section**

Open `myvectorbench.yml`. The current `workload:` section ends at `knn_queries: 200`. Add one line:

```yaml
workload:
  dataset: synthetic
  rows: 10000
  dim: 128
  M: 16
  ef_construction: 200
  knn_queries: 200
  knn_ann_queries: 200
```

- [ ] **Step 2: Add knn_ann thresholds**

The current `thresholds:` section ends at `recall_at_10: -0.05`. Add three lines:

```yaml
thresholds:
  index_build_time_s: +25%
  insert_qps: -25%
  knn_qps: -25%
  knn_p99_ms: +30%
  recall_at_10: -0.05   # absolute delta — NOT YET EVALUATED: always emitted as null in v1
  knn_ann_qps: -25%
  knn_ann_p50_ms: +30%
  knn_ann_p99_ms: +30%
```

- [ ] **Step 3: Commit**

```bash
git add myvectorbench.yml
git commit -m "config: add knn_ann_queries workload param and thresholds"
```

---

## Task 4: End-to-end verification

**Files:** none (read-only run)

These runs require Docker. Use the artifact dirs produced by the existing component build (`dist/component-8.4`) and plugin image (`myvector:mysql8.4-local`).

- [ ] **Step 1: Run against component — expect warning + zero QPS**

```bash
python3 scripts/myvectorbench.py \
  --mysql-version 8.4 \
  --build-path component \
  --artifact-dir dist/component-8.4 \
  --output /tmp/bench-knn-ann-component.json
```

Expected output must include:
```
  [knn_ann] 200 queries, dim=128
    ⚠ MYVECTOR_IS_ANN not supported (query rewrite inactive)
```

And `/tmp/bench-knn-ann-component.json` must contain:
```json
"knn_ann_qps": 0.0,
"knn_ann_p50_ms": null,
"knn_ann_p99_ms": null
```

- [ ] **Step 2: Run against plugin — expect real QPS**

```bash
python3 scripts/myvectorbench.py \
  --mysql-version 8.4 \
  --build-path plugin \
  --artifact-dir dist/plugin-8.4 \
  --output /tmp/bench-knn-ann-plugin.json
```

Expected output must include:
```
  [knn_ann] 200 queries, dim=128
    knn_ann_qps=<nonzero>  p50=<nonzero>ms  p99=<nonzero>ms
```

And `/tmp/bench-knn-ann-plugin.json` must contain a nonzero `knn_ann_qps`.

- [ ] **Step 3: Run compare — confirm 0.0 baseline produces N/A row**

```bash
python3 scripts/myvectorbench-compare.py \
  /tmp/bench-knn-ann-component.json \
  /tmp/bench-knn-ann-plugin.json \
  --config myvectorbench.yml
```

Expected: exit 0. The `knn_ann_qps` row shows N/A (baseline=0.0 triggers the zero-baseline guard).

- [ ] **Step 4: Final commit if any fixup needed; otherwise push and open PR**

```bash
git push -u origin <branch-name>
gh pr create \
  --title "feat: add knn_ann benchmark workload (MYVECTOR_IS_ANN gap instrument)" \
  --body "$(cat <<'EOF'
## Summary
- Adds `bench_knn_ann()` workload to myvectorbench using `MYVECTOR_IS_ANN` syntax
- Gracefully reports `knn_ann_qps=0.0` when component query rewrite is inactive
- Updates `myvectorbench.yml` with `knn_ann_queries` param and 3 new thresholds
- Adds 3 compare tests for null/zero baseline handling

## Why
The component build silently falls back to full table scans for `MYVECTOR_IS_ANN` queries
because `BEGIN_COMPONENT_PROVIDES` in `myvector_component.cc` is empty. This workload
proves the gap and becomes the regression gate for the component fix (follow-on PR).

## Test plan
- [ ] All 16 compare tests pass: `pytest tests/test_myvectorbench_compare.py -v`
- [ ] Component run prints `⚠ MYVECTOR_IS_ANN not supported` and emits `knn_ann_qps=0.0`
- [ ] Plugin run emits nonzero `knn_ann_qps`
- [ ] Compare tool exits 0 with N/A row when baseline is 0.0
EOF
)"
```
