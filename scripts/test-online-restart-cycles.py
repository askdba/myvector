#!/usr/bin/env python3
"""A file-backed online index over several restart cycles.

Each cycle: restart the server (the index is loaded from its files), insert
rows (applied from the binlog), rotate the binlog (the index is checkpointed
into its files). The index must hold every row after every step.

Before the fix, the second cycle broke the index: after a load, the checkpoint
treated the first node of the upper-level links file (offset 0) as not stored
yet and appended a second copy of its links without a directory entry. Every
upper-level node added later was then read from the wrong place at the next
load, and the graph had link lists full of other data: inserts failed with
"cand error" and the index stayed unusable until rebuilt.

  1-3. three cycles: row counts match after the restart and after the inserts.
  4.   every sampled row is found by a search with its own vector.
  5.   the server log has no failed insert and no corrupted-index message.

Usage:
  python3 scripts/test-online-restart-cycles.py --plugin-dir dist/plugin-8.4
  python3 scripts/test-online-restart-cycles.py \\
      --component-dir dist/component-8.4 --image mysql:8.4

Exit 0 = all checks passed.
"""

import argparse
import importlib.util
import os
import random
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "online_idle", os.path.join(HERE, "test-online-updates-idle.py"))
idle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(idle)

DIM = 16
BASE_ROWS = 5000
CYCLE_ROWS = 3000
CYCLES = 3


def main():
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--component-dir")
    mode.add_argument("--plugin-dir")
    ap.add_argument("--image", default="mysql:8.4")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    component = bool(args.component_dir)
    results = []
    srv = idle.Server(args.image, f"myv-cycles-{os.getpid()}")
    rnd = random.Random(3)
    vectors = {}

    def record(name, ok, detail=""):
        results.append(bool(ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}"[:400], flush=True)

    def insert(n):
        for a in range(0, n, 500):
            ids = range(len(vectors) + 1, len(vectors) + 1 + min(500, n - a))
            vals = []
            for i in ids:
                vectors[i] = "[" + ",".join(f"{rnd.uniform(-1, 1):.4f}"
                                           for _ in range(DIM)) + "]"
                vals.append(f"({i}, myvector_construct('{vectors[i]}'))")
            srv.sql("INSERT INTO t VALUES " + ", ".join(vals) + ";")

    def index_rows():
        out = srv.sql("CALL mysql.myvector_index_status('vtest.t.v');", check=False)
        m = re.search(r"Current Rows : (\d+)", out[0][0].replace("\\n", "\n")) if out else None
        return int(m.group(1)) if m else -1

    def caught_up(timeout=90):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if index_rows() == len(vectors):
                return True
            time.sleep(1)
        return False

    try:
        srv.wait_ready()
        if component:
            idle.install_component(srv, args.component_dir)
            col = (f"v VARBINARY({DIM * 4 + 64}) NULL COMMENT 'MYVECTOR COLUMN type=hnsw,"
                   f"dim={DIM},size=100000,idcol=id,dist=L2,online=Y'")
        else:
            idle.install_plugin(srv, args.plugin_dir)
            # The index directory must be the same after the restarts.
            srv.sql("SET PERSIST myvector_index_dir = '/var/lib/mysql';", db=None)
            col = (f"v MYVECTOR(type=HNSW,dim={DIM},size=100000,idcol=id,dist=L2,"
                   "online=Y) NULL")
        srv.sql("CREATE DATABASE vtest;", db=None)
        srv.sql(f"CREATE TABLE t (id INT PRIMARY KEY, {col});")
        insert(BASE_ROWS)
        out = srv.sql("CALL mysql.myvector_index_build('vtest.t.v','id');")[0][0]
        record("0. build", out.startswith("SUCCESS"), out[:70])

        for cycle in range(1, CYCLES + 1):
            idle.sh(["docker", "restart", srv.name])
            srv.wait_ready()
            ok1 = caught_up()
            r1 = index_rows()
            insert(CYCLE_ROWS)
            ok2 = caught_up()
            r2 = index_rows()
            srv.sql("FLUSH BINARY LOGS;", db=None)  # the file backend's checkpoint
            time.sleep(4)
            record(f"{cycle}. cycle {cycle}: loaded after the restart, then "
                   f"{CYCLE_ROWS} inserts applied", ok1 and ok2,
                   f"index {r1} rows after the restart, {r2} after the inserts; "
                   f"table {len(vectors)}")

        ids = rnd.sample(sorted(vectors), 60)
        q = "".join(f"SELECT myvector_ann_set('vtest.t.v','id',myvector_construct("
                    f"'{vectors[i]}'),'nn=1,ef_search=200');" for i in ids)
        got = [r[0] for r in srv.sql(q, check=False)]
        missing = [i for i, g in zip(ids, got) if g != f"[{i}]"]
        record("4. sampled rows are found by their own vector",
               len(got) == len(ids) and not missing,
               f"{len(ids)} checked, {len(missing)} not found, {len(got)} answers")
        bad = [ln for ln in srv.log().splitlines()
               if "failed (" in ln or "cand error" in ln or "seems to be corrupted" in ln]
        record("5. no failed insert or corrupted index in the server log", not bad,
               bad[0][:200] if bad else "")
    except Exception as e:  # noqa: BLE001
        record("test ran to completion", False, repr(e)[:300])

    if not all(results):
        log = idle.sh(["docker", "logs", "--tail", "30", srv.name], check=False)
        print((log.stdout + log.stderr)[-3000:])
    if args.keep:
        print(f"container kept: {srv.name}")
    else:
        srv.remove()
    ok = bool(results) and all(results)
    print(f"\n{'ALL PASSED' if ok else 'FAILED'} ({sum(results)}/{len(results)})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
