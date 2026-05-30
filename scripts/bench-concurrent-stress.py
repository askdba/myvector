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


def _writer_worker(container_name: str, root_pw: str, dim: int,
                   next_id: list, id_lock: threading.Lock,
                   stop_event: threading.Event) -> dict:
    """Online writer: INSERTs rows into an online-indexed table in a tight loop."""
    results: dict = {"ops": 0, "errors": 0}
    rng = random.Random(threading.get_ident())
    base_cmd = [
        "docker", "exec", "-e", f"MYSQL_PWD={root_pw}", container_name,
        "mysql", "-uroot", "-h127.0.0.1", "--batch", "--silent", "-D", "bench",
    ]
    while not stop_event.is_set():
        with id_lock:
            row_id = next_id[0]
            next_id[0] += 1
        v = [rng.gauss(0, 1) for _ in range(dim)]
        sql = (
            f"INSERT INTO bench.stress_write (id, vec) VALUES"
            f" ({row_id}, {_vec_literal(v)});"
        )
        r = subprocess.run(base_cmd + ["-e", sql], capture_output=True, text=True)
        if r.returncode != 0:
            results["errors"] += 1
        else:
            results["ops"] += 1
    return results


def _ann_reader_worker(container_name: str, root_pw: str, vectors: list,
                       stop_event: threading.Event) -> dict:
    """ANN reader: issues MYVECTOR_IS_ANN queries via query rewrite in a tight loop.

    Only dispatched when ann_rewrite_active=True on the 9.x component cell.
    """
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
            f" WHERE MYVECTOR_IS_ANN('bench.stress_knn.vec', 'id', {_vec_literal(q)})"
            f" ORDER BY myvector_row_distance(id) LIMIT 10;"
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



# ── dataset + table setup ─────────────────────────────────────────────────────

def _setup_stress_tables(container: Container, vectors: list, wp: dict) -> dict:
    """Create stress_knn (read-only HNSW) and stress_write (online write) tables.

    Returns {'knn_rows': N, 'write_base_id': M} for consistency checks.
    """
    dim = wp['dim']
    rows = len(vectors)
    M_val = wp.get('M', 16)
    ef = wp.get('ef_construction', 200)

    container.sql("CREATE DATABASE IF NOT EXISTS bench;")
    container.sql("DROP TABLE IF EXISTS bench.stress_knn;")
    container.sql(
        f"CREATE TABLE bench.stress_knn ("
        f"  id INT PRIMARY KEY,"
        f"  vec VARBINARY({dim * 4 + 8})"
        f"    COMMENT 'MYVECTOR COLUMN type=hnsw,dim={dim},size={rows},"
        f"m={M_val},ef={ef},idcol=id,dist=L2'"
        f");"
    )
    container.sql("DROP TABLE IF EXISTS bench.stress_write;")
    container.sql(
        f"CREATE TABLE bench.stress_write ("
        f"  id INT PRIMARY KEY,"
        f"  vec VARBINARY({dim * 4 + 8})"
        f"    COMMENT 'MYVECTOR COLUMN type=hnsw,dim={dim},size={rows * 2},"
        f"m={M_val},ef={ef},idcol=id,dist=L2,online=Y'"
        f");"
    )

    batch = 500
    for start in range(0, rows, batch):
        chunk = vectors[start:start + batch]
        vals = ", ".join(
            f"({start + i}, {_vec_literal(v)})" for i, v in enumerate(chunk)
        )
        container.sql_stdin(f"INSERT INTO bench.stress_knn (id, vec) VALUES {vals};", "bench")

    container.sql("CALL mysql.MYVECTOR_INDEX_BUILD('bench.stress_knn.vec', 'id');")
    print(f"  stress_knn: {rows} rows, HNSW built")
    return {"knn_rows": rows, "write_base_id": rows * 10}


def _probe_ann_rewrite(container: Container, dim: int) -> bool:
    """Return True when MYVECTOR_IS_ANN query rewrite is active."""
    probe_vec = "[" + ",".join(["0.0"] * dim) + "]"
    try:
        container.sql(
            f"SELECT MYVECTOR_IS_ANN('bench.stress_knn.vec', 'id',"
            f" myvector_construct('{probe_vec}'))"
            f" FROM bench.stress_knn LIMIT 0;",
            db="bench",
        )
        return True
    except RuntimeError as e:
        if "does not exist" in str(e) and "FUNCTION" in str(e):
            return False
        return True  # other error = rewrite IS active


# ── consistency checks ────────────────────────────────────────────────────────

def _run_consistency_checks(container: Container, setup_info: dict,
                             knn_before: list, write_expected: int,
                             futures_clean: bool) -> dict:
    """Run post-stress consistency assertions."""
    checks: dict = {}

    # 1. Index row count stable (stress_knn is read-only, must not drift)
    out = container.scalar("CALL mysql.MYVECTOR_INDEX_STATUS('bench.stress_knn.vec');")
    actual_rows_str = ""
    for part in out.split():
        if part.isdigit():
            actual_rows_str = part
            break
    checks["index_row_count_stable"] = (actual_rows_str == str(setup_info["knn_rows"]))

    # 2. KNN top-1 stable
    if knn_before:
        q = knn_before[0]["query"]
        sql = (
            f"SELECT id FROM bench.stress_knn"
            f" ORDER BY myvector_distance(vec, {_vec_literal(q)}, 'L2') LIMIT 1;"
        )
        out = container.sql(sql)
        lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
        top1_after = int(lines[-1]) if lines and lines[-1].isdigit() else -1
        checks["knn_result_stable"] = (top1_after == knn_before[0]["top1"])
    else:
        checks["knn_result_stable"] = True

    # 3. No deadlock in InnoDB status
    innodb = container.sql("SHOW ENGINE INNODB STATUS;")
    checks["no_deadlock"] = "DEADLOCK" not in innodb.upper()

    # 4. All threads clean exit
    checks["all_threads_clean_exit"] = futures_clean

    return checks


# ── pool runner ───────────────────────────────────────────────────────────────

def _run_pools(container_name: str, root_pw: str, vectors: list, dim: int,
               n_knn: int, n_write: int, n_ann: int,
               stop_event: threading.Event, next_id: list,
               id_lock: threading.Lock):
    """Start all worker pools. Always returns (fknn, fwrite, fann, (ex_knn, ex_write, ex_ann))."""
    ex_knn   = ThreadPoolExecutor(max_workers=max(n_knn, 1))
    ex_write = ThreadPoolExecutor(max_workers=max(n_write, 1))
    ex_ann   = ThreadPoolExecutor(max_workers=max(n_ann, 1))

    fknn = [
        ex_knn.submit(_knn_reader_worker, container_name, root_pw, vectors, stop_event)
        for _ in range(n_knn)
    ]
    fwrite = [
        ex_write.submit(_writer_worker, container_name, root_pw, dim,
                        next_id, id_lock, stop_event)
        for _ in range(n_write)
    ]
    fann = [
        ex_ann.submit(_ann_reader_worker, container_name, root_pw, vectors, stop_event)
        for _ in range(n_ann)
    ] if n_ann > 0 else []

    return fknn, fwrite, fann, (ex_knn, ex_write, ex_ann)


def _drain(fknn, fwrite, fann, execs, res_knn, res_write, res_ann) -> bool:
    """Collect results from all futures into the provided lists. Returns all_clean bool."""
    all_clean = True
    for res_list, flist in [(res_knn, fknn), (res_write, fwrite), (res_ann, fann)]:
        for f in flist:
            try:
                res_list.append(f.result(timeout=60))
            except Exception as e:
                res_list.append({"errors": 1, "_exception": str(e)})
                all_clean = False
    for ex in execs:
        ex.shutdown(wait=False)
    return all_clean


# ── main stress runner ────────────────────────────────────────────────────────

def run_stress(mysql_version: str, build: str, artifact_dir: str,
               config: dict, output: str,
               n_knn: int = 50, n_write: int = 50, n_ann: int = 20,
               duration_s: int = 60, image: str = None) -> int:
    """Run the concurrent stress harness. Returns 0 on pass, 1 on failure."""
    wp = config.get('workload', {'rows': 10000, 'dim': 128, 'M': 16, 'ef_construction': 200})
    dim = wp['dim']
    vectors = _synthetic_vectors(wp.get('rows', 10000), dim)

    with Container(mysql_version, image=image) as c:
        if build == "component":
            install_component(c, artifact_dir)
        elif artifact_dir:
            _bench_mod.install_plugin(c, os.path.join(artifact_dir, "myvector.so"))
        # else: plugin pre-installed in image (GHCR); UDFs + procs assumed present

        setup_info = _setup_stress_tables(c, vectors, wp)
        ann_active = _probe_ann_rewrite(c, dim)
        print(f"  ann_rewrite_active={ann_active}")
        n_ann_active = n_ann if ann_active else 0

        # Record top-1 KNN before stress for stability check.
        rng0 = random.Random(0)
        q0 = vectors[rng0.randint(0, len(vectors) - 1)]
        sql0 = (
            f"SELECT id FROM bench.stress_knn"
            f" ORDER BY myvector_distance(vec, {_vec_literal(q0)}, 'L2') LIMIT 1;"
        )
        top1_out = c.sql(sql0)
        lines0 = [ln.strip() for ln in top1_out.strip().splitlines() if ln.strip()]
        top1_before = int(lines0[-1]) if lines0 and lines0[-1].isdigit() else -1
        knn_before = [{"query": q0, "top1": top1_before}]

        WARMUP_S = 30
        container_name = c.name
        root_pw = c.root_pw

        # ── warmup ──────────────────────────────────────────────────────────
        print(f"  Warmup {WARMUP_S}s ...")
        warmup_stop = threading.Event()
        warmup_id = [setup_info["write_base_id"]]
        warmup_lock = threading.Lock()
        wfknn, wfwrite, wfann, wexecs = _run_pools(
            container_name, root_pw, vectors, dim,
            n_knn, n_write, n_ann_active, warmup_stop, warmup_id, warmup_lock,
        )
        time.sleep(WARMUP_S)
        warmup_stop.set()
        _drain(wfknn, wfwrite, wfann, wexecs, [], [], [])  # discard warmup results

        # ── measurement ─────────────────────────────────────────────────────
        print(f"  Measuring {duration_s}s ...")
        stop_event = threading.Event()
        next_id = [setup_info["write_base_id"] + warmup_id[0]]
        id_lock = threading.Lock()
        t0 = time.time()
        fknn, fwrite, fann, execs = _run_pools(
            container_name, root_pw, vectors, dim,
            n_knn, n_write, n_ann_active, stop_event, next_id, id_lock,
        )
        time.sleep(duration_s)
        stop_event.set()
        actual_duration = time.time() - t0

        # ── drain ────────────────────────────────────────────────────────────
        print("  Draining ...")
        res_knn: list = []
        res_write: list = []
        res_ann: list = []
        all_clean = _drain(fknn, fwrite, fann, execs, res_knn, res_write, res_ann)

        write_inserted = next_id[0] - (setup_info["write_base_id"] + warmup_id[0])
        checks = _run_consistency_checks(c, setup_info, knn_before, write_inserted, all_clean)

    pools: dict = {
        "knn_readers": _aggregate(res_knn, actual_duration),
        "writers":     _aggregate(res_write, actual_duration),
    }
    if ann_active and res_ann:
        pools["ann_readers"] = _aggregate(res_ann, actual_duration)

    passed = _evaluate_pass(pools, checks)
    result = {
        "workload": "concurrent_stress",
        "mysql_version": mysql_version,
        "build": build,
        "duration_s": round(actual_duration, 1),
        "ann_rewrite_active": ann_active,
        "pools": pools,
        "checks": checks,
        "passed": passed,
    }
    with open(output, "w") as f:
        json.dump(result, f, indent=2)
    print(f"  Result written to {output}")
    print(f"  passed={passed}")
    return 0 if passed else 1
