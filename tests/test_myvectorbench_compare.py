import importlib.util
import json
import os
import tempfile
import pytest


def _load_compare():
    path = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'myvectorbench-compare.py')
    spec = importlib.util.spec_from_file_location("myvectorbench_compare", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


compare_mod = _load_compare()
parse_threshold = compare_mod.parse_threshold
check_threshold = compare_mod.check_threshold
compare = compare_mod.compare


def test_parse_threshold_percent_upper():
    mode, val = parse_threshold('+25%')
    assert mode == 'percent_upper'
    assert abs(val - 0.25) < 1e-9


def test_parse_threshold_percent_lower():
    mode, val = parse_threshold('-25%')
    assert mode == 'percent_lower'
    assert abs(val - (-0.25)) < 1e-9


def test_parse_threshold_absolute_negative():
    mode, val = parse_threshold('-0.05')
    assert mode == 'absolute'
    assert abs(val - (-0.05)) < 1e-9


def test_check_no_breach_percent_upper():
    breached, _ = check_threshold(100.0, 120.0, '+25%')
    assert not breached


def test_check_breach_percent_upper():
    breached, _ = check_threshold(100.0, 130.0, '+25%')
    assert breached


def test_check_no_breach_percent_lower():
    # 440 → 338 is -23.2%, within the -25% threshold
    breached, _ = check_threshold(440.0, 338.0, '-25%')
    assert not breached


def test_check_breach_percent_lower():
    # 440 → 320 is -27.3%, exceeds the -25% threshold
    breached, _ = check_threshold(440.0, 320.0, '-25%')
    assert breached


def test_check_no_breach_absolute():
    # delta = -0.042, within -0.05
    breached, _ = check_threshold(0.942, 0.900, '-0.05')
    assert not breached


def test_check_breach_absolute():
    # delta = -0.062, exceeds -0.05
    breached, _ = check_threshold(0.942, 0.880, '-0.05')
    assert breached


def _make_json(tmp, name, metrics):
    path = os.path.join(tmp, name)
    data = {
        "git_ref": "v1.26.5.1",
        "mysql_version": "8.4",
        "build_path": "component",
        "timestamp": "2026-05-24T08:30:00Z",
        "runner": "test",
        "dataset": "synthetic",
        "workload_params": {"rows": 10000, "dim": 128},
        "metrics": metrics,
    }
    with open(path, 'w') as f:
        json.dump(data, f)
    return path


def _make_config(tmp, thresholds_yaml):
    path = os.path.join(tmp, 'myvectorbench.yml')
    with open(path, 'w') as f:
        f.write(f"thresholds:\n{thresholds_yaml}\n")
    return path


def test_compare_all_pass(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        baseline = _make_json(tmp, 'baseline.json', {
            'index_build_time_s': 12.0,
            'insert_qps': 440,
            'knn_qps': 810,
            'knn_p99_ms': 3.2,
            'recall_at_10': 0.942,
        })
        current = _make_json(tmp, 'current.json', {
            'index_build_time_s': 12.8,   # +6.7%, under +25%
            'insert_qps': 435,            # -1.1%, under -25%
            'knn_qps': 810,               # 0%, well within -25%
            'knn_p99_ms': 3.4,            # +6.3%, under +30%
            'recall_at_10': 0.940,        # -0.002, under -0.05
        })
        cfg = _make_config(tmp,
            "  index_build_time_s: +25%\n"
            "  insert_qps: -25%\n"
            "  knn_qps: -25%\n"
            "  knn_p99_ms: +30%\n"
            "  recall_at_10: -0.05\n"
        )
        rc = compare(baseline, current, cfg)
        captured = capsys.readouterr()
    assert "PASS: all metrics within threshold." in captured.out
    assert "## myvectorbench" in captured.out
    assert rc == 0


def test_compare_breach(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        baseline = _make_json(tmp, 'baseline.json', {'insert_qps': 440})
        current = _make_json(tmp, 'current.json', {'insert_qps': 300})  # -31.8%
        cfg = _make_config(tmp, "  insert_qps: -25%\n")
        rc = compare(baseline, current, cfg)
        captured = capsys.readouterr()
    assert "FAIL: one or more metrics exceeded threshold." in captured.out
    assert rc == 1


def test_compare_knn_qps_breach(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        baseline = _make_json(tmp, 'baseline.json', {'knn_qps': 810})
        current = _make_json(tmp, 'current.json', {'knn_qps': 600})  # -25.9%, exceeds -25%
        cfg = _make_config(tmp, "  knn_qps: -25%\n")
        rc = compare(baseline, current, cfg)
        captured = capsys.readouterr()
    assert "FAIL: one or more metrics exceeded threshold." in captured.out
    assert rc == 1


def test_compare_no_baseline(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        current = _make_json(tmp, 'current.json', {'insert_qps': 440})
        cfg = _make_config(tmp, "  insert_qps: -25%\n")
        rc = compare(os.path.join(tmp, 'nonexistent.json'), current, cfg)
        captured = capsys.readouterr()
    assert "NO_BASELINE" in captured.err
    assert rc == 0  # missing baseline → NO_BASELINE, not a failure


def test_compare_knn_ann_zero_baseline_skips(capsys):
    """knn_ann_qps=0.0 baseline → N/A row, no breach (workload was broken at baseline time)."""
    with tempfile.TemporaryDirectory() as tmp:
        baseline = _make_json(tmp, 'baseline.json', {'knn_ann_qps': 0.0})
        current = _make_json(tmp, 'current.json', {'knn_ann_qps': 45.2})
        cfg = _make_config(tmp, "  knn_ann_qps: -25%\n")
        rc = compare(baseline, current, cfg)
        captured = capsys.readouterr()
    assert rc == 0
    assert 'N/A' in captured.out


def test_compare_knn_ann_null_latency_skips(capsys):
    """knn_ann_p50_ms=null in baseline → N/A row, no breach."""
    with tempfile.TemporaryDirectory() as tmp:
        baseline = _make_json(tmp, 'baseline.json', {'knn_ann_p50_ms': None})
        current = _make_json(tmp, 'current.json', {'knn_ann_p50_ms': 21.3})
        cfg = _make_config(tmp, "  knn_ann_p50_ms: +30%\n")
        rc = compare(baseline, current, cfg)
        captured = capsys.readouterr()
    assert rc == 0
    assert 'N/A' in captured.out


def test_compare_knn_ann_qps_breach(capsys):
    """knn_ann_qps drops >25% after fix → breach detected."""
    with tempfile.TemporaryDirectory() as tmp:
        baseline = _make_json(tmp, 'baseline.json', {'knn_ann_qps': 45.0})
        current = _make_json(tmp, 'current.json', {'knn_ann_qps': 32.0})  # -28.9%
        cfg = _make_config(tmp, "  knn_ann_qps: -25%\n")
        rc = compare(baseline, current, cfg)
        captured = capsys.readouterr()
    assert rc == 1
    assert "FAIL: one or more metrics exceeded threshold." in captured.out
    assert 'knn_ann_qps' in captured.out
