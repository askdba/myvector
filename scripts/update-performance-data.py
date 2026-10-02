#!/usr/bin/env python3
"""Refresh docs/data/performance.json, the numbers on the docs Performance page.

  # Release block from a tag's CI results on the benchmarks branch:
  scripts/update-performance-data.py --release v1.27.0

  # Sweep block from a myvectorbench GloVe run (myvectorbench-glove.yml):
  scripts/update-performance-data.py --sweep glove-sweep.json

It only rewrites the JSON file. Review the diff and open a PR; the site
renders the page from the file (docs/hooks/performance.py).
"""

import argparse
import json
import re
import subprocess
import sys

DATA_FILE = "docs/data/performance.json"

# The cells the release table shows, in display order.
CELLS = [("8.4", "plugin"), ("8.4", "component"), ("9.7", "component"), ("26.7", "component")]


class UpdateError(Exception):
    pass


# ── release block ─────────────────────────────────────────────────────────────

def _newest_run(files, mysql, build, tag):
    # "<tag>-<YYYYMMDD>T<HHMMSS>.json" only, so v1.26.9 never matches v1.26.9-rc1-...
    pattern = re.compile(rf"^{re.escape(mysql)}/{re.escape(build)}/{re.escape(tag)}-(\d{{8}}T\d{{6}})\.json$")
    runs = [(m.group(1), f) for f in files for m in [pattern.match(f)] if m]
    return max(runs)[1] if runs else None


def release_block(tag, list_files, read_file):
    """Build the release block from the tag's newest run per cell."""
    files = list_files()
    cells, workload = [], None
    for mysql, build in CELLS:
        path = _newest_run(files, mysql, build, tag)
        if path is None:
            raise UpdateError(f"no {tag} result for MySQL {mysql} {build} on the benchmarks branch")
        result = json.loads(read_file(path))
        m = result["metrics"]
        recall = m.get("recall_at_10")
        cells.append({
            "mysql": mysql,
            "build": build,
            "index_build_s": round(m["index_build_time_s"], 2),
            "insert_qps": round(m["insert_qps"]),
            "recall_at_10": None if recall is None else round(recall, 3),
        })
        if workload is None:
            wp = result["workload_params"]
            workload = f"{result['dataset']}, {wp['rows']:,} rows × {wp['dim']} dimensions"
    return {"tag": tag, "workload": workload, "cells": cells}


# ── sweep block ───────────────────────────────────────────────────────────────

def sweep_block(result, existing):
    """Build the sweep block from a myvectorbench result.

    The build label comes from the result. Descriptive fields the result
    doesn't carry (dataset name, host, k) are kept from the existing block.
    """
    m = result["metrics"]
    points = m.get("ef_search_sweep") or []
    if not points:
        raise UpdateError("the result has no ef_search_sweep points (was ef_search_sweep configured?)")
    wp = result["workload_params"]
    ref = result["git_ref"]
    commit = ref.rsplit("-g", 1)[1] if "-g" in ref else ref
    return {
        "dataset": existing["dataset"],
        "indexed_rows": wp["rows"],
        "held_out_queries": wp.get("holdout_queries", 0),
        "distance": wp.get("distance", "L2"),
        "k": existing["k"],
        "build": f"{result['build_path']}, MySQL {result['mysql_version']}",
        "commit": commit,
        "measured": result["timestamp"][:10],
        "host": existing["host"],
        "index_build_s": round(m["index_build_time_s"], 1),
        "brute_force_qps": round(m["knn_qps"], 1),
        "points": [
            {"ef_search": p["ef_search"], "recall_at_10": round(p["recall_at_10"], 3),
             "qps": round(p["qps"]), "p50_ms": round(p["p50_ms"], 1), "p99_ms": round(p["p99_ms"], 1)}
            for p in points
        ],
    }


# ── file format ───────────────────────────────────────────────────────────────

def _compact(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(", ", ": "))


def dump(data):
    """Serialise in the committed layout: one line per sweep point and release cell."""
    lines = ["{"]
    for si, (section, block) in enumerate(data.items()):
        lines.append(f'  "{section}": {{')
        items = list(block.items())
        for i, (key, value) in enumerate(items):
            comma = "," if i < len(items) - 1 else ""
            if isinstance(value, list):
                lines.append(f'    "{key}": [')
                lines += [f"      {_compact(v)}{',' if j < len(value) - 1 else ''}"
                          for j, v in enumerate(value)]
                lines.append(f"    ]{comma}")
            else:
                lines.append(f'    "{key}": {_compact(value)}{comma}')
        lines.append("  }" + ("," if si < len(data) - 1 else ""))
    lines.append("}")
    return "\n".join(lines) + "\n"


# ── git access ────────────────────────────────────────────────────────────────

def _git(*args):
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise UpdateError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--release", metavar="TAG", help="refresh the release block from this tag's CI results")
    ap.add_argument("--sweep", metavar="RESULT_JSON", help="refresh the sweep block from a myvectorbench result")
    ap.add_argument("--branch", default="origin/benchmarks", help="ref holding CI results (default: %(default)s)")
    ap.add_argument("--data", default=DATA_FILE, help="data file to rewrite (default: %(default)s)")
    args = ap.parse_args(argv)
    if not (args.release or args.sweep):
        ap.error("give --release and/or --sweep")

    try:
        with open(args.data, encoding="utf-8") as f:
            data = json.load(f)
        if args.release:
            data["release"] = release_block(
                args.release,
                lambda: _git("ls-tree", "-r", "--name-only", args.branch).splitlines(),
                lambda path: _git("show", f"{args.branch}:{path}"),
            )
        if args.sweep:
            with open(args.sweep, encoding="utf-8") as f:
                data["sweep"] = sweep_block(json.load(f), data["sweep"])
    except (UpdateError, OSError, KeyError, json.JSONDecodeError) as e:
        print(f"update-performance-data: {e}", file=sys.stderr)
        return 1

    with open(args.data, "w", encoding="utf-8") as f:
        f.write(dump(data))
    print(f"Updated {args.data}. Review the diff, then open a PR.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
