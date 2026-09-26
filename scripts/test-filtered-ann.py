#!/usr/bin/env python3
"""Filtered ANN search test (MYVECTOR_IS_ANN / myvector_ann_set with a key filter).

Starts a fresh mysql:<ver> container, installs a locally built plugin, loads
random vectors, builds an HNSW index and checks that filtered ANN queries:
  - return only rows that pass the filter,
  - return min(k, matching rows) rows,
  - agree with brute force (ORDER BY myvector_distance) on the top-k.

Filters cover both code paths: few allowed rows (exact scan over the allowed
rows) and many allowed rows (HNSW graph walk with a filter).

Plugin builds are queried through MYVECTOR_IS_ANN. Component builds have no
query rewrite (#144), so they are queried through myvector_ann_set directly.

Usage:
  ./scripts/build-plugin-8.4-docker.sh mysql-8.4.8 dist/plugin-8.4
  python3 scripts/test-filtered-ann.py --plugin-dir dist/plugin-8.4

  ./scripts/build-component-9.7-docker.sh mysql-9.7.0 dist/component-9.7
  python3 scripts/test-filtered-ann.py --component-dir dist/component-9.7 \
      --image mysql:9.7

Exit 0 = pass, 1 = fail.
"""
import argparse
import os
import random
import subprocess
import sys
import time

PW = "myvector"
DIM = 16
K = 10


def sh(cmd, inp=None, check=True):
    r = subprocess.run(cmd, input=inp, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed:\n{r.stdout}\n{r.stderr}")
    return r


class Server:
    def __init__(self, image, name):
        self.name = name
        sh(["docker", "run", "-d", "--name", name,
            "-e", f"MYSQL_ROOT_PASSWORD={PW}", "-e", "MYSQL_ROOT_HOST=%",
            image])

    def sql(self, q, db="vtest", check=True):
        args = ["docker", "exec", "-i", self.name, "mysql", "-uroot",
                f"-p{PW}", "-N", "-B"]
        if db:
            args.append(db)
        r = sh(args, inp=q, check=check)
        return [line.split("\t") for line in r.stdout.splitlines() if line]

    def wait_ready(self):
        for _ in range(120):
            r = sh(["docker", "exec", self.name, "mysqladmin", "ping", "-uroot",
                    f"-p{PW}", "--silent"], check=False)
            if r.returncode == 0:
                # the entrypoint restarts mysqld once after init
                time.sleep(5)
                r = sh(["docker", "exec", self.name, "mysqladmin", "ping",
                        "-uroot", f"-p{PW}", "--silent"], check=False)
                if r.returncode == 0:
                    return
            time.sleep(2)
        raise RuntimeError("MySQL did not become ready")

    def remove(self):
        sh(["docker", "rm", "-fv", self.name], check=False)


def install_plugin(srv, plugin_dir):
    pdir = srv.sql("SELECT @@plugin_dir;", db=None)[0][0]
    sh(["docker", "cp", os.path.join(plugin_dir, "myvector.so"),
        f"{srv.name}:{pdir}/myvector.so"])
    sh(["docker", "exec", srv.name, "chmod", "755", f"{pdir}/myvector.so"])
    with open(os.path.join(plugin_dir, "myvectorplugin.sql")) as f:
        srv.sql(f.read(), db=None)
    srv.sql("SET GLOBAL myvector_index_dir='/var/lib/mysql';", db=None)
    sh(["docker", "exec", srv.name, "bash", "-c",
        "printf 'myvector_host=127.0.0.1\\nmyvector_port=3306\\n"
        f"myvector_user_id=root\\nmyvector_user_password={PW}\\n' "
        "> /var/lib/mysql/myvector.cnf && chown mysql:mysql "
        "/var/lib/mysql/myvector.cnf && chmod 600 /var/lib/mysql/myvector.cnf"])
    srv.sql("SET GLOBAL myvector_config_file='myvector.cnf';", db=None)


def install_component(srv, component_dir):
    # The plain mysql:<ver> image may lack libmysqlclient (see
    # scripts/smoke-component.sh); install it from the MySQL CDN if missing.
    sh(["docker", "exec", srv.name, "bash", "-c", """
        ldconfig -p | grep -q libmysqlclient && exit 0
        V=$(mysqld --version | grep -oE '[0-9]+\\.[0-9]+\\.[0-9]+' | head -1)
        B="https://cdn.mysql.com/Downloads/MySQL-${V%.*}"; A=$(uname -m)
        for p in common client-plugins libs; do
          rpm -ivh --nodeps "$B/mysql-community-$p-$V-1.el9.$A.rpm" || true
        done
        ldconfig"""], check=False)
    pdir = srv.sql("SELECT @@plugin_dir;", db=None)[0][0]
    sh(["docker", "cp", os.path.join(component_dir, "libmyvector_component.so"),
        f"{srv.name}:{pdir}/myvector.so"])
    sh(["docker", "cp", os.path.join(component_dir, "myvector.json"),
        f"{srv.name}:{pdir}/myvector.json"])
    # The installer does INSTALL COMPONENT and registers the supplemental UDFs
    # and MYVECTOR_INDEX_* procedures.
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(repo, "sql", "myvector_install_component.sql")) as f:
        srv.sql(f.read(), db="mysql")
    # The component reads myvector.cnf relative to mysqld's working directory.
    datadir = srv.sql("SELECT @@datadir;", db=None)[0][0]
    cnf = (f"myvector_host=127.0.0.1\\nmyvector_port=3306\\n"
           f"myvector_user_id=root\\nmyvector_user_password={PW}\\n")
    for path in (f"{datadir}myvector.cnf", "/myvector.cnf"):
        sh(["docker", "exec", srv.name, "bash", "-c",
            f"printf '{cnf}' > {path} && chmod 600 {path} && "
            f"chown mysql:mysql {path}"])


def vec_literal(v):
    return "'[" + ",".join(f"{x:.5f}" for x in v) + "]'"


def main():
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--plugin-dir", default="dist/plugin-8.4")
    mode.add_argument("--component-dir",
                      help="test a component build instead of the plugin")
    ap.add_argument("--image", default="mysql:8.4")
    ap.add_argument("--rows", type=int, default=20000)
    ap.add_argument("--queries", type=int, default=5)
    ap.add_argument("--min-recall", type=float, default=0.9)
    ap.add_argument("--keep", action="store_true",
                    help="keep the container for debugging")
    args = ap.parse_args()

    rnd = random.Random(42)
    srv = Server(args.image, f"myv-filtered-ann-{os.getpid()}")
    failures = []
    try:
        srv.wait_ready()
        srv.sql("CREATE DATABASE vtest;", db=None)
        component = bool(args.component_dir)
        if component:
            install_component(srv, args.component_dir)
            # No DDL rewrite on components: declare the index in a comment.
            srv.sql(f"""CREATE TABLE t (id INT PRIMARY KEY, cat INT NOT NULL,
                v VARBINARY({DIM * 4 + 64}) COMMENT 'MYVECTOR COLUMN
                type=hnsw,dim={DIM},size={args.rows + 1000},m=16,ef=100,idcol=id,dist=L2'
                );""")
        else:
            install_plugin(srv, args.plugin_dir)
            srv.sql(f"""CREATE TABLE t (id INT PRIMARY KEY, cat INT NOT NULL,
                v MYVECTOR(type=HNSW,dim={DIM},size={args.rows + 1000},M=16,ef=100));""")
        batch = []
        for i in range(1, args.rows + 1):
            v = [rnd.uniform(-1, 1) for _ in range(DIM)]
            batch.append(f"({i},{i % 1000},myvector_construct({vec_literal(v)}))")
            if len(batch) == 1000:
                srv.sql("INSERT INTO t VALUES " + ",".join(batch) + ";")
                batch = []
        if batch:
            srv.sql("INSERT INTO t VALUES " + ",".join(batch) + ";")

        out = srv.sql("CALL mysql.myvector_index_build('vtest.t.v','id');")
        print("index build:", out)

        filters = {
            # name: (WHERE predicate, expected path)
            "0.1% (exact scan)": "cat = 7",
            "10% (exact scan)": "cat < 100",
            "90% (graph walk)": "cat >= 100",
            "empty filter": "cat = -1",
        }
        for name, pred in filters.items():
            n_match = int(srv.sql(f"SELECT COUNT(*) FROM t WHERE {pred};")[0][0])
            hits = total = 0
            for _ in range(args.queries):
                q = vec_literal([rnd.uniform(-1, 1) for _ in range(DIM)])
                keys = f"(SELECT JSON_ARRAYAGG(id) FROM t WHERE {pred})"
                if component:
                    ann = srv.sql(f"""
                        SET @q = myvector_construct({q});
                        SELECT myvecid FROM
                          (SELECT myvector_ann_set('vtest.t.v', 'id', @q,
                                  'nn={K}', {keys}) AS js) src,
                          JSON_TABLE(src.js, '$[*]'
                                     COLUMNS(myvecid BIGINT PATH '$')) jt;""")
                else:
                    ann = srv.sql(f"""
                        SET @q = myvector_construct({q});
                        SELECT id FROM t
                        WHERE MYVECTOR_IS_ANN('vtest.t.v', 'id', @q, 'nn={K}',
                              {keys});""")
                exact = srv.sql(f"""
                    SET @q = myvector_construct({q});
                    SELECT id FROM t WHERE {pred}
                    ORDER BY myvector_distance(v, @q, 'L2') LIMIT {K};""")
                ann_ids = {int(r[0]) for r in ann}
                exact_ids = {int(r[0]) for r in exact}
                expect_n = min(K, n_match)
                if len(ann_ids) != expect_n:
                    failures.append(f"{name}: got {len(ann_ids)} rows, "
                                    f"expected {expect_n}")
                bad = srv.sql(f"SELECT COUNT(*) FROM t WHERE id IN "
                              f"({','.join(map(str, ann_ids)) or 'NULL'}) "
                              f"AND NOT ({pred});")[0][0]
                if int(bad):
                    failures.append(f"{name}: {bad} rows fail the filter")
                hits += len(ann_ids & exact_ids)
                total += len(exact_ids)
            recall = hits / total if total else 1.0
            print(f"{name:20s} matching={n_match:6d} recall@{K}={recall:.3f}")
            if recall < args.min_recall:
                failures.append(f"{name}: recall {recall:.3f} < {args.min_recall}")

        q = vec_literal([rnd.uniform(-1, 1) for _ in range(DIM)])
        # MYVECTOR_IS_ANN with a bare integer k (plugin only: the rewrite
        # turns k into 'nn=k'), without and with a filter.
        bare_k = {
            "unfiltered bare-k": "",
            "filtered bare-k": ", (SELECT JSON_ARRAYAGG(id) FROM t WHERE cat IN (1,2))",
        }
        for name, extra in ({} if component else bare_k).items():
            n = len(srv.sql(f"""SET @q = myvector_construct({q});
                SELECT id FROM t
                WHERE MYVECTOR_IS_ANN('vtest.t.v','id',@q,{K}{extra});"""))
            print(f"{name:20s} rows={n}")
            if n != K:
                failures.append(f"{name}: got {n} rows, expected {K}")

        # Direct UDF call (works on component builds too).
        r = srv.sql(f"""SET @q = myvector_construct({q});
            SELECT myvector_ann_set('vtest.t.v','id',@q,'nn=3','[1,2,3,4,5]');""")
        print("myvector_ann_set    ", r[0][0])
        got = set(int(x) for x in r[0][0].strip("[]").split(",") if x)
        if len(got) != 3 or not got <= {1, 2, 3, 4, 5}:
            failures.append(f"myvector_ann_set direct: {r[0][0]}")
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
