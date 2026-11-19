#!/usr/bin/env python3
"""Full-metric pgvector benchmark on a real ann-benchmarks dataset (HDF5),
scored against the dataset's PUBLISHED ground truth — the pgvector counterpart
of bench_real.py / bench_mariadb_real.py, emitting the SAME metric keys
(index build, insert QPS, exact KNN QPS + p50/p99, ANN QPS + p50/p99, recall@10,
ef_search sweep) so it drops into the same dashboard with the full metric set.

Rows are inserted with id == train index, so a published neighbor index is a row
id directly. pgvector HNSW: `vector(dim)` column, `<->` L2 operator, index
`USING hnsw (e vector_l2_ops) WITH (m, ef_construction)`, `SET hnsw.ef_search`.
Per-query latency comes from psql's `\\timing` (one session, server+round-trip).

Talks to a pgvector container via `docker exec psql`. Requires numpy + h5py.
"""
import argparse
import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from bench_real import load_hdf5, _recall  # noqa: E402


class PgContainer:
    def __init__(self, name, user="bench", db="bench", password="bench"):
        self.name, self.user, self.db, self.password = name, user, db, password

    def _run(self, extra, sql):
        cmd = ["docker", "exec", "-i", "-e", f"PGPASSWORD={self.password}", self.name,
               "psql", "-U", self.user, "-d", self.db, "-v", "ON_ERROR_STOP=1"] + extra
        return subprocess.run(cmd, input=sql, capture_output=True, text=True)

    def exec(self, sql):
        p = self._run(["-q"], sql)
        if p.returncode != 0:
            raise RuntimeError(f"psql failed: {p.stderr.strip()}\n--- sql ---\n{sql[:400]}")
        return p.stdout

    def scalar(self, sql):
        p = self._run(["-tA"], sql)
        if p.returncode != 0:
            raise RuntimeError(f"psql failed: {p.stderr.strip()}")
        return p.stdout.strip()

    def batch_results(self, queries, setup=""):
        """Return a per-query list of id-lists. Each query is preceded by a
        '@@Q<i>' sentinel so the -tA output can be split back per query. `setup`
        runs first IN THE SAME session (each docker-exec psql is a fresh session,
        so a session var like hnsw.ef_search must ride inside this batch)."""
        parts = [setup] if setup else []
        for i, q in enumerate(queries):
            parts.append(f"SELECT '@@Q{i}';")
            parts.append(q + ";")
        p = self._run(["-tA"], "\n".join(parts))
        if p.returncode != 0:
            raise RuntimeError(f"psql failed: {p.stderr.strip()}")
        # Per-query list of raw id STRINGS (matches myvectorbench's
        # sql_batch_results contract, which _recall() parses).
        per, cur = {}, None
        for line in p.stdout.splitlines():
            s = line.strip()
            if s.startswith("@@Q"):
                cur = int(s[3:]); per[cur] = []
            elif cur is not None and s.isdigit():
                per[cur].append(s)
        return [per.get(i, []) for i in range(len(queries))]

    def batch_timed(self, queries, setup=""):
        """Per-query latency (ms) via psql \\timing, one session. `setup` runs
        BEFORE `\\timing on` (so it isn't counted) but in the same session, so
        session vars (hnsw.ef_search, enable_indexscan) apply to the queries."""
        body = (setup + "\n" if setup else "") + "\\timing on\n" + \
               "\n".join(q + ";" for q in queries) + "\n"
        p = self._run(["-q"], body)
        if p.returncode != 0:
            raise RuntimeError(f"psql failed: {p.stderr.strip()}")
        return [float(x) for x in re.findall(r"Time:\s+([0-9.]+)\s+ms", p.stdout)]


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
    ap.add_argument("--container", default="dev-pgvector")
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
    OPS = {"L2": ("vector_l2_ops", "<->"), "Cosine": ("vector_cosine_ops", "<=>")}
    if metric not in OPS:
        raise SystemExit(f"bench_pgvector_real supports L2/Cosine (got {metric})")
    opclass, op = OPS[metric]
    dim = len(train[0])
    sweep_points = [int(x) for x in args.ef_search_sweep.split(",") if x.strip()]
    c = PgContainer(args.container)
    label = args.dataset_label or os.path.basename(args.dataset)
    print(f"== {args.container} (pgvector) · dataset {label} ==", flush=True)
    print(f"  train={ntrain} test={len(test)} dim={dim} metric={metric}", flush=True)

    c.exec("DROP TABLE IF EXISTS ann;")
    c.exec(f"CREATE TABLE ann (id INT PRIMARY KEY, e vector({dim}) NOT NULL);")

    # insert (no index yet) -> insert QPS; then CREATE INDEX -> index build time.
    t0 = time.time()
    batch = 500
    for s in range(0, ntrain, batch):
        vals = ",".join(f"({s+i},{vlit(train[s+i])})" for i in range(min(batch, ntrain - s)))
        c.exec(f"INSERT INTO ann (id,e) VALUES {vals};")
    insert_s = time.time() - t0
    insert_qps = ntrain / insert_s if insert_s else 0.0
    print(f"  [insert] {insert_s:.1f}s  insert_qps={insert_qps:.0f}", flush=True)

    t0 = time.time()
    c.exec(f"SET maintenance_work_mem='1GB'; CREATE INDEX ON ann USING hnsw (e {opclass}) "
           f"WITH (m={args.M}, ef_construction={args.ef_construction});")
    index_s = time.time() - t0
    print(f"  [index build] {index_s:.1f}s", flush=True)

    metrics = {"index_build_time_s": round(index_s, 2), "insert_qps": insert_qps}

    def ann_sql(q, k=10):
        return f"SELECT id FROM ann ORDER BY e {op} {vlit(q)} LIMIT {k}"

    # exact KNN baseline: force a seq scan (index off) for brute force, in-session.
    nk = min(args.knn_queries, len(test))
    lat = c.batch_timed([ann_sql(test[i]) for i in range(nk)],
                        setup="SET enable_indexscan=off; SET enable_bitmapscan=off;")
    q, p50, p99 = _qps_p(lat)
    metrics.update(knn_qps=q, knn_p50_ms=p50, knn_p99_ms=p99)
    print(f"  [exact KNN] {nk} queries  qps={q:.2f} p50={p50:.1f} p99={p99:.1f}", flush=True)

    # published ground truth
    nq = min(args.recall_queries, len(test))
    truth = [{int(x) for x in neighbors[i][:10] if 0 <= int(x) < ntrain} for i in range(nq)]

    # headline ANN at ef_search=200
    aq = [ann_sql(test[i]) for i in range(min(200, len(test)))]
    q, p50, p99 = _qps_p(c.batch_timed(aq, setup="SET hnsw.ef_search=200;"))
    metrics.update(knn_ann_qps=q, knn_ann_p50_ms=p50, knn_ann_p99_ms=p99, ann_rewrite_active=True)
    print(f"  [ANN ef=200] qps={q:.0f} p50={p50:.1f} p99={p99:.1f}", flush=True)

    # recall@10 at ef_search=200 vs published GT
    ann_blocks = c.batch_results([ann_sql(test[i]) for i in range(nq)],
                                 setup="SET hnsw.ef_search=200;")
    recall = _recall(truth, ann_blocks)
    metrics["recall_at_10"] = recall
    print(f"  [recall@10 ef=200] {recall:.4f}" if recall is not None else "  recall N/A", flush=True)

    # ef_search sweep (warm: discard a pass first)
    sq = min(args.sweep_queries, len(test))
    truth_s = truth[:sq]
    warm_q = [ann_sql(test[i]) for i in range(sq)]
    c.batch_timed(warm_q, setup=f"SET hnsw.ef_search={sweep_points[-1]};")  # warmup
    sweep = []
    for ef in sweep_points:
        setq = f"SET hnsw.ef_search={ef};"
        qs = [ann_sql(test[i]) for i in range(sq)]
        rec = _recall(truth_s, c.batch_results(qs, setup=setq))
        qq, pp50, pp99 = _qps_p(c.batch_timed(qs, setup=setq))
        sweep.append({"ef_search": ef, "recall_at_10": rec, "qps": qq, "p50_ms": pp50, "p99_ms": pp99})
        rs = f"{rec:.4f}" if rec is not None else "N/A"
        print(f"    ef={ef:<4} recall={rs}  qps={qq:.0f}  p50={pp50:.1f}", flush=True)
    metrics["ef_search_sweep"] = sweep

    ver = c.scalar("SHOW server_version;")
    result = {
        "version": "pgvector", "label": f"pgvector · Postgres {ver.split('.')[0]}",
        "engine": "pgvector HNSW", "container": args.container, "host_label": args.host_label,
        "arch": subprocess.run(["uname", "-m"], capture_output=True, text=True).stdout.strip(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dataset": label, "rows_indexed": ntrain,
        "recall_ground_truth": "published", "harness": "myvector bench_pgvector_real",
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
