#!/usr/bin/env python3
"""Server-crash regressions found by the security audit (run-1).

Two independent entry points used to be able to take down the whole mysqld
process for any connected session, or for an ordinary index build/refresh:

  1. MYVECTOR_IS_ANN(...) with an empty quoted-identifier argument
     (e.g. "MYVECTOR_IS_ANN(a, ,c)") threw an uncaught std::out_of_range from
     an unsigned-length underflow in the pre-parse query rewriter, before
     privilege checks even run. Fix: validate the argument length before
     stripping its surrounding quotes, and catch any exception at both
     rewrite-hook call sites as defense in depth.
  2. A NULL vector (or NULL id) in a row reached by MYVECTOR_INDEX_BUILD /
     MYVECTOR_INDEX_REFRESH fell through a dead "// TODO" guard straight into
     insertVector()/atol() on a NULL pointer. Fix: skip the row instead.

This test starts a fresh mysql:<ver> container, triggers both conditions, and
checks the server survives (mysqladmin ping, and a normal query) rather than
crashing -- and that the NULL-vector row is cleanly skipped from the index
instead of corrupting the build.

Usage:
  python3 scripts/test-crash-regressions.py --plugin-dir dist/plugin-8.4
  python3 scripts/test-crash-regressions.py \\
      --component-dir dist/component-9.7 --image mysql:9.7

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

DIM = 4


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

    component = bool(args.component_dir)
    srv = idle.Server(args.image, f"myv-crash-{os.getpid()}")
    results = []

    def record(name, ok, detail):
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")

    def run(q, db="vtest"):
        """(rows, error text) for one statement batch; error text is '' on success."""
        r = idle.sh(["docker", "exec", "-i", srv.name, "mysql", "-uroot",
                     f"-p{idle.PW}", "-N", "-B"] + ([db] if db else []),
                    inp=q, check=False)
        rows = [line.split("\t") for line in r.stdout.splitlines() if line]
        err = "\n".join(l for l in r.stderr.splitlines() if "password" not in l)
        return rows, err

    def alive():
        r = idle.sh(["docker", "exec", srv.name, "mysqladmin", "ping", "-uroot",
                     f"-p{idle.PW}", "--silent"], check=False)
        return r.returncode == 0

    try:
        srv.wait_ready()
        if component:
            idle.install_component(srv, args.component_dir)
        else:
            idle.install_plugin(srv, args.plugin_dir)
        srv.sql("CREATE DATABASE vtest;", db=None)

        # --- 1. malformed MYVECTOR_IS_ANN must not crash the server ---------
        _, err = run("SELECT 1 FROM dual WHERE MYVECTOR_IS_ANN(a, ,c);")
        record("1a. malformed MYVECTOR_IS_ANN fails cleanly (not a crash)",
               bool(err), f"err={err!r}")
        record("1b. server still answers ping after the malformed statement",
               alive(), "mysqladmin ping")
        rows, err2 = run("SELECT 1;")
        record("1c. server still executes a normal query afterwards",
               rows == [["1"]] and not err2, f"{rows}{' ' + err2 if err2 else ''}")

        # --- 2. NULL vector row must not crash MYVECTOR_INDEX_BUILD --------
        if component:
            # No DDL rewrite on components: declare the index in a comment.
            srv.sql(f"""CREATE TABLE t (id INT PRIMARY KEY,
                v VARBINARY({DIM * 4 + 64}) NULL COMMENT 'MYVECTOR COLUMN """
                    f"""type=hnsw,dim={DIM},size=100,idcol=id,dist=L2');""")
        else:
            srv.sql(f"""CREATE TABLE t (id INT PRIMARY KEY,
                v MYVECTOR(type=HNSW,dim={DIM},size=100) NULL);""")
        srv.sql(f"""INSERT INTO t VALUES
            (1, myvector_construct('[1,1,1,1]')),
            (2, NULL),
            (3, myvector_construct('[2,2,2,2]')),
            (4, myvector_construct('[3,3,3,3]'));""")

        build_rows, build_err = run("CALL mysql.myvector_index_build('vtest.t.v','id');")
        record("2a. MYVECTOR_INDEX_BUILD with a NULL vector row completes",
               not build_err and build_rows and build_rows[0]
               and build_rows[0][0].startswith("SUCCESS"),
               build_err or f"rows={build_rows}")
        record("2b. server still answers ping after the build",
               alive(), "mysqladmin ping")

        n = idle.current_rows(srv, "vtest.t.v")
        record("2c. the 3 non-NULL rows are indexed, the NULL row is skipped",
               n == 3, f"Current Rows = {n} (want 3)")
    finally:
        if not args.keep:
            srv.remove()

    print("PASSED" if results and all(results) else "FAILED")
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
