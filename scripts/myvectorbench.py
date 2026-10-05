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
import platform
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# ── config ────────────────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    import yaml
    with open(path) as f:
        return yaml.safe_load(f)


# ── Container ─────────────────────────────────────────────────────────────────

class Container:
    def __init__(self, mysql_version: str, root_pw: str = "benchroot",
                 extra_volumes: list = None, image: str = None):
        self.version = mysql_version
        self.root_pw = root_pw
        self.name = f"myvector-bench-{os.getpid()}-{mysql_version.replace('.', '')}"
        self._running = False
        self._extra_volumes = extra_volumes or []
        self._image = image or f"mysql:{mysql_version}"

    def start(self):
        cmd = ["docker", "run", "-d", "--name", self.name,
               "-e", f"MYSQL_ROOT_PASSWORD={self.root_pw}",
               "-e", "MYSQL_ROOT_HOST=%"]
        for vol in self._extra_volumes:
            cmd += ["-v", vol]
        cmd.append(self._image)
        subprocess.run(cmd, check=True, capture_output=True)
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
            subprocess.run(["docker", "rm", "-fv", self.name], capture_output=True)
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

    def sql_batch_timed(self, queries: list, db: str = "") -> list:
        """Run many one-shot SQL statements over a single persistent mysql
        client session (one docker exec, not one per query) and return each
        statement's server-observed latency in milliseconds.

        docker exec + a fresh mysql client process is ~67ms of pure overhead
        per call on a typical dev host (myvector#124) -- dwarfing the actual
        query time and making QPS/latency benchmarks measure process-spawn
        cost, not MyVector. Bracketing each query with `SELECT NOW(6)`
        markers and measuring the gap between them gives the query's real
        round-trip time over an already-open connection, with none of that
        per-query overhead, while still preserving per-query granularity
        (so p50/p99 remain meaningful, not just a batch average).
        """
        script_lines = []
        for q in queries:
            q = q.rstrip()
            if not q.endswith(";"):
                q += ";"
            script_lines.append("SELECT NOW(6);")
            script_lines.append(q)
            script_lines.append("SELECT NOW(6);")
        script = "\n".join(script_lines) + "\n"

        r = subprocess.run(self._base_cmd(db, interactive=True),
                           input=script.encode(), capture_output=True)
        if r.returncode != 0:
            raise RuntimeError(f"SQL batch failed (rc={r.returncode}): {r.stderr.decode().strip()}")

        ts_re = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+$")
        markers = []
        for line in r.stdout.decode().splitlines():
            line = line.strip()
            if ts_re.match(line):
                markers.append(datetime.strptime(line, "%Y-%m-%d %H:%M:%S.%f"))

        expected = 2 * len(queries)
        if len(markers) != expected:
            raise RuntimeError(
                f"SQL batch timing markers mismatch: expected {expected}, found "
                f"{len(markers)} -- a query's own output may resemble a timestamp, "
                f"or the batch didn't run to completion"
            )

        return [
            (markers[2 * i + 1] - markers[2 * i]).total_seconds() * 1000
            for i in range(len(queries))
        ]

    def sql_batch_results(self, queries: list, db: str = "") -> list:
        """Run many one-shot SQL statements over a single persistent mysql
        client session (one docker exec, not one per query) and return each
        statement's own output as a separate list of lines.

        Same per-query-exec-overhead motivation as sql_batch_timed(), but for
        callers that need the actual result set (e.g. computing recall)
        rather than just timing. A sentinel SELECT after each query marks
        where one result block ends and the next begins.
        """
        sentinel = "___MYVECTORBENCH_QEND___"
        script_lines = []
        for q in queries:
            q = q.rstrip()
            if not q.endswith(";"):
                q += ";"
            script_lines.append(q)
            script_lines.append(f"SELECT '{sentinel}';")
        script = "\n".join(script_lines) + "\n"

        r = subprocess.run(self._base_cmd(db, interactive=True),
                           input=script.encode(), capture_output=True)
        if r.returncode != 0:
            raise RuntimeError(f"SQL batch failed (rc={r.returncode}): {r.stderr.decode().strip()}")

        blocks = []
        current: list = []
        for line in r.stdout.decode().splitlines():
            if line.strip() == sentinel:
                blocks.append(current)
                current = []
            else:
                current.append(line)

        if len(blocks) != len(queries):
            raise RuntimeError(
                f"SQL batch result mismatch: expected {len(queries)} blocks, found "
                f"{len(blocks)} -- a query's own output may have collided with the "
                f"sentinel, or the batch didn't run to completion"
            )
        return blocks

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

    # ── server interface shared with HostServer ──────────────────────────────
    mode = "docker"
    connection = "docker-cli"
    myvector_port = 3306  # the server's port as seen from inside the container

    def execute(self, sql: str, db: str = ""):
        """Run one (possibly large) statement whose result is not needed."""
        self.sql_stdin(sql, db)

    def install_plugin_file(self, src: str, name: str):
        self.cp(src, f"{self.plugin_dir()}/{name}")

    def ensure_client_libs(self):
        _ensure_libmysqlclient(self)

    def write_myvector_cnf(self, text: str):
        data_dir = self.data_dir()
        owner = self.exec("stat", "-c", "%U", data_dir).stdout.strip() or "mysql"
        with tempfile.NamedTemporaryFile(mode='w', suffix='.cnf', delete=False) as tmp:
            tmp.write(text)
            tmp_path = tmp.name
        try:
            self.cp(tmp_path, f"{data_dir}myvector.cnf")
            self.cp(tmp_path, "/myvector.cnf")
        finally:
            os.unlink(tmp_path)
        self.exec("bash", "-c",
            f"chmod 0600 '{data_dir}myvector.cnf' && chown '{owner}' '{data_dir}myvector.cnf'"
            f" && chmod 0600 /myvector.cnf && chown '{owner}' /myvector.cnf"
        )


# ── HostServer: a real mysqld on the host (myvector#133) ──────────────────────

# Patch release benchmarked for each series: the same tags the build scripts use,
# so the MyVector build and the server it loads into come from one version.
MYSQL_FULL_VERSIONS = {"8.4": "8.4.8", "9.7": "9.7.0", "26.7": "26.7.0"}

_TARBALL_ARCH = {"aarch64": "aarch64", "arm64": "aarch64",
                 "x86_64": "x86_64", "amd64": "x86_64"}


def mysql_tarball_name(full_version: str, machine: str = None) -> str:
    """Basename (no extension) of Oracle's generic Linux tarball for a version."""
    machine = machine or platform.machine()
    arch = _TARBALL_ARCH.get(machine.lower())
    if not arch:
        raise ValueError(f"no MySQL generic tarball for architecture {machine!r}")
    return f"mysql-{full_version}-linux-glibc2.28-{arch}"


def mysql_tarball_urls(full_version: str, machine: str = None) -> list:
    """Download URLs to try in order: current releases first, then the archive
    (older patch releases, e.g. 8.4.8 x86_64, only live under archives/)."""
    series = ".".join(full_version.split(".")[:2])
    f = mysql_tarball_name(full_version, machine) + ".tar.xz"
    return [f"https://cdn.mysql.com/Downloads/MySQL-{series}/{f}",
            f"https://cdn.mysql.com/archives/mysql-{series}/{f}"]


def default_mysql_cache_dir() -> str:
    return os.environ.get("MYVECTORBENCH_MYSQL_CACHE") or str(
        Path.home() / ".cache" / "myvectorbench" / "mysql")


def resolve_mysql_basedir(full_version: str, cache_dir: str) -> str:
    """Return an extracted MySQL basedir for full_version under cache_dir,
    downloading and extracting the official tarball the first time."""
    import urllib.request
    cache = Path(cache_dir)
    name = mysql_tarball_name(full_version)
    basedir = cache / name
    if (basedir / "bin" / "mysqld").exists():
        return str(basedir)
    cache.mkdir(parents=True, exist_ok=True)
    tarball = cache / f"{name}.tar.xz"
    if not tarball.exists():
        # Unique staging names, so concurrent first runs sharing a cache never
        # write to or delete each other's partial files.
        fd, part_name = tempfile.mkstemp(prefix=f"{name}.tar.xz.part-", dir=cache)
        os.close(fd)
        part = Path(part_name)
        errors = []
        try:
            for url in mysql_tarball_urls(full_version):
                print(f"  Downloading {url}")
                try:
                    with urllib.request.urlopen(url) as r, open(part, "wb") as f:
                        shutil.copyfileobj(r, f, 1 << 20)
                    part.replace(tarball)
                    break
                except Exception as e:  # 404 on the first URL is expected for old releases
                    errors.append(f"{url}: {e}")
            else:
                raise RuntimeError("MySQL tarball download failed:\n  " + "\n  ".join(errors))
        finally:
            part.unlink(missing_ok=True)
    print(f"  Extracting {tarball.name} into {cache}")
    partial = Path(tempfile.mkdtemp(prefix=f"{name}.partial-", dir=cache))
    try:
        subprocess.run(["tar", "-xJf", str(tarball), "-C", str(partial),
                        "--strip-components=1"], check=True)
        partial.rename(basedir)
    except OSError:
        # Another run extracted it first: use theirs.
        if not (basedir / "bin" / "mysqld").exists():
            raise
    finally:
        shutil.rmtree(partial, ignore_errors=True)
    return str(basedir)


def _free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _system_lib_path(soname: str) -> str:
    """Path of a shared library known to the dynamic linker, or ''."""
    r = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        parts = line.strip().split(" => ")
        if len(parts) == 2 and parts[0].split(" ")[0] == soname:
            return parts[1].strip()
    return ""


def sql_string_body(text: str) -> str:
    """Escape text for a single-quoted SQL literal under the default sql_mode,
    where backslash is an escape character (so it must be doubled first)."""
    return text.replace("\\", "\\\\").replace("'", "''")


def _format_cell(v) -> str:
    """Render one value the way `mysql --batch --silent` does."""
    if v is None:
        return "NULL"
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", errors="replace")
    return str(v)


class HostServer:
    """An isolated mysqld from an extracted official tarball, run on the host.

    Same interface as Container. Setup SQL (DELIMITER scripts, CALLs) goes
    through the tarball's own mysql client over the socket; everything that is
    timed, or whose rows are read, goes over one persistent mysql.connector
    connection, so no process is spawned per query and the measured latency
    is the client-observed round trip.
    """

    mode = "host"
    connection = "connector"

    def __init__(self, mysql_version: str, basedir: str, workdir_root: str,
                 root_pw: str = "benchroot", keep_workdir: bool = False):
        self.version = mysql_version
        self.root_pw = root_pw
        self.basedir = Path(basedir)
        self.name = f"myvector-bench-{os.getpid()}-{mysql_version.replace('.', '')}"
        self.workdir = Path(workdir_root) / self.name
        self.datadir = self.workdir / "data"
        self.plugin_path = self.workdir / "plugin"
        self.port = self.myvector_port = _free_port()
        self.socket = str(self.workdir / "mysql.sock")
        if len(self.socket) > 100:  # sun_path limit is 108 bytes
            raise ValueError(f"socket path too long for a unix socket: {self.socket}; "
                             "use a shorter --workdir-root")
        self.keep_workdir = keep_workdir
        self._proc = None
        self._conn = None
        self._db = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    def _env(self) -> dict:
        env = dict(os.environ)
        env["MYSQL_PWD"] = self.root_pw
        libdir = str(self.workdir / "lib")
        env["LD_LIBRARY_PATH"] = libdir + (
            ":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
        return env

    def _warn_if_boot_disk(self):
        root = self.workdir.parent
        if os.stat(root).st_dev == os.stat("/").st_dev:
            free_gb = shutil.disk_usage(root).free / 2**30
            if free_gb < 20:
                print(f"  WARNING: workdir {root} is on the root filesystem with only "
                      f"{free_gb:.0f} GB free; pass --workdir-root on a data volume")

    def _prepare_workdir(self):
        self.workdir.parent.mkdir(parents=True, exist_ok=True)
        self._warn_if_boot_disk()
        shutil.rmtree(self.workdir, ignore_errors=True)
        for d in ("plugin", "tmp", "lib"):
            (self.workdir / d).mkdir(parents=True)
        # Own plugin dir of symlinks, so the MyVector .so never lands in the
        # cached (shared) tarball.
        for entry in (self.basedir / "lib" / "plugin").iterdir():
            (self.plugin_path / entry.name).symlink_to(entry)
        # Ubuntu 24.04 renamed libaio.so.1 to libaio.so.1t64; mysqld needs the old name.
        if not _system_lib_path("libaio.so.1"):
            t64 = _system_lib_path("libaio.so.1t64")
            if t64:
                (self.workdir / "lib" / "libaio.so.1").symlink_to(t64)

    def _mysqld_args(self) -> list:
        w = self.workdir
        return [str(self.basedir / "bin" / "mysqld"), "--no-defaults",
                f"--basedir={self.basedir}", f"--datadir={self.datadir}",
                f"--plugin-dir={self.plugin_path}", f"--tmpdir={w / 'tmp'}",
                f"--log-error={w / 'error.log'}"]

    def _error_log_tail(self, n: int = 30) -> str:
        try:
            return "\n".join((self.workdir / "error.log").read_text(
                errors="replace").splitlines()[-n:])
        except OSError:
            return "(no error log)"

    def start(self):
        self._prepare_workdir()
        print(f"  MySQL {self.version} host server: basedir={self.basedir}")
        print(f"    workdir={self.workdir} port={self.port}")
        r = subprocess.run(self._mysqld_args() + ["--initialize-insecure"],
                           env=self._env(), capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"mysqld --initialize-insecure failed (rc={r.returncode}):\n"
                               f"{r.stderr.strip()}\n{self._error_log_tail()}")
        # myvector.cnf goes in before anything is installed: the binlog listener
        # reads it when it starts and never connects if it only appears later.
        self.write_myvector_cnf(myvector_cnf(self.root_pw, self.port))
        init_file = self.workdir / "init.sql"
        pw = sql_string_body(self.root_pw)
        # root@% like the Docker image's MYSQL_ROOT_HOST=%; the server only
        # listens on 127.0.0.1, so this is not reachable from outside.
        init_file.write_text(
            f"ALTER USER 'root'@'localhost' IDENTIFIED BY '{pw}';\n"
            f"CREATE USER 'root'@'%' IDENTIFIED BY '{pw}';\n"
            f"GRANT ALL ON *.* TO 'root'@'%' WITH GRANT OPTION;\n")
        args = self._mysqld_args() + [
            f"--port={self.port}", "--bind-address=127.0.0.1",
            f"--socket={self.socket}", "--mysqlx=OFF", "--server-id=1",
            f"--pid-file={self.workdir / 'mysqld.pid'}", f"--init-file={init_file}",
        ]
        self._proc = subprocess.Popen(args, env=self._env(), stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
        try:
            self._wait_ready()
        except Exception:
            self.stop()
            raise
        print(f"  MySQL {self.version} host server ready (pid {self._proc.pid})")

    def _connect(self):
        import mysql.connector
        return mysql.connector.connect(unix_socket=self.socket, user="root",
                                       password=self.root_pw, autocommit=True)

    def _wait_ready(self):
        deadline = time.time() + 180
        last = None
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(f"mysqld exited with rc={self._proc.returncode}:\n"
                                   f"{self._error_log_tail()}")
            try:
                self._conn = self._connect()
                return
            except Exception as e:
                last = e
                time.sleep(0.5)
        raise RuntimeError(f"MySQL {self.version} host server did not become ready: {last}\n"
                           f"{self._error_log_tail()}")

    def stop(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        if self._proc is not None and self._proc.poll() is None:
            subprocess.run([str(self.basedir / "bin" / "mysqladmin"), "--no-defaults",
                            "-uroot", f"--socket={self.socket}", "shutdown"],
                           env=self._env(), capture_output=True)
            try:
                self._proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        self._proc = None
        if self.keep_workdir:
            print(f"  Kept workdir {self.workdir}")
        else:
            shutil.rmtree(self.workdir, ignore_errors=True)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()

    # ── SQL ──────────────────────────────────────────────────────────────────

    def _base_cmd(self, db: str = "") -> list:
        cmd = [str(self.basedir / "bin" / "mysql"), "--no-defaults", "-uroot",
               f"--socket={self.socket}", "--batch", "--silent"]
        if db:
            cmd += ["-D", db]
        return cmd

    def sql(self, sql: str, db: str = "") -> str:
        """Execute SQL with the mysql client, return stdout (untimed setup path)."""
        r = subprocess.run(self._base_cmd(db) + ["-e", sql], env=self._env(),
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"SQL failed (rc={r.returncode}): {r.stderr.strip()}")
        return r.stdout

    def sql_stdin(self, sql: str, db: str = ""):
        """Execute multi-statement SQL from stdin (handles DELIMITER)."""
        r = subprocess.run(self._base_cmd(db), env=self._env(), input=sql.encode(),
                           capture_output=True)
        if r.returncode != 0:
            raise RuntimeError(f"SQL (stdin) failed (rc={r.returncode}): "
                               f"{r.stderr.decode().strip()}")

    def scalar(self, sql: str, db: str = "") -> str:
        out = self.sql(sql, db).strip()
        return out.splitlines()[-1].strip() if out else ""

    def _cursor(self, db: str):
        if self._conn is None or not self._conn.is_connected():
            self._conn = self._connect()
            self._db = None
        cur = self._conn.cursor()
        if db and db != self._db:
            cur.execute(f"USE `{db}`")
            self._db = db
        return cur

    def _run(self, cur, q: str) -> list:
        import mysql.connector
        q = q.strip().rstrip(";")
        try:
            cur.execute(q)
            return cur.fetchall() if cur.with_rows else []
        except mysql.connector.Error as e:
            raise RuntimeError(f"SQL failed: {e}") from None

    def execute(self, sql: str, db: str = ""):
        """Run one (possibly large) statement over the persistent connection."""
        cur = self._cursor(db)
        try:
            self._run(cur, sql)
        finally:
            cur.close()

    def sql_batch_timed(self, queries: list, db: str = "") -> list:
        """Client-observed latency (ms) of each query over the persistent
        connection: send, execute, and read the full result set."""
        cur = self._cursor(db)
        latencies = []
        try:
            for q in queries:
                t0 = time.perf_counter()
                self._run(cur, q)
                latencies.append((time.perf_counter() - t0) * 1000)
        finally:
            cur.close()
        return latencies

    def sql_batch_results(self, queries: list, db: str = "") -> list:
        """Each query's rows as tab-separated lines, like `mysql --batch --silent`."""
        cur = self._cursor(db)
        try:
            return [["\t".join(_format_cell(v) for v in row) for row in self._run(cur, q)]
                    for q in queries]
        finally:
            cur.close()

    def plugin_dir(self) -> str:
        return str(self.plugin_path)

    def data_dir(self) -> str:
        return self.scalar("SELECT @@datadir;")

    def install_plugin_file(self, src: str, name: str):
        dst = self.plugin_path / name
        if dst.is_symlink() or dst.exists():
            dst.unlink()
        shutil.copy(src, dst)

    def ensure_client_libs(self):
        pass  # MyVector links libmysqlclient statically; nothing to install on the host

    def write_myvector_cnf(self, text: str):
        cnf = self.datadir / "myvector.cnf"
        cnf.write_text(text)
        cnf.chmod(0o600)


# ── host metadata (myvector#133: results comparable across machines) ─────────

def _read(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def host_metadata() -> dict:
    """Describe the machine a result came from. No hostname or user names."""
    cpu_model = ""
    try:
        lscpu = subprocess.run(["lscpu"], capture_output=True, text=True).stdout
    except OSError:  # no lscpu (e.g. macOS)
        lscpu = ""
    for line in lscpu.splitlines():
        if line.startswith("Model name:"):
            cpu_model = line.split(":", 1)[1].strip()
            break
    if not cpu_model:
        for line in _read("/proc/cpuinfo").splitlines():
            if line.startswith("model name"):
                cpu_model = line.split(":", 1)[1].strip()
                break
    mem_kb = 0
    for line in _read("/proc/meminfo").splitlines():
        if line.startswith("MemTotal:"):
            mem_kb = int(line.split()[1])
            break
    try:
        os_name = platform.freedesktop_os_release().get("PRETTY_NAME", "")
    except (AttributeError, OSError):  # Python < 3.10, or no os-release (macOS)
        os_name = platform.platform()
    arch = platform.machine()
    cores = os.cpu_count() or 0
    return {
        "arch": arch,
        "cpu_model": cpu_model or "unknown",
        "cpu_cores": cores,
        "cpu_governor": _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor") or None,
        "mem_total_gb": round(mem_kb / 2**20, 1),
        "kernel": platform.release(),
        "os": os_name,
        "machine_key": machine_key(arch, cpu_model or "unknown", cores),
    }


def machine_key(arch: str, cpu_model: str, cores: int) -> str:
    """Stable, filesystem-safe id for a machine type, used to keep baselines apart."""
    slug = re.sub(r"[^a-z0-9]+", "-", cpu_model.lower()).strip("-") or "unknown"
    return f"{arch}-{slug}-{cores}c"


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
  IF NOT REGEXP_LIKE(colinfo, '^[[:space:]]*MYVECTOR COLUMN', 'i') THEN
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


def check_index_build_result(output: str, what: str = "MYVECTOR_INDEX_BUILD") -> None:
    """Raise unless MYVECTOR_INDEX_BUILD reported SUCCESS.

    The procedure reports failures (no connection back to the server, index not saved
    to disk) as a result row, not as an SQL error. Without this check a failed build
    leaves an empty index and the benchmark carries on to report meaningless numbers
    (recall_at_10 = 0.0, ANN as slow as brute-force KNN).
    """
    if "SUCCESS" not in output:
        raise RuntimeError(f"{what}: MYVECTOR_INDEX_BUILD did not report SUCCESS: "
                           f"{output.strip()!r}")


def myvector_cnf(root_pw: str, port: int = 3306) -> str:
    """Contents of myvector.cnf: how the index build connects back to the server."""
    return (
        f"myvector_host=127.0.0.1\n"
        f"myvector_user_id=root\n"
        f"myvector_user_password={root_pw}\n"
        f"myvector_port={port}\n"
    )


def _configure_myvector(container: Container):
    """Write myvector.cnf and point the index directory at the datadir.

    Needed by both the plugin and the component: without the cnf the index build
    cannot connect back to the server and leaves an empty index.
    """
    data_dir = container.data_dir()
    container.write_myvector_cnf(myvector_cnf(container.root_pw, container.myvector_port))
    try:
        container.sql(f"SET GLOBAL myvector_index_dir='{data_dir}';")
    except RuntimeError:
        pass  # sysvar not available on all versions (the component has none)


def install_component(container: Container, comp_dir: str):
    """Install MyVector component build into the server."""
    container.ensure_client_libs()
    container.install_plugin_file(f"{comp_dir}/libmyvector_component.so", "myvector.so")
    container.install_plugin_file(f"{comp_dir}/myvector.json", "myvector.json")
    container.sql("INSTALL COMPONENT 'file://myvector';")

    _configure_myvector(container)
    # myvector_distance is auto-registered by the component framework (dynamic UDF).
    # myvector_row_distance / myvector_is_valid / myvector_search_open_udf are NOT
    # auto-registered — they must be added via CREATE FUNCTION ... SONAME.
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
    """Install MyVector plugin build into the server."""
    container.install_plugin_file(plugin_so, "myvector.so")
    # Check if the plugin is already active (e.g. loaded via plugin-load-add in my.cnf
    # on pre-built GHCR images). If load_option=ON it cannot be uninstalled while the
    # server is running; skip INSTALL PLUGIN and rely on the .so already being in place.
    out = container.sql(
        "SELECT load_option FROM information_schema.plugins"
        " WHERE plugin_name='myvector';",
        "information_schema",
    )
    already_permanent = any(l.strip().upper() == "ON" for l in out.splitlines() if l.strip())
    if not already_permanent:
        try:
            container.sql("UNINSTALL PLUGIN myvector;", "mysql")
        except RuntimeError:
            pass
        container.sql("INSTALL PLUGIN myvector SONAME 'myvector.so';", "mysql")
    container.sql(
        "DROP FUNCTION IF EXISTS myvector_construct;"
        " DROP FUNCTION IF EXISTS myvector_display;"
        " DROP FUNCTION IF EXISTS myvector_distance;"
        " DROP FUNCTION IF EXISTS myvector_ann_set;"
        " DROP FUNCTION IF EXISTS myvector_is_valid;"
        " DROP FUNCTION IF EXISTS myvector_row_distance;"
        " DROP FUNCTION IF EXISTS myvector_search_open_udf;"
        " CREATE FUNCTION myvector_construct       RETURNS STRING  SONAME 'myvector.so';"
        " CREATE FUNCTION myvector_display         RETURNS STRING  SONAME 'myvector.so';"
        " CREATE FUNCTION myvector_distance        RETURNS REAL    SONAME 'myvector.so';"
        " CREATE FUNCTION myvector_ann_set         RETURNS STRING  SONAME 'myvector.so';"
        " CREATE FUNCTION myvector_is_valid        RETURNS INTEGER SONAME 'myvector.so';"
        " CREATE FUNCTION myvector_row_distance    RETURNS REAL    SONAME 'myvector.so';"
        " CREATE FUNCTION myvector_search_open_udf RETURNS STRING  SONAME 'myvector.so';",
        "mysql",
    )
    # The plugin needs myvector.cnf too: without it the index build cannot connect back
    # to the server ("Can't connect to local MySQL server through socket ''"), leaves
    # an empty index and every ANN query returns nothing.
    _configure_myvector(container)
    container.sql_stdin(INSTALL_PROCS_SQL, "mysql")
    print("  Plugin installed.")


def _resolve_artifact_dir(artifact_key: str) -> str:
    """Return local path to component artifact directory.

    Checks dist/<artifact_key>/ first. If libmyvector_component.so is missing,
    downloads <artifact_key>.tar.gz from the latest GitHub release using gh CLI
    and extracts into dist/<artifact_key>/.
    """
    import re as _re
    if not _re.fullmatch(r'[A-Za-z0-9._-]+', artifact_key):
        raise ValueError(f"Invalid artifact_key {artifact_key!r}: only [A-Za-z0-9._-] allowed")
    local = Path(f"dist/{artifact_key}")
    if local.is_dir() and (local / "libmyvector_component.so").exists():
        return str(local)

    with tempfile.TemporaryDirectory() as dl_dir:
        try:
            r = subprocess.run(
                ["gh", "release", "download", "--pattern", f"{artifact_key}.tar.gz",
                 "--dir", dl_dir],
                capture_output=True, text=True,
            )
        except FileNotFoundError:
            raise RuntimeError(
                "gh CLI not found. Install it from https://cli.github.com "
                "or use --artifact-dir to specify a local path."
            )
        if r.returncode != 0:
            raise RuntimeError(
                f"gh release download failed for {artifact_key}.tar.gz:\n{r.stderr.strip()}\n"
                "Tip: release assets are named myvector-component-mysql<ver>-linux-<arch>.tar.gz; "
                "use --artifact-dir dist/<key> after running the build script locally."
            )
        archives = list(Path(dl_dir).glob(f"{artifact_key}.tar.gz"))
        if not archives:
            raise RuntimeError(
                f"Archive not found after gh release download: {artifact_key}.tar.gz\n"
                "Tip: use --artifact-dir dist/<key> instead of --artifact for local builds."
            )
        local.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                ["tar", "-xzf", str(archives[0]), "-C", str(local)],
                check=True,
            )
        except FileNotFoundError:
            raise RuntimeError("tar not found; install it or use --artifact-dir.")

    if not (local / "libmyvector_component.so").exists():
        raise RuntimeError(
            f"libmyvector_component.so not found in {local} after extraction"
        )
    return str(local)


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


def _is_float(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _tsv_has_word_column(path: str, sample: int = 1000) -> bool:
    with open(path, encoding="utf-8") as f:
        seen = 0
        for line in f:
            parts = line.split(maxsplit=1)
            if not parts:
                continue
            if not _is_float(parts[0]):
                return True
            seen += 1
            if seen >= sample:
                break
    return False


def _load_tsv(path: str, rows: int, dim: int) -> list:
    """Load up to `rows` vectors from a whitespace-separated file.

    Each line is either '<word> <f1> <f2> ...' or '<f1> <f2> ...'. A
    non-numeric first field is always a word. A numeric one is a word only
    if the file has a word column (some line among the first 1000 starts
    with a non-numeric field): GloVe has thousands of numeric words
    ("2008", "nan"), but in a file without words "1.0 1.1 1.2 9.9" is a
    vector with a trailing field.
    """
    has_words = _tsv_has_word_column(path)
    vectors = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if len(vectors) >= rows:
                break
            parts = line.strip().split()
            if not parts:
                continue
            if not _is_float(parts[0]):
                start = 1
            else:
                start = 1 if has_words and len(parts) > dim else 0
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


_DISTANCES = {"l2": "L2", "cosine": "Cosine"}


def distance_metric(wp: dict) -> str:
    """The workload's distance metric (``distance``, default L2), spelled the
    way MyVector's index option and myvector_distance() expect it."""
    given = str(wp.get("distance", "L2"))
    try:
        return _DISTANCES[given.lower()]
    except KeyError:
        raise ValueError(f"unsupported distance {given!r}; use L2 or Cosine") from None


def _holdout_count(wp: dict) -> int:
    n = int(wp.get("holdout_queries", 0))
    if n < 0:
        raise ValueError(f"holdout_queries must be >= 0, got {n}")
    return n


def load_workload(dataset: str, wp: dict) -> tuple:
    """Load `rows` indexed vectors plus `holdout_queries` held-out ones.

    Returns (indexed, held_out). If the dataset is too short, the holdout is
    still taken in full and `indexed` is whatever is left; callers report
    len(indexed), not the configured `rows`. May update wp['dim'] (GloVe).
    """
    holdout = _holdout_count(wp)
    load_wp = dict(wp, rows=wp.get("rows", 10000) + holdout)
    vectors = load_dataset(dataset, load_wp)
    wp['dim'] = load_wp.get('dim', wp.get('dim'))
    return split_holdout(vectors, holdout)


def split_holdout(vectors: list, n: int, seed: int = 131) -> tuple:
    """Split off ``n`` query vectors that are never inserted into the index.

    Returns (indexed, queries); with n == 0 every row is indexed and queries
    is None (the old behaviour: recall queries sampled from indexed rows,
    so each query's nearest neighbour is itself). The split is a seeded
    shuffle, so GloVe's frequency ordering doesn't bias the query set.
    """
    if n <= 0:
        return vectors, None
    if n >= len(vectors):
        raise ValueError(f"holdout_queries={n} leaves no rows to index ({len(vectors)} loaded)")
    order = list(range(len(vectors)))
    random.Random(seed).shuffle(order)
    held = set(order[:n])
    queries = [vectors[i] for i in order[:n]]
    indexed = [v for i, v in enumerate(vectors) if i not in held]
    return indexed, queries


def knn_sql(q: list, metric: str) -> str:
    """Brute-force top-10 ids for q: the ground truth for recall."""
    return (f"SELECT id FROM bench.build_t"
            f" ORDER BY myvector_distance(vec, {_vec_literal(q)}, '{metric}') LIMIT 10;")


def _query_sample(vectors: list, held_out, n: int, seed: int) -> list:
    """Held-out queries when there are any, else a seeded sample of indexed rows."""
    if held_out:
        return held_out[:n]
    rng = random.Random(seed)
    return [vectors[rng.randint(0, len(vectors) - 1)] for _ in range(n)]


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

    _create_bench_table(container, dim, rows, M, ef, "bench", "build_t",
                        dist=distance_metric(wp))

    batch = 500
    for start in range(0, rows, batch):
        chunk = vectors[start:start + batch]
        vals = ", ".join(f"({start + i}, {_vec_literal(v)})" for i, v in enumerate(chunk))
        container.execute(f"INSERT INTO bench.build_t (id, vec) VALUES {vals};", "bench")

    t0 = time.time()
    out = container.sql("CALL mysql.MYVECTOR_INDEX_BUILD('bench.build_t.vec', 'id');")
    elapsed = time.time() - t0
    check_index_build_result(out, "bench.build_t")
    print(f"    index_build_time_s = {elapsed:.2f}")
    return elapsed


def bench_insert_throughput(container: Container, vectors: list, wp: dict) -> float:
    """INSERT rows into an online=Y indexed table; return QPS."""
    dim = wp['dim']
    M = wp.get('M', 16)
    ef = wp.get('ef_construction', 200)
    rows = len(vectors)
    print(f"  [insert_throughput] {rows} rows, dim={dim}, online=Y")

    _create_bench_table(container, dim, rows, M, ef, "bench", "insert_t", online=True,
                        dist=distance_metric(wp))

    t0 = time.time()
    batch = 500
    for start in range(0, rows, batch):
        chunk = vectors[start:start + batch]
        vals = ", ".join(f"({start + i}, {_vec_literal(v)})" for i, v in enumerate(chunk))
        container.execute(f"INSERT INTO bench.insert_t (id, vec) VALUES {vals};", "bench")
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

    queries = [knn_sql(q, distance_metric(wp)) for q in query_vectors]
    latencies_ms = container.sql_batch_timed(queries)

    latencies_ms.sort()
    p50 = statistics.median(latencies_ms)
    p99 = latencies_ms[max(0, math.ceil(len(latencies_ms) * 0.99) - 1)]
    qps = n_queries / (sum(latencies_ms) / 1000) if latencies_ms else 0.0
    print(f"    knn_qps={qps:.0f}  p50={p50:.1f}ms  p99={p99:.1f}ms")
    return {"knn_qps": qps, "knn_p50_ms": p50, "knn_p99_ms": p99}


def bench_knn_ann(container: Container, vectors: list, wp: dict,
                  ann_gate: bool = False) -> dict:
    """Run MYVECTOR_IS_ANN queries using the HNSW index.

    When ann_gate=True (9.x component cell), probe failure is a hard error.
    Otherwise returns knn_ann_qps=0.0 and null latencies when query rewrite
    is inactive.
    """
    n_queries = wp.get('knn_ann_queries', 200)
    print(f"  [knn_ann] {n_queries} queries, dim={wp['dim']}")

    # Probe with a correctly-dimensioned zero vector. Using dim=1 previously
    # prevented the rewrite hook from matching the column and falsely reported
    # the rewrite as inactive.
    probe_vec = "[" + ",".join(["0.0"] * wp['dim']) + "]"
    probe_supported = True
    try:
        container.sql(
            f"SELECT MYVECTOR_IS_ANN('bench.build_t.vec', 'id',"
            f" myvector_construct('{probe_vec}'))"
            f" FROM bench.build_t LIMIT 0;",
            db="bench",
        )
    except RuntimeError as e:
        err = str(e)
        if "does not exist" in err and "FUNCTION" in err:
            probe_supported = False
        # Other errors (e.g. index not open, dim mismatch) mean rewrite IS active.

    if not probe_supported:
        if ann_gate:
            raise RuntimeError(
                "MYVECTOR_IS_ANN probe failed on gated cell: query rewrite inactive. "
                "Ensure INSTALL COMPONENT succeeded and the index is loaded."
            )
        print("    ⚠ MYVECTOR_IS_ANN not supported (query rewrite inactive)")
        return {
            "knn_ann_qps": 0.0,
            "knn_ann_p50_ms": None,
            "knn_ann_p99_ms": None,
            "ann_rewrite_active": False,
        }

    rng = random.Random(77)
    query_vectors = [vectors[rng.randint(0, len(vectors) - 1)] for _ in range(n_queries)]

    queries = [
        f"SELECT id, myvector_row_distance(id) AS dist"
        f" FROM bench.build_t"
        f" WHERE MYVECTOR_IS_ANN('bench.build_t.vec', 'id', {_vec_literal(q)})"
        f" ORDER BY dist LIMIT 10;"
        for q in query_vectors
    ]
    latencies_ms = container.sql_batch_timed(queries)

    latencies_ms.sort()
    p50 = statistics.median(latencies_ms)
    p99 = latencies_ms[max(0, math.ceil(len(latencies_ms) * 0.99) - 1)]
    qps = n_queries / (sum(latencies_ms) / 1000) if latencies_ms else 0.0
    print(f"    knn_ann_qps={qps:.0f}  p50={p50:.1f}ms  p99={p99:.1f}ms")
    return {
        "knn_ann_qps": qps,
        "knn_ann_p50_ms": p50,
        "knn_ann_p99_ms": p99,
        "ann_rewrite_active": True,
    }


def bench_recall(container: Container, vectors: list, wp: dict,
                 held_out: list = None) -> dict:
    """Measure recall@10: fraction of true KNN top-10 found by ANN, averaged over queries.

    Returns recall_at_10=None when MYVECTOR_IS_ANN is inactive: on
    component builds from before #156 (v1.26.9 and earlier, see #144). It
    is active on plugin builds and on components built after #156.
    """
    n_queries = min(wp.get('recall_queries', 50), len(held_out or vectors))
    print(f"  [recall] {n_queries} {'held-out ' if held_out else ''}queries, dim={wp['dim']}")

    # Same probe used by bench_knn_ann to detect inactive query rewrite.
    probe_supported = True
    probe_vec = "[" + ",".join(["0.0"] * wp['dim']) + "]"
    try:
        container.sql(
            f"SELECT MYVECTOR_IS_ANN('bench.build_t.vec', 'id',"
            f" myvector_construct('{probe_vec}'))"
            f" FROM bench.build_t LIMIT 0;",
            db="bench",
        )
    except RuntimeError as e:
        if "does not exist" in str(e) and "FUNCTION" in str(e):
            probe_supported = False

    if not probe_supported:
        print("    ⚠ MYVECTOR_IS_ANN not supported — recall_at_10=None")
        return {"recall_at_10": None}

    query_vectors = _query_sample(vectors, held_out, n_queries, seed=42)

    # Ground truth: brute-force top-10 by distance, and the ANN top-10 via
    # query rewrite, each batched over a single persistent session (not one
    # docker exec per query -- same motivation as sql_batch_timed/#124).
    knn_queries = [knn_sql(q, distance_metric(wp)) for q in query_vectors]
    ann_queries = [
        f"SELECT id FROM bench.build_t"
        f" WHERE MYVECTOR_IS_ANN('bench.build_t.vec', 'id', {_vec_literal(q)})"
        f" ORDER BY myvector_row_distance(id) LIMIT 10;"
        for q in query_vectors
    ]
    knn_blocks = container.sql_batch_results(knn_queries)
    ann_blocks = container.sql_batch_results(ann_queries)

    recalls = []
    for knn_block, ann_block in zip(knn_blocks, ann_blocks):
        # mysql --batch --silent prints no column-header row (verified
        # empirically), just the raw values -- no line to skip here. The
        # pre-batching version of this function used to skip line 1 assuming
        # a header was present, which silently dropped each set's true first
        # id and computed recall over 9-vs-9 candidates instead of 10-vs-10.
        knn_ids = {int(line) for line in knn_block if line.strip()}
        ann_ids = {int(line) for line in ann_block if line.strip()}
        if knn_ids:
            recalls.append(len(knn_ids & ann_ids) / len(knn_ids))

    recall = sum(recalls) / len(recalls) if recalls else None
    if recall is not None:
        print(f"    recall_at_10={recall:.3f}")
    return {"recall_at_10": recall}


def bench_ef_search_sweep(container: Container, vectors: list, wp: dict,
                          held_out: list = None) -> dict:
    """Sweep ef_search and record recall@10 + QPS/latency at each point
    (ann-benchmarks style), so the actual accuracy/throughput tradeoff --
    not just whatever ef_search the index happened to build with -- is
    visible and comparable across runs and datasets (myvector#131).

    Returns {"ef_search_sweep": []} when no sweep is configured
    (wp['ef_search_sweep'] empty/absent) or MYVECTOR_IS_ANN is inactive
    (same probe used by bench_knn_ann/bench_recall).
    """
    sweep_points = wp.get('ef_search_sweep') or []
    if not sweep_points:
        return {"ef_search_sweep": []}

    n_queries = min(wp.get('ef_search_sweep_queries', 50), len(held_out or vectors))
    print(f"  [ef_search_sweep] ef_search={sweep_points} x {n_queries}"
          f" {'held-out ' if held_out else ''}queries, dim={wp['dim']}")

    probe_supported = True
    probe_vec = "[" + ",".join(["0.0"] * wp['dim']) + "]"
    try:
        container.sql(
            f"SELECT MYVECTOR_IS_ANN('bench.build_t.vec', 'id',"
            f" myvector_construct('{probe_vec}'))"
            f" FROM bench.build_t LIMIT 0;",
            db="bench",
        )
    except RuntimeError as e:
        if "does not exist" in str(e) and "FUNCTION" in str(e):
            probe_supported = False

    if not probe_supported:
        print("    ⚠ MYVECTOR_IS_ANN not supported — ef_search_sweep skipped")
        return {"ef_search_sweep": []}

    # Same query sample at every sweep point, so points are directly
    # comparable to each other and not just to their own sampling noise.
    query_vectors = _query_sample(vectors, held_out, n_queries, seed=55)

    # Ground truth (brute-force top-10) doesn't depend on ef_search; compute
    # once and reuse across every sweep point.
    knn_queries = [knn_sql(q, distance_metric(wp)) for q in query_vectors]
    knn_blocks = container.sql_batch_results(knn_queries)
    # mysql --batch --silent prints no column-header row; nothing to skip.
    ground_truth = [
        {int(line) for line in block if line.strip()} for block in knn_blocks
    ]

    sweep = []
    for ef in sweep_points:
        opts = f"nn=10,ef_search={ef}"
        ann_queries = [
            f"SELECT id FROM bench.build_t"
            f" WHERE MYVECTOR_IS_ANN('bench.build_t.vec', 'id', {_vec_literal(q)}, '{opts}')"
            f" ORDER BY myvector_row_distance(id) LIMIT 10;"
            for q in query_vectors
        ]

        # Two separate batched passes over the same ann_queries -- one for
        # results (recall), one for timing (QPS/latency) -- rather than one
        # combined pass, since HNSW search is deterministic for a fixed
        # graph/ef_search (no randomness at query time), so this costs one
        # extra docker exec per sweep point but not a different answer.
        ann_blocks = container.sql_batch_results(ann_queries)
        recalls = []
        for truth, block in zip(ground_truth, ann_blocks):
            ann_ids = {int(line) for line in block if line.strip()}
            if truth:
                recalls.append(len(truth & ann_ids) / len(truth))
        recall = sum(recalls) / len(recalls) if recalls else None

        latencies_ms = container.sql_batch_timed(ann_queries)
        latencies_ms.sort()
        p50 = statistics.median(latencies_ms)
        p99 = latencies_ms[max(0, math.ceil(len(latencies_ms) * 0.99) - 1)]
        qps = n_queries / (sum(latencies_ms) / 1000) if latencies_ms else 0.0

        sweep.append({
            "ef_search": ef,
            "recall_at_10": recall,
            "qps": qps,
            "p50_ms": p50,
            "p99_ms": p99,
        })
        recall_str = f"{recall:.3f}" if recall is not None else "N/A"
        print(f"    ef_search={ef:<4} recall@10={recall_str}  qps={qps:.0f}  "
              f"p50={p50:.1f}ms  p99={p99:.1f}ms")

    return {"ef_search_sweep": sweep}


def run_workloads(container: Container, vectors: list, wp: dict,
                  build_path: str, mysql_version: str,
                  ann_gate: bool = False, held_out: list = None) -> dict:
    metrics = {}
    metrics["index_build_time_s"] = bench_index_build(container, vectors, wp)
    metrics["insert_qps"] = bench_insert_throughput(container, vectors, wp)
    metrics.update(bench_knn_search(container, vectors, wp))
    metrics.update(bench_knn_ann(container, vectors, wp, ann_gate=ann_gate))
    metrics.update(bench_recall(container, vectors, wp, held_out=held_out))
    metrics.update(bench_ef_search_sweep(container, vectors, wp, held_out=held_out))
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
    # Support both the new `cells` format and the legacy flat matrix format.
    cells = matrix.get("cells")
    if cells:
        pairs = [(str(cell['mysql']), cell['build']) for cell in cells]
    else:
        mysql_versions = matrix.get("mysql_versions", [])
        build_paths = matrix.get("build_paths", [])
        pairs = [(str(ver), bp) for ver in mysql_versions for bp in build_paths]

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
        for ver, bp in pairs:
            cell_dir = Path(wt_dir) / ver / bp
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

def _docker_extra_volumes(mysql_version: str, build_path: str, artifact_dir: str,
                          image: str = None) -> list:
    # For plugin builds, if a bundled libstdc++ is present we mount it over the
    # container's existing libstdc++.so.6.0.XX so mysqld starts with the newer one.
    # (Hot-swapping after mysqld starts is too late — dlopen uses the already-loaded lib.)
    extra_volumes: list = []
    if build_path == "plugin":
        import glob as _glob
        libstdcxx_files = sorted(_glob.glob(os.path.join(artifact_dir, "libstdc++.so.6.*")))
        if libstdcxx_files:
            libstdcxx_src = libstdcxx_files[-1]
            # Detect the versioned filename the image's libstdc++.so.6 symlink resolves to,
            # so the mount target stays correct across MySQL patch versions.
            probe_image = image or f"mysql:{mysql_version}"
            r = subprocess.run(
                ["docker", "run", "--rm", probe_image,
                 "readlink", "-f", "/lib64/libstdc++.so.6"],
                capture_output=True, text=True,
            )
            libstdcxx_target = r.stdout.strip() if r.returncode == 0 else ""
            if libstdcxx_target:
                extra_volumes.append(f"{libstdcxx_src}:{libstdcxx_target}:ro")
            else:
                print(
                    "  Warning: could not resolve /lib64/libstdc++.so.6 inside "
                    f"{probe_image}; skipping bundled libstdc++ mount"
                )
    return extra_volumes


def make_server(server: str, mysql_version: str, build_path: str, artifact_dir: str,
                image: str = None, host_opts: dict = None):
    """The server to benchmark: a Docker container (default) or a host mysqld."""
    if server == "docker":
        return Container(mysql_version, image=image,
                         extra_volumes=_docker_extra_volumes(
                             mysql_version, build_path, artifact_dir, image))
    host_opts = host_opts or {}
    basedir = host_opts.get("basedir")
    if not basedir:
        full = host_opts.get("full_version") or MYSQL_FULL_VERSIONS.get(mysql_version)
        if not full:
            raise ValueError(f"no default patch release for MySQL {mysql_version}; "
                             "pass --mysql-full-version or --mysql-basedir")
        basedir = resolve_mysql_basedir(full, host_opts.get("cache_dir")
                                        or default_mysql_cache_dir())
    workdir_root = host_opts.get("workdir_root") or str(
        Path(host_opts.get("cache_dir") or default_mysql_cache_dir()).parent / "bench")
    return HostServer(mysql_version, basedir, workdir_root,
                      keep_workdir=host_opts.get("keep_workdir", False))


def connection_latency(server, n: int = 200) -> dict:
    """Round trip of a trivial query: what the harness itself adds to every
    measured query (issue #133 wants this well under 1 ms on a host server)."""
    lat = sorted(server.sql_batch_timed(["SELECT 1"] * n))
    return {
        "select1_p50_ms": statistics.median(lat),
        "select1_p99_ms": lat[max(0, math.ceil(len(lat) * 0.99) - 1)],
    }


def run_benchmark(mysql_version: str, build_path: str, artifact_dir: str,
                  config: dict, output: str, image: str = None,
                  ann_gate: bool = False, server: str = "docker",
                  host_opts: dict = None):
    wp = dict(config.get('workload', {}))
    git_ref = _git_ref()
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    runner = os.environ.get("RUNNER_NAME", "local")
    dataset = wp.get("dataset", "synthetic")
    holdout = _holdout_count(wp)  # fail on bad settings before starting a server
    distance_metric(wp)

    print(f"=== myvectorbench: mysql:{mysql_version} {build_path} @ {git_ref}"
          f" (server={server}) ===")

    with make_server(server, mysql_version, build_path, artifact_dir,
                     image=image, host_opts=host_opts) as c:
        if build_path == "component":
            install_component(c, artifact_dir)
        else:
            install_plugin(c, os.path.join(artifact_dir, "myvector.so"))

        mysqld_version = c.scalar("SELECT @@version;")
        latency = connection_latency(c)
        print(f"  [connection] SELECT 1 p50={latency['select1_p50_ms']:.3f}ms"
              f"  p99={latency['select1_p99_ms']:.3f}ms ({c.connection})")

        vectors, held_out = load_workload(dataset, wp)
        metrics = run_workloads(c, vectors, wp, build_path, mysql_version,
                                ann_gate=ann_gate, held_out=held_out)
        metrics.update(latency)

    result = {
        "git_ref": git_ref,
        "mysql_version": mysql_version,
        "mysqld_version": mysqld_version,
        "build_path": build_path,
        "server_mode": server,
        "connection": c.connection,
        "timestamp": timestamp,
        "runner": runner,
        "host": host_metadata(),
        "dataset": dataset,
        "workload_params": {
            "rows": len(vectors),  # rows actually indexed
            "dim": wp.get("dim", 128),
            "M": wp.get("M", 16),
            "ef_construction": wp.get("ef_construction", 200),
            "knn_queries": wp.get("knn_queries", 200),
            "knn_ann_queries": wp.get("knn_ann_queries", 200),
            "recall_queries": wp.get("recall_queries", 50),
            "holdout_queries": holdout,
            "distance": distance_metric(wp),
        },
        "metrics": metrics,
    }

    Path(output).parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w") as f:
        json.dump(result, f, indent=2)
    print(f"  Result written to {output}")
    print(f"  Metrics: {json.dumps(metrics, indent=2)}")
    return result


def cell_artifact_dir(cell: dict, artifact_root: str) -> str:
    """Artifact directory for one matrix cell under artifact_root.

    Components: <root>/<cell.artifact or component-<ver>>. Plugin: prefer
    <root>/plugin-<ver>-ol9 (built by build-plugin-<ver>-docker.sh, the same
    OL9 toolchain family as Oracle's binaries) over <root>/plugin-<ver>.
    """
    ver = str(cell["mysql"])
    root = Path(artifact_root)
    if cell["build"] == "component":
        return str(root / cell.get("artifact", f"component-{ver}"))
    ol9 = root / f"plugin-{ver}-ol9"
    return str(ol9 if (ol9 / "myvector.so").exists() else root / f"plugin-{ver}")


def run_all_cells(config: dict, artifact_root: str, output_dir: str,
                  server: str, host_opts: dict, ann_gate: bool = False) -> int:
    """Benchmark every matrix cell in the config; return the number that failed.

    Host results go under <output_dir>/<machine_key>/ so baselines from
    different machine types are never mixed.
    """
    cells = config.get("matrix", {}).get("cells") or []
    if not cells:
        raise ValueError("config has no matrix.cells to run")
    out = Path(output_dir)
    if server == "host":
        out = out / host_metadata()["machine_key"]
    failed = []
    for cell in cells:
        ver, build = str(cell["mysql"]), cell["build"]
        try:
            run_benchmark(ver, build, cell_artifact_dir(cell, artifact_root), config,
                          str(out / f"{ver}-{build}.json"),
                          image=cell.get("image") if server == "docker" else None,
                          ann_gate=ann_gate, server=server, host_opts=host_opts)
        except Exception as e:
            print(f"!!! cell {ver}/{build} failed: {e}", file=sys.stderr)
            failed.append(f"{ver}/{build}")
    print(f"=== {len(cells) - len(failed)}/{len(cells)} cells OK"
          + (f"; failed: {', '.join(failed)}" if failed else "") + f" — results in {out}")
    return len(failed)


def main():
    parser = argparse.ArgumentParser(description="myvectorbench runner")
    parser.add_argument("--mysql-version", help="MySQL version, e.g. 8.4")
    parser.add_argument("--build-path", choices=["component", "plugin"])
    parser.add_argument("--artifact-dir", help="Dir containing build artifacts")
    parser.add_argument("--config", default="myvectorbench.yml")
    parser.add_argument("--output", default="result.json")
    parser.add_argument("--image", default=None,
                        help="Docker image to use, e.g. mysql:9.7")
    parser.add_argument("--artifact", default=None, metavar="ARTIFACT_KEY",
                        help="Artifact key e.g. component-9.7 (resolves dist/ or downloads via gh)")
    parser.add_argument("--ann-gate", action="store_true",
                        help="Fail cell if MYVECTOR_IS_ANN probe is inactive (for 9.x component cells)")
    parser.add_argument("--promote", metavar="GIT_REF",
                        help="Promote GIT_REF results to baseline on benchmarks/ branch")
    host = parser.add_argument_group("server selection (myvector#133)")
    host.add_argument("--server", choices=["docker", "host"], default="docker",
                      help="docker: mysql in a container (default, used by CI); "
                           "host: an isolated mysqld from the official tarball")
    host.add_argument("--mysql-basedir",
                      help="host: use this extracted MySQL basedir instead of downloading")
    host.add_argument("--mysql-full-version",
                      help="host: patch release to download, e.g. 8.4.8 "
                           f"(default per series: {MYSQL_FULL_VERSIONS})")
    host.add_argument("--cache-dir",
                      help="host: where tarballs are downloaded and extracted "
                           "($MYVECTORBENCH_MYSQL_CACHE, else ~/.cache/myvectorbench/mysql)")
    host.add_argument("--workdir-root",
                      help="host: parent of each run's datadir/index/socket "
                           "(default: <cache-dir>/../bench)")
    host.add_argument("--keep-workdir", action="store_true",
                      help="host: keep the run's datadir and error log afterwards")
    host.add_argument("--all-cells", action="store_true",
                      help="run every matrix cell in --config (artifacts from "
                           "--artifact-root, results into --output-dir)")
    host.add_argument("--artifact-root", default="dist",
                      help="--all-cells: dir holding component-<ver>/ and plugin-<ver>[-ol9]/")
    host.add_argument("--output-dir", default="results/bench",
                      help="--all-cells: where result JSONs go")
    args = parser.parse_args()

    if args.promote:
        promote(args.promote, args.config)
        return

    host_opts = {
        "basedir": args.mysql_basedir,
        "full_version": args.mysql_full_version,
        "cache_dir": args.cache_dir,
        "workdir_root": args.workdir_root,
        "keep_workdir": args.keep_workdir,
    }
    config = load_config(args.config)

    if args.all_cells:
        sys.exit(1 if run_all_cells(config, args.artifact_root, args.output_dir,
                                    args.server, host_opts, ann_gate=args.ann_gate)
                 else 0)

    if not all([args.mysql_version, args.build_path]):
        parser.error("--mysql-version and --build-path are required")

    artifact_dir = args.artifact_dir
    if args.artifact and args.build_path == "plugin":
        parser.error("--artifact is only supported for component builds; use --artifact-dir for plugin builds")
    if args.artifact:
        artifact_dir = _resolve_artifact_dir(args.artifact)
    elif not artifact_dir:
        parser.error("--artifact-dir or --artifact is required")

    run_benchmark(
        args.mysql_version, args.build_path, artifact_dir, config, args.output,
        image=args.image, ann_gate=args.ann_gate, server=args.server,
        host_opts=host_opts,
    )


if __name__ == "__main__":
    main()
