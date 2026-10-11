#!/usr/bin/env python3
"""Benchmark MariaDB native HNSW vector search on the SAME workload as the
MyVector/MySQL runs, emitting a result JSON with identical metric keys so it
drops into the same dashboard.

MariaDB vector API (11.8): VECTOR(n) column + `VECTOR INDEX(col) M=.. DISTANCE=euclidean`,
VEC_FromText('[..]'), VEC_DISTANCE_EUCLIDEAN(col, q), session/global `mhnsw_ef_search`.

Run on the dev host (container bench-mariadb). Reuses myvectorbench for the
seed-deterministic synthetic vectors (so recall is comparable across engines)
and its batched-timing helpers.
"""
import argparse
import json
import math
import os
import random
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import myvectorbench as mb  # noqa: E402


class MariaContainer(mb.Container):
    def __init__(self, name, root_pw):
        self.version = "mariadb"
        self.root_pw = root_pw
        self.name = name
        self._running = True

    def start(self): pass
    def stop(self): pass

    def _base_cmd(self, db="", interactive=False):
        cmd = ["docker", "exec"]
        if interactive:
            cmd.append("-i")
        cmd += ["-e", f"MYSQL_PWD={self.root_pw}", self.name,
                "mariadb", "-uroot", "-h127.0.0.1", "--batch", "--silent"]
        if db:
            cmd += ["-D", db]
        return cmd


def vlit(v):
    return "VEC_FromText('[" + ",".join(f"{x:.6f}" for x in v) + "]')"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--container", default="bench-mariadb")
    ap.add_argument("--root-pw", required=True)
    ap.add_argument("--host-label", default="myvector-dev · Ampere A1")
    ap.add_argument("--rows", type=int, default=50000)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    wp = {"dataset": "synthetic", "rows": args.rows, "dim": 128, "M": 16,
          "ef_construction": 200, "knn_queries": 200, "knn_ann_queries": 200,
          "recall_queries": 100, "holdout_queries": 200, "distance": "L2",
          "ef_search_sweep": [10, 20, 50, 100, 200], "ef_search_sweep_queries": 50}
    dim, M = wp["dim"], wp["M"]
    c = MariaContainer(args.container, args.root_pw)

    print(f"== {args.container} (MariaDB) ==", flush=True)
    vectors, held_out = mb.load_workload("synthetic", wp)
    rows = len(vectors)
    print(f"  {rows} vectors dim={dim}", flush=True)

    # ---- tables: indexed (ANN) + unindexed (exact ground truth) ----
    c.sql("CREATE DATABASE IF NOT EXISTS bench;")
    c.sql("DROP TABLE IF EXISTS bench.ann; DROP TABLE IF EXISTS bench.exact;")
    c.sql(f"CREATE TABLE bench.ann (id INT PRIMARY KEY, e VECTOR({dim}) NOT NULL, "
          f"VECTOR INDEX(e) M={M} DISTANCE=euclidean);")
    c.sql(f"CREATE TABLE bench.exact (id INT PRIMARY KEY, e VECTOR({dim}) NOT NULL);")

    def insert_all(table, timed):
        t0 = time.time()
        batch = 500
        for s in range(0, rows, batch):
            chunk = vectors[s:s + batch]
            vals = ",".join(f"({s+i},{vlit(v)})" for i, v in enumerate(chunk))
            c.sql_stdin(f"INSERT INTO bench.{table} (id,e) VALUES {vals};", "bench")
        return time.time() - t0

    # Build = load into the indexed table (MariaDB builds HNSW online during insert;
    # no separate build step, unlike MyVector's MYVECTOR_INDEX_BUILD).
    build_s = insert_all("ann", timed=True)
    insert_qps = rows / build_s if build_s else 0.0
    print(f"  [build/insert indexed] {build_s:.1f}s  insert_qps={insert_qps:.0f}", flush=True)
    insert_all("exact", timed=False)  # ground-truth table

    metrics = {"index_build_time_s": round(build_s, 2), "insert_qps": insert_qps}

    def ann_sql(q, k=10):
        return (f"SELECT id FROM bench.ann ORDER BY "
                f"VEC_DISTANCE_EUCLIDEAN(e,{vlit(q)}) LIMIT {k}")

    def exact_sql(q, k=10):
        return (f"SELECT id FROM bench.exact ORDER BY "
                f"VEC_DISTANCE_EUCLIDEAN(e,{vlit(q)}) LIMIT {k}")

    def timed_qps(queries):
        lat = c.sql_batch_timed(queries, "bench")
        lat.sort()
        p50 = statistics.median(lat)
        p99 = lat[max(0, math.ceil(len(lat) * 0.99) - 1)]
        qps = len(queries) / (sum(lat) / 1000) if lat else 0.0
        return qps, p50, p99

    # exact KNN (brute force on unindexed table)
    rng = random.Random(99)
    kq = [vectors[rng.randint(0, rows - 1)] for _ in range(wp["knn_queries"])]
    q, p50, p99 = timed_qps([exact_sql(v) for v in kq])
    metrics.update(knn_qps=q, knn_p50_ms=p50, knn_p99_ms=p99)
    print(f"  [exact KNN] qps={q:.0f} p50={p50:.1f} p99={p99:.1f}", flush=True)

    # headline ANN at ef_search=200 (comparable accuracy operating point)
    c.sql("SET GLOBAL mhnsw_ef_search=200;")
    aq = [vectors[rng.randint(0, rows - 1)] for _ in range(wp["knn_ann_queries"])]
    q, p50, p99 = timed_qps([ann_sql(v) for v in aq])
    metrics.update(knn_ann_qps=q, knn_ann_p50_ms=p50, knn_ann_p99_ms=p99, ann_rewrite_active=True)
    print(f"  [ANN ef=200] qps={q:.0f} p50={p50:.1f} p99={p99:.1f}", flush=True)

    # recall@10 (held-out queries) at ef_search=200
    rq = (held_out or vectors)[:wp["recall_queries"]]
    truth_blocks = c.sql_batch_results([exact_sql(v) for v in rq], "bench")
    ann_blocks = c.sql_batch_results([ann_sql(v) for v in rq], "bench")
    recalls = []
    for tb, ab in zip(truth_blocks, ann_blocks):
        t = {int(x) for x in tb if x.strip()}
        a = {int(x) for x in ab if x.strip()}
        if t:
            recalls.append(len(t & a) / len(t))
    recall = sum(recalls) / len(recalls) if recalls else None
    metrics["recall_at_10"] = recall
    print(f"  [recall@10 ef=200] {recall}", flush=True)

    # ef_search sweep
    sweep = []
    sq = (held_out or vectors)[:wp["ef_search_sweep_queries"]]
    truth = c.sql_batch_results([exact_sql(v) for v in sq], "bench")
    truth_sets = [{int(x) for x in b if x.strip()} for b in truth]
    for ef in wp["ef_search_sweep"]:
        c.sql(f"SET GLOBAL mhnsw_ef_search={ef};")
        qs = [ann_sql(v) for v in sq]
        ablocks = c.sql_batch_results(qs, "bench")
        rs = []
        for t, b in zip(truth_sets, ablocks):
            a = {int(x) for x in b if x.strip()}
            if t:
                rs.append(len(t & a) / len(t))
        rec = sum(rs) / len(rs) if rs else None
        qq, pp50, pp99 = timed_qps(qs)
        sweep.append({"ef_search": ef, "recall_at_10": rec, "qps": qq, "p50_ms": pp50, "p99_ms": pp99})
        print(f"    ef={ef:<4} recall={rec}  qps={qq:.0f}", flush=True)
    metrics["ef_search_sweep"] = sweep

    ver = c.scalar("SELECT VERSION();")
    result = {
        "version": "MariaDB", "label": f"MariaDB {ver.split('-')[0]}",
        "engine": "MariaDB native VECTOR/HNSW",
        "container": args.container, "host_label": args.host_label,
        "arch": subprocess.run(["uname", "-m"], capture_output=True, text=True).stdout.strip(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "rows_indexed": rows,
        "workload": {k: wp[k] for k in ("rows", "dim", "M", "distance", "holdout_queries")},
        "metrics": metrics,
    }
    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)
    print(f"  wrote {args.out}", flush=True)


if __name__ == "__main__":
    main()
