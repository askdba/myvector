#!/usr/bin/env python3
"""Compare myvectorbench result JSON files against a baseline and check thresholds.

Usage:
  ./scripts/myvectorbench-compare.py baseline.json current.json [--config myvectorbench.yml]

Exit 0: all metrics within threshold (or no baseline).
Exit 1: one or more metrics exceeded threshold.
"""

import argparse
import json
import sys

import yaml


def parse_threshold(value: str):
    """Parse a threshold string into (mode, limit).

    '+25%'  → ('percent_upper',  0.25)   breach if pct_delta > 0.25
    '-25%'  → ('percent_lower', -0.25)   breach if pct_delta < -0.25
    '-0.05' → ('absolute',      -0.05)   breach if delta < -0.05
    '+0.05' → ('absolute',       0.05)   breach if delta > 0.05
    """
    value = str(value).strip()
    if value.endswith('%'):
        pct = float(value[:-1]) / 100.0
        return ('percent_upper', pct) if pct >= 0 else ('percent_lower', pct)
    n = float(value)
    return ('absolute', n)


def check_threshold(baseline: float, current: float, threshold_str: str):
    """Return (breached: bool, delta_display: float).

    delta_display is pct_delta for percent thresholds, raw delta for absolute.
    """
    mode, limit = parse_threshold(threshold_str)
    if baseline == 0:
        return False, 0.0
    pct_delta = (current - baseline) / baseline
    delta = current - baseline
    if mode == 'percent_upper':
        return pct_delta > limit, pct_delta
    if mode == 'percent_lower':
        return pct_delta < limit, pct_delta
    return (delta < limit if limit < 0 else delta > limit), delta


def format_delta(delta: float, mode: str) -> str:
    sign = '+' if delta >= 0 else ''
    if mode.startswith('percent'):
        return f"{sign}{delta * 100:.1f}%"
    return f"{sign}{delta:.4f}"


def compare(baseline_path: str, current_path: str, config_path: str) -> int:
    with open(config_path) as f:
        config = yaml.safe_load(f)
    thresholds = config.get('thresholds', {})

    try:
        with open(baseline_path) as f:
            baseline = json.load(f)
    except FileNotFoundError:
        print(
            f"WARNING: no baseline at {baseline_path} — NO_BASELINE, skipping comparison",
            file=sys.stderr,
        )
        return 0

    with open(current_path) as f:
        current = json.load(f)

    bm = baseline.get('metrics', {})
    cm = current.get('metrics', {})

    print(
        f"## myvectorbench — {current.get('git_ref', '?')} · "
        f"mysql:{current.get('mysql_version', '?')} · {current.get('build_path', '?')}"
    )
    print()
    print(f"| {'Metric':<22} | {'Baseline':>10} | {'Current':>10} | {'Delta':>9} | Status |")
    print(f"|{'-'*24}|{'-'*12}|{'-'*12}|{'-'*11}|--------|")

    any_breach = False
    for metric, threshold_str in thresholds.items():
        bval = bm.get(metric)
        cval = cm.get(metric)
        if bval is None or cval is None:
            print(f"| {metric:<22} | {'N/A':>10} | {'N/A':>10} | {'N/A':>9} | ⚠️   |")
            continue
        mode, _ = parse_threshold(threshold_str)
        breached, delta = check_threshold(float(bval), float(cval), threshold_str)
        delta_str = format_delta(delta, mode)
        status = "❌" if breached else "✅"
        if breached:
            any_breach = True
        print(f"| {metric:<22} | {bval:>10.4g} | {cval:>10.4g} | {delta_str:>9} | {status}   |")

    print()
    if any_breach:
        print("FAIL: one or more metrics exceeded threshold.")
        return 1
    print("PASS: all metrics within threshold.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Compare myvectorbench JSON results")
    parser.add_argument("baseline", help="Path to baseline JSON")
    parser.add_argument("current", help="Path to current result JSON")
    parser.add_argument("--config", default="myvectorbench.yml", help="Config YAML path")
    args = parser.parse_args()
    sys.exit(compare(args.baseline, args.current, args.config))


if __name__ == "__main__":
    main()
