"""MkDocs hook: render the Performance page from docs/data/performance.json.

docs/PERFORMANCE.md holds the page text plus markers such as
<!-- perf:sweep-table -->. At build time this hook replaces each marker with
tables, headline figures or an SVG chart built from the data file, so every
number on the page comes from one reviewed file. Bad or missing data raises,
which fails `mkdocs build --strict` before anything is published.

See docs/superpowers/specs/2026-10-02-performance-page-design.md.
"""

import json
import re
import os

try:
    from mkdocs.exceptions import PluginError as _BuildError
except ImportError:  # the unit tests don't need mkdocs installed
    _BuildError = Exception


class PerformanceDataError(_BuildError):
    """The performance data file is missing, malformed or incomplete."""


# ── loading and validation ────────────────────────────────────────────────────

_NUMBER = (int, float)

_SWEEP_FIELDS = {
    "dataset": str, "indexed_rows": int, "held_out_queries": int, "distance": str,
    "k": int, "build": str, "commit": str, "measured": str, "host": str,
    "index_build_s": _NUMBER, "brute_force_qps": _NUMBER, "points": list,
}
_POINT_FIELDS = {
    "ef_search": int, "recall_at_10": _NUMBER, "qps": _NUMBER,
    "p50_ms": _NUMBER, "p99_ms": _NUMBER,
}
_RELEASE_FIELDS = {"tag": str, "workload": str, "cells": list}
_CELL_FIELDS = {
    "mysql": str, "build": str, "index_build_s": _NUMBER, "insert_qps": _NUMBER,
    "recall_at_10": _NUMBER,
}
_NULLABLE = {"release.cells.recall_at_10"}


def _check(obj, fields, where, path, generic):
    if not isinstance(obj, dict):
        raise PerformanceDataError(f"{path}: {where} must be an object")
    for name, kind in fields.items():
        field = f"{where}.{name}"
        if name not in obj:
            raise PerformanceDataError(f"{path}: missing field {field}")
        value = obj[name]
        if value is None and f"{generic}.{name}" in _NULLABLE:
            continue
        # bool is an int subclass; never accept it as a number
        if isinstance(value, bool) or not isinstance(value, kind):
            raise PerformanceDataError(f"{path}: field {field} has the wrong type ({value!r})")


def load_data(path):
    """Parse and validate the performance data file; raise PerformanceDataError."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise PerformanceDataError(f"cannot read performance data {path}: {e}") from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise PerformanceDataError(f"{path}: invalid JSON ({e})") from None

    if not isinstance(data, dict):
        raise PerformanceDataError(f"{path}: top level must be an object")
    for section in ("sweep", "release"):
        if section not in data:
            raise PerformanceDataError(f"{path}: missing field {section}")

    _check(data["sweep"], _SWEEP_FIELDS, "sweep", path, "sweep")
    if not data["sweep"]["points"]:
        raise PerformanceDataError(f"{path}: sweep.points is empty")
    for i, point in enumerate(data["sweep"]["points"]):
        _check(point, _POINT_FIELDS, f"sweep.points[{i}]", path, "sweep.points")

    _check(data["release"], _RELEASE_FIELDS, "release", path, "release")
    if not data["release"]["cells"]:
        raise PerformanceDataError(f"{path}: release.cells is empty")
    for i, cell in enumerate(data["release"]["cells"]):
        _check(cell, _CELL_FIELDS, f"release.cells[{i}]", path, "release.cells")
    return data


# ── tables ────────────────────────────────────────────────────────────────────

def _int(n):
    return f"{round(n):,}"


def _recall(r):
    return "—" if r is None else f"{r:.3f}"


def _table(header, rows):
    lines = ["| " + " | ".join(header) + " |",
             "|" + "|".join("-" * (len(h) + 2) for h in header) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines) + "\n"


def render_sweep_table(data):
    rows = [[str(p["ef_search"]), _recall(p["recall_at_10"]), _int(p["qps"]),
             f"{p['p50_ms']:.1f}", f"{p['p99_ms']:.1f}"]
            for p in data["sweep"]["points"]]
    return _table(["ef_search", "recall@10", "QPS", "p50 ms", "p99 ms"], rows)


def render_release_table(data):
    rows = [[f"{c['mysql']} {c['build']}", f"{c['index_build_s']:.2f} s",
             _int(c["insert_qps"]), _recall(c["recall_at_10"])]
            for c in data["release"]["cells"]]
    return _table(["MySQL / build", "Index build", "Insert QPS", "recall@10"], rows)


# ── headline figures ──────────────────────────────────────────────────────────

def render_headline(data):
    sweep = data["sweep"]
    points = sweep["points"]
    at_100 = [p for p in points if p["ef_search"] == 100]
    if not at_100:
        raise PerformanceDataError(
            "performance data: sweep.points has no ef_search 100 point, needed for the headline")
    top = max(points, key=lambda p: p["ef_search"])
    speedup = top["qps"] / sweep["brute_force_qps"]
    figures = [
        (f"{at_100[0]['recall_at_10']:.3f}", "recall@10 at ef_search 100"),
        (f"~{round(speedup)}×",
         f"ANN vs brute-force QPS at ef_search {top['ef_search']}"),
        (f"{sweep['index_build_s']:.1f} s",
         f"HNSW index build, {_int(sweep['indexed_rows'])} rows"),
    ]
    items = "\n".join(
        f'<div class="perf-figure"><span class="perf-value">{v}</span>'
        f'<span class="perf-label">{label}</span></div>'
        for v, label in figures)
    return f'<div class="perf-headline">\n{items}\n</div>\n'


# ── chart ─────────────────────────────────────────────────────────────────────

# Plot area inside the SVG viewBox; the margins hold the tick and axis labels.
CHART = {"width": 640, "height": 300, "left": 56, "right": 616, "top": 20, "bottom": 244}


def _nice_step(span, target_ticks=5):
    import math
    raw = span / target_ticks
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def chart_scale(points):
    """Axis ranges and tick values: QPS on x, recall@10 on y."""
    import math
    qps = [p["qps"] for p in points]
    step = _nice_step(max(qps) - min(qps) or max(qps))
    x_min = math.floor(min(qps) / step) * step
    x_max = math.ceil(max(qps) / step) * step
    x_ticks = [x_min + i * step for i in range(round((x_max - x_min) / step) + 1)]

    lowest = min(p["recall_at_10"] for p in points)
    y_step = 0.05 if lowest >= 0.75 else 0.1
    y_min = round(math.floor(lowest / y_step + 1e-9) * y_step, 2)
    y_max = 1.0
    y_ticks = [round(y_min + i * y_step, 2) for i in range(round((y_max - y_min) / y_step) + 1)]
    return {"x_min": x_min, "x_max": x_max, "x_ticks": x_ticks,
            "y_min": y_min, "y_max": y_max, "y_ticks": y_ticks}


def _xy(s, qps, recall):
    g = CHART
    x = g["left"] + (qps - s["x_min"]) / (s["x_max"] - s["x_min"]) * (g["right"] - g["left"])
    y = g["bottom"] - (recall - s["y_min"]) / (s["y_max"] - s["y_min"]) * (g["bottom"] - g["top"])
    return round(x, 1), round(y, 1)


def render_sweep_chart(data):
    g = CHART
    points = data["sweep"]["points"]
    s = chart_scale(points)
    out = [f'<svg class="perf-chart" viewBox="0 0 {g["width"]} {g["height"]}" role="img" '
           f'aria-label="recall@10 against queries per second for ef_search '
           f'{points[0]["ef_search"]} to {points[-1]["ef_search"]}">']
    for v in s["y_ticks"]:
        _, y = _xy(s, s["x_min"], v)
        out.append(f'<line class="perf-grid" x1="{g["left"]}" y1="{y}" x2="{g["right"]}" y2="{y}"/>')
        out.append(f'<text class="perf-tick perf-y" x="{g["left"] - 8}" y="{y + 4}" '
                   f'text-anchor="end">{v:.2f}</text>')
    for v in s["x_ticks"]:
        x, _ = _xy(s, v, s["y_min"])
        out.append(f'<line class="perf-grid" x1="{x}" y1="{g["bottom"]}" x2="{x}" y2="{g["bottom"] + 5}"/>')
        out.append(f'<text class="perf-tick perf-x" x="{x}" y="{g["bottom"] + 20}" '
                   f'text-anchor="middle">{_int(v)}</text>')
    mid_x = (g["left"] + g["right"]) / 2
    out.append(f'<text class="perf-axis" x="{mid_x}" y="{g["height"] - 6}" '
               f'text-anchor="middle">QPS (provisional)</text>')
    out.append(f'<text class="perf-axis" x="14" y="{(g["top"] + g["bottom"]) / 2}" text-anchor="middle" '
               f'transform="rotate(-90 14 {(g["top"] + g["bottom"]) / 2})">recall@10</text>')
    coords = [_xy(s, p["qps"], p["recall_at_10"]) for p in points]
    out.append('<polyline class="perf-line" points="'
               + " ".join(f"{x},{y}" for x, y in coords) + '"/>')
    for (x, y), p in zip(coords, points):
        out.append(f'<circle cx="{x}" cy="{y}" r="4" class="perf-point"/>')
        # The curve falls to the right, so label below-left of each point, clear of
        # the line; near the x-axis, label above-right instead, inside the plot.
        if y + 14 <= g["bottom"] - 2:
            label = f'x="{x - 8}" y="{y + 14}" text-anchor="end"'
        else:
            label = f'x="{x + 8}" y="{y - 8}"'
        out.append(f'<text class="perf-point-label" {label}>ef {p["ef_search"]}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


# ── summary sentences ─────────────────────────────────────────────────────────

def render_sweep_summary(data):
    s = data["sweep"]
    return (f"Measured on MyVector ({s['build']}) with {_int(s['indexed_rows'])} "
            f"{s['dataset']} vectors and {_int(s['held_out_queries'])} queries that are not in "
            f"the index ({s['distance']} distance, k = {s['k']}), on {s['measured']} at commit "
            f"`{s['commit']}`. Host: {s['host']}.\n")


def render_release_summary(data):
    r = data["release"]
    return (f"Release {r['tag']} in CI ({r['workload']}, GitHub-hosted runners): index build "
            f"and insert throughput for each supported MySQL version and build.\n")


# ── page assembly ─────────────────────────────────────────────────────────────

PAGE = "PERFORMANCE.md"
_RENDERERS = {
    "sweep-summary": render_sweep_summary,
    "release-summary": render_release_summary,
    "headline": render_headline,
    "sweep-chart": render_sweep_chart,
    "sweep-table": render_sweep_table,
    "release-table": render_release_table,
}
_MARKER = re.compile(r"<!--\s*perf:([\w-]+)\s*-->")


def render_page(markdown, data):
    """Replace each <!-- perf:NAME --> marker; fail on an unknown or leftover one."""
    def replace(m):
        name = m.group(1)
        if name not in _RENDERERS:
            raise PerformanceDataError(f"{PAGE}: unknown marker perf:{name}")
        return _RENDERERS[name](data)

    out = _MARKER.sub(replace, markdown)
    leftover = _MARKER.search(out)
    if leftover:
        raise PerformanceDataError(f"{PAGE}: marker perf:{leftover.group(1)} was not replaced")
    return out


def on_page_markdown(markdown, page, config, files):
    if page.file.src_uri != PAGE:
        return markdown
    data = load_data(os.path.join(config["docs_dir"], "data", "performance.json"))
    return render_page(markdown, data)
