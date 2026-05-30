"""Unit tests for bench-concurrent-stress.py — no Docker required."""
import importlib.util
import os
import pytest


def _load():
    path = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'bench-concurrent-stress.py')
    spec = importlib.util.spec_from_file_location("bench_stress", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_import():
    mod = _load()
    assert hasattr(mod, '_aggregate')
    assert hasattr(mod, '_evaluate_pass')
    assert hasattr(mod, '_build_result')
    assert hasattr(mod, 'run_stress')


def test_aggregate_basic():
    mod = _load()
    results = [
        {"queries": 100, "errors": 0, "latencies_ms": [5.0, 10.0, 8.0]},
        {"queries": 200, "errors": 0, "latencies_ms": [3.0, 7.0]},
    ]
    agg = mod._aggregate(results, duration_s=10.0)
    assert agg["threads"] == 2
    assert agg["qps"] == 30.0
    assert agg["errors"] == 0
    assert "p50_ms" in agg
    assert "p99_ms" in agg


def test_aggregate_with_errors():
    mod = _load()
    results = [
        {"queries": 50, "errors": 2, "latencies_ms": []},
        {"ops": 80, "errors": 1, "latencies_ms": []},
    ]
    agg = mod._aggregate(results, duration_s=5.0)
    assert agg["errors"] == 3
    assert agg["qps"] == 26.0


def test_evaluate_pass_all_good():
    mod = _load()
    pools = {
        "knn_readers": {"errors": 0, "qps": 500, "threads": 50},
        "writers": {"errors": 0, "qps": 200, "threads": 50},
        "ann_readers": {"errors": 0, "qps": 100, "threads": 20},
    }
    checks = {
        "index_row_count_stable": True,
        "knn_result_stable": True,
        "no_deadlock": True,
        "all_threads_clean_exit": True,
    }
    assert mod._evaluate_pass(pools, checks) is True


def test_evaluate_pass_errors_fail():
    mod = _load()
    pools = {"knn_readers": {"errors": 3, "qps": 500, "threads": 50}}
    checks = {"index_row_count_stable": True, "knn_result_stable": True,
              "no_deadlock": True, "all_threads_clean_exit": True}
    assert mod._evaluate_pass(pools, checks) is False


def test_evaluate_pass_check_fail():
    mod = _load()
    pools = {"knn_readers": {"errors": 0, "qps": 500, "threads": 50}}
    checks = {"index_row_count_stable": False, "knn_result_stable": True,
              "no_deadlock": True, "all_threads_clean_exit": True}
    assert mod._evaluate_pass(pools, checks) is False


def test_evaluate_pass_silent_pool_fail():
    """A pool that produced zero throughput (all workers crashed) must fail."""
    mod = _load()
    pools = {"knn_readers": {"errors": 0, "qps": 0.0, "threads": 50}}
    checks = {"index_row_count_stable": True, "knn_result_stable": True,
              "no_deadlock": True, "all_threads_clean_exit": True}
    assert mod._evaluate_pass(pools, checks) is False


def test_result_json_structure():
    """_build_result produces valid JSON with all required top-level keys."""
    mod = _load()
    pools = {
        "knn_readers": {"threads": 50, "qps": 1200.0, "errors": 0, "p50_ms": 5.1, "p99_ms": 8.2},
        "writers":     {"threads": 50, "qps": 300.0,  "errors": 0},
        "ann_readers": {"threads": 20, "qps": 280.0,  "errors": 0, "p50_ms": 9.0, "p99_ms": 14.1},
    }
    checks = {
        "index_row_count_stable": True,
        "knn_result_stable":      True,
        "no_deadlock":            True,
        "all_threads_clean_exit": True,
    }
    import json, tempfile
    result = mod._build_result("9.7", "component", 120.0, True, pools, checks)
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(result, f)
        tmp = f.name
    try:
        with open(tmp) as f:
            loaded = json.load(f)
        assert loaded["passed"] is True
        assert loaded["pools"]["knn_readers"]["errors"] == 0
        assert loaded["workload"] == "concurrent_stress"
        assert loaded["ann_rewrite_active"] is True
        assert "pools" in loaded and "checks" in loaded
    finally:
        os.unlink(tmp)


def test_result_json_fails_on_errors():
    """_build_result sets passed=False when any pool has errors."""
    mod = _load()
    pools = {"knn_readers": {"errors": 5, "qps": 500, "threads": 50}}
    checks = {"index_row_count_stable": True, "knn_result_stable": True,
              "no_deadlock": True, "all_threads_clean_exit": True}
    result = mod._build_result("9.7", "component", 60.0, False, pools, checks)
    assert result["passed"] is False
