import importlib.util
import json
import os

import pytest


def _load(name, *parts):
    path = os.path.join(os.path.dirname(__file__), '..', *parts)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


upd = _load("update_performance_data", "scripts", "update-performance-data.py")

DATA_FILE = os.path.join(os.path.dirname(__file__), '..', 'docs', 'data', 'performance.json')


def _result(mysql, build, build_s, insert_qps, recall, ts="2026-09-22T10:31:54Z"):
    return {
        "git_ref": "v1.26.9", "mysql_version": mysql, "build_path": build,
        "timestamp": ts, "dataset": "synthetic",
        "workload_params": {"rows": 10000, "dim": 128},
        "metrics": {"index_build_time_s": build_s, "insert_qps": insert_qps,
                    "recall_at_10": recall},
    }


BRANCH = {
    "8.4/plugin/v1.26.9-20260922T103313.json": _result("8.4", "plugin", 2.946380138397217, 3707.312864981214, 0.9777777777777775),
    # an older run of the same tag: the newest timestamp must win
    "8.4/plugin/v1.26.9-20260921T090000.json": _result("8.4", "plugin", 9.0, 1.0, 0.1),
    # an RC of the same version: must not match tag v1.26.9
    "8.4/plugin/v1.26.9-rc1-20260930T000000.json": _result("8.4", "plugin", 8.0, 2.0, 0.2),
    "8.4/component/v1.26.9-20260922T103316.json": _result("8.4", "component", 2.6712, 3941.6, None),
    "9.7/component/v1.26.9-20260922T103314.json": _result("9.7", "component", 2.6401, 3916.2, None),
    "26.7/component/v1.26.9-20260922T103322.json": _result("26.7", "component", 2.5299, 4061.9, None),
}


def _reader():
    return (lambda: sorted(BRANCH)), (lambda path: json.dumps(BRANCH[path]))


def test_release_block_matches_the_published_data():
    list_files, read_file = _reader()
    block = upd.release_block("v1.26.9", list_files, read_file)
    with open(DATA_FILE, encoding="utf-8") as f:
        assert block == json.load(f)["release"]


def test_release_ignores_older_runs_and_release_candidates():
    list_files, read_file = _reader()
    cell = upd.release_block("v1.26.9", list_files, read_file)["cells"][0]
    assert cell["index_build_s"] == 2.95  # not 9.0 (older run) or 8.0 (rc1)


def test_unknown_tag_is_an_error():
    list_files, read_file = _reader()
    with pytest.raises(upd.UpdateError, match="v9.9.9"):
        upd.release_block("v9.9.9", list_files, read_file)


def _sweep_result():
    raw = [
        (10, 0.8086, 1204.3, 0.82, 0.96), (20, 0.9069, 1124.1, 0.87, 1.06),
        (50, 0.9736, 1001.8, 0.99, 1.17), (100, 0.9923, 832.4, 1.21, 1.38),
        (200, 0.9985, 612.2, 1.67, 1.94), (400, 0.9998, 480.6, 2.12, 2.64),
    ]
    return {
        "git_ref": "v1.26.9-21-g4b0789f", "mysql_version": "8.4", "build_path": "plugin",
        "timestamp": "2026-09-29T19:31:02Z", "dataset": "glove50",
        "workload_params": {"rows": 100000, "dim": 50, "holdout_queries": 1000,
                            "distance": "Cosine"},
        "metrics": {
            "index_build_time_s": 36.37118911743164, "knn_qps": 12.701355380684705,
            "ef_search_sweep": [
                {"ef_search": e, "recall_at_10": r, "qps": q, "p50_ms": a, "p99_ms": b}
                for e, r, q, a, b in raw
            ],
        },
    }


def test_sweep_block_matches_the_published_data():
    with open(DATA_FILE, encoding="utf-8") as f:
        existing = json.load(f)["sweep"]
    assert upd.sweep_block(_sweep_result(), existing) == existing


def test_sweep_needs_sweep_points():
    r = _sweep_result()
    r["metrics"]["ef_search_sweep"] = []
    with pytest.raises(upd.UpdateError, match="ef_search_sweep"):
        upd.sweep_block(r, {"build": "x", "host": "y", "dataset": "z", "k": 10})


def test_dump_reproduces_the_committed_file_byte_for_byte():
    with open(DATA_FILE, encoding="utf-8") as f:
        text = f.read()
    assert upd.dump(json.loads(text)) == text
