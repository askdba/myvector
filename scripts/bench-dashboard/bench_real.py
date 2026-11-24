#!/usr/bin/env python3
"""Run a REAL ann-benchmarks dataset (HDF5) against ONE already-running container.

Unlike bench_run.py (synthetic, seed-deterministic, brute-forced ground truth),
this loads a published ann-benchmarks dataset in HDF5 form — `train`, `test`,
and `neighbors` (the published exact top-100 ground truth) — and measures recall
against that PUBLISHED ground truth, which is the ann-benchmarks methodology
(myvector#239).

The full `train` set is indexed with row id == train index, so a published
neighbor index maps directly to a row id with no translation. The separate
`test` set supplies held-out queries that are never inserted.

Metrics emitted match generate_page.py's schema (index build, insert QPS, exact
KNN, ANN QPS/latency, recall@10, ef_search sweep), so results render on the same
dashboard as the synthetic run.

Reuses scripts/myvectorbench.py primitives + bench_run.LiveContainer. Requires
numpy + h5py, so run it under a venv that has them, e.g.:
  /home/ubuntu/bench-venv/bin/python scripts/bench-dashboard/bench_real.py ...

Usage:
  bench_real.py --container bench-mysql84 --version 8.4 --root-pw PW \
      --dataset /data/bench-datasets/fashion-mnist-784-euclidean.hdf5 \
      --dataset-label fashion-mnist-784 \
      --host-label "myvector-dev (A1 16/96)" --out result-8.4.json
"""
import argparse
import json
import math
import os
import platform
import statistics
import sys
import time
from datetime import datetime, timezone

# Repo's benchmark module (scripts/myvectorbench.py) + the LiveContainer adapter.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, ".."))
sys.path.insert(0, _HERE)
import myvectorbench as mb  # noqa: E402
from bench_run import LiveContainer  # noqa: E402

import numpy as np  # noqa: E402
import h5py  # noqa: E402

# ann-benchmarks distance attr -> MyVector metric spelling.
DIST_MAP = {"euclidean": "L2", "angular": "Cosine", "cosine": "Cosine"}


def load_hdf5(path, max_rows=None, max_queries=None):
    """Load (train_rows, test, neighbors, metric) from an ann-benchmarks HDF5.

    train_rows is a list of 1-D numpy arrays (row id == list index). test and
    neighbors are numpy arrays; neighbors[i] is the published exact top-k for
    test[i], as indices into the full train set.
    """
    with h5py.File(path, "r") as f:
        train = np.asarray(f["train"], dtype=np.float32)
        test = np.asarray(f["test"], dtype=np.float32)
        neighbors = np.asarray(f["neighbors"], dtype=np.int64)
        distance = f.attrs.get("distance", "euclidean")
    if isinstance(distance, bytes):
        distance = distance.decode()
    metric = DIST_MAP.get(str(distance).lower())
    if metric is None:
        raise ValueError(f"unsupported dataset distance {distance!r}")
    if max_rows:
        train = train[:max_rows]
    if max_queries:
        test = test[:max_queries]
        neighbors = neighbors[:max_queries]
    # list of row-views keeps 60k×784 off a giant Python tolist() while still
    # being a plain sequence that myvectorbench's insert loops can index.
    return list(train), test, neighbors, metric, len(train)


def _ann_query(q_literal, opts=None):
    o = f", '{opts}'" if opts else ""
    return (
        f"SELECT id FROM bench.build_t"
        f" WHERE MYVECTOR_IS_ANN('bench.build_t.vec', 'id', {q_literal}{o})"
        f" ORDER BY myvector_row_distance(id) LIMIT 10;"
    )


def _recall(truth, blocks):
    """Mean recall@10 of ANN result id-sets (blocks) vs published truth sets."""
    recalls = []
    for t, block in zip(truth, blocks):
        ann_ids = {int(line) for line in block if line.strip()}
        if t:
            recalls.append(len(t & ann_ids) / len(t))
    return sum(recalls) / len(recalls) if recalls else None


def _ann_supported(c, dim):
    """True if the component's query-rewrite / MYVECTOR_IS_ANN is active."""
    probe_vec = "[" + ",".join(["0.0"] * dim) + "]"
    try:
        c.sql(
            f"SELECT MYVECTOR_IS_ANN('bench.build_t.vec', 'id',"
            f" myvector_construct('{probe_vec}')) FROM bench.build_t LIMIT 0;",
            db="bench",
        )
        return True
    except RuntimeError as e:
        if "does not exist" in str(e) and "FUNCTION" in str(e):
            return False
        return True  # other errors mean the rewrite IS active


def bench_recall_published(c, test, neighbors, wp, ntrain):
    """recall@10 at the index default ef_search + a full ef_search sweep, both
    scored against the dataset's PUBLISHED ground truth (neighbors[:10])."""
    dim = wp["dim"]
    if not _ann_supported(c, dim):
        print("    ⚠ MYVECTOR_IS_ANN not supported — recall/sweep skipped")
        return {"recall_at_10": None, "ef_search_sweep": []}

    nq = min(wp["recall_queries"], len(test))
    q_lits = [mb._vec_literal(test[i]) for i in range(nq)]
    # A published neighbor index only counts if its row was actually indexed
    # (always true for a full run; matters only when --max-rows caps train).
    truth = [
        {int(x) for x in neighbors[i][:10] if 0 <= int(x) < ntrain}
        for i in range(nq)
    ]
    print(f"  [recall] {nq} held-out queries vs published ground truth, dim={dim}")
    ann_blocks = c.sql_batch_results([_ann_query(l) for l in q_lits])
    default_recall = _recall(truth, ann_blocks)
    if default_recall is not None:
        print(f"    recall_at_10={default_recall:.3f}")

    sweep_points = wp.get("ef_search_sweep") or []
    sweep = []
    if sweep_points:
        sq = min(wp["ef_search_sweep_queries"], len(test))
        q_s, t_s = q_lits[:sq], truth[:sq]
        print(f"  [ef_search_sweep] ef_search={sweep_points} x {sq} queries")
        # Warmup: one discarded timed pass so the first measured sweep point
        # isn't penalised by cold caches / session setup (myvector#239 asks for
        # warmup). Without it the lowest ef_search point can mis-measure as the
        # slowest, inverting the start of the tradeoff curve.
        c.sql_batch_timed([_ann_query(l, f"nn=10,ef_search={sweep_points[0]}") for l in q_s])
        for ef in sweep_points:
            qs = [_ann_query(l, f"nn=10,ef_search={ef}") for l in q_s]
            rec = _recall(t_s, c.sql_batch_results(qs))
            lat = c.sql_batch_timed(qs)
            lat.sort()
            p50 = statistics.median(lat)
            p99 = lat[max(0, math.ceil(len(lat) * 0.99) - 1)]
            qps = sq / (sum(lat) / 1000) if lat else 0.0
            sweep.append({"ef_search": ef, "recall_at_10": rec,
                          "qps": qps, "p50_ms": p50, "p99_ms": p99})
            rs = f"{rec:.3f}" if rec is not None else "N/A"
            print(f"    ef_search={ef:<4} recall@10={rs}  qps={qps:.0f}  "
                  f"p50={p50:.1f}ms  p99={p99:.1f}ms")
    return {"recall_at_10": default_recall, "ef_search_sweep": sweep}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--container", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--root-pw", required=True)
    ap.add_argument("--dataset", required=True, help="path to ann-benchmarks HDF5")
    ap.add_argument("--dataset-label", default="")
    ap.add_argument("--host-label", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--M", type=int, default=16)
    ap.add_argument("--ef-construction", type=int, default=200)
    ap.add_argument("--knn-queries", type=int, default=50,
                    help="exact brute-force KNN probes (slow at scale; baseline only)")
    ap.add_argument("--recall-queries", type=int, default=1000)
    ap.add_argument("--sweep-queries", type=int, default=500)
    ap.add_argument("--ef-search-sweep", default="10,20,50,100,200,400,800")
    ap.add_argument("--max-rows", type=int, default=None, help="cap train rows (smoke only)")
    ap.add_argument("--max-queries", type=int, default=None, help="cap test queries (smoke only)")
    args = ap.parse_args()

    train, test, neighbors, metric, ntrain = load_hdf5(
        args.dataset, args.max_rows, args.max_queries)
    dim = len(train[0])
    sweep = [int(x) for x in args.ef_search_sweep.split(",") if x.strip()]
    wp = {
        "dim": dim,
        "M": args.M,
        "ef_construction": args.ef_construction,
        "distance": metric,
        "knn_queries": args.knn_queries,
        "knn_ann_queries": 200,
        "recall_queries": args.recall_queries,
        "ef_search_sweep": sweep,
        "ef_search_sweep_queries": args.sweep_queries,
    }

    c = LiveContainer(args.container, args.root_pw, args.version)
    label = args.dataset_label or os.path.basename(args.dataset)
    print(f"== {args.container} (MySQL {args.version}) · dataset {label} ==", flush=True)
    print(f"  train={ntrain} test={len(test)} dim={dim} metric={metric}", flush=True)
    print("  configuring myvector.cnf + stored procedures ...", flush=True)
    mb._configure_myvector(c)
    c.sql_stdin(mb.INSTALL_PROCS_SQL, "mysql")

    t0 = time.time()
    metrics = {}
    metrics["index_build_time_s"] = mb.bench_index_build(c, train, wp)
    metrics["insert_qps"] = mb.bench_insert_throughput(c, train, wp)
    metrics.update(mb.bench_knn_search(c, train, wp))
    metrics.update(mb.bench_knn_ann(c, train, wp, ann_gate=False))
    metrics.update(bench_recall_published(c, test, neighbors, wp, ntrain))
    wall = time.time() - t0

    result = {
        "version": args.version,
        "container": args.container,
        "host_label": args.host_label,
        "arch": platform.machine(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dataset": label,
        "rows_indexed": ntrain,
        "query_set": "held-out test set (published)",
        "recall_ground_truth": "published",
        "workload": {
            "rows": ntrain, "dim": dim, "M": args.M,
            "ef_construction": args.ef_construction, "distance": metric,
            "holdout_queries": min(args.recall_queries, len(test)),
        },
        "wall_seconds": round(wall, 1),
        "metrics": metrics,
    }
    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)
    print(f"  wrote {args.out} (wall {wall:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
