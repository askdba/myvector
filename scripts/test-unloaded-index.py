#!/usr/bin/env python3
"""Searching an HNSW index that has no graph, and reporting failed loads (#265).

An index can be registered in the collection without a graph:
VectorIndexCollection::open() creates it and puts it in the collection with no
initIndex()/loadIndex(), for example when MYVECTOR_INDEX_STATUS is called on an
index that was never built or loaded. A search on it then called through a null
m_alg_hnsw and crashed mysqld. Separately, MYVECTOR_INDEX_LOAD on corrupt index
files fell back to an empty index and reported SUCCESS, so searches returned
nothing, silently.

Checks:
  A. MYVECTOR_INDEX_STATUS on a never-built index, then an ANN search: the
     server survives and the search fails with an error naming the index.
  B. MYVECTOR_INDEX_LOAD on corrupt files: the load reports an error, not
     SUCCESS; the server survives; a search fails with an error instead of
     silently returning no rows.
  C. Control: with the files restored, MYVECTOR_INDEX_LOAD reports SUCCESS and
     the search returns rows.
  D. Plugin only (its myvector_index_dir can be changed at runtime): the index
     files exist but their directory can't be traversed (EACCES). The load must
     report an error, not start an empty index and report SUCCESS. Only a
     missing file (ENOENT) means "no saved index".

Usage:
  python3 scripts/test-unloaded-index.py --plugin-dir dist/plugin-8.4
  python3 scripts/test-unloaded-index.py \\
      --component-dir dist/component-9.7 --image mysql:9.7

Exit 0 = all checks passed.
"""

import argparse
import importlib.util
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "online_idle", os.path.join(HERE, "test-online-updates-idle.py"))
idle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(idle)

DIM = 4
SIZE = 1000
ROWS = 200
INDEX = "vtest.t.v"
QUERY_VEC = "myvector_construct('[0.1,0.2,0.3,0.4]')"


def main():
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--plugin-dir", default="dist/plugin-8.4")
    mode.add_argument("--component-dir",
                      help="test a component build instead of the plugin")
    ap.add_argument("--image", default="mysql:8.4")
    ap.add_argument("--keep", action="store_true",
                    help="keep the container for debugging")
    ap.add_argument("--cases", default="ABCD",
                    help="which checks to run (default ABCD; D is plugin"
                         " only). Without the fix, A crashes the server, so use"
                         " --cases BC to see B")
    args = ap.parse_args()

    component = bool(args.component_dir)
    srv = idle.Server(args.image, f"myv-unloaded-{os.getpid()}")
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

    def search(table_index=INDEX):
        return run(f"SELECT myvector_ann_set('{table_index}', 'id', "
                   f"{QUERY_VEC}, 'nn=3');")

    def find_index_file():
        r = idle.sh(["docker", "exec", srv.name, "bash", "-c",
                     "find /var/lib/mysql -iname '*vtest.t.v.hnsw.index'"],
                    check=False)
        paths = [p for p in r.stdout.splitlines() if p]
        if not paths:
            raise RuntimeError("no .hnsw.index file found under /var/lib/mysql")
        return paths[0]

    def create_table(name):
        if component:
            srv.sql(f"""CREATE TABLE {name} (id INT PRIMARY KEY,
                v VARBINARY({DIM * 4 + 64}) NULL COMMENT 'MYVECTOR COLUMN """
                    f"""type=hnsw,dim={DIM},size={SIZE},idcol=id,dist=L2');""")
        else:
            srv.sql(f"""CREATE TABLE {name} (id INT PRIMARY KEY,
                v MYVECTOR(type=HNSW,dim={DIM},size={SIZE},idcol=id,dist=L2)
                NULL);""")

    def fill(name):
        rnd = random.Random(1)
        values = ", ".join(
            f"({i}, myvector_construct('["
            + ",".join(f"{rnd.uniform(-1, 1):.5f}" for _ in range(DIM))
            + "]'))"
            for i in range(1, ROWS + 1))
        srv.sql(f"INSERT INTO {name} VALUES {values};")

    def case_a():
        """Registered by MYVECTOR_INDEX_STATUS, never built or loaded."""
        create_table("u")
        fill("u")
        run("CALL mysql.myvector_index_status('vtest.u.v');")
        rc, out, err = search("vtest.u.v")
        record("A1. server survives a search on an index with no graph",
               alive(), f"rc={rc} err={err[:160]!r}")
        record("A2. the search fails with an error naming the index",
               rc != 0 and "vtest.u.v" in err,
               f"rc={rc} out={out[:80]!r} err={err[:160]!r}")

    def case_bc():
        """B: corrupt files must fail the load. C: restored files load again."""
        create_table("t")
        fill("t")
        rc, out, err = run(f"CALL mysql.myvector_index_build('{INDEX}','id');")
        record("setup: index builds", rc == 0 and out.startswith("SUCCESS"),
               f"rc={rc} out={out[:80]!r} err={err[:160]!r}")
        rc, out, err = search()
        record("setup: search on the built index returns rows",
               rc == 0 and out.startswith("[") and out != "[]",
               f"rc={rc} out={out[:80]!r}")

        indexfile = find_index_file()
        with tempfile.TemporaryDirectory() as tmp:
            backup = os.path.join(tmp, "backup.hnsw.index")
            idle.sh(["docker", "cp", f"{srv.name}:{indexfile}", backup])
            # Truncate the index file to its header: the loader rejects it.
            idle.sh(["docker", "exec", srv.name, "truncate", "-s", "96",
                     indexfile])
            rc, out, err = run(f"CALL mysql.myvector_index_load('{INDEX}');")
            if "B" in args.cases:
                # The load reports its failure as a status row, not an SQL error.
                record("B1. a load from corrupt files reports the load error",
                       rc == 0 and out.startswith("ERROR:")
                       and "could not be loaded" in out and INDEX in out,
                       f"rc={rc} out={out[:120]!r} err={err[:160]!r}")
                record("B2. server survives the failed load", alive(),
                       "mysqladmin ping")
                rc, out, err = search()
                record("B3. a search after the failed load fails with an error",
                       rc != 0 and INDEX in err,
                       f"rc={rc} out={out[:80]!r} err={err[:160]!r}")
                record("B4. server survives that search", alive(),
                       "mysqladmin ping")
            if "C" in args.cases:
                idle.sh(["docker", "cp", backup, f"{srv.name}:{indexfile}"])
                idle.sh(["docker", "exec", srv.name, "chown", "mysql:mysql",
                         indexfile])
                rc, out, err = run(
                    f"CALL mysql.myvector_index_load('{INDEX}');")
                record("C1. a load from good files reports SUCCESS",
                       rc == 0 and out.startswith("SUCCESS"),
                       f"rc={rc} out={out[:80]!r} err={err[:160]!r}")
                rc, out, err = search()
                record("C2. the search returns rows again",
                       rc == 0 and out.startswith("[") and out != "[]",
                       f"rc={rc} out={out[:80]!r} err={err[:160]!r}")

    def case_d():
        """Plugin only: files exist, but their directory can't be traversed."""
        sub = "/var/lib/mysql/myvidx"
        idle.sh(["docker", "exec", srv.name, "bash", "-c",
                 f"mkdir -p {sub} && chown mysql:mysql {sub}"])
        srv.sql(f"SET GLOBAL myvector_index_dir='{sub}';", db=None)
        create_table("d")
        fill("d")
        rc, out, err = run("CALL mysql.myvector_index_build('vtest.d.v','id');")
        record("D0. index builds in the subdirectory",
               rc == 0 and out.startswith("SUCCESS"),
               f"rc={rc} out={out[:80]!r} err={err[:160]!r}")
        idle.sh(["docker", "exec", srv.name, "chmod", "000", sub])
        rc, out, err = run("CALL mysql.myvector_index_load('vtest.d.v');")
        record("D1. a load whose files can't be reached reports an error",
               rc == 0 and out.startswith("ERROR:") and "vtest.d.v" in out,
               f"rc={rc} out={out[:120]!r} err={err[:160]!r}")
        rc, out, err = search("vtest.d.v")
        record("D2. the search fails with an error, not an empty result",
               rc != 0 and "vtest.d.v" in err,
               f"rc={rc} out={out[:80]!r} err={err[:160]!r}")
        idle.sh(["docker", "exec", srv.name, "chmod", "750", sub])
        record("D3. server survives", alive(), "mysqladmin ping")

    try:
        srv.wait_ready()
        if component:
            idle.install_component(srv, args.component_dir)
        else:
            idle.install_plugin(srv, args.plugin_dir)
        srv.sql("CREATE DATABASE vtest;", db=None)

        if "A" in args.cases:
            case_a()
        if "B" in args.cases or "C" in args.cases:
            case_bc()
        if "D" in args.cases:
            if component:
                print("[SKIP] D: plugin only (the component's index directory"
                      " is fixed at startup)")
            else:
                case_d()
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
