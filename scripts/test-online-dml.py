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
  5. An UPDATE written with binlog_row_image=MINIMAL is skipped, not misread,
     and the skip is logged once and counted in MYVECTOR_INDEX_STATUS (#205).
  6. A table whose key column is not an integer (DOUBLE): its events are
     skipped with a warning naming the key type (#205).

With --key-type bigint the key column is BIGINT and every key is above 2^32,
so a key truncated to 32 bits would fail checks 1-4 (#204).

The table has TINYINT, DECIMAL, DATETIME(3), JSON, CHAR, ENUM and TEXT columns
around the vector, so the row parser must know each column's width.

Usage:
  python3 scripts/test-online-dml.py --plugin-dir dist/plugin-8.4 --image mysql:8.4
  python3 scripts/test-online-dml.py --plugin-dir dist/plugin-8.4 --key-type bigint
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
    ap.add_argument("--key-type", choices=("int", "bigint"), default="int",
                    help="type of the key column (bigint: keys above 2^32, #204)")
    args = ap.parse_args()
    # Every key is BASE + a small number: with BIGINT, past 2^32, so a key cut
    # to 32 bits would point at another row.
    BASE = 5_000_000_000 if args.key_type == "bigint" else 0
    KEYTYPE = args.key_type.upper()

    def K(k):
        return BASE + k

    def one(k):
        return f"[{K(k)}]"

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

    def status(index):
        r = srv.sql(f"CALL mysql.myvector_index_status('{index}');", check=False)
        return "\n".join(str(c) for row in (r or []) for c in row).replace("\\n", "\n")

    def skip_line(st):
        return next((ln for ln in st.splitlines() if ln.startswith("Online events skipped")), "")

    def log_lines(text):
        """How many lines of the server's error log contain text."""
        out = idle.sh(["docker", "logs", srv.name], check=False)
        return sum(text in ln for ln in (out.stdout + out.stderr).splitlines())

    def nn(v, k):
        r = srv.sql(f"SELECT myvector_ann_set('{INDEX}', 'id', "
                    f"myvector_construct('{v}'), 'nn={k}');")
        return r[0][0] if r else ""

    def state_problems():
        problems = []
        if rows() != 4:
            problems.append(f"rows={rows()} (want 4)")
        for v, want in (("[0,1,0]", one(2)), ("[9,9,9]", one(3)), ("[1,1,0]", one(40))):
            got = nn(v, 1)
            if got != want:
                problems.append(f"nearest {v}={got} (want {want})")
        ids = nn("[0,0,0]", 10)
        if any(f"{sep}{K(k)}{end}" in ids for k in (4, 5) for sep in "[," for end in ",]"):
            problems.append(f"ids={ids} (4 and 5 must be gone)")
        return problems, ids

    def check_state(stage, timeout=30):
        """Poll until the index matches the table, or the deadline passes."""
        deadline = time.time() + timeout
        while True:
            problems, ids = state_problems()
            if not problems or time.time() >= deadline:
                break
            time.sleep(1)
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
        srv.sql(f"""
            CREATE TABLE t (
              id  {KEYTYPE} PRIMARY KEY,
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
              ({K(1)}, '{{"a":1}}', myvector_construct('[1,0,0]'), 'one'),
              ({K(2)}, '{{"a":2}}', myvector_construct('[0,1,0]'), 'two'),
              ({K(3)}, NULL,      myvector_construct('[0,0,1]'), NULL),
              ({K(4)}, '{{"a":4}}', myvector_construct('[1,1,0]'), 'four'),
              ({K(5)}, '{{"a":5}}', myvector_construct('[0,1,1]'), 'five');
        """)
        srv.sql(f"CALL mysql.myvector_index_build('{INDEX}', 'id');")
        got = wait_rows(5)
        if got != 5:
            record("setup", False, f"index has {got} rows after the build, expected 5")
            return 1

        srv.sql(f"DELETE FROM t WHERE id = {K(2)};")
        got = wait_rows(4)
        ids = nn("[0,1,0]", 5)
        record("1. DELETE", got == 4 and str(K(2)) not in ids.strip("[]").split(","),
               f"rows={got}, nearest to [0,1,0]: {ids}")

        srv.sql(f"""
            UPDATE t SET vec = myvector_construct('[9,9,9]') WHERE id = {K(3)};
            UPDATE t SET id = {K(40)} WHERE id = {K(4)};
            UPDATE t SET vec = NULL WHERE id = {K(5)};
            UPDATE t SET note = 'changed', t = 9 WHERE id = {K(1)};
            INSERT INTO t (id, vec) VALUES ({K(2)}, myvector_construct('[0,1,0]'));
        """)
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

        srv.sql(f"INSERT INTO t (id, vec) VALUES ({K(6)}, myvector_construct('[5,5,5]'));")
        got = wait_rows(5)
        record("4. INSERT after the restart", got == 5 and nn("[5,5,5]", 1) == one(6),
               f"rows={got}, nearest to [5,5,5]: {nn('[5,5,5]', 1)}")

        # A MINIMAL row image leaves columns out; the listener must skip the
        # event, not read it as a full image (that dropped row 1 from the index).
        srv.sql("SET SESSION binlog_row_image = 'MINIMAL'; "
                f"UPDATE t SET note = 'minimal' WHERE id = {K(1)};")
        time.sleep(5)
        got = rows()
        st = status(INDEX)
        warns = log_lines("Online updates for vtest.t: skipping row events "
                          "(binlog_row_image is not FULL")
        record("5. UPDATE with binlog_row_image=MINIMAL is skipped, logged once and counted",
               got == 5 and nn("[1,0,0]", 1) == one(1)
               and "Online events skipped : 1 (binlog_row_image is not FULL: 1)" in st
               and warns == 1,
               f"rows={got}, nearest to [1,0,0]: {nn('[1,0,0]', 1)}, "
               f"status: {skip_line(st)!r}, warnings in the log: {warns}")

        # A key column that is not an integer: the listener cannot read the key,
        # so it skips the table's events, and says why.
        srv.sql("""
            CREATE TABLE t2 (
              id  DOUBLE PRIMARY KEY,
              vec VARBINARY(256) COMMENT
                'MYVECTOR COLUMN type=hnsw,dim=3,size=100,m=16,ef=50,idcol=id,dist=L2,online=Y'
            );
            INSERT INTO t2 VALUES (1, myvector_construct('[1,0,0]'));
        """)
        srv.sql("CALL mysql.myvector_index_build('vtest.t2.vec', 'id');", check=False)
        srv.sql("INSERT INTO t2 VALUES (2, myvector_construct('[0,1,0]'));")
        warns, deadline = 0, time.time() + 20
        while warns == 0 and time.time() < deadline:
            time.sleep(1)
            warns = log_lines("Online updates for vtest.t2: skipping row events "
                              "(key column is not an integer: key column is DOUBLE")
        st2 = status("vtest.t2.vec")
        record("6. a DOUBLE key column is skipped with a warning naming the type",
               warns == 1 and ("Online events skipped" not in st2
                               or "key column is not an integer" in st2),
               f"warnings in the log: {warns}, status: {skip_line(st2)!r}")
    finally:
        if not args.keep:
            srv.remove()

    print("PASSED" if all(results) else "FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
