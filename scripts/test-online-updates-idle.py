#!/usr/bin/env python3
"""Online index updates across idle periods and binlog rotation (#166).

The binlog listener reads with a 1-second timeout. The plugin used to exit on
the first timeout, which happens as soon as the server is idle for a second,
so online=Y indexes stopped receiving INSERTs shortly after install.

This test starts a fresh mysql:<ver> container, writes myvector.cnf into the
datadir BEFORE installing (the listener reads it once, at start), builds an
online=Y HNSW index and checks that INSERTs reach it:
  1. right after the build
  2. after an idle period
  3. after building a second, non-online index
  4. after FLUSH BINARY LOGS (a real binlog rotation)
  5. after another idle period
  6. for a burst of rows spread over several idle gaps and a rotation, where
     Current Rows must equal the exact count (nothing skipped or replayed)
  7. after the listener's binlog dump connection is killed (reconnect and
     resume), once on its own and once together with a rotation
It also checks the server log up to stage 7: no "Binlog fetch failed", no
exit of the listener, and no checkpoint per idle second ("CheckPoint line" count over an
idle window stays small).

Component builds (8.4, 9.7, 26.7) currently fail from the first stage after a
binlog rotation: the component strips no checksum from rotate events, so the
binlog file name it tracks ends in the event's 4 CRC bytes, and later updates
are skipped as older than that position. This is a known component issue,
separate from #166, which is plugin-only.

Usage:
  python3 scripts/test-online-updates-idle.py --plugin-dir dist/plugin-8.4
  python3 scripts/test-online-updates-idle.py \\
      --component-dir dist/component-9.7 --image mysql:9.7

Exit 0 = pass, 1 = fail.
"""
import argparse
import os
import random
import re
import subprocess
import sys
import time

PW = "myvector"
DIM = 4


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

    def log(self):
        r = sh(["docker", "logs", self.name], check=False)
        return r.stdout + r.stderr

    def remove(self):
        sh(["docker", "rm", "-fv", self.name], check=False)


def write_config(srv, paths):
    cnf = (f"myvector_host=127.0.0.1\\nmyvector_port=3306\\n"
           f"myvector_user_id=root\\nmyvector_user_password={PW}\\n")
    for path in paths:
        sh(["docker", "exec", srv.name, "bash", "-c",
            f"printf '{cnf}' > {path} && chown mysql:mysql {path} && "
            f"chmod 600 {path}"])


def install_plugin(srv, plugin_dir):
    datadir = srv.sql("SELECT @@datadir;", db=None)[0][0]
    # Config first: the listener starts on INSTALL PLUGIN and reads it once.
    write_config(srv, [f"{datadir}myvector.cnf"])
    pdir = srv.sql("SELECT @@plugin_dir;", db=None)[0][0]
    sh(["docker", "cp", os.path.join(plugin_dir, "myvector.so"),
        f"{srv.name}:{pdir}/myvector.so"])
    sh(["docker", "exec", srv.name, "chmod", "755", f"{pdir}/myvector.so"])
    sql_file = os.path.join(plugin_dir, "myvectorplugin.sql")
    if not os.path.exists(sql_file):
        sql_file = os.path.join(REPO, "sql", "myvectorplugin.sql")
    with open(sql_file) as f:
        srv.sql(f.read(), db=None)
    srv.sql("SET GLOBAL myvector_index_dir='/var/lib/mysql';", db=None)


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
    datadir = srv.sql("SELECT @@datadir;", db=None)[0][0]
    # The component reads myvector.cnf relative to mysqld's working directory.
    write_config(srv, [f"{datadir}myvector.cnf", "/myvector.cnf"])
    pdir = srv.sql("SELECT @@plugin_dir;", db=None)[0][0]
    sh(["docker", "cp", os.path.join(component_dir, "libmyvector_component.so"),
        f"{srv.name}:{pdir}/myvector.so"])
    sh(["docker", "cp", os.path.join(component_dir, "myvector.json"),
        f"{srv.name}:{pdir}/myvector.json"])
    with open(os.path.join(REPO, "sql", "myvector_install_component.sql")) as f:
        srv.sql(f.read(), db="mysql")


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def vec(rnd):
    return ("myvector_construct('[" +
            ",".join(f"{rnd.uniform(-1, 1):.5f}" for _ in range(DIM)) + "]')")


def current_rows(srv, index):
    status = srv.sql(f"CALL mysql.myvector_index_status('{index}');")[0][0]
    m = re.search(r"Current Rows : (\d+)", status.replace("\\n", "\n"))
    return int(m.group(1)) if m else -1


def main():
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--plugin-dir", default="dist/plugin-8.4")
    mode.add_argument("--component-dir",
                      help="test a component build instead of the plugin")
    ap.add_argument("--image", default="mysql:8.4")
    ap.add_argument("--idle", type=int, default=15,
                    help="seconds of idle time between stages")
    ap.add_argument("--keep", action="store_true",
                    help="keep the container for debugging")
    args = ap.parse_args()

    rnd = random.Random(166)
    srv = Server(args.image, f"myv-online-idle-{os.getpid()}")
    failures = []
    results = []
    next_id = [1]
    expected = [0]

    def insert(n):
        rows = []
        for _ in range(n):
            rows.append(f"({next_id[0]},{vec(rnd)})")
            next_id[0] += 1
        srv.sql("INSERT INTO t VALUES " + ",".join(rows) + ";")
        expected[0] += n

    def check(stage, timeout=15):
        """Wait for the index to show exactly the expected row count."""
        got = -1
        deadline = time.time() + timeout
        while time.time() < deadline:
            got = current_rows(srv, "vtest.t.v")
            if got == expected[0]:
                break
            time.sleep(0.5)
        ok = got == expected[0]
        results.append((stage, expected[0], got, ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {stage}: Current Rows "
              f"{got}, expected {expected[0]}", flush=True)
        if not ok:
            failures.append(f"{stage}: Current Rows {got}, expected "
                            f"{expected[0]}")

    try:
        srv.wait_ready()
        srv.sql("CREATE DATABASE vtest;", db=None)
        component = bool(args.component_dir)
        if component:
            install_component(srv, args.component_dir)
            opts = "type=hnsw,dim=4,size=10000,m=16,ef=50,idcol=id,dist=L2"
            srv.sql(f"""CREATE TABLE t (id INT PRIMARY KEY, v VARBINARY(128)
                COMMENT 'MYVECTOR COLUMN {opts},online=Y');""")
            srv.sql(f"""CREATE TABLE t2 (id INT PRIMARY KEY, v VARBINARY(128)
                COMMENT 'MYVECTOR COLUMN {opts}');""")
        else:
            install_plugin(srv, args.plugin_dir)
            srv.sql("""CREATE TABLE t (id INT PRIMARY KEY,
                v MYVECTOR(type=HNSW,dim=4,size=10000,M=16,ef=50,online=Y,
                idcol=id));""")
            srv.sql("""CREATE TABLE t2 (id INT PRIMARY KEY,
                v MYVECTOR(type=HNSW,dim=4,size=10000,M=16,ef=50));""")
        install_time = time.time()

        insert(100)
        srv.sql("INSERT INTO t2 VALUES " +
                ",".join(f"({i},{vec(rnd)})" for i in range(1, 101)) + ";")
        out = srv.sql("CALL mysql.myvector_index_build('vtest.t.v','id');")
        build = " ".join(" ".join(r) for r in out)
        print("index build:", build, flush=True)
        m = re.search(r"at \((\S+) (\d+)\)", build)
        if not m or not m.group(1) or int(m.group(2)) == 0:
            failures.append(f"build did not report a binlog position: {build}")

        insert(10)
        check("1. right after the build")

        time.sleep(args.idle)
        insert(10)
        check(f"2. after {args.idle}s idle")

        out = srv.sql("CALL mysql.myvector_index_build('vtest.t2.v','id');")
        print("non-online index build:", out, flush=True)
        insert(10)
        check("3. after building a non-online index")

        srv.sql("FLUSH BINARY LOGS;", db=None)
        insert(10)
        check("4. after FLUSH BINARY LOGS")

        # idle window: count checkpoints and new connections while idle
        ck_before = srv.log().count("CheckPoint line")
        conn_before = int(srv.sql("SHOW GLOBAL STATUS LIKE 'Connections';",
                                  db=None)[0][1])
        time.sleep(10)
        conn_after = int(srv.sql("SHOW GLOBAL STATUS LIKE 'Connections';",
                                 db=None)[0][1])
        ck_idle = srv.log().count("CheckPoint line") - ck_before
        # one of these connections is the second SHOW STATUS client itself
        idle_conns = conn_after - conn_before - 1
        print(f"idle 10s: {ck_idle} checkpoint lines, "
              f"{idle_conns} listener (re)connections", flush=True)
        results.append(("idle 10s: checkpoint lines", "<= 1", ck_idle,
                        ck_idle <= 1))
        results.append(("idle 10s: listener reconnections", "info",
                        idle_conns, True))
        if ck_idle > 1:
            failures.append(f"{ck_idle} checkpoints during a 10 s idle window")

        time.sleep(max(0, args.idle - 10))
        insert(10)
        check(f"5. after another {args.idle}s idle")

        # 6. burst over idle gaps and a rotation: exact count
        for i in range(6):
            insert(25)
            if i == 2:
                srv.sql("FLUSH BINARY LOGS;", db=None)
            time.sleep(2.5)
        check("6. burst of 150 rows over idle gaps + rotation (exact)",
              timeout=30)

        # log checks cover stages 1-6; stage 7 kills the connection on purpose
        log = srv.log()

        # 7. reconnect path: kill the listener's binlog dump connection, with
        # and without a rotation while it is down; rows must not be skipped or
        # applied twice
        def kill_dump():
            ids = srv.sql("SELECT id FROM information_schema.processlist "
                          "WHERE command='Binlog Dump';", db=None)
            for (i,) in ids:
                srv.sql(f"KILL {i};", db=None, check=False)
            return len(ids)
        n = kill_dump()
        insert(20)
        check(f"7a. after KILL of the binlog dump connection ({n} killed)")
        n = kill_dump()
        srv.sql("FLUSH BINARY LOGS;", db=None)
        insert(20)
        check(f"7b. after KILL + FLUSH BINARY LOGS ({n} killed)")

        for bad in ("Binlog fetch failed", "Exiting binlog",
                    "Binlog open failed"):
            n = log.count(bad)
            results.append((f"server log: '{bad}'", 0, n, n == 0))
            if n:
                failures.append(f"server log has {n} x '{bad}'")
        for line in log.splitlines():
            if re.search(r"Binlog|binlog thread|CheckPoint line", line):
                print("  log:", line[:200])
        print(f"total run time since install: {time.time() - install_time:.0f}s")
    except Exception as e:  # noqa: BLE001
        failures.append(f"exception: {e}")
    finally:
        if args.keep:
            print(f"container kept: {srv.name}")
        else:
            srv.remove()

    print("\nstage | expected | got | result")
    for stage, exp, got, ok in results:
        print(f"{stage} | {exp} | {got} | {'PASS' if ok else 'FAIL'}")
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nPASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
