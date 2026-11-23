#!/usr/bin/env python3
"""Markdown tables from the JSON files scripts/bench-innodb-storage.py writes.

Usage:
  python3 scripts/bench-innodb-storage-report.py results/rfc005-phase3

Prints one section per result file group: the storage comparison for each
tier (the median of the runs named <tier>-...-r1, -r2, ... where there are
several), and the chunk-size sweep.
"""

import glob
import json
import os
import re
import statistics
import sys


def mb(n):
    return f"{n / 1e6:,.0f} MB" if n < 1e10 else f"{n / 1e9:,.1f} GB"


def secs(ms):
    return f"{ms / 1000:,.1f} s"


def med(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def runs_of(files, storage):
    out = []
    for f in files:
        for r in json.load(open(f))["runs"]:
            if r["storage"] == storage:
                out.append(r)
    return out


def get(run, *path):
    for p in path:
        if run is None:
            return None
        run = run.get(p) if isinstance(run, dict) else None
    return run


def ckpt(run, change, n, key):
    for c in run.get("checkpoint", []):
        if c["change"] == change and c["n"] == n:
            return c.get(key)
    return None


def comparison(title, files):
    first = json.load(open(files[0]))
    f_runs, i_runs = runs_of(files, "file"), runs_of(files, "innodb")
    if not f_runs or not i_runs:
        return
    idx = f_runs[0]["index"]
    srv = f_runs[0]["server"]
    print(f"### {title}\n")
    print(f"{idx['rows']:,} x {idx['dim']}, {first['dataset']}; MySQL {srv['version']}, "
          f"{'defaults' if not srv['args'] else ' '.join(srv['args'])}; "
          f"{len(f_runs)} run{'s' if len(f_runs) > 1 else ''}"
          f"{' (median)' if len(f_runs) > 1 else ''}.\n")
    print("| | file | innodb |")
    print("|---|---|---|")

    def row(label, fn, fmt=lambda v: f"{v:,}"):
        a, b = med([fn(r) for r in f_runs]), med([fn(r) for r in i_runs])
        if a is None and b is None:
            return
        print(f"| {label} | {fmt(a) if a is not None else 'n/a'} | "
              f"{fmt(b) if b is not None else 'n/a'} |")

    row("Build and save", lambda r: get(r, "build", "build_s"), lambda v: f"{v:,.1f} s")
    row("of which the save", lambda r: get(r, "build", "save_ms"), secs)
    row("Stored size", lambda r: get(r, "build", "stored_bytes"), mb)
    row("Redo written by the build", lambda r: get(r, "build", "redo_bytes"), mb)
    row("Load of the stored index",
        lambda r: med(get(r, "load", "load_s") or []), lambda v: f"{v:,.2f} s")
    row("Queries/s, 1 session", lambda r: get(r, "search", "qps_1"))
    row("Queries/s, 8 sessions", lambda r: get(r, "search", "qps_8"))
    row("Recall@10", lambda r: get(r, "search", "recall_at_10"), lambda v: f"{v:.3f}")
    changes = [(c["change"], c["n"]) for c in f_runs[0].get("checkpoint", [])]
    for change, n in changes:
        what = f"{n:,} {'inserts' if change == 'insert' else 'vector updates'}"
        row(f"Checkpoint after {what}: time",
            lambda r: ckpt(r, change, n, "ckpt_ms"), secs)
        row(f"Checkpoint after {what}: bytes written by mysqld, change included",
            lambda r: ckpt(r, change, n, "write_bytes_total"), mb)
        row(f"Checkpoint after {what}: redo, change included",
            lambda r: ckpt(r, change, n, "redo_bytes"), mb)
    row("Insert to searchable under load, median",
        lambda r: get(r, "stall", "lag_ms_p50"), lambda v: f"{v:,.1f} ms")
    row("Insert to searchable under load, 99th percentile",
        lambda r: get(r, "stall", "lag_ms_p99"), lambda v: f"{v:,.0f} ms")
    row("Insert to searchable under load, worst",
        lambda r: get(r, "stall", "lag_ms_max"), lambda v: f"{v:,.0f} ms")
    row("Clean restart until loaded and caught up",
        lambda r: get(r, "restart", "clean_restart_s"), lambda v: f"{v:,.1f} s")
    row("Restart after kill -9, changes not checkpointed",
        lambda r: get(r, "restart", "kill_restart_s"), lambda v: f"{v:,.1f} s")
    print()
    # What the InnoDB checkpoints rewrote.
    r = i_runs[0]
    total_v, total_l = get(r, "build", "vector_chunks"), get(r, "build", "link_chunks")
    if total_v and r.get("checkpoint"):
        print(f"InnoDB checkpoints in detail (the build stored {total_v:,} vector chunks "
              f"and {total_l:,} link chunks; first run):\n")
        print("| Change | Checkpoints | Vector chunks | Link chunks | Upper rows | "
              "Chunk bytes |")
        print("|---|---|---|---|---|---|")
        for c in r["checkpoint"]:
            print(f"| {c['n']:,} {'inserts' if c['change'] == 'insert' else 'updates'} | "
                  f"{c['checkpoints']} | {c['vector_chunks']:,} | {c['link_chunks']:,} | "
                  f"{c['upper_rows']:,} | {mb(c['ckpt_bytes'])} |")
        print()


def sweep(title, files):
    rows = []
    for f in files:
        d = json.load(open(f))
        r = d["runs"][0]
        rows.append((d["settings"]["vec_chunk"], d["settings"]["lnk_chunk"], r))
    if not rows:
        return
    rows.sort(key=lambda x: (x[0], x[1]))
    changes = [(c["change"], c["n"]) for c in rows[0][2].get("checkpoint", [])]
    print(f"### {title}\n")
    head = ["Vector chunk", "Link chunk", "Save", "Load"]
    for change, n in changes:
        head.append(f"{n:,} {'ins' if change == 'insert' else 'upd'}: time / chunk bytes")
    print("| " + " | ".join(head) + " |")
    print("|" + "---|" * len(head))
    for v, l, r in rows:
        cells = [mb(v), f"{l // 1024:,} KB", secs(get(r, "build", "save_ms") or 0),
                 f"{med(get(r, 'load', 'load_s') or [0]):,.2f} s"]
        for change, n in changes:
            cells.append(f"{secs(ckpt(r, change, n, 'ckpt_ms') or 0)} / "
                         f"{mb(ckpt(r, change, n, 'ckpt_bytes') or 0)}")
        print("| " + " | ".join(cells) + " |")
    print()


def main():
    d = sys.argv[1] if len(sys.argv) > 1 else "results/rfc005-phase3"
    files = sorted(glob.glob(os.path.join(d, "*.json")))
    groups = {}
    for f in files:
        name = os.path.basename(f)[:-5]
        if name.startswith("sweep-"):
            groups.setdefault("sweep-" + name.split("-")[1], []).append(f)
        else:
            groups.setdefault(re.sub(r"-r\d+$", "", name), []).append(f)
    for name in sorted(groups):
        if name.startswith("sweep-"):
            sweep(f"Chunk-size sweep, tier {name[6:]}", groups[name])
        else:
            comparison(name, groups[name])
    return 0


if __name__ == "__main__":
    sys.exit(main())
