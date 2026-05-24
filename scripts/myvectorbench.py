#!/usr/bin/env python3
"""myvectorbench — benchmarking runner for MyVector.

Usage:
  ./scripts/myvectorbench.py \
      --mysql-version 8.4 --build-path component \
      --artifact-dir dist/component-8.4 \
      [--config myvectorbench.yml] [--output result.json]

  ./scripts/myvectorbench.py --promote <git-ref> [--config myvectorbench.yml]
"""

import argparse
import json
import math
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml


# ── config ────────────────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# ── Container ─────────────────────────────────────────────────────────────────

class Container:
    def __init__(self, mysql_version: str, root_pw: str = "benchroot"):
        self.version = mysql_version
        self.root_pw = root_pw
        self.name = f"myvector-bench-{os.getpid()}-{mysql_version.replace('.', '')}"
        self._running = False

    def start(self):
        subprocess.run(
            ["docker", "run", "-d", "--name", self.name,
             "-e", f"MYSQL_ROOT_PASSWORD={self.root_pw}",
             "-e", "MYSQL_ROOT_HOST=%",
             f"mysql:{self.version}"],
            check=True, capture_output=True,
        )
        self._running = True
        try:
            self._wait_ready()
        except Exception:
            self.stop()
            raise
        print(f"  MySQL {self.version} container ready ({self.name})")

    def _wait_ready(self):
        ready = 0
        for _ in range(60):
            try:
                r = subprocess.run(
                    ["docker", "exec", "-e", f"MYSQL_PWD={self.root_pw}",
                     self.name, "mysql", "-uroot", "-h127.0.0.1", "-e", "SELECT 1"],
                    capture_output=True, timeout=5,
                )
                ready = ready + 1 if r.returncode == 0 else 0
                if ready >= 3:
                    return
            except Exception:
                ready = 0
            time.sleep(2)
        raise RuntimeError(f"MySQL {self.version} container did not become ready")

    def stop(self):
        if self._running:
            subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)
            self._running = False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()

    def _base_cmd(self, db: str = "", interactive: bool = False) -> list:
        cmd = ["docker", "exec"]
        if interactive:
            cmd.append("-i")
        cmd += ["-e", f"MYSQL_PWD={self.root_pw}", self.name,
                "mysql", "-uroot", "-h127.0.0.1", "--batch", "--silent"]
        if db:
            cmd += ["-D", db]
        return cmd

    def sql(self, sql: str, db: str = "") -> str:
        """Execute SQL, return stdout. Raises RuntimeError on nonzero exit."""
        r = subprocess.run(self._base_cmd(db) + ["-e", sql], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"SQL failed (rc={r.returncode}): {r.stderr.strip()}")
        return r.stdout

    def sql_stdin(self, sql: str, db: str = ""):
        """Execute multi-statement SQL from stdin (handles DELIMITER)."""
        r = subprocess.run(self._base_cmd(db, interactive=True),
                           input=sql.encode(), capture_output=True)
        if r.returncode != 0:
            raise RuntimeError(f"SQL (stdin) failed (rc={r.returncode}): {r.stderr.decode().strip()}")

    def scalar(self, sql: str, db: str = "") -> str:
        """Return last non-empty line of sql() output."""
        out = self.sql(sql, db).strip()
        return out.splitlines()[-1].strip() if out else ""

    def cp(self, host_src: str, container_dst: str):
        subprocess.run(["docker", "cp", host_src, f"{self.name}:{container_dst}"], check=True)

    def exec(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(["docker", "exec", self.name] + list(args),
                               capture_output=True, text=True)

    def plugin_dir(self) -> str:
        return self.scalar("SELECT @@plugin_dir;")

    def data_dir(self) -> str:
        return self.scalar("SELECT @@datadir;")


# ── stored procedures SQL (same as pre-release-test.sh install_procs) ─────────

INSTALL_PROCS_SQL = """\
DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_INTERNAL;
DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_STATUS;
DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_DROP;
DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_BUILD;

DELIMITER //

CREATE PROCEDURE MYVECTOR_INDEX_STATUS(IN myvectorcolumn VARCHAR(256))
BEGIN
  DECLARE extra VARCHAR(1024); DECLARE pkid VARCHAR(1024);
  SET extra = ''; SET pkid = '';
  CALL MYVECTOR_INDEX_INTERNAL(myvectorcolumn, pkid, 'status', extra);
END //

CREATE PROCEDURE MYVECTOR_INDEX_DROP(IN myvectorcolumn VARCHAR(256))
BEGIN
  DECLARE extra VARCHAR(1024); DECLARE pkid VARCHAR(1024);
  SET extra = ''; SET pkid = '';
  CALL MYVECTOR_INDEX_INTERNAL(myvectorcolumn, pkid, 'drop', extra);
END //

CREATE PROCEDURE MYVECTOR_INDEX_INTERNAL(
    IN myvectorcolumn VARCHAR(256), IN pkidcolumn VARCHAR(64),
    IN action VARCHAR(64), IN extra VARCHAR(1024))
BEGIN
  DECLARE pos    INT; DECLARE status VARCHAR(1024);
  DECLARE temp   VARCHAR(256); DECLARE dbname VARCHAR(64);
  DECLARE tname  VARCHAR(64); DECLARE cname  VARCHAR(64);
  DECLARE colinfo VARCHAR(1024);
  DECLARE CONTINUE HANDLER FOR NOT FOUND SET colinfo = NULL;
  SET pos    = LOCATE('.', myvectorcolumn);
  SET dbname = SUBSTR(myvectorcolumn, 1, pos-1);
  SET temp   = SUBSTR(myvectorcolumn, pos+1);
  SET pos    = LOCATE('.', temp);
  SET tname  = SUBSTR(temp, 1, pos-1);
  SET cname  = SUBSTR(temp, pos+1);
  SELECT column_comment INTO colinfo
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE table_schema = dbname AND table_name = tname AND column_name = cname;
  IF colinfo IS NULL THEN
    SIGNAL SQLSTATE '50001' SET MESSAGE_TEXT = 'Vector column not found.';
  END IF;
  IF LOCATE('MYVECTOR COLUMN', colinfo) <> 1 THEN
    SIGNAL SQLSTATE '50002' SET MESSAGE_TEXT = 'Column is not a MYVECTOR column.';
  END IF;
  SET status = MYVECTOR_SEARCH_OPEN_UDF(myvectorcolumn, colinfo, pkidcolumn, action, extra);
  SELECT status AS Status;
END //

CREATE PROCEDURE MYVECTOR_INDEX_BUILD(
    IN myvectorcolumn VARCHAR(256), IN pkidcolumn VARCHAR(64))
BEGIN
  DECLARE extra VARCHAR(1024); SET extra = '';
  CALL MYVECTOR_INDEX_INTERNAL(myvectorcolumn, pkidcolumn, 'build', extra);
END //

DELIMITER ;
"""


# ── install helpers ───────────────────────────────────────────────────────────

def _ensure_libmysqlclient(container: Container):
    r = container.exec("sh", "-c", "ldconfig -p 2>/dev/null | grep -q libmysqlclient")
    if r.returncode == 0:
        return
    srv_ver = container.scalar("SELECT @@version;")
    arch = container.exec("uname", "-m").stdout.strip()
    base = f"https://cdn.mysql.com/Downloads/MySQL-{'.'.join(srv_ver.split('.')[:2])}"
    ver_rpm = f"{srv_ver}-1.el9"
    r = container.exec("bash", "-c", (
        f"rpm -ivh --nodeps '{base}/mysql-community-common-{ver_rpm}.{arch}.rpm' 2>/dev/null || true"
        f" && rpm -ivh --nodeps '{base}/mysql-community-client-plugins-{ver_rpm}.{arch}.rpm' 2>/dev/null || true"
        f" && rpm -ivh --nodeps '{base}/mysql-community-libs-{ver_rpm}.{arch}.rpm' 2>/dev/null"
        f" && ldconfig 2>/dev/null || true"
    ))
    if r.returncode != 0:
        print("  WARNING: libmysqlclient CDN install failed", file=sys.stderr)


def install_component(container: Container, comp_dir: str):
    """Install MyVector component build into the container."""
    _ensure_libmysqlclient(container)
    plugin_dir = container.plugin_dir()
    data_dir = container.data_dir()

    container.cp(f"{comp_dir}/libmyvector_component.so", f"{plugin_dir}/myvector.so")
    container.cp(f"{comp_dir}/myvector.json", f"{plugin_dir}/myvector.json")
    container.sql("INSTALL COMPONENT 'file://myvector';")

    owner = container.exec("stat", "-c", "%U", data_dir).stdout.strip() or "mysql"
    cnf = (
        f"myvector_host=127.0.0.1\n"
        f"myvector_user_id=root\n"
        f"myvector_user_password={container.root_pw}\n"
        f"myvector_port=3306\n"
    )
    with tempfile.NamedTemporaryFile(mode='w', suffix='.cnf', delete=False) as tmp:
        tmp.write(cnf)
        tmp_path = tmp.name
    try:
        container.cp(tmp_path, f"{data_dir}myvector.cnf")
        container.cp(tmp_path, "/myvector.cnf")
    finally:
        os.unlink(tmp_path)
    container.exec("bash", "-c",
        f"chmod 0600 '{data_dir}myvector.cnf' && chown '{owner}' '{data_dir}myvector.cnf'"
        f" && chmod 0600 /myvector.cnf && chown '{owner}' /myvector.cnf"
    )
    try:
        container.sql(f"SET GLOBAL myvector_index_dir='{data_dir}';")
    except RuntimeError:
        pass  # sysvar not available on all versions
    container.sql(
        "DROP FUNCTION IF EXISTS myvector_row_distance;"
        " DROP FUNCTION IF EXISTS myvector_is_valid;"
        " DROP FUNCTION IF EXISTS myvector_search_open_udf;"
        " CREATE FUNCTION myvector_row_distance    RETURNS REAL    SONAME 'myvector.so';"
        " CREATE FUNCTION myvector_is_valid        RETURNS INTEGER SONAME 'myvector.so';"
        " CREATE FUNCTION myvector_search_open_udf RETURNS STRING  SONAME 'myvector.so';",
        "mysql",
    )
    container.sql_stdin(INSTALL_PROCS_SQL, "mysql")
    print("  Component installed.")


def install_plugin(container: Container, plugin_so: str):
    """Install MyVector plugin build into the container."""
    plugin_dir = container.plugin_dir()
    data_dir = container.data_dir()
    container.cp(plugin_so, f"{plugin_dir}/myvector.so")
    container.sql("INSTALL PLUGIN myvector SONAME 'myvector.so';", "mysql")
    container.sql(
        "DROP FUNCTION IF EXISTS myvector_row_distance;"
        " DROP FUNCTION IF EXISTS myvector_is_valid;"
        " DROP FUNCTION IF EXISTS myvector_search_open_udf;"
        " CREATE FUNCTION myvector_row_distance    RETURNS REAL    SONAME 'myvector.so';"
        " CREATE FUNCTION myvector_is_valid        RETURNS INTEGER SONAME 'myvector.so';"
        " CREATE FUNCTION myvector_search_open_udf RETURNS STRING  SONAME 'myvector.so';",
        "mysql",
    )
    try:
        container.sql(f"SET GLOBAL myvector_index_dir='{data_dir}';")
    except RuntimeError:
        pass  # sysvar not available on all versions
    container.sql_stdin(INSTALL_PROCS_SQL, "mysql")
    print("  Plugin installed.")


# ── dataset helpers ───────────────────────────────────────────────────────────

def _synthetic_vectors(rows: int, dim: int, seed: int = 42) -> list:
    rng = random.Random(seed)
    return [[rng.gauss(0, 1) for _ in range(dim)] for _ in range(rows)]


def load_dataset(dataset: str, wp: dict) -> list:
    """Return a list of float lists (rows × dim).

    dataset values:
      'synthetic'          — deterministic Gaussian vectors (seed=42)
      'glove50'            — GloVe 6B 50d (downloaded to ~/.cache/myvectorbench/)
      'glove300'           — GloVe 6B 300d
      '/path/to/file.tsv'  — custom space/tab-separated file
    """
    rows = wp.get("rows", 10000)
    dim = wp.get("dim", 128)

    if dataset == "synthetic":
        return _synthetic_vectors(rows, dim)

    if dataset in ("glove50", "glove300"):
        dim_map = {"glove50": 50, "glove300": 300}
        actual_dim = dim_map[dataset]
        if dim != actual_dim:
            print(
                f"  WARNING: config dim={dim} overridden to {actual_dim} for {dataset}",
                file=sys.stderr,
            )
            wp['dim'] = actual_dim
        return _load_glove(dataset, rows, actual_dim)

    return _load_tsv(dataset, rows, dim)


def _load_glove(dataset: str, rows: int, dim: int) -> list:
    import urllib.request
    import zipfile

    filenames = {"glove50": "glove.6B.50d.txt", "glove300": "glove.6B.300d.txt"}
    glove_url = "https://nlp.stanford.edu/data/glove.6B.zip"
    cache_dir = Path(
        os.environ.get("MYVECTOR_DATASET_CACHE", os.path.expanduser("~/.cache/myvectorbench"))
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    txt_path = cache_dir / filenames[dataset]

    if not txt_path.exists():
        zip_path = cache_dir / "glove.6B.zip"
        if not zip_path.exists():
            print(f"  Downloading GloVe 6B from {glove_url} (~860 MB) ...")
            zip_tmp = zip_path.with_suffix(".zip.tmp")
            try:
                urllib.request.urlretrieve(glove_url, zip_tmp)
                zip_tmp.rename(zip_path)
            except Exception:
                zip_tmp.unlink(missing_ok=True)
                raise
        print(f"  Extracting {filenames[dataset]} ...")
        with zipfile.ZipFile(zip_path) as z:
            z.extract(filenames[dataset], cache_dir)

    return _load_tsv(str(txt_path), rows, dim)


def _load_tsv(path: str, rows: int, dim: int) -> list:
    """Load up to `rows` vectors from a whitespace-separated file.

    Each line is either '<word> <f1> <f2> ...' or '<f1> <f2> ...' — the
    first non-numeric field is treated as a word token and skipped.
    """
    vectors = []
    with open(path) as f:
        for line in f:
            if len(vectors) >= rows:
                break
            parts = line.strip().split()
            if not parts:
                continue
            start = 0
            try:
                float(parts[0])
            except ValueError:
                start = 1
            try:
                v = [float(x) for x in parts[start:start + dim]]
            except ValueError:
                continue
            if len(v) == dim and all(math.isfinite(x) for x in v):
                vectors.append(v)
    return vectors


# ── workloads (implemented in Task 3) ────────────────────────────────────────

def _vec_literal(v: list) -> str:
    """Format a float list as a myvector_construct('[...]') SQL literal."""
    if not all(math.isfinite(x) for x in v):
        raise ValueError(f"Vector contains non-finite value: {v[:8]}")
    inner = ",".join(f"{x:.6f}" for x in v)
    return f"myvector_construct('[{inner}]')"


def _create_bench_table(container: Container, dim: int, rows: int, M: int, ef: int,
                         db: str, table: str, online: bool = False,
                         dist: str = "L2") -> None:
    online_flag = ",online=Y" if online else ""
    container.sql(f"CREATE DATABASE IF NOT EXISTS {db};")
    container.sql(f"DROP TABLE IF EXISTS {db}.{table};")
    container.sql(
        f"CREATE TABLE {db}.{table} ("
        f"  id  INT PRIMARY KEY,"
        f"  vec VARBINARY({dim * 4 + 8})"
        f"    COMMENT 'MYVECTOR COLUMN type=hnsw,dim={dim},size={rows},m={M},ef={ef},"
        f"idcol=id,dist={dist}{online_flag}'"
        f");"
    )


def bench_index_build(container: Container, vectors: list, wp: dict) -> float:
    """INSERT all rows, then call MYVECTOR_INDEX_BUILD; return wall-clock seconds."""
    dim = wp['dim']
    M = wp.get('M', 16)
    ef = wp.get('ef_construction', 200)
    rows = len(vectors)
    print(f"  [index_build] {rows} rows, dim={dim}")

    _create_bench_table(container, dim, rows, M, ef, "bench", "build_t")

    batch = 500
    for start in range(0, rows, batch):
        chunk = vectors[start:start + batch]
        vals = ", ".join(f"({start + i}, {_vec_literal(v)})" for i, v in enumerate(chunk))
        container.sql_stdin(f"INSERT INTO bench.build_t (id, vec) VALUES {vals};")

    t0 = time.time()
    container.sql("CALL mysql.MYVECTOR_INDEX_BUILD('bench.build_t.vec', 'id');")
    elapsed = time.time() - t0
    print(f"    index_build_time_s = {elapsed:.2f}")
    return elapsed


def bench_insert_throughput(container: Container, vectors: list, wp: dict) -> float:
    """INSERT rows into an online=Y indexed table; return QPS."""
    dim = wp['dim']
    M = wp.get('M', 16)
    ef = wp.get('ef_construction', 200)
    rows = len(vectors)
    print(f"  [insert_throughput] {rows} rows, dim={dim}, online=Y")

    _create_bench_table(container, dim, rows, M, ef, "bench", "insert_t", online=True)

    t0 = time.time()
    batch = 500
    for start in range(0, rows, batch):
        chunk = vectors[start:start + batch]
        vals = ", ".join(f"({start + i}, {_vec_literal(v)})" for i, v in enumerate(chunk))
        container.sql_stdin(f"INSERT INTO bench.insert_t (id, vec) VALUES {vals};")
    elapsed = time.time() - t0

    qps = rows / elapsed if elapsed > 0 else 0.0
    print(f"    insert_qps = {qps:.0f}")
    return qps


def bench_knn_search(container: Container, vectors: list, wp: dict) -> dict:
    """Run knn_queries ORDER-BY-distance queries against build_t; return timing metrics."""
    n_queries = wp.get('knn_queries', 200)
    print(f"  [knn_search] {n_queries} queries, dim={wp['dim']}")

    rng = random.Random(99)
    query_vectors = [vectors[rng.randint(0, len(vectors) - 1)] for _ in range(n_queries)]

    latencies_ms = []
    for q in query_vectors:
        sql = (
            f"SELECT id, myvector_distance(vec, {_vec_literal(q)}, 'L2') AS dist"
            f" FROM bench.build_t ORDER BY dist LIMIT 10;"
        )
        t0 = time.time()
        container.sql(sql)
        latencies_ms.append((time.time() - t0) * 1000)

    latencies_ms.sort()
    p50 = statistics.median(latencies_ms)
    p99 = latencies_ms[max(0, math.ceil(len(latencies_ms) * 0.99) - 1)]
    qps = n_queries / (sum(latencies_ms) / 1000) if latencies_ms else 0.0
    print(f"    knn_qps={qps:.0f}  p50={p50:.1f}ms  p99={p99:.1f}ms")
    return {"knn_qps": qps, "knn_p50_ms": p50, "knn_p99_ms": p99}


def run_workloads(container: Container, vectors: list, wp: dict,
                  build_path: str, mysql_version: str) -> dict:
    metrics = {}
    metrics["index_build_time_s"] = bench_index_build(container, vectors, wp)
    metrics["insert_qps"] = bench_insert_throughput(container, vectors, wp)
    metrics.update(bench_knn_search(container, vectors, wp))
    metrics["recall_at_10"] = None  # requires ANN query API not available in v1
    return metrics


# ── promote (implemented in Task 4) ──────────────────────────────────────────

def promote(git_ref: str, config_path: str):
    """Copy git_ref result JSONs to baseline.json on the benchmarks/ branch.

    Uses a temporary git worktree; requires the benchmarks/ branch to exist
    (created by the first CI run) or origin/benchmarks to be fetchable.
    """
    r0 = subprocess.run(["git", "rev-parse", "--git-dir"], capture_output=True, text=True)
    if r0.returncode != 0:
        print("ERROR: not inside a git repository.", file=sys.stderr)
        sys.exit(1)

    config = load_config(config_path)
    matrix = config.get("matrix", {})
    mysql_versions = matrix.get("mysql_versions", [])
    build_paths = matrix.get("build_paths", [])

    with tempfile.TemporaryDirectory() as wt_dir:
        r = subprocess.run(
            ["git", "worktree", "add", wt_dir, "benchmarks"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            r2 = subprocess.run(
                ["git", "worktree", "add", "--track", "-b", "benchmarks",
                 wt_dir, "origin/benchmarks"],
                capture_output=True, text=True,
            )
            if r2.returncode != 0:
                print(
                    "ERROR: benchmarks/ branch does not exist locally or on origin.\n"
                    "It is created automatically by the first CI run.\n"
                    "Run the myvectorbench workflow at least once before promoting.",
                    file=sys.stderr,
                )
                sys.exit(1)

        promoted = 0
        for ver in mysql_versions:
            for bp in build_paths:
                cell_dir = Path(wt_dir) / str(ver) / bp
                if not cell_dir.exists():
                    print(f"  SKIP {ver}/{bp}: no results directory on benchmarks/ branch")
                    continue
                candidates = sorted(cell_dir.glob(f"{git_ref}-*.json"))
                if not candidates:
                    print(f"  SKIP {ver}/{bp}: no result file for {git_ref}")
                    continue
                src = candidates[-1]
                dst = cell_dir / "baseline.json"
                shutil.copy(src, dst)
                print(f"  PROMOTED {ver}/{bp}: {src.name} → baseline.json")
                promoted += 1

        if promoted == 0:
            print(f"  No results found for {git_ref} on benchmarks/ branch — nothing promoted.")
            subprocess.run(["git", "worktree", "remove", "--force", wt_dir], capture_output=True)
            return

        try:
            subprocess.run(["git", "-C", wt_dir, "add", "-A"], check=True)
            subprocess.run(
                ["git", "-C", wt_dir, "commit", "-m", f"promote: set baseline to {git_ref}"],
                check=True,
            )
            subprocess.run(["git", "worktree", "remove", wt_dir], check=True)
        except Exception:
            subprocess.run(["git", "worktree", "remove", "--force", wt_dir], capture_output=True)
            raise
    print(f"  Done: promoted {promoted} cell(s) to baseline for {git_ref}.")


# ── git helper ────────────────────────────────────────────────────────────────

def _git_ref() -> str:
    try:
        r = subprocess.run(
            ["git", "describe", "--tags", "--always"],
            capture_output=True, text=True, check=True,
        )
        return r.stdout.strip()
    except Exception:
        return "unknown"


# ── main benchmark orchestration ─────────────────────────────────────────────

def run_benchmark(mysql_version: str, build_path: str, artifact_dir: str,
                  config: dict, output: str):
    wp = config.get('workload', {})
    git_ref = _git_ref()
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    runner = os.environ.get("RUNNER_NAME", "local")
    dataset = wp.get("dataset", "synthetic")

    print(f"=== myvectorbench: mysql:{mysql_version} {build_path} @ {git_ref} ===")

    with Container(mysql_version) as c:
        if build_path == "component":
            install_component(c, artifact_dir)
        else:
            install_plugin(c, os.path.join(artifact_dir, "myvector.so"))

        vectors = load_dataset(dataset, wp)
        metrics = run_workloads(c, vectors, wp, build_path, mysql_version)

    result = {
        "git_ref": git_ref,
        "mysql_version": mysql_version,
        "build_path": build_path,
        "timestamp": timestamp,
        "runner": runner,
        "dataset": dataset,
        "workload_params": {
            "rows": wp.get("rows", 10000),
            "dim": wp.get("dim", 128),
            "M": wp.get("M", 16),
            "ef_construction": wp.get("ef_construction", 200),
            "knn_queries": wp.get("knn_queries", 200),
        },
        "metrics": metrics,
    }

    with open(output, "w") as f:
        json.dump(result, f, indent=2)
    print(f"  Result written to {output}")
    print(f"  Metrics: {json.dumps(metrics, indent=2)}")


def main():
    parser = argparse.ArgumentParser(description="myvectorbench runner")
    parser.add_argument("--mysql-version", help="MySQL version, e.g. 8.4")
    parser.add_argument("--build-path", choices=["component", "plugin"])
    parser.add_argument("--artifact-dir", help="Dir containing build artifacts")
    parser.add_argument("--config", default="myvectorbench.yml")
    parser.add_argument("--output", default="result.json")
    parser.add_argument("--promote", metavar="GIT_REF",
                        help="Promote GIT_REF results to baseline on benchmarks/ branch")
    args = parser.parse_args()

    if args.promote:
        promote(args.promote, args.config)
        return

    if not all([args.mysql_version, args.build_path, args.artifact_dir]):
        parser.error("--mysql-version, --build-path, and --artifact-dir are required")

    config = load_config(args.config)
    run_benchmark(args.mysql_version, args.build_path, args.artifact_dir, config, args.output)


if __name__ == "__main__":
    main()
