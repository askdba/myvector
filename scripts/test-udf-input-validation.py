#!/usr/bin/env python3
"""UDF input-length validation regressions found by the security audit (run-1,
Slice 3).

Three independent gaps let an ordinary SQL client corrupt memory or read out
of bounds inside the mysqld worker thread, with no special privileges:

  1. myvector_construct()/myvector_construct_bv() never clamped an unbounded
     numeric token against its 64-byte stack buffer (stack overflow), never
     checked for a missing closing bracket before scanning past the string's
     NUL terminator (OOB read), and never checked the overall converted
     length against the 128000-byte output buffer (heap overflow). Fix:
     clamp/check all of these before every write.
  2. myvector_ann_set() read a fixed stride (the index's own dimension) from
     the caller-supplied query vector argument with no check that the
     argument was actually that long (OOB read). Fix: validate the argument's
     real byte length first, in both the plugin's and the component's
     independent implementations.
  3. insertVector() ignores the dim it is given and always reads a fixed
     stride from whatever raw pointer it receives; nothing validated that an
     online-tracked column, a build/refresh SELECT, or a direct add-row UDF
     call actually held that many bytes before handing the pointer to
     insertVector() (OOB read). Fix: validate at every entry point that feeds
     raw bytes in -- the real-time binlog apply path, the bulk build/refresh
     fetch loop (both plugin and component have independent copies), and the
     add-row UDF.

This test starts a fresh mysql:<ver> container and checks the server survives
(mysqladmin ping, and a normal query) rather than crashing or corrupting
memory, and that undersized vectors are cleanly skipped/rejected instead of
being read past their real length.

Usage:
  python3 scripts/test-udf-input-validation.py --plugin-dir dist/plugin-8.4
  python3 scripts/test-udf-input-validation.py \\
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
UNDERSIZED_VEC = "0x0102"  # 2 bytes; a real dim=4 FP32 vector needs 16.


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
    srv = idle.Server(args.image, f"myv-udfval-{os.getpid()}")
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

    def rejected(rows, err):
        """True if the UDF safely rejected the input: either a visible SQL
        error (the plugin build's MYVECTOR_UDF_FAIL calls my_error()) or a
        clean NULL result (the component build's pre-existing convention:
        its own UDF service logs server-side via MYVEC_LOG_ERROR and sets
        *error without raising a client-visible error -- MySQL's UDF
        contract then returns NULL for that row with no error text)."""
        return bool(err) or not rows or rows in ([["NULL"]], [[None]])

    def assert_alive_and_responsive(label):
        record(f"{label}: server still answers ping", alive(), "mysqladmin ping")
        rows, err = run("SELECT 1;")
        record(f"{label}: server still executes a normal query",
               rows == [["1"]] and not err, f"{rows}{' ' + err if err else ''}")

    def wait_rows(index, want, timeout=20):
        deadline = time.time() + timeout
        n = -1
        while time.time() < deadline:
            n = idle.current_rows(srv, index)
            if n == want:
                return n
            time.sleep(0.5)
        return n

    try:
        srv.wait_ready()
        if component:
            idle.install_component(srv, args.component_dir)
        else:
            idle.install_plugin(srv, args.plugin_dir)
        srv.sql("CREATE DATABASE vtest;", db=None)

        # Wrap every direct SELECT of myvector_construct()'s result in HEX():
        # its real return value is a binary blob, and the mysql CLI prints
        # that blob's raw bytes to stdout -- not an encoding we can safely
        # decode as text, and not what these checks care about anyway.

        # --- 1a. oversized single token must not overflow the 64-byte stack
        # buffer in the string tokenizer, and is now rejected rather than
        # silently truncated and parsed (which could overflow atof() to Inf
        # with *error left at 0 -- review of audit run-1, Slice 3) ----------
        rows, err = run("SELECT HEX(myvector_construct(CONCAT('[', REPEAT('1', 1000), ']')));")
        record("1a. oversized numeric token in myvector_construct is rejected",
               rejected(rows, err), f"rows={rows} err={err!r}")
        assert_alive_and_responsive("1a")

        # --- 1b. missing closing bracket must not scan past the end of the
        # argument's real length (not just rely on finding a NUL byte -- not
        # guaranteed for a BLOB/VARBINARY argument) and must now be rejected
        # rather than silently accepted (review of audit run-1, Slice 3) ----
        rows, err = run("SELECT HEX(myvector_construct('[1,2,3'));")
        record("1b. unterminated literal in myvector_construct is rejected",
               rejected(rows, err), f"rows={rows} err={err!r}")
        assert_alive_and_responsive("1b")

        # --- 1c. oversized raw bv input must not overflow the 128000-byte
        # output buffer -------------------------------------------------------
        rows, err = run(
            "SELECT HEX(myvector_construct(REPEAT(0x41, 200000), 'i=bv,o=bv'));")
        record("1c. oversized i=bv,o=bv myvector_construct input is rejected",
               rejected(rows, err), f"rows={rows} err={err!r}")
        assert_alive_and_responsive("1c")

        # --- 1d. oversized raw float input, same output buffer -------------
        rows, err = run(
            "SELECT HEX(myvector_construct(REPEAT(0x00, 200000), 'i=float,o=float'));")
        record("1d. oversized i=float,o=float myvector_construct input is rejected",
               rejected(rows, err), f"rows={rows} err={err!r}")
        assert_alive_and_responsive("1d")

        # --- 1e. a non-string first argument must be rejected outright: MySQL
        # does not maintain args->lengths[] for a numeric argument, so the
        # i=float path could derive a dim from a "length" that has nothing to
        # do with the 8-byte buffer args->args[0] actually points to (review
        # of audit run-1, Slice 3) -------------------------------------------
        rows, err = run("SELECT HEX(myvector_construct(12345));")
        record("1e. a non-string myvector_construct argument is rejected",
               rejected(rows, err), f"rows={rows} err={err!r}")
        assert_alive_and_responsive("1e")

        # --- 1f. an out-of-range byte-value token must be rejected, not
        # silently narrowed by an unchecked atoi()/atof() (review of audit
        # run-1, Slice 3) -----------------------------------------------------
        rows, err = run(
            "SELECT HEX(myvector_construct('[9999999999]', 'i=string,o=bv'));")
        record("1f. an out-of-range byte-value token is rejected",
               rejected(rows, err), f"rows={rows} err={err!r}")
        assert_alive_and_responsive("1f")

        # --- Set up an index for the remaining checks -----------------------
        if component:
            srv.sql(f"""CREATE TABLE t (id INT PRIMARY KEY,
                v VARBINARY({DIM * 4 + 64}) NULL COMMENT 'MYVECTOR COLUMN """
                    f"""type=hnsw,dim={DIM},size=100,idcol=id,dist=L2,online=Y');""")
        else:
            srv.sql(f"""CREATE TABLE t (id INT PRIMARY KEY,
                v MYVECTOR(type=HNSW,dim={DIM},size=100,idcol=id,dist=L2,online=Y) NULL);""")
        srv.sql(f"""INSERT INTO t VALUES
            (1, myvector_construct('[1,1,1,1]')),
            (2, myvector_construct('[2,2,2,2]')),
            (3, myvector_construct('[3,3,3,3]')),
            (4, {UNDERSIZED_VEC});""")

        # --- 2. bulk MYVECTOR_INDEX_BUILD must skip the undersized row, not
        # read past it (BuildMyVectorIndexSQL, both plugin and component have
        # independent copies of this fetch loop) ------------------------------
        build_rows, build_err = run("CALL mysql.myvector_index_build('vtest.t.v','id');")
        record("2a. MYVECTOR_INDEX_BUILD with an undersized vector row completes",
               not build_err and build_rows and build_rows[0]
               and build_rows[0][0].startswith("SUCCESS"),
               build_err or f"rows={build_rows}")
        assert_alive_and_responsive("2a")
        n = wait_rows("vtest.t.v", 3)
        record("2b. the 3 well-formed rows are indexed, the undersized row is skipped",
               n == 3, f"Current Rows = {n} (want 3)")

        # --- 3. myvector_ann_set with an undersized query vector must not
        # read past it ------------------------------------------------------
        rows, err = run(
            f"SELECT myvector_ann_set('vtest.t.v', 'id', {UNDERSIZED_VEC}, 'nn=5');")
        record("3a. myvector_ann_set with an undersized query vector is rejected",
               rejected(rows, err), f"rows={rows} err={err!r}")
        assert_alive_and_responsive("3a")

        # --- 3b. a numeric (non-string) query-vector argument must also be
        # rejected: MySQL does not maintain args->lengths[] for it, so the
        # length check above could otherwise pass on a bogus "length" while
        # args->args[2] only ever holds the 8-byte numeric value (review of
        # audit run-1, Slice 3) -----------------------------------------------
        rows, err = run(
            "SELECT myvector_ann_set('vtest.t.v', 'id', 1234567890123, 'nn=5');")
        record("3b. myvector_ann_set with a numeric query-vector argument is rejected",
               rejected(rows, err), f"rows={rows} err={err!r}")
        assert_alive_and_responsive("3b")

        # --- 4. the real-time binlog apply path (myvector_table_op) must also
        # skip an undersized row written directly (bypassing myvector_construct),
        # while a well-formed row inserted alongside it is still applied -------
        srv.sql(f"""INSERT INTO t VALUES (5, {UNDERSIZED_VEC}), (6, myvector_construct('[4,4,4,4]'));""")
        n = wait_rows("vtest.t.v", 4)
        record("4a. online apply skips the undersized row but applies the good one",
               n == 4, f"Current Rows = {n} (want 4: 3 from build + row 6)")
        assert_alive_and_responsive("4a")

        # --- 5. sanity: ordinary construct/build/search still works ---------
        nn_rows, nn_err = run(
            "SELECT myvector_ann_set('vtest.t.v', 'id', "
            "myvector_construct('[1,1,1,1]'), 'nn=1');")
        record("5. ordinary myvector_ann_set still returns a result",
               not nn_err and nn_rows and nn_rows[0] and nn_rows[0][0] == "[1]",
               f"rows={nn_rows}{' err=' + nn_err if nn_err else ''}")

        # --- 6. HNSW_BV with a dim not a multiple of 8 must not reject a
        # correctly-sized vector: the real per-vector storage size is
        # floor(dim/8) bytes (matches HammingBinaryVectorSpace::data_size_ and
        # the bv_literal() helper other tests already use); the length check
        # added for finding #2 above must use the same floor, not a ceiling
        # that would demand one byte more than any real vector ever has ----
        BV_DIM = 20  # floor(20/8) = 2 bytes/vector; a ceiling would wrongly
                     # demand 3 and reject every well-formed row
        if component:
            srv.sql(f"""CREATE TABLE tbv (id INT PRIMARY KEY,
                v VARBINARY(64) NULL COMMENT 'MYVECTOR COLUMN """
                    f"""type=hnsw_bv,dim={BV_DIM},size=100,idcol=id');""")
        else:
            srv.sql(f"""CREATE TABLE tbv (id INT PRIMARY KEY,
                v MYVECTOR(type=HNSW_BV,dim={BV_DIM},size=100,idcol=id) NULL);""")
        srv.sql("""INSERT INTO tbv VALUES
            (1, myvector_construct('[10,20]', 'i=string,o=bv')),
            (2, myvector_construct('[30,40]', 'i=string,o=bv'));""")
        build_rows, build_err = run(
            "CALL mysql.myvector_index_build('vtest.tbv.v','id');")
        record("6a. HNSW_BV build with dim not a multiple of 8 completes",
               not build_err and build_rows and build_rows[0]
               and build_rows[0][0].startswith("SUCCESS"),
               build_err or f"rows={build_rows}")
        n = wait_rows("vtest.tbv.v", 2)
        record("6b. both correctly-sized (floor(dim/8)-byte) rows are "
               "indexed, not rejected as undersized",
               n == 2, f"Current Rows = {n} (want 2)")
        rows, err = run(
            "SELECT myvector_ann_set('vtest.tbv.v', 'id', "
            "myvector_construct('[10,20]', 'i=string,o=bv'), 'nn=2');")
        record("6c. myvector_ann_set accepts a correctly-sized HNSW_BV query vector",
               not err and rows and rows[0], f"rows={rows}{' err=' + err if err else ''}")
    finally:
        if not args.keep:
            srv.remove()

    print("PASSED" if results and all(results) else "FAILED")
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
