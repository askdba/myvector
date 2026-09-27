#!/usr/bin/env python3
"""Filtered ANN search for broad filters: mysql.MYVECTOR_ANN_FILTERED.

Starts a fresh mysql:<ver> container, installs a locally built plugin or
component (helpers from scripts/test-filtered-ann.py), loads random vectors,
builds an index and checks that
  CALL mysql.myvector_ann_filtered('db.t.v', 'id', @q, k, '<predicate>')
  - returns min(k, matching rows) rows, all of which pass the predicate,
  - returns them nearest first,
  - agrees with brute force (ORDER BY myvector_distance) on the top-k.

Filters cover each path of the procedure: broad (90%, 50%: the first fetch is
enough), selective (1%: needs retries), very selective (5 rows: falls back to
the key list after 10000 candidates) and empty. HNSW_BV covers the "every
indexed row seen" exit. It also checks an online=Y index (a row inserted after
the build is returned), argument errors, and that the predicate runs with the
caller's privileges.

--bench instead loads a larger table and compares query latency of phase 1's
key list (MYVECTOR_IS_ANN / myvector_ann_set with JSON_ARRAYAGG) against the
procedure, for several filters.

Usage:
  python3 scripts/test-filtered-ann-broad.py --plugin-dir dist/plugin-8.4
  python3 scripts/test-filtered-ann-broad.py --component-dir dist/component-9.7 \
      --image mysql:9.7
  python3 scripts/test-filtered-ann-broad.py --plugin-dir dist/plugin-8.4 \
      --bench --rows 300000

Exit 0 = pass, 1 = fail.
"""
import argparse
import importlib.util
import os
import random
import re
import sys
import time

_here = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "filtered_ann", os.path.join(_here, "test-filtered-ann.py"))
fa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fa)

DIM = 16
K = 10


def load_table(srv, name, rows, component, rnd, dim=DIM):
    """t(id, cat = id % 100, v): cat < 90 matches 90% of the rows, cat = 7 1%."""
    if component:
        srv.sql(f"""CREATE TABLE {name} (id INT PRIMARY KEY, cat INT NOT NULL,
            v VARBINARY({dim * 4 + 64}) COMMENT 'MYVECTOR COLUMN """
                f"""type=hnsw,dim={dim},size={rows + 1000},m=16,ef=100,idcol=id,dist=L2');""")
    else:
        srv.sql(f"""CREATE TABLE {name} (id INT PRIMARY KEY, cat INT NOT NULL,
            v MYVECTOR(type=HNSW,dim={dim},size={rows + 1000},M=16,ef=100));""")
    chunk = []
    for start in range(1, rows + 1, 1000):
        vals = []
        for i in range(start, min(start + 1000, rows + 1)):
            v = [rnd.uniform(-1, 1) for _ in range(dim)]
            vals.append(f"({i},{i % 100},myvector_construct({fa.vec_literal(v)}))")
        chunk.append(f"INSERT INTO {name} VALUES " + ",".join(vals) + ";")
        if len(chunk) == 20:
            srv.sql("\n".join(chunk))
            chunk = []
    if chunk:
        srv.sql("\n".join(chunk))
    out = srv.sql(f"CALL mysql.myvector_index_build('vtest.{name}.v','id');")
    print("index build:", out)
    return out


def write_cnf(srv):
    """Write myvector.cnf before installing: the binlog listener reads it when
    it starts, and never connects if it comes later."""
    cnf = (f"myvector_host=127.0.0.1\\nmyvector_port=3306\\n"
           f"myvector_user_id=root\\nmyvector_user_password={fa.PW}\\n")
    for path in ("/var/lib/mysql/myvector.cnf", "/myvector.cnf"):
        fa.sh(["docker", "exec", srv.name, "bash", "-c",
               f"printf '{cnf}' > {path} && chmod 600 {path} && "
               f"chown mysql:mysql {path}"])


def proc_call(q, k, pred, table="t"):
    p = pred.replace("\\", "\\\\").replace("'", "''")
    return (f"SET @q = myvector_construct({q});\n"
            f"CALL mysql.myvector_ann_filtered('vtest.{table}.v', 'id', @q, {k}, '{p}');")


def run_tests(srv, args, component, rnd, failures):
    # Online index: a row inserted after the build is found through the binlog.
    # Built first: on plugin builds (origin/main too), building a non-online
    # index stops the binlog listener ("Binlog fetch failed"), after which
    # online=Y indexes are no longer updated.
    if component:
        srv.sql("""CREATE TABLE tol (id INT PRIMARY KEY, cat INT NOT NULL,
            v VARBINARY(128) COMMENT 'MYVECTOR COLUMN """
                """type=hnsw,dim=16,size=5000,m=16,ef=100,idcol=id,dist=L2,online=Y');""")
    else:
        srv.sql("""CREATE TABLE tol (id INT PRIMARY KEY, cat INT NOT NULL,
            v MYVECTOR(type=HNSW,dim=16,size=5000,M=16,ef=100,online=Y,idcol=id,dist=L2));""")
    vals = [f"({i},{i % 100},myvector_construct("
            f"{fa.vec_literal([rnd.uniform(-1, 1) for _ in range(DIM)])}))"
            for i in range(1, 2001)]
    srv.sql("INSERT INTO tol VALUES " + ",".join(vals) + ";")
    out = srv.sql("CALL mysql.myvector_index_build('vtest.tol.v','id');")[0][0]
    print("online index build:", out)
    if not re.search(r"binlog\.\d+ [1-9]", out):
        failures.append(f"online index: no binlog position in build result: {out}")
    qv = [0.5] * DIM
    srv.sql(f"INSERT INTO tol VALUES (9999, 7, myvector_construct({fa.vec_literal(qv)}));")
    first = None
    for _ in range(30):
        rows = srv.sql(proc_call(fa.vec_literal(qv), K, "cat = 7", table="tol"))
        first = rows[0][0] if rows else None
        if first == "9999":
            break
        time.sleep(1)
    print(f"{'online insert':20s} nearest cat=7 row: {first}")
    if first != "9999":
        failures.append(f"online index: new row not found (got {first})")

    load_table(srv, "t", args.rows, component, rnd)
    fa.check_index_type(srv, "vtest.t.v", "HNSW", failures)

    filters = {
        "90%": "cat < 90",
        "50%": "cat < 50",
        "1% (retries)": "cat = 7",
        # 5 rows: 10000 candidates hold fewer than k, so it falls back to
        # the key list.
        "5 rows (fallback)": f"id % {args.rows // 5} = 1",
        "empty": "cat = -1",
        # a trailing comment must not swallow the rest of the query
        "90% + comment": "cat < 90 -- trailing comment",
    }
    for name, pred in filters.items():
        n_match = int(srv.sql(f"SELECT COUNT(*) FROM t WHERE {pred}\n;")[0][0])
        hits = total = 0
        for _ in range(args.queries):
            q = fa.vec_literal([rnd.uniform(-1, 1) for _ in range(DIM)])
            rows = srv.sql(proc_call(q, K, pred))
            exact = srv.sql(f"""
                SET @q = myvector_construct({q});
                SELECT id FROM t WHERE {pred}
                ORDER BY myvector_distance(v, @q, 'L2') LIMIT {K};""")
            ids = [int(r[0]) for r in rows]
            dists = [float(r[1]) for r in rows]
            exact_ids = {int(r[0]) for r in exact}
            expect_n = min(K, n_match)
            if len(ids) != expect_n or len(set(ids)) != len(ids):
                failures.append(f"{name}: got {len(ids)} rows ({len(set(ids))} "
                                f"distinct), expected {expect_n}")
            if dists != sorted(dists):
                failures.append(f"{name}: rows not nearest first: {dists}")
            if any(d > 1e10 for d in dists):
                failures.append(f"{name}: missing distance: {dists}")
            if ids:
                bad = srv.sql(f"SELECT COUNT(*) FROM t WHERE id IN "
                              f"({','.join(map(str, ids))}) AND NOT ({pred}\n);")[0][0]
                if int(bad):
                    failures.append(f"{name}: {bad} rows fail the predicate")
            hits += len(set(ids) & exact_ids)
            total += len(exact_ids)
        recall = hits / total if total else 1.0
        print(f"{name:20s} matching={n_match:6d} recall@{K}={recall:.3f}")
        if recall < args.min_recall:
            failures.append(f"{name}: recall {recall:.3f} < {args.min_recall}")

    # k larger than the number of matches, where every match must come back.
    n = len(srv.sql(proc_call(fa.vec_literal([0.1] * DIM), 50, "id <= 20")))
    print(f"{'k=50, 20 matching':20s} rows={n}")
    if n != 20:
        failures.append(f"k=50 over 20 matching rows: got {n}")

    # Argument errors.
    q = fa.vec_literal([0.1] * DIM)
    for name, call in {
        "k=0": proc_call(q, 0, "cat < 90"),
        "k=10001": proc_call(q, 10001, "cat < 90"),
        "unknown column": proc_call(q, K, "cat < 90").replace("vtest.t.v", "vtest.t.nope"),
        "non-vector column": proc_call(q, K, "cat < 90").replace("vtest.t.v", "vtest.t.cat"),
        "unknown key column": proc_call(q, K, "cat < 90").replace("'id'", "'nope'"),
        "NULL vector": "CALL mysql.myvector_ann_filtered('vtest.t.v','id',NULL,10,'');",
        "bad predicate": proc_call(q, K, "no_such_col = 1"),
    }.items():
        r = fa.sh(["docker", "exec", "-i", srv.name, "mysql", "-uroot",
                   f"-p{fa.PW}", "-N", "-B", "vtest"], inp=call, check=False)
        err = (r.stderr.strip().splitlines() or [""])[-1]
        print(f"{name:20s} rc={r.returncode} {err[:90]}")
        if r.returncode == 0:
            failures.append(f"{name}: expected an error")

    # The predicate runs with the caller's privileges (SQL SECURITY INVOKER),
    # so a user limited to vtest.t cannot read mysql.user through it.
    srv.sql("""CREATE USER lim@'%' IDENTIFIED BY 'Lim-pw-1';
        GRANT SELECT ON vtest.t TO lim@'%';
        GRANT EXECUTE ON PROCEDURE mysql.myvector_ann_filtered TO lim@'%';""",
            db=None)
    base = ["docker", "exec", "-i", srv.name, "mysql", "-ulim", "-pLim-pw-1",
            "-N", "-B", "vtest"]
    ok = fa.sh(base, inp=proc_call(q, K, "cat < 90"), check=False)
    esc = fa.sh(base, inp=proc_call(
        q, K, "id IN (SELECT 1 FROM mysql.user)"), check=False)
    print(f"{'limited user':20s} own table rc={ok.returncode} "
          f"rows={len(ok.stdout.splitlines())}; mysql.user rc={esc.returncode} "
          f"{(esc.stderr.strip().splitlines() or [''])[-1][:70]}")
    if ok.returncode != 0 or len(ok.stdout.splitlines()) != K:
        failures.append(f"limited user: own-table call failed: {ok.stderr}")
    if esc.returncode == 0 or "denied" not in esc.stderr:
        failures.append("limited user: predicate read mysql.user")

    # 10000 keys of 13 digits need about 140 KB, more than the old 128,000-byte
    # myvector_ann_set() buffer. The result must be the full, valid JSON array.
    big = 10**12
    if component:
        srv.sql("""CREATE TABLE tbig (id BIGINT PRIMARY KEY, v VARBINARY(96)
            COMMENT 'MYVECTOR COLUMN type=hnsw,dim=4,size=11000,m=16,ef=100,idcol=id,dist=L2');""")
    else:
        srv.sql("""CREATE TABLE tbig (id BIGINT PRIMARY KEY,
            v MYVECTOR(type=HNSW,dim=4,size=11000,M=16,ef=100));""")
    for start in range(1, 10501, 1000):
        srv.sql("INSERT INTO tbig VALUES " + ",".join(
            f"({big + i},myvector_construct("
            f"{fa.vec_literal([rnd.uniform(-1, 1) for _ in range(4)])}))"
            for i in range(start, min(start + 1000, 10501))) + ";")
    srv.sql("CALL mysql.myvector_index_build('vtest.tbig.v','id');")
    r = srv.sql("""SET @q = myvector_construct('[0.1,0.2,0.3,0.4]');
        SELECT JSON_VALID(js), JSON_LENGTH(js), LENGTH(js) FROM
          (SELECT myvector_ann_set('vtest.tbig.v','id',@q,'nn=10000') js) s;""")[0]
    print(f"{'13-digit keys':20s} valid={r[0]} keys={r[1]} bytes={r[2]}")
    if r[0] != "1" or r[1] != "10000":
        failures.append(f"13-digit keys: myvector_ann_set returned {r}")

    # HNSW_BV: 5000 rows, so a 1% filter reaches the "every indexed row seen"
    # exit before the 10000 cap. Hamming distances tie often: a row counts as
    # a hit if it is no farther than the k-th exact distance.
    bv_dim, bv_rows = 64, 5000
    if component:
        srv.sql(f"""CREATE TABLE tb (id INT PRIMARY KEY, cat INT NOT NULL,
            v VARBINARY({bv_dim // 8 + 64}) COMMENT 'MYVECTOR COLUMN """
                f"""type=hnsw_bv,dim={bv_dim},size={bv_rows + 1000},m=16,ef=100,idcol=id');""")
    else:
        srv.sql(f"""CREATE TABLE tb (id INT PRIMARY KEY, cat INT NOT NULL,
            v MYVECTOR(type=HNSW_BV,dim={bv_dim},size={bv_rows + 1000},M=16,ef=100));""")

    def rand_bytes():
        return [rnd.randrange(256) for _ in range(bv_dim // 8)]

    def bv_literal(b):
        return "'[" + ",".join(map(str, b)) + "]'"

    def hamming(a, b):
        return sum(bin(x ^ y).count("1") for x, y in zip(a, b))

    bvecs, batch = {}, []
    for i in range(1, bv_rows + 1):
        bvecs[i] = rand_bytes()
        batch.append(f"({i},{i % 100},myvector_construct("
                     f"{bv_literal(bvecs[i])},'i=string,o=bv'))")
        if len(batch) == 1000:
            srv.sql("INSERT INTO tb VALUES " + ",".join(batch) + ";")
            batch = []
    print("bv index build:",
          srv.sql("CALL mysql.myvector_index_build('vtest.tb.v','id');"))
    fa.check_index_type(srv, "vtest.tb.v", "HNSW_BV", failures)
    for name, pred, fn in [("bv 90%", "cat < 90", lambda i: i % 100 < 90),
                           ("bv 1%", "cat = 7", lambda i: i % 100 == 7)]:
        allowed = [i for i in bvecs if fn(i)]
        hits = total = 0
        for _ in range(args.queries):
            qb = rand_bytes()
            rows = srv.sql(f"""
                SET @q = myvector_construct({bv_literal(qb)}, 'i=string,o=bv');
                CALL mysql.myvector_ann_filtered('vtest.tb.v', 'id', @q, {K},
                                                 '{pred}');""")
            ids = [int(r[0]) for r in rows]
            if len(ids) != min(K, len(allowed)):
                failures.append(f"{name}: got {len(ids)} rows")
            if [i for i in ids if not fn(i)]:
                failures.append(f"{name}: rows fail the predicate")
            exact = sorted(hamming(qb, bvecs[i]) for i in allowed)[:K]
            kth = exact[-1] if exact else 0
            hits += sum(1 for i in ids if fn(i) and hamming(qb, bvecs[i]) <= kth)
            total += len(exact)
        recall = hits / total if total else 1.0
        print(f"{name:20s} matching={len(allowed):6d} recall@{K}={recall:.3f}")
        if recall < args.min_recall:
            failures.append(f"{name}: recall {recall:.3f} < {args.min_recall}")


def timed(srv, setup, stmt, n):
    """Average server-side latency in ms of stmt over n runs, in one session."""
    body = "\n".join([setup, "SET @t0 = NOW(6);"] + [stmt] * n +
                     [f"SELECT 'ELAPSED', TIMESTAMPDIFF(MICROSECOND, @t0, NOW(6)) / {n} / 1000;"])
    rows = srv.sql(body)
    return float([r for r in rows if r[0] == "ELAPSED"][0][1]), rows


def run_bench(srv, args, component, rnd, failures):
    t0 = time.time()
    load_table(srv, "t", args.rows, component, rnd)
    print(f"loaded {args.rows} rows + index in {time.time() - t0:.0f}s")
    fa.check_index_type(srv, "vtest.t.v", "HNSW", failures)
    filters = {"90%": "cat < 90", "50%": "cat < 50", "1%": "cat = 7"}
    print(f"\n| filter | matching rows | key list (phase 1) ms | "
          f"myvector_ann_filtered ms | recall@{K} key list | recall@{K} procedure |")
    print("|---|---|---|---|---|---|")
    for name, pred in filters.items():
        n_match = int(srv.sql(f"SELECT COUNT(*) FROM t WHERE {pred};")[0][0])
        lat_kl = lat_pr = 0.0
        rec_kl = rec_pr = 0
        for _ in range(args.queries):
            q = fa.vec_literal([rnd.uniform(-1, 1) for _ in range(DIM)])
            setup = f"SET @q = myvector_construct({q});"
            keys = f"(SELECT JSON_ARRAYAGG(id) FROM t WHERE {pred})"
            if component:
                kl = (f"SELECT myvecid FROM (SELECT myvector_ann_set('vtest.t.v', 'id', @q, "
                      f"'nn={K}', {keys}) AS js) src, JSON_TABLE(src.js, '$[*]' "
                      f"COLUMNS(myvecid BIGINT PATH '$')) jt;")
            else:
                kl = (f"SELECT id FROM t WHERE MYVECTOR_IS_ANN('vtest.t.v', 'id', @q, "
                      f"'nn={K}', {keys});")
            pr = f"CALL mysql.myvector_ann_filtered('vtest.t.v', 'id', @q, {K}, '{pred}');"
            ms, rows = timed(srv, setup, kl, args.reps)
            lat_kl += ms
            ids_kl = {int(r[0]) for r in rows[:K] if r[0] != "ELAPSED"}
            ms, rows = timed(srv, setup, pr, args.reps)
            lat_pr += ms
            ids_pr = {int(r[0]) for r in rows[:K] if r[0] != "ELAPSED"}
            exact = {int(r[0]) for r in srv.sql(f"""{setup}
                SELECT id FROM t WHERE {pred}
                ORDER BY myvector_distance(v, @q, 'L2') LIMIT {K};""")}
            rec_kl += len(ids_kl & exact)
            rec_pr += len(ids_pr & exact)
            if len(ids_pr) != K:
                failures.append(f"bench {name}: procedure returned {len(ids_pr)} rows")
        nq = args.queries
        print(f"| {name} | {n_match} | {lat_kl / nq:.1f} | {lat_pr / nq:.1f} | "
              f"{rec_kl / (nq * K):.3f} | {rec_pr / (nq * K):.3f} |")
    ms, _ = timed(srv, "SET @q = myvector_construct('[" + ",".join(["0.1"] * DIM) + "]');",
                  "SELECT myvector_ann_set('vtest.t.v', 'id', @q, 'nn=10');", args.reps)
    print(f"\nunfiltered myvector_ann_set nn=10: {ms:.2f} ms")


def main():
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--plugin-dir", default="dist/plugin-8.4")
    mode.add_argument("--component-dir",
                      help="test a component build instead of the plugin")
    ap.add_argument("--image", default="mysql:8.4")
    ap.add_argument("--rows", type=int, default=None,
                    help="table size (default 20000, or 300000 with --bench)")
    ap.add_argument("--queries", type=int, default=5)
    ap.add_argument("--reps", type=int, default=5,
                    help="--bench: runs of each query per timing")
    ap.add_argument("--min-recall", type=float, default=0.9)
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--keep", action="store_true",
                    help="keep the container for debugging")
    args = ap.parse_args()
    if args.rows is None:
        args.rows = 300000 if args.bench else 20000

    rnd = random.Random(42)
    srv = fa.Server(args.image, f"myv-filtered-broad-{os.getpid()}")
    failures = []
    try:
        srv.wait_ready()
        # mysqladmin ping also succeeds on "access denied" and against the
        # entrypoint's temporary server. That one runs with --skip-networking,
        # so wait for a TCP login, which only the final server accepts.
        for _ in range(90):
            r = fa.sh(["docker", "exec", srv.name, "mysql", "-h127.0.0.1",
                       "-uroot", f"-p{fa.PW}", "-e", "SELECT 1"], check=False)
            if r.returncode == 0:
                break
            time.sleep(2)
        srv.sql("CREATE DATABASE vtest;", db=None)
        component = bool(args.component_dir)
        write_cnf(srv)
        if component:
            fa.install_component(srv, args.component_dir)
        else:
            fa.install_plugin(srv, args.plugin_dir)
        if args.bench:
            run_bench(srv, args, component, rnd, failures)
        else:
            run_tests(srv, args, component, rnd, failures)
    finally:
        if args.keep:
            print("container kept:", srv.name)
        else:
            srv.remove()

    if failures:
        print("FAIL")
        for f in failures:
            print("  -", f)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
