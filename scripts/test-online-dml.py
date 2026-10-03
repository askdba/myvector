#!/usr/bin/env python3
"""Online DELETE and UPDATE on an online=Y index, for the plugin or the component.

Starts a fresh mysql:<ver> container, installs a MyVector build, and checks that
the binlog listener applies DELETE and UPDATE, not only INSERT (#188, #194):

  1. DELETE removes the row from the index.
  2. UPDATE of the vector, UPDATE of the key, vector set to NULL, an UPDATE of
     other columns only, and a re-INSERT of a deleted key: the index matches
     the table.
  3. After FLUSH BINARY LOGS (the listener checkpoints the index) and a server
     restart, the index still matches: deletes and updates were saved to disk.
  4. An INSERT after that restart reaches the index (the listener resumed).

The table has TINYINT, DECIMAL, DATETIME(3), JSON, CHAR, ENUM and TEXT columns
around the vector, so the row parser must know each column's width.

Usage:
  python3 scripts/test-online-dml.py --plugin-dir dist/plugin-8.4 --image mysql:8.4
  python3 scripts/test-online-dml.py --component-dir dist/component-9.7 --image mysql:9.7

Exit 0 = all checks passed.
"""

import argparse
import importlib.util
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "online_idle", os.path.join(HERE, "test-online-updates-idle.py"))
idle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(idle)

INDEX = "vtest.t.vec"


def main():
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--plugin-dir", default="dist/plugin-8.4")
    mode.add_argument("--component-dir",
                      help="test a component build instead of the plugin")
    ap.add_argument("--image", default="mysql:8.4")
    ap.add_argument("--keep", action="store_true",
                    help="keep the container for debugging")
    args = ap.parse_args()

    srv = idle.Server(args.image, f"myv-online-dml-{os.getpid()}")
    results = []

    def record(name, ok, detail):
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")

    def rows():
        try:
            return idle.current_rows(srv, INDEX)
        except Exception:
            return -1

    def wait_rows(want, timeout=20):
        got = -1
        deadline = time.time() + timeout
        while time.time() < deadline:
            got = rows()
            if got == want:
                break
            time.sleep(0.5)
        return got

    def nn(v, k):
        r = srv.sql(f"SELECT myvector_ann_set('{INDEX}', 'id', "
                    f"myvector_construct('{v}'), 'nn={k}');")
        return r[0][0] if r else ""

    def check_state(stage):
        problems = []
        if rows() != 4:
            problems.append(f"rows={rows()} (want 4)")
        for v, want in (("[0,1,0]", "[2]"), ("[9,9,9]", "[3]"), ("[1,1,0]", "[40]")):
            got = nn(v, 1)
            if got != want:
                problems.append(f"nearest {v}={got} (want {want})")
        ids = nn("[0,0,0]", 10)
        if any(f"{sep}{k}{end}" in ids for k in (4, 5) for sep in "[," for end in ",]"):
            problems.append(f"ids={ids} (4 and 5 must be gone)")
        record(stage, not problems, "; ".join(problems) or f"rows=4, ids {ids}")

    try:
        srv.wait_ready()
        if args.component_dir:
            idle.install_component(srv, args.component_dir)
        else:
            idle.install_plugin(srv, args.plugin_dir)
        # Plugin: keep the index directory across the restart (no-op on the component).
        srv.sql("SET PERSIST myvector_index_dir='/var/lib/mysql';", db=None, check=False)
        srv.sql("CREATE DATABASE vtest;", db=None)
        srv.sql("""
            CREATE TABLE t (
              id  INT PRIMARY KEY,
              t   TINYINT DEFAULT 7,
              d   DECIMAL(10,3) DEFAULT 12.5,
              dt  DATETIME(3) DEFAULT '2026-10-03 10:00:00.123',
              j   JSON,
              c   CHAR(10) DEFAULT 'abc',
              e   ENUM('x','y') DEFAULT 'y',
              vec VARBINARY(256) COMMENT
                'MYVECTOR COLUMN type=hnsw,dim=3,size=1000,m=16,ef=50,idcol=id,dist=L2,online=Y',
              note TEXT
            );
            INSERT INTO t (id, j, vec, note) VALUES
              (1, '{"a":1}', myvector_construct('[1,0,0]'), 'one'),
              (2, '{"a":2}', myvector_construct('[0,1,0]'), 'two'),
              (3, NULL,      myvector_construct('[0,0,1]'), NULL),
              (4, '{"a":4}', myvector_construct('[1,1,0]'), 'four'),
              (5, '{"a":5}', myvector_construct('[0,1,1]'), 'five');
        """)
        srv.sql(f"CALL mysql.myvector_index_build('{INDEX}', 'id');")
        got = wait_rows(5)
        if got != 5:
            record("setup", False, f"index has {got} rows after the build, expected 5")
            return 1

        srv.sql("DELETE FROM t WHERE id = 2;")
        got = wait_rows(4)
        ids = nn("[0,1,0]", 5)
        record("1. DELETE", got == 4 and "2" not in ids.strip("[]").split(","),
               f"rows={got}, nearest to [0,1,0]: {ids}")

        srv.sql("""
            UPDATE t SET vec = myvector_construct('[9,9,9]') WHERE id = 3;
            UPDATE t SET id = 40 WHERE id = 4;
            UPDATE t SET vec = NULL WHERE id = 5;
            UPDATE t SET note = 'changed', t = 9 WHERE id = 1;
            INSERT INTO t (id, vec) VALUES (2, myvector_construct('[0,1,0]'));
        """)
        time.sleep(3)
        check_state("2. UPDATE / NULL / re-INSERT")

        srv.sql("FLUSH BINARY LOGS;", db=None)
        time.sleep(3)
        idle.sh(["docker", "restart", srv.name])
        srv.wait_ready()
        deadline = time.time() + 30
        while rows() < 0 and time.time() < deadline:
            time.sleep(1)
        if rows() < 0:  # not reopened by the listener: load it
            print("note: the index was not reopened at boot; loading it")
            srv.sql(f"CALL mysql.myvector_index_load('{INDEX}');", check=False)
        check_state("3. after a checkpoint and a restart")

        srv.sql("INSERT INTO t (id, vec) VALUES (6, myvector_construct('[5,5,5]'));")
        got = wait_rows(5)
        record("4. INSERT after the restart", got == 5 and nn("[5,5,5]", 1) == "[6]",
               f"rows={got}, nearest to [5,5,5]: {nn('[5,5,5]', 1)}")
    finally:
        if not args.keep:
            srv.remove()

    print("PASSED" if all(results) else "FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
