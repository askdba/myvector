#!/usr/bin/env python3
"""Index-governance security fixes found by the security audit (run-1, slice 2).

Three independent fixes, verified together since the first closes the
prerequisite for the other two:

  1. Access control: myvector_search_open_udf is an ordinary SQL-callable
     function, so a caller could invoke it directly and skip
     MYVECTOR_INDEX_INTERNAL's INFORMATION_SCHEMA validation entirely --
     opening/building/dropping an index under an arbitrary name with no real
     backing column. Fix: myvector_open_index_impl now independently
     re-validates against information_schema for every externally-triggered
     call, using the column's real comment instead of whatever the caller
     claimed.
  2. SQL injection: pkidcolumn and a `track=` column-comment option were
     interpolated unescaped into SQL built by BuildMyVectorIndexSQL /
     GetBaseTableColumnPositions, executed over the internal myvector.cnf
     connection. Fix: pkidcolumn is now validated as a real column of the
     base table (closing it off at the access-control layer above), and
     every identifier is additionally backtick-quoted (or escaped, where
     used as a string literal) before reaching SQL text, as defense in depth.
  3. Path traversal: an unsanitized index id (vecid, ~ the column name) was
     concatenated into on-disk index file paths with no filtering. Fix:
     MyVectorIndexBase now neutralizes path separators unconditionally.

This test starts a fresh mysql:<ver> container and checks:
  - the ordinary build/status/refresh/drop workflow still works exactly as
    before (no behavior change for well-formed input);
  - a direct call naming a nonexistent table is rejected, not silently
    "succeeding";
  - an injected pkidcolumn is rejected rather than executed as SQL;
  - a column name containing a path-traversal sequence does not let the
    server write/delete a file outside myvector_index_dir.

Usage:
  python3 scripts/test-index-governance.py --plugin-dir dist/plugin-8.4
  python3 scripts/test-index-governance.py \\
      --component-dir dist/component-9.7 --image mysql:9.7

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
    srv = idle.Server(args.image, f"myv-gov-{os.getpid()}")
    results = []

    def record(name, ok, detail):
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")

    def run(q, db="vtest"):
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

        # --- 1. ordinary workflow is unaffected -----------------------------
        if component:
            srv.sql(f"""CREATE TABLE t (id INT PRIMARY KEY,
                v VARBINARY({DIM * 4 + 64}) COMMENT 'MYVECTOR COLUMN """
                    f"""type=hnsw,dim={DIM},size=100,idcol=id,dist=L2,track=ts',
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP);""")
        else:
            # The plugin's inline MYVECTOR(...) DDL rewrite auto-adds the
            # tracking column itself when track= is given; adding it again
            # here would be a duplicate-column error.
            srv.sql(f"""CREATE TABLE t (id INT PRIMARY KEY,
                v MYVECTOR(type=HNSW,dim={DIM},size=100,track=ts));""")
        srv.sql(f"""INSERT INTO t (id, v) VALUES
            (1, myvector_construct('[1,1,1,1]')),
            (2, myvector_construct('[2,2,2,2]')),
            (3, myvector_construct('[3,3,3,3]'));""")

        build_rows, build_err = run("CALL mysql.myvector_index_build('vtest.t.v','id');")
        record("1a. ordinary MYVECTOR_INDEX_BUILD still succeeds",
               not build_err and build_rows and build_rows[0]
               and build_rows[0][0].startswith("SUCCESS"),
               build_err or f"rows={build_rows}")

        n = idle.current_rows(srv, "vtest.t.v")
        record("1b. all 3 rows are indexed", n == 3, f"Current Rows = {n} (want 3)")

        # The refresh tracking column has second granularity and the build
        # above just set previous_ts = now(); sleep past the second boundary
        # so this row's timestamp is strictly greater, not tied.
        time.sleep(1.5)
        srv.sql("INSERT INTO t (id, v) VALUES (4, myvector_construct('[4,4,4,4]'));")
        refresh_rows, refresh_err = run(
            "CALL mysql.myvector_index_refresh('vtest.t.v','id');")
        record("1c. ordinary MYVECTOR_INDEX_REFRESH still succeeds",
               not refresh_err and refresh_rows and refresh_rows[0]
               and refresh_rows[0][0].startswith("SUCCESS"),
               refresh_err or f"rows={refresh_rows}")
        n = idle.current_rows(srv, "vtest.t.v")
        record("1d. the refreshed row is indexed too", n == 4, f"Current Rows = {n} (want 4)")

        status_rows, status_err = run("CALL mysql.myvector_index_status('vtest.t.v');")
        record("1e. MYVECTOR_INDEX_STATUS still works",
               not status_err and status_rows, status_err or f"rows={status_rows}")

        drop_rows, drop_err = run("CALL mysql.myvector_index_drop('vtest.t.v');")
        record("1f. MYVECTOR_INDEX_DROP still works", not drop_err, drop_err or f"rows={drop_rows}")

        # Rebuild for the remaining checks.
        run("CALL mysql.myvector_index_build('vtest.t.v','id');")

        # --- 2. access-control bypass is closed -----------------------------
        bypass_rows, bypass_err = run(
            "SELECT myvector_search_open_udf("
            "'vtest.nonexistent_table.nonexistent_col', "
            "'MYVECTOR COLUMN type=HNSW,dim=4', 'id', 'build', '');")
        bypass_result = bypass_rows[0][0] if bypass_rows else ""
        record("2a. direct UDF call naming a nonexistent table is rejected",
               (not bypass_err) and bypass_result.startswith("ERROR"),
               f"result={bypass_result!r} err={bypass_err!r}")
        record("2b. server still answers ping after the rejected call",
               alive(), "mysqladmin ping")

        # --- 3. SQL injection via pkidcolumn is closed ----------------------
        inj_rows, inj_err = run(
            "CALL mysql.myvector_index_build("
            "'vtest.t.v', 'id FROM mysql.user -- ');")
        # Either the CALL itself errors (quoting broke the injected SQL into a
        # syntax error) or the procedure reports a clean validation failure;
        # what must NOT happen is a silent SUCCESS (which would mean the
        # injected text was executed as-is) or a crash.
        inj_status = inj_rows[0][0] if inj_rows else ""
        record("3a. injected pkidcolumn is not silently accepted",
               bool(inj_err) or not inj_status.startswith("SUCCESS"),
               f"status={inj_status!r} err={inj_err!r}")
        record("3b. server still answers ping after the injection attempt",
               alive(), "mysqladmin ping")

        # --- 4. path traversal via a crafted column name is closed ----------
        if component:
            srv.sql("""CREATE TABLE t2 (id INT PRIMARY KEY,
                `v/../../../../tmp/evil` VARBINARY(80) COMMENT """
                    """'MYVECTOR COLUMN type=hnsw,dim=4,size=10,idcol=id,dist=L2');""",
                    check=False)
        else:
            srv.sql("""CREATE TABLE t2 (id INT PRIMARY KEY,
                `v/../../../../tmp/evil` MYVECTOR(type=HNSW,dim=4,size=10));""",
                    check=False)
        traversal_rows, traversal_err = run(
            "CALL mysql.myvector_index_build("
            "'vtest.t2.v/../../../../tmp/evil', 'id');", db=None)
        record("4a. server survives a build against a slash-containing column name",
               alive(), f"err={traversal_err!r}")
        evil_check = idle.sh(["docker", "exec", srv.name, "sh", "-c",
                              "ls /tmp/evil* /evil* 2>/dev/null; true"], check=False)
        record("4b. no index file was written outside myvector_index_dir",
               not evil_check.stdout.strip(), f"stray files: {evil_check.stdout!r}")
    finally:
        if not args.keep:
            srv.remove()

    print("PASSED" if results and all(results) else "FAILED")
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
