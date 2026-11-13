#!/usr/bin/env python3
"""storage=innodb indexes (RFC-005 Phase 1a): an HNSW index kept in myvector.*
InnoDB tables instead of files in myvector_index_dir.

Component build (8.4, 9.7, 26.10):
  1. Refusals: GTID mode off, then (after turning it on online) super_read_only,
     online=Y, a KNN index with storage=innodb, an unknown storage, and a
     max_allowed_packet too small for one node.
  2. Build: status shows "Storage : innodb", index_meta is active at
     generation 1, chunk and upper rows exist, nothing reached the binlog.
  3. Round trip: an explicit reload and a server restart + load give the same
     search results as right after the build.
  4. Rebuild: generation 2, the generation-1 rows are gone.
  5. Corruption is rejected on load: a vector chunk byte (CRC), the header
     (CRC), and link lists with a valid CRC but a neighbor count or id out of
     range; a rebuild recovers.
  6. Names that differ only in case are two indexes.
  7. Uninstall + reinstall keeps the index.
  8. mysqldump --hex-blob of myvector.* restores and loads.
  9. Drop: tombstone, rows gone, searches refused; a rebuild continues the
     generation counter.
 10. A server with innodb_page_size=4096 installs the schema and stores an index.
 11. An empty index builds, loads and searches; a rebuild after the table was
     emptied publishes the empty graph instead of keeping the old one.
Plugin build: storage=innodb is refused.

Usage:
  python3 scripts/test-innodb-storage.py --component-dir dist/component-8.4 \\
      --image mysql:8.4
  python3 scripts/test-innodb-storage.py --plugin-dir dist/plugin-8.4

Exit 0 = all checks passed.
"""

import argparse
import importlib.util
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "online_idle", os.path.join(HERE, "test-online-updates-idle.py"))
idle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(idle)

DIM = 8
ROWS = 3000
SIZE = 10000
QUERIES = 20


def start_server(image, name, extra_args):
    """Like idle.Server(), with extra mysqld arguments."""
    idle.sh(["docker", "run", "-d", "--name", name,
             "-e", f"MYSQL_ROOT_PASSWORD={idle.PW}", "-e", "MYSQL_ROOT_HOST=%",
             image] + extra_args)
    srv = object.__new__(idle.Server)
    srv.name = name
    return srv


def main():
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--component-dir")
    mode.add_argument("--plugin-dir")
    ap.add_argument("--image", default="mysql:8.4")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    component = bool(args.component_dir)
    results = []
    servers = []

    def record(name, ok, detail=""):
        results.append(bool(ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}"[:400])

    def run(srv, q, db="vtest"):
        cmd = ["docker", "exec", "-i", srv.name, "mysql", "-uroot",
               f"-p{idle.PW}", "-N", "-B"]
        if db:
            cmd.append(db)
        r = idle.sh(cmd, inp=q, check=False)
        err = "\n".join(l for l in r.stderr.splitlines() if "Using a password" not in l)
        return r.returncode, r.stdout.strip(), err.strip()

    def alive(srv):
        return idle.sh(["docker", "exec", srv.name, "mysqladmin", "ping", "-uroot",
                        f"-p{idle.PW}", "--silent"], check=False).returncode == 0

    def create(srv, table, opts, data=True):
        if component:
            srv.sql(f"CREATE TABLE {table} (id INT PRIMARY KEY, v VARBINARY({DIM * 4 + 64}) "
                    f"NULL COMMENT 'MYVECTOR COLUMN type=hnsw,dim={DIM},size={SIZE},"
                    f"idcol=id,dist=L2{opts}');")
        else:
            srv.sql(f"CREATE TABLE {table} (id INT PRIMARY KEY, v MYVECTOR(type=HNSW,"
                    f"dim={DIM},size={SIZE},idcol=id,dist=L2{opts}) NULL);")
        if data:
            rnd = random.Random(table.lower())
            for start in range(1, ROWS + 1, 1000):
                vals = ", ".join(
                    f"({i}, myvector_construct('["
                    + ",".join(f"{rnd.uniform(-1, 1):.5f}" for _ in range(DIM)) + "]'))"
                    for i in range(start, min(start + 1000, ROWS + 1)))
                srv.sql(f"INSERT INTO {table} VALUES {vals};")

    def build(srv, idx):
        return run(srv, f"CALL mysql.myvector_index_build('{idx}','id');")

    def load(srv, idx):
        return run(srv, f"CALL mysql.myvector_index_load('{idx}');")

    rnd_q = random.Random(99)
    qvecs = ["myvector_construct('[" + ",".join(f"{rnd_q.uniform(-1, 1):.4f}"
             for _ in range(DIM)) + "]')" for _ in range(QUERIES)]

    def searches(srv, idx):
        out = []
        for q in qvecs:
            rc, o, e = run(srv, f"SELECT myvector_ann_set('{idx}','id',{q},'nn=10');")
            out.append(o if rc == 0 else f"ERR:{e[:80]}")
        return out

    def meta(srv, idx):
        rows = srv.sql(f"SELECT state, generation, node_count, index_id FROM "
                       f"myvector.index_meta WHERE index_name = '{idx}';", db=None)
        return rows[0] if rows else None

    def counts(srv, index_id, gen=None):
        g = f" AND generation = {gen}" if gen is not None else ""
        return tuple(int(srv.sql(f"SELECT COUNT(*) FROM myvector.{t} WHERE index_id = "
                                 f"{index_id}{g};", db=None)[0][0])
                     for t in ("index_vectors", "index_links", "index_upper"))

    try:
        if not component:
            srv = idle.Server(args.image, f"myv-innodb-{os.getpid()}")
            servers.append(srv)
            srv.wait_ready()
            idle.install_plugin(srv, args.plugin_dir)
            srv.sql("CREATE DATABASE vtest;", db=None)
            create(srv, "t", ",storage=innodb")
            rc, out, err = build(srv, "vtest.t.v")
            record("plugin: storage=innodb is refused",
                   out.startswith("ERROR:") and "component" in out, out)
            return finish(results, servers, args.keep)

        srv = start_server(args.image, f"myv-innodb-{os.getpid()}", [])
        servers.append(srv)
        srv.wait_ready()
        idle.install_component(srv, args.component_dir)
        srv.sql("CREATE DATABASE vtest;", db=None)
        create(srv, "t", ",storage=innodb")
        idx = "vtest.t.v"

        # --- 1. refusals -------------------------------------------------
        gtid = srv.sql("SELECT @@GLOBAL.gtid_mode;", db=None)[0][0]
        if gtid != "ON":
            rc, out, err = build(srv, idx)
            record("1a. GTID mode off: storage=innodb is refused",
                   out.startswith("ERROR:") and "gtid_mode" in out, out)
            # PERSIST: the restart in step 3 must keep GTID mode on.
            srv.sql("SET PERSIST enforce_gtid_consistency = ON;", db=None)
            for m in ("OFF_PERMISSIVE", "ON_PERMISSIVE", "ON"):
                srv.sql(f"SET PERSIST gtid_mode = {m};", db=None)
        else:
            record("1a. GTID mode off: refused (image has GTID on; not checked)", True)
        srv.sql("SET GLOBAL super_read_only = ON;", db=None)
        rc, out, err = build(srv, idx)
        srv.sql("SET GLOBAL super_read_only = OFF; SET GLOBAL read_only = OFF;", db=None)
        record("1b. super_read_only: refused",
               out.startswith("ERROR:") and "read_only" in out, out)
        create(srv, "o", ",storage=innodb,online=Y", data=False)
        rc, out, err = build(srv, "vtest.o.v")
        record("1c. online=Y with storage=innodb: refused",
               out.startswith("ERROR:") and "online=Y" in out, out)
        srv.sql(f"CREATE TABLE k (id INT PRIMARY KEY, v VARBINARY({DIM * 4 + 64}) NULL "
                f"COMMENT 'MYVECTOR COLUMN type=knn,dim={DIM},idcol=id,storage=innodb');")
        rc, out, err = build(srv, "vtest.k.v")
        record("1d. KNN with storage=innodb: refused",
               out.startswith("ERROR:") and "KNN" in out, out)
        create(srv, "u", ",storage=floppy", data=False)
        rc, out, err = build(srv, "vtest.u.v")
        record("1e. unknown storage: refused",
               out.startswith("ERROR:") and "unknown storage" in out, out)
        srv.sql("SET GLOBAL max_allowed_packet = 1024;", db=None)
        rc, out, err = build(srv, idx)
        srv.sql("SET GLOBAL max_allowed_packet = 67108864;", db=None)
        record("1f. max_allowed_packet too small: the save fails clearly",
               "max_allowed_packet" in out and not out.startswith("SUCCESS"), out)

        # --- 2. build ----------------------------------------------------
        log_before = srv.sql("SELECT @@GLOBAL.gtid_executed;", db=None)
        rc, out, err = build(srv, idx)
        record("2a. build succeeds", rc == 0 and out.startswith("SUCCESS"), out or err)
        status = run(srv, f"CALL mysql.myvector_index_status('{idx}');")[1]
        record("2b. status shows Storage : innodb", "Storage : innodb" in status,
               status.replace("\\n", " | ")[:200])
        m = meta(srv, idx)
        record("2c. index_meta active at generation 1 with every row",
               m is not None and m[0] == "active" and m[1] == "1" and m[2] == str(ROWS),
               f"{m}")
        c = counts(srv, m[3]) if m else (0, 0, 0)
        record("2d. vector, link and upper rows stored", all(x > 0 for x in c), f"{c}")
        # Read the binlog over SQL (the image has no mysqlbinlog): every event of
        # every file, and none may touch the myvector schema.
        logs = [r[0] for r in srv.sql("SHOW BINARY LOGS;", db=None)]
        events, mentions = 0, 0
        for lg in logs:
            for r in srv.sql(f"SHOW BINLOG EVENTS IN '{lg}';", db=None):
                events += 1
                if "myvector.index_" in "\t".join(r) or "`myvector`" in "\t".join(r):
                    mentions += 1
        record("2e. no index rows in the binlog (sql_log_bin = 0)",
               logs and events > 0 and mentions == 0,
               f"files={len(logs)} events={events} mentions={mentions}")
        base = searches(srv, idx)
        record("2f. searches return rows", all(b.startswith("[") and b != "[]" for b in base),
               base[0])

        # --- 3. round trip -----------------------------------------------
        rc, out, err = load(srv, idx)
        record("3a. explicit reload succeeds", out == "SUCCESS", out or err)
        record("3b. same results after reload", searches(srv, idx) == base, "")
        idle.sh(["docker", "restart", srv.name])
        srv.wait_ready()
        rc, out, err = load(srv, idx)
        record("3c. load after a server restart succeeds", out == "SUCCESS", out or err)
        record("3d. same results after restart", searches(srv, idx) == base, "")

        # --- 4. rebuild --------------------------------------------------
        rc, out, err = build(srv, idx)
        m2 = meta(srv, idx)
        record("4a. rebuild publishes generation 2",
               out.startswith("SUCCESS") and m2 and m2[1] == "2", f"{out[:60]} {m2}")
        record("4b. generation-1 rows are gone", counts(srv, m2[3], 1) == (0, 0, 0),
               f"{counts(srv, m2[3], 1)}")
        base = searches(srv, idx)
        iid, gen = m2[3], m2[1]

        # --- 5. corruption -----------------------------------------------
        def corrupt_and_load(label, sql, expect):
            srv.sql("SET SESSION sql_log_bin = 0; " + sql, db=None)
            rc, out, err = load(srv, idx)
            record(label, out.startswith("ERROR:") and expect in out and alive(srv), out)
            rc2, o2, e2 = run(srv, f"SELECT myvector_ann_set('{idx}','id',{qvecs[0]},'nn=3');")
            record(label + " (search refused)", rc2 != 0, e2[:100])
            rc3, o3, e3 = build(srv, idx)
            return rc3 == 0 and o3.startswith("SUCCESS")

        w = f"WHERE index_id = {iid} AND generation = {gen} AND chunk_id = 0"
        ok = corrupt_and_load("5a. flipped byte in a vector chunk: CRC check fails",
                              f"UPDATE myvector.index_vectors SET data = CONCAT("
                              f"CHAR(ASCII(SUBSTRING(data,1,1)) ^ 1), SUBSTRING(data,2)) {w};",
                              "CRC")
        gen = meta(srv, idx)[1]
        w = f"WHERE index_id = {iid} AND generation = {gen} AND chunk_id = 0"
        ok = corrupt_and_load("5b. changed header: CRC check fails",
                              f"UPDATE myvector.index_meta SET header = CONCAT(X'FF', "
                              f"SUBSTRING(header,2)) WHERE index_id = {iid};", "CRC") and ok
        gen = meta(srv, idx)[1]
        w = f"WHERE index_id = {iid} AND generation = {gen} AND chunk_id = 0"
        ok = corrupt_and_load("5c. neighbor count out of range (valid CRC): rejected",
                              f"UPDATE myvector.index_links SET level0 = CONCAT(X'FFFF', "
                              f"SUBSTRING(level0,3)), crc32 = CRC32(CONCAT(X'FFFF', "
                              f"SUBSTRING(level0,3))) {w};", "neighbor") and ok
        gen = meta(srv, idx)[1]
        w = f"WHERE index_id = {iid} AND generation = {gen} AND chunk_id = 0"
        ok = corrupt_and_load("5d. neighbor id out of range (valid CRC): rejected",
                              f"UPDATE myvector.index_links SET level0 = CONCAT("
                              f"X'0100', SUBSTRING(level0,3,2), X'FFFFFF7F', "
                              f"SUBSTRING(level0,9)), crc32 = CRC32(CONCAT(X'0100', "
                              f"SUBSTRING(level0,3,2), X'FFFFFF7F', SUBSTRING(level0,9))) "
                              f"{w};", "neighbor") and ok
        record("5e. a rebuild recovers after each corruption",
               ok and searches(srv, idx)[0].startswith("["), "")
        base = searches(srv, idx)

        # --- 6. names that differ only in case ---------------------------
        # A stored row whose name differs from the index only in case must be a
        # different row: the key is the SHA-256 of the exact bytes (RFC-005
        # 4.2), not a case-insensitive name.
        srv.sql("SET SESSION sql_log_bin = 0; INSERT INTO myvector.index_meta "
                "(index_name, name_sha256, state, format_version, generation, ckpt_seq, "
                "ckpt_chunks, header, header_crc32, metric, dim, vec_chunk_nodes, "
                "lnk_chunk_nodes, ckpt_gtids, writer_uuid, ckpt_time, node_count, update_ts) "
                "VALUES ('vtest.T.V', UNHEX(SHA2('vtest.T.V', 256)), 'active', 1, 41, 1, 0, "
                "'', 0, 'L2', 8, 0, 0, '', @@server_uuid, NOW(6), 0, 0);", db=None)
        o1 = build(srv, idx)[1]
        rows = srv.sql("SELECT index_name, generation FROM myvector.index_meta WHERE "
                       "index_name IN ('vtest.T.V', 'vtest.t.v') ORDER BY index_name;",
                       db=None)
        record("6. a name differing only in case is a separate row, left untouched",
               o1.startswith("SUCCESS") and len(rows) == 2 and
               ["vtest.T.V", "41"] in rows, f"{o1[:40]} {rows}")
        srv.sql("SET SESSION sql_log_bin = 0; DELETE FROM myvector.index_meta WHERE "
                "index_name = 'vtest.T.V';", db=None)
        base = searches(srv, idx)

        # --- 7. uninstall + reinstall ------------------------------------
        with open(os.path.join(idle.REPO, "sql", "myvector_uninstall_component.sql")) as f:
            un = run(srv, f.read(), db="mysql")
        idle.install_component(srv, args.component_dir)
        rc, out, err = load(srv, idx)
        record("7. uninstall + reinstall keeps the index",
               out == "SUCCESS" and searches(srv, idx) == base, out or err or un[2][:80])

        # --- 8. mysqldump --hex-blob -------------------------------------
        d = idle.sh(["docker", "exec", srv.name, "bash", "-c",
                     f"mysqldump -uroot -p{idle.PW} --hex-blob --single-transaction "
                     f"--set-gtid-purged=OFF myvector > /tmp/mv.sql && "
                     f"mysql -uroot -p{idle.PW} -e 'SET sql_log_bin=0; "
                     f"DELETE FROM myvector.index_vectors; DELETE FROM myvector.index_links; "
                     f"DELETE FROM myvector.index_upper; DELETE FROM myvector.index_meta;' && "
                     f"mysql -uroot -p{idle.PW} myvector < /tmp/mv.sql"], check=False)
        rc, out, err = load(srv, idx)
        record("8. mysqldump --hex-blob restore loads the same index",
               d.returncode == 0 and out == "SUCCESS" and searches(srv, idx) == base,
               out or d.stderr[-200:])

        # --- 9. drop -----------------------------------------------------
        gen_before = int(meta(srv, idx)[1])
        rc, out, err = run(srv, f"CALL mysql.myvector_index_drop('{idx}');")
        m = meta(srv, idx)
        record("9a. drop leaves a tombstone and no rows",
               m and m[0] == "dropped" and counts(srv, m[3]) == (0, 0, 0), f"{m}")
        rc, out, err = run(srv, f"SELECT myvector_ann_set('{idx}','id',{qvecs[0]},'nn=3');")
        record("9b. search after drop is refused", rc != 0, err[:100])
        rc, out, err = build(srv, idx)
        m = meta(srv, idx)
        record("9c. rebuild after drop continues the generation counter",
               out.startswith("SUCCESS") and m and int(m[1]) == gen_before + 1,
               f"before={gen_before} now={m}")

        # --- 11. empty indexes -------------------------------------------
        create(srv, "e", ",storage=innodb", data=False)
        o1 = build(srv, "vtest.e.v")[1]
        o2 = load(srv, "vtest.e.v")[1]
        rc, o3, e3 = run(srv, f"SELECT myvector_ann_set('vtest.e.v','id',{qvecs[0]},'nn=3');")
        record("11a. an empty index builds, loads and searches (no rows, no error)",
               o1.startswith("SUCCESS") and o2 == "SUCCESS" and rc == 0 and o3 == "[]",
               f"build={o1[:40]} load={o2} search rc={rc} {o3!r} {e3[:80]}")
        srv.sql("DELETE FROM t;")
        o1 = build(srv, idx)[1]
        o2 = load(srv, idx)[1]
        rc, o3, e3 = run(srv, f"SELECT myvector_ann_set('{idx}','id',{qvecs[0]},'nn=3');")
        record("11b. a rebuild after the table was emptied publishes the empty graph",
               o1.startswith("SUCCESS") and o2 == "SUCCESS" and o3 == "[]",
               f"build={o1[:40]} load={o2} search={o3!r}")

        # --- 10. innodb_page_size = 4096 ---------------------------------
        srv4 = start_server(args.image, f"myv-innodb4k-{os.getpid()}",
                            ["--innodb-page-size=4096", "--gtid-mode=ON",
                             "--enforce-gtid-consistency=ON"])
        servers.append(srv4)
        srv4.wait_ready()
        idle.install_component(srv4, args.component_dir)
        srv4.sql("CREATE DATABASE vtest;", db=None)
        create(srv4, "t", ",storage=innodb")
        rc, out, err = build(srv4, idx)
        ok4 = out.startswith("SUCCESS") and load(srv4, idx)[1] == "SUCCESS"
        record("10. innodb_page_size=4096: install, build and load", ok4, out or err)
    except Exception as e:  # noqa: BLE001
        record("test ran to completion", False, repr(e))
    return finish(results, servers, args.keep)


def finish(results, servers, keep):
    if not all(results):
        for srv in servers:
            log = idle.sh(["docker", "logs", "--tail", "40", srv.name], check=False)
            print(f"--- {srv.name} log (tail) ---")
            print((log.stdout + log.stderr)[-4000:])
    for srv in servers:
        if keep:
            print(f"container kept: {srv.name}")
        else:
            srv.remove()
    ok = bool(results) and all(results)
    print(f"\n{'ALL PASSED' if ok else 'FAILED'} ({sum(results)}/{len(results)})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
