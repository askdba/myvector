#!/usr/bin/env python3
"""myvector_distance() / myvector_display() NULL and error handling (#170, #171).

Starts a fresh mysql:<ver> container, installs a MyVector build (plugin or
component), and checks:

  1. A NULL vector gives NULL for that row only; later rows are computed.
  2. A NULL metric (from a column) gives NULL for that row only.
  3. An unknown constant metric fails the statement, with a clear message.
  4. An unknown metric from a column fails the statement (ER_UDF_ERROR).
  5. Vectors of different dimensions fail the statement, naming both.
  6. myvector_display() of a NULL gives NULL for that row only.
  7. L2, EUCLIDEAN, Cosine and IP still work, in any case.

Usage:
  python3 scripts/test-distance-udf.py --plugin-dir dist/plugin-8.4 --image mysql:8.4
  python3 scripts/test-distance-udf.py --component-dir dist/component-9.7 --image mysql:9.7

Exit 0 = all checks passed.
"""

import argparse
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "online_idle", os.path.join(HERE, "test-online-updates-idle.py"))
idle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(idle)


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

    srv = idle.Server(args.image, f"myv-distance-{os.getpid()}")
    results = []

    def record(name, ok, detail):
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")

    def run(q):
        """(rows, error text) for one statement batch; error text is '' on success."""
        r = idle.sh(["docker", "exec", "-i", srv.name, "mysql", "-uroot",
                     f"-p{idle.PW}", "-N", "-B", "vtest"], inp=q, check=False)
        rows = [line.split("\t") for line in r.stdout.splitlines() if line]
        err = "\n".join(l for l in r.stderr.splitlines() if "password" not in l)
        return rows, err

    try:
        srv.wait_ready()
        if args.component_dir:
            idle.install_component(srv, args.component_dir)
        else:
            idle.install_plugin(srv, args.plugin_dir)
        srv.sql("CREATE DATABASE vtest;", db=None)
        srv.sql("""
            CREATE TABLE p (id INT PRIMARY KEY, v VARBINARY(64), m VARCHAR(16));
            INSERT INTO p VALUES
              (1, myvector_construct('[1,1]'), 'L2'),
              (2, NULL,                        'L2'),
              (3, myvector_construct('[2,2]'), NULL),
              (4, myvector_construct('[3,3]'), 'cosine'),
              (5, myvector_construct('[4,4]'), 'L2');
        """)

        rows, err = run("SELECT id, myvector_distance(v, myvector_construct('[0,0]'), 'L2') "
                        "FROM p ORDER BY id;")
        got = [(r[0], r[1]) for r in rows]
        want = [("1", "2"), ("2", "NULL"), ("3", "8"), ("4", "18"), ("5", "32")]
        record("1. NULL vector -> NULL for that row only", got == want and not err,
               f"{got}{' ' + err if err else ''}")

        rows, err = run("SELECT id, myvector_distance(v, myvector_construct('[0,0]'), m) "
                        "FROM p WHERE id IN (1, 3, 5) ORDER BY id;")
        got = [(r[0], r[1]) for r in rows]
        record("2. NULL metric (column) -> NULL for that row only",
               got == [("1", "2"), ("3", "NULL"), ("5", "32")] and not err,
               f"{got}{' ' + err if err else ''}")

        rows, err = run("SELECT myvector_distance(v, myvector_construct('[0,0]'), 'MANHATTAN') FROM p;")
        record("3. unknown constant metric fails the statement",
               not rows and "unknown distance metric 'MANHATTAN'" in err, err or f"rows={rows}")

        srv.sql("UPDATE p SET m = 'manhattan' WHERE id = 4;")
        rows, err = run("SELECT id, myvector_distance(v, myvector_construct('[0,0]'), m) "
                        "FROM p WHERE id IN (1, 4, 5) ORDER BY id;")
        record("4. unknown metric from a column fails the statement",
               "myvector_distance UDF failed" in err and "unknown distance metric 'manhattan'" in err,
               err or f"rows={rows} (no error)")

        rows, err = run("SELECT myvector_distance(myvector_construct('[1,2,3]'), "
                        "myvector_construct('[1,2]'), 'L2');")
        record("5. different dimensions fail the statement",
               "myvector_distance UDF failed" in err and "different dimensions (3 and 2)" in err,
               err or f"rows={rows} (no error)")

        rows, err = run("SELECT id, myvector_display(v) FROM p WHERE id IN (1, 2, 3) ORDER BY id;")
        got = [(r[0], r[1]) for r in rows]
        record("6. myvector_display(NULL) -> NULL for that row only",
               got == [("1", "[1, 1]"), ("2", "NULL"), ("3", "[2, 2]")] and not err,
               f"{got}{' ' + err if err else ''}")

        rows, err = run("SELECT myvector_distance(myvector_construct('[1,0]'), myvector_construct('[0,1]'), 'l2'),"
                        " myvector_distance(myvector_construct('[1,0]'), myvector_construct('[0,1]'), 'EUCLIDEAN'),"
                        " myvector_distance(myvector_construct('[1,0]'), myvector_construct('[0,1]'), 'Cosine'),"
                        " myvector_distance(myvector_construct('[1,0]'), myvector_construct('[0,1]'), 'ip'),"
                        " myvector_distance(myvector_construct('[1,0]'), myvector_construct('[0,1]'));")
        got = rows[0] if rows else []
        record("7. L2 / EUCLIDEAN / Cosine / IP / default", got == ["2", "2", "1", "1", "2"] and not err,
               f"{got}{' ' + err if err else ''}")
    finally:
        if not args.keep:
            srv.remove()

    print("PASSED" if results and all(results) else "FAILED")
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
