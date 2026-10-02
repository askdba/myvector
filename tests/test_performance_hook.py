import copy
import importlib.util
import json
import os
import re

import pytest


def _load_hook():
    path = os.path.join(os.path.dirname(__file__), '..', 'docs', 'hooks', 'performance.py')
    spec = importlib.util.spec_from_file_location("performance_hook", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = _load_hook()

DATA_FILE = os.path.join(os.path.dirname(__file__), '..', 'docs', 'data', 'performance.json')


def sample_data():
    return {
        "sweep": {
            "dataset": "GloVe 6B 50d",
            "indexed_rows": 100000,
            "held_out_queries": 1000,
            "distance": "Cosine",
            "k": 10,
            "build": "plugin, MySQL 8.4",
            "commit": "4b0789f",
            "measured": "2026-09-29",
            "host": "8-core Arm Neoverse-N1 (aarch64), 46 GB RAM",
            "index_build_s": 36.4,
            "brute_force_qps": 12.7,
            "points": [
                {"ef_search": 10, "recall_at_10": 0.809, "qps": 1204, "p50_ms": 0.8, "p99_ms": 1.0},
                {"ef_search": 20, "recall_at_10": 0.907, "qps": 1124, "p50_ms": 0.9, "p99_ms": 1.1},
                {"ef_search": 50, "recall_at_10": 0.974, "qps": 1002, "p50_ms": 1.0, "p99_ms": 1.2},
                {"ef_search": 100, "recall_at_10": 0.992, "qps": 832, "p50_ms": 1.2, "p99_ms": 1.4},
                {"ef_search": 200, "recall_at_10": 0.999, "qps": 612, "p50_ms": 1.7, "p99_ms": 1.9},
                {"ef_search": 400, "recall_at_10": 1.000, "qps": 481, "p50_ms": 2.1, "p99_ms": 2.6},
            ],
        },
        "release": {
            "tag": "v1.26.9",
            "workload": "synthetic, 10,000 rows × 128 dimensions",
            "cells": [
                {"mysql": "8.4", "build": "plugin", "index_build_s": 2.95, "insert_qps": 3707, "recall_at_10": 0.978},
                {"mysql": "8.4", "build": "component", "index_build_s": 2.67, "insert_qps": 3942, "recall_at_10": None},
                {"mysql": "9.7", "build": "component", "index_build_s": 2.64, "insert_qps": 3916, "recall_at_10": None},
                {"mysql": "26.7", "build": "component", "index_build_s": 2.53, "insert_qps": 4062, "recall_at_10": None},
            ],
        },
    }


def _write(tmp_path, obj):
    p = tmp_path / "performance.json"
    p.write_text(obj if isinstance(obj, str) else json.dumps(obj))
    return str(p)


# ── Task 1: loading and validation ────────────────────────────────────────────

def test_committed_data_file_matches_the_spec_sample():
    assert hook.load_data(DATA_FILE) == sample_data()


def test_valid_sample_loads(tmp_path):
    assert hook.load_data(_write(tmp_path, sample_data())) == sample_data()


def test_missing_file_names_the_file(tmp_path):
    missing = str(tmp_path / "nope.json")
    with pytest.raises(hook.PerformanceDataError, match="nope.json"):
        hook.load_data(missing)


def test_invalid_json_is_rejected(tmp_path):
    with pytest.raises(hook.PerformanceDataError, match="JSON"):
        hook.load_data(_write(tmp_path, "{not json"))


def test_missing_sweep_points_names_the_field(tmp_path):
    d = sample_data()
    del d["sweep"]["points"]
    with pytest.raises(hook.PerformanceDataError, match=r"sweep\.points"):
        hook.load_data(_write(tmp_path, d))


def test_non_numeric_qps_names_the_field(tmp_path):
    d = sample_data()
    d["sweep"]["points"][2]["qps"] = "fast"
    with pytest.raises(hook.PerformanceDataError, match=r"sweep\.points\[2\]\.qps"):
        hook.load_data(_write(tmp_path, d))


def test_release_cell_missing_insert_qps_names_the_field(tmp_path):
    d = sample_data()
    del d["release"]["cells"][1]["insert_qps"]
    with pytest.raises(hook.PerformanceDataError, match=r"release\.cells\[1\]\.insert_qps"):
        hook.load_data(_write(tmp_path, d))


def test_null_recall_allowed_in_release_cells_only(tmp_path):
    hook.load_data(_write(tmp_path, sample_data()))  # release cells carry nulls
    d = sample_data()
    d["sweep"]["points"][0]["recall_at_10"] = None
    with pytest.raises(hook.PerformanceDataError, match=r"sweep\.points\[0\]\.recall_at_10"):
        hook.load_data(_write(tmp_path, d))


# ── Task 2: tables ────────────────────────────────────────────────────────────

def test_sweep_table_header_and_formatting():
    t = hook.render_sweep_table(sample_data())
    lines = t.strip().splitlines()
    assert lines[0] == "| ef_search | recall@10 | QPS | p50 ms | p99 ms |"
    assert re.fullmatch(r"\|(\s*-+:?\s*\|){5}", lines[1])
    assert "| 10 | 0.809 | 1,204 | 0.8 | 1.0 |" in lines
    assert "| 400 | 1.000 | 481 | 2.1 | 2.6 |" in lines  # recall always 3 decimals
    assert len(lines) == 2 + 6


def test_release_table_rows_and_null_recall():
    t = hook.render_release_table(sample_data())
    lines = t.strip().splitlines()
    assert lines[0] == "| MySQL / build | Index build | Insert QPS | recall@10 |"
    assert "| 8.4 plugin | 2.95 s | 3,707 | 0.978 |" in lines
    assert "| 26.7 component | 2.53 s | 4,062 | — |" in lines
    assert len(lines) == 2 + 4


# ── Task 3: headline figures ──────────────────────────────────────────────────

def test_headline_figures_are_derived_from_the_data():
    h = hook.render_headline(sample_data())
    assert 'class="perf-headline"' in h
    assert h.count('class="perf-figure"') == 3
    assert "0.992" in h and "recall@10 at ef_search 100" in h
    assert "~38×" in h  # 481 / 12.7 = 37.9
    assert "36.4 s" in h and "100,000 rows" in h


def test_headline_needs_an_ef_search_100_point():
    d = sample_data()
    d["sweep"]["points"] = [p for p in d["sweep"]["points"] if p["ef_search"] != 100]
    with pytest.raises(hook.PerformanceDataError, match="ef_search 100"):
        hook.render_headline(d)


# ── Task 4: chart ─────────────────────────────────────────────────────────────

def _nice(step):
    m = step / 10 ** __import__("math").floor(__import__("math").log10(step))
    return round(m, 6) in (1, 2, 5)


def test_chart_scale_brackets_the_data_with_nice_steps():
    s = hook.chart_scale(sample_data()["sweep"]["points"])
    xt, yt = s["x_ticks"], s["y_ticks"]
    assert xt[0] <= 481 and xt[-1] >= 1204
    steps = {round(b - a, 6) for a, b in zip(xt, xt[1:])}
    assert len(steps) == 1 and _nice(steps.pop())
    assert (s["x_min"], s["x_max"]) == (xt[0], xt[-1])
    assert yt[0] == pytest.approx(0.80) and yt[-1] == pytest.approx(1.00)
    assert all(b - a == pytest.approx(0.05) for a, b in zip(yt, yt[1:]))


def _svg():
    return hook.render_sweep_chart(sample_data())


def test_chart_points_sit_where_the_scale_puts_them():
    d = sample_data()
    s = hook.chart_scale(d["sweep"]["points"])
    g = hook.CHART
    circles = re.findall(r'<circle cx="([\d.]+)" cy="([\d.]+)"', _svg())
    assert len(circles) == 6
    for (cx, cy), p in zip(circles, d["sweep"]["points"]):
        ex = g["left"] + (p["qps"] - s["x_min"]) / (s["x_max"] - s["x_min"]) * (g["right"] - g["left"])
        ey = g["bottom"] - (p["recall_at_10"] - s["y_min"]) / (s["y_max"] - s["y_min"]) * (g["bottom"] - g["top"])
        assert float(cx) == pytest.approx(ex, abs=0.5)
        assert float(cy) == pytest.approx(ey, abs=0.5)


def test_chart_tick_labels_lie_on_the_axes():
    d = sample_data()
    s = hook.chart_scale(d["sweep"]["points"])
    svg = _svg()
    xs = [int(t.replace(",", "")) for t in re.findall(r'<text class="perf-tick perf-x"[^>]*>([\d,]+)</text>', svg)]
    ys = [float(t) for t in re.findall(r'<text class="perf-tick perf-y"[^>]*>([\d.]+)</text>', svg)]
    assert xs and ys
    assert all(s["x_min"] <= x <= s["x_max"] for x in xs)
    assert all(s["y_min"] - 1e-9 <= y <= s["y_max"] + 1e-9 for y in ys)


def test_chart_labels_every_point_and_is_accessible():
    svg = _svg()
    for ef in (10, 20, 50, 100, 200, 400):
        assert f">ef {ef}</text>" in svg
    assert 'role="img"' in svg and 'aria-label="' in svg
    assert "<script" not in svg
    vb = [float(v) for v in re.search(r'viewBox="([\d. ]+)"', svg).group(1).split()]
    assert vb[2] > hook.CHART["right"] and vb[3] > hook.CHART["bottom"]


def test_point_labels_stay_inside_the_plot_area():
    g = hook.CHART
    for y in re.findall(r'<text class="perf-point-label" x="[\d.]+" y="([\d.]+)"', _svg()):
        assert g["top"] <= float(y) <= g["bottom"] - 2


# ── Task 5: markers and the hook entry point ──────────────────────────────────

PAGE = """# Performance

<!-- perf:headline -->

## Recall vs throughput

<!-- perf:sweep-chart -->

<!-- perf:sweep-table -->

## Release

<!-- perf:release-table -->
"""


def test_render_page_replaces_every_marker():
    out = hook.render_page(PAGE, sample_data())
    assert "<!-- perf:" not in out
    assert 'class="perf-headline"' in out and "<svg" in out
    assert "| 10 | 0.809 |" in out and "| 8.4 plugin |" in out


def test_unknown_marker_fails():
    with pytest.raises(hook.PerformanceDataError, match="perf:nope"):
        hook.render_page(PAGE + "\n<!-- perf:nope -->\n", sample_data())


def test_page_without_markers_is_unchanged():
    text = "# Something else\n\nNo markers here.\n"
    assert hook.render_page(text, sample_data()) == text


class _File:
    def __init__(self, src_uri):
        self.src_uri = src_uri


class _Page:
    def __init__(self, src_uri):
        self.file = _File(src_uri)


def _docs_dir(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "performance.json").write_text(json.dumps(sample_data()))
    return str(tmp_path)


def test_hook_only_touches_the_performance_page(tmp_path):
    config = {"docs_dir": _docs_dir(tmp_path)}
    other = "# Usage\n\n<!-- perf:headline -->\n"
    assert hook.on_page_markdown(other, page=_Page("usage.md"), config=config, files=None) == other
    out = hook.on_page_markdown(PAGE, page=_Page("PERFORMANCE.md"), config=config, files=None)
    assert "<!-- perf:" not in out and "0.992" in out


def test_hook_reads_data_from_docs_dir(tmp_path):
    config = {"docs_dir": str(tmp_path)}  # no data/performance.json there
    with pytest.raises(hook.PerformanceDataError, match="performance.json"):
        hook.on_page_markdown(PAGE, page=_Page("PERFORMANCE.md"), config=config, files=None)
