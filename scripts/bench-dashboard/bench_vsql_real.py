#!/usr/bin/env python3
"""Full-metric VillageSQL vsql_vector benchmark on a real ann-benchmarks dataset
(HDF5), scored against the PUBLISHED ground truth — the vsql_vector counterpart
of bench_real.py / bench_mariadb_real.py, emitting the SAME metric keys so it
drops into the dashboard with the full metric set.

vsql_vector is a VillageSQL *extension* on a VillageSQL (MySQL-fork) dev-server:
`SVECTOR(dim)` column, `CREATE INDEX ... USING EXTENDED(hnsw) WITH (M, ef_construction)`,
`L2_DISTANCE(v, '[...]')`, session var `vsql_vector.ef_search`. It is an ordinary
ORDER-BY-distance engine, so the shape matches MariaDB/pgvector.

Connects to a NATIVE dev-server over its unix socket via the server's own mysql
client (root, no password), inheriting myvectorbench's batched NOW(6) timing.
ef_search rides per-connection via the client's --init-command. Requires numpy+h5py.
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
from bench_real import load_hdf5, _recall  # noqa: E402


class VsqlServer(mb.Container):
    """Drives a native VillageSQL dev-server via its bundled mysql client over a
    unix socket. init_cmd (if set) runs on every connection (for ef_search)."""

    def __init__(self, mysql_bin, socket, version="vsql"):
        self.version = version
        self.mysql_bin = mysql_bin
        self.socket = socket
        self.init_cmd = ""
        self._running = True

    def start(self): pass
    def stop(self): pass

    def _base_cmd(self, db="", interactive=False):
        cmd = [self.mysql_bin, "-S", self.socket, "-uroot", "--batch", "--silent"]
        if self.init_cmd:
            cmd.append(f"--init-command={self.init_cmd}")
        if db:
            cmd += ["-D", db]
        return cmd


def vlit(v):
    return "'[" + ",".join(f"{x:.6f}" for x in v) + "]'"


def _qps_p(lat):
    lat = sorted(lat)
    p50 = statistics.median(lat) if lat else 0.0
    p99 = lat[max(0, math.ceil(len(lat) * 0.99) - 1)] if lat else 0.0
    qps = len(lat) / (sum(lat) / 1000) if lat and sum(lat) > 0 else 0.0
    return qps, p50, p99


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mysql-bin", required=True, help="path to the dev-server's bin/mysql")
    ap.add_argument("--socket", required=True, help="dev-server unix socket")
    ap.add_argument("--version", default="vsql_vector", help="result version key")
    ap.add_argument("--label", default="vsql_vector · VillageSQL")
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--dataset-label", default="")
    ap.add_argument("--host-label", default="myvector-dev (A1 16/96)")
    ap.add_argument("--M", type=int, default=16)
    ap.add_argument("--ef-construction", type=int, default=200)
    ap.add_argument("--knn-queries", type=int, default=50)
    ap.add_argument("--recall-queries", type=int, default=1000)
    ap.add_argument("--sweep-queries", type=int, default=500)
    ap.add_argument("--ef-search-sweep", default="10,20,50,100,200,400,800")
    ap.add_argument("--max-rows", type=int, default=None)
    ap.add_argument("--max-queries", type=int, default=None)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    train, test, neighbors, metric, ntrain = load_hdf5(args.dataset, args.max_rows, args.max_queries)
    VIDX = {"L2": ("hnsw_l2", "L2_DISTANCE"), "Cosine": ("hnsw_cosine", "COSINE_DISTANCE")}
    if metric not in VIDX:
        raise SystemExit(f"bench_vsql_real supports L2/Cosine (got {metric})")
    idxmod, distfn = VIDX[metric]
    dim = len(train[0])
    sweep_points = [int(x) for x in args.ef_search_sweep.split(",") if x.strip()]
    c = VsqlServer(args.mysql_bin, args.socket)
    label = args.dataset_label or os.path.basename(args.dataset)
    print(f"== vsql_vector · dataset {label} ==", flush=True)
    print(f"  train={ntrain} test={len(test)} dim={dim} metric={metric}", flush=True)

    c.sql("CREATE DATABASE IF NOT EXISTS bench;")
    c.sql("DROP TABLE IF EXISTS bench.t; DROP TABLE IF EXISTS bench.exact;")
    c.sql(f"CREATE TABLE bench.t (id INT PRIMARY KEY, v SVECTOR({dim}) NOT NULL);")
    c.sql(f"CREATE TABLE bench.exact (id INT PRIMARY KEY, v SVECTOR({dim}) NOT NULL);")

    def insert_all(table):
        t0 = time.time()
        batch = 500
        for s in range(0, ntrain, batch):
            vals = ",".join(f"({s+i},{vlit(train[s+i])})" for i in range(min(batch, ntrain - s)))
            c.sql_stdin(f"INSERT INTO bench.{table} (id,v) VALUES {vals};", "bench")
        return time.time() - t0

    insert_s = insert_all("t")
    insert_qps = ntrain / insert_s if insert_s else 0.0
    print(f"  [insert] {insert_s:.1f}s  insert_qps={insert_qps:.0f}", flush=True)

    t0 = time.time()
    c.sql(f"CREATE INDEX idx_v ON bench.t (v {idxmod}) USING EXTENDED(hnsw) "
          f"WITH (M = {args.M}, ef_construction = {args.ef_construction});", "bench")
    index_s = time.time() - t0
    print(f"  [index build] {index_s:.1f}s", flush=True)
    insert_all("exact")  # unindexed, for the exact-KNN baseline

    metrics = {"index_build_time_s": round(index_s, 2), "insert_qps": insert_qps}

    def ann_sql(q, k=10):
        return f"SELECT id FROM bench.t ORDER BY {distfn}(v, {vlit(q)}) LIMIT {k}"

    def exact_sql(q, k=10):
        return f"SELECT id FROM bench.exact ORDER BY {distfn}(v, {vlit(q)}) LIMIT {k}"

    # exact KNN baseline (brute force on the unindexed table)
    c.init_cmd = ""
    nk = min(args.knn_queries, len(test))
    q, p50, p99 = _qps_p(c.sql_batch_timed([exact_sql(test[i]) for i in range(nk)], "bench"))
    metrics.update(knn_qps=q, knn_p50_ms=p50, knn_p99_ms=p99)
    print(f"  [exact KNN] {nk} queries  qps={q:.2f} p50={p50:.1f} p99={p99:.1f}", flush=True)

    # published ground truth
    nq = min(args.recall_queries, len(test))
    truth = [{int(x) for x in neighbors[i][:10] if 0 <= int(x) < ntrain} for i in range(nq)]

    # headline ANN at ef_search=200
    c.init_cmd = "SET SESSION vsql_vector.ef_search=200"
    aq = [ann_sql(test[i]) for i in range(min(200, len(test)))]
    q, p50, p99 = _qps_p(c.sql_batch_timed(aq, "bench"))
    metrics.update(knn_ann_qps=q, knn_ann_p50_ms=p50, knn_ann_p99_ms=p99, ann_rewrite_active=True)
    print(f"  [ANN ef=200] qps={q:.0f} p50={p50:.1f} p99={p99:.1f}", flush=True)

    # recall@10 at ef_search=200 vs published GT
    ann_blocks = c.sql_batch_results([ann_sql(test[i]) for i in range(nq)], "bench")
    recall = _recall(truth, ann_blocks)
    metrics["recall_at_10"] = recall
    print(f"  [recall@10 ef=200] {recall:.4f}" if recall is not None else "  recall N/A", flush=True)

    # ef_search sweep (warm: discard a pass first)
    sq = min(args.sweep_queries, len(test))
    truth_s = truth[:sq]
    c.init_cmd = f"SET SESSION vsql_vector.ef_search={sweep_points[-1]}"
    c.sql_batch_timed([ann_sql(test[i]) for i in range(sq)], "bench")  # warmup
    sweep = []
    for ef in sweep_points:
        c.init_cmd = f"SET SESSION vsql_vector.ef_search={ef}"
        qs = [ann_sql(test[i]) for i in range(sq)]
        rec = _recall(truth_s, c.sql_batch_results(qs, "bench"))
        qq, pp50, pp99 = _qps_p(c.sql_batch_timed(qs, "bench"))
        sweep.append({"ef_search": ef, "recall_at_10": rec, "qps": qq, "p50_ms": pp50, "p99_ms": pp99})
        rs = f"{rec:.4f}" if rec is not None else "N/A"
        print(f"    ef={ef:<4} recall={rs}  qps={qq:.0f}  p50={pp50:.1f}", flush=True)
    metrics["ef_search_sweep"] = sweep

    c.init_cmd = ""
    ver = c.scalar("SELECT VERSION();")
    result = {
        "version": args.version, "label": args.label,
        "engine": "VillageSQL vsql_vector (SVECTOR+HNSW)",
        "host_label": args.host_label,
        "arch": subprocess.run(["uname", "-m"], capture_output=True, text=True).stdout.strip(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dataset": label, "rows_indexed": ntrain,
        "server_version": ver.strip(),
        "recall_ground_truth": "published", "harness": "myvector bench_vsql_real",
        "workload": {"rows": ntrain, "dim": dim, "M": args.M,
                     "ef_construction": args.ef_construction, "distance": metric,
                     "holdout_queries": nq},
        "metrics": metrics,
    }
    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)
    print(f"  wrote {args.out}", flush=True)


if __name__ == "__main__":
    main()
