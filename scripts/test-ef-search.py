#!/usr/bin/env python3
"""ef_search is per query (#165).

A query with myvector_ann_set(..., 'nn=10,ef_search=N') used to store N on the
shared index. Later queries from any session then searched with N instead of
the index default, and the write raced with concurrent searches.

This test builds an HNSW index and records the results of default queries.
It then runs ef_search queries, a single one and a concurrent burst mixing
low and high values, and checks that the same default queries return exactly
the same results afterwards. HNSW search with the same ef is deterministic,
so any difference means ef_search leaked into the index.

Usage:
  python3 scripts/test-ef-search.py --plugin-dir dist/plugin-8.4
  python3 scripts/test-ef-search.py --component-dir dist/component-9.7 --image mysql:9.7

Exit 0 = pass, 1 = fail.
"""
import argparse
import importlib.util
import os
import random
import sys
from concurrent.futures import ThreadPoolExecutor

# Reuse the container and install helpers from test-filtered-ann.py.
_spec = importlib.util.spec_from_file_location(
    "tfa", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "test-filtered-ann.py"))
tfa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tfa)

DIM = 16
K = 10


def ann(srv, q, opts):
    r = srv.sql(f"SET @q = myvector_construct({q});"
                f"SELECT myvector_ann_set('vtest.t.v', 'id', @q, '{opts}');")
    return [int(x) for x in r[0][0].strip("[]").split(",") if x]


def main():
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--plugin-dir", default="dist/plugin-8.4")
    mode.add_argument("--component-dir")
    ap.add_argument("--image", default="mysql:8.4")
    ap.add_argument("--rows", type=int, default=20000)
    ap.add_argument("--queries", type=int, default=20)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    rnd = random.Random(165)
    srv = tfa.Server(args.image, f"myv-ef-search-{os.getpid()}")
    failures = []
    try:
        srv.wait_ready()
        srv.sql("CREATE DATABASE vtest;", db=None)
        comment = (f"MYVECTOR COLUMN type=hnsw,dim={DIM},size={args.rows + 1000},"
                   f"M=16,ef=100,idcol=id,dist=L2")
        if args.component_dir:
            tfa.install_component(srv, args.component_dir)
        else:
            tfa.install_plugin(srv, args.plugin_dir)
        srv.sql(f"CREATE TABLE t (id INT PRIMARY KEY, "
                f"v VARBINARY({DIM * 4 + 64}) COMMENT '{comment}');")
        batch = []
        for i in range(1, args.rows + 1):
            v = [rnd.uniform(-1, 1) for _ in range(DIM)]
            batch.append(f"({i},myvector_construct({tfa.vec_literal(v)}))")
            if len(batch) == 1000:
                srv.sql("INSERT INTO t VALUES " + ",".join(batch) + ";")
                batch = []
        print("index build:",
              srv.sql("CALL mysql.myvector_index_build('vtest.t.v','id');"))
        tfa.check_index_type(srv, "vtest.t.v", "HNSW", failures)

        queries = [tfa.vec_literal([rnd.uniform(-1, 1) for _ in range(DIM)])
                   for _ in range(args.queries)]
        baseline = [ann(srv, q, f"nn={K}") for q in queries]

        # ef_search takes effect for the query that sets it.
        low = [ann(srv, q, f"nn={K},ef_search=10") for q in queries]
        high = [ann(srv, q, f"nn={K},ef_search=400") for q in queries]
        exact = []
        for q in queries:
            r = srv.sql(f"SET @q = myvector_construct({q});"
                        f"SELECT id FROM t ORDER BY myvector_distance(v, @q, 'L2') "
                        f"LIMIT {K};")
            exact.append({int(x[0]) for x in r})

        def recall(res):
            return sum(len(set(a) & e) for a, e in zip(res, exact)) / (K * len(exact))

        r_base, r_low, r_high = recall(baseline), recall(low), recall(high)
        print(f"recall@{K}: default={r_base:.3f} ef_search=10:{r_low:.3f} "
              f"ef_search=400:{r_high:.3f}")
        if any(len(x) != K for x in low + high):
            failures.append("an ef_search query returned fewer than k rows")
        if r_high < r_base:
            failures.append(f"ef_search=400 recall {r_high:.3f} < default {r_base:.3f}")

        # After a single low ef_search query, default queries are unchanged.
        ann(srv, queries[0], f"nn={K},ef_search=10")
        after_one = [ann(srv, q, f"nn={K}") for q in queries]
        changed = sum(a != b for a, b in zip(baseline, after_one))
        print(f"default queries changed after one ef_search=10 query: {changed}/{len(queries)}")
        if changed:
            failures.append(f"{changed} default queries changed after one "
                            f"ef_search=10 query (ef_search leaked into the index)")

        # Concurrent burst mixing low and high ef_search, then defaults again.
        # Each worker uses its own connection (docker exec per call).
        jobs = [(queries[i % len(queries)], 10 if i % 2 else 400) for i in range(80)]
        with ThreadPoolExecutor(max_workers=8) as ex:
            burst = list(ex.map(lambda j: ann(srv, j[0], f"nn={K},ef_search={j[1]}"), jobs))
        if any(len(x) != K for x in burst):
            failures.append("a concurrent ef_search query returned fewer than k rows")
        after_burst = [ann(srv, q, f"nn={K}") for q in queries]
        changed = sum(a != b for a, b in zip(baseline, after_burst))
        print(f"default queries changed after a concurrent ef_search burst: "
              f"{changed}/{len(queries)}")
        if changed:
            failures.append(f"{changed} default queries changed after a concurrent "
                            f"ef_search burst")
    finally:
        if args.keep:
            print("container kept:", srv.name)
        else:
            srv.remove()

    if failures:
        print("FAIL")
        for f in failures:
            print("  -", f)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
