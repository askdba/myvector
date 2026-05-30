#!/usr/bin/env python3
"""bench-concurrent-stress.py — RFC-004 concurrent stress test for MyVector.

Usage:
  python3 scripts/bench-concurrent-stress.py \
      --mysql-version 8.4 --build component \
      --artifact-dir dist/component-8.4 \
      --duration 60

  # Full RFC-004 scenario (200+100+100 threads, 120s):
  python3 scripts/bench-concurrent-stress.py \
      --mysql-version 9.7 --build component \
      --artifact-dir dist/component-9.7 \
      --threads-knn 200 --threads-write 100 --threads-ann 100 \
      --duration 120
"""

import argparse
import importlib.util
import json
import math
import os
import random
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Import shared helpers from myvectorbench.py without requiring it on PYTHONPATH.
_script_dir = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "myvectorbench",
    os.path.join(_script_dir, "myvectorbench.py"),
)
_bench_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bench_mod)

Container = _bench_mod.Container
install_component = _bench_mod.install_component
_vec_literal = _bench_mod._vec_literal
_create_bench_table = _bench_mod._create_bench_table
_synthetic_vectors = _bench_mod._synthetic_vectors


# ── aggregation + pass evaluation ────────────────────────────────────────────

def _aggregate(thread_results: list, duration_s: float) -> dict:
    """Aggregate per-thread result dicts into pool-level metrics."""
    total_queries = sum(r.get("queries", 0) + r.get("ops", 0) for r in thread_results)
    total_errors = sum(r.get("errors", 0) for r in thread_results)
    all_lat = []
    for r in thread_results:
        all_lat.extend(r.get("latencies_ms", []))

    agg: dict = {
        "threads": len(thread_results),
        "qps": round(total_queries / duration_s, 1) if duration_s > 0 else 0.0,
        "errors": total_errors,
    }
    if all_lat:
        all_lat.sort()
        agg["p99_ms"] = round(
            all_lat[max(0, math.ceil(len(all_lat) * 0.99) - 1)], 1
        )
    return agg


def _evaluate_pass(pools: dict, checks: dict) -> bool:
    """Return True iff all pass criteria hold."""
    for pool_name, pool_metrics in pools.items():
        if pool_metrics.get("errors", 0) > 0:
            return False
    return all(checks.values())



# ── worker functions ──────────────────────────────────────────────────────────

def _knn_reader_worker(container_name: str, root_pw: str, vectors: list,
                       stop_event: threading.Event) -> dict:
    """KNN reader: runs SELECT ... ORDER BY myvector_distance LIMIT 10 in a tight loop."""
    results: dict = {"queries": 0, "errors": 0, "latencies_ms": []}
    rng = random.Random(threading.get_ident())
    base_cmd = [
        "docker", "exec", "-e", f"MYSQL_PWD={root_pw}", container_name,
        "mysql", "-uroot", "-h127.0.0.1", "--batch", "--silent", "-D", "bench",
    ]
    while not stop_event.is_set():
        q = vectors[rng.randint(0, len(vectors) - 1)]
        sql = (
            f"SELECT id FROM bench.stress_knn"
            f" ORDER BY myvector_distance(vec, {_vec_literal(q)}, 'L2') LIMIT 10;"
        )
        t0 = time.time()
        r = subprocess.run(base_cmd + ["-e", sql], capture_output=True, text=True)
        elapsed_ms = (time.time() - t0) * 1000
        if r.returncode != 0:
            results["errors"] += 1
        else:
            results["queries"] += 1
            results["latencies_ms"].append(elapsed_ms)
    return results


def run_stress(*args, **kwargs):
    """Entry point for RFC-004 concurrent stress run. To be implemented."""
    raise NotImplementedError("run_stress not yet implemented")
