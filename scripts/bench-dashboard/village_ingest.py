#!/usr/bin/env python3
"""Convert a vector-dev-bench `recall_bench.py` stdout log into a dashboard
result JSON (the same schema generate_page.py reads), so a cross-engine village
run folds into bench.myvector.online.

recall_bench prints a header line, a `build_time_s=...` line, and an ef_search
sweep table (`ef_search  recall@10  qps`). We don't get insert/exact-KNN/p50/p99
from it, so those fields are simply absent (rendered as "—"); the ANN QPS and
recall@10 cards come from the warm sweep point via the page's `headline_ef`.

Usage:
  village_ingest.py <recall_bench.log> <version-key> <label> <out.json>
"""
import json
import re
import sys
from datetime import datetime, timezone


def parse(log_text):
    build_s = None
    dim = rows = None
    m = re.search(r"build_time_s=([0-9.]+)", log_text)
    if m:
        build_s = float(m.group(1))
    h = re.search(r"\bdim=(\d+)\b.*?\bn=(\d+)\b", log_text)
    if h:
        dim, rows = int(h.group(1)), int(h.group(2))
    sweep = []
    in_table = False
    for line in log_text.splitlines():
        if re.match(r"\s*ef_search\s+recall", line):
            in_table = True
            continue
        if in_table:
            parts = line.split()
            if len(parts) >= 3 and re.match(r"^\d+$", parts[0]):
                sweep.append({
                    "ef_search": int(parts[0]),
                    "recall_at_10": float(parts[1]),
                    "qps": float(parts[2]),
                })
            elif parts:  # table ended at a non-data line
                break
    return build_s, dim, rows, sweep


def main():
    log_path, version, label, out_path = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    build_s, dim, rows, sweep = parse(open(log_path).read())
    if not sweep:
        print(f"WARNING: no sweep rows parsed from {log_path}", file=sys.stderr)
    result = {
        "version": version,
        "label": label,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dataset": "fashion-mnist-784",
        "rows_indexed": rows,
        "recall_ground_truth": "published",
        "harness": "vector-dev-bench (recall_bench)",
        "workload": {"rows": rows, "dim": dim, "distance": "L2"},
        "metrics": {
            "index_build_time_s": build_s,
            "ef_search_sweep": sweep,
        },
    }
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    pt = sweep[len(sweep) // 2] if sweep else {}
    print(f"wrote {out_path}: {version} build={build_s}s, {len(sweep)} sweep pts "
          f"(mid ef={pt.get('ef_search')} recall={pt.get('recall_at_10')} qps={pt.get('qps')})")


if __name__ == "__main__":
    main()
