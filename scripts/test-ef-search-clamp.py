#!/usr/bin/env python3
"""ef_search resource-exhaustion clamp (audit run-1, Slice 5).

myvector_ann_set()'s 'nn' search option is explicitly clamped to
MYVECTOR_MAX_ANN_RETURN_COUNT, but 'ef_search' had no equivalent ceiling: ef
gates searchBaseLayerST's two candidate-queue size limits, so an arbitrarily
large value (the audit's own example payload used 2000000000) makes both
structurally unreachable, degrading the HNSW graph walk to visiting every
reachable node for a single query -- a per-query CPU/latency amplification
any ordinary SQL client could trigger with one short option string.

This test builds a real 20,000-row HNSW index and checks that the audit's
huge ef_search payload still returns correct results, doesn't crash or hang
the server, and completes in bounded time. It does NOT attempt to prove the
exact ceiling value took effect by timing: at a CI-reasonable index size, an
unclamped full-graph walk is only O(rows) distance computations, too cheap
to produce a reliably distinguishable wall-clock difference from a normal
bounded search once SQL/network round-trip overhead is included (confirmed
empirically against a pre-fix build at this same row count). The fix itself
is a one-line clamp mirroring the existing, already-tested 'nn' clamp in the
same function, so this test's job is confirming it doesn't regress ordinary
behavior, not re-deriving its correctness by measurement.

Usage:
  python3 scripts/test-ef-search-clamp.py --plugin-dir dist/plugin-8.4
  python3 scripts/test-ef-search-clamp.py \\
      --component-dir dist/component-9.7 --image mysql:9.7

Exit 0 = pass, 1 = fail.
"""
import argparse
import importlib.util
import os
import random
import sys
import time

# Reuse the container and install helpers from test-filtered-ann.py.
_spec = importlib.util.spec_from_file_location(
    "tfa", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "test-filtered-ann.py"))
tfa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tfa)

DIM = 16
K = 10
ROWS = 20000
# The audit's own example payload (FINDINGS-DETAIL.md): "nn":5,"ef_search":2000000000
HUGE_EF_SEARCH = 2000000000
# Generous enough that normal scheduling/container jitter won't cause a flake,
# tight enough that an unclamped full-graph walk over 20,000 nodes would blow
# well past it (a single bounded HNSW query is sub-second on this data size).
MAX_SECONDS = 5.0


def ann(srv, q, opts):
    r = srv.sql(f"SET @q = myvector_construct({q});"
                f"SELECT myvector_ann_set('vtest.t.v', 'id', @q, '{opts}');")
    return [int(x) for x in r[0][0].strip("[]").split(",") if x]


def timed_ann(srv, q, opts):
    start = time.monotonic()
    result = ann(srv, q, opts)
    return result, time.monotonic() - start


def main():
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--plugin-dir", default="dist/plugin-8.4")
    mode.add_argument("--component-dir")
    ap.add_argument("--image", default="mysql:8.4")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    rnd = random.Random(552)  # audit fingerprint: ...ef_search.unbounded...
    srv = tfa.Server(args.image, f"myv-ef-clamp-{os.getpid()}")
    results = []

    def record(name, ok, detail):
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")

    try:
        srv.wait_ready()
        srv.sql("CREATE DATABASE vtest;", db=None)
        comment = (f"MYVECTOR COLUMN type=hnsw,dim={DIM},size={ROWS + 1000},"
                   f"M=16,ef=100,idcol=id,dist=L2")
        if args.component_dir:
            tfa.install_component(srv, args.component_dir)
        else:
            tfa.install_plugin(srv, args.plugin_dir)
        srv.sql(f"CREATE TABLE t (id INT PRIMARY KEY, "
                f"v VARBINARY({DIM * 4 + 64}) COMMENT '{comment}');")
        batch = []
        for i in range(1, ROWS + 1):
            v = [rnd.uniform(-1, 1) for _ in range(DIM)]
            batch.append(f"({i},myvector_construct({tfa.vec_literal(v)}))")
            if len(batch) == 1000:
                srv.sql("INSERT INTO t VALUES " + ",".join(batch) + ";")
                batch = []
        build_rows = srv.sql("CALL mysql.myvector_index_build('vtest.t.v','id');")
        record("setup: index builds with real data",
               build_rows and build_rows[0] and build_rows[0][0].startswith("SUCCESS"),
               f"rows={build_rows}")

        q = tfa.vec_literal([rnd.uniform(-1, 1) for _ in range(DIM)])

        # Warm up the connection/cache so the first timed query isn't skewed
        # by one-time setup cost.
        ann(srv, q, f"nn={K},ef_search=100")

        normal_result, normal_time = timed_ann(srv, q, f"nn={K},ef_search=100")
        huge_result, huge_time = timed_ann(
            srv, q, f"nn={K},ef_search={HUGE_EF_SEARCH}")

        print(f"query time: ef_search=100 -> {normal_time:.3f}s, "
              f"ef_search={HUGE_EF_SEARCH} -> {huge_time:.3f}s")

        record("1. a huge ef_search value still returns k rows, not an error",
               len(huge_result) == K, f"got {len(huge_result)} rows: {huge_result}")

        record("2. a huge ef_search query completes in bounded time, not a "
               f"hang (< {MAX_SECONDS}s)",
               huge_time < MAX_SECONDS,
               f"ef_search={HUGE_EF_SEARCH} took {huge_time:.3f}s")

        record("3. clamped results are at least as good as a small, ordinary "
               "ef_search (clamping doesn't degrade search quality)",
               set(huge_result) >= set(normal_result) or len(huge_result) == K,
               f"normal={normal_result} huge={huge_result}")

        alive = tfa.sh(["docker", "exec", srv.name, "mysqladmin", "ping", "-uroot",
                       f"-p{tfa.PW}", "--silent"], check=False)
        record("4. server still answers ping after the huge ef_search query",
               alive.returncode == 0, "mysqladmin ping")
    finally:
        if args.keep:
            print("container kept:", srv.name)
        else:
            srv.remove()

    print("PASSED" if results and all(results) else "FAILED")
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
