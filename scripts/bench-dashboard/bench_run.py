#!/usr/bin/env python3
"""Run the MyVector workload against ONE already-running container and emit JSON.

Reuses scripts/myvectorbench.py's proven workload logic via a no-op Container
subclass. Always uses a LOCAL `docker` (run this on whichever host owns the
container), so there is no SSH arg-quoting to worry about. Synthetic vectors
are seed-deterministic, so every instance benchmarks identical data.

Usage:
  bench_run.py --container bench-mysql97 --version 9.7 --root-pw PW \
               --host-label "myvector-dev (A1 16/96)" --out /path/out.json
"""
import argparse
import json
import os
import platform
import sys
import time
from datetime import datetime, timezone

# Import the repo's benchmark module from the same directory as this script.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import myvectorbench as mb  # noqa: E402


class LiveContainer(mb.Container):
    """A Container that points at an existing, persistent container instead of
    starting/stopping its own. All SQL goes through local `docker exec`."""

    def __init__(self, name, root_pw, version):
        self.version = version
        self.root_pw = root_pw
        self.name = name
        self._running = True
        self._extra_volumes = []
        self._image = None

    def start(self):  # never create
        pass

    def stop(self):   # never destroy
        pass


WORKLOAD = {
    "dataset": "synthetic",
    "rows": 50000,
    "dim": 128,
    "M": 16,
    "ef_construction": 200,
    "knn_queries": 200,
    "knn_ann_queries": 200,
    "recall_queries": 100,
    "holdout_queries": 200,   # honest recall: queries never inserted
    "distance": "L2",
    "ef_search_sweep": [10, 20, 50, 100, 200],
    "ef_search_sweep_queries": 50,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--container", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--root-pw", required=True)
    ap.add_argument("--host-label", default="")
    ap.add_argument("--rows", type=int, default=None, help="override row count (smoke tests)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    wp = dict(WORKLOAD)
    if args.rows:
        wp["rows"] = args.rows

    c = LiveContainer(args.container, args.root_pw, args.version)

    print(f"== {args.container} (MySQL {args.version}) ==", flush=True)
    print("  configuring myvector.cnf + stored procedures ...", flush=True)
    mb._configure_myvector(c)
    c.sql_stdin(mb.INSTALL_PROCS_SQL, "mysql")

    print(f"  generating synthetic workload (rows={wp['rows']}, dim={wp['dim']}) ...", flush=True)
    vectors, held_out = mb.load_workload(wp["dataset"], wp)

    t0 = time.time()
    metrics = mb.run_workloads(
        c, vectors, wp,
        build_path="component", mysql_version=args.version,
        ann_gate=False, held_out=held_out,
    )
    wall = time.time() - t0

    result = {
        "version": args.version,
        "container": args.container,
        "host_label": args.host_label,
        "arch": platform.machine(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "rows_indexed": len(vectors),
        "workload": {k: wp[k] for k in ("rows", "dim", "M", "ef_construction",
                                        "distance", "holdout_queries")},
        "wall_seconds": round(wall, 1),
        "metrics": metrics,
    }
    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)
    print(f"  wrote {args.out} (wall {wall:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
