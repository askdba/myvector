#!/usr/bin/env python3
"""Disk-persisted HNSW index-loader hardening regressions (audit run-1,
Slice 4).

Four independent gaps let a corrupted or maliciously crafted .hnsw.index /
.hnsw.index.links file corrupt memory during HierarchicalDiskNSW::loadIndex
(include/hnswdisk.i), reachable given OS-level write access to
myvector_index_dir (or, separately, the path-traversal primitive closed in
Slice 2):

  1. cur_element_count/max_elements_/size_data_per_element_ were trusted
     verbatim from the file with no cross-validation, so a header where
     cur_element_count exceeds max_elements_ -- or whose own declared size
     does not actually fit in the file -- under-allocates
     data_level0_memory_ relative to what is then read into it.
  2. The structural integrity check hnswalg.h's in-memory loader has was
     disabled via #if 0 in this disk-persistence fork and never replaced.
  3. label_offset_/offsetData_ were trusted verbatim from the file and used
     unchecked in every getExternalLabel()/setExternalLabel() address
     computation -- the latter reachable again from ordinary INSERT traffic
     via the replace-deleted-element path, not just at load time.
  4. A nodeID read from the .links sidecar file was used unchecked as an
     index into two max_elements_-sized arrays (heap write).

Fix: validate cur_element_count <= max_elements_ and the level0 data block's
size against the real file size (overflow-safely) before any malloc/read;
recompute label_offset_/offsetData_/offsetLevel0_ from maxM0_/data_size_
instead of trusting the file (so corrupting them is now a no-op); validate
each .links nodeID against max_elements_ before using it as an array index.

This test builds a real HNSW index, corrupts specific header/sidecar fields
in the saved files byte-for-byte, reloads, and checks: the server survives
(mysqladmin ping, a normal query), the corruption is rejected (this loader's
existing catch block already falls back to a fresh empty index on any
std::runtime_error -- the same path a missing file already took before this
fix), and the index is fully usable again afterward. The label_offset_ case
is a positive control: since that field is no longer trusted at all, it
should reload successfully despite the corruption.

Usage:
  python3 scripts/test-hnsw-loader-hardening.py --plugin-dir dist/plugin-8.4
  python3 scripts/test-hnsw-loader-hardening.py \\
      --component-dir dist/component-9.7 --image mysql:9.7

Exit 0 = all checks passed.
"""

import argparse
import importlib.util
import os
import random
import struct
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "online_idle", os.path.join(HERE, "test-online-updates-idle.py"))
idle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(idle)

DIM = 4
SIZE = 1000  # index capacity (max_elements_)
ROWS = 300   # enough rows that at least one node almost certainly gets
             # level > 0, so the .links sidecar actually has a record

# Header layout written by saveIndexHeader()/readIndexHeader() (hnswdisk.i):
# every field is a raw sequential Write(), no struct padding.
#   offsetLevel0_          size_t   @0
#   max_elements_          size_t   @8
#   cur_element_count      size_t   @16
#   size_data_per_element_ size_t   @24
#   label_offset_          size_t   @32
#   offsetData_            size_t   @40
#   maxlevel_              int      @48
#   enterpoint_node_       u32      @52
#   maxM_                  size_t   @56
#   maxM0_                 size_t   @64
#   ... (header continues; not needed below)
OFF_CUR_ELEMENT_COUNT = 16
OFF_LABEL_OFFSET = 32
OFF_MAXM0 = 64


def main():
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--plugin-dir", default="dist/plugin-8.4")
    mode.add_argument("--component-dir",
                      help="test a component build instead of the plugin")
    ap.add_argument("--image", default="mysql:8.4")
    ap.add_argument("--keep", action="store_true",
                    help="keep the container for debugging")
    args = ap.parse_args()

    component = bool(args.component_dir)
    srv = idle.Server(args.image, f"myv-hnswload-{os.getpid()}")
    results = []

    def record(name, ok, detail):
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")

    def alive():
        r = idle.sh(["docker", "exec", srv.name, "mysqladmin", "ping", "-uroot",
                     f"-p{idle.PW}", "--silent"], check=False)
        return r.returncode == 0

    def assert_alive_and_responsive(label):
        record(f"{label}: server still answers ping", alive(), "mysqladmin ping")
        rows = srv.sql("SELECT 1;", db=None, check=False)
        record(f"{label}: server still executes a normal query",
               rows == [["1"]], f"{rows}")

    def find_index_file():
        r = idle.sh(["docker", "exec", srv.name, "bash", "-c",
                     "find /var/lib/mysql -iname '*.hnsw.index'"], check=False)
        paths = [p for p in r.stdout.splitlines() if p]
        if not paths:
            raise RuntimeError("no .hnsw.index file found under /var/lib/mysql")
        return paths[0]

    def docker_cp_out(container_path, local_path):
        idle.sh(["docker", "cp", f"{srv.name}:{container_path}", local_path])

    def docker_cp_in(local_path, container_path):
        idle.sh(["docker", "cp", local_path, f"{srv.name}:{container_path}"])
        idle.sh(["docker", "exec", srv.name, "chown", "mysql:mysql", container_path])

    def reload_index():
        """MYVECTOR_INDEX_LOAD calls loadIndex() directly on the already-open
        index object (it deletes the in-memory HierarchicalDiskNSW and
        constructs a fresh one from the on-disk files) -- the same function a
        real restart would call, without the extra time cost of one."""
        srv.sql("CALL mysql.myvector_index_load('vtest.t.v');", check=False)

    def log_tail():
        r = idle.sh(["docker", "logs", "--tail", "400", srv.name], check=False)
        return r.stdout + r.stderr

    def patch_header(local_path, offset, value, size=8):
        with open(local_path, "r+b") as f:
            f.seek(offset)
            f.write(value.to_bytes(size, "little"))

    try:
        srv.wait_ready()
        if component:
            idle.install_component(srv, args.component_dir)
        else:
            idle.install_plugin(srv, args.plugin_dir)
        srv.sql("CREATE DATABASE vtest;", db=None)

        if component:
            srv.sql(f"""CREATE TABLE t (id INT PRIMARY KEY,
                v VARBINARY({DIM * 4 + 64}) NULL COMMENT 'MYVECTOR COLUMN """
                    f"""type=hnsw,dim={DIM},size={SIZE},idcol=id,dist=L2');""")
        else:
            srv.sql(f"""CREATE TABLE t (id INT PRIMARY KEY,
                v MYVECTOR(type=HNSW,dim={DIM},size={SIZE},idcol=id,dist=L2) NULL);""")

        rnd = random.Random(1)
        values = ", ".join(
            f"({i}, myvector_construct('[{rnd.uniform(-1,1):.5f},"
            f"{rnd.uniform(-1,1):.5f},{rnd.uniform(-1,1):.5f},{rnd.uniform(-1,1):.5f}]'))"
            for i in range(1, ROWS + 1))
        srv.sql(f"INSERT INTO t VALUES {values};")
        build_rows = srv.sql("CALL mysql.myvector_index_build('vtest.t.v','id');")
        record("setup: index builds with real data",
               build_rows and build_rows[0] and build_rows[0][0].startswith("SUCCESS"),
               f"rows={build_rows}")

        indexfile = find_index_file()
        linksfile = indexfile + ".links"

        with tempfile.TemporaryDirectory() as tmp:
            backup_index = os.path.join(tmp, "backup.hnsw.index")
            backup_links = os.path.join(tmp, "backup.links")
            docker_cp_out(indexfile, backup_index)
            docker_cp_out(linksfile, backup_links)

            def restore():
                docker_cp_in(backup_index, indexfile)
                docker_cp_in(backup_links, linksfile)

            # --- 1. cur_element_count > max_elements_ must be rejected,
            # not under-allocate data_level0_memory_ relative to the read --
            work = os.path.join(tmp, "a.hnsw.index")
            idle.sh(["cp", backup_index, work])
            patch_header(work, OFF_CUR_ELEMENT_COUNT, (1 << 62))
            docker_cp_in(work, indexfile)
            reload_index()
            assert_alive_and_responsive("1a. cur_element_count > max_elements_")
            log = log_tail()
            record("1b. corruption is logged, not silently accepted",
                   "cur_element_count exceeds max_elements_" in log
                   or "Error loading hnsw index" in log,
                   "(looked for the rejection message in the server log)")
            restore()

            # --- 2. declared element count larger than what the file
            # actually holds must be rejected (reinstated structural check) -
            work = os.path.join(tmp, "b.hnsw.index")
            idle.sh(["cp", backup_index, work])
            patch_header(work, OFF_CUR_ELEMENT_COUNT, SIZE)  # <= max_elements_,
            # but the file was only ever written with ROWS elements' worth of
            # level0 data, so it doesn't actually hold SIZE elements.
            docker_cp_in(work, indexfile)
            reload_index()
            assert_alive_and_responsive("2a. declared count exceeds real file size")
            log = log_tail()
            record("2b. corruption is logged, not silently accepted",
                   "file too small for declared element count" in log
                   or "Error loading hnsw index" in log,
                   "(looked for the rejection message in the server log)")
            restore()

            # --- 3. positive control: a corrupted label_offset_ must have no
            # effect at all, since it is recomputed rather than trusted -----
            work = os.path.join(tmp, "c.hnsw.index")
            idle.sh(["cp", backup_index, work])
            patch_header(work, OFF_LABEL_OFFSET, (1 << 48))
            docker_cp_in(work, indexfile)
            reload_index()
            assert_alive_and_responsive("3a. corrupted label_offset_")
            n = idle.current_rows(srv, "vtest.t.v")
            record("3b. the index reloads normally despite the corrupted field "
                   "(label_offset_ is recomputed, not trusted)",
                   n == ROWS, f"Current Rows = {n} (want {ROWS})")
            restore()

            # --- 4. a .links record naming an out-of-range nodeID must be
            # rejected before it is used as an array index ------------------
            with open(backup_links, "rb") as f:
                first_record = f.read(8)
            node_id, link_list_size = struct.unpack("<II", first_record)
            if link_list_size == 0:
                print("note: no node in this build got level > 0; "
                      "skipping the .links nodeID check")
            else:
                work = os.path.join(tmp, "d.links")
                idle.sh(["cp", backup_links, work])
                with open(work, "r+b") as f:
                    f.write(struct.pack("<II", 0xFFFFFFFF, link_list_size))
                docker_cp_in(work, linksfile)
                reload_index()
                assert_alive_and_responsive("4a. out-of-range .links nodeID")
                log = log_tail()
                record("4b. corruption is logged, not silently accepted",
                       "nodeID out of range" in log
                       or "Error loading hnsw index" in log,
                       "(looked for the rejection message in the server log)")
                restore()

                # --- 4c. a .links nodeID within capacity (max_elements_,
                # SIZE above) but beyond the real element count
                # (cur_element_count, ROWS above) must also be rejected: a
                # real save never writes a record for a nodeID that isn't an
                # actual element, and clear() only frees linkLists_[i] for
                # i < cur_element_count, so this would otherwise leak a
                # buffer nothing ever frees (review of audit run-1, Slice 4:
                # PR #235) ---------------------------------------------------
                work = os.path.join(tmp, "e.links")
                idle.sh(["cp", backup_links, work])
                with open(work, "r+b") as f:
                    f.write(struct.pack("<II", ROWS + 50, link_list_size))
                docker_cp_in(work, linksfile)
                reload_index()
                assert_alive_and_responsive(
                    "4c. in-capacity but out-of-count .links nodeID")
                log = log_tail()
                record("4d. corruption is logged, not silently accepted",
                       "nodeID out of range" in log
                       or "Error loading hnsw index" in log,
                       "(looked for the rejection message in the server log)")
                restore()

            # --- 5. maxM0_ chosen to overflow size_links_level0_'s
            # computation (maxM0_*sizeof(tableint)+sizeof(linklistsizeint))
            # must be rejected before that wrapped value is trusted to size
            # the per-element stride (review of audit run-1, Slice 4: PR #235)
            work = os.path.join(tmp, "f.hnsw.index")
            idle.sh(["cp", backup_index, work])
            patch_header(work, OFF_MAXM0, (1 << 62) - 1)
            docker_cp_in(work, indexfile)
            reload_index()
            assert_alive_and_responsive("5a. maxM0_ overflow")
            log = log_tail()
            record("5b. corruption is logged, not silently accepted",
                   "maxM0_/maxM_ overflow" in log
                   or "Error loading hnsw index" in log,
                   "(looked for the rejection message in the server log)")
            restore()

            # --- 6. sanity: after all the corrupt-reload cycles, a plain
            # rebuild from the original (never-corrupted) table data works -
            reload_index()
            rebuild_rows = srv.sql("CALL mysql.myvector_index_build('vtest.t.v','id');")
            n = idle.current_rows(srv, "vtest.t.v")
            record("6. the index is fully usable again after restoring the "
                   "real files and rebuilding",
                   rebuild_rows and rebuild_rows[0]
                   and rebuild_rows[0][0].startswith("SUCCESS") and n == ROWS,
                   f"rows={rebuild_rows}, Current Rows = {n} (want {ROWS})")
    finally:
        if not args.keep:
            srv.remove()

    print("PASSED" if results and all(results) else "FAILED")
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
