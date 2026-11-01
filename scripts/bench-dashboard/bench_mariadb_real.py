#!/usr/bin/env python3
"""MariaDB native HNSW on a REAL ann-benchmarks dataset (HDF5), scored against
the dataset's PUBLISHED ground truth — the MariaDB counterpart to bench_real.py,
emitting the same metric keys so it drops into the same dashboard (myvector#239).

Rows are inserted with id == train index, so a published neighbor index is a
row id directly (no exact-GT table needed for recall). A small unindexed table
is still built for the exact-KNN latency baseline, matching the MySQL cells.

MariaDB vector API: VECTOR(n) + `VECTOR INDEX(col) M=.. DISTANCE=euclidean`,
VEC_FromText('[..]'), VEC_DISTANCE_EUCLIDEAN(col,q), global `mhnsw_ef_search`,
`mhnsw_max_cache_size` (size above the index or it thrashes to disk).

Requires numpy + h5py — run under a venv that has them.
"""
import argparse
import json
import math
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, ".."))
sys.path.insert(0, _HERE)
import myvectorbench as mb  # noqa: E402
from bench_mariadb import MariaContainer, vlit  # noqa: E402
from bench_real import load_hdf5, _recall  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--container", default="bench-mariadb")
    ap.add_argument("--root-pw", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--dataset-label", default="")
    ap.add_argument("--host-label", default="myvector-dev (A1 16/96)")
    ap.add_argument("--M", type=int, default=16)
    ap.add_argument("--knn-queries", type=int, default=50)
    ap.add_argument("--recall-queries", type=int, default=1000)
    ap.add_argument("--sweep-queries", type=int, default=500)
    ap.add_argument("--ef-search-sweep", default="10,20,50,100,200,400,800")
    ap.add_argument("--max-rows", type=int, default=None)
    ap.add_argument("--max-queries", type=int, default=None)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    train, test, neighbors, metric, ntrain = load_hdf5(
        args.dataset, args.max_rows, args.max_queries)
    if metric != "L2":
        # MariaDB 12.x indexes support euclidean + cosine; this harness wires
        # euclidean only so far. Fail loud rather than silently mismeasure.
        raise SystemExit(f"bench_mariadb_real currently supports L2 datasets only (got {metric})")
    dim = len(train[0])
    sweep_points = [int(x) for x in args.ef_search_sweep.split(",") if x.strip()]
    rows = ntrain
    c = MariaContainer(args.container, args.root_pw)
    label = args.dataset_label or os.path.basename(args.dataset)

    print(f"== {args.container} (MariaDB) · dataset {label} ==", flush=True)
    print(f"  train={rows} test={len(test)} dim={dim} metric={metric}", flush=True)

    # Size the HNSW graph cache above the index (the 16 MB default thrashes).
    mhnsw_cache = max(1073741824, rows * dim * 4 * 4)
    c.sql(f"SET GLOBAL mhnsw_max_cache_size={mhnsw_cache};")
    print(f"  mhnsw_max_cache_size = {mhnsw_cache//1024//1024} MiB", flush=True)

    c.sql("CREATE DATABASE IF NOT EXISTS bench;")
    c.sql("DROP TABLE IF EXISTS bench.ann; DROP TABLE IF EXISTS bench.exact;")
    c.sql(f"CREATE TABLE bench.ann (id INT PRIMARY KEY, e VECTOR({dim}) NOT NULL, "
          f"VECTOR INDEX(e) M={args.M} DISTANCE=euclidean);")
    c.sql(f"CREATE TABLE bench.exact (id INT PRIMARY KEY, e VECTOR({dim}) NOT NULL);")

    def insert_all(table):
        t0 = time.time()
        batch = 500
        for s in range(0, rows, batch):
            chunk = train[s:s + batch]
            vals = ",".join(f"({s+i},{vlit(v)})" for i, v in enumerate(chunk))
            c.sql_stdin(f"INSERT INTO bench.{table} (id,e) VALUES {vals};", "bench")
        return time.time() - t0

    # Build = online HNSW load into the indexed table.
    build_s = insert_all("ann")
    insert_qps = rows / build_s if build_s else 0.0
    print(f"  [build/insert indexed] {build_s:.1f}s  insert_qps={insert_qps:.0f}", flush=True)
    insert_all("exact")  # unindexed, for the exact-KNN baseline

    metrics = {"index_build_time_s": round(build_s, 2), "insert_qps": insert_qps}

    def ann_sql(q, k=10):
        return f"SELECT id FROM bench.ann ORDER BY VEC_DISTANCE_EUCLIDEAN(e,{vlit(q)}) LIMIT {k}"

    def exact_sql(q, k=10):
        return f"SELECT id FROM bench.exact ORDER BY VEC_DISTANCE_EUCLIDEAN(e,{vlit(q)}) LIMIT {k}"

    def timed_qps(queries):
        lat = c.sql_batch_timed(queries, "bench")
        lat.sort()
        p50 = statistics.median(lat)
        p99 = lat[max(0, math.ceil(len(lat) * 0.99) - 1)]
        qps = len(queries) / (sum(lat) / 1000) if lat else 0.0
        return qps, p50, p99

    # exact KNN baseline (brute force on the unindexed table), real test queries
    nk = min(args.knn_queries, len(test))
    q, p50, p99 = timed_qps([exact_sql(test[i]) for i in range(nk)])
    metrics.update(knn_qps=q, knn_p50_ms=p50, knn_p99_ms=p99)
    print(f"  [exact KNN] {nk} queries  qps={q:.0f} p50={p50:.1f} p99={p99:.1f}", flush=True)

    # published ground truth: neighbors[i][:10] are row ids directly
    nq = min(args.recall_queries, len(test))
    truth = [{int(x) for x in neighbors[i][:10] if 0 <= int(x) < ntrain} for i in range(nq)]

    # headline ANN at ef_search=200 (comparable operating point)
    c.sql("SET GLOBAL mhnsw_ef_search=200;")
    q, p50, p99 = timed_qps([ann_sql(test[i]) for i in range(min(200, len(test)))])
    metrics.update(knn_ann_qps=q, knn_ann_p50_ms=p50, knn_ann_p99_ms=p99, ann_rewrite_active=True)
    print(f"  [ANN ef=200] qps={q:.0f} p50={p50:.1f} p99={p99:.1f}", flush=True)

    # recall@10 at ef_search=200 vs published GT
    ann_blocks = c.sql_batch_results([ann_sql(test[i]) for i in range(nq)], "bench")
    recall = _recall(truth, ann_blocks)
    metrics["recall_at_10"] = recall
    print(f"  [recall@10 ef=200] {recall:.3f}" if recall is not None else "  [recall@10] N/A", flush=True)

    # ef_search sweep vs published GT
    sq = min(args.sweep_queries, len(test))
    truth_s = truth[:sq]
    # Warmup: MariaDB loads its HNSW graph into the mhnsw cache lazily over the
    # first ~1k+ queries, so a single discarded pass leaves the low-ef points
    # still warming (they mis-measure as slower than higher-ef points). Warm
    # with several passes at the MAX ef_search (touches the most graph nodes)
    # until the cache is populated, so the whole sweep is measured warm.
    c.sql(f"SET GLOBAL mhnsw_ef_search={sweep_points[-1]};")
    warm_q = [ann_sql(test[i]) for i in range(sq)]
    for _ in range(3):
        timed_qps(warm_q)
    sweep = []
    for ef in sweep_points:
        c.sql(f"SET GLOBAL mhnsw_ef_search={ef};")
        qs = [ann_sql(test[i]) for i in range(sq)]
        rec = _recall(truth_s, c.sql_batch_results(qs, "bench"))
        qq, pp50, pp99 = timed_qps(qs)
        sweep.append({"ef_search": ef, "recall_at_10": rec, "qps": qq, "p50_ms": pp50, "p99_ms": pp99})
        rs = f"{rec:.3f}" if rec is not None else "N/A"
        print(f"    ef={ef:<4} recall={rs}  qps={qq:.0f}", flush=True)
    metrics["ef_search_sweep"] = sweep

    ver = c.scalar("SELECT VERSION();")
    result = {
        "version": "MariaDB", "label": f"MariaDB {ver.split('-')[0]}",
        "engine": "MariaDB native VECTOR/HNSW",
        "container": args.container, "host_label": args.host_label,
        "arch": subprocess.run(["uname", "-m"], capture_output=True, text=True).stdout.strip(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dataset": label,
        "rows_indexed": rows,
        "query_set": "held-out test set (published)",
        "recall_ground_truth": "published",
        "workload": {"rows": rows, "dim": dim, "M": args.M, "distance": metric,
                     "holdout_queries": nq},
        "mhnsw_cache_mib": mhnsw_cache // 1024 // 1024,
        "metrics": metrics,
    }
    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)
    print(f"  wrote {args.out}", flush=True)


if __name__ == "__main__":
    main()
