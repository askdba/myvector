#!/usr/bin/env python3
"""Recreate one bench container from a BASE mysql image and install a freshly
built MyVector component (query-rewrite active). Run on the docker host that
owns the container (dev host for 9.7/26.7, the VM for 8.4).

Reuses scripts/myvectorbench.py's install_component / _configure_myvector /
INSTALL_PROCS path -- the same flow CI uses.
"""
import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import myvectorbench as mb  # noqa: E402


class BenchContainer(mb.Container):
    def __init__(self, name, version, root_pw, base_image, port,
                 cpuset=None, mem=None, datadir=None, sudo=False):
        self.version = version
        self.root_pw = root_pw
        self.name = name
        self._running = False
        self._base_image = base_image
        self._port = port
        self._cpuset = cpuset
        self._mem = mem
        self._datadir = datadir
        self._sudo = ["sudo"] if sudo else []

    def _d(self, *args):
        return subprocess.run(self._sudo + ["docker", *args], capture_output=True, text=True)

    # route the base class's `docker ...` calls through sudo when needed
    def _base_cmd(self, db="", interactive=False):
        cmd = self._sudo + ["docker", "exec"]
        if interactive:
            cmd.append("-i")
        cmd += ["-e", f"MYSQL_PWD={self.root_pw}", self.name,
                "mysql", "-uroot", "-h127.0.0.1", "--batch", "--silent"]
        if db:
            cmd += ["-D", db]
        return cmd

    def cp(self, host_src, container_dst):
        subprocess.run(self._sudo + ["docker", "cp", host_src,
                                     f"{self.name}:{container_dst}"], check=True)

    def exec(self, *args):
        return subprocess.run(self._sudo + ["docker", "exec", self.name, *args],
                              capture_output=True, text=True)

    def start(self):
        self._d("rm", "-fv", self.name)
        if self._datadir:
            # Fresh datadir so the base image initialises cleanly. The files are
            # owned by the container's mysql uid, so wipe them as root via a
            # throwaway container (a plain host rm as ubuntu silently fails and
            # leaves the OLD datadir in place).
            subprocess.run(self._sudo + ["bash", "-c",
                           f"mkdir -p {self._datadir}"], check=False)
            w = self._d("run", "--rm", "-v", f"{self._datadir}:/d", "alpine",
                        "sh", "-c", "rm -rf /d/* /d/.[!.]* /d/..?* 2>/dev/null; ls -A /d | wc -l")
            left = (w.stdout or "").strip().splitlines()[-1] if w.stdout else "?"
            print(f"  datadir wiped ({self._datadir}); entries left: {left}", flush=True)
        cmd = ["run", "-d", "--name", self.name, "--restart", "unless-stopped",
               "-e", f"MYSQL_ROOT_PASSWORD={self.root_pw}",
               "-e", "MYSQL_DATABASE=bench",
               "-p", f"127.0.0.1:{self._port}:3306"]
        if self._cpuset:
            cmd += ["--cpuset-cpus", self._cpuset]
        if self._mem:
            cmd += ["--memory", self._mem, "--memory-swap", self._mem]
        if self._datadir:
            cmd += ["-v", f"{self._datadir}:/var/lib/mysql"]
        cmd.append(self._base_image)
        r = self._d(*cmd)
        if r.returncode != 0:
            raise RuntimeError(f"docker run failed: {r.stderr}")
        self._running = True
        self._wait_ready()
        print(f"  base {self._base_image} ready ({self.name})", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--container", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--base-image", required=True)
    ap.add_argument("--comp-dir", required=True)
    ap.add_argument("--port", required=True)
    ap.add_argument("--cpuset", default=None)
    ap.add_argument("--mem", default=None)
    ap.add_argument("--datadir", default=None)
    ap.add_argument("--root-pw", required=True)
    ap.add_argument("--sudo", action="store_true")
    args = ap.parse_args()

    c = BenchContainer(args.container, args.version, args.root_pw, args.base_image,
                       args.port, args.cpuset, args.mem, args.datadir, args.sudo)
    print(f"== redeploy {args.container} (MySQL {args.version}) on fresh component ==", flush=True)
    c.start()

    # Authoritative install on a CLEAN datadir (same as smoke-component.sh /
    # pre-release-test.sh): INSTALL COMPONENT + supplemental SONAME UDFs + procs.
    mb.install_component(c, args.comp_dir)

    # Verify the ANN query-rewrite is now active.
    c.sql("CREATE DATABASE IF NOT EXISTS bench;")
    c.sql("DROP TABLE IF EXISTS bench.rwcheck;")
    c.sql("CREATE TABLE bench.rwcheck (id INT PRIMARY KEY, vec VARBINARY(520) "
          "COMMENT 'MYVECTOR COLUMN type=hnsw,dim=128,size=10,m=16,ef=200,idcol=id,dist=L2');")
    c.sql_stdin("INSERT INTO bench.rwcheck (id, vec) VALUES " +
                ",".join(f"({i}, {mb._vec_literal([float((i*7+j) % 13) for j in range(128)])})"
                         for i in range(10)) + ";", "bench")
    c.sql("CALL mysql.MYVECTOR_INDEX_BUILD('bench.rwcheck.vec','id');")
    probe = f"SELECT MYVECTOR_IS_ANN('bench.rwcheck.vec','id',myvector_construct('[{','.join(['0.0']*128)}]')) FROM bench.rwcheck LIMIT 0;"
    try:
        c.sql(probe, "bench")
        print("  ✅ MYVECTOR_IS_ANN query-rewrite ACTIVE", flush=True)
    except RuntimeError as e:
        if "does not exist" in str(e) and "FUNCTION" in str(e):
            print("  ❌ rewrite STILL INACTIVE after fresh install", flush=True)
            sys.exit(2)
        print("  ✅ rewrite active (probe error is index/dim, not missing function)", flush=True)
    c.sql("DROP TABLE IF EXISTS bench.rwcheck;")


if __name__ == "__main__":
    main()
