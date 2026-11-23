#!/usr/bin/env python3
"""RFC-005 Phase 3: measure storage=innodb against storage=file.

One fresh MySQL server (Docker) per storage backend, the same data and the
same steps on both, results as JSON. The table has an online index
(online=Y), so builds, loads, checkpoints and restarts are all measured on the
kind of index that needs them.

Measurements (--measure, default all):
  build     build and save: time, stored bytes, InnoDB redo and data written
  load      MYVECTOR_INDEX_LOAD of the stored index (buffer pool warm), and
            whether searches return the same rows before and after
  search    queries per second with 1 and 8 sessions; recall@10 against exact
            neighbors (from the dataset, or a brute-force scan)
  ckpt      cost of a checkpoint after N inserts and after N vector updates
  stall     time from an insert's commit to the row being searchable (sampled
            about twenty times a second), under a steady insert load with a
            short checkpoint interval
  restart   time from server start to an index that is loaded and caught up,
            after a clean restart and after kill -9 with changes not yet
            checkpointed

Tiers (--tier): smoke 5k x 32, t1 100k x 128, t3 1M x 768 (synthetic), and
t2 = an ann-benchmarks HDF5 file (--hdf5), e.g. fashion-mnist-784-euclidean.

Usage:
  python3 scripts/bench-innodb-storage.py --tier t1 \\
      --component-dir dist/component-8.4 --image mysql:8.4 \\
      --workdir /data/bench-p3 --output results/rfc005-phase3/t1-8.4.json

Needs numpy (and h5py for --hdf5). The work directory holds the data files the
server loads; it must be readable by the container.
"""

import argparse
import importlib.util
import json
import os
import random
import re
import struct
import subprocess
import sys
import threading
import time
import zlib

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "online_idle", os.path.join(HERE, "test-online-updates-idle.py"))
idle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(idle)

TIERS = {
    "smoke": dict(rows=5000, dim=32, inserts=[500], updates=[200], behind=1000),
    "t1": dict(rows=100000, dim=128, inserts=[1000, 10000, 100000],
               updates=[1000, 10000], behind=100000),
    "t2": dict(rows=None, dim=None, inserts=[1000, 10000], updates=[1000, 10000],
               behind=10000),
    "t3": dict(rows=1000000, dim=768, inserts=[1000, 10000, 100000],
               updates=[1000, 10000], behind=100000),
}
PROFILES = {
    "default": [],
    "sized": ["--innodb-buffer-pool-size=16G", "--innodb-redo-log-capacity=8G"],
}
CONFIGS = ("/var/lib/mysql/myvector.cnf", "/myvector.cnf")
FILES = "/var/lib/mysql-files"  # the image's secure_file_priv


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Client:
    """A mysql client that stays connected, for timings finer than the cost
    of starting one."""

    def __init__(self, container):
        self.p = subprocess.Popen(
            ["docker", "exec", "-i", container, "mysql", "-uroot", f"-p{idle.PW}",
             "-N", "-B", "-n", "vtest"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1)

    def query(self, sql):
        self.p.stdin.write(sql.rstrip(";") + ";\nSELECT '__end__';\n")
        self.p.stdin.flush()
        out = []
        for line in self.p.stdout:
            line = line.rstrip("\n")
            if line == "__end__":
                break
            out.append(line)
        return out

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=10)
        except Exception:
            self.p.kill()


class Bench:
    def __init__(self, args, storage, tier):
        self.args, self.storage, self.tier = args, storage, tier
        self.name = f"myv-p3-{storage}-{os.getpid()}"
        self.dir = os.path.join(args.workdir, self.name)
        os.makedirs(self.dir, exist_ok=True)
        os.chmod(self.dir, 0o777)
        self.srv = None
        self.next_id = 0
        self.rows = 0
        self.res = {"storage": storage}
        self.trailer = None  # per-row trailer of the stored vector format
        self.rng = np.random.default_rng(42)

    # ---- server ---------------------------------------------------------
    def start(self):
        idle.sh(["docker", "run", "-d", "--name", self.name,
                 "-e", f"MYSQL_ROOT_PASSWORD={idle.PW}", "-e", "MYSQL_ROOT_HOST=%",
                 "-v", f"{self.dir}:{FILES}", self.args.image,
                 "--gtid-mode=ON", "--enforce-gtid-consistency=ON"]
                + PROFILES[self.args.profile])
        self.srv = object.__new__(idle.Server)
        self.srv.name = self.name
        self.srv.wait_ready()
        idle.install_component(self.srv, self.args.component_dir)
        self.srv.sql("CREATE DATABASE vtest;", db=None)
        self.configure(self.args.interval)
        version = self.srv.sql("SELECT VERSION();", db=None)[0][0]
        self.res["server"] = {"version": version, "image": self.args.image,
                              "profile": self.args.profile,
                              "args": PROFILES[self.args.profile]}
        # Before MySQL 9 a stored vector carries 4 bytes of metadata and a CRC.
        self.legacy = int(version.split(".")[0]) < 9

    def configure(self, interval, restart=True):
        lines = [f"myvector_checkpoint_interval={interval}"]
        if self.args.vec_chunk:
            lines.append(f"myvector_index_vec_chunk_bytes={self.args.vec_chunk}")
        if self.args.lnk_chunk:
            lines.append(f"myvector_index_lnk_chunk_bytes={self.args.lnk_chunk}")
        text = "\\n".join(lines) + "\\n"
        for path in CONFIGS:
            idle.sh(["docker", "exec", self.name, "bash", "-c",
                     f"sed -i '/checkpoint_interval/d;/_chunk_bytes/d' {path} && "
                     f"printf '{text}' >> {path}"])
        if restart:
            self.restart()

    def restart(self):
        # A long stop timeout: docker's default 10 s kills a mysqld that is
        # still shutting down, which would make this a crash, not a restart.
        idle.sh(["docker", "restart", "-t", "900", self.name], timeout=1800)
        self.srv.wait_ready()

    def sql(self, q, db="vtest", timeout=7200):
        args = ["docker", "exec", "-i", self.name, "mysql", "-uroot", f"-p{idle.PW}",
                "-N", "-B"]
        if db:
            args.append(db)
        r = idle.sh(args, inp=q, timeout=timeout)
        return [ln.split("\t") for ln in r.stdout.splitlines() if ln]

    def status(self, name):
        return int(self.sql(f"SHOW GLOBAL STATUS LIKE '{name}';", db=None)[0][1])

    def counters(self):
        return {"redo": self.status("Innodb_os_log_written"),
                "data": self.status("Innodb_data_written")}

    def server_log(self):
        return self.srv.log()

    # ---- data -----------------------------------------------------------
    def encode(self, vecs):
        """Hex of the stored value of each vector."""
        out = []
        for v in vecs:
            b = v.astype("<f4").tobytes()
            if self.legacy:
                b += struct.pack("<I", 0x0101)
                b += struct.pack("<I", zlib.crc32(b) & 0xFFFFFFFF)
            out.append(b.hex())
        return out

    def load_rows(self, vecs, table="t"):
        """Append rows through LOAD DATA; returns their ids."""
        ids = list(range(self.next_id + 1, self.next_id + 1 + len(vecs)))
        self.next_id += len(vecs)
        step = 20000
        for a in range(0, len(vecs), step):
            fn = f"load-{ids[a]}.tsv"
            with open(os.path.join(self.dir, fn), "w") as f:
                for i, h in zip(ids[a:a + step], self.encode(vecs[a:a + step])):
                    f.write(f"{i}\t{h}\n")
            os.chmod(os.path.join(self.dir, fn), 0o644)
            self.sql(f"LOAD DATA INFILE '{FILES}/{fn}' INTO TABLE {table} (id, @h) "
                     "SET v = UNHEX(@h);")
            os.remove(os.path.join(self.dir, fn))
        self.rows += len(vecs)
        return ids

    def vectors(self, n):
        return self.rng.uniform(-1, 1, size=(n, self.dim)).astype("float32")

    def create(self):
        t = self.tier
        if self.args.hdf5:
            import h5py
            with h5py.File(self.args.hdf5, "r") as f:
                self.train = np.array(f["train"], dtype="float32")
                self.test = np.array(f["test"], dtype="float32")[:200]
                self.truth = np.array(f["neighbors"])[:200, :10]
            self.dim = self.train.shape[1]
            base = self.train
            self.id_offset = 1  # row id = train index + 1
        else:
            self.dim = t["dim"]
            base = None
        rows = len(base) if base is not None else t["rows"]
        extra = sum(t["inserts"]) + t["behind"] + 200000
        opts = (f"type=hnsw,dim={self.dim},size={rows + extra},idcol=id,dist=L2,"
                f"M=16,ef=100,threads={self.args.threads},online=Y")
        if self.storage == "innodb":
            opts += ",storage=innodb"
        self.sql(f"CREATE TABLE t (id INT PRIMARY KEY, v VARBINARY({self.dim * 4 + 64}) "
                 f"NULL COMMENT 'MYVECTOR COLUMN {opts}');")
        self.res["index"] = {"rows": rows, "dim": self.dim, "options": opts}
        # The stored format computed here must be what myvector_construct makes.
        probe = self.vectors(1)
        text = "[" + ",".join(f"{x:.6f}" for x in probe[0]) + "]"
        got = self.sql(f"SELECT HEX(myvector_construct('{text}'));")[0][0].lower()
        want = self.encode(np.array([[float(f"{x:.6f}") for x in probe[0]]],
                                    dtype="float32"))[0]
        if got != want:
            raise RuntimeError("stored vector format differs from myvector_construct()")
        log(f"{self.storage}: loading {rows} x {self.dim}")
        t0 = time.time()
        if base is not None:
            self.load_rows(base)
            self.queries = self.test
        else:
            for a in range(0, rows, 100000):
                self.load_rows(self.vectors(min(100000, rows - a)))
            self.queries = self.vectors(200)
            self.truth = None
        self.res["data_load_s"] = round(time.time() - t0, 1)

    # ---- helpers --------------------------------------------------------
    def index_rows(self):
        out = self.sql("CALL mysql.myvector_index_status('vtest.t.v');")
        m = re.search(r"Current Rows : (\d+)", out[0][0].replace("\\n", "\n")) if out else None
        return int(m.group(1)) if m else -1

    def wait_applied(self, timeout=7200):
        """Until the index holds every row of the table; seconds waited."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.index_rows() == self.rows:
                return time.time() - t0
            time.sleep(0.5)
        raise RuntimeError(f"index has {self.index_rows()} rows, table {self.rows}")

    def ckpt_lines(self):
        """(seq, ms, vector chunks, link chunks, upper rows, bytes) of every
        InnoDB checkpoint logged so far."""
        pat = re.compile(r"checkpoint (\d+) written to InnoDB in (\d+) ms: (\d+) vector "
                         r"chunks, (\d+) link chunks, (\d+) upper rows, (\d+) bytes")
        return [tuple(int(x) for x in m.groups()) for m in pat.finditer(self.server_log())]

    def settled(self):
        """True when the stored index includes everything the server executed."""
        r = self.sql("SELECT GTID_SUBSET(@@GLOBAL.gtid_executed, ckpt_gtids) FROM "
                     "myvector.index_meta WHERE index_name = 'vtest.t.v' AND "
                     "state = 'active';", db=None)
        return bool(r) and r[0][0] == "1"

    def wait_settled(self, timeout=7200):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.settled():
                return time.time() - t0
            time.sleep(1)
        raise RuntimeError("the stored index did not catch up")

    def file_checkpoint(self):
        """Trigger the file backend's checkpoint (a binlog rotation) and wait
        for it. A rotation makes two: one for the changes, one at the new
        file's start. Returns their total duration in ms, from the log's own
        timestamps."""
        pat = re.compile(r"ts=(\d+):(\d+),ckptid=[^,]*,status=(\d+)\|")
        seen = len(pat.findall(self.server_log()))
        self.sql("FLUSH BINARY LOGS;", db=None)
        t0, last, quiet = time.time(), -1, 0
        while time.time() - t0 < 3600:
            ts = pat.findall(self.server_log())[seen:]
            done = sum(1 for x in ts if x[2] == "11000")
            if done and len(ts) == last:
                quiet += 1
                if quiet >= 4:  # nothing new for two seconds
                    total, begin = 0.0, None
                    for sec, usec, st in ts:
                        t = int(sec) * 1e6 + int(usec)
                        if st == "10001":
                            begin = t
                        elif st == "11000" and begin is not None:
                            total += t - begin
                            begin = None
                    return round(total / 1000, 1)
            else:
                quiet = 0
            last = len(ts)
            time.sleep(0.5)
        raise RuntimeError("no file checkpoint")

    def proc_write_bytes(self):
        r = idle.sh(["docker", "exec", "-u", "mysql", self.name, "cat", "/proc/1/io"])
        return int(re.search(r"^write_bytes: (\d+)", r.stdout, re.M).group(1))

    def stored_bytes(self):
        if self.storage == "innodb":
            r = idle.sh(["docker", "exec", self.name, "bash", "-c",
                         "du -sb /var/lib/mysql/myvector | cut -f1"])
        else:
            r = idle.sh(["docker", "exec", self.name, "bash", "-c",
                         "cat /var/lib/mysql/vtest.t.v.hnsw.index* | wc -c"])
        return int(r.stdout.split()[0])

    def qtext(self, v):
        return "myvector_construct('[" + ",".join(f"{x:.6f}" for x in v) + "]')"

    def search_all(self, nn=10, ef=None):
        opts = f"nn={nn}" + (f",ef_search={ef}" if ef else "")
        q = "".join(f"SELECT myvector_ann_set('vtest.t.v','id',{self.qtext(v)},'{opts}');"
                    for v in self.queries)
        return [r[0] for r in self.sql(q)]

    # ---- measurements ---------------------------------------------------
    def m_build(self):
        c0 = self.counters()
        t0 = time.time()
        out = self.sql("CALL mysql.myvector_index_build('vtest.t.v','id');")[0][0]
        dt = time.time() - t0
        if not out.startswith("SUCCESS"):
            raise RuntimeError(f"build failed: {out}")
        c1 = self.counters()
        r = {"build_s": round(dt, 1), "stored_bytes": self.stored_bytes(),
             "redo_bytes": c1["redo"] - c0["redo"], "data_bytes": c1["data"] - c0["data"]}
        m = re.findall(r"saved to InnoDB as generation \d+ in (\d+) ms: (\d+) vector "
                       r"chunks, (\d+) link chunks, (\d+) upper rows, (\d+) bytes",
                       self.server_log())
        if m:
            ms, vc, lc, ur, by = (int(x) for x in m[-1])
            r.update(save_ms=ms, vector_chunks=vc, link_chunks=lc, upper_rows=ur,
                     save_bytes=by)
        self.res["build"] = r
        log(f"{self.storage}: build {r}")

    def m_load(self):
        if self.storage == "innodb":
            self.wait_settled()
        before = self.search_all()
        times = []
        for _ in range(3):
            t0 = time.time()
            out = self.sql("CALL mysql.myvector_index_load('vtest.t.v');")
            times.append(time.time() - t0)
            if not out or out[0][0] != "SUCCESS":
                raise RuntimeError(f"load failed: {out}")
        after = self.search_all()
        self.res["load"] = {"load_s": [round(x, 2) for x in times],
                            "same_results": before == after}
        log(f"{self.storage}: load {self.res['load']}")

    def m_search(self):
        n = len(self.queries)
        q = "".join(f"SELECT myvector_ann_set('vtest.t.v','id',{self.qtext(v)},'nn=10');"
                    for v in self.queries) * max(1, 2000 // n)
        count = n * max(1, 2000 // n)
        idle.sh(["docker", "exec", "-i", self.name, "bash", "-c", "cat > /tmp/q.sql"], inp=q)
        r = {}
        for sessions in (1, 8):
            best = 0
            for _ in range(3):
                script = (f'M="mysql -uroot -p{idle.PW} -N -B vtest"; s=$(date +%s.%N); '
                          f'for w in $(seq 1 {sessions}); do ( $M < /tmp/q.sql >/dev/null 2>&1 ) & '
                          'done; wait; e=$(date +%s.%N); echo "$s $e"')
                o = idle.sh(["docker", "exec", self.name, "bash", "-c", script]).stdout.split()
                best = max(best, sessions * count / (float(o[1]) - float(o[0])))
            r[f"qps_{sessions}"] = round(best)
        got = [set(int(x) for x in re.findall(r"\d+", s)) for s in self.search_all()]
        if self.truth is not None:
            want = [set(int(i) + 1 for i in row) for row in self.truth]
        else:
            want = []
            for v in self.queries[:20]:
                rows = self.sql("SELECT id FROM t ORDER BY myvector_distance(v, "
                                f"{self.qtext(v)}, 'L2') LIMIT 10;")
                want.append(set(int(x[0]) for x in rows))
        hits = sum(len(g & w) for g, w in zip(got, want))
        r["recall_at_10"] = round(hits / (10 * len(want)), 4)
        r["recall_queries"] = len(want)
        self.res["search"] = r
        log(f"{self.storage}: search {r}")

    def one_checkpoint(self, what, n, change):
        """Apply `change`, then measure the checkpoint that stores it."""
        if self.storage == "innodb":
            self.wait_settled()
        seen = len(self.ckpt_lines())
        c0 = self.counters()
        w_start = self.proc_write_bytes()
        t0 = time.time()
        change()
        apply_s = time.time() - t0 + self.wait_applied()
        r = {"change": what, "n": n, "apply_s": round(apply_s, 1)}
        if self.storage == "innodb":
            self.wait_settled()
            lines = self.ckpt_lines()[seen:]
            for _ in range(20):  # the log can trail the table by a moment
                time.sleep(0.5)
                again = self.ckpt_lines()[seen:]
                if lines and len(again) == len(lines):
                    break
                lines = again
            r.update(checkpoints=len(lines), ckpt_ms=sum(x[1] for x in lines),
                     vector_chunks=sum(x[2] for x in lines),
                     link_chunks=sum(x[3] for x in lines),
                     upper_rows=sum(x[4] for x in lines),
                     ckpt_bytes=sum(x[5] for x in lines))
        else:
            time.sleep(2)  # let the binlog and redo of the change itself settle
            w0 = self.proc_write_bytes()
            r["ckpt_ms"] = self.file_checkpoint()
            r["ckpt_bytes"] = self.proc_write_bytes() - w0
            r["checkpoints"] = 1
        c1 = self.counters()
        r["redo_bytes"] = c1["redo"] - c0["redo"]
        # Everything mysqld wrote for the change and its checkpoint together:
        # the one figure measured the same way for both backends.
        time.sleep(2)
        r["write_bytes_total"] = self.proc_write_bytes() - w_start
        log(f"{self.storage}: checkpoint {r}")
        return r

    def m_ckpt(self):
        out = []
        for n in self.tier["inserts"]:
            out.append(self.one_checkpoint(
                "insert", n, lambda n=n: self.load_rows(self.vectors(n))))
        for n in self.tier["updates"]:
            ids = self.rng.choice(np.arange(1, self.next_id + 1), size=n, replace=False)
            vecs = self.encode(self.vectors(n))

            def change(ids=ids, vecs=vecs):
                q = "".join(f"UPDATE t SET v = UNHEX('{h}') WHERE id = {i};"
                            for i, h in zip(ids, vecs))
                idle.sh(["docker", "exec", "-i", self.name, "mysql", "-uroot",
                         f"-p{idle.PW}", "vtest"], inp=q, timeout=7200)
                time.sleep(2)  # the row count does not change: let them apply
            out.append(self.one_checkpoint("update", n, change))
        self.res["checkpoint"] = out

    def m_stall(self):
        """Insert at a steady rate; about twenty times a second, time a marker
        row from its commit to its first appearance in a search."""
        if self.storage == "innodb":
            self.configure(self.args.stall_interval)
            self.wait_up()  # the restart reloads the index: measure after that
            self.wait_settled()
        stop = threading.Event()
        rate = self.args.stall_rate

        def writer():
            c = Client(self.name)
            while not stop.is_set():
                t0 = time.time()
                vecs = self.encode(self.vectors(rate // 10))
                base = self.next_id + 1
                self.next_id += len(vecs)
                self.rows += len(vecs)
                c.query("INSERT INTO t VALUES " + ", ".join(
                    f"({base + k}, UNHEX('{h}'))" for k, h in enumerate(vecs)))
                time.sleep(max(0, 0.1 - (time.time() - t0)))
            c.close()

        th = threading.Thread(target=writer, daemon=True)
        seen = len(self.ckpt_lines())
        th.start()
        probe = Client(self.name)
        lags = []
        t_end = time.time() + self.args.stall_seconds
        marker = 2000000000
        while time.time() < t_end:
            t_loop = time.time()
            marker += 1
            v = np.full(self.dim, 50.0 + (marker % 1000), dtype="float32")
            h = self.encode([v])[0]
            probe.query(f"INSERT INTO t VALUES ({marker}, UNHEX('{h}'))")
            t0 = time.time()
            q = f"SELECT myvector_ann_set('vtest.t.v','id',UNHEX('{h}'),'nn=1')"
            while time.time() - t0 < 600:
                if probe.query(q) == [f"[{marker}]"]:
                    break
                time.sleep(0.002)
            lags.append(time.time() - t0)
            self.rows += 1
            time.sleep(max(0, 0.05 - (time.time() - t_loop)))
        stop.set()
        th.join(timeout=60)
        probe.close()
        lags.sort()
        r = {"rate_rows_s": rate, "seconds": self.args.stall_seconds,
             "samples": len(lags),
             "lag_ms_p50": round(lags[len(lags) // 2] * 1000, 1),
             "lag_ms_p99": round(lags[min(len(lags) - 1, int(len(lags) * 0.99))] * 1000, 1),
             "lag_ms_max": round(lags[-1] * 1000, 1)}
        self.wait_applied()
        if self.storage == "innodb":
            self.wait_settled()
            lines = self.ckpt_lines()[seen:]
            r.update(interval_s=self.args.stall_interval, checkpoints=len(lines),
                     ckpt_ms_max=max([x[1] for x in lines] or [0]),
                     ckpt_ms_total=sum(x[1] for x in lines))
            self.configure(self.args.interval)
        self.res["stall"] = r
        log(f"{self.storage}: stall {r}")

    def wait_up(self):
        """Seconds from now until the index is loaded and holds every row."""
        t0 = time.time()
        while time.time() - t0 < 7200:
            r = idle.sh(["docker", "exec", self.name, "mysqladmin", "ping", "-uroot",
                         f"-p{idle.PW}", "--silent"], check=False)
            if r.returncode == 0:
                try:
                    if self.index_rows() == self.rows:
                        return time.time() - t0
                except Exception:
                    pass
            time.sleep(0.5)
        raise RuntimeError("index not caught up after restart")

    def m_restart(self):
        r = {}
        if self.storage == "innodb":
            self.wait_settled()
        else:
            self.file_checkpoint()
        idle.sh(["docker", "stop", "-t", "600", self.name], timeout=900)
        idle.sh(["docker", "start", self.name])
        r["clean_restart_s"] = round(self.wait_up(), 1)
        # Changes that no checkpoint holds: no timer checkpoint, no rotation.
        self.configure(3600)
        self.wait_up()
        n = self.tier["behind"]
        self.load_rows(self.vectors(n))
        self.wait_applied()
        idle.sh(["docker", "kill", "-s", "KILL", self.name])
        idle.sh(["docker", "start", self.name])
        r["behind_changes"] = n
        r["kill_restart_s"] = round(self.wait_up(), 1)
        self.configure(self.args.interval)
        self.wait_up()
        self.res["restart"] = r
        log(f"{self.storage}: restart {r}")

    def run(self):
        try:
            self.start()
            self.create()
            steps = self.args.measure.split(",")
            self.m_build()
            for s in ("load", "search", "ckpt", "stall", "restart"):
                if s in steps:
                    getattr(self, "m_" + s)()
        finally:
            if self.srv is not None and not self.args.keep:
                self.srv.remove()
                idle.sh(["docker", "run", "--rm", "-v", f"{self.args.workdir}:/w",
                         "alpine", "rm", "-rf", f"/w/{self.name}"], check=False)
        return self.res


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--component-dir", required=True)
    ap.add_argument("--image", default="mysql:8.4")
    ap.add_argument("--tier", choices=sorted(TIERS), default="smoke")
    ap.add_argument("--hdf5", help="ann-benchmarks HDF5 dataset (tier t2)")
    ap.add_argument("--storage", default="file,innodb")
    ap.add_argument("--measure", default="build,load,search,ckpt,stall,restart")
    ap.add_argument("--profile", choices=sorted(PROFILES), default="default")
    ap.add_argument("--threads", type=int, default=16, help="index build threads")
    ap.add_argument("--interval", type=int, default=15,
                    help="checkpoint interval in seconds, outside the stall step")
    ap.add_argument("--stall-interval", type=int, default=10)
    ap.add_argument("--stall-seconds", type=int, default=90)
    ap.add_argument("--stall-rate", type=int, default=500, help="rows inserted per second")
    ap.add_argument("--vec-chunk", type=int, default=0, help="vector chunk bytes (innodb)")
    ap.add_argument("--lnk-chunk", type=int, default=0, help="link chunk bytes (innodb)")
    ap.add_argument("--workdir", default="/data/bench-p3")
    ap.add_argument("--output", default="bench-innodb-storage.json")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    if args.tier == "t2" and not args.hdf5:
        ap.error("--tier t2 needs --hdf5")
    os.makedirs(args.workdir, exist_ok=True)
    result = {"tier": args.tier, "dataset": args.hdf5 or "synthetic uniform(-1, 1)",
              "settings": {k: getattr(args, k) for k in
                           ("threads", "interval", "stall_interval", "stall_seconds",
                            "stall_rate", "vec_chunk", "lnk_chunk", "profile")},
              "runs": []}
    for storage in args.storage.split(","):
        result["runs"].append(Bench(args, storage, TIERS[args.tier]).run())
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w") as f:
            json.dump(result, f, indent=1)
    log(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
