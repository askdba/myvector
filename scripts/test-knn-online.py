#!/usr/bin/env python3
"""Online (binlog) updates follow online=Y for KNN indexes too (#287).

KNNIndex::supportsIncrUpdates() returned true unconditionally, so every KNN
index took INSERTs from the binlog listener whether or not its column had
online=Y, contrary to docs/ONLINE_INDEX_UPDATES.md. HNSW accepted only an
upper-case online=Y, while both listeners' discovery accepts Y or y.

To see that an INSERT was *not* applied, each check inserts into the table
under test first and then into an online control table, and waits until the
control index has every new row. Row events are spread over worker queues by
key (pkid % workers), and each queue is applied in order, so the control rows
use CONTROL_ROWS consecutive keys: one in every queue for up to that many
workers. Once all are in, every queue is past the earlier INSERT.

Checks:
  A. KNN without online=Y: an INSERT is not applied (Rows Inserted unchanged).
  B. KNN with online=Y: an INSERT is applied.
  C. A column changed from online HNSW to non-online KNN (ALTER, rebuild):
     INSERTs are no longer applied.
  D. HNSW with a lower-case online=y: an INSERT is applied.
  E. Component only: a non-online KNN index whose table is dropped is removed
     (the DROP handling of #286 must not depend on online=Y).

Usage:
  python3 scripts/test-knn-online.py --plugin-dir dist/plugin-8.4
  python3 scripts/test-knn-online.py \\
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

DIM = 4
CONTROL_ROWS = 16  # >= the binlog worker count (component default 2)


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
    srv = idle.Server(args.image, f"myv-knn-online-{os.getpid()}")
    results = []
    next_id = [100]

    def record(name, ok, detail):
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")

    def run(q, db="vtest"):
        cmd = ["docker", "exec", "-i", srv.name, "mysql", "-uroot",
               f"-p{idle.PW}", "-N", "-B"]
        if db:
            cmd.append(db)
        r = idle.sh(cmd, inp=q, check=False)
        return r.returncode, r.stdout.strip(), r.stderr.strip()

    def column(opts):
        if component:
            return (f"v VARBINARY({DIM * 4 + 64}) NULL COMMENT "
                    f"'MYVECTOR COLUMN {opts},idcol=id'")
        return f"v MYVECTOR({opts},idcol=id) NULL"

    def create(table, opts):
        srv.sql(f"DROP TABLE IF EXISTS {table}; CREATE TABLE {table} "
                f"(id INT PRIMARY KEY, {column(opts)});")

    def insert(table, n=1):
        values = []
        for _ in range(n):
            next_id[0] += 1
            i = next_id[0]
            values.append(f"({i}, myvector_construct('[{i % 7},1,2,3]'))")
        srv.sql(f"INSERT INTO {table} VALUES {', '.join(values)};")

    def build(table):
        rc, out, err = run(f"CALL mysql.myvector_index_build('vtest.{table}.v','id');")
        return rc == 0 and out.startswith("SUCCESS"), out[:80] or err[:120]

    def rows(table):
        """Rows in the index: 'Rows Inserted' (KNN) or 'Current Rows' (HNSW)."""
        rc, out, _ = run(f"CALL mysql.myvector_index_status('vtest.{table}.v');")
        m = re.search(r"(?:Rows Inserted|Current Rows) : (\d+)",
                      out.replace("\\n", "\n"))
        return int(m.group(1)) if m else -1

    def wait_rows(table, want, timeout=60):
        deadline = time.time() + timeout
        while True:
            n = rows(table)
            if n == want or time.time() >= deadline:
                return n
            time.sleep(1)

    try:
        srv.wait_ready()
        if component:
            idle.install_component(srv, args.component_dir)
        else:
            idle.install_plugin(srv, args.plugin_dir)
        srv.sql("CREATE DATABASE vtest;", db=None)

        # Online control table: once it has a row, the listener has applied
        # every event before that row's INSERT.
        create("ctl", f"type=HNSW,dim={DIM},size=1000,online=Y,dist=L2")
        insert("ctl")
        ok, out = build("ctl")
        record("setup: online control index builds", ok, out)
        ctl = rows("ctl")

        def control_step():
            nonlocal ctl
            insert("ctl", CONTROL_ROWS)
            ctl += CONTROL_ROWS
            got = wait_rows("ctl", ctl)
            if got != ctl:
                raise RuntimeError(f"control index did not reach {ctl} rows "
                                   f"(has {got}): listener not applying events")

        # A. KNN, no online=Y
        create("a", f"type=KNN,dim={DIM},dist=L2")
        insert("a", 3)
        ok, out = build("a")
        before = rows("a")
        insert("a", 2)
        control_step()
        after = rows("a")
        record("A. KNN without online=Y: INSERT not applied",
               ok and before == 3 and after == 3,
               f"build={out!r} rows {before} -> {after}")

        # B. KNN with online=Y
        create("b", f"type=KNN,dim={DIM},online=Y,dist=L2")
        insert("b", 3)
        ok, out = build("b")
        insert("b", 2)
        control_step()
        got = wait_rows("b", 5, timeout=15)
        record("B. KNN with online=Y: INSERT applied", ok and got == 5,
               f"build={out!r} rows={got}")

        # C. online HNSW -> non-online KNN
        create("c", f"type=HNSW,dim={DIM},size=1000,online=Y,dist=L2")
        insert("c", 3)
        build("c")
        srv.sql(f"ALTER TABLE c MODIFY {column(f'type=KNN,dim={DIM},dist=L2')};")
        ok, out = build("c")
        before = rows("c")
        insert("c", 2)
        control_step()
        after = rows("c")
        record("C. online HNSW changed to non-online KNN: INSERT not applied",
               ok and before == 3 and after == 3,
               f"build={out!r} rows {before} -> {after}")

        # D. HNSW with a lower-case online=y
        create("d", f"type=HNSW,dim={DIM},size=1000,online=y,dist=L2")
        insert("d", 3)
        ok, out = build("d")
        insert("d", 2)
        control_step()
        got = wait_rows("d", 5, timeout=15)
        record("D. HNSW with online=y (lower case): INSERT applied",
               ok and got == 5, f"build={out!r} rows={got}")

        # E. component: a dropped table's non-online KNN index is removed
        if component:
            create("e", f"type=KNN,dim={DIM},dist=L2")
            insert("e", 3)
            ok, out = build("e")
            srv.sql("DROP TABLE e;")
            control_step()  # the listener has passed the DROP
            evicted = re.search(r"Index vtest\.e\.v: its table was dropped",
                                srv.log()) is not None
            record("E. non-online KNN index of a dropped table is removed",
                   ok and evicted, f"build={out!r} evicted={evicted}")
        else:
            print("[SKIP] E: component only (the plugin's binlog listener does"
                  " not watch DROP statements)")
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
