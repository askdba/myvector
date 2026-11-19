#!/usr/bin/env python3
"""Online indexes stored in InnoDB (RFC-005 Phase 1b): online=Y with
storage=innodb. The binlog listener keeps the index current, checkpoints it
into the myvector.* tables on a timer, and after a restart resumes from the
GTID set stored with the last checkpoint.

Every check compares the index with its table: the index's row count equals
the table's (a row change applied twice shows up as one row too many), a search
with a row's own vector returns that row, and a deleted row is never returned.

Component build (8.4, 9.7, 26.10). Part A runs with the checkpoint timer off
(myvector_checkpoint_interval=3600), part B with a 2 second timer:
  A1. Build: the stored index records the transactions it includes.
  A2. Online INSERT / UPDATE / DELETE reach the index. An explicit load of the
      stored index, now older than the index in memory, is refused and leaves
      the index in memory alone.
  A3. A binlog rotation does not checkpoint it (that is the file backend's
      trigger); nothing is written until the timer says so.
  A4. A 10,000-row transaction reaches the index.
  A5. kill -9 with no checkpoint since the build: after the restart the index
      is loaded from the build's rows and catches up from the binlog, applying
      nothing twice.
  A6. A transaction with a tagged GTID (MySQL 8.4+) reaches the index.
  A7. A build while rows are being inserted misses none and doubles none.
  A8. The binary logs since an index's last checkpoint are purged: after a
      restart that index is not loaded and says it must be rebuilt, the other
      index works, and a rebuild recovers.
  B1. A timer checkpoint writes new versions of the changed chunks only, drops
      the versions it replaced, and writes nothing to the binlog.
  B2. An idle index is not rewritten; when only other tables change (rows, or
      a DDL statement as the last transaction), only its stored GTID set moves.
  B3. A tagged GTID is recorded in the stored set.
  B4. kill -9 after a checkpoint and more changes: nothing missing, nothing
      applied twice.
  B5. kill -9 in the middle of a checkpoint (rows written, not yet published):
      the restart loads the previous checkpoint, and the next one cleans up.
  B6. A file-backed and an InnoDB-stored online index side by side survive a
      kill -9.
  B7. Repeated kill -9 under a write load: every restart loads a consistent
      index that catches up.
Plugin build: nothing to test (storage=innodb needs the component).

Usage:
  python3 scripts/test-innodb-online.py --component-dir dist/component-8.4 \\
      --image mysql:8.4

Exit 0 = all checks passed.
"""

import argparse
import importlib.util
import os
import random
import re
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "online_idle", os.path.join(HERE, "test-online-updates-idle.py"))
idle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(idle)

DIM = 8
ROWS = 2000
SIZE = 60000
SMALL_PACKET = 70000  # clamps the chunks of a build to a few dozen nodes each
CONFIGS = ("/var/lib/mysql/myvector.cnf", "/myvector.cnf")


def vec_text(row_id, version):
    """The vector of row `row_id` after `version` updates: reproducible, so a
    deleted row's last vector is known without having kept it."""
    rnd = random.Random(row_id * 1000 + version)
    return "[" + ",".join(f"{rnd.uniform(-1, 1):.5f}" for _ in range(DIM)) + "]"


class Table:
    """A table with an online index, and what the test did to it."""

    def __init__(self, srv, name, opts):
        self.srv = srv
        self.name = name
        self.idx = f"vtest.{name}.v"
        self.next_id = 1
        self.version = {}  # id -> number of updates, for rows believed present
        self.deleted = {}  # id -> last vector text, for rows believed deleted
        self.touched = []  # ids changed most recently
        srv.sql(f"CREATE TABLE {name} (id INT PRIMARY KEY, v VARBINARY({DIM * 4 + 64}) "
                f"NULL COMMENT 'MYVECTOR COLUMN type=hnsw,dim={DIM},size={SIZE},"
                f"idcol=id,dist=L2,online=Y{opts}');")

    def insert_sql(self, n):
        ids = list(range(self.next_id, self.next_id + n))
        self.next_id += n
        vals = ", ".join(f"({i}, myvector_construct('{vec_text(i, 0)}'))" for i in ids)
        return ids, f"INSERT INTO {self.name} VALUES {vals};"

    def insert(self, n, check=True, prefix=""):
        ids, q = self.insert_sql(n)
        self.srv.sql(prefix + q, check=check)
        for i in ids:
            self.version[i] = 0
        self.touched = ids
        return ids

    def update(self, n, check=True):
        ids = random.sample(sorted(self.version), min(n, len(self.version)))
        q = "".join(f"UPDATE {self.name} SET v = myvector_construct("
                    f"'{vec_text(i, self.version[i] + 1)}') WHERE id = {i};" for i in ids)
        self.srv.sql("BEGIN;" + q + "COMMIT;", check=check)
        for i in ids:
            self.version[i] += 1
        self.touched = ids
        return ids

    def delete(self, n, check=True):
        ids = random.sample(sorted(self.version), min(n, len(self.version)))
        self.srv.sql(f"DELETE FROM {self.name} WHERE id IN ({','.join(map(str, ids))});",
                     check=check)
        for i in ids:
            self.deleted[i] = vec_text(i, self.version.pop(i))
        return ids

    def resync(self):
        """After a kill -9 the last statement may or may not have committed:
        the table is the truth."""
        present = {int(r[0]) for r in self.srv.sql(f"SELECT id FROM {self.name};")}
        for i in list(self.version):
            if i not in present:
                self.version.pop(i)
        for i in list(self.deleted):
            if i in present:
                self.deleted.pop(i)
        self.touched = [i for i in self.touched if i in present]
        return present


def main():
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--component-dir")
    mode.add_argument("--plugin-dir")
    ap.add_argument("--image", default="mysql:8.4")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    if not args.component_dir:
        print("[PASS] plugin build: storage=innodb needs the component; nothing to test")
        print("\nALL PASSED (1/1)")
        return 0

    results = []
    name = f"myv-ionline-{os.getpid()}"
    idle.sh(["docker", "run", "-d", "--name", name,
             "-e", f"MYSQL_ROOT_PASSWORD={idle.PW}", "-e", "MYSQL_ROOT_HOST=%",
             args.image, "--gtid-mode=ON", "--enforce-gtid-consistency=ON"])
    srv = object.__new__(idle.Server)
    srv.name = name

    def record(what, ok, detail=""):
        results.append(bool(ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {what}: {detail}"[:500], flush=True)

    def run(q, db="vtest"):
        cmd = ["docker", "exec", "-i", srv.name, "mysql", "-uroot",
               f"-p{idle.PW}", "-N", "-B"]
        if db:
            cmd.append(db)
        r = idle.sh(cmd, inp=q, check=False)
        err = "\n".join(l for l in r.stderr.splitlines() if "Using a password" not in l)
        return r.returncode, r.stdout.strip(), err.strip()

    def set_interval(seconds):
        """The component reads myvector.cnf when it starts."""
        for path in CONFIGS:
            idle.sh(["docker", "exec", srv.name, "bash", "-c",
                     f"sed -i '/myvector_checkpoint_interval/d' {path} && "
                     f"echo 'myvector_checkpoint_interval={seconds}' >> {path}"])
        idle.sh(["docker", "restart", srv.name])
        srv.wait_ready()

    def crash():
        """kill -9 mysqld (pid 1 of the container) and start it again."""
        idle.sh(["docker", "kill", "-s", "KILL", srv.name])
        idle.sh(["docker", "start", srv.name])
        srv.wait_ready()

    def meta(t):
        rows = srv.sql("SELECT index_id, generation, ckpt_seq, ckpt_gtids, ckpt_time, "
                       f"node_count FROM myvector.index_meta WHERE index_name = '{t.idx}' "
                       "AND state = 'active';", db=None)
        if not rows:
            return None
        r = rows[0]
        return {"id": r[0], "gen": int(r[1]), "seq": int(r[2]),
                "gtids": r[3].replace("\\n", ""), "time": r[4], "nodes": int(r[5])}

    def versions(t, table, key):
        """(rows, distinct keys, rows at the newest checkpoint) of the active
        generation."""
        m = meta(t)
        r = srv.sql(f"SELECT COUNT(*), COUNT(DISTINCT {key}), "
                    f"COALESCE(SUM(version = {m['seq']}), 0), "
                    f"COALESCE(SUM(version > {m['seq']}), 0) FROM myvector.{table} "
                    f"WHERE index_id = {m['id']} AND generation = {m['gen']};", db=None)[0]
        return tuple(int(x) for x in r)

    def index_rows(t):
        rc, out, err = run(f"CALL mysql.myvector_index_status('{t.idx}');")
        m = re.search(r"Current Rows : (\d+)", out.replace("\\n", "\n"))
        return int(m.group(1)) if m else -1

    def verify(t, exact=True, timeout=90):
        """(ok, detail): the index holds exactly the table's rows."""
        present = t.resync()
        deadline = time.time() + timeout
        rows = -1
        while time.time() < deadline:
            rows = index_rows(t)
            if rows == len(present):
                break
            time.sleep(1)
        ids = list(dict.fromkeys(
            t.touched[-60:] + random.sample(sorted(present), min(20, len(present)))))
        gone = random.sample(sorted(t.deleted), min(30, len(t.deleted)))
        q = "".join(f"SELECT myvector_ann_set('{t.idx}','id',(SELECT v FROM {t.name} "
                    f"WHERE id = {i}),'nn=1,ef_search=400');" for i in ids)
        q += "".join(f"SELECT myvector_ann_set('{t.idx}','id',myvector_construct("
                     f"'{t.deleted[i]}'),'nn=1,ef_search=400');" for i in gone)
        missing, returned = ids, []
        for _ in range(max(1, int(deadline - time.time()))):
            rc, out, err = run(q)
            got = out.splitlines()
            if rc != 0 or len(got) != len(ids) + len(gone):
                missing, returned = ids, [err[:120]]
            else:
                missing = [i for i, g in zip(ids, got) if g != f"[{i}]"]
                returned = [i for i, g in zip(gone, got[len(ids):]) if g == f"[{i}]"]
            if not missing and not returned:
                break
            time.sleep(1)
        ok = not missing and not returned and (rows == len(present) or not exact)
        return ok, (f"table {len(present)} rows, index {rows}; {len(ids)} present rows "
                    f"checked ({len(missing)} not found), {len(gone)} deleted rows checked "
                    f"({len(returned)} still returned) {returned[:1] if missing else ''}")

    def build(t, small=False):
        if small:
            srv.sql(f"SET GLOBAL max_allowed_packet = {SMALL_PACKET};", db=None)
        rc, out, err = run(f"CALL mysql.myvector_index_build('{t.idx}','id');")
        if small:
            srv.sql("SET GLOBAL max_allowed_packet = 67108864;", db=None)
        return out or err

    def wait_checkpoint(t, before, what="seq", timeout=30):
        """Wait until the stored checkpoint moves on from `before` (a meta())."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            m = meta(t)
            if m and m[what] != before[what]:
                return m
            time.sleep(0.5)
        return None

    def binlog_mentions():
        logs = [r[0] for r in srv.sql("SHOW BINARY LOGS;", db=None)]
        events, mentions = 0, 0
        for lg in logs:
            for r in srv.sql(f"SHOW BINLOG EVENTS IN '{lg}';", db=None):
                events += 1
                line = "\t".join(r)
                if "myvector.index_" in line or "`myvector`" in line:
                    mentions += 1
        return events, mentions

    class Writer(threading.Thread):
        """Inserts, updates and deletes rows of a table until stopped. Errors
        are expected when the server is killed under it."""

        def __init__(self, t, mixed):
            super().__init__(daemon=True)
            self.t, self.mixed, self.stop_flag, self.batches = t, mixed, False, 0

        def run(self):
            while not self.stop_flag:
                try:
                    self.t.insert(100, check=False)
                    if self.mixed:
                        self.t.update(20, check=False)
                        self.t.delete(10, check=False)
                    self.batches += 1
                except Exception:
                    time.sleep(0.2)

        def stop(self):
            self.stop_flag = True
            self.join(timeout=120)

    try:
        srv.wait_ready()
        idle.install_component(srv, args.component_dir)
        srv.sql("CREATE DATABASE vtest;", db=None)
        srv.sql("CREATE TABLE other (id INT PRIMARY KEY AUTO_INCREMENT, n INT);")

        # ================= Part A: no timer checkpoints =================
        set_interval(3600)
        a = Table(srv, "a", ",storage=innodb")
        for _ in range(ROWS // 500):
            a.insert(500)

        out = build(a, small=True)
        m = meta(a)
        executed = srv.sql("SELECT @@GLOBAL.gtid_executed;", db=None)[0][0].replace("\\n", "")
        record("A1. build of an online index with storage=innodb",
               out.startswith("SUCCESS") and m is not None and m["seq"] == 1 and
               m["gtids"] != "" and m["gtids"] == executed,
               f"{out[:70]} | stored set {m['gtids'][:60] if m else None}")
        ok, detail = verify(a)
        record("A1. the index matches the table after the build", ok, detail)

        a.insert(300)
        a.update(100)
        a.delete(100)
        a.touched = a.touched + sorted(a.version)[-300:]
        ok, detail = verify(a)
        record("A2. online INSERT / UPDATE / DELETE reach the index", ok, detail)

        rc, out, err = run(f"CALL mysql.myvector_index_load('{a.idx}');")
        ok, detail = verify(a)
        record("A2. a load of the older stored index is refused and leaves the index "
               "in memory as it is",
               out.startswith("ERROR") and "older than the binlog listener" in out and ok,
               f"{(out or err)[:150]} | {detail}")

        srv.sql("FLUSH BINARY LOGS;", db=None)
        a.insert(50)
        srv.sql("FLUSH BINARY LOGS;", db=None)
        time.sleep(4)
        m2 = meta(a)
        record("A3. a binlog rotation does not checkpoint the index",
               m2["seq"] == 1 and m2["time"] == m["time"],
               f"checkpoint {m2['seq']} at {m2['time']}")

        stmts = []
        big = []
        for _ in range(10):
            ids, q = a.insert_sql(1000)
            stmts.append(q)
            big += ids
        srv.sql("BEGIN;" + "".join(stmts) + "COMMIT;")
        for i in big:
            a.version[i] = 0
        a.touched = big[::200] + big[-20:]
        ok, detail = verify(a)
        record("A4. a 10,000-row transaction reaches the index", ok, detail)

        before = meta(a)
        crash()
        after = meta(a)
        ok, detail = verify(a)
        record("A5. kill -9 with no checkpoint since the build: caught up from the "
               "binlog, nothing applied twice",
               ok and before["seq"] == 1 and after["seq"] == 1, detail)

        rc, out, err = run("SET gtid_next = 'AUTOMATIC:mvtag';"
                           + a.insert_sql(40)[1] + "SET gtid_next = 'AUTOMATIC';")
        tagged = rc == 0  # every supported server (8.4+) has tagged GTIDs
        for i in range(a.next_id - 40, a.next_id):
            a.version[i] = 0
        a.touched = list(range(a.next_id - 40, a.next_id))
        ok, detail = verify(a)
        record("A6. a transaction with a tagged GTID reaches the index", tagged and ok,
               err[:100] or detail)

        # The binary logs up to here go away. Index a's stored set is still
        # the one from its build; index b is built after the purge.
        srv.sql("FLUSH BINARY LOGS;", db=None)
        current = srv.sql("SHOW BINARY LOGS;", db=None)[-1][0]
        srv.sql(f"PURGE BINARY LOGS TO '{current}';", db=None)
        b = Table(srv, "b", ",storage=innodb")
        b.insert(500)
        w = Writer(b, mixed=False)
        w.start()
        time.sleep(1)
        out = build(b)
        time.sleep(1)
        w.stop()
        ok, detail = verify(b)
        record("A7. a build while rows are inserted misses none and doubles none",
               out.startswith("SUCCESS") and ok and w.batches > 0,
               f"{out[:50]} | {w.batches} batches | {detail}")

        idle.sh(["docker", "restart", srv.name])
        srv.wait_ready()
        okb, detailb = verify(b)
        rc, out, err = run(f"SELECT myvector_ann_set('{a.idx}','id',"
                           f"myvector_construct('{vec_text(1, 0)}'),'nn=1');")
        log = srv.log()
        record("A8. purged binary logs: the index is not loaded after the restart",
               rc != 0 and "no longer on this server" in log, (err or out)[:160])
        record("A8. the other index works", okb, detailb)
        rc, out, err = run(f"CALL mysql.myvector_index_load('{a.idx}');")
        record("A8. an explicit load is refused and asks for a rebuild",
               out.startswith("ERROR") and "MYVECTOR_INDEX_BUILD" in out, (out or err)[:200])
        out = build(a, small=True)
        ok, detail = verify(a)
        record("A8. a rebuild recovers", out.startswith("SUCCESS") and ok,
               f"{out[:50]} | {detail}")

        # ================= Part B: checkpoints every 2 seconds =================
        set_interval(2)
        ok, detail = verify(a)
        record("B0. loaded after the restart", ok, detail)
        time.sleep(5)  # let the first timer checkpoint, if any, pass

        m0 = meta(a)
        a.update(1)
        executed = srv.sql("SELECT @@GLOBAL.gtid_executed;", db=None)[0][0]
        m1 = wait_checkpoint(a, m0)
        v = versions(a, "index_vectors", "chunk_id") if m1 else (0, 0, 0, 0)
        l = versions(a, "index_links", "chunk_id") if m1 else (0, 0, 0, 0)
        record("B1. a timer checkpoint writes the changed chunks only",
               m1 is not None and 1 <= v[2] <= 2 and v[1] >= 20 and 1 <= l[2] < l[1],
               f"checkpoint {m0['seq']} -> {m1['seq'] if m1 else None}; vector chunks: "
               f"{v[2]} of {v[1]} rewritten; link chunks: {l[2]} of {l[1]}")
        record("B1. the versions it replaced are gone, nothing is left unpublished",
               v[0] == v[1] and l[0] == l[1] and v[3] == 0 and l[3] == 0,
               f"vector rows {v[0]} for {v[1]} chunks; link rows {l[0]} for {l[1]} chunks")
        executed2 = srv.sql("SELECT @@GLOBAL.gtid_executed;", db=None)[0][0]
        events, mentions = binlog_mentions()
        record("B1. the checkpoint wrote nothing to the binlog",
               executed2 == executed and events > 0 and mentions == 0,
               f"events={events} mentions of myvector.*={mentions}")
        ok, detail = verify(a)
        record("B1. the index matches the table", ok, detail)

        m1 = meta(a)
        time.sleep(7)
        m2 = meta(a)
        record("B2. an idle index is not rewritten",
               m2["seq"] == m1["seq"] and m2["time"] == m1["time"],
               f"checkpoint {m2['seq']} at {m2['time']}")
        srv.sql("INSERT INTO other (n) VALUES (1), (2), (3);")
        m3 = wait_checkpoint(a, m2, "gtids")
        record("B2. changes to other tables only move its stored GTID set",
               m3 is not None and m3["seq"] == m2["seq"] and
               versions(a, "index_vectors", "chunk_id")[:2] == v[:2],
               f"checkpoint {m3['seq'] if m3 else None}; set {m3['gtids'][-40:] if m3 else None}")

        srv.sql("CREATE TABLE ddl_last (id INT PRIMARY KEY);")
        m5 = wait_checkpoint(a, m3, "gtids") if m3 else None
        record("B2. a DDL statement as the last transaction is recorded too",
               m5 is not None and m5["seq"] == m2["seq"],
               f"set {m5['gtids'][-40:] if m5 else None}")

        mt = meta(a)
        rc, out, err = run("SET gtid_next = 'AUTOMATIC:mvtag';" + a.insert_sql(5)[1])
        for i in range(a.next_id - 5, a.next_id):
            a.version[i] = 0
        m4 = wait_checkpoint(a, mt)
        record("B3. a tagged GTID is recorded in the stored set",
               rc == 0 and m4 is not None and ":mvtag:" in m4["gtids"],
               err[:100] or (m4["gtids"][-60:] if m4 else "no checkpoint"))

        mb = meta(a)
        a.insert(400)
        a.update(100)
        a.delete(100)
        mc = wait_checkpoint(a, mb)
        ins = a.insert(300)
        a.update(50)
        a.delete(50)
        a.touched = a.touched + ins
        crash()
        ok, detail = verify(a)
        record("B4. kill -9 after a checkpoint and more changes: nothing missing, "
               "nothing applied twice", mc is not None and ok, detail)

        # B5: another session holds a read lock on index_meta, so a checkpoint
        # writes its chunk rows and then waits to publish them.
        time.sleep(5)
        mb = meta(a)
        holder = subprocess.Popen(
            ["docker", "exec", "-i", srv.name, "mysql", "-uroot", f"-p{idle.PW}", "-e",
             "LOCK TABLES myvector.index_meta READ; SELECT SLEEP(300);"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
        a.update(30)
        unpublished = 0
        for _ in range(40):
            unpublished = versions(a, "index_links", "chunk_id")[3]
            if unpublished:
                break
            time.sleep(0.5)
        mk = meta(a)  # still the checkpoint from before: the new rows are not published
        # And what a checkpoint interrupted after publishing leaves: an older
        # version of rows that have a newer one.
        srv.sql("SET sql_log_bin = 0; INSERT INTO myvector.index_links SELECT index_id, "
                "generation, chunk_id, 0, level0, crc32 FROM myvector.index_links WHERE "
                f"index_id = {mk['id']} AND generation = {mk['gen']} AND version <= "
                f"{mk['seq']} LIMIT 5;", db=None)
        replaced = versions(a, "index_links", "chunk_id")
        crash()
        holder.wait(timeout=30)
        ok, detail = verify(a)
        record("B5. kill -9 mid-checkpoint: the restart loads the published checkpoint "
               "and the index catches up",
               unpublished > 0 and mk["seq"] == mb["seq"] and ok,
               f"{unpublished} unpublished link rows at the kill, checkpoint still "
               f"{mk['seq']}; {detail}")
        md = meta(a)
        a.update(5)
        me = wait_checkpoint(a, md)
        v = versions(a, "index_vectors", "chunk_id") if me else (0, 1, 0, 1)
        l = versions(a, "index_links", "chunk_id") if me else (0, 1, 0, 1)
        record("B5. the next checkpoint removes the unpublished rows and the replaced "
               "versions an interrupted one left",
               me is not None and replaced[0] > replaced[1] and v[0] == v[1] and
               l[0] == l[1] and v[3] == 0 and l[3] == 0,
               f"vector rows {v[0]} for {v[1]} chunks; link rows {l[0]} for {l[1]} chunks "
               f"({replaced[0]} for {replaced[1]} at the kill)")

        f = Table(srv, "f", "")
        f.insert(500)
        out = build(f)
        f.insert(200)
        f.delete(50)
        a.insert(200)
        mb = meta(a)
        a.delete(50)
        wait_checkpoint(a, mb)
        f.insert(100)
        a.insert(100)
        crash()
        oka, detaila = verify(a)
        okf, detailf = verify(f, exact=False)
        record("B6. a file-backed and an InnoDB-stored online index survive a kill -9",
               out.startswith("SUCCESS") and oka and okf, f"innodb: {detaila} | file: {detailf}")

        for round_no in range(1, 4):
            w = Writer(a, mixed=True)
            w.start()
            time.sleep(random.uniform(3, 7))
            idle.sh(["docker", "kill", "-s", "KILL", srv.name])
            w.stop()
            idle.sh(["docker", "start", srv.name])
            srv.wait_ready()
            ok, detail = verify(a)
            record(f"B7. kill -9 under a write load, round {round_no}", ok,
                   f"{w.batches} batches | {detail}")

        events, mentions = binlog_mentions()
        record("B8. no index rows in the binlog at the end", mentions == 0,
               f"events={events} mentions={mentions}")
        errors = [ln for ln in srv.log().splitlines()
                  if "checkpoint into InnoDB failed" in ln]
        record("B8. no checkpoint failed", not errors, errors[0][:200] if errors else "")
    except Exception as e:  # a crashed step: report it and show the log
        record("test run", False, f"{type(e).__name__}: {e}")

    if not all(results):
        log = idle.sh(["docker", "logs", srv.name], check=False)
        lines = [ln for ln in (log.stdout + log.stderr).splitlines()
                 if ln.startswith("MyVector:") or "MYVEC ERR" in ln or "not loaded" in ln
                 or "[ERROR]" in ln]
        print(f"--- {srv.name} log (MyVector messages and errors) ---")
        print("\n".join(ln[:300] for ln in lines if "index not found" not in ln)[-8000:])
    if args.keep:
        print(f"container kept: {srv.name}")
    else:
        srv.remove()
    ok = bool(results) and all(results)
    print(f"\n{'ALL PASSED' if ok else 'FAILED'} ({sum(results)}/{len(results)})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
