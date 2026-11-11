#!/usr/bin/env python3
"""Stale indexes after a table is recreated (#283), and full HNSW graphs (#285).

#283: an index stays open in memory after its table is dropped. MYVECTOR_INDEX_
BUILD on a recreated table of the same name (or after the column COMMENT
changed) reused that index object, with the old metric, dim, M, ef and size,
and reported SUCCESS.

#285: hnswlib throws once size= rows are in the graph, and nothing caught it, so
a row past the capacity terminated mysqld: from MYVECTOR_INDEX_BUILD, and from
the binlog worker applying an online=Y INSERT. With online=Y the stale index of
#283 also kept following the binlog, so a recreated table's INSERTs flowed into
the old graph until it overflowed.

Checks:
  A. #283 metric: build an L2 index, drop and recreate the table as Cosine, build
     again. The status reports Cosine, and an ANN search returns the cosine
     nearest neighbour, not the L2 one.
  B. #283 parameters: change M= in the COMMENT (ALTER TABLE) and rebuild; the
     status reports the new M.
  C. #285 build: a table with more rows than size= fails to build with an error
     naming the index, instead of crashing; the server survives.
  D. #285 online: an online=Y index at its capacity takes more INSERTs; the
     server survives, MYVECTOR_INDEX_STATUS counts the rejected rows, and an
     UPDATE of a row already in the index still applies.
  E. Component only: an online=Y index's database is dropped, the table is
     recreated with the old name and filled past the old size=. The server
     survives and logs that the old index was removed; the recreated table then
     builds with every row, and after a restart (which replays the binlog,
     including the old DROP) the new index still has every row.

Usage:
  python3 scripts/test-stale-index-overflow.py --plugin-dir dist/plugin-8.4
  python3 scripts/test-stale-index-overflow.py \\
      --component-dir dist/component-9.7 --image mysql:9.7

Exit 0 = all checks passed.
"""

import argparse
import importlib.util
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "online_idle", os.path.join(HERE, "test-online-updates-idle.py"))
idle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(idle)

DIM = 2
# Nearest neighbour of [1,0]: by L2 it is id 2 ([0.5,0.5]); by cosine it is
# id 1 ([10,1], almost the same direction).
ROWS = [(1, "[10,1]"), (2, "[0.5,0.5]")]
QUERY = "myvector_construct('[1,0]')"


def main():
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--plugin-dir", default="dist/plugin-8.4")
    mode.add_argument("--component-dir",
                      help="test a component build instead of the plugin")
    ap.add_argument("--image", default="mysql:8.4")
    ap.add_argument("--keep", action="store_true",
                    help="keep the container for debugging")
    ap.add_argument("--cases", default="ABCDE",
                    help="which checks to run (default ABCDE; E is component"
                         " only). Without the fix, C, D and E crash the server")
    args = ap.parse_args()

    component = bool(args.component_dir)
    srv = idle.Server(args.image, f"myv-stale-{os.getpid()}")
    results = []

    def record(name, ok, detail):
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")

    def alive():
        r = idle.sh(["docker", "exec", srv.name, "mysqladmin", "ping", "-uroot",
                     f"-p{idle.PW}", "--silent"], check=False)
        return r.returncode == 0

    def run(q, db="vtest"):
        """Run SQL; return (returncode, stdout, stderr) without raising."""
        cmd = ["docker", "exec", "-i", srv.name, "mysql", "-uroot",
               f"-p{idle.PW}", "-N", "-B"]
        if db:
            cmd.append(db)
        r = idle.sh(cmd, inp=q, check=False)
        err = "\n".join(line for line in r.stderr.splitlines()
                        if "Using a password" not in line)
        return r.returncode, r.stdout.strip(), err.strip()

    def column(opts):
        """Column definition for a vector column with these MYVECTOR options."""
        if component:
            return (f"v VARBINARY({DIM * 4 + 64}) NULL COMMENT "
                    f"'MYVECTOR COLUMN {opts},idcol=id'")
        return f"v MYVECTOR({opts},idcol=id) NULL"

    def create(table, opts, db="vtest"):
        srv.sql(f"DROP TABLE IF EXISTS {table}; CREATE TABLE {table} "
                f"(id INT PRIMARY KEY, {column(opts)});", db=db)

    def insert(table, rows, db="vtest"):
        values = ", ".join(f"({i}, myvector_construct('{v}'))" for i, v in rows)
        srv.sql(f"INSERT INTO {table} VALUES {values};", db=db)

    def build(index):
        return run(f"CALL mysql.myvector_index_build('{index}','id');")

    def status(index):
        rc, out, err = run(f"CALL mysql.myvector_index_status('{index}');")
        return out.replace("\\n", "\n") if rc == 0 else f"<rc={rc} {err}>"

    def ann(index, n=1):
        return run(f"SELECT myvector_ann_set('{index}', 'id', {QUERY}, "
                   f"'nn={n}');")

    def case_a():
        create("a", f"type=HNSW,dim={DIM},size=10,M=16,ef=64,dist=L2")
        insert("a", ROWS)
        rc, out, err = build("vtest.a.v")
        rc, out, err = ann("vtest.a.v")
        record("A0. setup: the L2 index returns the L2 neighbour (id 2)",
               rc == 0 and out == "[2]", f"rc={rc} out={out!r} err={err[:120]!r}")
        srv.sql("DROP TABLE a;")
        create("a", f"type=HNSW,dim={DIM},size=10,M=16,ef=64,dist=Cosine")
        insert("a", ROWS)
        rc, out, err = build("vtest.a.v")
        record("A1. the rebuild of the recreated table succeeds",
               rc == 0 and out.startswith("SUCCESS"),
               f"rc={rc} out={out[:80]!r} err={err[:120]!r}")
        st = status("vtest.a.v")
        record("A2. the status reports the new metric (Cosine)",
               "Distance : Cosine" in st, repr(st[:200]))
        rc, out, err = ann("vtest.a.v")
        record("A3. the search returns the cosine neighbour (id 1)",
               rc == 0 and out == "[1]", f"rc={rc} out={out!r} err={err[:120]!r}")

    def case_b():
        create("b", f"type=HNSW,dim={DIM},size=10,M=16,ef=64,dist=L2")
        insert("b", ROWS)
        build("vtest.b.v")
        srv.sql(f"ALTER TABLE b MODIFY {column(f'type=HNSW,dim={DIM},size=20,M=8,ef=64,dist=L2')};")
        rc, out, err = build("vtest.b.v")
        st = status("vtest.b.v")
        record("B1. a changed COMMENT is used on rebuild (M = 8, capacity 20)",
               rc == 0 and out.startswith("SUCCESS") and "M = 8" in st
               and "Max. Capacity : 20" in st,
               f"rc={rc} out={out[:60]!r} status={st[:200]!r}")

    def case_c():
        create("c", f"type=HNSW,dim={DIM},size=3,M=16,ef=64,dist=L2")
        insert("c", [(i, f"[{i},{i}]") for i in range(1, 6)])
        rc, out, err = build("vtest.c.v")
        record("C1. building 5 rows into size=3 reports an error, not SUCCESS",
               rc == 0 and out.startswith("ERROR") and "full" in out
               and "vtest.c.v" in out,
               f"rc={rc} out={out[:160]!r} err={err[:160]!r}")
        record("C2. server survives", alive(), "mysqladmin ping")

    def case_d():
        create("d", f"type=HNSW,dim={DIM},size=3,M=16,ef=64,online=Y,dist=L2")
        insert("d", [(1, "[0,0]"), (2, "[5,5]"), (3, "[9,9]")])
        rc, out, err = build("vtest.d.v")
        record("D0. setup: the online index builds at its capacity",
               rc == 0 and out.startswith("SUCCESS"), f"rc={rc} out={out[:80]!r}")
        insert("d", [(4, "[20,20]"), (5, "[30,30]")])
        time.sleep(4)  # the binlog worker applies them asynchronously
        record("D1. server survives INSERTs past size=", alive(),
               "mysqladmin ping")
        st = status("vtest.d.v")
        record("D2. the status counts the rejected rows",
               "index is full" in st and "Online events skipped : 2" in st,
               repr(st[-300:]))
        srv.sql("UPDATE d SET v = myvector_construct('[40,40]') WHERE id = 1;")
        time.sleep(4)
        rc, out, err = run(f"SELECT myvector_ann_set('vtest.d.v', 'id', "
                           f"myvector_construct('[40,40]'), 'nn=1');")
        record("D3. an UPDATE of an indexed row still applies on a full index",
               rc == 0 and out == "[1]", f"rc={rc} out={out!r} err={err[:120]!r}")

    def case_e():
        srv.sql("CREATE DATABASE e;", db=None)
        create("t", f"type=HNSW,dim={DIM},size=3,M=16,ef=64,online=Y,dist=L2",
               db="e")
        insert("t", [(1, "[0,0]"), (2, "[5,5]")], db="e")
        rc, out, err = build("e.t.v")
        record("E0. setup: the online index builds",
               rc == 0 and out.startswith("SUCCESS"), f"rc={rc} out={out[:80]!r}")
        srv.sql("DROP DATABASE e; CREATE DATABASE e;", db=None)
        create("t", f"type=HNSW,dim={DIM},size=10,M=16,ef=64,online=Y,dist=L2",
               db="e")
        insert("t", [(i, f"[{i},{i}]") for i in range(1, 9)], db="e")
        time.sleep(4)
        record("E1. server survives 8 INSERTs into the recreated table",
               alive(), "mysqladmin ping")
        record("E2. the log reports that the dropped table's index was removed",
               re.search(r"Index e\.t\.v: its table was dropped", srv.log())
               is not None, "server log")
        rc, out, err = build("e.t.v")
        rows = idle.current_rows(srv, "e.t.v")
        record("E3. the recreated table builds with all 8 rows",
               rc == 0 and out.startswith("SUCCESS") and rows == 8,
               f"rc={rc} out={out[:80]!r} rows={rows}")
        idle.sh(["docker", "restart", srv.name])
        srv.wait_ready()
        rows = idle.current_rows(srv, "e.t.v")
        record("E4. after a restart (binlog replay) the new index keeps 8 rows",
               rows == 8, f"rows={rows}")

    try:
        srv.wait_ready()
        if component:
            idle.install_component(srv, args.component_dir)
        else:
            idle.install_plugin(srv, args.plugin_dir)
        srv.sql("CREATE DATABASE vtest;", db=None)

        for c, fn in (("A", case_a), ("B", case_b), ("C", case_c),
                      ("D", case_d)):
            if c in args.cases:
                fn()
        if "E" in args.cases:
            if component:
                case_e()
            else:
                print("[SKIP] E: component only (the plugin's binlog listener"
                      " does not watch DROP statements)")
    except Exception as e:  # noqa: BLE001 - report and fail
        record("test ran to completion", False, repr(e))
    finally:
        if not all(results):
            log = idle.sh(["docker", "logs", "--tail", "60", srv.name],
                          check=False)
            print("--- server log (tail) ---")
            print((log.stdout + log.stderr)[-6000:])
        if args.keep:
            print(f"container kept: {srv.name}")
        else:
            srv.remove()

    ok = all(results) and results
    print(f"\n{'ALL PASSED' if ok else 'FAILED'} "
          f"({sum(results)}/{len(results)})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
